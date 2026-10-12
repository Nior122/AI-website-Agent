"""Phase 22 inspection, repeatability, and preview deployment readiness tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from builder.cli import serve_build
from builder.website_builder import WebsiteBuilder
from deployment.deployer import VercelDeploymentProvider
from opportunities.service import assess_opportunity
from templates.registry import select_template
from tracking.store import TrackingStore
from validation.quality_validator import validate_rendered_html


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "fictional_test_business_lagos_fresh_kitchen.json"
REAL_PHASE20_AUDIT = Path(__file__).parents[1] / "data" / "leads" / "phase20-calabar-kitchen-website-verification-20261011.json"
REAL_PHASE19_AUDIT = Path(__file__).parents[1] / "data" / "leads" / "phase19-lagos-restaurants-backlog-review-20261010.json"


class Phase22InspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture_data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_serve_build_validates_index_html_existence(self) -> None:
        """Confirms serve_build fails closed when directory or index.html is missing."""
        empty_dir = self.root / "empty"
        empty_dir.mkdir(parents=True)
        with self.assertRaises(ValueError) as ctx:
            serve_build(empty_dir)
        self.assertIn("does not contain an index.html file", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            serve_build(self.root / "nonexistent")
        self.assertIn("does not contain an index.html file", str(ctx.exception))

    def test_repeatability_and_preservation_across_two_runs(self) -> None:
        """Confirms running the demonstration twice produces valid builds and preserves historical records."""
        store_path = self.root / "tracking_state.json"
        store = TrackingStore(store_path)
        output_root = self.root / "output"
        cand = self.fixture_data["candidate"]

        # --- RUN 1 ---
        lead1 = store.create_lead({
            "business_name": cand["businessName"],
            "business_type": cand["niche"],
            "city": cand["location"]["city"],
            "country": cand["location"]["country"],
            "qualification_status": "QUALIFIED",
            "website_status": "WEBSITE_OUTDATED",
            "source_urls": cand["sourceUrls"],
            "phone": cand["contact"]["phone"],
            "whatsapp": cand["contact"]["whatsapp"],
            "data_classification": "TEST_FIXTURE",
        })
        opp1 = assess_opportunity(cand, lead_id=lead1["lead_id"])
        store.record_qualification(opp1)
        project1 = store.create_project(lead1["lead_id"])

        builder = WebsiteBuilder(output_root=output_root)
        lead_input1 = copy.deepcopy(self.fixture_data)
        lead_input1["lead_id"] = lead1["lead_id"]
        build1 = builder.build(lead_input1)
        self.assertEqual(build1.status, "COMPLETED")

        store.record_build(project1["project_id"], build1.to_dict())
        html1 = Path(build1.output_location).read_text(encoding="utf-8")
        val1 = validate_rendered_html(
            html1,
            cand["businessName"],
            real_build=True,
            build_id=build1.build_id,
            lead_id=lead1["lead_id"],
            expected_contact={"phone": cand["contact"]["phone"], "whatsapp": cand["contact"]["whatsapp"]}, synthetic_contact=True,
        )
        self.assertEqual(val1.status, "PASS")
        store.record_validation(project1["project_id"], val1.to_dict())

        deployer = VercelDeploymentProvider(self.root)
        dep1 = deployer.deploy(Path(build1.output_location).parent, dry_run=True)
        self.assertEqual(dep1.deployment_status, "FAILED")
        self.assertIsNone(dep1.deployment_url)

        store.record_review(project1["project_id"], "PENDING", notes="Run 1 review pending")
        snap1 = store.snapshot(project1["project_id"])
        self.assertEqual(snap1["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap1["project"]["review_status"], "PENDING")

        # --- RUN 2 (Rerun with duplicate candidate) ---
        lead_payload2 = {
            "business_name": cand["businessName"],
            "phone": cand["contact"]["phone"],
            "city": cand["location"]["city"],
            "country": cand["location"]["country"],
        }
        dup_check = store.find_duplicate(lead_payload2)
        self.assertEqual(dup_check["match"], "EXACT_MATCH")
        self.assertEqual(dup_check["lead_id"], lead1["lead_id"])

        # Reusing existing lead and active project
        lead2_id = dup_check["lead_id"]
        project2 = store.create_project(lead2_id)
        self.assertEqual(project2["project_id"], project1["project_id"])

        # Transition to BUILDING for the rerun build
        store.transition(project2["project_id"], "BUILDING")

        # Execute second build
        lead_input2 = copy.deepcopy(self.fixture_data)
        lead_input2["lead_id"] = lead2_id
        build2 = builder.build(lead_input2)
        self.assertEqual(build2.status, "COMPLETED")
        self.assertNotEqual(build1.build_id, build2.build_id)

        store.record_build(project2["project_id"], build2.to_dict())
        html2 = Path(build2.output_location).read_text(encoding="utf-8")
        val2 = validate_rendered_html(
            html2,
            cand["businessName"],
            real_build=True,
            build_id=build2.build_id,
            lead_id=lead2_id,
            expected_contact={"phone": cand["contact"]["phone"], "whatsapp": cand["contact"]["whatsapp"]}, synthetic_contact=True,
        )
        self.assertEqual(val2.status, "PASS")
        store.record_validation(project2["project_id"], val2.to_dict())

        dep2 = deployer.deploy(Path(build2.output_location).parent, dry_run=True)
        self.assertEqual(dep2.deployment_status, "FAILED")
        self.assertIsNone(dep2.deployment_url)

        store.record_review(project2["project_id"], "PENDING", notes="Run 2 review pending")
        snap2 = store.snapshot(project2["project_id"])

        # Verification of state preservation and integrity
        self.assertEqual(snap2["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap2["project"]["review_status"], "PENDING")
        self.assertEqual(snap2["project"]["assigned_build_id"], build2.build_id)

        # Both builds exist on disk
        self.assertTrue(Path(build1.output_location).is_file())
        self.assertTrue(Path(build2.output_location).is_file())
        self.assertNotEqual(build1.output_location, build2.output_location)

        # Both builds recorded in tracking store
        all_build_ids = [b["build_id"] for b in store.state["builds"].values() if b.get("project_id") == project1["project_id"]]
        self.assertIn(build1.build_id, all_build_ids)
        self.assertIn(build2.build_id, all_build_ids)
        self.assertEqual(len(all_build_ids), 2)

        # Both validations recorded in tracking store
        all_val_ids = [v["validation_id"] for v in store.state["validations"].values() if v.get("project_id") == project1["project_id"]]
        self.assertIn(val1.validation_id, all_val_ids)
        self.assertIn(val2.validation_id, all_val_ids)
        self.assertEqual(len(all_val_ids), 2)

        # Zero deployment records created in store
        self.assertEqual(len(store.state["deployments"]), 0)

    def test_artifact_integrity_and_link_resolution(self) -> None:
        """Verifies HTML structure, anchor link resolution, landmarks, and styling."""
        output_root = self.root / "output"
        builder = WebsiteBuilder(output_root=output_root)
        result = builder.build(copy.deepcopy(self.fixture_data))
        self.assertEqual(result.status, "COMPLETED")

        html_path = Path(result.output_location)
        html = html_path.read_text(encoding="utf-8")
        manifest_path = html_path.parent / "build.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(Path(manifest["output_location"]).resolve(), html_path.resolve())

        # Verify semantic landmarks
        self.assertIn("<header", html)
        self.assertIn("<main", html)
        self.assertIn("<footer", html)
        self.assertIn("<h1", html)

        # Verify internal anchor destinations exist
        expected_anchors = ["about", "menu", "location", "hours", "contact"]
        for anchor in expected_anchors:
            self.assertIn(f'href="#{anchor}"', html)
            self.assertIn(f'id="{anchor}"', html)
        self.assertIn('href="#top"', html)
        self.assertIn('id="top"', html)

        # Verify responsive styles
        self.assertIn("@media (max-width:", html)
        self.assertIn('name="viewport"', html)

        # Fictional contacts must not create active real-world destinations.
        self.assertNotIn("tel:+234 800 000 0099", html)
        self.assertNotIn("https://wa.me/2348000000099", html)
        self.assertNotIn("instagram.example.invalid", html)
        self.assertIn("Fictional demonstration website", html)
        self.assertIn("hero-art", html)
        self.assertIn("Lagos Jollof Plate", html)

    def test_fictional_data_isolation_no_real_business_leakage(self) -> None:
        """Verifies no real candidate keywords leak into output and historical records are unmodified."""
        output_root = self.root / "output"
        builder = WebsiteBuilder(output_root=output_root)
        result = builder.build(copy.deepcopy(self.fixture_data))
        html = Path(result.output_location).read_text(encoding="utf-8")

        real_keywords = ["cactus", "calabar", "oluwaleyimu", "ozumba", "samik", "allen avenue"]
        for keyword in real_keywords:
            self.assertNotIn(keyword, html.casefold())

        # Confirm historical audits exist and are unmodified
        self.assertTrue(REAL_PHASE20_AUDIT.is_file())
        self.assertTrue(REAL_PHASE19_AUDIT.is_file())
        phase20 = json.loads(REAL_PHASE20_AUDIT.read_text(encoding="utf-8"))
        self.assertEqual(phase20["resulting_state"]["qualification"], "NOT_YET_QUALIFIED")
