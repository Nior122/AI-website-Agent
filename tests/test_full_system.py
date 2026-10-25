"""Fictional, local end-to-end lifecycle, module integration, and recovery/retry tests.

Never contacts or deploys to a real external service and never modifies real-business records.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from builder.website_builder import WebsiteBuilder
from deployment.deployer import VercelDeploymentProvider
from opportunities.service import assess_opportunity
from orchestration.health import readiness_for, system_health
from orchestration.jobs import JobService, WorkerResult
from orchestration.workers import MockWorker, WorkerRegistry
from outreach.service import OutreachService
from research.research_agent import FixtureResearchSource, ResearchAgent
from scout.scout_agent import FixtureSource, Scout, Target
from tracking.cli import confirmation_token, run_cli
from tracking.store import TrackingStore
from validation.quality_validator import validate_rendered_html

FIXTURE = Path(__file__).parent / "fixtures" / "qualified_leads.json"


REPO_ROOT = Path(__file__).resolve().parents[1]

#: The committed inputs `TrackingStore.recover_from_verified_exports` refuses to run without. A fresh
#: checkout has no `data/tracking/state.json` (it is deliberately not in Git), so a test that wants the
#: workspace's project records has to be able to rebuild them from these instead of reading a file that
#: only exists after some other module happened to run first.
_WORKSPACE_RECOVERY_INPUTS = (
    Path("preview-export/lead-0002-pitstop-lagos/review-package.json"),
    Path("preview-export/lead-0002-pitstop-lagos/index.html"),
    Path("preview-export/lead-0002-pitstop-lagos/REVIEW_README.md"),
    Path("tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json"),
)


def _workspace_store_copy(root: Path, *, name: str = "state-workspace-copy.json") -> TrackingStore:
    """Read the workspace's recorded project state through a disposable copy.

    These tests must see the project-0002 build/validation records exactly as recorded, but building
    or verifying a review package persists `updated_at` and package digests onto those records. Going
    through the default state path would mutate real workspace data every time the suite runs, so the
    copy carries the writes. The build artifact itself is still read read-only from the repository.

    The tracking state file is untracked on purpose, so on a fresh clone there is nothing to copy. The
    records are then rebuilt inside this temporary root from the SHA-256-verified review package - the
    same recovery the application performs - which keeps the test independent of module order and
    guarantees it never writes into the workspace it is checking.
    """
    temp_state = Path(root) / name
    workspace_state = Path("data/tracking/state.json")
    if workspace_state.is_file():
        temp_state.write_bytes(workspace_state.read_bytes())
        return TrackingStore(temp_state)
    for relative in _WORKSPACE_RECOVERY_INPUTS:
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)
    store = TrackingStore(temp_state)
    store.recover_from_verified_exports(repository_root=Path(root))
    return store


class FullSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"
        self.store = TrackingStore(self.state_path)
        self.jobs = JobService(self.store)
        self.registry = WorkerRegistry(self.store)
        self.registry.register(
            "fictional-builder",
            name="Fictional test worker",
            capabilities=["BUILD", "VALIDATE", "DEPLOY"],
            worker_type="OTHER",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fictional_candidates_and_evidence(self) -> tuple[Path, Path]:
        candidates = [
            {
                "candidate_id": "cand-fictional-001",
                "data_classification": "TEST_FIXTURE",
                "business_name": "Lagos Fresh Kitchen (TEST / FICTIONAL)",
                "niche": "Restaurants",
                "city": "Lagos",
                "country": "Nigeria",
                "address": "12 Fictional Way, Victoria Island, Lagos",
                "phone": "+2348000000099",
                "whatsapp": "+2348000000099",
                "instagram_url": "https://instagram.example.invalid/lagos-fresh-kitchen",
                "services": ["Rice bowls", "Grilled fish"],
                "description": "Fictional restaurant integration fixture.",
                "opening_hours": "09:00-18:00",
                "website_status": "none_known",
                "source_url": "https://fixture.example.invalid/lagos-fresh-kitchen",
            }
        ]
        evidence = [
            {
                "candidate_name": "Lagos Fresh Kitchen (TEST / FICTIONAL)",
                "business_name": "Lagos Fresh Kitchen (TEST / FICTIONAL)",
                "city": "Lagos",
                "country": "Nigeria",
                "address": "12 Fictional Way, Victoria Island, Lagos",
                "phone": "+2348000000099",
                "instagram_url": "https://instagram.example.invalid/lagos-fresh-kitchen",
                "services": ["Rice bowls", "Grilled fish"],
                "description": "Fictional restaurant integration fixture.",
                "activity_signal": "fictional public fixture",
                "source_url": "https://fixture.example.invalid/lagos-fresh-kitchen-directory",
            }
        ]
        candidate_path = self.root / "candidate.json"
        evidence_path = self.root / "evidence.json"
        candidate_path.write_text(json.dumps(candidates), encoding="utf-8")
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        return candidate_path, evidence_path

    def test_fictional_lifecycle_stops_at_ready_to_send(self) -> None:
        candidate_path, evidence_path = self._fictional_candidates_and_evidence()
        scout = Scout(Target("Nigeria", "Lagos", "Restaurant", lead_limit=1), [FixtureSource(candidate_path)]).run()
        self.assertEqual(scout.status, "completed")
        researched = ResearchAgent([FixtureResearchSource(evidence_path)]).run(scout.leads[0])
        self.assertEqual(researched.record["research"]["qualificationStatus"], "QUALIFIED")
        research = researched.record
        research["lead_id"] = "fictional-lagos-fresh-kitchen"
        research["demo"] = True
        build = WebsiteBuilder(self.root / "output").build(research)
        self.assertEqual(build.status, "COMPLETED")
        html = Path(build.output_location).read_text(encoding="utf-8")
        validation = validate_rendered_html(
            html,
            "Lagos Fresh Kitchen (TEST / FICTIONAL)",
            real_build=False,
            build_id=build.build_id,
            lead_id=build.lead_id,
        ).to_dict()
        self.assertEqual(validation["status"], "PASS")
        lead = self.store.create_lead(
            {
                "business_name": "Lagos Fresh Kitchen (TEST / FICTIONAL)",
                "business_type": "Restaurant",
                "city": "Lagos",
                "country": "Nigeria",
                "qualification_status": "QUALIFIED",
                "website_status": "NO_WEBSITE",
                "source_urls": ["https://fixture.example.invalid/lagos-fresh-kitchen"],
                "research_source_urls": ["https://fixture.example.invalid/lagos-fresh-kitchen"],
                "phone": "+2348000000099",
                "whatsapp": "+2348000000099",
            }
        )
        project = self.store.create_project(lead["lead_id"])
        job = self.jobs.create(
            "BUILD",
            job_id="fictional-build",
            lead_id=lead["lead_id"],
            project_id=project["project_id"],
            build_id=build.build_id,
        )
        self.assertEqual(readiness_for(self.store, self.jobs, "fictional-build", self.registry)["status"], "READY")
        tracked_build = build.to_dict()
        tracked_build["lead_id"] = lead["lead_id"]
        self.store.record_build(project["project_id"], tracked_build)
        self.store.record_validation(project["project_id"], validation)
        # This is a fictional preview record, not a Vercel request or deployment.
        self.store.record_deployment(
            project["project_id"],
            {
                "deployment_id": "fictional-preview",
                "build_id": build.build_id,
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://fictional-preview.example.invalid",
            },
        )
        self.store.record_review(project["project_id"], "APPROVED", "fictional human review")
        draft = OutreachService(self.store).create(project["project_id"], "WHATSAPP")
        approved = OutreachService(self.store).review(draft["outreach_id"], "APPROVED")
        self.assertEqual(approved["status"], "READY_TO_SEND")
        self.assertNotIn("send", OutreachService.__dict__)
        self.assertEqual(self.store.snapshot(project["project_id"])["latestDeployment"]["build_id"], build.build_id)

    def test_health_and_readiness_report_missing_external_configuration(self) -> None:
        job = self.jobs.create("SCOUT", job_id="health-check")
        health = system_health(self.store, self.registry, repository_root=self.root)
        self.assertEqual(health["application"]["status"], "healthy")
        self.assertEqual(health["deployment"]["status"], "degraded")
        self.assertEqual(readiness_for(self.store, self.jobs, job["job_id"], self.registry)["status"], "NOT_READY")

    def test_end_to_end_pipeline_integration_and_isolation(self) -> None:
        """Exercises stages 1-12 end-to-end through actual module interfaces without manual dict glue."""
        candidate_path, evidence_path = self._fictional_candidates_and_evidence()

        # 1. Discovery -> 2. Research -> 3. Opportunity assessment -> 4. Lead qualification & ingestion
        scout_res = Scout(Target("Nigeria", "Lagos", "Restaurants", lead_limit=1), [FixtureSource(candidate_path)]).run()
        self.assertEqual(scout_res.status, "completed")
        candidate = scout_res.leads[0]
        self.assertEqual(candidate["dataClassification"], "TEST_FIXTURE")

        research_res = ResearchAgent([FixtureResearchSource(evidence_path)]).run(candidate)
        self.assertEqual(research_res.status, "completed")
        self.assertEqual(research_res.record["data_classification"], "TEST_FIXTURE")
        self.assertEqual(research_res.record["research"]["qualificationStatus"], "QUALIFIED")
        # Confirm verified facts vs uncertainties are distinguished (originalWebsite was None -> uncertainty)
        uncertainty_fields = {u["field"] for u in research_res.record["research"]["uncertainties"]}
        self.assertIn("originalWebsite", uncertainty_fields)

        assessment = assess_opportunity(candidate)
        self.assertEqual(assessment["data_classification"], "TEST_FIXTURE")

        # Ingest into TrackingStore via integrated handoff
        lead = self.store.ingest_research_record(research_res.record)
        self.assertEqual(lead["lead_id"], "lead-0001")
        self.assertEqual(lead["data_classification"], "TEST_FIXTURE")
        self.assertEqual(lead["qualification_status"], "QUALIFIED")
        self.assertEqual(lead["website_status"], "NO_WEBSITE")

        project = self.store.create_project(lead["lead_id"])
        self.assertEqual(project["project_id"], "project-0001")
        self.assertEqual(project["lead_id"], lead["lead_id"])
        self.assertEqual(project["current_stage"], "QUALIFIED")
        self.assertEqual(project["review_status"], "PENDING")

        # 5. Business-data preparation -> 6. Template selection -> 7. Website generation -> 8. Quality validation
        build_input = self.store.build_payload(lead["lead_id"])
        builder = WebsiteBuilder(output_root=self.root / "output")
        build_res = builder.build(build_input)
        self.assertEqual(build_res.status, "COMPLETED")
        self.assertEqual(build_res.lead_id, lead["lead_id"])
        self.assertEqual(build_res.template_id, "restaurant-menu-v1")
        self.assertEqual(build_res.metadata["data_classification"], "TEST_FIXTURE")
        self.assertTrue(Path(build_res.output_location).is_file())
        rendered_html = Path(build_res.output_location).read_text(encoding="utf-8")
        self.assertIn("Lagos Fresh Kitchen (TEST / FICTIONAL)", rendered_html)
        self.assertIn("Rice bowls", rendered_html)
        self.assertEqual(build_res.validation["status"], "PASS")
        self.assertEqual(
            Path(build_res.validation["artifact_path"]).resolve(),
            Path(build_res.output_location).resolve(),
        )

        # 9. Build and validation tracking: project reaches READY_FOR_REVIEW only after validation PASS
        self.store.record_build(project["project_id"], build_res.to_dict())
        self.assertEqual(self.store.snapshot(project["project_id"])["project"]["current_stage"], "VALIDATING")
        self.store.record_validation(project["project_id"], build_res.validation)
        snap = self.store.snapshot(project["project_id"])
        self.assertEqual(snap["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap["project"]["review_status"], "PENDING")
        self.assertEqual(snap["project"]["assigned_build_id"], build_res.build_id)
        self.assertEqual(snap["latestValidation"]["validation_id"], build_res.validation["validation_id"])

        # Failed builds on a second fictional lead cannot proceed to validation or deployment
        bad_lead = self.store.create_lead({
            "business_name": "Broken Fictional Cafe",
            "business_type": "Restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+2348000000199",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        bad_proj = self.store.create_project(bad_lead["lead_id"])
        failed_build = builder.build({"lead_id": bad_lead["lead_id"], "research": {"qualificationStatus": "QUALIFIED"}, "candidate": {}})
        self.assertEqual(failed_build.status, "FAILED")
        self.store.record_build(bad_proj["project_id"], failed_build.to_dict())
        self.assertEqual(self.store.snapshot(bad_proj["project_id"])["project"]["current_stage"], "FAILED")
        with self.assertRaisesRegex(ValueError, "Failed build cannot be validated"):
            self.store.record_validation(bad_proj["project_id"], {
                "validation_id": "val-broken",
                "build_id": failed_build.build_id,
                "status": "PASS",
            })

        # Stale validation cannot authorize a newer build
        build_res_2 = builder.build(build_input)
        self.assertNotEqual(build_res_2.build_id, build_res.build_id)
        self.store.record_build(project["project_id"], build_res_2.to_dict())
        with self.assertRaisesRegex(ValueError, "Validation does not belong to this project build"):
            self.store.record_validation(project["project_id"], build_res.validation)
        with self.assertRaisesRegex(ValueError, "Deployment requires a recorded PASS validation"):
            self.store.record_deployment(project["project_id"], {
                "deployment_id": "dep-stale",
                "build_id": build_res_2.build_id,
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://ai-website-agent-preview.vercel.app",
            })
        # Record build_res_2's own PASS validation so project-0001 reaches READY_FOR_REVIEW again
        self.store.record_validation(project["project_id"], build_res_2.validation)
        self.assertEqual(self.store.snapshot(project["project_id"])["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(self.store.snapshot(project["project_id"])["project"]["review_status"], "PENDING")

        # Fictional and real-business data never cross-contaminate
        real_copy = json.loads(json.dumps(research_res.record))
        real_copy["data_classification"] = "REAL_PUBLIC_DATA"
        real_copy["candidate"]["dataClassification"] = "REAL_PUBLIC_DATA"
        with self.assertRaisesRegex(ValueError, "Cannot merge fictional and real-business lead records"):
            self.store.ingest_research_record(real_copy)

    def test_recovery_and_retry_failure_scenarios(self) -> None:
        """Exercises all 8 failure, retry, and recovery scenarios in Requirement 4."""
        candidate_path, evidence_path = self._fictional_candidates_and_evidence()
        scout_res = Scout(Target("Nigeria", "Lagos", "Restaurants", lead_limit=1), [FixtureSource(candidate_path)]).run()
        research_res = ResearchAgent([FixtureResearchSource(evidence_path)]).run(scout_res.leads[0])

        # 1. Missing source lead -> clear errors rather than fabricated defaults
        with self.assertRaisesRegex(ValueError, "Unknown lead: lead-9999"):
            self.store.build_payload("lead-9999")
        with self.assertRaisesRegex(ValueError, "Research record must contain 'candidate' and 'research'"):
            self.store.ingest_research_record({})
        missing_job = self.jobs.create("BUILD", job_id="job-missing-lead", lead_id="lead-9999")
        blocked_res = self.jobs.run("job-missing-lead", MockWorker([]))
        self.assertEqual(blocked_res["status"], "BLOCKED")
        self.assertIn("Lead reference is required", blocked_res["error"])

        # 2. Repeated execution of the same ingestion, project creation, build, validation, deployment, and job
        lead_a = self.store.ingest_research_record(research_res.record)
        lead_b = self.store.ingest_research_record(research_res.record)
        self.assertEqual(lead_a["lead_id"], lead_b["lead_id"])
        self.assertEqual(len(self.store.state["leads"]), 1)

        proj_a = self.store.create_project(lead_a["lead_id"])
        proj_b = self.store.create_project(lead_a["lead_id"])
        self.assertEqual(proj_a["project_id"], proj_b["project_id"])
        self.assertEqual(len(self.store.state["projects"]), 1)

        # 3. Failed website generation & 4. Failed validation stop downstream progression
        builder = WebsiteBuilder(output_root=self.root / "output")
        bad_payload = json.loads(json.dumps(self.store.build_payload(lead_a["lead_id"])))
        bad_payload["research"]["verifiedFacts"] = []
        bad_payload["candidate"] = {}
        failed_gen = builder.build(bad_payload)
        self.assertEqual(failed_gen.status, "FAILED")
        self.store.record_build(proj_a["project_id"], failed_gen.to_dict())
        self.assertEqual(self.store.snapshot(proj_a["project_id"])["project"]["current_stage"], "FAILED")

        # Replace failed build with a valid build, then test failed validation
        good_build_1 = builder.build(self.store.build_payload(lead_a["lead_id"]))
        self.assertEqual(good_build_1.status, "COMPLETED")
        self.store.record_build(proj_a["project_id"], good_build_1.to_dict())
        # Idempotent retry of record_build with the same build_id
        self.store.record_build(proj_a["project_id"], good_build_1.to_dict())
        self.assertEqual(self.store.snapshot(proj_a["project_id"])["project"]["current_stage"], "VALIDATING")

        failed_val = {**good_build_1.validation, "validation_id": "val-fail-001", "status": "FAIL", "passed": False}
        self.store.record_validation(proj_a["project_id"], failed_val)
        self.assertEqual(self.store.snapshot(proj_a["project_id"])["project"]["current_stage"], "FAILED")
        # Idempotent retry of record_validation with the same validation_id
        self.store.record_validation(proj_a["project_id"], failed_val)

        # 5. A new build replacing an older build & 6. Stale validation/deployment blocked
        good_build_2 = builder.build(self.store.build_payload(lead_a["lead_id"]))
        self.store.record_build(proj_a["project_id"], good_build_2.to_dict())
        self.store.record_validation(proj_a["project_id"], good_build_2.validation)
        # Idempotent retry of PASS validation
        self.store.record_validation(proj_a["project_id"], good_build_2.validation)
        self.assertEqual(self.store.snapshot(proj_a["project_id"])["project"]["current_stage"], "READY_FOR_REVIEW")

        # 7. Missing artifact after workspace restoration -> recovered from durable research_record without fabricating
        artifact_file = Path(good_build_2.output_location)
        self.assertTrue(artifact_file.is_file())
        artifact_file.unlink()
        check_missing = self.store.check_local_state(repository_root=self.root)
        self.assertEqual(check_missing["status"], "MISSING_LOCAL_STATE")
        self.assertIn(str(artifact_file), check_missing["missing_build_artifacts"])

        recovered = self.store.recover_local_state(repository_root=self.root)
        self.assertEqual(recovered["recovered_builds"], [good_build_2.build_id])
        self.assertTrue(artifact_file.is_file())
        self.assertEqual(self.store.snapshot(proj_a["project_id"])["project"]["review_status"], "PENDING")

        # 8. Rejected draft returning for correction -> blocks deployment until rebuilt and re-validated
        rej_token = confirmation_token(proj_a["project_id"], good_build_2.build_id, "REJECTED")
        rc = run_cli(
            [
                "--state-path",
                str(self.state_path),
                "review",
                proj_a["project_id"],
                "--decision",
                "REJECTED",
                "--notes",
                "Reject draft to verify correction loop",
                "--confirm",
                rej_token,
            ],
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(rc, 0)
        reloaded = TrackingStore(self.state_path)
        self.assertEqual(reloaded.snapshot(proj_a["project_id"])["project"]["current_stage"], "BUILDING")
        self.assertEqual(reloaded.snapshot(proj_a["project_id"])["project"]["review_status"], "REJECTED")

        # Deploying a rejected build must fail both in TrackingStore and JobService
        with self.assertRaisesRegex(ValueError, "Project is not in READY_FOR_REVIEW"):
            reloaded.record_deployment(proj_a["project_id"], {
                "deployment_id": "dep-rejected",
                "build_id": good_build_2.build_id,
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://ai-website-agent-preview.vercel.app",
            })
        jobs_reloaded = JobService(reloaded)
        dep_job = jobs_reloaded.create(
            "DEPLOY",
            job_id="job-deploy-rejected",
            lead_id=lead_a["lead_id"],
            project_id=proj_a["project_id"],
            build_id=good_build_2.build_id,
        )
        dep_job_res = jobs_reloaded.run("job-deploy-rejected", MockWorker([]))
        self.assertEqual(dep_job_res["status"], "BLOCKED")
        self.assertIn("not rejected or awaiting rebuild", dep_job_res["error"])

        # Rebuild after rejection, validate, and verify job chaining and idempotency
        good_build_3 = builder.build(reloaded.build_payload(lead_a["lead_id"]))
        build_job = jobs_reloaded.create(
            "BUILD",
            job_id="job-build-3",
            lead_id=lead_a["lead_id"],
            project_id=proj_a["project_id"],
        )
        worker_out = WorkerResult(
            "COMPLETED",
            output={"build_id": good_build_3.build_id, "project_id": proj_a["project_id"], "lead_id": lead_a["lead_id"]},
            artifacts=[good_build_3.output_location],
        )
        reloaded.record_build(proj_a["project_id"], good_build_3.to_dict())
        done_build_job = jobs_reloaded.run(build_job["job_id"], MockWorker([worker_out]))
        # Running the completed job a second time is a no-op
        done_build_job_retry = jobs_reloaded.run(build_job["job_id"], MockWorker([]))
        self.assertEqual(done_build_job["job_id"], done_build_job_retry["job_id"])
        self.assertEqual(done_build_job_retry["attempts"], 1)

        # Chain BUILD -> VALIDATE via create_next and confirm build_id propagated automatically
        val_job = jobs_reloaded.create_next("job-build-3", next_job_id="job-val-3")
        self.assertEqual(val_job["build_id"], good_build_3.build_id)
        reloaded.record_validation(proj_a["project_id"], good_build_3.validation)
        self.assertEqual(reloaded.snapshot(proj_a["project_id"])["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(reloaded.snapshot(proj_a["project_id"])["project"]["review_status"], "PENDING")

    def test_phase30_pitstop_review_and_deployment_isolation_safeguards(self):
        """Phase 30 safeguards: sourceUrl requirement, card numbering, rejected manifest & real-preview isolation."""
        import copy
        from templates.components import card_list

        # 1. Card numbering formats 10+ items as two digits (10, 11) rather than 010, 011
        cards_html = card_list(
            "offerings",
            "Offerings",
            [{"name": f"Service {i}"} for i in range(1, 12)],
        )
        self.assertIn(">01<", cards_html)
        self.assertIn(">09<", cards_html)
        self.assertIn(">10<", cards_html)
        self.assertIn(">11<", cards_html)
        self.assertNotIn(">010<", cards_html)

        # 2. Real-business WebsiteBuilder build requires preserved public sourceUrls
        builder = WebsiteBuilder(output_root=self.root / "output")
        sourceless_real = {
            "lead_id": "lead-sourceless",
            "demo": False,
            "data_classification": "REAL_PUBLIC_DATA",
            "candidate": {
                "businessName": "Sourceless Cafe",
                "niche": "restaurant",
                "location": {"city": "Lagos", "country": "Nigeria"},
                "sourceUrls": [],
                "dataClassification": "REAL_PUBLIC_DATA",
            },
            "research": {
                "qualificationStatus": "QUALIFIED",
                "verifiedFacts": [
                    {"field": "businessName", "value": "Sourceless Cafe"},
                    {"field": "location", "value": {"city": "Lagos", "country": "Nigeria"}},
                ],
            },
        }
        sourceless_res = builder.build(sourceless_real)
        self.assertEqual(sourceless_res.status, "FAILED")
        self.assertTrue(any("sourceUrls" in err for err in sourceless_res.errors))

        # 3. Deployer blocks manifest marked REJECTED/CHANGES_REQUESTED or with mismatched validation lead_id
        sourced_real = copy.deepcopy(sourceless_real)
        sourced_real["candidate"]["sourceUrls"] = ["https://pitstoplagos.com/"]
        sourced_real["research"]["verifiedFacts"].append(
            {"field": "contact", "value": {"phone": "+234 807 749 8324"}}
        )
        good_res = builder.build(sourced_real)
        self.assertEqual(good_res.status, "COMPLETED")
        build_dir = Path(good_res.output_location).parent
        manifest_path = build_dir / "build.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        manifest_rejected = {**manifest, "review_status": "REJECTED"}
        manifest_path.write_text(json.dumps(manifest_rejected), encoding="utf-8")
        provider = VercelDeploymentProvider(self.root, runner=lambda *a, **kw: None)
        dep_rejected = provider.deploy(
            build_dir,
            dry_run=False,
            project_name="pitstop-lagos-isolated-preview",
            approve_real_preview=True,
        )
        self.assertEqual(dep_rejected.deployment_status, "FAILED")
        self.assertIn("REJECTED", dep_rejected.error)

        manifest_wrong_lead = {
            **manifest,
            "validation": {**manifest["validation"], "lead_id": "lead-other"},
        }
        manifest_path.write_text(json.dumps(manifest_wrong_lead), encoding="utf-8")
        dep_wrong_lead = provider.deploy(
            build_dir,
            dry_run=False,
            project_name="pitstop-lagos-isolated-preview",
            approve_real_preview=True,
        )
        self.assertEqual(dep_wrong_lead.deployment_status, "FAILED")
        self.assertIn("lead_id", dep_wrong_lead.error)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        # 4. TrackingStore & JobService block REAL_PUBLIC_DATA deployment to shared fictional preview project
        store = TrackingStore(self.root / "state-p30.json")
        lead = store.create_lead({
            "business_name": "Pitstop Lagos",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 807 749 8324",
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        proj = store.create_project(lead["lead_id"])
        store.record_build(proj["project_id"], {**good_res.to_dict(), "lead_id": lead["lead_id"]})
        store.record_validation(proj["project_id"], {**good_res.validation, "lead_id": lead["lead_id"]})

        with self.assertRaisesRegex(ValueError, "shared fictional preview project"):
            store.record_deployment(proj["project_id"], {
                "deployment_id": "dep-shared-leak",
                "build_id": good_res.build_id,
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://ai-website-agent-preview.vercel.app",
                "metadata": {"project_name": "ai-website-agent-preview"},
            })

        jobs = JobService(store)
        unauth_job = jobs.create(
            "DEPLOY",
            job_id="job-real-unauth",
            lead_id=lead["lead_id"],
            project_id=proj["project_id"],
            build_id=good_res.build_id,
        )
        unauth_res = jobs.run("job-real-unauth", MockWorker([]))
        self.assertEqual(unauth_res["status"], "BLOCKED")
        self.assertIn("approve_real_preview", unauth_res["error"])

    def test_phase31_evidence_hierarchy_conflict_withholding_and_review_package(self):
        """Phase 31: conflicting facts withheld, fixture/doc rejected as real evidence, stale validation invalidated, and review package verified."""
        import copy
        from validation.quality_validator import validate_rendered_html

        # 1. Conflicting address and phone across ResearchAgent or WebsiteBuilder are withheld, never silently picked
        conflicting_payload = {
            "lead_id": "lead-conflict-001",
            "demo": False,
            "data_classification": "REAL_PUBLIC_DATA",
            "candidate": {
                "businessName": "Pitstop Lagos",
                "niche": "restaurant",
                "location": {"city": "Lagos", "country": "Nigeria", "address": "92A Younis Bashorun Street"},
                "contact": {"phone": "+234 807 749 8324", "email": "info@pitstoplagos.com"},
                "social": {"instagramUrl": "https://www.instagram.com/pitstoplagos/"},
                "sourceUrls": ["https://pitstoplagos.com/", "https://www.instagram.com/pitstoplagos/"],
                "dataClassification": "REAL_PUBLIC_DATA",
            },
            "research": {
                "qualificationStatus": "QUALIFIED",
                "researchSources": ["https://pitstoplagos.com/", "https://www.instagram.com/pitstoplagos/"],
                "verifiedFacts": [
                    {"field": "businessName", "value": "Pitstop Lagos", "sourceUrls": ["https://pitstoplagos.com/"]},
                    {"field": "location", "value": {"city": "Lagos", "country": "Nigeria", "address": "92A Younis Bashorun Street"}, "sourceUrls": ["https://pitstoplagos.com/"]},
                    {"field": "address", "value": "92b Younis Bashorun Street", "sourceUrls": ["https://www.instagram.com/pitstoplagos/"]},
                    {"field": "contact", "value": {"phone": "+234 807 749 8324", "email": "info@pitstoplagos.com"}, "sourceUrls": ["https://pitstoplagos.com/"]},
                    {"field": "phone", "value": "+234 817 999 9005", "sourceUrls": ["https://www.instagram.com/pitstoplagos/"]},
                    {"field": "social", "value": {"instagramUrl": "https://www.instagram.com/pitstoplagos/"}, "sourceUrls": ["https://www.instagram.com/pitstoplagos/"]},
                    {"field": "description", "value": "A wellness and cycling-inspired cafe and restaurant in Victoria Island, Lagos.", "sourceUrls": ["https://pitstoplagos.com/"]},
                    {"field": "services", "value": [{"name": "Wattbike Training Studio", "description": "Dedicated indoor cycling training."}], "sourceUrls": ["https://pitstoplagos.com/"]},
                ],
            },
        }
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(conflicting_payload)
        self.assertEqual(res.status, "COMPLETED")
        html = Path(res.output_location).read_text(encoding="utf-8")
        self.assertNotIn("92A Younis Bashorun", html)
        self.assertNotIn("92b Younis Bashorun", html)
        self.assertNotIn("807 749 8324", html)
        self.assertNotIn("817 999 9005", html)
        self.assertNotIn("tel:", html)
        self.assertIn("info@pitstoplagos.com", html)
        self.assertIn("https://www.instagram.com/pitstoplagos/", html)
        self.assertIn("address", res.metadata["withheld_fields"])
        self.assertIn("phone", res.metadata["withheld_fields"])

        # Validator rejects rendered HTML if a disputed fact leaks into the page
        leaked_disputed_html = html.replace("Email: info@pitstoplagos.com", "Tel: +234 807 749 8324 · Email: info@pitstoplagos.com")
        val_leak = validate_rendered_html(
            leaked_disputed_html,
            "Pitstop Lagos",
            real_build=True,
            expected_contact={"phone": None, "email": "info@pitstoplagos.com"},
            expected_facts={"disputed_facts": res.metadata["disputed_facts"]},
        )
        self.assertEqual(val_leak.status, "FAIL")
        self.assertTrue(any(f.code == "DISPUTED_FACT_RENDERED" for f in val_leak.findings))

        # 2. A test fixture or changelog/documentation file cannot serve as evidence for a real-business claim
        fixture_sourced = copy.deepcopy(conflicting_payload)
        fixture_sourced["candidate"]["sourceUrls"] = ["tests/test_phase26_real_draft.py", "CHANGELOG.md"]
        fixture_sourced["research"]["researchSources"] = ["https://example.invalid/pitstop"]
        for item in fixture_sourced["research"]["verifiedFacts"]:
            item["sourceUrls"] = ["tests/test_phase26_real_draft.py", "CHANGELOG.md"]
        bad_res = builder.build(fixture_sourced)
        self.assertEqual(bad_res.status, "FAILED")
        self.assertTrue(any("test fixtures and documentation cannot serve as evidence" in e for e in bad_res.errors))

        # 3. Verify workspace project-0002 new build, stale validation invalidation, and human-review package
        ws_store = _workspace_store_copy(self.root)
        snap = ws_store.snapshot("project-0002")
        self.assertEqual(snap["project"]["assigned_build_id"], "build-2026-10-13T012227z")
        self.assertEqual(snap["project"]["latest_validation_id"], "validation-2026-10-13T012227z")
        self.assertEqual(snap["project"]["review_status"], "PENDING")
        # Previous validation (validation-2026-10-12T193556z) cannot authorize deployment for the new build
        with self.assertRaisesRegex(ValueError, "Deployment validation_id does not match|Deployment must be a successful PASS"):
            ws_store.record_deployment("project-0002", {
                "deployment_id": "dep-stale-val",
                "build_id": "build-2026-10-13T012227z",
                "validation_id": "validation-2026-10-12T193556z",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://pitstop-lagos-preview.vercel.app",
                "metadata": {"project_name": "pitstop-lagos-preview"},
            })

        pkg = ws_store.build_review_package("project-0002", export_dir=self.root / "review-pkg")
        self.assertEqual(pkg["build_id"], "build-2026-10-13T012227z")
        self.assertTrue(pkg["disclosure_present_in_html"])
        self.assertEqual(pkg["external_links"], ["https://www.instagram.com/pitstoplagos/"])
        disputed_names = {item["field"] for item in pkg["unresolved_or_conflicting_facts"] if item["status"] == "CONFLICTING_WITHHELD"}
        self.assertEqual(disputed_names, {"address", "phone"})
        self.assertTrue((self.root / "review-pkg" / "index.html").is_file())
        self.assertTrue((self.root / "review-pkg" / "review-package.json").is_file())
        self.assertTrue((self.root / "review-pkg" / "REVIEW_README.md").is_file())

    def test_phase32_review_package_reproducibility_stale_detection_and_local_server(self):
        """Phase 32: review-package reproducibility, stale/tampered export detection, and 127.0.0.1-only local server."""
        from builder.cli import validate_serve_target

        ws_store = _workspace_store_copy(self.root)
        pkg_dir_1 = self.root / "pkg-1"
        pkg_dir_2 = self.root / "pkg-2"
        pkg1 = ws_store.build_review_package("project-0002", export_dir=pkg_dir_1)
        pkg2 = ws_store.build_review_package("project-0002", export_dir=pkg_dir_1)
        self.assertEqual(pkg1["build_id"], pkg2["build_id"])
        self.assertEqual(pkg1["artifact_sha256"], pkg2["artifact_sha256"])

        # Re-exporting to the same path is byte-for-byte reproducible
        ws_store.build_review_package("project-0002", export_dir=pkg_dir_2)
        self.assertEqual((pkg_dir_1 / "index.html").read_bytes(), (pkg_dir_2 / "index.html").read_bytes())

        ver_ok = ws_store.verify_review_package("project-0002", pkg_dir_1)
        self.assertEqual(ver_ok["status"], "VALID")
        self.assertTrue(ver_ok["hash_matches"])
        self.assertEqual(ver_ok["issues"], [])

        # Detect stale export build_id/validation_id
        stale_json = json.loads((pkg_dir_2 / "review-package.json").read_text(encoding="utf-8"))
        stale_json["build_id"] = "build-2026-10-12T193556z"
        stale_json["validation"]["validation_id"] = "validation-2026-10-12T193556z"
        (pkg_dir_2 / "review-package.json").write_text(json.dumps(stale_json), encoding="utf-8")
        ver_stale = ws_store.verify_review_package("project-0002", pkg_dir_2)
        self.assertEqual(ver_stale["status"], "INVALID")
        stale_codes = {item["code"] for item in ver_stale["issues"]}
        self.assertIn("STALE_EXPORT_BUILD_ID", stale_codes)
        self.assertIn("STALE_EXPORT_VALIDATION_ID", stale_codes)

        # Detect tampered HTML, fictional leak, and disputed-fact leak
        (pkg_dir_2 / "index.html").write_text(
            (pkg_dir_1 / "index.html").read_text(encoding="utf-8") + "<!-- Lagos Fresh Kitchen +234 807 749 8324 -->",
            encoding="utf-8",
        )
        ver_tampered = ws_store.verify_review_package("project-0002", pkg_dir_2)
        self.assertEqual(ver_tampered["status"], "INVALID")
        tampered_codes = {item["code"] for item in ver_tampered["issues"]}
        self.assertIn("PACKAGE_HASH_MISMATCH", tampered_codes)
        self.assertIn("CANONICAL_HASH_MISMATCH", tampered_codes)
        self.assertIn("FICTIONAL_DATA_IN_REAL_PACKAGE", tampered_codes)
        self.assertIn("DISPUTED_FACT_IN_EXPORTED_HTML", tampered_codes)

        # Local preview server enforces 127.0.0.1 loopback-only binding and verifies package hash
        self.assertEqual(validate_serve_target(pkg_dir_1, host="127.0.0.1"), pkg_dir_1.resolve())
        with self.assertRaisesRegex(ValueError, "only loopback '127.0.0.1'"):
            validate_serve_target(pkg_dir_1, host="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "only loopback '127.0.0.1'"):
            validate_serve_target(pkg_dir_1, host="192.168.1.10")
        with self.assertRaisesRegex(ValueError, "Review package integrity error"):
            validate_serve_target(pkg_dir_2, host="127.0.0.1")

        # Deployment safeguards on an isolated copy of the workspace tracking store
        iso_store = _workspace_store_copy(self.root, name="state-p32.json")
        iso_jobs = JobService(iso_store)

        # (a) Production target rejected in TrackingStore and JobService
        with self.assertRaisesRegex(ValueError, "production target"):
            iso_store.record_deployment("project-0002", {
                "deployment_id": "dep-prod-attempt",
                "build_id": "build-2026-10-13T012227z",
                "validation_id": "validation-2026-10-13T012227z",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://pitstop-lagos-prod.vercel.app",
                "metadata": {"project_name": "pitstop-lagos-prod", "target": "production"},
            })
        prod_job = iso_jobs.create(
            "DEPLOY",
            job_id="job-prod-p32",
            lead_id="lead-0002",
            project_id="project-0002",
            build_id="build-2026-10-13T012227z",
            input={"approve_real_preview": True, "project_name": "pitstop-lagos-prod"},
        )
        self.assertEqual(iso_jobs.run("job-prod-p32", MockWorker([]))["status"], "BLOCKED")

        # (b) Real-business deployment without isolated preview project target is rejected
        with self.assertRaisesRegex(ValueError, "isolated preview project"):
            iso_store.record_deployment("project-0002", {
                "deployment_id": "dep-no-target",
                "build_id": "build-2026-10-13T012227z",
                "validation_id": "validation-2026-10-13T012227z",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://pitstop.vercel.app",
                "metadata": {},
            })

        # (c) PENDING review cannot approve without deployed preview, and REJECTED/CHANGES_REQUESTED block deployment
        with self.assertRaisesRegex(ValueError, "Approval requires a deployed preview"):
            iso_store.record_review("project-0002", "APPROVED", notes="Premature approval")
        iso_store.record_review("project-0002", "CHANGES_REQUESTED", notes="Hold for human review")
        with self.assertRaisesRegex(ValueError, "Project is not in READY_FOR_REVIEW"):
            iso_store.record_deployment("project-0002", {
                "deployment_id": "dep-after-changes",
                "build_id": "build-2026-10-13T012227z",
                "validation_id": "validation-2026-10-13T012227z",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://pitstop-lagos-preview.vercel.app",
                "metadata": {"project_name": "pitstop-lagos-preview"},
            })

    def test_phase33_local_pipeline_worker_review_state_machine_and_operator_workflow(self):
        """Phase 33: LocalPipelineWorker end-to-end execution, 7-state review machine, and operator CLI workflow."""
        from orchestration.workers import LocalPipelineWorker

        candidate_path, evidence_path = self._fictional_candidates_and_evidence()
        worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output-p33")

        # 1. Run SCOUT -> RESEARCH -> QUALIFY -> BUILD -> VALIDATE purely via JobService + LocalPipelineWorker
        j_scout = self.jobs.create("SCOUT", job_id="p33-scout", input={"source_file": str(candidate_path), "niche": "Restaurants"})
        r_scout = self.jobs.run("p33-scout", worker)
        self.assertEqual(r_scout["status"], "COMPLETED")
        self.assertEqual(r_scout["next_job_types"], ["RESEARCH"])

        j_res = self.jobs.create_next("p33-scout", next_job_id="p33-research", input={"evidence_file": str(evidence_path)})
        r_res = self.jobs.run("p33-research", worker)
        self.assertEqual(r_res["status"], "COMPLETED")
        lead_id = r_res["output"]["lead_id"]

        j_qual = self.jobs.create_next("p33-research", next_job_id="p33-qualify")
        r_qual = self.jobs.run("p33-qualify", worker)
        self.assertEqual(r_qual["status"], "COMPLETED")
        project_id = r_qual["output"]["project_id"]
        self.assertIsNotNone(project_id)

        j_build = self.jobs.create_next("p33-qualify", next_job_id="p33-build")
        r_build = self.jobs.run("p33-build", worker)
        self.assertEqual(r_build["status"], "COMPLETED")
        build_1 = r_build["output"]["build_id"]
        # After BUILD before VALIDATE, workflow_state is DRAFT_GENERATED
        self.assertEqual(self.store.snapshot(project_id)["workflow_state"], "DRAFT_GENERATED")

        j_val = self.jobs.create_next("p33-build", next_job_id="p33-validate")
        r_val = self.jobs.run("p33-validate", worker)
        self.assertEqual(r_val["status"], "COMPLETED")
        # After VALIDATE PASS, workflow_state is VALIDATION_PASSED_READY_FOR_REVIEW
        self.assertEqual(self.store.snapshot(project_id)["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")

        # 2. Human inspects local draft BEFORE deployment and requests changes (no deployment required)
        self.store.record_review(project_id, "CHANGES_REQUESTED", notes="Refine local draft before preview", build_id=build_1)
        self.assertEqual(self.store.snapshot(project_id)["workflow_state"], "CHANGES_REQUESTED")

        # Rebuild via CLI 'build' command without manual state.json edits
        out_buf = io.StringIO()
        rc_build = run_cli(
            ["--state-path", str(self.state_path), "build", project_id, "--output-root", str(self.root / "output-p33")],
            stdout=out_buf,
            stderr=io.StringIO(),
        )
        self.assertEqual(rc_build, 0)
        cli_build_out = json.loads(out_buf.getvalue())
        build_2 = cli_build_out["build_id"]
        self.assertNotEqual(build_1, build_2)
        self.assertEqual(cli_build_out["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")

        # 3. Approving the wrong build_id is rejected; approving build_2 for PREVIEW_DEPLOYMENT succeeds
        with self.assertRaisesRegex(ValueError, "does not match project's assigned_build_id"):
            TrackingStore(self.state_path).record_review(
                project_id,
                "APPROVED_FOR_PREVIEW",
                notes="Wrong build",
                build_id=build_1,
                action="PREVIEW_DEPLOYMENT",
            )
        tok_pre = confirmation_token(project_id, build_2, "APPROVED_FOR_PREVIEW", action="PREVIEW_DEPLOYMENT")
        rc_rev_pre = run_cli(
            [
                "--state-path",
                str(self.state_path),
                "review",
                project_id,
                "--decision",
                "APPROVED",
                "--action",
                "PREVIEW_DEPLOYMENT",
                "--build-id",
                build_2,
                "--notes",
                "Local draft approved for preview deployment",
                "--confirm",
                tok_pre,
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(rc_rev_pre, 0)
        snap_pre = TrackingStore(self.state_path).snapshot(project_id)
        self.assertEqual(snap_pre["workflow_state"], "APPROVED_FOR_PREVIEW_DEPLOYMENT")
        self.assertEqual(snap_pre["project"]["approved_build_id"], build_2)
        self.assertEqual(snap_pre["project"]["approved_action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(snap_pre["next_action"], "Deploy isolated preview")
        # Pre-deployment approval does NOT unlock outreach preparation before preview is deployed & reviewed
        self.assertEqual(OutreachService(TrackingStore(self.state_path)).readiness(project_id), "NO_PREVIEW")

        # 4. Recording the deployed preview transitions to ISOLATED_PREVIEW_DEPLOYED and resets review_status to PENDING
        reloaded = TrackingStore(self.state_path)
        reloaded.record_deployment(project_id, {
            "deployment_id": "dep-p33-001",
            "build_id": build_2,
            "validation_id": snap_pre["latestValidation"]["validation_id"],
            "validation_status": "PASS",
            "deployment_status": "DEPLOYED",
            "deployment_url": "https://ai-website-agent-preview.vercel.app",
            "metadata": {"project_name": "ai-website-agent-preview"},
        })
        snap_dep = reloaded.snapshot(project_id)
        self.assertEqual(snap_dep["workflow_state"], "ISOLATED_PREVIEW_DEPLOYED")
        self.assertEqual(snap_dep["project"]["review_status"], "PENDING")
        self.assertEqual(snap_dep["project"]["pre_deployment_review_status"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(OutreachService(reloaded).readiness(project_id), "AWAITING_HUMAN_REVIEW")

        # 5. Subsequent human review of the deployed preview approves build_2 for OUTREACH_PREPARATION
        reloaded.record_review(
            project_id,
            "APPROVED",
            notes="Live preview verified and approved for outreach preparation",
            build_id=build_2,
            action="OUTREACH_PREPARATION",
        )
        snap_final = reloaded.snapshot(project_id)
        self.assertEqual(snap_final["workflow_state"], "PREVIEW_APPROVED_READY_FOR_OUTREACH")
        self.assertEqual(snap_final["project"]["approved_build_id"], build_2)
        self.assertEqual(snap_final["project"]["approved_action"], "OUTREACH_PREPARATION")
        self.assertEqual(OutreachService(reloaded).readiness(project_id), "READY")

        # 6. Verify assess_opportunity recommends salon-service-v1 for non-restaurant categories and ingest_research_record never copies unverified real candidate contact fields
        salon_assessment = assess_opportunity({
            "businessName": "Fictional Salon",
            "niche": "Salon",
            "location": {"city": "Lagos", "country": "Nigeria"},
            "sourceUrls": ["https://fixture.example.invalid/s1", "https://fixture.example.invalid/s2"],
            "websiteStatus": "outdated",
            "dataClassification": "TEST_FIXTURE",
        })
        self.assertEqual(salon_assessment["content_brief"]["recommended_template"], "salon-service-v1")

        unverified_real_rec = {
            "data_classification": "REAL_PUBLIC_DATA",
            "candidate": {
                "businessName": "Unverified Contact Cafe",
                "niche": "Restaurant",
                "location": {"city": "Lagos", "country": "Nigeria", "address": "99 Unverified Street"},
                "contact": {"phone": "+2348009998888", "email": "unverified@example.org"},
                "social": {"instagramUrl": "https://www.instagram.com/unverified/"},
                "sourceUrls": ["https://example.org/listing"],
                "dataClassification": "REAL_PUBLIC_DATA",
            },
            "research": {
                "qualificationStatus": "NEEDS_REVIEW",
                "verifiedFacts": [
                    {"field": "businessName", "value": "Unverified Contact Cafe", "sourceUrls": ["https://example.org/listing"]},
                ],
                "conflictingFacts": [],
                "researchSources": ["https://example.org/listing"],
            },
        }
        real_lead = reloaded.ingest_research_record(unverified_real_rec)
        self.assertIsNone(real_lead["address"])
        self.assertIsNone(real_lead["phone"])
        self.assertIsNone(real_lead["email"])
        self.assertIsNone(real_lead["instagram_url"])

    def test_phase34_operational_verification_review_cli_11_step_workflow_and_worker_safeguards(self):
        """Phase 34: review-CLI conventions, 11-step human review workflow, loopback serving, and worker gates."""
        import socket
        import threading
        import urllib.request
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from functools import partial
        from builder.cli import validate_serve_target
        from orchestration.workers import LocalPipelineWorker

        initial_vercel_exists = Path(".vercel").exists()

        # --- Step 2 & Step 3: Isolated temporary REAL_PUBLIC_DATA 11-step workflow ---
        p34_state = self.root / "state-p34.json"
        p34_out_root = self.root / "output-p34"
        p34_record_file = self.root / "temp_pitstop_record.json"
        p34_record_file.write_text(
            Path("data/leads/pitstop_lagos_qualified_lead.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        def _cli(args_list: list[str]) -> tuple[int, dict, dict]:
            out_s, err_s = io.StringIO(), io.StringIO()
            rc = run_cli(["--state-path", str(p34_state), *args_list], stdout=out_s, stderr=err_s)
            out_val = json.loads(out_s.getvalue()) if out_s.getvalue().strip().startswith("{") else {}
            err_val = json.loads(err_s.getvalue().splitlines()[-1]) if err_s.getvalue().strip().endswith("}") else {}
            return rc, out_val, err_val

        # Step 3.1: Create isolated temporary project via ingest-lead --create-project
        rc_ing, ing_out, _ = _cli(["ingest-lead", "--record", str(p34_record_file), "--create-project"])
        self.assertEqual(rc_ing, 0)
        proj_id = ing_out["project_id"]
        lead_id = ing_out["lead_id"]
        self.assertEqual(ing_out["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(ing_out["qualification_status"], "QUALIFIED")

        # Step 3.2 & 3.3: Generate and validate Build 1
        rc_b1, b1_out, _ = _cli(["build", proj_id, "--output-root", str(p34_out_root)])
        self.assertEqual(rc_b1, 0)
        build_1 = b1_out["build_id"]
        val_1 = b1_out["validation_id"]
        self.assertEqual(b1_out["validation_status"], "PASS")
        self.assertEqual(b1_out["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")

        # Step 3.4: Export and verify review package for Build 1
        pkg1_dir = self.root / "review-pkg-b1"
        rc_pkg1, pkg1_out, _ = _cli(["review-package", proj_id, "--export-dir", str(pkg1_dir)])
        self.assertEqual(rc_pkg1, 0)
        self.assertEqual(pkg1_out["build_id"], build_1)
        rc_ver1, ver1_out, _ = _cli(["review-package", proj_id, "--verify-dir", str(pkg1_dir)])
        self.assertEqual(rc_ver1, 0)
        self.assertEqual(ver1_out["status"], "VALID")
        self.assertTrue(ver1_out["hash_matches"])

        # Step 3.5: Inspect local draft and request changes BEFORE any deployment
        tok_chg = confirmation_token(proj_id, build_1, "CHANGES_REQUESTED")
        rc_chg, chg_out, _ = _cli([
            "review", proj_id,
            "--decision", "CHANGES_REQUESTED",
            "--build-id", build_1,
            "--notes", "Adjust spacing in local draft before any preview deployment",
            "--confirm", tok_chg,
        ])
        self.assertEqual(rc_chg, 0)
        self.assertEqual(chg_out["current_stage"], "BUILDING")
        self.assertEqual(chg_out["workflow_state"], "CHANGES_REQUESTED")
        self.assertEqual(chg_out["review_status"], "CHANGES_REQUESTED")

        # Step 3.6: Confirm deployment is blocked after CHANGES_REQUESTED
        _cli(["configure-preview", proj_id, "--project-name", "temp-pitstop-preview"])
        rc_prep_blocked, prep_blocked_out, _ = _cli([
            "prepare-preview", proj_id,
            "--approve-real-preview",
            "--project-name", "temp-pitstop-preview",
            "--repo-root", str(self.root),
        ])
        self.assertEqual(rc_prep_blocked, 1)
        self.assertEqual(prep_blocked_out["status"], "BLOCKED")
        with self.assertRaisesRegex(ValueError, "Project is not in READY_FOR_REVIEW"):
            TrackingStore(p34_state).record_deployment(proj_id, {
                "deployment_id": "dep-blocked-b1",
                "build_id": build_1,
                "validation_id": val_1,
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://temp-pitstop-preview.vercel.app",
                "metadata": {"project_name": "temp-pitstop-preview"},
            })

        # Step 3.7: Generate and validate Build 2; confirm Build 1 review package is now rejected as stale
        rc_b2, b2_out, _ = _cli(["build", proj_id, "--output-root", str(p34_out_root)])
        self.assertEqual(rc_b2, 0)
        build_2 = b2_out["build_id"]
        self.assertNotEqual(build_1, build_2)
        self.assertEqual(b2_out["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")

        rc_ver_stale, ver_stale_out, _ = _cli(["review-package", proj_id, "--verify-dir", str(pkg1_dir)])
        self.assertEqual(rc_ver_stale, 1)
        self.assertEqual(ver_stale_out["status"], "INVALID")
        self.assertIn("STALE_EXPORT_BUILD_ID", {i["code"] for i in ver_stale_out["issues"]})
        self.assertIn("STALE_EXPORT_VALIDATION_ID", {i["code"] for i in ver_stale_out["issues"]})

        # Modifying exported index.html is also caught by both validate_serve_target and verify-dir
        (pkg1_dir / "index.html").write_text(
            (pkg1_dir / "index.html").read_text(encoding="utf-8") + "<!-- modified -->",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Review package integrity error"):
            validate_serve_target(pkg1_dir, host="127.0.0.1")
        rc_ver_mod, ver_mod_out, _ = _cli(["review-package", proj_id, "--verify-dir", str(pkg1_dir)])
        self.assertEqual(rc_ver_mod, 1)
        self.assertIn("PACKAGE_HASH_MISMATCH", {i["code"] for i in ver_mod_out["issues"]})

        # Step 2 verification: test both direct '--decision APPROVED_FOR_PREVIEW' and alias '--decision APPROVED --action PREVIEW_DEPLOYMENT'
        # Also verify that deployment BEFORE pre-deployment human approval is blocked for REAL_PUBLIC_DATA
        rc_prep_unapproved, prep_unapproved_out, _ = _cli([
            "prepare-preview", proj_id,
            "--approve-real-preview",
            "--project-name", "temp-pitstop-preview",
            "--repo-root", str(self.root),
        ])
        self.assertEqual(rc_prep_unapproved, 1)
        self.assertEqual(prep_unapproved_out["status"], "BLOCKED")
        self.assertIn("APPROVED_FOR_PREVIEW", prep_unapproved_out["error"])

        tok_b2_direct = confirmation_token(proj_id, build_2, "APPROVED_FOR_PREVIEW")
        rc_rev_b2, rev_b2_out, _ = _cli([
            "review", proj_id,
            "--decision", "APPROVED_FOR_PREVIEW",
            "--build-id", build_2,
            "--notes", "Approve Build 2 directly via APPROVED_FOR_PREVIEW",
            "--confirm", tok_b2_direct,
        ])
        self.assertEqual(rc_rev_b2, 0)
        self.assertEqual(rev_b2_out["review_status"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(rev_b2_out["approved_build_id"], build_2)
        self.assertEqual(rev_b2_out["approved_action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(rev_b2_out["workflow_state"], "APPROVED_FOR_PREVIEW_DEPLOYMENT")

        # Step 3.8: Rebuild to Build 3 and confirm Build 2's prior approval is invalidated and cannot authorize Build 3
        rc_b3, b3_out, _ = _cli(["build", proj_id, "--output-root", str(p34_out_root)])
        self.assertEqual(rc_b3, 0)
        build_3 = b3_out["build_id"]
        self.assertNotEqual(build_2, build_3)
        snap_b3 = TrackingStore(p34_state).snapshot(proj_id)
        self.assertEqual(snap_b3["project"]["review_status"], "PENDING")
        self.assertIsNone(snap_b3["project"]["approved_build_id"])
        self.assertIsNone(snap_b3["project"]["approved_action"])

        # Attempting to deploy Build 3 without approving Build 3 is blocked
        rc_prep_b3_unapproved, prep_b3_unapproved, _ = _cli([
            "prepare-preview", proj_id,
            "--approve-real-preview",
            "--project-name", "temp-pitstop-preview",
            "--repo-root", str(self.root),
        ])
        self.assertEqual(rc_prep_b3_unapproved, 1)
        self.assertEqual(prep_b3_unapproved["status"], "BLOCKED")
        with self.assertRaisesRegex(ValueError, "requires explicit pre-deployment human review approval"):
            TrackingStore(p34_state).record_deployment(proj_id, {
                "deployment_id": "dep-unapproved-b3",
                "build_id": build_3,
                "validation_id": b3_out["validation_id"],
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://temp-pitstop-preview.vercel.app",
                "metadata": {"project_name": "temp-pitstop-preview"},
            })

        # Attempting to approve superseded Build 2 fails closed
        rc_wrong_b, _, err_wrong_b = _cli([
            "review", proj_id,
            "--decision", "APPROVED_FOR_PREVIEW",
            "--build-id", build_2,
            "--notes", "Attempt to approve stale build_2",
            "--confirm", f"{proj_id}:{build_2}:APPROVED_FOR_PREVIEW:PREVIEW_DEPLOYMENT",
        ])
        self.assertEqual(rc_wrong_b, 1)
        self.assertIn("does not match project's assigned_build_id", err_wrong_b["error"])

        # Step 3.9: Approve exact new Build 3 for PREVIEW_DEPLOYMENT using alias '--decision APPROVED --action PREVIEW_DEPLOYMENT'
        rc_rev_b3, rev_b3_out, _ = _cli([
            "review", proj_id,
            "--decision", "APPROVED",
            "--action", "PREVIEW_DEPLOYMENT",
            "--build-id", build_3,
            "--notes", "Approve Build 3 for isolated preview deployment",
            "--confirm", f"{proj_id}:{build_3}:APPROVED:PREVIEW_DEPLOYMENT",
        ])
        self.assertEqual(rc_rev_b3, 0)
        self.assertEqual(rev_b3_out["review_status"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(rev_b3_out["approved_build_id"], build_3)
        self.assertEqual(rev_b3_out["approved_action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(rev_b3_out["workflow_state"], "APPROVED_FOR_PREVIEW_DEPLOYMENT")

        # Step 3.10: Confirm workflow is ready for authorized isolated preview deployment in dry-run without deploying
        vercel_dir = self.root / ".vercel"
        vercel_dir.mkdir(exist_ok=True)
        (vercel_dir / "project.json").write_text(
            json.dumps({"projectId": "prj_tempisolated", "orgId": "team_temp", "projectName": "temp-pitstop-preview"}),
            encoding="utf-8",
        )
        rc_prep_ok, prep_ok_out, _ = _cli([
            "prepare-preview", proj_id,
            "--approve-real-preview",
            "--project-name", "temp-pitstop-preview",
            "--repo-root", str(self.root),
        ])
        self.assertEqual(rc_prep_ok, 0)
        self.assertEqual(prep_ok_out["status"], "COMPLETED")
        self.assertEqual(prep_ok_out["output"]["deployment_status"], "PENDING")
        self.assertIsNone(TrackingStore(p34_state).snapshot(proj_id)["latestDeployment"])

        # Step 3.11: Confirm outreach remains blocked until post-deployment review requirements are satisfied
        rc_out_blocked1, _, err_out_blocked1 = _cli(["outreach", proj_id])
        self.assertEqual(rc_out_blocked1, 1)
        self.assertIn("NO_PREVIEW", err_out_blocked1["error"])
        p34_jobs = JobService(TrackingStore(p34_state))
        j_out_blocked = p34_jobs.create("PREPARE_OUTREACH", job_id="j-out-blocked-1", lead_id=lead_id, project_id=proj_id, build_id=build_3)
        self.assertEqual(p34_jobs.run("j-out-blocked-1", LocalPipelineWorker(repository_root=self.root, output_root=p34_out_root))["status"], "BLOCKED")

        # The local .vercel link alone is not proof of a safe per-business target.
        # Persist and bind the isolated provider identity before recording the deployment.
        s_dep = TrackingStore(p34_state)
        s_dep.begin_vercel_project_provisioning(
            proj_id, "temp-pitstop-preview", base_name="temp-pitstop-preview"
        )
        s_dep.bind_vercel_project(
            proj_id, vercel_project_id="prj_tempisolated", project_name="temp-pitstop-preview"
        )
        # Even after recording isolated preview deployment, outreach remains blocked until post-deployment APPROVED review
        s_dep.record_deployment(proj_id, {
            "deployment_id": "dep-b3-001",
            "build_id": build_3,
            "validation_id": b3_out["validation_id"],
            "validation_status": "PASS",
            "deployment_status": "DEPLOYED",
            "deployment_url": "https://temp-pitstop-preview.vercel.app",
            "vercel_project_id": "prj_tempisolated",
            "metadata": {"project_name": "temp-pitstop-preview"},
        })
        self.assertEqual(s_dep.snapshot(proj_id)["workflow_state"], "ISOLATED_PREVIEW_DEPLOYED")
        self.assertEqual(s_dep.snapshot(proj_id)["project"]["review_status"], "PENDING")
        rc_out_blocked2, _, err_out_blocked2 = _cli(["outreach", proj_id])
        self.assertEqual(rc_out_blocked2, 1)
        self.assertIn("AWAITING_HUMAN_REVIEW", err_out_blocked2["error"])

        # Post-deployment approval of exact Build 3 for OUTREACH_PREPARATION unlocks outreach draft preparation
        tok_post = confirmation_token(proj_id, build_3, "APPROVED", action="OUTREACH_PREPARATION")
        rc_rev_post, rev_post_out, _ = _cli([
            "review", proj_id,
            "--decision", "APPROVED",
            "--action", "OUTREACH_PREPARATION",
            "--build-id", build_3,
            "--notes", "Deployed isolated preview inspected and approved for outreach preparation",
            "--confirm", tok_post,
        ])
        self.assertEqual(rc_rev_post, 0)
        self.assertEqual(rev_post_out["workflow_state"], "PREVIEW_APPROVED_READY_FOR_OUTREACH")
        rc_out_ok, out_ok_payload, _ = _cli(["outreach", proj_id])
        self.assertEqual(rc_out_ok, 0)
        self.assertEqual(out_ok_payload["build_id"], build_3)
        self.assertEqual(out_ok_payload["deployment_id"], "dep-b3-001")
        self.assertEqual(out_ok_payload["status"], "AWAITING_REVIEW")

        # --- Step 4: Verify loopback HTTP serving of exported review package on 127.0.0.1 ---
        target_serve_dir = validate_serve_target(Path("preview-export/lead-0002-pitstop-lagos"), host="127.0.0.1")
        handler = partial(SimpleHTTPRequestHandler, directory=str(target_serve_dir))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                served_bytes = resp.read()
                self.assertEqual(resp.status, 200)
        finally:
            httpd.shutdown()
            httpd.server_close()
            server_thread.join(timeout=2)
        import hashlib
        self.assertEqual(
            hashlib.sha256(served_bytes).hexdigest(),
            "6d58ba2594fb3f92713aef3671646c65c015d2041f93dc4e6bd5282f5753e99b",
        )

        # --- Step 6: Verify LocalPipelineWorker idempotency on retry/re-run and structured failure handling ---
        w_state = self.root / "state-worker.json"
        w_store = TrackingStore(w_state)
        w_jobs = JobService(w_store)
        w_worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output-worker")
        cand_path, evid_path = self._fictional_candidates_and_evidence()

        # Re-running SCOUT and RESEARCH on the same candidate does not create duplicate leads or duplicate projects
        w_jobs.create("SCOUT", job_id="w-scout-1", input={"source_file": str(cand_path), "niche": "Restaurants"})
        w_jobs.run("w-scout-1", w_worker)
        w_jobs.create("SCOUT", job_id="w-scout-2", input={"source_file": str(cand_path), "niche": "Restaurants"})
        w_jobs.run("w-scout-2", w_worker)
        self.assertEqual(len(w_store.state["leads"]), 1)

        only_lead_id = next(iter(w_store.state["leads"]))
        w_jobs.create("RESEARCH", job_id="w-res-1", lead_id=only_lead_id, input={"evidence_file": str(evid_path)})
        w_jobs.run("w-res-1", w_worker)
        w_jobs.create("RESEARCH", job_id="w-res-2", lead_id=only_lead_id, input={"evidence_file": str(evid_path)})
        w_jobs.run("w-res-2", w_worker)
        self.assertEqual(len(w_store.state["leads"]), 1)

        w_jobs.create("QUALIFY", job_id="w-qual-1", lead_id=only_lead_id)
        w_jobs.run("w-qual-1", w_worker)
        w_jobs.create("QUALIFY", job_id="w-qual-2", lead_id=only_lead_id)
        w_jobs.run("w-qual-2", w_worker)
        self.assertEqual(len(w_store.state["projects"]), 1)

        # Missing inputs record clear FAILED states rather than raising unhandled exceptions
        w_jobs.create("SCOUT", job_id="w-scout-bad", input={})
        self.assertEqual(w_jobs.run("w-scout-bad", w_worker)["status"], "FAILED")
        w_jobs.create("RESEARCH", job_id="w-res-bad", lead_id=only_lead_id, input={})
        self.assertEqual(w_jobs.run("w-res-bad", w_worker)["status"], "FAILED")

        # --- Step 5: Confirm workspace project-0002 remains completely untouched at PENDING ---
        ws_snap = TrackingStore().snapshot("project-0002")
        self.assertEqual(ws_snap["project"]["assigned_build_id"], "build-2026-10-13T012227z")
        self.assertEqual(ws_snap["latestValidation"]["validation_id"], "validation-2026-10-13T012227z")
        self.assertEqual(ws_snap["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(ws_snap["workflow_state"], "VALIDATION_PASSED_READY_FOR_REVIEW")
        self.assertEqual(ws_snap["project"]["review_status"], "PENDING")
        self.assertIsNone(ws_snap["latestDeployment"])
        if not initial_vercel_exists:
            self.assertFalse(Path(".vercel").exists())


if __name__ == "__main__":
    unittest.main()
