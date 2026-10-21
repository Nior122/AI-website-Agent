"""Phase 40 tests: API authentication, reviewer identity, input boundaries, and end-to-end safety.

Everything here runs against fictional ``TEST_FIXTURE`` state in temporary directories, on the loopback
interface, with local JSON/SQLite storage. The suite covers the four risks the earlier phases left open:

1. Authentication: every route that reads or mutates business state requires the shared bearer token;
   missing, malformed, wrong and duplicated credentials are one indistinguishable refusal that never
   echoes the credential; ``/healthz`` stays the only public route; unsupported verbs answer JSON 405;
   and a bind reachable outside the machine refuses a short or placeholder token.
2. Reviewer identity: an automation caller can record a decision but can never *assert* that a human
   identity was verified - neither through the HTTP API nor through the store directly.
3. Input boundaries: identifiers, review text, payload size and framing, and review-package export
   directories are validated before they reach state, SQL parameters, or the filesystem, and every
   refusal carries sanitized, bounded text instead of an internal path or a stack trace.
4. End-to-end: lead -> project -> build -> validation -> review package -> human decision ->
   authorization -> dry run -> operator views, through the real CLI, worker, and HTTP entry points,
   with no external process, no publish, and no write outside the disposable fixture directory.

No real provider, publish, lead contact, paid service, or production database is used.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
import urllib.error
import urllib.parse
import urllib.request
import zlib

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment.authorization import read_approval, revoke_approval, verify_approval
from orchestration.api import serve
from orchestration.api import make_handler
from orchestration.jobs import JobService
from orchestration.observability import redact_local_paths, sanitize_diagnostic
from orchestration.workers import LocalPipelineWorker
from tracking.cli import run_cli
from tracking.database import DatabasePersistenceError
from tracking.store import (
    MAX_REVIEWER_CHARS,
    MAX_REVIEW_NOTES_CHARS,
    TrackingStore,
    clamp_reviewer_identity_claim,
    validate_bounded_text,
    validate_export_directory,
    validate_record_id,
)

FIXTURE_TOKEN = "fixture-only-token-0123456789abcdef"
CANARY = "canarysecretneverlogged"
SALON_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "qualified_lead_salon.json"
HTML = "<html><body><h1>Fictional Sole Stitch Salon — FICTIONAL TEST BUSINESS</h1></body></html>"


class _Fixture(unittest.TestCase):
    """Shared disposable state plus an HTTP client that exposes status, headers and body."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.output_root = self.root / "output"
        self.export_root = self.root / "preview-export"
        self.store = TrackingStore(self.state_path)
        self.lead = self._lead("Fictional Sole Stitch Salon")
        self.project = self.store.create_project(self.lead["lead_id"])
        self.project_id = self.project["project_id"]
        self.service = JobService(self.store)
        self._servers = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    # -- fixtures ----------------------------------------------------------------------------
    def _lead(self, name: str, *, store: TrackingStore | None = None) -> dict:
        target = store or self.store
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        digits = f"{abs(zlib.crc32(name.encode())) % 100000000:08d}"
        return target.create_lead({
            "business_name": name,
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": f"+234 800 {digits[:4]} {digits[4:]}",
            "website_status": "WEBSITE_OUTDATED",
            "existing_website": f"https://{slug}.example.invalid",
            "source_urls": [f"https://fixture.example.invalid/{slug}"],
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })

    def build_dir(self, build_id: str, *, lead_id: str | None = None) -> Path:
        return self.output_root / (lead_id or self.lead["lead_id"]) / build_id

    def _build_and_validate(self, build_id: str, validation_id: str, *, html: str = HTML) -> str:
        directory = self.build_dir(build_id)
        directory.mkdir(parents=True, exist_ok=True)
        index = directory / "index.html"
        index.write_text(html, encoding="utf-8")
        (directory / "build.json").write_text(json.dumps({
            "build_id": build_id, "lead_id": self.lead["lead_id"], "status": "COMPLETED",
            "data_classification": "TEST_FIXTURE", "output_location": str(index),
            "validation": {"validation_id": validation_id, "lead_id": self.lead["lead_id"],
                           "build_id": build_id, "status": "PASS"},
        }), encoding="utf-8")
        self.store.record_build(self.project_id, {
            "build_id": build_id, "lead_id": self.lead["lead_id"], "status": "COMPLETED",
            "template_id": "restaurant-menu-v1", "data_classification": "TEST_FIXTURE",
            "output_location": str(index),
        })
        self.store.record_validation(self.project_id, {
            "validation_id": validation_id, "build_id": build_id, "status": "PASS", "score": 100,
        })
        return build_id

    def _project(self, store: TrackingStore | None = None) -> dict:
        return (store or self.store).state["projects"][self.project_id]

    # -- transports --------------------------------------------------------------------------
    def _http(self, token: str = FIXTURE_TOKEN, *, worker=None, store: TrackingStore | None = None):
        """Serve the real handler on loopback and return (request, raw) clients."""
        handler = make_handler(
            JobService(store or self.store),
            token,
            worker=worker,
            output_root=self.output_root,
            export_root=self.export_root,
            repository_root=self.root,
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self._servers.append(httpd)
        port = httpd.server_address[1]

        def request(method: str, path: str, body: dict | None = None, *, authenticated: bool = True,
                    override_token: str | None = None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {override_token if override_token is not None else token}"
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
            last_exc: BaseException | None = None
            for _attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=10) as response:
                        return response.status, dict(response.headers), json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as error:
                    raw = error.read().decode("utf-8")
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {"raw": raw}
                    return error.code, dict(error.headers), payload
                except (ConnectionAbortedError, ConnectionResetError, OSError) as exc:
                    # Windows may briefly abort connections when a previous test's server daemon
                    # thread is still winding down on the same port (WinError 10053 / 10054).
                    last_exc = exc
                    import time
                    time.sleep(0.05)
            raise AssertionError(f"request({method} {path}) failed after 3 attempts: {last_exc}") from last_exc

        def raw(text: str) -> tuple[int, str, str]:
            """Send a hand-built request so malformed framing reaches the server unchanged."""
            with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
                sock.sendall(text.encode("latin-1"))
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                reader = sock.makefile("rb")
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = reader.read1(4096)
                    if not chunk:
                        break
                    head += chunk
                body = reader.read()
            status_line, _, rest = head.decode("latin-1").partition("\r\n")
            headers_text, _, body_text = rest.partition("\r\n\r\n")
            return int(status_line.split(" ")[1]), headers_text, (body_text + body.decode("utf-8", "replace")).strip()

        def authorized_raw(headers: str, body: str = "") -> tuple[int, str, str]:
            return raw(
                f"POST /jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {FIXTURE_TOKEN}\r\n"
                f"{headers}Connection: close\r\n\r\n{body}"
            )

        return request, raw, authorized_raw

    def _cli(self, *argv: str, state_path: Path | None = None) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_cli(["--state-path", str(state_path or self.state_path), *argv], stdout=out, stderr=err)
        text = out.getvalue()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text}
        return code, payload, err.getvalue()

    def _review_payload(self, decision: str = "APPROVED_FOR_PREVIEW", **extra) -> dict:
        assigned = self._project()["assigned_build_id"]
        payload = {
            "decision": decision,
            "notes": "Checked the fictional salon draft against the fixture brief; copy and contact block are correct.",
            "reviewer": "operator:fixture",
            "confirm": f"{self.project_id}:{assigned}:{decision}",
        }
        payload.update(extra)
        return payload


# ---------------------------------------------------------------------------------------------
# 1. Authentication on every state-touching route
# ---------------------------------------------------------------------------------------------
class ApiAuthenticationTests(_Fixture):
    def test_every_state_touching_route_requires_the_bearer_token(self) -> None:
        self._build_and_validate("build-0001", "validation-0001")
        request, _, _ = self._http()
        protected = [
            ("GET", "/health"), ("GET", "/readiness"), ("GET", "/attention"),
            ("GET", "/deployment-status"), ("GET", "/jobs"), ("GET", "/jobs/job-lead-1"),
            ("GET", "/jobs/job-lead-1/history"), ("GET", "/jobs/job-lead-1/readiness"),
            ("GET", "/leads"), ("GET", f"/leads/{self.lead['lead_id']}"),
            ("GET", "/projects"), ("GET", f"/projects/{self.project_id}"),
            ("GET", f"/projects/{self.project_id}/snapshot"),
            ("GET", f"/projects/{self.project_id}/artifact"),
            ("GET", f"/projects/{self.project_id}/review-package"),
            ("GET", f"/projects/{self.project_id}/deployment-authorization"),
            ("POST", "/jobs"), ("POST", "/jobs/recover-stale"), ("POST", "/jobs/job-lead-1/run"),
            ("POST", "/jobs/job-lead-1/next"), ("POST", "/jobs/job-lead-1/retry"),
            ("POST", "/jobs/job-lead-1/complete"), ("POST", "/jobs/job-lead-1/fail"),
            ("POST", "/leads/deduplicate"), ("POST", f"/projects/{self.project_id}/review"),
            ("POST", f"/projects/{self.project_id}/review-package"),
        ]
        for method, path in protected:
            with self.subTest(route=f"{method} {path}"):
                status, headers, body = request(method, path, {} if method == "POST" else None, authenticated=False)
                self.assertEqual(status, 401, f"{method} {path} answered {status}: {body}")
                self.assertEqual(body, {"error": "Unauthorized"}, "no state or hint may leak before auth")
                self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))

    def test_missing_malformed_wrong_and_duplicated_credentials_are_one_refusal(self) -> None:
        _, raw, authorized_raw = self._http()
        refusals = []
        cases = (
            ("missing", ""),
            ("wrong-scheme", "Authorization: Basic ZlhpY3Rpb25hbA==\r\n"),
            ("empty-bearer", "Authorization: Bearer\r\n"),
            ("wrong-token", f"Authorization: Bearer {CANARY}\r\n"),
            ("lowercase-scheme", f"Authorization: bearer {FIXTURE_TOKEN}\r\n"),
            ("duplicated", f"Authorization: Bearer {FIXTURE_TOKEN}\r\nAuthorization: Bearer {CANARY}\r\n"),
        )
        for label, headers in cases:
            with self.subTest(credential=label):
                status, _, body_text = raw(
                    f"GET /jobs HTTP/1.1\r\nHost: 127.0.0.1\r\n{headers}Connection: close\r\n\r\n"
                )
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(body_text), {"error": "Unauthorized"})
                self.assertNotIn(CANARY, body_text)
                self.assertNotIn(FIXTURE_TOKEN, body_text)
                refusals.append(body_text)
        self.assertEqual(len(set(refusals)), 1, "refusals must not reveal which part of the credential failed")
        # Only the documented `Bearer <token>` form is accepted; a scheme-spelling variant is a refusal,
        # not a lenient match, so a proxy cannot accidentally widen what counts as a credential.
        request, _, _ = self._http()
        self.assertEqual(request("GET", "/jobs")[0], 200, "the correct credential still works")

    def test_an_unconfigured_token_refuses_everything_except_liveness(self) -> None:
        # A handler built without a token must fail closed rather than default to an open surface.
        request, _, _ = self._http(token="")
        for path in ("/jobs", "/attention", "/readiness", f"/projects/{self.project_id}"):
            with self.subTest(path=path):
                self.assertEqual(request("GET", path, authenticated=False)[0], 401)
                self.assertEqual(request("GET", path)[0], 401, "no token means no accepted credential")
        self.assertEqual(request("GET", "/healthz")[0:1], (200,), "liveness stays public by design")

    def test_healthz_is_public_and_carries_no_operational_data(self) -> None:
        self._build_and_validate("build-0001", "validation-0001")
        request, _, _ = self._http()
        status, _, body = request("GET", "/healthz", authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"}, "liveness only proves the process answers")
        text = json.dumps(body)
        for forbidden in ("state", "job", "database", "token", str(self.root)):
            self.assertNotIn(forbidden, text)

    def test_auth_refusals_are_logged_without_the_credential(self) -> None:
        log_dir = self.root / "logs"
        request, _, _ = self._http()
        with mock.patch.dict(os.environ, {
            "AI_WEBSITE_AGENT_LOG_DIR": str(log_dir), "AI_WEBSITE_AGENT_LOG_LEVEL": "INFO",
        }):
            status, _, _ = request("GET", "/jobs", authenticated=True, override_token=f"{CANARY}wrong")
        self.assertEqual(status, 401)
        recorded = (log_dir / "operations.jsonl").read_text(encoding="utf-8")
        self.assertIn("AUTH_FAILED", recorded)
        self.assertIn("http.auth", recorded)
        self.assertIn("/jobs", recorded)
        for secret in (CANARY, FIXTURE_TOKEN, "Bearer"):
            self.assertNotIn(secret, recorded, "the log must never carry credential material")

    def test_unsupported_verbs_answer_json_405_not_the_stdlib_error_page(self) -> None:
        request, _, _ = self._http()
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = request(method, f"/projects/{self.project_id}")
                self.assertEqual(status, 405)
                if method == "HEAD":
                    continue  # a HEAD answer carries no body by definition
                self.assertEqual(body["error_code"], "METHOD_NOT_ALLOWED")
                self.assertIn("GET, POST", headers.get("Allow", ""))
                self.assertNotIn("<html", json.dumps(body))
                self.assertNotIn("Traceback", json.dumps(body))
        status, _, _ = request("PUT", f"/projects/{self.project_id}", {}, authenticated=False)
        self.assertEqual(status, 401, "a verb probe is still an unauthenticated request")

    def test_a_weak_shared_token_is_refused_for_a_reachable_bind(self) -> None:
        for label, token in (("short", "abc123"), ("placeholder", "ChangeMe"), ("empty-ish", "   ")):
            with self.subTest(token=label):
                with mock.patch.dict(os.environ, {
                    "AI_WEBSITE_AGENT_API_TOKEN": token, "AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK": "true",
                }, clear=False):
                    with self.assertRaises(RuntimeError) as caught:
                        serve(state_path=str(self.root / "never-created.json"), host="0.0.0.0", port=0,
                              allow_non_loopback=True)
                    message = str(caught.exception)
                    self.assertTrue("required" in message or "too weak" in message, message)
                    self.assertNotIn(token.strip() or "changeme", message, "the refusal must not echo the credential")
        # A strong token is accepted for the same bind; the test asserts the refusal reason, not a token value.
        with mock.patch.dict(os.environ, {"AI_WEBSITE_AGENT_API_TOKEN": "x" * 40}, clear=False):
            with mock.patch("orchestration.api.ThreadingHTTPServer") as server:
                serve(state_path=str(self.root / "serve-state.json"), host="0.0.0.0", port=0,
                      allow_non_loopback=True, export_root=str(self.export_root),
                      output_root=str(self.output_root))
        server.return_value.serve_forever.assert_called_once()

    def test_local_loopback_development_keeps_working_with_any_token(self) -> None:
        with mock.patch.dict(os.environ, {"AI_WEBSITE_AGENT_API_TOKEN": "local-dev"}, clear=False):
            with mock.patch("orchestration.api.ThreadingHTTPServer") as server:
                serve(state_path=str(self.root / "dev-state.json"), host="127.0.0.1", port=0,
                      export_root=str(self.export_root), output_root=str(self.output_root))
        self.assertTrue(server.called)


# ---------------------------------------------------------------------------------------------
# 2. Payload and identifier boundaries
# ---------------------------------------------------------------------------------------------
class RequestBoundaryTests(_Fixture):
    def test_an_oversized_body_is_refused_before_it_is_read(self) -> None:
        _, _, authorized_raw = self._http()
        declared = 5 * 1024 * 1024
        status, _, body_text = authorized_raw(f"Content-Length: {declared}\r\nContent-Type: application/json\r\n",
                                              "x" * 32)
        self.assertEqual(status, 413)
        payload = json.loads(body_text)
        self.assertEqual(payload["error_code"], "REQUEST_BODY_TOO_LARGE")
        self.assertNotIn("Traceback", body_text)
        # A body at the limit is accepted, so the boundary is a size policy and not a broken parser.
        small = {"job_type": "BUILD", "job_id": "job-size-check", "project_id": self.project_id}
        status, _, body = self._http()[0]("POST", "/jobs", small)
        self.assertIn(status, (200, 201, 400, 409), json.dumps(body))

    def test_chunked_bodies_are_refused_with_a_useful_400(self) -> None:
        _, _, authorized_raw = self._http()
        status, _, body_text = authorized_raw("Transfer-Encoding: chunked\r\n", "0\r\n\r\n")
        self.assertEqual(status, 400)
        payload = json.loads(body_text)
        self.assertEqual(payload["error_code"], "INVALID_REQUEST")
        self.assertIn("Chunked", payload["error"])

    def test_malformed_framing_and_bodies_are_sanitized_400s(self) -> None:
        _, _, authorized_raw = self._http()
        status, _, body_text = authorized_raw("Content-Length: twelve\r\nContent-Type: application/json\r\n", "{}")
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", json.loads(body_text)["error"])

        status, _, body_text = authorized_raw("Content-Length: 400\r\nContent-Type: application/json\r\n", "{}")
        self.assertEqual(status, 400)
        self.assertIn("truncated", json.loads(body_text)["error"])

        status, _, body_text = authorized_raw("Content-Length: 4\r\nContent-Type: application/json\r\n",
                                              "\x80\x81\x82\x83")
        self.assertEqual(status, 400)
        self.assertIn("UTF-8", json.loads(body_text)["error"])
        self.assertNotIn(str(self.root), body_text)

        # Malformed JSON and a non-object body are exercised through the same raw path.
        status, _, body_text = authorized_raw("Content-Length: 9\r\nContent-Type: application/json\r\n", "{not json")
        self.assertEqual(status, 400)
        self.assertIn("valid JSON", json.loads(body_text)["error"])
        status, _, body_text = authorized_raw("Content-Length: 2\r\nContent-Type: application/json\r\n", "[]")
        self.assertEqual(status, 400)
        self.assertIn("JSON object", json.loads(body_text)["error"])

    def test_unsafe_path_identifiers_are_400_and_unknown_records_are_404(self) -> None:
        request, _, _ = self._http()
        for path in (
            "/projects/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "/projects/..%5c..%5cwindows",
            "/jobs/lead%20001",
            "/leads/lead-0001%00",
            "/projects/proj%24x/artifact",
        ):
            with self.subTest(path=path):
                status, _, body = request("GET", path)
                self.assertEqual(status, 400, json.dumps(body))
                self.assertEqual(body["error_code"], "INVALID_REQUEST")
                self.assertNotIn("/etc", body["error"])
                self.assertNotIn("Traceback", json.dumps(body))
        for path in ("/jobs/job-never-created", "/leads/lead-9999-not-real"):
            with self.subTest(path=path):
                status, _, body = request("GET", path)
                self.assertEqual(status, 404, json.dumps(body))
                self.assertEqual(body["error_code"], "RECORD_NOT_FOUND")
        status, _, body = request("POST", "/jobs", {"job_type": "BUILD", "job_id": "job-x",
                                                    "project_id": "/etc/passwd"})
        self.assertEqual(status, 400)
        self.assertIn("project_id", body["error"])
        self.assertNotIn("/etc/passwd", json.dumps(body), "the rejected value is not reflected verbatim")

    def test_validators_reject_malformed_ids_and_oversized_text(self) -> None:
        for bad in ("", " ", "with space", "../escape", "/absolute/id", "a" * 121, "id\nwith-newline", "id\x00"):
            with self.subTest(identifier=repr(bad)):
                with self.assertRaises(ValueError):
                    validate_record_id(bad, kind="project_id")
        self.assertEqual(validate_record_id(" project-0002 "), "project-0002")
        self.assertEqual(validate_record_id("build-2026-10-13T012227z"), "build-2026-10-13T012227z")
        with self.assertRaises(ValueError):
            validate_bounded_text("x" * (MAX_REVIEW_NOTES_CHARS + 1), field="notes", limit=MAX_REVIEW_NOTES_CHARS)
        with self.assertRaises(ValueError):
            validate_bounded_text("   ", field="reviewer", limit=MAX_REVIEWER_CHARS)
        self.assertEqual(validate_bounded_text("  ok  ", field="notes", limit=40), "ok")


# ---------------------------------------------------------------------------------------------
# 3. Reviewer identity stays honest
# ---------------------------------------------------------------------------------------------
class ReviewerIdentityTests(_Fixture):
    def test_the_api_cannot_self_assert_a_verified_human_identity(self) -> None:
        self._build_and_validate("build-0001", "validation-0001")
        request, _, _ = self._http()
        status, _, body = request("POST", f"/projects/{self.project_id}/review", self._review_payload(
            identity_verified=True, reviewer_type="HUMAN", reviewer_id="operator:fixture"
        ))
        self.assertEqual(status, 400, json.dumps(body))
        self.assertEqual(body["error_code"], "IDENTITY_ASSERTION_REFUSED")
        self.assertNotIn(FIXTURE_TOKEN, json.dumps(body))
        self.assertEqual(self.store.get_review_history(self.project_id), [], "a refused review records nothing")
        self.assertEqual(self._project()["review_status"], "PENDING")

        # A review carrying its own approval file cannot be smuggled in either.
        self.assertIsNone(read_approval(self.build_dir("build-0001")))

        status, _, body = request("POST", f"/projects/{self.project_id}/review", self._review_payload(
            reviewer_type="HUMAN", reviewer_id="operator:fixture", identity_verified=False
        ))
        self.assertEqual(status, 200, json.dumps(body))
        self.assertFalse(body["identity_verified"])
        self.assertEqual(body["identity_source"], "orchestration-api")
        self.assertFalse(body["deployed"])
        entry = self.store.get_review_history(self.project_id)[-1]
        self.assertFalse(entry["reviewer"]["identity_verified"])
        self.assertNotIn("identity_claim_refused", entry["reviewer"])
        approval = read_approval(self.build_dir("build-0001"))
        self.assertIsNotNone(approval, "the recorded decision still produces the preview capability")
        self.assertFalse(approval["identity_verified"])
        self.assertEqual(approval["reviewer_identity_source"], "orchestration-api")
        self.assertEqual(approval["build_id"], "build-0001")

    def test_the_store_clamps_a_verified_identity_claim_from_an_unverified_source(self) -> None:
        self._build_and_validate("build-0002", "validation-0002")
        self.store.record_review(
            self.project_id,
            "APPROVED_FOR_PREVIEW",
            "Operator notes recorded directly through the store for the identity-clamp check.",
            reviewer={"reviewer_id": "operator:fixture", "reviewer_type": "HUMAN", "identity_verified": True},
            identity_source="some-automation",
        )
        entry = self.store.get_review_history(self.project_id)[-1]
        self.assertFalse(entry["reviewer"]["identity_verified"])
        self.assertIn("no verified-identity mechanism", entry["reviewer"]["identity_claim_refused"])
        approval = read_approval(self.build_dir("build-0002"))
        self.assertFalse(approval["identity_verified"])
        self.assertFalse(approval["reviewer"]["identity_verified"])
        # The clamp is a pure function of (claim, source), and the verified-source set is still empty.
        clamped = clamp_reviewer_identity_claim({"identity_verified": True}, "some-automation")
        self.assertFalse(clamped["identity_verified"])
        self.assertIn("identity_claim_refused", clamped)
        self.assertEqual(clamp_reviewer_identity_claim("plain-name", "cli"), "plain-name")
        untouched = {"identity_verified": False}
        self.assertIs(clamp_reviewer_identity_claim(untouched, "x"), untouched)

    def test_review_input_validation_and_stale_confirmation(self) -> None:
        self._build_and_validate("build-0003", "validation-0003")
        request, _, _ = self._http()
        cases = {
            "empty notes": {"notes": "   "},
            "oversized notes": {"notes": "x" * (MAX_REVIEW_NOTES_CHARS + 1)},
            "oversized reviewer": {"reviewer": "r" * (MAX_REVIEWER_CHARS + 1)},
            "non-string notes": {"notes": {"nested": True}},
        }
        for label, patch in cases.items():
            with self.subTest(case=label):
                payload = {**self._review_payload(), **patch}
                status, _, body = request("POST", f"/projects/{self.project_id}/review", payload)
                self.assertEqual(status, 400, json.dumps(body))
                self.assertEqual(body["error_code"], "INVALID_REQUEST")
                self.assertNotIn("Traceback", json.dumps(body))
        # A lone 'reviewer_type' is not an error: the reviewer id is derived from the reviewer string and
        # the recorded identity stays unverified, so nothing in the record can be read as proof of a human.
        status, _, body = request("POST", f"/projects/{self.project_id}/review", self._review_payload(
            reviewer_type="HUMAN"
        ))
        self.assertEqual(status, 200, json.dumps(body))
        recorded_reviewer = body["review"]["reviewer"]
        self.assertEqual(recorded_reviewer["reviewer_type"], "HUMAN")
        self.assertEqual(recorded_reviewer["reviewer_id"], "operator:fixture")
        self.assertFalse(recorded_reviewer["identity_verified"])
        status, _, body = request("POST", f"/projects/{self.project_id}/review", self._review_payload("NOPE"))
        self.assertEqual(status, 400)
        self.assertIn("APPROVED_FOR_PREVIEW", body["error"], "the refusal lists the usable decisions")

        # A decision about a superseded build stays refused, and the reflected token stays bounded.
        stale = f"{self.project_id}:build-0003:APPROVED_FOR_PREVIEW"
        self._build_and_validate("build-0004", "validation-0004")
        status, _, body = request("POST", f"/projects/{self.project_id}/review", {
            "decision": "APPROVED_FOR_PREVIEW", "notes": "Approving the build I actually looked at.",
            "reviewer": "operator:fixture", "confirm": stale + ("-extra" * 60),
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "REVIEW_CONFIRMATION_MISMATCH")
        self.assertLessEqual(len(body["received_confirmation"]), 200)
        self.assertIn(f"{self.project_id}:build-0004:APPROVED_FOR_PREVIEW", body["expected_confirmation"])
        self.assertEqual(self._project()["review_status"], "PENDING", "a mismatched confirmation changes nothing")


# ---------------------------------------------------------------------------------------------
# 4. Filesystem boundaries for review-package exports
# ---------------------------------------------------------------------------------------------
class ExportConfinementTests(_Fixture):
    def setUp(self) -> None:
        super().setUp()
        self._build_and_validate("build-0001", "validation-0001")

    def test_a_request_cannot_write_a_review_package_outside_the_export_root(self) -> None:
        request, _, _ = self._http()
        outside = self.root / "escaped-export"
        for label, export_dir in (
            ("relative traversal", "../../escaped-export"),
            ("absolute path", str(outside)),
            ("dot segment inside", "./../escaped-export"),
            ("root-then-escape", f"{self.export_root.name}/../../escaped-export"),
            ("hidden directory", "../.ssh"),
        ):
            with self.subTest(export_dir=label):
                quoted = urllib.parse.quote(export_dir, safe="")
                status, _, body = request("GET", f"/projects/{self.project_id}/review-package?export_dir={quoted}")
                self.assertEqual(status, 400, json.dumps(body))
                self.assertEqual(body["error_code"], "EXPORT_PATH_NOT_ALLOWED")
                self.assertNotIn(str(self.root), body["error"], "the refusal must not map the filesystem")
                self.assertNotIn("Traceback", json.dumps(body))
                status, _, body = request("POST", f"/projects/{self.project_id}/review-package",
                                          {"export_dir": export_dir})
                self.assertEqual(status, 400, json.dumps(body))
                self.assertEqual(body["error_code"], "EXPORT_PATH_NOT_ALLOWED")
        self.assertFalse(outside.exists(), "nothing may be created outside the export root")
        self.assertFalse((self.root / ".ssh").exists())

    def test_a_named_directory_inside_the_export_root_still_works(self) -> None:
        request, _, _ = self._http()
        status, _, body = request("POST", f"/projects/{self.project_id}/review-package", {"export_dir": "salon-pkg-1"})
        self.assertEqual(status, 200, json.dumps(body))
        target = self.export_root / "salon-pkg-1"
        self.assertTrue((target / "index.html").is_file())
        self.assertTrue((target / "review-package.json").is_file())
        status, _, body = request("GET", f"/projects/{self.project_id}/review-package?export_dir=salon-pkg-1")
        self.assertEqual(status, 200, json.dumps(body))
        self.assertEqual(body["verification"]["status"], "VALID")
        # The default target stays <export_root>/<lead>-<project>, which needs no caller input at all.
        status, _, body = request("POST", f"/projects/{self.project_id}/review-package", {})
        self.assertEqual(status, 200, json.dumps(body))
        self.assertTrue((self.export_root / f"{self.lead['lead_id']}-{self.project_id}").is_dir())

    def test_a_symlink_in_the_export_root_cannot_be_used_to_escape(self) -> None:
        target = self.root / "outside-target"
        target.mkdir()
        self.export_root.mkdir(parents=True, exist_ok=True)
        link = self.export_root / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available in this environment")
        with self.assertRaises(ValueError):
            validate_export_directory("link", base_root=self.export_root, allowed_roots=[self.export_root])
        request, _, _ = self._http()
        status, _, body = request("POST", f"/projects/{self.project_id}/review-package", {"export_dir": "link/pkg"})
        self.assertEqual(status, 400, json.dumps(body))
        self.assertEqual(body["error_code"], "EXPORT_PATH_NOT_ALLOWED")
        self.assertEqual(list(target.iterdir()), [], "no file may land behind the symlink")

    def test_the_local_operator_paths_are_unchanged(self) -> None:
        # The confinement is at the network boundary: the store-level helper still honours an explicit
        # operator directory, which is what the CLI uses with the operator's own authority.
        outside = self.root / "operator-choice"
        resolved = validate_export_directory(outside, base_root=self.export_root, allowed_roots=[outside])
        self.assertEqual(resolved, outside.resolve())


# ---------------------------------------------------------------------------------------------
# 5. Storage safety and sanitized failures
# ---------------------------------------------------------------------------------------------
class StorageAndErrorTests(_Fixture):
    def test_injection_shaped_values_stay_data_and_never_reach_sql_as_code(self) -> None:
        db_file = self.root / "tracking.sqlite"
        store = TrackingStore(self.root / "db-state.json", database_url=f"sqlite:///{db_file}")
        hostile = "Robert'); DROP TABLE records; --"
        lead = self._lead(f"{hostile} Fictional Injection Co", store=store)
        project = store.create_project(lead["lead_id"])
        directory = self.build_dir("build-inj-1", lead_id=lead["lead_id"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_text(HTML, encoding="utf-8")
        store.record_build(project["project_id"], {
            "build_id": "build-inj-1", "lead_id": lead["lead_id"], "status": "COMPLETED",
            "template_id": "restaurant-menu-v1", "data_classification": "TEST_FIXTURE",
            "output_location": str(directory / "index.html"),
        })
        store.record_validation(project["project_id"], {
            "validation_id": "validation-inj-1", "build_id": "build-inj-1", "status": "PASS", "score": 100,
        })
        request, _, _ = self._http(store=store)
        status, _, body = request("POST", f"/projects/{project['project_id']}/review", {
            "decision": "APPROVED_FOR_PREVIEW",
            "notes": f"{hostile} review notes",
            "reviewer": hostile,
            "confirm": f"{project['project_id']}:build-inj-1:APPROVED_FOR_PREVIEW",
        })
        self.assertEqual(status, 200, json.dumps(body))
        entry = store.get_review_history(project["project_id"])[-1]
        self.assertEqual(entry["reviewer"], hostile, "the value is stored verbatim, as inert text")
        store.db_backend.verify_schema()  # every tracked collection still exists
        reloaded = TrackingStore(self.root / "db-state.json", database_url=f"sqlite:///{db_file}")
        self.assertEqual(reloaded.snapshot(project["project_id"])["lead"]["business_name"],
                         f"{hostile} Fictional Injection Co")
        self.assertEqual(len(reloaded.state["leads"]), 1)
        status, _, body = request("GET", f"/projects/{project['project_id']}/snapshot")
        self.assertEqual(status, 200, json.dumps(body))
        # A hostile identifier is refused before it can become a path or a query parameter.
        status, _, body = request("GET", "/projects/" + urllib.parse.quote(hostile))
        self.assertEqual(status, 400)
        self.assertEqual(body["error_code"], "INVALID_REQUEST")

    def test_storage_and_unexpected_failures_are_sanitized(self) -> None:
        request, _, _ = self._http()
        leaky = (f"could not open {self.root / 'state.json'} for writing password=hunter2 "
                 "at postgres://user:hunter2@db.internal:5432/state")
        with mock.patch.object(type(self.store), "snapshot", side_effect=DatabasePersistenceError(leaky)):
            status, _, body = request("GET", f"/projects/{self.project_id}/snapshot")
        self.assertEqual(status, 503)
        self.assertEqual(body["error_code"], "STORAGE_UNAVAILABLE")
        text = json.dumps(body)
        self.assertNotIn(str(self.root), text, "no internal filesystem path in a response")
        self.assertNotIn("hunter2", text, "no credential in a response")
        self.assertNotIn("Traceback", text)

        with mock.patch.object(type(self.store), "snapshot", side_effect=RuntimeError(f"boom {self.root / 'x'}")):
            status, _, body = request("GET", f"/projects/{self.project_id}/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(body["error_code"], "INTERNAL_ERROR")
        self.assertNotIn(str(self.root), json.dumps(body))
        self.assertIn("RuntimeError", body["detail"], "the class name helps an operator; the path does not")

    def test_path_redaction_keeps_urls_and_relative_paths_usable(self) -> None:
        self.assertEqual(
            redact_local_paths("published to https://ai-website-agent-preview.vercel.app ok"),
            "published to https://ai-website-agent-preview.vercel.app ok",
        )
        self.assertEqual(
            redact_local_paths("artifact at output/lead-0001/build-x/index.html"),
            "artifact at output/lead-0001/build-x/index.html",
        )
        self.assertEqual(redact_local_paths(sanitize_diagnostic(f"missing {self.root}/state.json")),
                         "missing [LOCAL-PATH-REDACTED]")


# ---------------------------------------------------------------------------------------------
# 6. The approval gate stays closed through every entry point
# ---------------------------------------------------------------------------------------------
class ApprovalBoundaryTests(_Fixture):
    """API, CLI, worker, and retry all have to pass the same capability check - or refuse."""

    def setUp(self) -> None:
        super().setUp()
        self._build_and_validate("build-0001", "validation-0001")

    def _approve(self) -> None:
        request, _, _ = self._http()
        status, _, body = request("POST", f"/projects/{self.project_id}/review", self._review_payload())
        self.assertEqual(status, 200, json.dumps(body))

    def test_automation_api_cannot_enqueue_a_publish_job(self) -> None:
        request, _, _ = self._http()
        status, _, body = request("POST", "/jobs", {"job_type": "DEPLOY", "job_id": "job-deploy-now",
                                                    "project_id": self.project_id, "run": True})
        self.assertEqual(status, 400, json.dumps(body))
        self.assertIn("not permitted", body["error"])
        self.assertNotIn("DEPLOY", str(self.store.state["jobs"].keys()))

    def test_worker_and_retry_refuse_without_and_with_a_revoked_capability(self) -> None:
        worker = LocalPipelineWorker(repository_root=self.root, output_root=self.output_root)
        service = JobService(self.store)
        service.create("DEPLOY", job_id="job-deploy-worker", project_id=self.project_id, lead_id=self.lead["lead_id"],
                       build_id="build-0001", dry_run=False)
        blocked = service.run("job-deploy-worker", worker)
        self.assertEqual(blocked["status"], "BLOCKED", "the approval gate stops the job before the provider runs")
        self.assertEqual(blocked["error_code"], "APPROVAL_MISSING")
        self.assertEqual(blocked["attempts"], 0, "a blocked publish consumes no attempt")
        self.assertIsNone(blocked["output"], "no provider output exists when the gate refused")
        self.assertTrue(blocked["requires_operator_action"])

        # A retry re-runs the same checks; it cannot manufacture authorization.
        request, _, _ = self._http()
        status, _, body = request("POST", "/jobs/job-deploy-worker/retry", {})
        self.assertIn(status, (200, 409), json.dumps(body))
        retried = service.run("job-deploy-worker", worker)
        self.assertEqual(retried["status"], "BLOCKED")
        self.assertEqual(retried["error_code"], "APPROVAL_MISSING")

        # Even an approved capability only permits the preview action for this exact build and artifact,
        # and it still requires a linked preview project - so the publish attempt fails, it does not publish.
        self._approve()
        service.create("DEPLOY", job_id="job-deploy-after-approval", project_id=self.project_id,
                       lead_id=self.lead["lead_id"], build_id="build-0001", dry_run=False)
        approved = service.run("job-deploy-after-approval", worker)
        self.assertEqual(approved["output"]["outcome"], "DEPLOYMENT_FAILED", "no preview project is linked here")
        self.assertIsNone(approved["output"].get("deployment_url"))

        # Tampering with the capability - including trying to upgrade the identity flag - fails the digest.
        approval_path = self.build_dir("build-0001") / "deployment-approval.json"
        record = json.loads(approval_path.read_text(encoding="utf-8"))
        record["identity_verified"] = True
        approval_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        authorized, code, _, _ = verify_approval(self.build_dir("build-0001"))
        self.assertFalse(authorized)
        self.assertEqual(code, "APPROVAL_TAMPERED")

        # A revoked approval stays revoked, and an artifact that changed under it is stale.
        approval_path.unlink()
        self._approve()
        revoke_approval(self.build_dir("build-0001"), reason="test revocation", revoked_by="operator:fixture")
        authorized, code, _, _ = verify_approval(self.build_dir("build-0001"))
        self.assertEqual((authorized, code), (False, "APPROVAL_REVOKED"))
        self.build_dir("build-0001").joinpath("index.html").write_text(HTML + "<!-- changed -->", encoding="utf-8")
        # Re-materializing would need a new review; the existing capability is now bound to a stale artifact.
        self.store.record_review(
            self.project_id, "CHANGES_REQUESTED", "Artifact changed after approval; asking for a rebuild.",
            reviewer="operator:fixture",
        )
        authorized, code, _, _ = verify_approval(
            self.build_dir("build-0001"),
            manifest=json.loads((self.build_dir("build-0001") / "build.json").read_text(encoding="utf-8")),
        )
        self.assertFalse(authorized)
        self.assertIn(code, {"APPROVAL_REVOKED", "APPROVAL_ARTIFACT_STALE"})
        self.assertEqual(self._project()["review_status"], "CHANGES_REQUESTED")

    def test_cli_health_and_deployment_checks_never_grant_authorization(self) -> None:
        # Read-only operational surfaces must not change the gate answer.
        code, check, _ = self._cli("deploy-check", self.project_id)
        self.assertEqual(code, 1)
        self.assertEqual(check["code"], "APPROVAL_MISSING")
        request, _, _ = self._http()
        for path in ("/attention", "/deployment-status", "/health", "/readiness", "/jobs?status=FAILED"):
            self.assertEqual(request("GET", path)[0], 200, path)
        code_after, check_after, _ = self._cli("deploy-check", self.project_id)
        self.assertEqual((code_after, check_after["code"]), (1, "APPROVAL_MISSING"))
        self.assertFalse(self.build_dir("build-0001").joinpath("deployment-approval.json").exists(),
                         "monitoring must not materialize a capability")


# ---------------------------------------------------------------------------------------------
# 6. End-to-end fictional workflow across CLI, worker, and HTTP
# ---------------------------------------------------------------------------------------------
class EndToEndWorkflowTests(_Fixture):
    def test_fictional_lead_to_dry_run_across_every_entry_point(self) -> None:
        # A guard, not a fixture: the real workspace state is gitignored and must not be touched.
        real_state = REPO_ROOT / "data" / "tracking" / "state.json"
        before = real_state.read_bytes() if real_state.is_file() else b""
        e2e_state = self.root / "e2e-state.json"
        e2e_store = TrackingStore(e2e_state)

        # 1. One fictional test lead, through the real ingest CLI and the existing salon fixture.
        code, ingested, err = self._cli("ingest-lead", "--record", str(SALON_FIXTURE), "--create-project",
                                        state_path=e2e_state)
        self.assertEqual(code, 0, err)
        e2e_store = TrackingStore(e2e_state)
        lead_id, project_id = ingested["lead_id"], ingested["project_id"]
        self.assertEqual(e2e_store.state["leads"][lead_id]["data_classification"], "TEST_FIXTURE")

        # 2-4. Project, build, and validation through the CLI's own JobService + worker (salon template).
        code, built, err = self._cli("build", project_id, "--output-root", str(self.output_root),
                                     "--job-id", "job-e2e-build", state_path=e2e_state)
        self.assertEqual(code, 0, err)
        build_id, validation_id = built["build_id"], built["validation_id"]
        self.assertEqual(built["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(built["validation_status"], "PASS")
        artifact = self.build_dir(build_id, lead_id=lead_id) / "index.html"
        self.assertTrue(artifact.is_file(), "the fixture build really produced a local artifact")
        self.assertIn("FICTIONAL", artifact.read_text(encoding="utf-8").upper())

        # 5. Review evidence, exported through the API into the confined export root. The store is
        #    re-read so the API answers from what the CLI actually persisted.
        e2e_store = TrackingStore(e2e_state)
        request, _, _ = self._http(store=e2e_store)
        status, _, package = request("POST", f"/projects/{project_id}/review-package", {"export_dir": "e2e-pkg"})
        self.assertEqual(status, 200, json.dumps(package))
        self.assertEqual(package["verification"]["status"], "VALID")
        self.assertEqual(package["build_id"], build_id)

        # 6. The human decision, through the existing review mechanism. An API caller is authenticated
        #    as automation, so the decision is recorded honestly as identity-unverified.
        status, _, review = request("POST", f"/projects/{project_id}/review", {
            "decision": "APPROVED_FOR_PREVIEW",
            "notes": "Reviewed the fictional salon draft in the exported package; headline, services and "
                     "contact details are correct for this fixture. No real business was contacted.",
            "reviewer": "operator:e2e-fixture",
            "reviewer_type": "HUMAN",
            "reviewer_id": "operator:e2e-fixture",
            "build_id": build_id,
            "confirm": f"{project_id}:{build_id}:APPROVED_FOR_PREVIEW",
        })
        self.assertEqual(status, 200, json.dumps(review))
        self.assertFalse(review["identity_verified"], "an API decision is never reported as identity-verified")
        self.assertFalse(review["deployed"])
        self.assertEqual(review["review_status"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(review["current_stage"], "READY_FOR_REVIEW",
                         "an approval for preview never claims that something was deployed")
        self.assertEqual(review["review"]["build_id"], build_id)
        self.assertEqual(review["review"]["validation_id"], validation_id)

        # 7. Deployment authorization: bound to this exact build and artifact, for the preview action only.
        gate = review["deployment_authorization"]
        self.assertEqual(gate["build_id"], build_id)
        self.assertEqual(gate["action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(gate["data_classification"], "TEST_FIXTURE")
        build_dir = self.build_dir(build_id, lead_id=lead_id)
        approval = read_approval(build_dir)
        self.assertIsNotNone(approval)
        self.assertEqual(approval["build_id"], build_id)
        self.assertEqual(approval["validation_id"], validation_id)
        self.assertFalse(approval["identity_verified"])
        # The capability verifier answers with its own success code; the store-side gate answers with
        # APPROVED_FOR_DEPLOYMENT. Both must agree that this exact build may go to preview only.
        authorized, code, _, record = verify_approval(build_dir)
        self.assertTrue(authorized, code)
        self.assertEqual(code, "APPROVED")
        self.assertFalse(record["identity_verified"])

        # 8. The dry run, against a fixture preview link. It verifies and stages; it never publishes.
        (self.root / ".vercel").mkdir(exist_ok=True)
        (self.root / ".vercel" / "project.json").write_text(json.dumps(
            {"projectId": "prj_fixture_preview", "projectName": "ai-website-agent-preview"}), encoding="utf-8")
        code, dry, err = self._cli("prepare-preview", project_id, "--repo-root", str(self.root),
                                   state_path=e2e_state)
        self.assertEqual(code, 0, json.dumps(dry)[:800])
        output = dry["output"]
        self.assertEqual(output["outcome"], "DRY_RUN_VERIFIED_NOT_DEPLOYED")
        self.assertTrue(output["metadata"]["dry_run"])
        self.assertIsNone(output["deployment_url"], "a dry run reports no live URL")
        self.assertEqual(dry["status"], "COMPLETED")
        code, check, err = self._cli("deploy-check", project_id, state_path=e2e_state)
        self.assertEqual(code, 0, json.dumps(check)[:400])
        self.assertEqual(check["code"], "APPROVED_FOR_DEPLOYMENT")
        self.assertIn("Nothing was published", check["note"])
        self.assertFalse(check["publish_state_uncertain"])

        # 9. Operator views reflect exactly this state, and prove nothing was published anywhere.
        e2e_store = TrackingStore(e2e_state)
        request, _, _ = self._http(store=e2e_store)
        status, _, history = request("GET", "/jobs/job-e2e-build/history")
        self.assertEqual(status, 200, json.dumps(history))
        self.assertEqual([e["status"] for e in history["executions"]], ["COMPLETED"])
        status, _, attention = request("GET", "/attention")
        self.assertEqual(status, 200, json.dumps(attention))
        status, _, deployment_status = request("GET", "/deployment-status")
        self.assertEqual(status, 200, json.dumps(deployment_status))
        self.assertIn(project_id, json.dumps(deployment_status))
        project = e2e_store.state["projects"][project_id]
        self.assertIsNone(project.get("deployment_url"))
        self.assertNotEqual(project["current_stage"], "DEPLOYED")
        for deployment in (e2e_store.state.get("deployments") or {}).values():
            self.assertIn(deployment.get("deployment_status"), {"PENDING", "FAILED", None})
            self.assertIsNone(deployment.get("deployment_url"))
        self.assertFalse((build_dir / "deployment-attempts.jsonl").exists(),
                         "a dry run writes no publish ledger")
        self.assertEqual(real_state.read_bytes() if real_state.is_file() else b"", before,
                         "the end-to-end run must not touch real tracking state")


if __name__ == "__main__":
    unittest.main()
