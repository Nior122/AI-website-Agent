from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from orchestration.api import make_handler
from orchestration.jobs import JobService, WorkerResult
from tracking.store import TrackingStore


class ResultWorker:
    name = "fictional-worker"
    def __init__(self, *results): self.results = list(results)
    def execute(self, job, context): return self.results.pop(0)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TrackingStore(Path(self.temp.name) / "state.json")
        self.jobs = JobService(self.store, max_attempts=3)
        self.lead = self.store.create_lead({"business_name": "Fictional Studio", "qualification_status": "QUALIFIED"})
        self.project = self.store.create_project(self.lead["lead_id"])

    def tearDown(self): self.temp.cleanup()

    def worker(self, status="COMPLETED", **kwargs): return ResultWorker(WorkerResult(status, **kwargs))

    def test_create_scout_job_and_idempotency(self):
        first = self.jobs.create("SCOUT", job_id="scout-001", input={"city": "Fictional"})
        second = self.jobs.create("SCOUT", job_id="scout-001", input={"city": "Other"})
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.store.state["jobs"]), 1)
        with self.assertRaises(ValueError): self.jobs.create("BUILD", job_id="scout-001")

    def test_prerequisite_gates_block_unsafe_work(self):
        bad = self.store.create_lead({"business_name": "Unqualified", "qualification_status": "REVIEW"})
        build = self.jobs.create("BUILD", job_id="build-bad", lead_id=bad["lead_id"])
        validate = self.jobs.create("VALIDATE", job_id="validate-none", project_id=self.project["project_id"])
        deploy = self.jobs.create("DEPLOY", job_id="deploy-none", project_id=self.project["project_id"])
        outreach = self.jobs.create("PREPARE_OUTREACH", job_id="outreach-none", project_id=self.project["project_id"])
        for job in (build, validate, deploy, outreach): self.assertEqual(self.jobs.run(job["job_id"], self.worker())["status"], "BLOCKED")

    def test_successful_chain_is_explicit_and_preserves_references(self):
        scout = self.jobs.create("SCOUT", job_id="scout-chain")
        self.assertEqual(self.jobs.run(scout["job_id"], self.worker(output={"lead_ids": [self.lead["lead_id"]]}))["next_job_types"], ["RESEARCH"])
        research = self.jobs.create_next("scout-chain", next_job_id="research-chain", lead_id=self.lead["lead_id"], input={"fixture": True})
        self.assertEqual(research["lead_id"], self.lead["lead_id"])
        self.assertEqual(self.jobs.run(research["job_id"], self.worker())["status"], "COMPLETED")
        qualify = self.jobs.create("QUALIFY", job_id="qualify-chain", lead_id=self.lead["lead_id"])
        self.assertEqual(self.jobs.run(qualify["job_id"], self.worker())["status"], "COMPLETED")
        build = self.jobs.create("BUILD", job_id="build-chain", lead_id=self.lead["lead_id"], project_id=self.project["project_id"])
        self.assertEqual(self.jobs.run(build["job_id"], self.worker())["status"], "COMPLETED")
        self.store.record_build(self.project["project_id"], {"build_id": "build-chain", "lead_id": self.lead["lead_id"]})
        validate = self.jobs.create("VALIDATE", job_id="validate-chain", project_id=self.project["project_id"], build_id="build-chain")
        self.assertEqual(self.jobs.run(validate["job_id"], self.worker(output={"validation_status": "PASS"}))["status"], "COMPLETED")
        self.store.record_validation(self.project["project_id"], {"validation_id": "validation-chain", "build_id": "build-chain", "status": "PASS"})
        deploy = self.jobs.create("DEPLOY", job_id="deploy-chain", project_id=self.project["project_id"], build_id="build-chain", dry_run=True)
        self.assertEqual(self.jobs.run(deploy["job_id"], self.worker(output={"deployment_status": "PENDING"}))["status"], "COMPLETED")
        self.store.record_deployment(self.project["project_id"], {"deployment_id": "deployment-chain", "build_id": "build-chain", "validation_status": "PASS", "deployment_status": "DEPLOYED", "deployment_url": "https://fictional.vercel.app"})
        self.store.record_review(self.project["project_id"], "APPROVED")
        outreach = self.jobs.create("PREPARE_OUTREACH", job_id="outreach-chain", project_id=self.project["project_id"])
        self.assertEqual(self.jobs.run(outreach["job_id"], self.worker(output={"draft": "local only"}))["status"], "COMPLETED")

    def test_retry_permanent_failure_and_stale_recovery(self):
        retry = self.jobs.create("SCOUT", job_id="retry")
        self.assertEqual(self.jobs.run("retry", ResultWorker(WorkerResult("FAILED", error="temporary", retryable=True)))["status"], "QUEUED")
        self.assertEqual(self.jobs.run("retry", self.worker())["status"], "COMPLETED")
        permanent = self.jobs.create("SCOUT", job_id="permanent")
        self.assertEqual(self.jobs.run("permanent", self.worker("FAILED", error="bad input"))["status"], "FAILED")
        stuck = self.jobs.create("SCOUT", job_id="stuck")
        stuck.update({"status": "RUNNING", "heartbeat_at": "2000-01-01T00:00:00Z"})
        self.assertEqual(self.jobs.recover_stale(older_than_seconds=1)[0]["status"], "NEEDS_REVIEW")

    def test_dry_run_and_api_authentication(self):
        dry = self.jobs.create("SCOUT", job_id="dry", dry_run=True)
        self.assertTrue(dry["dry_run"])
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.jobs, "test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f"http://127.0.0.1:{server.server_port}/jobs"
        try:
            with self.assertRaises(HTTPError) as denied: urlopen(url)
            self.assertEqual(denied.exception.code, 401)
            request = Request(url, data=json.dumps({"job_type": "SCOUT", "job_id": "api-job"}).encode(), headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"}, method="POST")
            self.assertEqual(json.loads(urlopen(request).read())["job_id"], "api-job")
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__": unittest.main()
