"""Phase 39 tests: monitoring and operational controls.

These exercise only the operational surfaces added in this phase, against fictional
``TEST_FIXTURE`` state in temporary directories, loopback HTTP, and an injected fake transport:

1. Liveness versus readiness: an alive process is not reported ready when the source of truth
   cannot be verified, and a readiness answer never carries credentials.
2. Job and project status reporting: timestamps, stage visibility, retry history and the
   project-level reasons the pipeline is stopped.
3. Durable failure recording: stable codes, sanitized messages that survive a reload, and no secret.
4. Retry eligibility and retry safety, including that no monitoring or retry surface can bypass the
   Phase 3 deployment approval gate.
5. Deployment uncertainty: an unresolved publish is reported as uncertain, never as success or as an
   ordinary failure, and read-only surfaces change nothing.
6. The notification hook: critical alerts come from recorded state, deduplicate, and only reach an
   externally supplied transport.

No real database, provider, publish, lead contact, or paid monitoring service is used, and no
worker in this file can publish anything.
"""

from __future__ import annotations

import contextlib
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
import unittest.mock as mock
import urllib.error
import urllib.request
import zlib

from deployment.authorization import ATTEMPTS_FILENAME, append_attempt, publish_outcome
from orchestration.alerts import LogAlertSink, WebhookAlertSink, check_alerts, critical_alerts
from orchestration.api import make_handler
from orchestration.cli import main as orchestration_main
from orchestration.health import (
    deployment_report,
    operator_attention,
    storage_report,
    system_health,
    verify_storage,
)
from orchestration.jobs import JobService, WorkerResult
from orchestration.observability import log_event, recorded_age_seconds, sanitize_diagnostic
from tracking.database import DatabasePersistenceError
from tracking.store import TrackingStore

FAINT_TOKEN = "vcnt_0f1c2a3b4c5d6e7f8a9b"
HTML = "<html><body><h1>Fictional Monitor Bistro — FICTIONAL TEST BUSINESS</h1></body></html>"


class _BrokenBackend:
    """Stand-in for an unreachable relational backend (the only way to test real outage states)."""

    dialect = "postgres"
    redacted_url = "db.test.example:5432/postgres"

    def load_state(self) -> dict:
        return {}

    def sync_state(self, state):  # noqa: ANN001
        return state

    def verify_schema(self) -> None:
        raise DatabasePersistenceError(f"Failed to connect to {self.redacted_url}: simulated connection refusal")


class _FakeWorker:
    """Worker double whose outcome each test decides, so no pipeline stage is really executed."""

    name = "fake-worker"

    def __init__(self, result: WorkerResult | None = None, *, raises: Exception | None = None) -> None:
        self.result = result or WorkerResult("COMPLETED", summary="fixture work", output={"stage": "SCOUT"})
        self.raises = raises
        self.calls = 0

    def execute(self, job, context):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


class MonitoringTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state_path = self.root / "state.json"
        self.store = TrackingStore(self.state_path)
        self.lead = self._lead("Fictional Monitor Bistro")
        self.project = self.store.create_project(self.lead["lead_id"])
        self.project_id = self.project["project_id"]
        self.build_id = self._build_and_validate("build-0001", "validation-0001")
        self.service = JobService(self.store)

    # -- fixtures ---------------------------------------------------------------------------
    def _lead(self, name: str, *, store: TrackingStore | None = None) -> dict:
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
            "data_classification": "TEST_FIXTURE",
        })

    def build_dir(self, build_id: str, lead_id: str | None = None) -> Path:
        return self.root / "output" / (lead_id or self.lead["lead_id"]) / build_id

    def _build_and_validate(self, build_id: str, validation_id: str, *, project_id: str | None = None,
                            store: TrackingStore | None = None, lead_id: str | None = None) -> str:
        lead_id = lead_id or self.lead["lead_id"]
        build_dir = self.build_dir(build_id, lead_id)
        build_dir.mkdir(parents=True, exist_ok=True)
        index = build_dir / "index.html"
        index.write_text(HTML, encoding="utf-8")
        (build_dir / "build.json").write_text(json.dumps({
            "build_id": build_id, "lead_id": lead_id, "status": "COMPLETED",
            "data_classification": "TEST_FIXTURE", "output_location": str(index),
            "validation": {"validation_id": validation_id, "lead_id": lead_id, "build_id": build_id, "status": "PASS"},
        }), encoding="utf-8")
        target = store or self.store
        target.record_build(project_id or self.project_id, {
            "build_id": build_id, "lead_id": lead_id, "status": "COMPLETED",
            "template_id": "restaurant-menu-v1", "data_classification": "TEST_FIXTURE",
            "output_location": str(index),
        })
        target.record_validation(project_id or self.project_id, {
            "validation_id": validation_id, "build_id": build_id, "status": "PASS", "score": 100,
        })
        return build_id

    def _server(self, service: JobService, token: str = "test-token-123", worker=False):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, token, worker=worker))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close(), thread.join(timeout=5)))

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
                raw = error.read().decode("utf-8")
                try:
                    return error.code, json.loads(raw)
                except json.JSONDecodeError:
                    return error.code, {"raw": raw}

        return request

    def _cli(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = orchestration_main([*argv, "--repository-root", str(self.root)])
        return code, json.loads(buffer.getvalue())

    # -- 1. liveness versus readiness -------------------------------------------------------
    def test_liveness_stays_alive_while_readiness_refuses_without_storage(self) -> None:
        store = TrackingStore(self.root / "readiness.json")
        store.state["jobs"] = {}
        store.db_backend = _BrokenBackend()
        self.assertEqual(storage_report(store)["status"], "healthy")  # configuration-only view
        self.assertEqual(verify_storage(store)["status"], "unavailable")  # active verification

        # A report built without a probe must not pretend the backend was checked.
        unprobed = system_health(store, repository_root=self.root)
        self.assertEqual(unprobed["source_of_truth"]["verification"], "configuration_only")
        # A configured-but-unprobed remote backend is never reported as healthy: an alive process
        # does not imply the source of truth was reached.
        self.assertEqual(unprobed["source_of_truth"]["status"], "unverified")
        self.assertIn("STORAGE_NOT_PROBED", [i["code"] for i in unprobed["readiness"]["degradations"]])
        self.assertEqual(unprobed["readiness"]["blocking"], [])

        health = system_health(store, repository_root=self.root, storage=verify_storage(store))
        self.assertEqual(health["source_of_truth"]["verification"], "verified")
        self.assertEqual(health["liveness"]["status"], "ALIVE")
        self.assertEqual(health["application"]["status"], "degraded")
        self.assertEqual(health["source_of_truth"]["status"], "unavailable")
        self.assertEqual(health["readiness"]["status"], "NOT_READY")
        self.assertEqual([item["code"] for item in health["readiness"]["blocking"]], ["STORAGE_UNAVAILABLE"])
        self.assertIn("python3 -m orchestration.cli", health["readiness"]["blocking"][0]["next_action"])

        request = self._server(JobService(store))
        status, payload = request("GET", "/healthz", authenticated=False)
        self.assertEqual((status, payload), (200, {"status": "ok"}))
        status, payload = request("GET", "/readiness")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "NOT_READY")
        self.assertEqual(payload["liveness"], "ALIVE")
        self.assertEqual(payload["error_code"], "STORAGE_UNAVAILABLE")
        self.assertEqual(payload["storage_backend"], "postgres")
        self.assertEqual([item["code"] for item in payload["blocking"]], ["STORAGE_UNAVAILABLE"])
        text = json.dumps(payload)
        self.assertNotIn("@", text)
        self.assertNotIn(FAINT_TOKEN, text)

    def test_readiness_is_ready_and_degrades_only_for_advisory_conditions(self) -> None:
        request = self._server(self.service)
        status, payload = request("GET", "/readiness")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["storage_backend"], "json")
        self.assertEqual(payload["blocking"], [])
        self.assertEqual(payload["liveness"], "ALIVE")

        health = system_health(self.store, repository_root=self.root)
        # No worker is registered in this fixture: the report must say "ready but no capacity",
        # never "not ready", because capacity is advisory for HTTP serving itself.
        self.assertEqual(health["readiness"]["status"], "DEGRADED")
        self.assertEqual(health["readiness"]["blocking"], [])
        self.assertIn("NO_AVAILABLE_WORKER", [item["code"] for item in health["readiness"]["degradations"]])
        self.assertEqual(health["worker_system"]["available_workers"], 0)
        self.assertEqual(health["configuration"]["status"], "healthy")
        self.assertFalse(health["configuration"]["api_token_configured"])
        self.assertNotIn(FAINT_TOKEN, json.dumps(health))

    def test_missing_local_build_artifacts_are_a_degradation_with_a_recovery_action(self) -> None:
        (self.build_dir(self.build_id) / "index.html").unlink()
        health = system_health(self.store, repository_root=self.root)
        codes = [item["code"] for item in health["readiness"]["degradations"]]
        self.assertIn("LOCAL_BUILD_ARTIFACTS_MISSING", codes)
        self.assertEqual(health["readiness"]["status"], "DEGRADED")
        item = next(i for i in health["readiness"]["degradations"] if i["code"] == "LOCAL_BUILD_ARTIFACTS_MISSING")
        self.assertIn("recover-workspace", item["next_action"])

    # -- 2. job and project status reporting ------------------------------------------------
    def test_timestamps_and_execution_history_are_recorded_per_attempt(self) -> None:
        job = self.service.create("SCOUT", job_id="mon-scout")
        completed = self.service.run(job["job_id"], _FakeWorker())
        self.assertEqual(completed["status"], "COMPLETED")
        for field in ("created_at", "started_at", "updated_at", "completed_at"):
            self.assertIsInstance(completed.get(field), str, field)
        executions = self.service.executions_for(job["job_id"])
        self.assertEqual(len(executions), 1)
        attempt = executions[0]
        self.assertEqual(attempt["attempt_number"], 1)
        self.assertEqual(attempt["sequence"], 1)
        self.assertEqual(attempt["status"], "COMPLETED")
        self.assertEqual(attempt["job_type"], "SCOUT")
        self.assertIsInstance(attempt["duration_seconds"], int)
        self.assertIsNone(attempt["error_code"])
        self.assertEqual(attempt["result"]["output"], {"stage": "SCOUT"})
        self.assertEqual(self.store.state["jobs"][job["job_id"]]["execution_id"], attempt["execution_id"])

        no_op = self.service.retry(job["job_id"], input={"again": True})
        self.assertEqual(no_op["status"], "COMPLETED")  # a completed job is never silently re-run

        review_job = self.service.create("SCOUT", job_id="mon-review")
        self.service.run(review_job["job_id"], _FakeWorker(result=WorkerResult(
            "NEEDS_REVIEW", summary="copy needs a decision", error="approve or reject the draft",
            error_type="HUMAN_REVIEW", error_code="HUMAN_REVIEW_REQUIRED")))
        self.assertEqual(self.service.get(review_job["job_id"])["status"], "NEEDS_REVIEW")
        retried = self.service.retry(review_job["job_id"], input={"again": True})
        self.assertEqual(retried["status"], "QUEUED")
        self.assertEqual(retried["input"]["again"], True)
        self.service.run(review_job["job_id"], _FakeWorker())
        history = self.service.executions_for(review_job["job_id"])
        self.assertEqual([record["attempt_number"] for record in history], [1, 2])
        self.assertEqual([record["status"] for record in history], ["NEEDS_REVIEW", "COMPLETED"])
        self.assertEqual(history[0]["error_code"], "HUMAN_REVIEW_REQUIRED")

    def test_blocked_and_stale_states_are_visible_without_inventing_evidence(self) -> None:
        job = self.service.create("DEPLOY", job_id="mon-deploy", project_id=self.project_id,
                                 build_id=self.build_id, dry_run=False)
        blocked = self.service.run(job["job_id"], _FakeWorker())
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["error_code"], "APPROVAL_MISSING")
        self.assertIn("explicit human approval", blocked["error"])
        self.assertEqual(blocked["error_code"], "APPROVAL_MISSING")
        self.assertIn("review project-0001", blocked["next_action"])
        self.assertIn("--confirm project-0001:build-0001", blocked["next_action"])
        self.assertTrue(blocked["requires_operator_action"])
        # A gated job never started: no attempt may be claimed.
        self.assertEqual(self.service.executions_for(job["job_id"]), [])
        self.assertIsNone(blocked["started_at"])
        self.assertEqual(blocked["attempts"], 0)

        # A running job whose heartbeat went stale is reported as needing inspection, with the age
        # derived from the recorded heartbeat only.
        self.service.create("SCOUT", job_id="mon-stale")
        self.service.run("mon-stale", _FakeWorker())
        self.store.state["jobs"]["mon-stale"].update({"status": "RUNNING", "heartbeat_at": "2020-01-01T00:00:00Z"})
        self.store._save()
        report = operator_attention(self.store, repository_root=self.root, include_deployment=False)
        kinds = {item["kind"] for item in report["items"]}
        self.assertIn("stale_job", kinds)
        stale_item = next(item for item in report["items"] if item["kind"] == "stale_job")
        self.assertEqual(stale_item["code"], "HEARTBEAT_EXPIRED")
        self.assertGreater(stale_item["age_seconds"], 900)
        self.assertIn("recover-stale", stale_item["next_action"])

        never_heartbeated = self.service.create("SCOUT", job_id="mon-queued")
        self.store.state["jobs"]["mon-queued"].update({"status": "RUNNING", "heartbeat_at": None, "started_at": None})
        self.store._save()
        ages = {
            item.get("job_id"): item.get("age_seconds")
            for item in operator_attention(self.store, repository_root=self.root, include_deployment=False)["items"]
        }
        self.assertIsNone(ages.get("mon-queued"))  # no timestamp recorded -> no age claimed

    def test_project_stage_reporting_keeps_review_validation_and_build_states_distinct(self) -> None:
        attention = operator_attention(self.store, repository_root=self.root, include_deployment=False)
        project_items = [item for item in attention["items"] if item["kind"] == "project"]
        self.assertEqual([item["code"] for item in project_items], ["AWAITING_HUMAN_REVIEW"])
        item = project_items[0]
        self.assertEqual(item["project_id"], self.project_id)
        self.assertEqual(item["build_id"], self.build_id)
        self.assertEqual(item["review_status"], "PENDING")
        self.assertIn("tracking.cli review", item["next_action"])
        self.assertNotIn("deploy", json.dumps(item).lower().replace("deployment_id", ""))

        self.store.record_review(self.project_id, "CHANGES_REQUESTED", "Hero copy is fictional placeholder only.",
                                reviewer="operator:test")
        follow_up = operator_attention(self.store, repository_root=self.root, include_deployment=False)
        project_codes = [item["code"] for item in follow_up["items"] if item["kind"] == "project"]
        self.assertIn("REVIEW_CHANGES_REQUESTED", project_codes)
        # A project the reviewer sent back is never reported as merely awaiting a first review.
        self.assertNotIn("AWAITING_HUMAN_REVIEW", project_codes)
        changes = next(i for i in follow_up["items"] if i["code"] == "REVIEW_CHANGES_REQUESTED")
        self.assertIn("Hero copy is fictional placeholder only", changes["message"])

    def test_health_reports_read_existing_records_without_adding_state(self) -> None:
        before = set(self.store.state)
        health = system_health(self.store, repository_root=self.root)
        self.assertEqual(health["job_system"]["jobs"], len(self.store.state["jobs"]))
        self.assertEqual(sum(health["workflow_execution"]["by_status"].values()), len(self.store.state["jobs"]))
        self.assertEqual(set(self.store.state), before)
        self.assertNotIn("attention_items", before)
        self.assertEqual(health["source_of_truth"]["local_state"]["status"], "HEALTHY")
        self.assertEqual(health["deployment"]["live_url_checked"], False)
        self.assertEqual(health["deployment"]["configuration"]["cli_available"], False)
        self.assertEqual(health["n8n"]["status"], "unavailable")
        self.assertEqual(health["n8n"]["workflows_shipped"], 0)  # fixture root has no n8n directory

    # -- 3. durable failure recording and sanitized diagnostics ---------------------------
    def test_worker_exception_is_recorded_durably_with_a_stable_code_and_no_secret(self) -> None:
        noisy = ValueError(
            f"Template fetch failed for Authorization: Bearer {FAINT_TOKEN} "
            f"and DATABASE_URL=postgres://svc:hunter2@db.internal:5432/state"
        )
        job = self.service.create("RESEARCH", job_id="mon-fail", lead_id=self.lead["lead_id"])
        failed = self.service.run(job["job_id"], _FakeWorker(raises=noisy))
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error_code"], "WORKER_FAULT")
        self.assertEqual(failed["failure"]["attempt"], 1)
        self.assertNotIn(FAINT_TOKEN, failed["error"])
        self.assertNotIn("hunter2", failed["error"])
        self.assertIn("REDACTED", failed["error"])
        self.assertIn("Template fetch failed", failed["error"])
        self.assertTrue(failed["next_action"])
        attempt = self.service.executions_for(job["job_id"])[-1]
        self.assertEqual(attempt["error_type"], "ValueError")
        self.assertEqual(attempt["error_code"], "WORKER_FAULT")
        self.assertNotIn(FAINT_TOKEN, json.dumps(attempt))

        reloaded = TrackingStore(self.state_path)
        persisted = reloaded.state["jobs"]["mon-fail"]
        self.assertEqual(persisted["status"], "FAILED")
        self.assertEqual(persisted["error_code"], "WORKER_FAULT")
        self.assertEqual(persisted["error"], failed["error"])
        self.assertEqual(len(reloaded.state.get("executions") or {}), 1)

    def test_sanitizer_handles_structured_and_connection_string_shapes(self) -> None:
        self.assertIsNone(sanitize_diagnostic(None))
        text = sanitize_diagnostic(
            "refused: postgres://user:pa55word@host/db?sslmode=require\n"
            f"token: {FAINT_TOKEN}\n" + "x" * 2000
        )
        self.assertNotIn("pa55word", text)
        self.assertNotIn(FAINT_TOKEN, text)
        self.assertNotIn("\n", text)
        self.assertTrue(text.endswith("[truncated]"))
        self.assertIsNone(recorded_age_seconds(None))
        self.assertIsNone(recorded_age_seconds("not-a-timestamp"))

    def test_failure_log_line_is_structured_and_secret_free(self) -> None:
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"AI_WEBSITE_AGENT_LOG_LEVEL": "DEBUG"}):
            record = log_event(
                severity="ERROR", operation="job.build", code="BUILD_FAILED",
                message=f"upload rejected (token={FAINT_TOKEN})",
                job_id="job-1", project_id=self.project_id, stream=stream,
            )
        line = stream.getvalue().strip()
        parsed = json.loads(line)
        self.assertEqual(parsed["code"], "BUILD_FAILED")
        self.assertEqual(parsed["job_id"], "job-1")
        self.assertEqual(parsed["severity"], "ERROR")
        self.assertNotIn(FAINT_TOKEN, line)
        self.assertIn("timestamp", record)

    def test_api_and_health_surfaces_never_echo_credentials(self) -> None:
        job = self.service.create("RESEARCH", job_id="mon-secret", lead_id=self.lead["lead_id"])
        self.service.run(job["job_id"], _FakeWorker(raises=RuntimeError(
            f"connection string postgres://admin:{FAINT_TOKEN}@db.internal/prod rejected"
        )))
        request = self._server(self.service)
        for path in ("/health", "/attention", "/readiness", "/deployment-status", "/jobs/mon-secret",
                     "/jobs/mon-secret/history", "/jobs?status=FAILED", f"/projects/{self.project_id}/deployment-authorization"):
            status, payload = request("GET", path)
            text = json.dumps(payload)
            self.assertEqual(status, 200, path)
            self.assertNotIn(FAINT_TOKEN, text)
            self.assertNotIn("admin:", text)
            self.assertNotIn("hunter2", text)
            self.assertNotIn("Bearer " + "test", text)
        # Where a failure detail is surfaced it is redacted, not dropped: the message stays readable.
        for path in ("/jobs/mon-secret", "/jobs/mon-secret/history"):
            _, payload = request("GET", path)
            text = json.dumps(payload)
            self.assertIn("[REDACTED]", text)
            self.assertIn("connection string", text)
            # The worker's own raw output copy is redacted too, not just the summary field.
            self.assertNotIn(FAINT_TOKEN, text)
        status, payload = request("GET", "/jobs/mon-secret")
        self.assertEqual(payload["error_code"], "WORKER_FAULT")
        # A failure inside the attempt budget is the system's to retry, so it is reported as workflow
        # state rather than as operator attention - and it is still visible in both job views.
        _, health_payload = request("GET", "/health")
        self.assertEqual(health_payload["health"]["job_system"]["by_status"]["FAILED"], 1)
        self.assertEqual([i for i in health_payload["health"]["operator_attention"]["items"] if i["kind"] == "job"], [])
        self.assertEqual([i["code"] for i in health_payload["health"]["workflow_execution"]["recent_failures"]], ["WORKER_FAULT"])
        # Attempts remain inside the budget, so the pre-existing classification stays "recoverable":
        # monitoring must not relabel retryable failures as permanent.
        self.assertEqual(payload["execution_category"], "RECOVERABLE")
        self.assertTrue(payload["recoverable"])
        self.assertEqual(payload["requires_operator_action"], False)

    # -- 4. retry eligibility and retry safety ---------------------------------------------
    def test_retry_eligibility_follows_the_recorded_attempt_budget(self) -> None:
        service = JobService(self.store, max_attempts=2)
        job = service.create("SCOUT", job_id="mon-budget")
        self.assertEqual(service.retry_eligibility(job["job_id"])["reason"] and
                         service.retry_eligibility(job["job_id"])["eligible"], False)
        failing = _FakeWorker(result=WorkerResult("FAILED", error="transient", retryable=False, error_code="WORKER_FAILED"))
        service.run(job["job_id"], failing)
        service.run(job["job_id"], failing)
        exhausted = service.get(job["job_id"])
        self.assertEqual(exhausted["status"], "FAILED")
        eligibility = service.retry_eligibility(job["job_id"])
        self.assertTrue(eligibility["eligible"])
        self.assertTrue(eligibility["requires_reset_attempts"])
        self.assertEqual(eligibility["attempts_remaining"], 0)
        self.assertEqual(eligibility["error_code"], "WORKER_FAILED")  # the recorded cause, not a generic label
        self.assertIn("reset_attempts", eligibility["next_action"])
        # Calling run() again after exhaustion is the RETRY_LIMIT_REACHED path: still no attempt made.
        stalled = service.run(job["job_id"], failing)
        self.assertEqual((stalled["status"], stalled["error_code"]), ("FAILED", "RETRY_LIMIT_REACHED"))
        self.assertIn("reset_attempts", stalled["next_action"])
        self.assertEqual(len(service.executions_for(job["job_id"])), 2)
        with self.assertRaises(ValueError) as refused:
            service.retry(job["job_id"])
        self.assertIn("reset_attempts", str(refused.exception))
        self.assertEqual(service.get(job["job_id"])["status"], "FAILED")  # refusal changed nothing

        resumed = service.retry(job["job_id"], reset_attempts=True, worker=_FakeWorker())
        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(resumed["retry_count"], 1)
        self.assertEqual(resumed["last_error_code"], "RETRY_LIMIT_REACHED")
        self.assertIsNone(resumed["error"])
        self.assertIsNone(resumed["error_code"])
        self.assertEqual(service.executions_for(job["job_id"])[-1]["status"], "COMPLETED")
        self.assertEqual(len(service.executions_for(job["job_id"])), 3)  # two failures + the successful retry
        completed_eligibility = service.retry_eligibility(job["job_id"])
        self.assertFalse(completed_eligibility["eligible"])
        self.assertIn("already completed", completed_eligibility["reason"])

    def test_worker_registry_path_uses_the_same_sanitized_failure_vocabulary(self) -> None:
        from orchestration.workers import WorkerRegistry

        registry = WorkerRegistry(self.store)
        registry.register("worker-fixture", name="fixture worker", worker_type="LOCAL_AGENT", capabilities={"SCOUT"})
        job = self.service.create("SCOUT", job_id="mon-registry")
        claimed = registry.claim(job["job_id"], "worker-fixture")
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            reported = registry.report(job["job_id"], "worker-fixture", claimed["lease"]["lease_id"], WorkerResult(
                "FAILED", error=f"provider refused: Bearer {FAINT_TOKEN} and password=hunter2 for lead-0001",
                retryable=False, error_type="DETERMINISTIC",
            ))
        self.assertEqual(reported["status"], "FAILED")
        self.assertEqual(reported["error_code"], "WORKER_FAILED")
        self.assertNotIn(FAINT_TOKEN, json.dumps(reported))
        self.assertNotIn("hunter2", json.dumps(reported))
        self.assertIn("[REDACTED]", reported["error"])
        self.assertIn("provider refused", reported["error"])
        self.assertTrue(reported["next_action"])
        self.assertEqual(reported["failure"]["code"], "WORKER_FAILED")
        execution = self.service.executions_for(job["job_id"])[-1]
        self.assertEqual(execution["error_type"], "DETERMINISTIC")
        self.assertEqual(execution["error_code"], "WORKER_FAILED")
        self.assertNotIn(FAINT_TOKEN, json.dumps(execution))
        self.assertIsInstance(execution["duration_seconds"], int)
        self.assertNotIn(FAINT_TOKEN, json.dumps(self.store.state["workers"]["worker-fixture"]))
        emitted = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip().startswith("{")]
        self.assertEqual(emitted[-1]["code"], "WORKER_FAILED")
        self.assertEqual(emitted[-1]["operation"], "worker.scout")
        # The attention view sees the same thing the registry wrote.
        codes = [item["code"] for item in operator_attention(self.store, repository_root=self.root,
                                                            include_deployment=False)["items"] if item["kind"] == "job"]
        self.assertNotIn("WORKER_FAILED", codes)  # still inside the attempt budget: retryable, not operator work
        eligibility = self.service.retry_eligibility(job["job_id"])
        self.assertTrue(eligibility["eligible"])
        self.assertEqual(eligibility["error_code"], "WORKER_FAILED")

    def test_expired_worker_lease_is_recovered_as_a_coded_attention_item(self) -> None:
        from orchestration.workers import WorkerRegistry

        registry = WorkerRegistry(self.store, lease_seconds=30)
        registry.register("worker-lease", name="lease fixture", worker_type="LOCAL_AGENT", capabilities={"SCOUT"})
        job = self.service.create("SCOUT", job_id="mon-lease")
        registry.claim(job["job_id"], "worker-lease")
        self.store.state["jobs"]["mon-lease"]["lease"]["expires_at"] = "2020-01-01T00:00:00Z"
        self.store._save()
        recovered = registry.recover_expired()
        self.assertEqual([item["job_id"] for item in recovered], ["mon-lease"])
        self.assertEqual(recovered[0]["status"], "QUEUED")
        self.assertEqual(recovered[0]["error_code"], "LEASE_EXPIRED")
        self.assertIn("recover-stale", recovered[0]["next_action"])
        execution = self.service.executions_for("mon-lease")[-1]
        self.assertEqual((execution["status"], execution["error_code"]), ("QUEUED", "LEASE_EXPIRED"))
        self.assertIsNotNone(execution["completed_at"])

    def test_cancelled_and_running_jobs_are_never_silently_retried(self) -> None:
        cancelled = self.service.create("SCOUT", job_id="mon-cancel")
        self.service.cancel(cancelled["job_id"])
        self.assertFalse(self.service.retry_eligibility("mon-cancel")["eligible"])
        with self.assertRaises(ValueError):
            self.service.retry("mon-cancel")
        running = self.service.create("SCOUT", job_id="mon-running")
        self.store.state["jobs"]["mon-running"].update({"status": "RUNNING"})
        self.store._save()
        with self.assertRaises(ValueError):
            self.service.retry("mon-running")

    def test_retry_surfaces_cannot_bypass_the_approval_gate(self) -> None:
        provider_calls = []

        def forbidden_provider(*args, **kwargs):  # pragma: no cover - must never be reached
            provider_calls.append((args, kwargs))
            raise AssertionError("a monitoring or retry surface must not reach the provider")

        job = self.service.create("DEPLOY", job_id="mon-gate", project_id=self.project_id,
                                 build_id=self.build_id, dry_run=False)
        worker = _FakeWorker()
        self.service.run(job["job_id"], worker)
        self.assertEqual(worker.calls, 0)  # the gate stops the job before any worker or provider
        with mock.patch("deployment.deployer.VercelDeploymentProvider.deploy", side_effect=forbidden_provider):
            request = self._server(self.service, worker=_FakeWorker())
            status, payload = request("POST", "/jobs/mon-gate/retry", {"run": True})
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["error_code"], "APPROVAL_MISSING")
            self.assertIn("review project-0001", payload["next_action"])
            self.assertIn("no retry can substitute", payload["next_action"])
            self.assertIsNone(payload.get("deployment_id"))
            self.assertEqual(worker.calls, 0)
            self.assertEqual(provider_calls, [])
            self.assertEqual(self.store.state["deployments"], {})
            self.assertFalse((self.build_dir(self.build_id) / ATTEMPTS_FILENAME).exists())
            # Approval is still not granted anywhere by having looked.
            gate = self.store.authorize_preview_deployment(self.project_id, build_id=self.build_id)
            self.assertFalse(gate["authorized"])
            self.assertEqual(gate["code"], "APPROVAL_MISSING")
            status, again = request("POST", "/jobs/mon-gate/retry", {"run": True})
            self.assertEqual(status, 200)  # idempotently blocked again, never authorized by looking
            self.assertEqual((again["status"], again["error_code"]), ("BLOCKED", "APPROVAL_MISSING"))
            self.assertEqual(self.store.state["projects"][self.project_id]["review_status"], "PENDING")

    def test_api_reports_actionable_refusal_for_an_unretryable_job(self) -> None:
        cancelled = self.service.create("SCOUT", job_id="mon-cancelled")
        self.service.cancel(cancelled["job_id"])
        request = self._server(self.service)
        status, payload = request("POST", "/jobs/mon-cancelled/retry", {"run": False})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "RETRY_NOT_ELIGIBLE")
        self.assertEqual(payload["job_id"], "mon-cancelled")
        self.assertEqual(payload["retry_eligibility"]["status"], "CANCELLED")
        self.assertIn("Cancelled", payload["retry_eligibility"]["reason"])
        self.assertTrue(payload["next_action"])
        # A completed job stays an explicit no-op, visible through the history endpoint.
        completed = self.service.create("SCOUT", job_id="mon-done")
        self.service.run(completed["job_id"], _FakeWorker())
        status, done = request("POST", "/jobs/mon-done/retry", {"run": False})
        self.assertEqual((status, done["status"]), (200, "COMPLETED"))
        status, history = request("GET", "/jobs/mon-done/history")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["executions"]), 1)
        self.assertFalse(history["retry_eligibility"]["eligible"])
        self.assertIn("already completed", history["retry_eligibility"]["reason"])

    # -- 5. deployment uncertainty and approval preservation --------------------------------
    def test_unresolved_publish_is_reported_uncertain_not_failed_or_verified(self) -> None:
        build_dir = self.build_dir(self.build_id)
        append_attempt(build_dir, {
            "attempt": 1, "deployment_status": "DEPLOYING", "deployment_id": "deployment-0001",
            "build_id": self.build_id, "deployment_url": None, "created_at": "2026-10-15T00:00:00Z",
            "error": None,
        })
        outcome = publish_outcome(build_dir)
        self.assertEqual((outcome["state"], outcome["code"]), ("UNCERTAIN", "PUBLISH_STATE_UNCERTAIN"))
        self.assertEqual(outcome["attempt_count"], 1)
        self.assertFalse(outcome["provider_contacted"])
        self.assertIn("--resolve-uncertain-publish", outcome["next_action"])

        report = deployment_report(self.store, repository_root=self.root)
        self.assertEqual(report["outcomes"].get("UNCERTAIN"), 1)
        self.assertEqual(report["status"], "degraded")
        self.assertFalse(report["live_url_checked"])
        entry = report["attention"][0]
        self.assertEqual(entry["project_id"], self.project_id)
        self.assertEqual(entry["deployment_url_host"], None)

        attention = operator_attention(self.store, repository_root=self.root)
        self.assertEqual([item["kind"] for item in attention["items"] if item["kind"] == "deployment"], ["deployment"])
        alerts = critical_alerts(self.store, repository_root=self.root)
        self.assertEqual([a["code"] for a in alerts if a["kind"] == "deployment"], ["PUBLISH_STATE_UNCERTAIN"])

        # A read-only report never resolves the uncertainty or rewrites the ledger.
        ledger_path = build_dir / ATTEMPTS_FILENAME
        before = ledger_path.read_text(encoding="utf-8")
        system_health(self.store, repository_root=self.root)
        self.assertEqual(ledger_path.read_text(encoding="utf-8"), before)

    def test_verified_record_and_never_attempted_stay_distinct(self) -> None:
        self.store.record_deployment(self.project_id, {
            "deployment_id": "deployment-0001", "build_id": self.build_id,
            "validation_id": "validation-0001", "validation_status": "PASS",
            "deployment_status": "DEPLOYED", "deployment_url": "https://fictional-preview.example.invalid",
            "provider": "vercel", "environment": "preview", "dry_run": False,
        })
        other_lead = self._lead("Fictional Second Bistro")
        other_project = self.store.create_project(other_lead["lead_id"])
        second_build = self._build_and_validate("build-0002", "validation-0002", project_id=other_project["project_id"],
                                                lead_id=other_lead["lead_id"])
        report = deployment_report(self.store, repository_root=self.root)
        by_project = {item["project_id"]: item for item in report["attention"]}
        self.assertEqual(report["outcomes"].get("VERIFIED_RECORDED"), 1)
        self.assertEqual(report["outcomes"].get("NOT_ATTEMPTED"), 1)
        self.assertNotIn(self.project_id, by_project)  # verified: no attention
        health = system_health(self.store, repository_root=self.root)
        self.assertEqual(health["deployment"]["outcomes"]["VERIFIED_RECORDED"], 1)
        self.assertEqual(health["deployment"]["configuration"]["project_linked"], False)
        self.assertEqual(health["deployment"]["reason"], "No approved Vercel project link.")

    # -- 6. notification integration --------------------------------------------------------
    def test_critical_alerts_use_the_same_state_and_dedupe_within_a_process(self) -> None:
        job = self.service.create("SCOUT", job_id="mon-alert")
        self.service.run(job["job_id"], _FakeWorker(raises=RuntimeError("permanent fault")))
        self.store.state["jobs"]["mon-alert"].update({"attempts": 5, "max_attempts": 3})
        self.store._save()

        alerts = critical_alerts(self.store, repository_root=self.root)
        self.assertEqual([a["code"] for a in alerts], ["WORKER_FAULT"])
        self.assertEqual(alerts[0]["job_id"], "mon-alert")
        self.assertEqual(alerts[0]["severity"], "CRITICAL")
        self.assertTrue(alerts[0]["next_action"])
        self.assertEqual(len(alerts[0]["alert_id"]), 16)

        stream = io.StringIO()
        first = check_alerts(self.store, repository_root=self.root, sinks=[LogAlertSink(stream=stream)], notify=True)
        self.assertEqual((first["critical_count"], first["notified"]), (1, 1))
        emitted = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(emitted[0]["severity"], "CRITICAL")
        self.assertEqual(emitted[0]["operation"], "alert.job")
        self.assertEqual(emitted[0]["code"], "WORKER_FAULT")

        seen = {first["delivery"][0]["alert_id"]}
        second = check_alerts(self.store, repository_root=self.root, sinks=[LogAlertSink(stream=stream)],
                             notify=True, seen=seen)
        self.assertEqual(second["notified"], 0)
        self.assertEqual(second["suppressed_duplicates"], 1)
        self.assertEqual(len([line for line in stream.getvalue().splitlines() if line.strip()]), 1)

        without_notification = check_alerts(self.store, repository_root=self.root)
        self.assertEqual(without_notification["notified"], 0)
        self.assertEqual(without_notification["notification_enabled"], False)
        self.assertEqual(len(without_notification["alerts"]), 1)  # evaluation never depends on delivery

    def test_webhook_sink_requires_an_injected_transport_and_never_messages_anyone_elsewhere(self) -> None:
        alert = {"alert_id": "abc", "severity": "CRITICAL", "kind": "deployment",
                 "code": "PUBLISH_STATE_UNCERTAIN", "message": "attempt left open",
                 "project_id": self.project_id, "next_action": "inspect then resolve deliberately"}
        unconfigured = WebhookAlertSink("https://alerts.example.invalid/hook").publish(alert)
        self.assertEqual((unconfigured["delivered"], unconfigured["code"]),
                         (False, "SKIPPED_TRANSPORT_NOT_CONFIGURED"))

        sent: list[tuple[str, dict, float]] = []

        def transport(url, payload, timeout):
            sent.append((url, payload, timeout))
            return {"status": 202}

        delivered = WebhookAlertSink("https://alerts.example.invalid/hook", transport=transport).publish(alert)
        self.assertEqual((delivered["delivered"], delivered["provider_status"]), (True, 202))
        self.assertEqual(sent[0][0], "https://alerts.example.invalid/hook")
        self.assertEqual(sent[0][1]["alert"]["code"], "PUBLISH_STATE_UNCERTAIN")
        self.assertNotIn(FAINT_TOKEN, json.dumps(sent[0][1]))

        def broken(url, payload, timeout):
            raise OSError("channel unavailable")

        failed = WebhookAlertSink("https://alerts.example.invalid/hook", transport=broken).publish(alert)
        self.assertEqual((failed["delivered"], failed["code"]), (False, "DELIVERY_FAILED"))
        self.assertIn("channel unavailable", failed["error"])

        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network from alerts")) as guard:
            job = self.service.create("SCOUT", job_id="mon-no-network")
            self.service.run(job["job_id"], _FakeWorker(raises=RuntimeError("fault")))
            self.store.state["jobs"]["mon-no-network"].update({"attempts": 9, "max_attempts": 3})
            self.store._save()
            report = check_alerts(self.store, repository_root=self.root, sinks=[LogAlertSink(stream=io.StringIO())],
                                 notify=True)
            self.assertGreaterEqual(report["critical_count"], 1)
            guard.assert_not_called()

    # -- operator CLI -----------------------------------------------------------------------
    def test_cli_surfaces_report_state_and_leave_the_workspace_untouched(self) -> None:
        job = self.service.create("SCOUT", job_id="mon-cli")
        self.service.run(job["job_id"], _FakeWorker(raises=RuntimeError("boom in fixture worker")))
        # Exhaust the attempt budget so the failure is critical rather than merely retryable.
        self.store.state["jobs"]["mon-cli"].update({"attempts": 3})
        self.store._save()
        state_before = self.state_path.read_bytes()
        build_dir = self.build_dir(self.build_id)
        append_attempt(build_dir, {"attempt": 1, "deployment_status": "PUBLISH_UNCERTAIN",
                                  "build_id": self.build_id, "created_at": "2026-10-15T00:00:00Z"})

        code, health = self._cli("health", "--state-path", str(self.state_path))
        self.assertEqual(code, 0)
        self.assertEqual(health["application"]["status"], "healthy")
        self.assertEqual(health["source_of_truth"]["verification"], "verified")
        self.assertEqual(health["job_system"]["attention"], ["FAILED"])
        self.assertIn("FAILED", health["job_system"]["by_status"])

        code, attention = self._cli("attention", "--state-path", str(self.state_path), "--brief")
        self.assertEqual(code, 1)
        self.assertTrue(attention["requires_attention"])
        self.assertIn("mon-cli", [item["job_id"] for item in attention["items"]])
        self.assertTrue(all("next_action" in item for item in attention["items"]))

        code, listed = self._cli("list-jobs", "--state-path", str(self.state_path), "--status", "FAILED", "--brief")
        self.assertEqual(code, 0)
        self.assertEqual([job["job_id"] for job in listed["jobs"]], ["mon-cli"])
        self.assertEqual(listed["jobs"][0]["retry_exhausted"], True)
        self.assertEqual(listed["jobs"][0]["error_code"], "WORKER_FAULT")
        code, attention_only = self._cli("list-jobs", "--state-path", str(self.state_path), "--attention-only")
        self.assertEqual(code, 1)
        self.assertEqual(attention_only["count"], 1)

        code, inspected = self._cli("inspect-job", "--state-path", str(self.state_path), "--job-id", "mon-cli", "--history")
        self.assertEqual(code, 1)
        self.assertEqual(inspected["job"]["status"], "FAILED")
        self.assertEqual(inspected["executions"][0]["error_message"], inspected["job"]["error"])
        self.assertTrue(inspected["retry_eligibility"]["eligible"])

        code, deployment = self._cli("deployment-status", "--state-path", str(self.state_path))
        self.assertEqual(code, 1)
        self.assertEqual(deployment["attention"][0]["state"], "UNCERTAIN")
        self.assertEqual(deployment["configuration"]["cli_available"], False)

        code, alerts_report = self._cli("check-alerts", "--state-path", str(self.state_path))
        self.assertEqual(code, 1)
        self.assertEqual({a["code"] for a in alerts_report["alerts"]}, {"PUBLISH_STATE_UNCERTAIN", "WORKER_FAULT"})
        self.assertEqual(alerts_report["evaluated_without_sink"], 2)
        self.assertEqual(alerts_report["notified"], 0)

        self.assertEqual(self.state_path.read_bytes(), state_before)

    def test_cli_recovers_stale_jobs_and_reports_the_verdict_by_exit_code(self) -> None:
        job = self.service.create("SCOUT", job_id="mon-recover")
        self.service.run(job["job_id"], _FakeWorker())
        self.store.state["jobs"]["mon-recover"].update({"status": "RUNNING", "heartbeat_at": "2020-01-01T00:00:00Z"})
        self.store._save()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = orchestration_main(["recover-stale", "--state-path", str(self.state_path),
                                       "--older-than-seconds", "900", "--repository-root", str(self.root)])
        recovered = json.loads(buffer.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual([job["job_id"] for job in recovered], ["mon-recover"])
        self.assertEqual(recovered[0]["status"], "NEEDS_REVIEW")
        self.assertEqual(recovered[0]["error_code"], "HEARTBEAT_EXPIRED")
        self.assertIn("recover-stale", recovered[0]["next_action"])
        # Recovery is not a reset, and it is durable: a freshly loaded store shows the closed attempt.
        reloaded = TrackingStore(self.state_path)
        executions = reloaded.state["executions"]
        closed = next(record for record in executions.values() if record.get("job_id") == "mon-recover")
        self.assertEqual(closed["status"], "NEEDS_REVIEW")
        self.assertEqual(closed["error_code"], "HEARTBEAT_EXPIRED")
        self.assertIsNotNone(closed["completed_at"])
        self.assertIsNotNone(closed["duration_seconds"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
