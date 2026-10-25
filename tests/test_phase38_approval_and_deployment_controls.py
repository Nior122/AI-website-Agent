"""Phase 38 tests: explicit human approval and safe deployment controls.

These tests cover only the approval and deployment-control behaviour added in this phase:

1. Generated / validated / awaiting-review / approved / deployed / rejected stay distinct, and a
   build or a PASS validation never implies approval or deployment.
2. Approve, reject, and request-changes are recorded (project, lead, build, validation, decision,
   timestamp, reviewer identity); an identical repeat is idempotent, a conflicting or stale
   decision is refused, and a decision about an older build never approves a newer one.
3. The server-side gate at the operation that actually publishes rejects an unapproved attempt
   without any external provider call.
4. A dry run is never a deployment; a retried job does not blindly repeat a recorded deployment;
   an interrupted publish lands in an explicit uncertain, recoverable state.
5. Approval history stays append-only for distinct decisions and the approval is single-use.
6. Every server-side entry point (worker, job service, operator CLI, n8n API, deployment CLI)
   refuses the same unauthorized state with the same code.
7. Real-business data follows the same approval rule plus its isolation rules.

Everything here uses fictional ``TEST_FIXTURE`` data and isolated fictional fixtures in temporary
directories, a fake provider CLI, and loopback HTTP only. No real lead, credential, outreach
message, production target, or actual deployment is involved, and nothing in this file can
publish anything.
"""

from __future__ import annotations

from contextlib import chdir, redirect_stdout
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
import urllib.error
import urllib.request
import unittest
import zlib
from unittest.mock import patch

from deployment.authorization import (
    APPROVAL_FILENAME,
    ATTEMPTS_FILENAME,
    approval_digest,
    approval_path,
    artifact_sha256,
    verify_approval,
    write_approval,
)
from deployment.cli import main as deployment_cli_main
from deployment.deployer import DeploymentResult, VercelDeploymentProvider
from orchestration.api import make_handler
from orchestration.jobs import JobService, WorkerResult
from orchestration.workers import LocalPipelineWorker
from tracking.cli import run_cli
from tracking.store import TrackingStore

PREVIEW_URL = "https://fictional-studio-git-preview.vercel.app"
HTML_V1 = "<html><body><h1>Fictional Review Bistro — FICTIONAL TEST BUSINESS</h1></body></html>"


class _RecordingRunner:
    """Stand-in for the provider CLI: records invocations and replays a canned result."""

    def __init__(self, stdout: str = PREVIEW_URL + "\n", returncode: int = 0, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.stdout, self.returncode, self.error = stdout, returncode, error

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


class _CountingWorker:
    """Worker double used to prove the job-service gate runs before any execution."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, job, context):
        self.calls += 1
        return WorkerResult("COMPLETED", summary="should never run")


class ApprovalTestCase(unittest.TestCase):
    """Shared fictional project/build/validation fixture plus a linked preview project."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state_path = self.root / "state.json"
        self.store = TrackingStore(self.state_path)
        self.lead = self._lead("Fictional Review Bistro")
        self.project = self.store.create_project(self.lead["lead_id"])
        self.project_id = self.project["project_id"]
        self.build_id = self._build_and_validate("build-0001", "validation-0001", HTML_V1)
        linked = self.root / ".vercel"
        linked.mkdir()
        (linked / "project.json").write_text(
            json.dumps({"projectId": "prj_fixture", "orgId": "team_fixture", "projectName": "ai-website-agent-preview"}),
            encoding="utf-8",
        )

    # -- fixture helpers -------------------------------------------------------------------
    def _lead(self, name: str, *, classification: str = "TEST_FIXTURE", store: TrackingStore | None = None) -> dict:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        digits = f"{abs(zlib.crc32(name.encode())) % 100000000:08d}"
        return (store or self.store).create_lead({
            "business_name": name,
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": f"+234 800 {digits[:4]} {digits[4:]}",
            "website_status": "WEBSITE_OUTDATED",
            "existing_website": f"https://{slug}.example.invalid",
            "source_urls": [f"https://fixture.example.invalid/{slug}"],
            "qualification_status": "QUALIFIED",
            "data_classification": classification,
        })

    def build_dir(self, build_id: str | None = None, lead_id: str | None = None) -> Path:
        return self.root / "output" / (lead_id or self.lead["lead_id"]) / (build_id or self.build_id)

    def _build_and_validate(
        self,
        build_id: str,
        validation_id: str,
        html: str,
        *,
        project_id: str | None = None,
        lead_id: str | None = None,
        data_classification: str = "TEST_FIXTURE",
        store: TrackingStore | None = None,
    ) -> str:
        lead_id = lead_id or self.lead["lead_id"]
        build_dir = self.build_dir(build_id, lead_id)
        build_dir.mkdir(parents=True, exist_ok=True)
        index_path = build_dir / "index.html"
        index_path.write_text(html, encoding="utf-8")
        (build_dir / "build.json").write_text(json.dumps({
            "build_id": build_id,
            "lead_id": lead_id,
            "status": "COMPLETED",
            "data_classification": data_classification,
            "output_location": str(index_path),
            "validation": {"validation_id": validation_id, "lead_id": lead_id, "build_id": build_id, "status": "PASS"},
        }), encoding="utf-8")
        (store or self.store).record_build(project_id or self.project_id, {
            "build_id": build_id,
            "lead_id": lead_id,
            "status": "COMPLETED",
            "template_id": "restaurant-menu-v1",
            "data_classification": data_classification,
            "output_location": str(index_path),
        })
        (store or self.store).record_validation(project_id or self.project_id, {
            "validation_id": validation_id,
            "build_id": build_id,
            "status": "PASS",
            "score": 100,
        })
        return build_id

    def _approve(self, notes: str = "Fictional copy and layout approved for preview.", *, reviewer: str = "operator:ada") -> dict:
        return self.store.record_review(
            self.project_id,
            "APPROVED_FOR_PREVIEW",
            notes,
            reviewer=reviewer,
            action="PREVIEW_DEPLOYMENT",
            identity_source="test-harness",
        )

    def _provider(self, runner=None, **kwargs) -> VercelDeploymentProvider:
        return VercelDeploymentProvider(self.root, runner or _RecordingRunner(), **kwargs)

    def _capability(self, build_id: str | None = None, lead_id: str | None = None) -> dict:
        path = approval_path(self.build_dir(build_id, lead_id))
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _attempts(self, build_id: str | None = None, lead_id: str | None = None) -> list[dict]:
        path = self.build_dir(build_id, lead_id) / ATTEMPTS_FILENAME
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def _gate(self, build_id: str | None = None, project_id: str | None = None) -> dict:
        return self.store.authorize_preview_deployment(project_id or self.project_id, build_id=build_id)

    def _api(self, service: JobService, token: str = "test-token"):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, token, worker=False))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def request(method: str, path: str, body: dict | None = None, *, authenticated: bool = True):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                payload = error.read().decode("utf-8")
                try:
                    return error.code, json.loads(payload)
                except json.JSONDecodeError:
                    return error.code, {"raw": payload}

        def shutdown():
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.addCleanup(shutdown)
        return request

    def _run_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run_cli(["--state-path", str(self.state_path), *argv], stdout=out, stderr=err)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip().startswith("{") else {}), err.getvalue()

    def _deployment_cli(self, argv: list[str]) -> tuple[int, dict]:
        out = io.StringIO()
        with chdir(self.root), redirect_stdout(out):
            code = deployment_cli_main(argv)
        text = out.getvalue()
        return code, (json.loads(text) if text.strip().startswith("{") else {"raw": text})


class ApprovalDecisionTests(ApprovalTestCase):
    """The lifecycle states stay distinct and only a human can move a build to approved."""

    def test_generated_and_validated_is_neither_approved_nor_deployed(self):
        project = self.store.state["projects"][self.project_id]
        self.assertEqual(project["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(project["review_status"], "PENDING")
        self.assertIsNone(project.get("approved_build_id"))
        self.assertIsNone(project.get("latest_deployment_id"))
        self.assertEqual(self.store.snapshot(self.project_id)["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")
        self.assertFalse(approval_path(self.build_dir()).is_file(), "a PASS validation must not create an approval capability")
        gate = self._gate()
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "APPROVAL_MISSING")
        self.assertIn("approval", gate["reason"])

    def test_approval_records_the_decision_without_deploying_anything(self):
        self._approve()
        entry = self.store.get_review_history(self.project_id)[-1]
        for field in (
            "review_id", "project_id", "lead_id", "build_id", "validation_id", "decision",
            "intended_action", "notes", "reviewer", "timestamp", "identity_source",
        ):
            self.assertIn(field, entry, f"review history must record {field}")
        self.assertEqual(entry["decision"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(entry["build_id"], self.build_id)
        self.assertEqual(entry["validation_id"], "validation-0001")
        self.assertEqual(entry["reviewer"], "operator:ada")
        self.assertEqual(entry["identity_source"], "test-harness")
        # The decision authorizes one action on one build; it is not a deployment.
        project = self.store.state["projects"][self.project_id]
        self.assertEqual(project["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(project["review_status"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(project["approved_build_id"], self.build_id)
        self.assertEqual(project["approved_action"], "PREVIEW_DEPLOYMENT")
        self.assertIsNone(project["latest_deployment_id"])
        self.assertEqual(self.store.state["deployments"], {})
        # It does materialize the narrow capability the publish step verifies.
        capability = self._capability()
        self.assertEqual(capability["build_id"], self.build_id)
        self.assertEqual(capability["action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(capability["artifact_sha256"], artifact_sha256(self.build_dir()))
        self.assertEqual(
            capability["approval_sha256"],
            approval_digest({key: value for key, value in capability.items() if key != "approval_sha256"}),
        )
        self.assertFalse(capability["reviewer"]["identity_verified"])
        self.assertEqual(capability["reviewer"]["reviewer_id"], "operator:ada")
        gate = self._gate()
        self.assertTrue(gate["authorized"])
        self.assertEqual(gate["code"], "APPROVED_FOR_DEPLOYMENT")
        self.assertEqual(gate["review_id"], entry["review_id"])
        self.assertTrue(verify_approval(self.build_dir(), manifest=json.loads((self.build_dir() / "build.json").read_text()))[0])

    def test_repeated_identical_approval_is_idempotent(self):
        self._approve()
        first = self.store.get_review_history(self.project_id)
        self._approve()
        second = self.store.get_review_history(self.project_id)
        self.assertEqual(len(first), len(second), "an identical decision must not duplicate history")
        self.assertEqual(second[-1]["review_id"], first[-1]["review_id"])
        self.assertEqual(len(self.store.state["reviews"]), 1)
        self.assertTrue(self._gate()["authorized"])

    def test_rejection_after_approval_is_appended_and_revokes_the_capability(self):
        self._approve()
        self.store.record_review(self.project_id, "REJECTED", "Fictional business refused any public preview.", action="DISCARD")
        history = self.store.get_review_history(self.project_id)
        self.assertEqual([entry["decision"] for entry in history], ["APPROVED_FOR_PREVIEW", "REJECTED"])
        capability = self._capability()
        self.assertEqual(capability["status"], "REVOKED", "history is append-only; the capability is revoked, never rewritten")
        self.assertIn("revoked_reason", capability)
        gate = self._gate()
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "REVIEW_REJECTED")
        refused = self._provider(_RecordingRunner()).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_REVOKED", refused.error or "")

    def test_change_request_revokes_and_resets_the_build_cycle(self):
        self._approve()
        self.store.record_review(self.project_id, "CHANGES_REQUESTED", "Fictional price list is stale.", action="REBUILD")
        self.assertEqual(self._capability()["status"], "REVOKED")
        project = self.store.state["projects"][self.project_id]
        self.assertEqual(project["current_stage"], "BUILDING")
        self.assertEqual(self.store.snapshot(self.project_id)["workflow_state"], "CHANGES_REQUESTED")
        gate = self._gate()
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "REVIEW_CHANGES_REQUESTED")
        refused = self._provider(_RecordingRunner()).deploy(self.build_dir(), dry_run=False)
        self.assertIn("APPROVAL_REVOKED", refused.error or "")

    def test_a_stale_build_approval_cannot_authorize_a_newer_build(self):
        self._approve()
        second = self._build_and_validate("build-0002", "validation-0002", HTML_V1.replace("Bistro", "Bistro v2"))
        self.assertEqual(self._capability("build-0001")["status"], "REVOKED", "superseding a build revokes the old capability")
        gate = self._gate()
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "APPROVAL_MISSING")
        self.assertEqual(gate["build_id"], second)
        # Even a copied older capability cannot authorize the newer build directory.
        write_approval(self.build_dir(second), {
            "project_id": self.project_id,
            "lead_id": self.lead["lead_id"],
            "build_id": "build-0001",
            "validation_id": "validation-0001",
            "review_id": "review-0001",
            "decision": "APPROVED_FOR_PREVIEW",
            "action": "PREVIEW_DEPLOYMENT",
            "artifact_sha256": artifact_sha256(self.build_dir(second)),
        })
        runner = _RecordingRunner()
        refused = self._provider(runner).deploy(self.build_dir(second), dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_BUILD_MISMATCH", refused.error or "")
        self.assertEqual(runner.calls, [], "a build mismatch must not reach the provider CLI")

    def test_invalid_review_transitions_are_rejected(self):
        with self.assertRaises(ValueError) as not_deployed:
            self.store.record_review(self.project_id, "APPROVED", "Fictional outreach approval.", action="OUTREACH_PREPARATION")
        self.assertIn("deployed preview", str(not_deployed.exception))
        with self.assertRaises(ValueError) as stale_build:
            self.store.record_review(self.project_id, "APPROVED_FOR_PREVIEW", "Fictional approval.", build_id="build-9999")
        self.assertIn("does not match project's assigned_build_id", str(stale_build.exception))
        self.store.record_review(self.project_id, "CHANGES_REQUESTED", "Fictional rebuild requested.", action="REBUILD")
        with self.assertRaises(ValueError) as wrong_stage:
            self.store.record_review(self.project_id, "APPROVED_FOR_PREVIEW", "Fictional approval.")
        self.assertIn("READY_FOR_REVIEW", str(wrong_stage.exception))
        self.assertEqual([entry["decision"] for entry in self.store.get_review_history(self.project_id)], ["CHANGES_REQUESTED"])


class PublishBoundaryTests(ApprovalTestCase):
    """The gate that matters is the operation that actually initiates the publish."""

    def test_publish_without_approval_never_reaches_the_provider_cli(self):
        runner = _RecordingRunner()
        refused = self._provider(runner).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_MISSING", refused.error or "")
        self.assertEqual(refused.metadata.get("denial_code"), "APPROVAL_MISSING")
        self.assertIsNone(refused.deployment_url)
        self.assertEqual(runner.calls, [])
        self.assertNotIn("DEPLOYING", [attempt["deployment_status"] for attempt in self._attempts()])
        recorded = json.loads((self.build_dir() / "deployment.json").read_text(encoding="utf-8"))
        self.assertIn("APPROVAL_MISSING", recorded["error"])
        self.assertIsNone(recorded["deployment_url"])

    def test_tampered_capability_is_refused(self):
        self._approve()
        damaged = {**self._capability(), "artifact_sha256": "0" * 64}
        (self.build_dir() / APPROVAL_FILENAME).write_text(json.dumps(damaged), encoding="utf-8")
        runner = _RecordingRunner()
        refused = self._provider(runner).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_TAMPERED", refused.error or "")
        self.assertEqual(runner.calls, [])

    def test_capability_is_bound_to_the_exact_artifact_bytes(self):
        self._approve()
        (self.build_dir() / "index.html").write_text(HTML_V1 + "<p>edited after approval</p>", encoding="utf-8")
        runner = _RecordingRunner()
        refused = self._provider(runner).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_ARTIFACT_STALE", refused.error or "")
        self.assertEqual(runner.calls, [])

    def test_approval_for_a_different_action_is_refused(self):
        write_approval(self.build_dir(), {
            "project_id": self.project_id,
            "lead_id": self.lead["lead_id"],
            "build_id": self.build_id,
            "validation_id": "validation-0001",
            "review_id": "review-0001",
            "decision": "APPROVED_FOR_PREVIEW",
            "action": "OUTREACH_PREPARATION",
        })
        runner = _RecordingRunner()
        refused = self._provider(runner).deploy(self.build_dir(), dry_run=False)
        self.assertIn("APPROVAL_WRONG_ACTION", refused.error or "")
        self.assertEqual(runner.calls, [])
        # ...while a dry run of the same build stays possible without any approval.
        self.assertEqual(self._provider(runner).deploy(self.build_dir()).deployment_status, "PENDING")
        self.assertEqual(runner.calls, [])

    def test_dry_run_needs_no_approval_and_records_no_deployment(self):
        runner = _RecordingRunner()
        dry = self._provider(runner).deploy(self.build_dir())
        self.assertEqual(dry.deployment_status, "PENDING")
        self.assertEqual(runner.calls, [], "a dry run must not invoke the provider CLI")
        self.assertEqual(self._attempts(), [], "a dry run must not enter the publish ledger")
        manifest = json.loads((self.build_dir() / "deployment.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["deployment_status"], "PENDING")
        self.assertTrue(manifest["metadata"]["dry_run"])
        self.assertIsNone(manifest["deployment_url"])
        with self.assertRaises(ValueError) as refuse:
            self.store.record_deployment(self.project_id, {**dry.to_dict(), "validation_id": "validation-0001"})
        self.assertIn("successful PASS deployment", str(refuse.exception))
        self.assertIsNone(self.store.state["projects"][self.project_id]["latest_deployment_id"])

    def test_approved_publish_is_ledgered_and_the_approval_is_single_use(self):
        self._approve()
        runner = _RecordingRunner()
        deployed = self._provider(runner).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(deployed.deployment_status, "DEPLOYED")
        self.assertEqual(deployed.deployment_url, PREVIEW_URL)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual([attempt["deployment_status"] for attempt in self._attempts()], ["DEPLOYING", "DEPLOYED"])
        self.assertIn("--project", runner.calls[0])
        self.assertNotIn("--prod", runner.calls[0])
        self.store.record_deployment(self.project_id, {**deployed.to_dict(), "validation_id": "validation-0001"})
        self.assertEqual(self._capability()["status"], "REVOKED", "one approval authorizes one publish")
        second_runner = _RecordingRunner()
        repeat = self._provider(second_runner).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(repeat.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_REVOKED", repeat.error or "")
        self.assertEqual(second_runner.calls, [])
        gate = self._gate()
        self.assertTrue(gate["authorized"], "the recorded deployment is already complete")
        self.assertTrue(gate["skip_publish"])
        self.assertEqual(gate["code"], "ALREADY_DEPLOYED")

    def test_repeat_publish_of_the_same_artifact_is_refused_without_recalling_the_provider(self):
        self._approve()
        runner = _RecordingRunner()
        provider = self._provider(runner)
        self.assertEqual(provider.deploy(self.build_dir(), dry_run=False).deployment_status, "DEPLOYED")
        refused = provider.deploy(self.build_dir(), dry_run=False)
        self.assertEqual(refused.deployment_status, "FAILED")
        self.assertIn("DEPLOYMENT_ALREADY_RECORDED", refused.error or "")
        self.assertEqual(refused.metadata.get("denial_code"), "DEPLOYMENT_ALREADY_RECORDED")
        self.assertEqual(len(runner.calls), 1)

    def test_interrupted_publish_is_uncertain_and_blocks_an_automatic_retry(self):
        self._approve()
        interrupted = _RecordingRunner(error=subprocess.TimeoutExpired(cmd=["vercel"], timeout=300))
        first = self._provider(interrupted).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(first.deployment_status, "PUBLISH_UNCERTAIN")
        self.assertIsNone(first.deployment_url)
        self.assertIn("may have been accepted", first.error or "")
        self.assertEqual([attempt["deployment_status"] for attempt in self._attempts()], ["DEPLOYING", "PUBLISH_UNCERTAIN"])

        retry_runner = _RecordingRunner()
        refused = self._provider(retry_runner).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("PUBLISH_STATE_UNCERTAIN", refused.error or "")
        self.assertEqual(retry_runner.calls, [], "an uncertain publish must not be retried automatically")

        resolved = self._provider(retry_runner).deploy(self.build_dir(), dry_run=False, resolve_uncertain_publish=True)
        self.assertEqual(resolved.deployment_status, "DEPLOYED", "an explicit operator resolution allows a deliberate retry")
        self.assertEqual(len(retry_runner.calls), 1)
        self.assertEqual(self._attempts()[-1]["deployment_status"], "DEPLOYED")

    def test_missing_provider_cli_is_reported_as_incomplete_configuration(self):
        self._approve()
        with patch("deployment.deployer.shutil.which", return_value=None):
            result = VercelDeploymentProvider(self.root).deploy(self.build_dir(), dry_run=False)
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertIn("not available on PATH", result.error or "")
        self.assertNotIn("DEPLOYING", [attempt["deployment_status"] for attempt in self._attempts()])

    def test_worker_records_the_publish_once_and_never_repeats_it(self):
        self._approve()
        runner = _RecordingRunner()
        worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output", deployment_provider=self._provider(runner))
        job = {"job_id": "deploy-1", "job_type": "DEPLOY", "lead_id": self.lead["lead_id"], "project_id": self.project_id,
               "build_id": self.build_id, "input": {}, "dry_run": False}
        result = worker.execute(job, {"tracking_store": self.store, "dry_run": False})
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output["outcome"], "PREVIEW_DEPLOYED")
        self.assertEqual(self.store.state["projects"][self.project_id]["current_stage"], "PREVIEW_DEPLOYED")
        self.assertEqual(self.store.snapshot(self.project_id)["workflow_state"], "ISOLATED_PREVIEW_DEPLOYED")
        repeat = worker.execute(job, {"tracking_store": self.store, "dry_run": False})
        self.assertEqual(repeat.status, "COMPLETED")
        self.assertEqual(repeat.output["outcome"], "ALREADY_DEPLOYED_REPEAT_SKIPPED")
        self.assertEqual(len(runner.calls), 1)


class RelationalBackendApprovalTests(ApprovalTestCase):
    """The approval and its refusal reasons must survive the configured relational test backend."""

    def test_approval_and_gate_round_trip_through_the_relational_backend(self):
        sqlite_url = f"sqlite:///{self.root / 'approval.sqlite'}"
        store = TrackingStore(self.root / "relational-state.json", database_url=sqlite_url)
        lead = store.create_lead({
            "business_name": "Fictional Relational Bistro",
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 800 313 3133",
            "website_status": "WEBSITE_OUTDATED",
            "existing_website": "https://relational-bistro.example.invalid",
            "source_urls": ["https://fixture.example.invalid/relational-bistro"],
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        project = store.create_project(lead["lead_id"])
        build_id = self._build_and_validate(
            "build-rel-0001", "validation-rel-0001", "<html><body>Fictional relational bistro</body></html>",
            project_id=project["project_id"], lead_id=lead["lead_id"], store=store,
        )
        self.assertFalse(store.authorize_preview_deployment(project["project_id"])["authorized"])
        store.record_review(
            project["project_id"], "APPROVED_FOR_PREVIEW", "Fictional approval stored relationally.",
            reviewer="operator:relational", action="PREVIEW_DEPLOYMENT", identity_source="test-harness",
        )
        self.assertEqual(store.authorize_preview_deployment(project["project_id"])["code"], "APPROVED_FOR_DEPLOYMENT")

        # A second, independent store instance must reach the same verdict from committed state.
        fresh = TrackingStore(self.root / "fresh-state.json", database_url=sqlite_url)
        fresh.refresh()
        entry = fresh.get_review_history(project["project_id"])[-1]
        self.assertEqual(entry["decision"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(entry["reviewer"], "operator:relational")
        self.assertEqual(entry["identity_source"], "test-harness")
        self.assertEqual(entry["build_id"], build_id)
        gate = fresh.authorize_preview_deployment(project["project_id"])
        self.assertTrue(gate["authorized"], gate["reason"])
        self.assertEqual(gate["artifact_sha256"], artifact_sha256(self.build_dir(build_id, lead["lead_id"])))
        # ...and the refusal reason stays identical for an unapproved sibling build.
        other_lead = self._lead("Fictional Unapproved Bistro", store=store)
        other_project = store.create_project(other_lead["lead_id"])
        self.assertEqual(store.authorize_preview_deployment(other_project["project_id"])["code"], "BUILD_NOT_ASSIGNED")


class EntryPathParityTests(ApprovalTestCase):
    """API, CLI, worker, and job service must all refuse the same unauthorized state."""

    def test_worker_refuses_an_unauthorized_publish(self):
        provider = _RecordingRunner()
        worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output", deployment_provider=self._provider(provider))
        job = {"job_id": "deploy-noauth", "job_type": "DEPLOY", "lead_id": self.lead["lead_id"], "project_id": self.project_id,
               "build_id": self.build_id, "input": {}, "dry_run": False}
        refused = worker.execute(job, {"tracking_store": self.store, "dry_run": False})
        self.assertEqual(refused.status, "NEEDS_REVIEW")
        self.assertFalse(refused.retryable)
        self.assertEqual(refused.output["outcome"], "DEPLOYMENT_UNAUTHORIZED")
        self.assertEqual(refused.output["authorization_code"], "APPROVAL_MISSING")
        self.assertIsNone(refused.output.get("deployment_id"), "a refusal never reports a deployment id")
        self.assertEqual(provider.calls, [])
        self.assertIsNone(self.store.state["projects"][self.project_id]["latest_deployment_id"])
        dry = worker.execute({**job, "dry_run": True}, {"tracking_store": self.store, "dry_run": True})
        self.assertEqual(dry.status, "COMPLETED")
        self.assertEqual(dry.output["outcome"], "DRY_RUN_VERIFIED_NOT_DEPLOYED")
        self.assertEqual(provider.calls, [])

    def test_worker_surfaces_an_uncertain_publish_as_review(self):
        self._approve()
        runner = _RecordingRunner(error=subprocess.TimeoutExpired(cmd=["vercel"], timeout=300))
        worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output", deployment_provider=self._provider(runner))
        job = {"job_id": "deploy-timeout", "job_type": "DEPLOY", "lead_id": self.lead["lead_id"], "project_id": self.project_id,
               "build_id": self.build_id, "input": {}, "dry_run": False}
        result = worker.execute(job, {"tracking_store": self.store, "dry_run": False})
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertEqual(result.output["outcome"], "PUBLISH_STATE_UNCERTAIN")
        self.assertFalse(result.retryable)

    def test_job_service_blocks_an_unauthorized_deploy_job(self):
        jobs = JobService(self.store)
        jobs.create("DEPLOY", job_id="deploy-blocked", lead_id=self.lead["lead_id"], project_id=self.project_id, build_id=self.build_id, dry_run=False)
        worker = _CountingWorker()
        blocked = jobs.run("deploy-blocked", worker)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("explicit human approval", blocked["error"])
        self.assertIn("APPROVAL_MISSING", blocked["error"])
        self.assertEqual(worker.calls, 0)
        # a dry-run DEPLOY job stays allowed, because it cannot publish
        jobs.create("DEPLOY", job_id="deploy-dry", lead_id=self.lead["lead_id"], project_id=self.project_id, build_id=self.build_id, dry_run=True)
        self.assertEqual(jobs.run("deploy-dry", worker)["status"], "COMPLETED")

    def test_deployment_cli_refuses_execute_without_the_store_gate(self):
        code, payload = self._deployment_cli(["--build", str(self.build_dir()), "--execute", "--state-path", str(self.state_path)])
        self.assertEqual(code, 1)
        self.assertEqual(payload["code"], "PROJECT_ID_REQUIRED")
        self.assertFalse((self.build_dir() / "deployment.json").exists(), "a refused CLI call must not stage or record anything")

        with self.assertRaises(SystemExit) as missing_state:
            self._deployment_cli(["--build", str(self.build_dir()), "--execute", "--project-id", self.project_id, "--state-path", str(self.root / "absent.json")])
        self.assertIn("tracking state file", str(missing_state.exception))

        code, payload = self._deployment_cli([
            "--build", str(self.build_dir()), "--execute", "--project-id", self.project_id,
            "--state-path", str(self.state_path), "--build-id", "build-0001",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["authorized"])
        self.assertEqual(payload["code"], "APPROVAL_MISSING")
        self.assertEqual(payload["deployment_status"], "NEEDS_REVIEW")
        self.assertFalse(payload["provider_cli_invoked"])
        self.assertEqual(self._attempts(), [], "the CLI gate refuses before the publish boundary records an attempt")

    def test_deployment_cli_executes_only_after_an_approval_is_recorded(self):
        self._approve()
        stub = DeploymentResult(
            "deployment-0001", self.lead["lead_id"], "Fictional Review Bistro", self.build_id, "validation-0001", "PASS",
            "vercel", "DEPLOYED", PREVIEW_URL, "dpl_fixture", "prj_fixture", None, str(self.build_dir()),
            "2026-10-15T00:00:00Z", "2026-10-15T00:00:05Z", None, {"dry_run": False, "target": "preview"},
        )
        with patch("deployment.deployer.VercelDeploymentProvider.deploy", return_value=stub) as deploy:
            code, payload = self._deployment_cli([
                "--build", str(self.build_dir()), "--execute", "--project-id", self.project_id, "--state-path", str(self.state_path),
            ])
        self.assertEqual(code, 0)
        self.assertTrue(deploy.called)
        self.assertEqual(payload["deployment_status"], "DEPLOYED")
        self.assertEqual(payload["authorization"]["code"], "APPROVED_FOR_DEPLOYMENT")

    def test_operator_cli_review_and_deploy_check_share_one_answer(self):
        self._build_and_validate("build-0002", "validation-0002", HTML_V1.replace("Bistro", "Bistro v2"))
        code, payload, err = self._run_cli(["deploy-check", self.project_id])
        self.assertEqual(code, 1)
        self.assertEqual(payload["code"], "APPROVAL_MISSING")
        self.assertIn("APPROVED_FOR_PREVIEW", payload["next_command"])
        self.assertIn("Nothing was published", err)

        confirm = f"{self.project_id}:build-0002:APPROVED_FOR_PREVIEW"
        code, payload, _ = self._run_cli([
            "review", self.project_id, "--decision", "APPROVED_FOR_PREVIEW", "--action", "PREVIEW_DEPLOYMENT",
            "--notes", "Fictional v2 approved for an isolated preview.", "--reviewer", "operator:ben", "--confirm", confirm,
        ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["deployment_authorization"]["code"], "APPROVED_FOR_DEPLOYMENT")
        self.assertFalse(payload["deployed"], "the review command never deploys")
        self.assertEqual(payload["workflow_state"], "APPROVED_FOR_PREVIEW_DEPLOYMENT")

        code, payload, _ = self._run_cli(["deploy-check", self.project_id])
        self.assertEqual(code, 0)
        self.assertTrue(payload["authorized"])
        self.assertEqual(payload["code"], "APPROVED_FOR_DEPLOYMENT")

    def test_api_records_review_and_reports_authorization(self):
        jobs = JobService(self.store)
        request = self._api(jobs)
        code, payload = request("POST", f"/projects/{self.project_id}/review", {"decision": "APPROVED_FOR_PREVIEW", "notes": "x"})
        self.assertEqual(code, 400)
        self.assertIn("requires", payload["error"])
        self.assertIn("confirm", payload["error"])
        self.assertEqual(self.store.get_review_history(self.project_id), [])

        code, payload = request("POST", f"/projects/{self.project_id}/review", {
            "decision": "APPROVED_FOR_PREVIEW", "notes": "Fictional preview approved.", "reviewer": "operator:ciro",
            "confirm": f"{self.project_id}:build-0000:APPROVED_FOR_PREVIEW",
        })
        self.assertEqual(code, 409, "a stale confirmation must not record a decision")
        self.assertEqual(payload["expected_confirmation"], f"{self.project_id}:{self.build_id}:APPROVED_FOR_PREVIEW")
        self.assertEqual(self.store.get_review_history(self.project_id), [])

        code, payload = request("GET", f"/projects/{self.project_id}/deployment-authorization")
        self.assertEqual(code, 200)
        self.assertFalse(payload["authorized"])
        self.assertEqual(payload["code"], "APPROVAL_MISSING")

        code, payload = request("POST", f"/projects/{self.project_id}/review", {
            "decision": "APPROVED_FOR_PREVIEW", "notes": "Fictional preview approved via the automation API.",
            "reviewer": "operator:ciro", "confirm": f"{self.project_id}:{self.build_id}:APPROVED_FOR_PREVIEW",
        })
        self.assertEqual(code, 200)
        self.assertEqual(payload["current_stage"], "READY_FOR_REVIEW")
        self.assertFalse(payload["deployed"])
        self.assertFalse(payload["identity_verified"], "the API never asserts a verified human identity")
        self.assertTrue(payload["deployment_authorization"]["authorized"])
        self.assertEqual(payload["review"]["reviewer"], "operator:ciro")
        self.assertEqual(payload["review"]["identity_source"], "orchestration-api")
        self.assertEqual(self._capability()["reviewer"]["reviewer_id"], "operator:ciro")
        self.assertEqual(self._capability()["review_id"], payload["deployment_authorization"]["review_id"])

        code, payload = request("GET", f"/projects/{self.project_id}/deployment-authorization")
        self.assertEqual(code, 200)
        self.assertEqual(payload["code"], "APPROVED_FOR_DEPLOYMENT")

        code, payload = request("POST", f"/projects/{self.project_id}/review", {
            "decision": "REJECTED", "notes": "Fictional refusal.", "reviewer": "operator:ciro",
            "confirm": f"{self.project_id}:{self.build_id}:REJECTED",
        }, authenticated=False)
        self.assertEqual(code, 401, "the review endpoint is authenticated like every other mutation")

    def test_every_entry_path_refuses_the_same_unauthorized_build(self):
        self.assertEqual(self._gate()["code"], "APPROVAL_MISSING")
        code, payload, _ = self._run_cli(["deploy-check", self.project_id])
        self.assertEqual((code, payload["code"]), (1, "APPROVAL_MISSING"))
        jobs = JobService(self.store)
        created = jobs.create("DEPLOY", job_id="parity", lead_id=self.lead["lead_id"], project_id=self.project_id, build_id=self.build_id, dry_run=False)
        self.assertIn("APPROVAL_MISSING", jobs.run("parity", _CountingWorker())["error"])
        cli_code, cli_payload = self._deployment_cli([
            "--build", str(self.build_dir()), "--execute", "--project-id", self.project_id, "--state-path", str(self.state_path),
        ])
        self.assertEqual((cli_code, cli_payload["code"]), (1, "APPROVAL_MISSING"))
        request = self._api(jobs)
        api_code, api_payload = request("GET", f"/projects/{self.project_id}/deployment-authorization")
        self.assertEqual((api_code, api_payload["code"]), (200, "APPROVAL_MISSING"))
        provider_result = self._provider(_RecordingRunner()).deploy(self.build_dir(), dry_run=False)
        self.assertIn("APPROVAL_MISSING", provider_result.error or "")
        attempts = self._attempts()
        self.assertTrue(all(attempt.get("published") is False for attempt in attempts))
        self.assertNotIn("DEPLOYING", [attempt["deployment_status"] for attempt in attempts], "no entry path may start a publish without approval")
        self.assertIsNone(self.store.state["projects"][self.project_id]["latest_deployment_id"])
        self.assertEqual(created["status"], "BLOCKED", "the job is blocked before any execution")

    def test_real_public_data_follows_the_same_rule_plus_isolation(self):
        lead = self._lead("Fictional Isolated Salon", classification="REAL_PUBLIC_DATA")
        project = self.store.create_project(lead["lead_id"])
        build_id = self._build_and_validate(
            "build-real-0001", "validation-real-0001", "<html><body>Fictional isolated salon</body></html>",
            project_id=project["project_id"], lead_id=lead["lead_id"], data_classification="REAL_PUBLIC_DATA",
        )
        build_dir = self.build_dir(build_id, lead["lead_id"])
        gate = self.store.authorize_preview_deployment(project["project_id"])
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "APPROVAL_MISSING")
        self.assertEqual(gate["data_classification"], "REAL_PUBLIC_DATA")

        self.store.record_review(project["project_id"], "APPROVED_FOR_PREVIEW", "Fictional isolated salon approved for its own preview.", reviewer="operator:dani", action="PREVIEW_DEPLOYMENT")
        gate = self.store.authorize_preview_deployment(project["project_id"])
        self.assertFalse(gate["authorized"])
        self.assertEqual(gate["code"], "REAL_ISOLATED_TARGET_REQUIRED", "real data needs an isolated target as well as an approval")

        self.store.configure_preview_target(project["project_id"], "fictional-salon-preview")
        gate = self.store.authorize_preview_deployment(project["project_id"])
        self.assertTrue(gate["authorized"], gate["reason"])
        self.assertFalse(gate["skip_publish"])

        provider = VercelDeploymentProvider(self.root, _RecordingRunner(), expected_project_name="fictional-salon-preview")
        refused = provider.deploy(build_dir, dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("--approve-real-preview", refused.error or "")

        # Bind the approved real-business tracking project to a verified Vercel project ID. A local
        # .vercel link alone must no longer be enough to publish REAL_PUBLIC_DATA builds.
        self.store.begin_vercel_project_provisioning(
            project["project_id"], "fictional-salon-preview", base_name="fictional-salon-preview",
        )
        self.store.bind_vercel_project(
            project["project_id"], vercel_project_id="prj_fixture", project_name="fictional-salon-preview",
            provider_created_at="2026-10-23T00:00:00Z", binding_source="api-created",
        )
        api_client = type("FakeVercelClient", (), {
            "token": "fixture-token",
            "team_id": "team_fixture",
            "get_project": lambda self, project_id: {
                "id": project_id, "name": "fictional-salon-preview",
            },
            # The managed publish path must prove project ownership and READY state via provider read-back.
            "get_deployment_by_url": lambda self, url: {
                "id": "dpl_fixture",
                "url": "fictional-studio-git-preview.vercel.app",
                "project_id": "prj_fixture",
                "ready_state": "READY",
            },
        })()
        from unittest.mock import patch
        with patch("deployment.site_management.VercelProjectsClient", return_value=api_client):
            deployed = provider.deploy(
                build_dir, dry_run=False, approve_real_preview=True,
                project_name="fictional-salon-preview", vercel_project_id="prj_fixture",
            )
        self.assertEqual(deployed.deployment_status, "DEPLOYED", deployed.error)


if __name__ == "__main__":
    unittest.main()
