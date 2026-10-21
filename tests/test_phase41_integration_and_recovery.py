"""Phase 41 tests: integration readiness and reliable recovery.

Everything here runs against fictional ``TEST_FIXTURE`` state in temporary directories, on loopback
sockets, and on the SQLite compatibility backend. Nothing is published, no provider is contacted, no lead
is contacted, and no real workspace file is written.

Four defect classes are pinned, each one a case where the system said something reassuring that was not
true, or crashed where it should have answered:

1. **Recovery of an interrupted worker.** A job claimed by a worker that died before its first heartbeat
   stayed ``RUNNING`` forever while ``GET /attention`` already reported it stale; monitoring and recovery
   disagreed about who owned the fix, and a ``RUNNING`` job cannot be retried - so the work was stranded
   with a visible problem and no action.
2. **An uncertain publish stays uncertain, and resolving it leaves a record.** A refusal appended to the
   attempt ledger used to read as a terminal result, so the *next* run saw a clean history and republished
   with no operator flag; and ``--resolve-uncertain-publish`` promised an audit record it never wrote.
3. **The local n8n verifier answers with data instead of a traceback**, and refuses to carry the API bearer
   token to a host outside this machine.
4. **Deployment topology matches the code it describes**, so ``render.yaml`` cannot name a flag, route, or
   variable that the running code ignores.

PostgreSQL is exercised only through an injected DB-API double and the SQLite backend: this environment has
no PostgreSQL server, and no test here pretends otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock as mock
import urllib.error
import urllib.request
import zlib

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment import authorization as authz
from deployment.deployer import VercelDeploymentProvider
from orchestration.api import make_handler
from orchestration.health import NEXT_ACTIONS, deployment_report, operator_attention
from orchestration.jobs import JobService
from orchestration.n8n_runner import N8nWorkflowRunner, N8nWorkflowTargetRefused, validate_n8n_workflow_json
from tracking.database import DatabasePersistenceError, RelationalStateBackend
from tracking.store import TrackingStore

FIXTURE_TOKEN = "fixture-only-token-0123456789abcdef"
SECRET_CANARY = "hunter2-topsecret"
SALON_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "qualified_lead_salon.json"
HTML = "<html><body><h1>Fictional Sole Stitch Salon — FICTIONAL TEST BUSINESS</h1></body></html>"
BUILD_ID = "build-20261015t000000z"
VALIDATION_ID = "validation-0001"
PROJECT_NAME = "ai-website-agent-preview"
PROJECT_ID_JSON = {"projectId": "prj_fixture_preview", "projectName": PROJECT_NAME, "orgId": "team_fixture"}


def _stamp(**shift) -> str:
    return (datetime.now(timezone.utc) - timedelta(**shift)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# =============================================================================================
# Shared fixtures: a disposable workspace, and the storage double used by section 5
# =============================================================================================
class _FaultyCursor:
    def __init__(self, cursor, *, fail_when) -> None:
        self._cursor, self._fail_when = cursor, fail_when

    def execute(self, sql, params=()):
        if self._fail_when(sql):
            raise RuntimeError(f"connection reset by peer password={SECRET_CANARY} host=db.internal")
        return self._cursor.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _InjectableConnection:
    """A real SQLite connection with one scripted failure, so rollback runs against a real engine."""

    def __init__(self, connection, *, fail_when, ledger: dict) -> None:
        self._connection, self._fail_when, self._ledger = connection, fail_when, ledger
        self.autocommit = False

    def cursor(self):
        return _FaultyCursor(self._connection.cursor(), fail_when=self._fail_when)

    def commit(self):
        self._ledger["commits"] += 1
        self._connection.commit()

    def rollback(self):
        self._ledger["rollbacks"] += 1
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _Disposable(unittest.TestCase):
    """One temporary workspace: state file, output root, export root, and a fictional linked Vercel project."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.output_root = self.root / "output"
        self.export_root = self.root / "preview-export"
        (self.root / ".vercel").mkdir()
        (self.root / ".vercel" / "project.json").write_text(json.dumps(PROJECT_ID_JSON), encoding="utf-8")
        self.store = TrackingStore(self.state_path)
        self.service = JobService(self.store)
        self.project_id = self.lead_id = None
        self._servers: list[ThreadingHTTPServer] = []
        self.addCleanup(self._stop_servers)

    def _stop_servers(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    # -- fixtures ---------------------------------------------------------------------------
    def lead(self, name: str = "Fictional Sole Stitch Salon") -> dict:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        digits = f"{abs(zlib.crc32(name.encode())) % 100000000:08d}"
        created = self.store.create_lead({
            "business_name": name,
            "business_type": "salon",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": f"+234 800 {digits[:4]} {digits[4:]}",
            "website_status": "WEBSITE_OUTDATED",
            "existing_website": f"https://{slug}.example.invalid",
            "source_urls": [f"https://fixture.example.invalid/{slug}"],
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        self.lead_id = created["lead_id"]
        return created

    def build(self) -> Path:
        lead = self.lead() if self.lead_id is None else next(iter(self.store.state["leads"].values()))
        project = self.store.create_project(lead["lead_id"])
        self.project_id = project["project_id"]
        directory = self.output_root / lead["lead_id"] / BUILD_ID
        directory.mkdir(parents=True, exist_ok=True)
        index = directory / "index.html"
        index.write_text(HTML, encoding="utf-8")
        (directory / "build.json").write_text(json.dumps({
            "build_id": BUILD_ID, "lead_id": lead["lead_id"], "status": "COMPLETED",
            "data_classification": "TEST_FIXTURE", "output_location": str(index),
            "validation": {"validation_id": VALIDATION_ID, "lead_id": lead["lead_id"], "build_id": BUILD_ID,
                           "status": "PASS"},
        }), encoding="utf-8")
        self.store.record_build(project["project_id"], {
            "build_id": BUILD_ID, "lead_id": lead["lead_id"], "status": "COMPLETED",
            "template_id": "salon-service-v1", "data_classification": "TEST_FIXTURE",
            "output_location": str(index),
        })
        self.store.record_validation(project["project_id"], {
            "validation_id": VALIDATION_ID, "build_id": BUILD_ID, "status": "PASS", "score": 100,
        })
        return directory

    def approve(self, build_dir: Path) -> dict:
        """Record the human decision through the real mechanism, so the durable state and the build-local
        capability are produced by the same code a production review would go through."""
        entry = self.store.record_review(
            self.project_id, "APPROVED_FOR_PREVIEW",
            "Fictional fixture approval; reviewer identity is not verifiable through this path.",
            reviewer="chioma", build_id=BUILD_ID, action=authz.DEPLOYMENT_ACTION)
        capability = authz.read_approval(build_dir)
        self.assertIsNotNone(capability, "the recorded decision has to materialise the build-local capability")
        self.assertIs(capability["identity_verified"], False,
                      "a decision recorded without a verifiable identity is never marked verified")
        return entry

    def unresolved_publish(self, build_dir: Path, state: str = "DEPLOYING") -> dict:
        """Record exactly what an interrupted publish leaves behind: an attempt with no terminal result."""
        return authz.append_attempt(build_dir, {
            "attempt": 1, "deployment_status": state, "deployment_id": "deployment-20261015t000000z",
            "build_id": BUILD_ID, "lead_id": self.lead_id, "vercel_project_id": PROJECT_ID_JSON["projectId"],
            "artifact_sha256": authz.artifact_sha256(build_dir), "created_at": _stamp(hours=2),
        })[-1]

    # -- transports -------------------------------------------------------------------------
    def http(self, *, worker=None, store: TrackingStore | None = None, token: str = FIXTURE_TOKEN):
        handler = make_handler(JobService(store or self.store), token, worker=worker,
                               output_root=self.output_root, export_root=self.export_root,
                               repository_root=self.root)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self._servers.append(httpd)
        port = httpd.server_address[1]

        def request(method: str, path: str, body: dict | None = None, *,
                    authenticated: bool = True) -> tuple[int, dict]:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8")
                try:
                    return error.code, json.loads(raw)
                except json.JSONDecodeError:
                    return error.code, {"raw": raw}

        return request

    def _clean_env(self) -> dict[str, str]:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        env.pop("AI_WEBSITE_AGENT_OPERATOR_ID", None)
        filtered_path = [
            p for p in env.get("PATH", "").split(os.pathsep)
            if not any((Path(p) / v).is_file() for v in ("vercel", "vercel.cmd", "vercel.exe", "vercel.ps1"))
        ]
        env["PATH"] = os.pathsep.join(filtered_path)
        return env

    def module(self, name: str, *argv: str) -> subprocess.CompletedProcess:
        """Run a module's CLI in this disposable workspace, as an operator at this checkout would."""
        return subprocess.run([sys.executable, "-m", name, *argv], cwd=self.root, env=self._clean_env(),
                              capture_output=True, text=True, timeout=180)

    def provider(self, runner) -> VercelDeploymentProvider:
        return VercelDeploymentProvider(self.root, runner=runner, expected_project_name=PROJECT_NAME)

    def recording_runner(self, calls: list, *, build_dir: Path, outcome: str = "ok"):
        def runner(cmd, **kwargs):
            calls.append(list(authz.read_attempts(build_dir)))
            if outcome == "hang":
                raise subprocess.TimeoutExpired(cmd, timeout=5)
            return subprocess.CompletedProcess(cmd, 0, stdout="https://fixture-preview.vercel.app\n", stderr="")

        return runner

    def deploy(self, build_dir: Path, *, calls: list, **kwargs) -> object:
        with mock.patch("deployment.deployer.shutil.which", return_value="/usr/bin/true"):
            return self.provider(self.recording_runner(calls, build_dir=build_dir,
                                                       outcome=kwargs.pop("outcome", "ok"))).deploy(
                build_dir, dry_run=False, project_name=PROJECT_NAME, **kwargs)


# =============================================================================================
# 1. Recovery: a job whose worker died before its first heartbeat must be recoverable
# =============================================================================================
class StaleJobRecoveryTests(_Disposable):
    def _running_without_heartbeat(self, job_id: str = "job-crashed-worker") -> dict:
        self.service.create("BUILD", job_id=job_id, input={"project_id": "project-0001"})
        self.store.state["jobs"][job_id].update({
            "status": "RUNNING", "attempts": 1, "started_at": _stamp(hours=1), "heartbeat_at": None,
            "updated_at": _stamp(hours=1),
        })
        self.store._save()
        return self.store.state["jobs"][job_id]

    def test_interrupted_worker_without_heartbeat_is_recovered_and_left_retryable(self) -> None:
        self.lead()
        self._running_without_heartbeat()
        recovered = self.service.recover_stale(older_than_seconds=900)
        self.assertEqual([job["job_id"] for job in recovered], ["job-crashed-worker"],
                         "a job with no heartbeat was invisible to recovery while monitoring reported it stale")
        job = self.store.state["jobs"]["job-crashed-worker"]
        self.assertEqual(job["status"], "NEEDS_REVIEW")
        # The distinct code matters: no heartbeat ever arrived, so the worker died on claim rather than
        # part-way through the run, and the investigation an operator starts with is different.
        self.assertEqual(job["error_code"], "HEARTBEAT_UNDATED")
        self.assertIn("without recording a heartbeat", job["error"])
        self.assertTrue(job["requires_operator_action"])
        self.assertIn("recover-stale", job["next_action"])
        self.assertIn("HEARTBEAT_UNDATED", NEXT_ACTIONS)
        eligibility = self.service.retry_eligibility("job-crashed-worker")
        self.assertTrue(eligibility["eligible"], "recovery has to leave a retry path, or the job is stranded")
        self.assertEqual(eligibility["attempts_remaining"], 2)

    def test_monitoring_and_recovery_age_the_same_job_the_same_way(self) -> None:
        self.lead()
        self._running_without_heartbeat()
        flagged = [item for item in operator_attention(self.store)["items"]
                   if item.get("job_id") == "job-crashed-worker"]
        self.assertEqual([item["code"] for item in flagged], ["HEARTBEAT_UNDATED"],
                         "the report and the recovery command must name the same condition")
        self.assertIn("recover-stale", flagged[0]["next_action"])
        self.assertEqual(len(self.service.recover_stale(older_than_seconds=900)), 1)
        after = operator_attention(self.store)
        self.assertEqual([item for item in after["items"] if item.get("code") == "HEARTBEAT_UNDATED"
                          and item.get("kind") == "stale_job"], [], "recovered work must leave the stale queue")
        self.assertEqual(after["counts"]["needs_review"], 1)

    def test_an_expired_heartbeat_keeps_its_established_code(self) -> None:
        self.lead()
        self.service.create("BUILD", job_id="job-lost-heartbeat", input={"project_id": "project-0001"})
        self.store.state["jobs"]["job-lost-heartbeat"].update({
            "status": "RUNNING", "attempts": 1, "started_at": _stamp(minutes=40),
            "heartbeat_at": _stamp(minutes=40), "updated_at": _stamp(minutes=40)})
        self.store._save()
        recovered = self.service.recover_stale(older_than_seconds=900)
        self.assertEqual([record["error_code"] for record in recovered], ["HEARTBEAT_EXPIRED"])
        stale = [item for item in operator_attention(self.store)["items"] if item.get("kind") == "stale_job"]
        self.assertEqual([item["code"] for item in stale], [])

    def test_a_live_worker_and_a_fresh_claim_are_left_alone(self) -> None:
        self.lead()
        for job_id, heartbeat in (("job-alive", _stamp(seconds=5)), ("job-young", None)):
            self.service.create("BUILD", job_id=job_id, input={"project_id": "project-0001"})
            self.store.state["jobs"][job_id].update({
                "status": "RUNNING", "attempts": 1, "started_at": _stamp(seconds=5),
                "heartbeat_at": heartbeat, "updated_at": _stamp(seconds=5)})
        self.store._save()
        self.assertEqual(self.service.recover_stale(older_than_seconds=900), [])
        self.assertEqual({self.store.state["jobs"][key]["status"] for key in ("job-alive", "job-young")},
                         {"RUNNING"})
        counts = operator_attention(self.store)["counts"]
        self.assertEqual((counts["stale_running"], counts["undated_running"]), (0, 0))

    def test_a_running_job_with_no_recorded_time_is_visible_but_not_guessed_at(self) -> None:
        """No timestamp means no honest age: recovery refuses to invent one, and the report surfaces it."""
        self.lead()
        self.service.create("BUILD", job_id="job-undated", input={"project_id": "project-0001"})
        self.store.state["jobs"]["job-undated"].update({
            "status": "RUNNING", "attempts": 1, "started_at": None, "heartbeat_at": None, "updated_at": None})
        self.store._save()
        self.assertEqual(self.service.recover_stale(older_than_seconds=900), [])
        self.assertEqual(self.store.state["jobs"]["job-undated"]["status"], "RUNNING")
        report = operator_attention(self.store)
        items = [item for item in report["items"] if item.get("job_id") == "job-undated"]
        self.assertEqual([item["code"] for item in items], ["HEARTBEAT_UNDATED"])
        self.assertEqual(items[0]["kind"], "undated_job")
        self.assertIsNone(items[0]["age_seconds"])
        self.assertEqual(report["counts"]["undated_running"], 1)
        self.assertEqual(report["counts"]["stale_running"], 0, "an unknowable age must not be counted as stale")

    def test_recovery_reports_the_same_result_over_http_and_the_cli(self) -> None:
        self.lead()
        self._running_without_heartbeat("job-http")
        self._running_without_heartbeat("job-cli")
        request = self.http()
        status, payload = request("POST", "/jobs/recover-stale", {"older_than_seconds": 900})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["recovered_count"], 2)
        self.assertEqual(sorted(job["job_id"] for job in payload["recovered_jobs"]), ["job-cli", "job-http"])
        self.assertEqual({job["error_code"] for job in payload["recovered_jobs"]}, {"HEARTBEAT_UNDATED"})
        self.assertNotIn(SECRET_CANARY, json.dumps(payload))
        # Both entry points write the same durable record, not just the same response.
        reloaded = TrackingStore(self.state_path)
        self.assertEqual({reloaded.state["jobs"][key]["status"] for key in ("job-http", "job-cli")},
                         {"NEEDS_REVIEW"})

        again = self.module("orchestration.cli", "recover-stale", "--state-path", str(self.state_path),
                           "--older-than-seconds", "900")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout), [], "recovery is idempotent: nothing is recovered twice")

        self._running_without_heartbeat("job-cli-second")
        found = self.module("orchestration.cli", "recover-stale", "--state-path", str(self.state_path),
                            "--older-than-seconds", "900")
        # The command exits 1 when it found stranded work, because an operator still has something to
        # clear; it prints the recovered records themselves so the exit code never has to carry the reason.
        self.assertEqual(found.returncode, 1, found.stderr)
        printed = json.loads(found.stdout)
        self.assertEqual([job["error_code"] for job in printed], ["HEARTBEAT_UNDATED"])
        self.assertEqual(printed[0]["status"], "NEEDS_REVIEW")
        self.assertEqual(TrackingStore(self.state_path).state["jobs"]["job-cli-second"]["error_code"],
                         "HEARTBEAT_UNDATED")


# =============================================================================================
# 2. Uncertain publishes: never a success, never cleared by a refusal, resolution is recorded
# =============================================================================================
class UncertainPublishAuditTests(_Disposable):
    def setUp(self) -> None:
        super().setUp()
        self.build_dir = self.build()
        self.approve(self.build_dir)
        self.sha = authz.artifact_sha256(self.build_dir)
        self.ledger = self.build_dir / authz.ATTEMPTS_FILENAME

    def test_uncertain_publish_is_refused_and_names_the_evidence_and_both_options(self) -> None:
        self.unresolved_publish(self.build_dir)
        proceed, code, reason, prior = authz.classify_prior_attempt(
            self.build_dir, artifact_sha=self.sha, project_id=PROJECT_ID_JSON["projectId"])
        self.assertFalse(proceed)
        self.assertEqual(code, "PUBLISH_STATE_UNCERTAIN")
        for fragment in ("attempt 1", "deployment-20261015t000000z", PROJECT_ID_JSON["projectId"],
                         authz.ATTEMPTS_FILENAME, "--resolve-uncertain-publish", "--resolve-note"):
            self.assertIn(fragment, reason, f"the refusal has to say what and where: {fragment}")
        self.assertEqual(prior["deployment_status"], "DEPLOYING")

    def test_a_logged_refusal_does_not_clear_the_uncertainty(self) -> None:
        """Regression: the refusal entry became "the latest attempt", and the next run republished freely."""
        self.unresolved_publish(self.build_dir)
        first: list = []
        result = self.deploy(self.build_dir, calls=first)
        self.assertEqual(result.deployment_status, "NEEDS_REVIEW")
        self.assertIn("PUBLISH_STATE_UNCERTAIN", result.error)
        self.assertEqual(first, [], "a refused publish must not invoke the provider CLI at all")
        self.assertEqual([entry["deployment_status"] for entry in authz.read_attempts(self.build_dir)],
                         ["DEPLOYING", "NEEDS_REVIEW"])
        second: list = []
        again = self.deploy(self.build_dir, calls=second)
        self.assertEqual(again.deployment_status, "NEEDS_REVIEW", "a refusal must not unlock its own retry")
        self.assertEqual(second, [])
        self.assertEqual(authz.publish_outcome(self.build_dir)["state"], "UNCERTAIN")
        self.assertEqual(authz.latest_publish_attempt(self.build_dir)["deployment_status"], "DEPLOYING")

    def test_a_dry_run_stays_available_while_a_publish_is_unresolved(self) -> None:
        self.unresolved_publish(self.build_dir)
        before = authz.read_attempts(self.build_dir)
        calls: list = []
        with mock.patch("deployment.deployer.shutil.which", return_value="/usr/bin/true"):
            result = self.provider(self.recording_runner(calls, build_dir=self.build_dir)).deploy(
                self.build_dir, dry_run=True)
        self.assertEqual(result.deployment_status, "PENDING")
        self.assertEqual(calls, [], "a dry run never calls the provider")
        self.assertEqual(authz.read_attempts(self.build_dir), before, "a verification must not write history")

    def test_resolution_is_recorded_before_the_provider_is_contacted(self) -> None:
        self.unresolved_publish(self.build_dir)
        calls: list = []
        result = self.deploy(self.build_dir, calls=calls, resolve_uncertain_publish=True,
                            resolution_note="Provider console lists no deployment for this artifact; "
                                            "retrying deliberately.",
                            resolved_by="chioma@fixture")
        self.assertEqual(len(calls), 1)
        ledger_at_call = [entry["deployment_status"] for entry in calls[0]]
        self.assertEqual(ledger_at_call[-2:], [authz.UNCERTAIN_RESOLUTION_STATE, "DEPLOYING"],
                         "the decision must be on disk before anything is sent, or a crash loses it")
        self.assertEqual(result.deployment_status, "DEPLOYED")
        annotation = next(entry for entry in authz.read_attempts(self.build_dir)
                          if entry["deployment_status"] == authz.UNCERTAIN_RESOLUTION_STATE)
        self.assertEqual(annotation["resolved_by"], "chioma@fixture")
        self.assertEqual(annotation["resolves_attempt"], 1)
        self.assertEqual(annotation["resolves_status"], "DEPLOYING")
        self.assertIs(annotation["published"], False)
        self.assertIs(annotation["provider_state_verified"], False)
        self.assertIn("no deployment for this artifact", annotation["resolution_note"])
        self.assertEqual(result.metadata["publish_resolution"]["resolves_attempt"], 1)

    def test_a_timeout_still_ends_uncertain_rather_than_failed(self) -> None:
        calls: list = []
        result = self.deploy(self.build_dir, calls=calls, outcome="hang")
        self.assertEqual(result.deployment_status, "PUBLISH_UNCERTAIN")
        self.assertIn("may have been accepted by the provider", result.error)
        self.assertEqual(authz.publish_outcome(self.build_dir)["state"], "UNCERTAIN")

    def test_a_resolution_never_reports_the_publish_as_resolved(self) -> None:
        self.unresolved_publish(self.build_dir)
        authz.append_resolution_attempt(
            self.build_dir, resolves=authz.latest_publish_attempt(self.build_dir),
            resolved_by="tobi@fixture", note="Checked the project; nothing was deployed.")
        outcome = authz.publish_outcome(self.build_dir)
        self.assertEqual((outcome["state"], outcome["code"]), ("UNCERTAIN", "PUBLISH_STATE_UNCERTAIN"))
        self.assertEqual(outcome["latest_publish_attempt"]["deployment_status"], "DEPLOYING")
        self.assertEqual(outcome["resolution_count"], 1)
        self.assertIs(outcome["latest_resolution"]["published"], False)
        self.assertEqual(outcome["latest_resolution"]["resolved_by"], "tobi@fixture")
        self.assertIn("1 resolution(s) are recorded", outcome["next_action"])
        proceed, code, reason, _ = authz.classify_prior_attempt(
            self.build_dir, artifact_sha=self.sha, project_id=PROJECT_ID_JSON["projectId"])
        self.assertFalse(proceed, "a resolution is history, not standing permission")
        self.assertEqual(code, "PUBLISH_STATE_UNCERTAIN")
        self.assertIn("1 operator resolution(s) are already recorded", reason)

    def test_history_is_append_only_and_an_unattributed_resolution_stays_honest(self) -> None:
        self.unresolved_publish(self.build_dir, state="PUBLISH_UNCERTAIN")
        before = authz.read_attempts(self.build_dir)
        authz.append_resolution_attempt(self.build_dir, resolves=before[-1], resolved_by=None, note=None)
        after = authz.read_attempts(self.build_dir)
        self.assertEqual(after[:len(before)], before, "resolving must not rewrite the attempt it annotates")
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1]["resolved_by"], "unknown")
        self.assertEqual(after[-1]["resolution_note"], "No operator note was supplied.")

    def test_untrusted_resolution_text_is_bounded_before_it_is_stored(self) -> None:
        self.unresolved_publish(self.build_dir)
        authz.append_resolution_attempt(
            self.build_dir, resolves=authz.latest_publish_attempt(self.build_dir),
            resolved_by="ops\n\t@example", note="  checked\tconsole " + SECRET_CANARY + "\n" + ("x" * 500))
        annotation = authz.read_attempts(self.build_dir)[-1]
        for key, bound in (("resolution_note", 400), ("resolved_by", 120)):
            self.assertNotIn("\n", annotation[key])
            self.assertNotIn("\t", annotation[key])
            self.assertLessEqual(len(annotation[key]), bound)

    def test_a_corrupt_ledger_is_reported_and_never_overwritten(self) -> None:
        self.unresolved_publish(self.build_dir)
        self.ledger.write_text("{ not json", encoding="utf-8")
        outcome = authz.publish_outcome(self.build_dir)
        self.assertEqual((outcome["state"], outcome["code"]), ("EVIDENCE_UNREADABLE", "PUBLISH_LEDGER_UNREADABLE"))
        self.assertFalse(outcome["ledger_readable"])
        self.assertIn("not valid JSON", outcome["next_action"])
        self.assertIn("instead of 'no publish attempted'", outcome["next_action"])
        proceed, code, reason, _ = authz.classify_prior_attempt(self.build_dir, artifact_sha=self.sha,
                                                                project_id=PROJECT_ID_JSON["projectId"])
        self.assertFalse(proceed)
        self.assertEqual(code, "PUBLISH_LEDGER_UNREADABLE")
        self.assertIn("cannot be trusted", reason)
        with self.assertRaises(ValueError) as caught:
            authz.append_attempt(self.build_dir, {"deployment_status": "DEPLOYING"})
        self.assertIn("Preserve the file", str(caught.exception))
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), "{ not json")
        calls: list = []
        result = self.deploy(self.build_dir, calls=calls)
        self.assertEqual(result.deployment_status, "NEEDS_REVIEW")
        self.assertEqual(calls, [], "an untrusted history must stop the publish, not be repaired by it")
        self.assertIn("PUBLISH_LEDGER_UNREADABLE", result.error)

    def test_an_uncertain_publish_reaches_every_operator_surface(self) -> None:
        self.unresolved_publish(self.build_dir)
        self.store.state["deployments"]["deployment-0001"] = {
            "deployment_id": "deployment-0001", "project_id": self.project_id, "build_id": BUILD_ID,
            "lead_id": self.lead_id, "deployment_status": "PUBLISH_UNCERTAIN", "deployment_url": None,
            "dry_run": False, "created_at": _stamp(hours=2),
        }
        self.store.state["projects"][self.project_id]["latest_deployment_id"] = "deployment-0001"
        self.store._save()
        report = deployment_report(self.store, repository_root=self.root)
        entry = next(item for item in report["attention"] if item.get("build_id") == BUILD_ID)
        self.assertEqual((entry["state"], entry["code"]), ("UNCERTAIN", "PUBLISH_STATE_UNCERTAIN"))
        self.assertEqual(Path(entry["ledger_path"]).name, authz.ATTEMPTS_FILENAME,
                         "the report has to name the file an operator needs to read")
        self.assertIn("--resolve-uncertain-publish", entry["next_action"])
        self.assertFalse(entry["provider_contacted"])
        self.assertEqual(report["status"], "degraded")

        status_run = self.module("orchestration.cli", "deployment-status", "--state-path", str(self.state_path),
                                 "--repository-root", str(self.root))
        # 1 is this command's "degraded" answer: an uncertain publish is exactly what it exists to surface.
        self.assertEqual(status_run.returncode, 1, status_run.stderr)
        printed = json.loads(status_run.stdout)
        self.assertEqual(printed["status"], "degraded")
        self.assertEqual([item["state"] for item in printed["attention"] if item.get("build_id") == BUILD_ID],
                         ["UNCERTAIN"])
        self.assertFalse(printed["configuration"]["cli_available"],
                         "no provider CLI is wired up here, which is why nothing could have been published")
        check = self.module("tracking.cli", "--state-path", str(self.state_path), "deploy-check", self.project_id)
        # 1 is the CLI's "not authorized" answer; it never means the check itself broke.
        self.assertEqual(check.returncode, 1, check.stderr)
        gate = json.loads(check.stdout)
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "PUBLISH_STATE_UNCERTAIN")
        self.assertIn("--resolve-note", gate["reason"])
        self.assertNotIn(SECRET_CANARY, check.stdout)

    def test_the_http_deployment_report_names_the_ledger_without_mapping_the_disk(self) -> None:
        """The same report is served over HTTP, so paths stay repository-relative there."""
        self.unresolved_publish(self.build_dir)
        self.ledger.write_text("{ corrupted", encoding="utf-8")
        status, body = self.http()("GET", "/deployment-status")
        self.assertEqual(status, 200, body)
        text = json.dumps(body)
        self.assertNotIn(str(self.root), text, "an authenticated response must not reveal where the workspace lives")
        self.assertIn(authz.ATTEMPTS_FILENAME, text, "but it must still name the file to inspect")
        entries = [item for item in body.get("attention", []) if item.get("build_id") == BUILD_ID]
        self.assertEqual([item["state"] for item in entries], ["EVIDENCE_UNREADABLE"])
        self.assertTrue(Path(entries[0]["ledger_path"]).is_relative_to("."),
                        "the report still says which file to read, relative to the workspace")
        self.assertEqual(entries[0]["ledger_path"], f"output/{self.lead_id}/{BUILD_ID}/{authz.ATTEMPTS_FILENAME}")
        self.assertNotIn(SECRET_CANARY, text)

    def test_every_uncertain_execution_is_listed_not_just_the_first(self) -> None:
        self.store.state.setdefault("executions", {})
        for index in range(3):
            project_id = f"project-unsettled-{index}"
            self.store.state["projects"][project_id] = {"project_id": project_id, "lead_id": f"lead-{index}",
                                                        "assigned_build_id": None, "current_stage": "BLOCKED"}
            self.store.state["executions"][f"exec-{index}"] = {
                "execution_id": f"exec-{index}", "job_id": f"job-{index}", "project_id": project_id,
                "status": "NEEDS_REVIEW", "error_code": "PUBLISH_UNCERTAIN",
                "error_message": "provider answer could not be verified", "completed_at": _stamp(hours=1),
            }
        self.store._save()
        report = deployment_report(self.store, repository_root=self.root)
        flagged = [item for item in report["attention"] if item.get("kind") == "execution"]
        self.assertEqual(len(flagged), 3, "under-reporting operator work is a monitoring failure")
        self.assertEqual(report["attention_count"], 3)


class UncertainPublishCliTests(_Disposable):
    """The documented operator path, through the real CLI, with the real (absent) provider binary."""

    def setUp(self) -> None:
        super().setUp()
        self.build_dir = self.build()
        self.approve(self.build_dir)
        self.unresolved_publish(self.build_dir)

    def deployment_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return self.module("deployment.cli", "--build", str(self.build_dir), "--state-path",
                           str(self.state_path), *argv)

    def test_resolve_flag_without_a_note_is_refused_before_anything_changes(self) -> None:
        attempts = authz.read_attempts(self.build_dir)
        result = self.deployment_cli("--execute", "--project-id", self.project_id, "--project-name", PROJECT_NAME,
                                    "--resolve-uncertain-publish")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --resolve-note", result.stderr)
        self.assertEqual(authz.read_attempts(self.build_dir), attempts, "a refused invocation writes nothing")

    def test_a_note_without_the_resolve_flag_is_refused(self) -> None:
        result = self.deployment_cli("--execute", "--project-id", self.project_id, "--project-name", PROJECT_NAME,
                                    "--resolve-note", "checked the console")
        self.assertEqual(result.returncode, 2)
        self.assertIn("only applies with --resolve-uncertain-publish", result.stderr)

    def test_a_resolved_retry_is_recorded_then_fails_on_the_absent_cli(self) -> None:
        """No ``vercel`` binary exists here: the decision must still be recorded, and nothing is published."""
        self.assertIsNone(shutil.which("vercel", path=self._clean_env()["PATH"]),
                          "this environment must not have a real Vercel CLI for the test to mean anything")
        result = self.deployment_cli("--execute", "--project-id", self.project_id, "--project-name", PROJECT_NAME,
                                    "--resolve-uncertain-publish",
                                    "--resolve-note", "Provider project list shows no deployment for this artifact "
                                                      "hash; retrying once, deliberately.",
                                    "--resolved-by", "chioma@fixture")
        self.assertEqual(result.returncode, 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["deployment_status"], "FAILED")
        self.assertIsNone(payload["deployment_url"], "a publish that never happened reports no URL")
        statuses = [entry["deployment_status"] for entry in authz.read_attempts(self.build_dir)]
        self.assertIn(authz.UNCERTAIN_RESOLUTION_STATE, statuses,
                      "the operator's decision is recorded even when the retry then fails")
        position = statuses.index(authz.UNCERTAIN_RESOLUTION_STATE)
        self.assertGreater(position, 0, "the resolution is appended after the attempt it resolves")
        self.assertEqual(statuses[-1], "FAILED", "the retry was attempted and reported its real outcome")
        annotation = authz.read_attempts(self.build_dir)[position]
        self.assertEqual(annotation["resolved_by"], "chioma@fixture")
        self.assertIn("no deployment for this artifact hash", annotation["resolution_note"])
        outcome = authz.publish_outcome(self.build_dir)
        self.assertEqual(outcome["resolution_count"], 1)
        self.assertEqual(outcome["state"], "FAILED", "the failed retry is the current truth, recorded as such")
        self.assertNotIn(SECRET_CANARY, json.dumps(authz.read_attempts(self.build_dir)))

    def test_the_cli_help_matches_what_the_code_actually_does(self) -> None:
        """Defect 2 was help text promising a record that nothing wrote; keep text and behaviour aligned."""
        source = (REPO_ROOT / "deployment" / "cli.py").read_text(encoding="utf-8")
        block = source.split('"--resolve-uncertain-publish"', 1)[1].split('"--resolve-note"', 1)[0]
        for fragment in ("records", authz.ATTEMPTS_FILENAME, "never restates"):
            self.assertIn(fragment, block, f"the help text no longer describes the flag: {fragment}")
        self.assertIn("requires --resolve-note", source)
        deployer = (REPO_ROOT / "deployment" / "deployer.py").read_text(encoding="utf-8")
        self.assertIn("append_resolution_attempt", deployer, "the promised record has to be written somewhere")


class EndToEndInterruptedFlowTests(_Disposable):
    """One fictional lead all the way to a recovered, auditable deployment boundary.

    Real entry points only: the tracking CLI for ingest/build, the HTTP API for the human decision, the
    deployment CLI for the publish boundary, and the durable ledger as the record. No provider is called.
    """

    def _cli(self, *argv: str) -> tuple[int, dict, str]:
        # `--state-path` is a global option of the tracking CLI, so it precedes the subcommand.
        result = self.module("tracking.cli", "--state-path", str(self.state_path), *argv)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {"raw": result.stdout}
        return result.returncode, payload, result.stderr

    def test_lead_to_approval_to_interruption_to_resolved_retry(self) -> None:
        # 1. One fictional lead and its project, through the real ingest command and fixture.
        code, ingested, err = self._cli("ingest-lead", "--record", str(SALON_FIXTURE), "--create-project")
        self.assertEqual(code, 0, err)
        project_id = ingested["project_id"]

        # 2-3. Build and validate with the CLI's own worker, then check what the artifact really is.
        code, built, err = self._cli("build", project_id, "--output-root", str(self.output_root),
                                    "--job-id", "job-e2e-build")
        self.assertEqual(code, 0, err)
        build_id = built["build_id"]
        self.assertEqual(built["validation_status"], "PASS")
        build_dir = self.output_root / built["lead_id"] / build_id
        self.assertIn("FICTIONAL", (build_dir / "index.html").read_text(encoding="utf-8").upper())

        # 4. The human decision, recorded through the API: authenticated as automation, so the reviewer
        #    identity stays explicitly unverified and nothing is deployed by the decision itself.
        store = TrackingStore(self.state_path)
        status, review = self.http(store=store)("POST", f"/projects/{project_id}/review", {
            "decision": "APPROVED_FOR_PREVIEW",
            "notes": "Fictional salon draft checked against the fixture brief; contact block and services "
                     "are correct for this test business.",
            "reviewer": "operator:e2e-fixture", "reviewer_type": "HUMAN", "reviewer_id": "operator:e2e-fixture",
            "build_id": build_id, "confirm": f"{project_id}:{build_id}:APPROVED_FOR_PREVIEW",
        })
        self.assertEqual(status, 200, review)
        self.assertFalse(review["identity_verified"])
        self.assertFalse(review["deployed"])

        # 5. Authorization and the dry run: verification only, no publish, and no attempt history.
        gate = review["deployment_authorization"]
        self.assertEqual(gate["code"], "APPROVED_FOR_DEPLOYMENT")
        self.assertEqual(gate["build_id"], build_id)
        code, dry, err = self._cli("prepare-preview", project_id, "--repo-root", str(self.root))
        self.assertEqual(code, 0, json.dumps(dry)[:600])
        self.assertEqual(dry["output"]["outcome"], "DRY_RUN_VERIFIED_NOT_DEPLOYED")
        self.assertIsNone(dry["output"]["deployment_url"])
        self.assertFalse((build_dir / authz.ATTEMPTS_FILENAME).exists())

        # 6. An interrupted publish: exactly the ledger line a killed worker leaves behind. The next
        #    attempt is refused, the retry is not silent, and monitoring says the same thing.
        self.unresolved_publish(build_dir)
        code, blocked, err = self._cli("deploy-check", project_id)
        self.assertEqual(code, 1, err)
        self.assertEqual(blocked["code"], "PUBLISH_STATE_UNCERTAIN")
        self.assertIn("--resolve-note", blocked["reason"])
        report = deployment_report(TrackingStore(self.state_path), repository_root=self.root)
        self.assertEqual(report["status"], "degraded")

        # 7. The operator resolves it explicitly, with a reason. Nothing was published here: this
        #    environment has no Vercel CLI, so the retry fails - and the decision is still on record.
        result = self.module("deployment.cli", "--build", str(build_dir), "--state-path", str(self.state_path),
                            "--execute", "--project-id", project_id, "--project-name", PROJECT_NAME,
                            "--resolve-uncertain-publish", "--resolve-note",
                            "Provider project list shows no deployment for this artifact; retrying once.",
                            "--resolved-by", "chioma@fixture")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["deployment_status"], "FAILED")
        self.assertIsNone(payload["deployment_url"], "a failed retry must not be reported as a deployment")
        ledger = authz.read_attempts(build_dir)
        statuses = [entry["deployment_status"] for entry in ledger]
        self.assertEqual(statuses[0], "DEPLOYING", "the interrupted publish is still the first word here")
        self.assertGreater(statuses.index(authz.UNCERTAIN_RESOLUTION_STATE), 0,
                           "the resolution follows the attempt it resolves")
        self.assertEqual(statuses[-1], "FAILED", "the retry ran and failed on the absent provider CLI")
        self.assertEqual(authz.publish_outcome(build_dir)["resolution_count"], 1)

        # 8. A decision that came from the API is still not treated as identity-verified, and the durable
        #    records agree with what each command reported.
        final = TrackingStore(self.state_path)
        self.assertIs(authz.read_approval(build_dir)["identity_verified"], False)
        self.assertEqual(final.state["projects"][project_id]["assigned_build_id"], build_id)
        self.assertEqual(final.state["projects"][project_id]["review_status"], "APPROVED_FOR_PREVIEW")
        self.assertNotIn("deployment_url", json.dumps(final.state["deployments"]) if final.state.get("deployments") else "",
                         "nothing was recorded as deployed by this flow")


# =============================================================================================
# 3. The local n8n verifier: faults become answers, and the token stays on the machine
# =============================================================================================
class N8nRunnerFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def workflow(self, url: str, *, name: str = "probe-workflow", method: str = "GET") -> Path:
        workflow = {
            "name": name,
            "active": False,
            "nodes": [
                {"name": "Manual Trigger", "type": "n8n-nodes-base.manualTrigger", "parameters": {}},
                {"name": "Call API", "type": "n8n-nodes-base.httpRequest", "parameters": {
                    "method": method, "url": url,
                    "bodyParameters": {"parameters": [{"name": "job_type", "value": "SCOUT"}]},
                    "headerParameters": {"parameters": [
                        {"name": "Authorization", "value": "Bearer {{ $env.AI_WEBSITE_AGENT_API_TOKEN }}"}]},
                }},
            ],
            "connections": {"Manual Trigger": {"main": [[{"node": "Call API"}]]}},
        }
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(workflow), encoding="utf-8")
        return path

    def serve(self, status: int, body: bytes, content_type: str) -> int:
        """Answer once from a throwaway loopback server and return its port."""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server contract
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def run_workflow(self, url: str, *, env: dict[str, str] | None = None) -> dict:
        return N8nWorkflowRunner(self.workflow(url), env=env or {}).execute({})

    def test_an_unreachable_api_becomes_a_structured_failure_not_a_traceback(self) -> None:
        result = self.run_workflow("http://127.0.0.1:1/healthz",
                                   env={"AI_WEBSITE_AGENT_API_TOKEN": FIXTURE_TOKEN})
        output = result["node_outputs"]["Call API"]
        self.assertEqual(output["error_kind"], "connection_failed")
        self.assertEqual(output["status"], "FAILED")
        self.assertIn("could not reach the API", output["error"])
        self.assertIn("orchestration.cli serve", output["next_action"])
        self.assertEqual(result["execution_status"], "FAILED")
        self.assertEqual([item["node"] for item in result["errors"]], ["Call API"])
        self.assertNotIn(FIXTURE_TOKEN, json.dumps(result))

    def test_an_api_error_answer_keeps_its_own_explanation(self) -> None:
        port = self.serve(404, json.dumps({"error": "UNKNOWN_PROJECT: project-404 does not exist",
                                          "error_code": "RECORD_NOT_FOUND"}).encode("utf-8"),
                         "application/json")
        output = self.run_workflow(f"http://127.0.0.1:{port}/jobs/job-1")["node_outputs"]["Call API"]
        self.assertEqual(output["http_status"], 404)
        self.assertEqual(output["error_kind"], "http_error")
        self.assertEqual(output["error_code"], "RECORD_NOT_FOUND")
        self.assertIn("UNKNOWN_PROJECT", output["error"])

    def test_a_success_that_is_not_json_is_a_failure_not_an_empty_success(self) -> None:
        port = self.serve(200, b"<html><body>proxy error</body></html>", "text/html")
        output = self.run_workflow(f"http://127.0.0.1:{port}/healthz")["node_outputs"]["Call API"]
        self.assertEqual(output["error_kind"], "invalid_response")
        self.assertEqual(output["http_status"], 200)
        self.assertEqual(output["status"], "FAILED")

    def test_an_empty_answer_is_reported_as_empty(self) -> None:
        port = self.serve(204, b"", "application/json")
        output = self.run_workflow(f"http://127.0.0.1:{port}/healthz")["node_outputs"]["Call API"]
        self.assertEqual(output["error_kind"], "empty_response")
        self.assertEqual(output["status"], "FAILED")

    def test_the_credential_is_never_sent_to_a_host_outside_this_machine(self) -> None:
        path = self.workflow("http://attacker.example.invalid:9/attention")
        runner = N8nWorkflowRunner(path, env={"AI_WEBSITE_AGENT_API_TOKEN": FIXTURE_TOKEN})
        with self.assertRaises(N8nWorkflowTargetRefused) as caught:
            runner.execute({})
        message = str(caught.exception)
        self.assertIn("attacker.example.invalid", message)
        self.assertIn("bearer token", message)
        self.assertNotIn(FIXTURE_TOKEN, message)
        self.assertEqual(N8nWorkflowRunner(path, env={}, allow_remote=True).workflow["name"], "probe-workflow",
                         "an operator may opt in deliberately; the default is what protects the token")

    def test_missing_and_malformed_workflow_files_explain_themselves(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_n8n_workflow_json(self.root / "not-exported-yet.json")
        self.assertIn("n8n/workflows/", str(caught.exception))
        self.assertIn("Export it from n8n", str(caught.exception))
        broken = self.root / "broken.json"
        broken.write_text("{\"nodes\": [", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            validate_n8n_workflow_json(broken)
        self.assertIn("not valid JSON", str(caught.exception))
        self.assertNotIn("Traceback", str(caught.exception))


# =============================================================================================
# 4. Topology: what the deployment configuration claims, the code must actually do
# =============================================================================================
class DeploymentTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.render = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
        cls.requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        cls.cli_source = (REPO_ROOT / "orchestration" / "cli.py").read_text(encoding="utf-8")
        cls.api_source = (REPO_ROOT / "orchestration" / "api.py").read_text(encoding="utf-8")

    def scalar(self, key: str) -> str:
        match = re.search(rf"^\s*{re.escape(key)}:\s*(.+?)\s*$", self.render, re.MULTILINE)
        self.assertIsNotNone(match, f"render.yaml no longer declares {key}")
        return match.group(1).strip().strip('"')

    def env_pairs(self) -> dict[str, str]:
        """Render's blueprint either sets a value or marks the variable sync-only (supplied in its dashboard)."""
        pairs: dict[str, str] = {}
        for key, body in re.findall(r"^\s*-\s*key:\s*([A-Z0-9_]+)\s*$\n(.*?)(?=^\s*-\s*key:|\Z)",
                                    self.render, re.MULTILINE | re.DOTALL):
            value = re.search(r"^\s*value:\s*(.+?)\s*$", body, re.MULTILINE)
            pairs[key] = value.group(1).strip('"') if value else ""
        return pairs

    def test_start_command_flags_exist_in_the_parser_it_calls(self) -> None:
        start = self.scalar("startCommand")
        self.assertIn("orchestration.cli", start)
        self.assertIn("serve", start)
        flags = set(re.findall(r"--[a-z][a-z0-9-]+", start))
        declared = set(re.findall(r'add_argument\(\s*"(--[a-z][a-z0-9-]+)"', self.cli_source))
        self.assertTrue(flags)
        self.assertEqual(sorted(flags - declared), [],
                         "render.yaml passes a flag the CLI does not accept, so the service would not start")
        self.assertIn("--allow-non-loopback", flags)
        self.assertIn("--host", flags)

    def test_health_check_path_is_the_route_answered_before_authentication(self) -> None:
        self.assertEqual(self.scalar("healthCheckPath"), "/healthz")
        probe_index = self.api_source.index('if path == ["healthz"]:')
        auth_index = self.api_source.index("if not self._require_auth():", probe_index)
        self.assertLess(probe_index, auth_index, "the liveness probe must stay unauthenticated")
        block = self.api_source[probe_index:probe_index + 1200]
        self.assertIn('return self._json(200, {"status": "ok"})', block.split("\n            if")[0] + block,
                      "the probe answer is a fixed liveness object")
        self.assertNotIn("_require_auth", block.split('return self._json(200')[0],
                         "nothing may precede the liveness answer but the route match itself")

    def test_every_declared_environment_variable_is_read_by_the_code(self) -> None:
        pairs = self.env_pairs()
        self.assertEqual(sorted(pairs), sorted([
            "AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK", "AI_WEBSITE_AGENT_API_TOKEN", "AI_WEBSITE_AGENT_BIND_HOST",
            "AI_WEBSITE_AGENT_ENV", "AI_WEBSITE_AGENT_STORAGE_BACKEND", "DATABASE_URL",
        ]), "the blueprint's variable set changed without a matching code or documentation change")
        self.assertEqual(pairs["AI_WEBSITE_AGENT_ENV"], "production")
        self.assertEqual(pairs["AI_WEBSITE_AGENT_STORAGE_BACKEND"], "postgres")
        self.assertEqual(pairs["AI_WEBSITE_AGENT_BIND_HOST"], "0.0.0.0")
        self.assertEqual(pairs["AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK"], "true")
        for secret in ("DATABASE_URL", "AI_WEBSITE_AGENT_API_TOKEN"):
            self.assertEqual(pairs[secret], "", f"{secret} must be sync-only: a value here would commit a secret")
        sources = "\n".join((REPO_ROOT / module / name).read_text(encoding="utf-8")
                            for module in ("orchestration", "tracking", "deployment")
                            for name in sorted(os.listdir(REPO_ROOT / module)) if name.endswith(".py"))
        for key in pairs:
            self.assertIn(f'"{key}"', sources, f"render.yaml sets {key} but no code reads it")

    def test_build_command_never_migrates_and_dependencies_match_the_dialect(self) -> None:
        build = self.scalar("buildCommand")
        self.assertNotIn("migrat", build.casefold(), "migrations belong to the run command or a manual step")
        self.assertIn("requirements.txt", build)
        self.assertIn("psycopg", self.requirements, "the postgres backend the blueprint selects must be installed")
        self.assertEqual(self.scalar("autoDeploy"), "false")

    def test_serve_command_forwards_the_roots_it_documents(self) -> None:
        from orchestration.cli import main as orchestration_main
        with mock.patch("orchestration.cli.serve") as served:
            code = orchestration_main(["serve", "--state-path", "s.json", "--output-root", "out",
                                      "--export-root", "exports", "--repository-root", "repo"])
        self.assertEqual(code, 0)
        kwargs = served.call_args.kwargs
        self.assertEqual((kwargs["output_root"], kwargs["export_root"], kwargs["repository_root"]),
                         ("out", "exports", "repo"), "a documented flag the server ignores configures nothing")


# =============================================================================================
# 5. Storage: a database fault is reported, never papered over with local state
# =============================================================================================
class StorageRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "tracking.sqlite"

    def backend(self, **kwargs) -> RelationalStateBackend:
        return RelationalStateBackend(f"sqlite:///{self.db_path}", **kwargs)

    def test_a_database_read_failure_never_falls_back_to_local_json(self) -> None:
        def explode():
            raise ConnectionError(f"could not connect to postgresql://svc:{SECRET_CANARY}@db.internal:5432/db")

        state_file = self.root / "state.json"
        state_file.write_text(json.dumps({"leads": {"lead-9999": {"lead_id": "lead-9999"}}}), encoding="utf-8")
        backend = RelationalStateBackend("postgresql://svc:pw@db.internal:5432/db", connection_factory=explode,
                                         auto_migrate=False)
        with self.assertRaises(DatabasePersistenceError) as caught:
            TrackingStore(state_file, db_backend=backend)
        message = str(caught.exception)
        self.assertIn("db.internal", message, "the host is a useful, safe diagnostic")
        self.assertNotIn(SECRET_CANARY, message, "a credential must never reach an error message")
        self.assertNotIn("lead-9999", message, "state contents do not belong in a storage fault")
        self.assertTrue(state_file.is_file(), "the local file is left alone, not adopted or rewritten")
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["leads"].keys(), {"lead-9999"})

    def test_a_failed_transaction_rolls_back_and_leaves_the_stored_state_usable(self) -> None:
        backend = self.backend()
        backend.sync_state({"leads": {"lead-0001": {"lead_id": "lead-0001",
                                                    "business_name": "Fictional Sole Stitch Salon"}}})
        ledger = {"commits": 0, "rollbacks": 0}
        real_connect = sqlite3.connect

        def faulty(sqlite_path, **kwargs):
            return _InjectableConnection(real_connect(sqlite_path, **kwargs), ledger=ledger,
                                         fail_when=lambda sql: "INSERT INTO leads" in " ".join(str(sql).split()))

        with mock.patch("tracking.database.sqlite3.connect", side_effect=faulty):
            with self.assertRaises(DatabasePersistenceError) as caught:
                self.backend().sync_state({"leads": {"lead-0002": {"lead_id": "lead-0002",
                                                                  "business_name": "Fictional Second"}}})
        self.assertNotIn(SECRET_CANARY, str(caught.exception))
        self.assertEqual(ledger["commits"], 0, "an interrupted write of state must not commit")
        self.assertGreaterEqual(ledger["rollbacks"], 1, "the aborted transaction has to be rolled back")
        self.assertEqual(sorted(backend.load_state()["leads"]), ["lead-0001"],
                         "the aborted record must stay invisible after the fault")
        backend.sync_state({"leads": {
            "lead-0001": {"lead_id": "lead-0001", "business_name": "Fictional Sole Stitch Salon"},
            "lead-0003": {"lead_id": "lead-0003", "business_name": "Fictional Third"},
        }})
        self.assertEqual(sorted(backend.load_state()["leads"]), ["lead-0001", "lead-0003"],
                         "one failed transaction must not wedge the store")

    def test_the_migration_stays_inside_the_documented_postgres_feature_set(self) -> None:
        """No PostgreSQL server exists here, so the migration is checked against the version it claims."""
        sql = (REPO_ROOT / "migrations" / "0001_supabase_postgres_schema.sql").read_text(encoding="utf-8")
        declared = next(line for line in sql.splitlines()[:6] if "Compatible with" in line)
        minimum = int(re.search(r"PostgreSQL (\d+)", declared).group(1))
        self.assertGreaterEqual(minimum, 12, "the supported floor should be stated and modern")
        for label, pattern in {
            "SQL/JSON table functions (PostgreSQL 16+)": r"\bJSON_TABLE\s*\(",
            "MERGE statements (narrows compatibility)": r"\bMERGE\s+INTO\b",
            "identity columns (unused by this schema)": r"GENERATED\s+ALWAYS\s+AS\s+IDENTITY",
            "extension install (needs superuser on Supabase)": r"CREATE\s+EXTENSION",
        }.items():
            self.assertIsNone(re.search(pattern, sql, re.IGNORECASE),
                              f"{label} would break the declared PostgreSQL {minimum}+ target")
        for feature in (r"\bON\s+CONFLICT\b", r"\bJSONB\b", r"ADD COLUMN IF NOT EXISTS"):
            if re.search(feature, sql, re.IGNORECASE):
                self.assertGreaterEqual(minimum, 9, f"{feature} is older than the declared floor")
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("schema_migrations", sql)
        self.assertIn("schema_migrations", (REPO_ROOT / "tracking" / "database.py").read_text(encoding="utf-8"),
                      "an applied migration has to be recorded, not assumed")


# =============================================================================================
# 6. Isolation: what this phase's tests are allowed to touch
# =============================================================================================
class TestIsolationTests(unittest.TestCase):
    """The suite must not disturb the workspace it is run from.

    `data/tracking/state.json`, the committed fictional build artifact, and the exported review package are
    real project data (gitignored, not recoverable from Git). Every test in this module works in a temporary
    directory; this guards that claim so a future change here cannot quietly start writing into the
    workspace it is being tested in.
    """

    PROTECTED = (
        Path("data/tracking/state.json"),
        Path("output/lead-0002/build-2026-10-13T012227z/index.html"),
        Path("output/lead-0002/build-2026-10-13T012227z/build.json"),
        Path("preview-export/lead-0002-pitstop-lagos/index.html"),
    )

    def test_the_workspace_data_this_module_reads_is_left_byte_identical(self) -> None:
        before = {relative: (REPO_ROOT / relative).read_bytes() for relative in self.PROTECTED
                  if (REPO_ROOT / relative).is_file()}
        self.assertTrue(before, "the workspace should contain the records this test protects")

        case = UncertainPublishCliTests("test_resolve_flag_without_a_note_is_refused_before_anything_changes")
        case.setUp()
        try:
            case.test_resolve_flag_without_a_note_is_refused_before_anything_changes()
        finally:
            case.doCleanups()
            case._stop_servers()

        for relative, payload in before.items():
            self.assertEqual((REPO_ROOT / relative).read_bytes(), payload,
                             f"{relative} was modified while the test ran in a temporary directory")

    def test_no_test_in_this_module_constructs_the_default_state_store(self) -> None:
        """A store built with no path resolves to the workspace's own ``data/tracking/state.json``."""
        source = Path(__file__).read_text(encoding="utf-8")
        no_argument = re.findall(r"TrackingStore\(\s*\)", source)
        self.assertEqual(no_argument, [], "a default-path store reads - and on any mutation writes - real state")
        writes = [line.strip() for line in source.splitlines()
                  if re.search(r"(write_text|write_bytes|mkdir|unlink|touch)\(", line)
                  and "REPO_ROOT" in line and "read_text" not in line]
        self.assertEqual(writes, [], "this module may read the workspace, never write into it")


if __name__ == "__main__":
    unittest.main()
