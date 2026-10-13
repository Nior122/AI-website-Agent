"""End-to-end integration tests for n8n workflow JSON definitions and the orchestration HTTP API.

Starts a real loopback ThreadingHTTPServer running `orchestration.api.make_handler`
and executes the exported n8n workflow JSON files (`n8n/workflows/*.json`) over
real HTTP requests against the existing Python application services using only
fictional test businesses (`TEST_FIXTURE`) in isolated temporary directories.
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from orchestration.api import make_handler
from orchestration.jobs import JobService
from orchestration.n8n_runner import N8nWorkflowRunner, validate_n8n_workflow_json
from orchestration.workers import LocalPipelineWorker
from tracking.store import TrackingStore


class N8nIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"
        self.output_root = self.root / "output"
        self.export_root = self.root / "preview-export"
        self.store = TrackingStore(self.state_path)
        self.jobs = JobService(self.store, max_attempts=3)
        self.worker = LocalPipelineWorker(
            repository_root=Path("."),
            output_root=self.output_root,
        )
        self.token = "test-n8n-bearer-token"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.jobs,
                self.token,
                worker=self.worker,
                output_root=self.output_root,
                export_root=self.export_root,
                repository_root=Path("."),
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_url = f"http://127.0.0.1:{self.server.server_port}"
        self.env = {
            "AI_WEBSITE_AGENT_API_URL": self.api_url,
            "AI_WEBSITE_AGENT_API_TOKEN": self.token,
        }
        self.core_workflow_path = Path("n8n/workflows/03-core-lead-to-review-package-pipeline.json")
        self.recovery_workflow_path = Path("n8n/workflows/04-recover-and-retry-jobs.json")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _write_fictional_fixtures(
        self,
        *,
        business_name: str = "Fictional Lagoon Bistro (TEST / FICTIONAL)",
        niche: str = "Restaurants",
        phone: str = "+2348000001101",
        website: str | None = None,
        website_status: str = "none_known",
        modern_website: bool = False,
        duplicate_in_source: bool = False,
        include_evidence: bool = True,
    ) -> tuple[Path, Path]:
        cand = {
            "candidate_id": "cand-fictional-001",
            "data_classification": "TEST_FIXTURE",
            "business_name": business_name,
            "niche": niche,
            "city": "Lagos",
            "country": "Nigeria",
            "address": "10 Fictional Marina Road, Lagos",
            "phone": phone,
            "whatsapp": phone,
            "website": website,
            "website_status": "professional" if modern_website else ("outdated" if website else website_status),
            "instagram_url": "https://instagram.example.invalid/fictional_lagoon_bistro",
            "description": f"{business_name} serves seasonal coastal dishes in Lagos.",
            "services": ["Coastal Grill", "Group Dining"],
            "opening_hours": "Tue-Sun 12:00-22:00",
            "source_url": "https://directory.example.invalid/fictional-lagoon-bistro",
        }
        candidates = [cand, dict(cand, candidate_id="cand-fictional-002")] if duplicate_in_source else [cand]
        source_file = self.root / f"candidates-{abs(hash((business_name, niche, duplicate_in_source)))}.json"
        source_file.write_text(json.dumps(candidates, indent=2), encoding="utf-8")

        evidence_records = []
        if include_evidence:
            ev_rec: dict = {
                "candidate_name": business_name,
                "business_name": business_name,
                "city": "Lagos",
                "country": "Nigeria",
                "address": "10 Fictional Marina Road, Lagos",
                "phone": phone,
                "instagram_url": "https://instagram.example.invalid/fictional_lagoon_bistro",
                "description": f"{business_name} serves seasonal coastal dishes in Lagos.",
                "services": ["Coastal Grill", "Group Dining"],
                "activity_signal": "fictional public fixture",
                "source_url": "https://research.example.invalid/fictional-lagoon-bistro",
            }
            if modern_website:
                ev_rec["website_observation"] = {
                    "source_url": "https://inspection.example.invalid/fictional-modern",
                    "load": "loaded",
                    "correct_business": True,
                    "mobile_friendly": True,
                    "clear_navigation": True,
                    "clear_services": True,
                    "easy_contact": True,
                    "clear_cta": True,
                }
            elif website:
                ev_rec["website_observation"] = {
                    "source_url": "https://inspection.example.invalid/fictional-outdated",
                    "load": "loaded",
                    "correct_business": True,
                    "mobile_friendly": False,
                    "poor_mobile": True,
                    "missing_contact": True,
                    "outdated_information": True,
                }
            evidence_records.append(ev_rec)
        evidence_file = self.root / f"evidence-{abs(hash((business_name, modern_website, include_evidence)))}.json"
        evidence_file.write_text(json.dumps(evidence_records, indent=2), encoding="utf-8")
        return source_file, evidence_file

    def _api_request(self, method: str, path: str, body: dict | None = None, *, token: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(f"{self.api_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_00_all_exported_n8n_workflows_and_api_auth_are_valid(self) -> None:
        for wf_file in sorted(Path("n8n/workflows").glob("*.json")):
            wf = validate_n8n_workflow_json(wf_file)
            self.assertFalse(wf["active"])
        code_unauth, body_unauth = self._api_request("GET", "/health", token="wrong-token")
        self.assertEqual(code_unauth, 401)
        self.assertEqual(body_unauth["error"], "Unauthorized")
        code_ok, body_ok = self._api_request("GET", "/health", token=self.token)
        self.assertEqual(code_ok, 200)
        self.assertEqual(body_ok["api_status"], "OK")
        self.assertTrue(body_ok["worker_registered"])

        # Verify invalid inputs return useful 400 errors and disallowed deployment/outreach stages are rejected
        code_missing, err_missing = self._api_request("POST", "/jobs", {"job_type": "SCOUT"}, token=self.token)
        self.assertEqual(code_missing, 400)
        self.assertIn("requires 'job_type' and 'job_id'", err_missing["error"])

        code_deploy, err_deploy = self._api_request(
            "POST",
            "/jobs",
            {"job_type": "DEPLOY", "job_id": "forbid-deploy-1"},
            token=self.token,
        )
        self.assertEqual(code_deploy, 400)
        self.assertIn("not permitted via the n8n automation API", err_deploy["error"])

        # Verify starter workflows 01-create-job.json and 02-inspect-job.json run unmodified and report PENDING category
        r01 = N8nWorkflowRunner("n8n/workflows/01-create-job.json", env=self.env).execute({}, execution_id="starter-01")
        self.assertEqual(r01["final_output"]["job_id"], "n8n-scout-starter-01")
        self.assertEqual(r01["final_output"]["status"], "PENDING")
        self.assertEqual(r01["final_output"]["execution_category"], "PENDING")
        self.assertFalse(r01["final_output"]["recoverable"])

        self._api_request("POST", "/jobs", {"job_type": "SCOUT", "job_id": "REPLACE_WITH_JOB_ID"}, token=self.token)
        r02 = N8nWorkflowRunner("n8n/workflows/02-inspect-job.json", env=self.env).execute({}, execution_id="starter-02")
        self.assertEqual(r02["final_output"]["job_id"], "REPLACE_WITH_JOB_ID")
        self.assertEqual(r02["final_output"]["execution_category"], "PENDING")

    def test_01_successful_end_to_end_execution_to_review_package_and_human_review_pending(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures()
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        exec_res = runner.execute(
            {
                "run_id": "run-e2e-01",
                "source_file": str(source_file),
                "evidence_file": str(evidence_file),
                "export_dir": str(self.export_root / "run-e2e-01"),
            },
            execution_id="exec-01",
        )
        self.assertEqual(exec_res["terminal_node"], "Outcome - Successful Build Ready For Human Review")
        final = exec_res["final_output"]
        self.assertEqual(final["pipeline_status"], "COMPLETED_AWAITING_HUMAN_REVIEW")
        self.assertEqual(final["stage"], "HUMAN_REVIEW_PENDING")
        self.assertEqual(final["outcome"], "SUCCESSFUL_BUILD_READY_FOR_REVIEW")
        self.assertEqual(final["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(final["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")
        self.assertEqual(final["review_status"], "PENDING")
        self.assertEqual(final["review_package_status"], "VALID")
        self.assertTrue(final["human_review_pending"])
        self.assertTrue(final["hash_matches"])

        # Confirm no deployment or outreach occurred
        snap = self.store.snapshot(final["project_id"])
        self.assertIsNone(snap["latestDeployment"])
        self.assertEqual(len(self.store.state["outreach"]), 0)

    def test_02_duplicate_discovery_results_are_deduplicated_and_do_not_create_duplicate_leads(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures(duplicate_in_source=True)
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)

        # First run discovers the duplicate records in source_file and creates exactly 1 lead and 1 project
        res1 = runner.execute(
            {"run_id": "run-dup-01", "source_file": str(source_file), "evidence_file": str(evidence_file)},
            execution_id="exec-dup-1",
        )
        self.assertEqual(res1["final_output"]["outcome"], "SUCCESSFUL_BUILD_READY_FOR_REVIEW")
        self.assertEqual(len(self.store.state["leads"]), 1)
        self.assertEqual(len(self.store.state["projects"]), 1)

        # Second discovery run on the same business detects that the duplicate is already READY_FOR_REVIEW and branches to Outcome - Duplicate Business
        res2 = runner.execute(
            {"run_id": "run-dup-02", "source_file": str(source_file), "evidence_file": str(evidence_file)},
            execution_id="exec-dup-2",
        )
        self.assertEqual(res2["terminal_node"], "Outcome - Duplicate Business")
        self.assertEqual(res2["final_output"]["pipeline_status"], "SKIPPED_DUPLICATE")
        self.assertEqual(res2["final_output"]["outcome"], "DUPLICATE_ALREADY_READY_FOR_REVIEW")
        self.assertEqual(res2["final_output"]["duplicate_count"], 1)
        self.assertEqual(len(self.store.state["leads"]), 1)
        self.assertEqual(len(self.store.state["projects"]), 1)
        self.assertEqual(len(self.store.state["builds"]), 1)

    def test_03_unqualified_business_and_insufficient_evidence_halt_before_build(self) -> None:
        # 3A: Modern website with no defects -> UNQUALIFIED_OPPORTUNITY
        mod_source, mod_evidence = self._write_fictional_fixtures(
            business_name="Fictional Modern Dining (TEST / FICTIONAL)",
            phone="+2348000001103",
            website="https://fictional-modern-dining.example.invalid",
            modern_website=True,
        )
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        res_mod = runner.execute(
            {"run_id": "run-unqual-01", "source_file": str(mod_source), "evidence_file": str(mod_evidence)},
            execution_id="exec-unqual-1",
        )
        self.assertEqual(res_mod["terminal_node"], "Outcome - Unqualified Opportunity")
        self.assertEqual(res_mod["final_output"]["pipeline_status"], "HALTED_BEFORE_BUILD")
        self.assertEqual(res_mod["final_output"]["outcome"], "UNQUALIFIED_OPPORTUNITY")
        self.assertEqual(res_mod["final_output"]["qualification_status"], "DISQUALIFIED")
        self.assertEqual(res_mod["final_output"]["project_id"], "")
        self.assertEqual(len(self.store.state["projects"]), 0)
        self.assertEqual(len(self.store.state["builds"]), 0)

        # 3B: Insufficient research evidence (no matching evidence records and single source) -> INSUFFICIENT_RESEARCH_EVIDENCE
        ins_cand = {
            "candidate_id": "cand-ins-001",
            "data_classification": "TEST_FIXTURE",
            "business_name": "Fictional Sparse Cafe (TEST / FICTIONAL)",
            "niche": "Restaurants",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+2348000001104",
            "source_url": "https://single.example.invalid/sparse",
        }
        ins_source = self.root / "candidates-sparse.json"
        ins_source.write_text(json.dumps([ins_cand]), encoding="utf-8")
        ins_evidence = self.root / "evidence-sparse.json"
        ins_evidence.write_text(json.dumps([]), encoding="utf-8")

        res_ins = runner.execute(
            {"run_id": "run-ins-01", "source_file": str(ins_source), "evidence_file": str(ins_evidence)},
            execution_id="exec-ins-1",
        )
        self.assertEqual(res_ins["terminal_node"], "Outcome - Insufficient Research Evidence")
        self.assertEqual(res_ins["final_output"]["pipeline_status"], "HALTED_BEFORE_BUILD")
        self.assertEqual(res_ins["final_output"]["outcome"], "INSUFFICIENT_RESEARCH_EVIDENCE")
        self.assertEqual(len(self.store.state["projects"]), 0)

        # 3C: Research requiring additional work (website listed without inspection observation -> REVIEW -> RESEARCH_REQUIRES_ADDITIONAL_WORK)
        rev_cand = {
            "candidate_id": "cand-rev-001",
            "data_classification": "TEST_FIXTURE",
            "business_name": "Fictional Uninspected Grill (TEST / FICTIONAL)",
            "niche": "Restaurants",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+2348000001109",
            "website": "https://fictional-uninspected.example.invalid",
            "website_status": "unknown",
            "source_url": "https://directory.example.invalid/uninspected-grill",
        }
        rev_ev = {
            "candidate_name": "Fictional Uninspected Grill (TEST / FICTIONAL)",
            "business_name": "Fictional Uninspected Grill (TEST / FICTIONAL)",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+2348000001109",
            "services": ["Seafood"],
            "activity_signal": "fictional fixture activity",
            "source_url": "https://research.example.invalid/uninspected-grill",
        }
        rev_source = self.root / "candidates-rev.json"
        rev_source.write_text(json.dumps([rev_cand]), encoding="utf-8")
        rev_evidence = self.root / "evidence-rev.json"
        rev_evidence.write_text(json.dumps([rev_ev]), encoding="utf-8")

        res_rev = runner.execute(
            {"run_id": "run-rev-01", "source_file": str(rev_source), "evidence_file": str(rev_evidence)},
            execution_id="exec-rev-1",
        )
        self.assertEqual(res_rev["terminal_node"], "Outcome - Research Requires Additional Work")
        self.assertEqual(res_rev["final_output"]["pipeline_status"], "HALTED_BEFORE_BUILD")
        self.assertEqual(res_rev["final_output"]["outcome"], "RESEARCH_REQUIRES_ADDITIONAL_WORK")
        self.assertEqual(res_rev["final_output"]["qualification_status"], "NEEDS_REVIEW")
        self.assertEqual(len(self.store.state["projects"]), 0)

    def test_04_research_failure_routes_to_failure_branch_and_blocks_downstream(self) -> None:
        source_file, _ = self._write_fictional_fixtures(
            business_name="Fictional Research Fail Cafe",
            phone="+2348000001105",
            website="https://fictional-res-fail.example.invalid",
        )
        missing_evidence = self.root / "nonexistent-evidence.json"
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        res = runner.execute(
            {"run_id": "run-resfail-01", "source_file": str(source_file), "evidence_file": str(missing_evidence)},
            execution_id="exec-resfail-1",
        )
        self.assertEqual(res["terminal_node"], "Outcome - Research Failed")
        self.assertEqual(res["final_output"]["pipeline_status"], "FAILED")
        self.assertEqual(res["final_output"]["stage"], "RESEARCH")
        self.assertEqual(res["final_output"]["outcome"], "RESEARCH_FAILED")
        self.assertEqual(len(self.store.state["projects"]), 0)
        self.assertEqual(len(self.store.state["builds"]), 0)

    def test_05_website_build_failure_routes_to_failure_branch_and_records_failed_stage(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures(
            business_name="Fictional Unsupported Niche Co",
            niche="Aerospace Manufacturing",
            phone="+2348000001106",
            website="https://fictional-aerospace.example.invalid",
        )
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        res = runner.execute(
            {
                "run_id": "run-buildfail-01",
                "source_file": str(source_file),
                "evidence_file": str(evidence_file),
                "niche": "Aerospace Manufacturing",
            },
            execution_id="exec-buildfail-1",
        )
        self.assertEqual(res["terminal_node"], "Outcome - Build Failure")
        self.assertEqual(res["final_output"]["pipeline_status"], "FAILED")
        self.assertEqual(res["final_output"]["stage"], "WEBSITE_BUILD")
        self.assertEqual(res["final_output"]["outcome"], "BUILD_FAILURE")
        proj_id = res["final_output"]["project_id"]
        self.assertEqual(self.store.snapshot(proj_id)["project"]["current_stage"], "FAILED")

    def test_06_validation_failure_routes_to_failure_branch_and_blocks_review_package(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures(
            business_name="Fictional Validation Fail Bistro",
            phone="+2348000001107",
            website="https://fictional-val-fail.example.invalid",
        )

        # Inject a broken internal link into the generated HTML artifact right after Run Website Build and before Run Automated Validation
        def _tamper_built_html(build_job_json: dict) -> None:
            out_loc = (build_job_json.get("output") or {}).get("output_location")
            if out_loc:
                p = Path(out_loc)
                p.write_text(p.read_text(encoding="utf-8") + '<a href="#nonexistent-section">Broken link</a>', encoding="utf-8")

        runner = N8nWorkflowRunner(
            self.core_workflow_path,
            env=self.env,
            node_hooks={"Run Website Build": _tamper_built_html},
        )
        res = runner.execute(
            {"run_id": "run-valfail-01", "source_file": str(source_file), "evidence_file": str(evidence_file)},
            execution_id="exec-valfail-1",
        )
        self.assertEqual(res["terminal_node"], "Outcome - Validation Failure")
        self.assertEqual(res["final_output"]["pipeline_status"], "FAILED")
        self.assertEqual(res["final_output"]["stage"], "AUTOMATED_VALIDATION")
        self.assertEqual(res["final_output"]["outcome"], "VALIDATION_FAILURE")
        proj_id = res["final_output"]["project_id"]
        self.assertEqual(self.store.snapshot(proj_id)["project"]["current_stage"], "FAILED")

    def test_07_retry_after_failure_recovers_and_completes_pipeline_without_duplicates(self) -> None:
        source_file, valid_evidence_file = self._write_fictional_fixtures(
            business_name="Fictional Retry Kitchen",
            phone="+2348000001108",
            website="https://fictional-retry-kitchen.example.invalid",
        )
        bad_evidence_file = self.root / "missing-retry-evidence.json"

        # 1. First run fails at RESEARCH due to missing evidence file
        core_runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        fail_res = core_runner.execute(
            {"run_id": "run-retry-01", "source_file": str(source_file), "evidence_file": str(bad_evidence_file)},
            execution_id="exec-retry-1",
        )
        self.assertEqual(fail_res["terminal_node"], "Outcome - Research Failed")
        failed_job_id = fail_res["final_output"]["job_id"]
        self.assertEqual(self.store.state["jobs"][failed_job_id]["status"], "FAILED")
        self.assertEqual(self.store.state["jobs"][failed_job_id]["attempts"], 1)

        # 2. Run the n8n recovery/retry workflow (`04-recover-and-retry-jobs.json`) with the valid evidence file
        rec_runner = N8nWorkflowRunner(self.recovery_workflow_path, env=self.env)
        rec_res = rec_runner.execute(
            {
                "job_id": failed_job_id,
                "retry_input_json": json.dumps({"evidence_file": str(valid_evidence_file)}),
            },
            execution_id="exec-retry-2",
        )
        self.assertEqual(rec_res["terminal_node"], "Outcome - Job Recovered & Completed")
        self.assertEqual(rec_res["final_output"]["recovery_status"], "RECOVERED_AND_COMPLETED")
        self.assertEqual(rec_res["final_output"]["attempts"], 2)

        # 3. Re-trigger the core workflow with the same run_id: completed SCOUT and RESEARCH jobs are reused and the remaining stages complete
        resume_res = core_runner.execute(
            {"run_id": "run-retry-01", "source_file": str(source_file), "evidence_file": str(valid_evidence_file)},
            execution_id="exec-retry-3",
        )
        self.assertEqual(resume_res["terminal_node"], "Outcome - Successful Build Ready For Human Review")
        self.assertEqual(len(self.store.state["leads"]), 1)
        self.assertEqual(len(self.store.state["projects"]), 1)

    def test_08_repeated_trigger_is_idempotent_and_does_not_repeat_completed_work(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures(
            business_name="Fictional Idempotent Cafe",
            phone="+2348000001109",
            website="https://fictional-idempotent.example.invalid",
        )
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        first = runner.execute(
            {"run_id": "run-idem-01", "source_file": str(source_file), "evidence_file": str(evidence_file)},
            execution_id="exec-idem-1",
        )
        second = runner.execute(
            {"run_id": "run-idem-01", "source_file": str(source_file), "evidence_file": str(evidence_file)},
            execution_id="exec-idem-2",
        )
        self.assertEqual(first["final_output"]["project_id"], second["final_output"]["project_id"])
        self.assertEqual(first["final_output"]["build_id"], second["final_output"]["build_id"])
        self.assertEqual(first["final_output"]["validation_id"], second["final_output"]["validation_id"])
        self.assertEqual(len(self.store.state["leads"]), 1)
        self.assertEqual(len(self.store.state["projects"]), 1)
        self.assertEqual(len(self.store.state["builds"]), 1)
        for job in self.store.state["jobs"].values():
            self.assertEqual(job["attempts"], 1)

    def test_09_recovery_after_interruption_moves_stale_running_job_to_needs_review_and_retries_cleanly(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures(
            business_name="Fictional Interrupted Bakery",
            phone="+2348000001110",
            website="https://fictional-interrupted.example.invalid",
        )
        # Simulate a SCOUT job that was interrupted mid-run with an expired heartbeat
        stale_job = self.jobs.create(
            "SCOUT",
            job_id="run-int-01-scout",
            input={"source_file": str(source_file), "country": "Nigeria", "city": "Lagos", "niche": "Restaurants"},
        )
        stale_job.update({
            "status": "RUNNING",
            "attempts": 1,
            "started_at": "2020-01-01T00:00:00Z",
            "heartbeat_at": "2020-01-01T00:00:00Z",
        })
        self.store._save()

        # Run the n8n recovery workflow (`04-recover-and-retry-jobs.json`), which calls /jobs/recover-stale then /jobs/:id/retry
        rec_runner = N8nWorkflowRunner(self.recovery_workflow_path, env=self.env)
        rec_res = rec_runner.execute(
            {"job_id": "run-int-01-scout", "older_than_seconds": 60},
            execution_id="exec-int-1",
        )
        self.assertEqual(rec_res["terminal_node"], "Outcome - Job Recovered & Completed")
        self.assertEqual(rec_res["final_output"]["recovery_status"], "RECOVERED_AND_COMPLETED")
        self.assertEqual(rec_res["final_output"]["attempts"], 2)

        # Resume the core workflow for run-int-01 and confirm it completes cleanly to READY_FOR_REVIEW
        core_runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        fin_res = core_runner.execute(
            {"run_id": "run-int-01", "source_file": str(source_file), "evidence_file": str(evidence_file)},
            execution_id="exec-int-2",
        )
        self.assertEqual(fin_res["terminal_node"], "Outcome - Successful Build Ready For Human Review")
        self.assertEqual(fin_res["final_output"]["review_status"], "PENDING")

    def test_10_final_review_package_generation_verification_and_stale_package_rejection(self) -> None:
        source_file, evidence_file = self._write_fictional_fixtures(
            business_name="Fictional Package Verify Grill",
            phone="+2348000001111",
            website="https://fictional-pkg-verify.example.invalid",
        )
        exp_dir = self.export_root / "pkg-verify-01"
        runner = N8nWorkflowRunner(self.core_workflow_path, env=self.env)
        res = runner.execute(
            {
                "run_id": "run-pkg-01",
                "source_file": str(source_file),
                "evidence_file": str(evidence_file),
                "export_dir": str(exp_dir),
            },
            execution_id="exec-pkg-1",
        )
        out = res["final_output"]
        proj_id = out["project_id"]
        build_1 = out["build_id"]
        self.assertEqual(out["review_package_status"], "VALID")
        self.assertTrue((exp_dir / "index.html").is_file())
        self.assertTrue((exp_dir / "review-package.json").is_file())
        self.assertTrue((exp_dir / "REVIEW_README.md").is_file())

        # Approve build_1 for preview deployment, then trigger a new build_2 and verify old package & approval are invalidated
        self.store.record_review(proj_id, "APPROVED_FOR_PREVIEW", notes="Approve build_1", build_id=build_1)
        self.assertEqual(self.store.snapshot(proj_id)["project"]["approved_build_id"], build_1)

        code_b2, b2_job = self._api_request(
            "POST",
            "/jobs",
            {
                "job_type": "BUILD",
                "job_id": "run-pkg-01-build-2",
                "lead_id": out["lead_id"],
                "project_id": proj_id,
                "run": True,
            },
            token=self.token,
        )
        self.assertEqual(code_b2, 201)
        build_2 = b2_job["output"]["build_id"]
        self.assertNotEqual(build_1, build_2)

        code_v2, v2_job = self._api_request(
            "POST",
            "/jobs",
            {
                "job_type": "VALIDATE",
                "job_id": "run-pkg-01-validate-2",
                "lead_id": out["lead_id"],
                "project_id": proj_id,
                "build_id": build_2,
                "run": True,
            },
            token=self.token,
        )
        self.assertEqual(code_v2, 201)
        self.assertEqual(v2_job["status"], "COMPLETED")

        # Prior approval for build_1 was cleared and old exported package in exp_dir is now rejected as stale
        snap_after = self.store.snapshot(proj_id)
        self.assertEqual(snap_after["project"]["review_status"], "PENDING")
        self.assertIsNone(snap_after["project"]["approved_build_id"])
        ver_stale = self.store.verify_review_package(proj_id, exp_dir)
        self.assertEqual(ver_stale["status"], "INVALID")
        self.assertIn("STALE_EXPORT_BUILD_ID", {i["code"] for i in ver_stale["issues"]})


if __name__ == "__main__":
    unittest.main()
