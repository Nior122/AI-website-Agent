"""Phase 27 end-to-end workflow reliability, state association, review gate, and recovery tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from builder.website_builder import WebsiteBuilder
from deployment.deployer import VercelDeploymentProvider
from orchestration.health import system_health
from orchestration.jobs import JobService, WorkerResult
from outreach.service import OutreachService
from tests.test_phase26_real_draft import pitstop_lead_payload
from tracking.store import TrackingStore


class _PassWorker:
    name = "test-worker"

    def execute(self, job, context):
        return WorkerResult("COMPLETED", output={"ok": True})


class Phase27WorkflowReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TrackingStore(self.root / "state.json", auto_bootstrap_workspace=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_qualified_project(self, *, real: bool = False, name: str = "Pitstop Lagos") -> tuple[dict, dict]:
        lead = self.store.create_lead({
            "business_name": name,
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 807 749 8324" if real else "+234 800 000 0099",
            "website_status": "WEBSITE_POOR" if real else "WEBSITE_OUTDATED",
            "source_urls": ["https://pitstoplagos.com/", "https://www.instagram.com/pitstoplagos/"] if real else ["https://fixture.example.invalid"],
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA" if real else "TEST_FIXTURE",
        })
        project = self.store.create_project(lead["lead_id"])
        return lead, project

    def test_failed_build_cannot_silently_become_successful_or_validated(self) -> None:
        lead, proj = self._create_qualified_project(real=True)
        failed_build = {
            "build_id": "build-failed-001",
            "lead_id": lead["lead_id"],
            "status": "FAILED",
            "errors": ["Template error"],
        }
        self.store.record_build(proj["project_id"], failed_build)
        self.assertEqual(self.store.snapshot(proj["project_id"])["project"]["current_stage"], "FAILED")

        # Cannot validate a failed build even if caller supplies status="PASS"
        with self.assertRaisesRegex(ValueError, "Failed build cannot be validated"):
            self.store.record_validation(proj["project_id"], {
                "validation_id": "val-failed-001",
                "build_id": "build-failed-001",
                "status": "PASS",
            })

        # Cannot overwrite the same failed build_id with status="COMPLETED"
        with self.assertRaisesRegex(ValueError, "Failed build cannot become a successful build"):
            self.store.record_build(proj["project_id"], {
                "build_id": "build-failed-001",
                "lead_id": lead["lead_id"],
                "status": "COMPLETED",
            })

        # Recording a new build with a fresh build_id recovers cleanly from FAILED -> BUILDING -> VALIDATING
        self.store.record_build(proj["project_id"], {
            "build_id": "build-recovered-002",
            "lead_id": lead["lead_id"],
            "status": "COMPLETED",
        })
        self.assertEqual(self.store.snapshot(proj["project_id"])["project"]["current_stage"], "VALIDATING")

    def test_new_build_resets_stale_validation_deployment_and_review_references(self) -> None:
        lead, proj = self._create_qualified_project(real=False, name="Fictional Cafe")
        self.store.record_build(proj["project_id"], {"build_id": "build-1", "lead_id": lead["lead_id"], "status": "COMPLETED"})
        self.store.record_validation(proj["project_id"], {"validation_id": "val-1", "build_id": "build-1", "status": "PASS"})
        self.store.record_deployment(proj["project_id"], {
            "deployment_id": "dep-1",
            "build_id": "build-1",
            "validation_status": "PASS",
            "deployment_status": "DEPLOYED",
            "deployment_url": "https://fictional.vercel.app",
        })
        self.store.record_review(proj["project_id"], "CHANGES_REQUESTED", notes="Update layout")
        self.assertEqual(self.store.snapshot(proj["project_id"])["project"]["current_stage"], "BUILDING")

        # Record Build 2: stale val-1, dep-1, and CHANGES_REQUESTED status must be cleared
        self.store.record_build(proj["project_id"], {"build_id": "build-2", "lead_id": lead["lead_id"], "status": "COMPLETED"})
        snap = self.store.snapshot(proj["project_id"])
        self.assertEqual(snap["project"]["assigned_build_id"], "build-2")
        self.assertIsNone(snap["project"]["latest_validation_id"])
        self.assertIsNone(snap["project"]["latest_deployment_id"])
        self.assertEqual(snap["project"]["review_status"], "PENDING")
        self.assertIsNone(snap["latestValidation"])
        self.assertIsNone(snap["latestDeployment"])

        # JobService must block DEPLOY on build-2 until build-2 itself passes validation
        jobs = JobService(self.store)
        deploy_job = jobs.create("DEPLOY", job_id="deploy-build-2", project_id=proj["project_id"], build_id="build-2")
        self.assertEqual(jobs.run(deploy_job["job_id"], _PassWorker())["status"], "BLOCKED")

    def test_validation_belongs_to_exact_artifact_and_lead(self) -> None:
        lead1, proj1 = self._create_qualified_project(real=True, name="Pitstop Lagos")
        lead2 = self.store.create_lead({
            "business_name": "Second Business",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 809 111 2222",
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        artifact_a = self.root / "output" / "lead-0001" / "build-a" / "index.html"
        artifact_b = self.root / "output" / "lead-0001" / "build-b" / "index.html"
        self.store.record_build(proj1["project_id"], {
            "build_id": "build-a",
            "lead_id": lead1["lead_id"],
            "status": "COMPLETED",
            "output_location": str(artifact_a),
        })

        # Mismatched artifact_path is rejected
        with self.assertRaisesRegex(ValueError, "Validation artifact_path does not match build output_location"):
            self.store.record_validation(proj1["project_id"], {
                "validation_id": "val-a",
                "build_id": "build-a",
                "lead_id": lead1["lead_id"],
                "artifact_path": str(artifact_b),
                "status": "PASS",
            })

        # Mismatched lead_id is rejected
        with self.assertRaisesRegex(ValueError, "Validation lead does not match project lead"):
            self.store.record_validation(proj1["project_id"], {
                "validation_id": "val-a",
                "build_id": "build-a",
                "lead_id": lead2["lead_id"],
                "artifact_path": str(artifact_a),
                "status": "PASS",
            })

        # Matching artifact_path and lead_id succeeds
        recorded = self.store.record_validation(proj1["project_id"], {
            "validation_id": "val-a",
            "build_id": "build-a",
            "lead_id": lead1["lead_id"],
            "artifact_path": str(artifact_a),
            "status": "PASS",
        })
        self.assertEqual(recorded["artifact_path"], str(artifact_a))

    def test_deployment_requires_matching_assigned_build_and_recorded_pass_validation(self) -> None:
        lead, proj = self._create_qualified_project(real=False, name="Fictional Bistro")
        self.store.record_build(proj["project_id"], {"build_id": "build-1", "lead_id": lead["lead_id"], "status": "COMPLETED"})

        # Cannot deploy before validation is recorded in TrackingStore even if payload says validation_status="PASS"
        with self.assertRaisesRegex(ValueError, "Deployment requires a recorded PASS validation"):
            self.store.record_deployment(proj["project_id"], {
                "deployment_id": "dep-unvalidated",
                "build_id": "build-1",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
            })

        self.store.record_validation(proj["project_id"], {"validation_id": "val-1", "build_id": "build-1", "status": "PASS"})
        self.store.transition(proj["project_id"], "BUILDING")
        self.store.record_build(proj["project_id"], {"build_id": "build-2", "lead_id": lead["lead_id"], "status": "COMPLETED"})
        self.store.record_validation(proj["project_id"], {"validation_id": "val-2", "build_id": "build-2", "status": "PASS"})

        # Cannot record a deployment for superseded build-1 when assigned_build_id is build-2
        with self.assertRaisesRegex(ValueError, "Deployment must be a successful PASS deployment for this project build"):
            self.store.record_deployment(proj["project_id"], {
                "deployment_id": "dep-stale",
                "build_id": "build-1",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
            })

        # Cannot record a deployment with mismatched validation_id
        with self.assertRaisesRegex(ValueError, "Deployment validation_id does not match"):
            self.store.record_deployment(proj["project_id"], {
                "deployment_id": "dep-wrong-val",
                "build_id": "build-2",
                "validation_id": "val-1",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
            })

    def test_rejected_or_changes_requested_draft_can_be_corrected_and_reviewed_again_with_audit_trail(self) -> None:
        lead, proj = self._create_qualified_project(real=True, name="Pitstop Lagos")
        self.store.record_build(proj["project_id"], {"build_id": "build-v1", "lead_id": lead["lead_id"], "status": "COMPLETED"})
        self.store.record_validation(proj["project_id"], {"validation_id": "val-v1", "build_id": "build-v1", "status": "PASS"})

        # Unreviewed draft at READY_FOR_REVIEW cannot be approved without deployed preview
        with self.assertRaisesRegex(ValueError, "Approval requires a deployed preview"):
            self.store.record_review(proj["project_id"], "APPROVED")

        # Rejecting the draft at READY_FOR_REVIEW transitions project back to BUILDING and logs audit entry
        self.store.record_review(proj["project_id"], "REJECTED", notes="Contains fictional menu items")
        self.assertEqual(self.store.snapshot(proj["project_id"])["project"]["current_stage"], "BUILDING")
        self.assertEqual(self.store.snapshot(proj["project_id"])["project"]["review_status"], "REJECTED")

        # Corrected build v2 is recorded and validated
        self.store.record_build(proj["project_id"], {"build_id": "build-v2", "lead_id": lead["lead_id"], "status": "COMPLETED"})
        self.store.record_validation(proj["project_id"], {"validation_id": "val-v2", "build_id": "build-v2", "status": "PASS"})
        self.store.record_review(proj["project_id"], "PENDING", notes="Corrected draft ready for human inspection")

        snap = self.store.snapshot(proj["project_id"])
        self.assertEqual(snap["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap["project"]["review_status"], "PENDING")
        self.assertEqual(len(snap["project"]["review_history"]), 2)
        self.assertEqual(snap["project"]["review_history"][0]["status"], "REJECTED")
        self.assertEqual(snap["project"]["review_history"][0]["build_id"], "build-v1")
        self.assertEqual(snap["project"]["review_history"][1]["status"], "PENDING")
        self.assertEqual(snap["project"]["review_history"][1]["build_id"], "build-v2")

    def test_fictional_and_real_business_records_cannot_cross_contaminate_in_tracking_or_deployment(self) -> None:
        real_lead, real_proj = self._create_qualified_project(real=True, name="Pitstop Lagos")
        fictional_lead, fictional_proj = self._create_qualified_project(real=False, name="Fictional Test Kitchen")

        # Cannot attach TEST_FIXTURE build to REAL_PUBLIC_DATA project
        with self.assertRaisesRegex(ValueError, "Cannot attach fictional/test-fixture build to a real-business project"):
            self.store.record_build(real_proj["project_id"], {
                "build_id": "build-fictional-leak",
                "lead_id": real_lead["lead_id"],
                "status": "COMPLETED",
                "data_classification": "TEST_FIXTURE",
            })

        # Cannot attach REAL_PUBLIC_DATA build to TEST_FIXTURE project
        with self.assertRaisesRegex(ValueError, "Cannot attach real-business build to a fictional test project"):
            self.store.record_build(fictional_proj["project_id"], {
                "build_id": "build-real-leak",
                "lead_id": fictional_lead["lead_id"],
                "status": "COMPLETED",
                "data_classification": "REAL_PUBLIC_DATA",
            })

        # Deployment adapter blocks REAL_PUBLIC_DATA build from overwriting shared fictional ai-website-agent-preview project
        builder = WebsiteBuilder(output_root=self.root / "output")
        real_build = builder.build(pitstop_lead_payload())
        self.assertEqual(real_build.status, "COMPLETED")
        vercel_dir = self.root / ".vercel"
        vercel_dir.mkdir(exist_ok=True)
        (vercel_dir / "project.json").write_text(
            json.dumps({"projectId": "prj_shared_demo", "orgId": "team_demo", "projectName": "ai-website-agent-preview"}),
            encoding="utf-8",
        )
        called = []

        def runner(*args, **kwargs):
            called.append(args[0])
            return subprocess.CompletedProcess(args[0], 0, "https://pitstop-preview.vercel.app\n", "")

        blocked = VercelDeploymentProvider(self.root, runner).deploy(Path(real_build.output_location).parent, dry_run=False)
        self.assertEqual(blocked.deployment_status, "NEEDS_REVIEW")
        self.assertIn("disabled by default", blocked.error or "")
        self.assertEqual(called, [])

        # Even with approve_real_preview=True, real builds cannot fall back to shared ai-website-agent-preview
        refused_shared = VercelDeploymentProvider(self.root, runner).deploy(
            Path(real_build.output_location).parent,
            dry_run=False,
            approve_real_preview=True,
        )
        self.assertEqual(refused_shared.deployment_status, "FAILED")
        self.assertIn("cannot use or fall back to the shared fictional preview project", refused_shared.error or "")
        self.assertEqual(called, [])

    def test_missing_local_state_after_sandbox_restart_is_detected_and_recovered_safely(self) -> None:
        sim_root = self.root / "simulated_repo"
        (sim_root / "tests" / "fixtures").mkdir(parents=True)
        fixture_src = Path("tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json")
        (sim_root / "tests" / "fixtures" / "fictional_test_business_lagos_fresh_kitchen.json").write_text(
            fixture_src.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        cold_store = TrackingStore(sim_root / "data" / "tracking" / "state.json")
        initial_check = cold_store.check_local_state(repository_root=sim_root)
        self.assertEqual(initial_check["status"], "MISSING_LOCAL_STATE")
        self.assertFalse(initial_check["state_file_exists"])

        # Without state.json on disk, recovery refuses to fabricate leads or projects
        no_fab = cold_store.recover_local_state(repository_root=sim_root)
        self.assertEqual(no_fab["status"], "MISSING_LOCAL_STATE")
        self.assertEqual(no_fab["recovered_builds"], [])
        self.assertEqual(len(cold_store.state["leads"]), 0)

        # When state.json and durable lead source exist on disk, missing HTML artifact is rebuilt without altering review_status
        (sim_root / "data" / "leads").mkdir(parents=True, exist_ok=True)
        (sim_root / "data" / "leads" / "pitstop_lagos_qualified_lead.json").write_text(
            json.dumps(pitstop_lead_payload(), indent=2),
            encoding="utf-8",
        )
        lead = cold_store.create_lead({
            "business_name": "Pitstop Lagos",
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 807 749 8324",
            "source_urls": ["https://pitstoplagos.com/", "https://www.instagram.com/pitstoplagos/"],
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        proj = cold_store.create_project(lead["lead_id"])
        rel_artifact = "output/lead-0001/build-sim-001/index.html"
        cold_store.record_build(proj["project_id"], {
            "build_id": "build-sim-001",
            "lead_id": lead["lead_id"],
            "status": "COMPLETED",
            "output_location": str(sim_root / rel_artifact),
            "data_classification": "REAL_PUBLIC_DATA",
        })
        cold_store.record_validation(proj["project_id"], {
            "validation_id": "val-sim-001",
            "build_id": "build-sim-001",
            "status": "PASS",
            "artifact_path": str(sim_root / rel_artifact),
        })
        self.assertFalse((sim_root / rel_artifact).is_file())
        recovered = cold_store.recover_local_state(repository_root=sim_root)
        self.assertEqual(recovered["recovered_builds"], ["build-sim-001"])
        self.assertTrue((sim_root / rel_artifact).is_file())
        self.assertEqual(cold_store.snapshot(proj["project_id"])["project"]["review_status"], "PENDING")

    def test_pitstop_lagos_project_0002_remains_pending_review_and_undeployed(self) -> None:
        workspace_store = TrackingStore()
        snap = workspace_store.snapshot("project-0002")
        self.assertEqual(snap["lead"]["lead_id"], "lead-0002")
        self.assertEqual(snap["lead"]["business_name"], "Pitstop Lagos")
        self.assertEqual(snap["lead"]["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(snap["project"]["project_id"], "project-0002")
        self.assertEqual(snap["project"]["assigned_build_id"], "build-2026-10-13T012227z")
        self.assertEqual(snap["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap["project"]["review_status"], "PENDING")
        self.assertIsNone(snap["latestDeployment"])
        self.assertTrue(snap["artifact_exists"])

        # Outreach remains blocked while preview is undeployed and human review is PENDING
        outreach = OutreachService(workspace_store)
        self.assertEqual(outreach.readiness("project-0002"), "NO_PREVIEW")
        health = system_health(workspace_store)
        self.assertEqual(health["source_of_truth"]["local_state"]["status"], "HEALTHY")


if __name__ == "__main__":
    unittest.main()
