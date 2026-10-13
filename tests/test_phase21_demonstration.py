"""Phase 21 end-to-end builder demonstration tests and safety verifications."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from builder.website_builder import WebsiteBuilder
from deployment.deployer import VercelDeploymentProvider
from opportunities.service import assess_opportunity
from templates.registry import select_template
from tracking.store import TrackingStore
from validation.quality_validator import validate_rendered_html


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "fictional_test_business_lagos_fresh_kitchen.json"
REAL_PHASE20_AUDIT = Path(__file__).parents[1] / "data" / "leads" / "phase20-calabar-kitchen-website-verification-20261011.json"
REAL_PHASE19_AUDIT = Path(__file__).parents[1] / "data" / "leads" / "phase19-lagos-restaurants-backlog-review-20261010.json"


class Phase21DemonstrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TrackingStore(self.root / "state.json")
        self.fixture_data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_end_to_end_fictional_demonstration_workflow(self) -> None:
        """Demonstrates: Test Business -> Content Brief -> Template Selection -> Generation -> Validation -> Deployment Check -> Human Review Record."""
        candidate = self.fixture_data["candidate"]

        # Step 1: Structured Content Brief via assess_opportunity
        opportunity = assess_opportunity(candidate, lead_id=self.fixture_data["lead_id"])
        self.assertEqual(opportunity["status"], "QUALIFIED")
        self.assertEqual(opportunity["data_classification"], "TEST_FIXTURE")
        self.assertEqual(opportunity["website_status"], "WEBSITE_OUTDATED")
        brief = opportunity["content_brief"]
        self.assertEqual(brief["recommended_template"], "restaurant-menu-v1")
        self.assertTrue(len(brief["verified"]) >= 4)

        # Step 2: Website Template Selection
        template = select_template(candidate["niche"])
        self.assertEqual(template.template_id, "restaurant-menu-v1")
        self.assertEqual(template.name, "Restaurant Menu")

        # Step 3: Website Generation via existing WebsiteBuilder
        output_root = self.root / "output"
        builder = WebsiteBuilder(output_root=output_root)
        build_result = builder.build(copy.deepcopy(self.fixture_data))

        self.assertEqual(build_result.status, "COMPLETED")
        self.assertEqual(build_result.template_id, "restaurant-menu-v1")
        self.assertIsNotNone(build_result.output_location)
        output_file = Path(build_result.output_location)
        self.assertTrue(output_file.is_file())
        html = output_file.read_text(encoding="utf-8")
        self.assertIn("Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS", html)
        self.assertIn("Lagos Jollof Plate", html)

        # Step 4: Automated Validation
        validation = validate_rendered_html(
            html,
            "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS",
            real_build=True,
            build_id=build_result.build_id,
            lead_id=build_result.lead_id,
            expected_contact={"phone": candidate["contact"]["phone"], "whatsapp": candidate["contact"]["whatsapp"]}, synthetic_contact=True,
        )
        self.assertEqual(validation.status, "PASS")
        self.assertEqual(validation.score, 100)
        self.assertTrue(validation.checks["build_integrity"])
        self.assertTrue(validation.checks["business_name"])
        self.assertTrue(validation.checks["mobile_support"])
        self.assertTrue(validation.checks["links"])
        self.assertTrue(validation.checks["placeholder_scan"])
        self.assertTrue(validation.checks["secret_scan"])

        # Step 5: Safe Preview Deployment Check (Unlinked project fails closed)
        deployer = VercelDeploymentProvider(self.root)
        deployment = deployer.deploy(output_file.parent, dry_run=True)
        self.assertEqual(deployment.deployment_status, "FAILED")
        self.assertIsNone(deployment.deployment_url)
        self.assertIn("No linked Vercel project found", deployment.error or "")

        # Step 6: Tracking and Human Review Record (Review remains PENDING, never automatically approved)
        lead = self.store.create_lead({
            "business_name": candidate["businessName"],
            "business_type": candidate["niche"],
            "city": candidate["location"]["city"],
            "country": candidate["location"]["country"],
            "qualification_status": "QUALIFIED",
            "website_status": "WEBSITE_OUTDATED",
            "source_urls": candidate["sourceUrls"],
            "phone": candidate["contact"]["phone"],
            "whatsapp": candidate["contact"]["whatsapp"],
            "data_classification": "TEST_FIXTURE",
        })
        self.assertEqual(lead["data_classification"], "TEST_FIXTURE")

        # Record qualification
        opp_record = copy.deepcopy(opportunity)
        opp_record["lead_id"] = lead["lead_id"]
        self.store.record_qualification(opp_record)

        # Project creation
        project = self.store.create_project(lead["lead_id"])
        self.assertEqual(project["current_stage"], "QUALIFIED")

        # Record build & validation
        tracked_build = build_result.to_dict()
        tracked_build["lead_id"] = lead["lead_id"]
        self.store.record_build(project["project_id"], tracked_build)
        self.store.record_validation(project["project_id"], validation.to_dict())

        # Human Review record
        self.store.record_review(
            project["project_id"],
            "PENDING",
            notes="Fictional test build for Lagos Fresh Kitchen — awaiting human inspection. Not auto-approved.",
        )
        snapshot = self.store.snapshot(project["project_id"])
        self.assertEqual(snapshot["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snapshot["project"]["review_status"], "PENDING")
        self.assertIsNone(snapshot["latestDeployment"])

    def test_fictional_data_isolation_and_no_real_business_leakage(self) -> None:
        """Confirms fictional fixture is labeled TEST_FIXTURE and contains no real-business URLs or claims."""
        self.assertEqual(self.fixture_data["data_classification"], "TEST_FIXTURE")
        self.assertEqual(self.fixture_data["candidate"]["dataClassification"], "TEST_FIXTURE")
        for url in self.fixture_data["candidate"]["sourceUrls"]:
            self.assertIn("example.invalid", url)
        self.assertIn("FICTIONAL TEST BUSINESS", self.fixture_data["candidate"]["businessName"])

    def test_failed_validation_blocks_deployment(self) -> None:
        """Confirms that a failed validation result blocks deployment."""
        build_dir = self.root / "broken_build"
        build_dir.mkdir(parents=True)
        (build_dir / "index.html").write_text("<!doctype html><html><head><title>Broken</title></head><body>Broken</body></html>", encoding="utf-8")
        manifest = {
            "build_id": "build-broken-001",
            "lead_id": "lead-broken",
            "output_location": str(build_dir / "index.html"),
            "validation": {"status": "FAIL"},
        }
        (build_dir / "build.json").write_text(json.dumps(manifest), encoding="utf-8")
        deployer = VercelDeploymentProvider(self.root)
        deployment = deployer.deploy(build_dir, dry_run=True)
        self.assertEqual(deployment.deployment_status, "FAILED")
        self.assertIn("blocked because validation status is FAIL", deployment.error or "")

    def test_human_approval_is_not_silently_granted(self) -> None:
        """Confirms human approval cannot be granted without deployed preview and explicit status."""
        lead = self.store.create_lead({
            "business_name": "Test Co",
            "qualification_status": "QUALIFIED",
            "city": "Lagos",
            "country": "Nigeria",
        })
        project = self.store.create_project(lead["lead_id"])
        with self.assertRaises(ValueError):
            # Cannot approve when stage is not PREVIEW_DEPLOYED
            self.store.record_review(project["project_id"], "APPROVED")
        # Review status defaults to PENDING
        self.assertEqual(project["review_status"], "PENDING")

    def test_duplicate_lead_prevention(self) -> None:
        """Confirms repeated lead creation with same phone or website raises duplicate error."""
        lead_data = {
            "business_name": "Unique Test",
            "phone": "+2348009999999",
            "city": "Lagos",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
        }
        self.store.create_lead(lead_data)
        with self.assertRaises(ValueError) as ctx:
            self.store.create_lead(lead_data)
        self.assertIn("Duplicate lead", str(ctx.exception))

    def test_historical_audit_records_remain_intact(self) -> None:
        """Confirms Phase 19 and Phase 20 audit records exist when present and preserve their non-qualified decisions without fabrication."""
        live_store = TrackingStore()
        for lead in live_store.state["leads"].values():
            self.assertNotIn(lead.get("business_name"), {"Calabar Kitchen", "Cactus Restaurant"})
        if REAL_PHASE20_AUDIT.is_file():
            phase20_data = json.loads(REAL_PHASE20_AUDIT.read_text(encoding="utf-8"))
            self.assertEqual(phase20_data["candidate"]["business_name"], "Calabar Kitchen")
            self.assertEqual(phase20_data["resulting_state"]["qualification"], "NOT_YET_QUALIFIED")
            self.assertFalse(phase20_data["records_created"]["lead"])
            self.assertFalse(phase20_data["records_created"]["project"])
        else:
            ws_check = live_store.check_local_state()
            self.assertIn(f"data/leads/{REAL_PHASE20_AUDIT.name}", ws_check["irrecoverable_gitignored_audit_files"])

        if REAL_PHASE19_AUDIT.is_file():
            phase19_data = json.loads(REAL_PHASE19_AUDIT.read_text(encoding="utf-8"))
            self.assertEqual(phase19_data["mode"], "REVIEW_ONLY")
        else:
            ws_check = live_store.check_local_state()
            self.assertIn(f"data/leads/{REAL_PHASE19_AUDIT.name}", ws_check["irrecoverable_gitignored_audit_files"])
