import json
import tempfile
import unittest
from pathlib import Path

from builder.website_builder import WebsiteBuilder
from tracking.store import TrackingStore
from validation.quality_validator import validate_rendered_html


def pitstop_lead_payload():
    return {
        "lead_id": "lead-0002",
        "demo": False,
        "data_classification": "REAL_PUBLIC_DATA",
        "candidate": {
            "candidate_id": "phase25-search-pitstop-lagos",
            "businessName": "Pitstop Lagos",
            "niche": "restaurant",
            "location": {
                "city": "Lagos",
                "country": "Nigeria",
                "address": "92A Younis Bashorun Street, Victoria Island, Lagos",
            },
            "contact": {
                "phone": "+234 807 749 8324",
                "email": "info@pitstoplagos.com",
            },
            "social": {
                "instagramUrl": "https://www.instagram.com/pitstoplagos/",
            },
            "publicBusinessInformation": {
                "description": "A wellness and cycling-inspired cafe and restaurant in Victoria Island, Lagos.",
            },
            "sourceUrls": [
                "https://pitstoplagos.com/",
                "https://www.instagram.com/pitstoplagos/",
            ],
            "websiteStatus": "poor",
            "dataClassification": "REAL_PUBLIC_DATA",
        },
        "research": {
            "qualificationStatus": "QUALIFIED",
            "verifiedFacts": [
                {"field": "businessName", "value": "Pitstop Lagos"},
                {"field": "location", "value": {"city": "Lagos", "country": "Nigeria", "address": "92A Younis Bashorun Street, Victoria Island, Lagos"}},
                {"field": "contact", "value": {"phone": "+234 807 749 8324", "email": "info@pitstoplagos.com"}},
                {"field": "social", "value": {"instagramUrl": "https://www.instagram.com/pitstoplagos/"}},
                {"field": "description", "value": "A wellness and cycling-inspired cafe and restaurant in Victoria Island, Lagos."},
                {"field": "services", "value": [
                    {"name": "Healthy-Inspired Cafe & Dining", "description": "Fresh meals and recovery snacks."},
                    {"name": "Cycling Community Rides", "description": "Weekend group rides."},
                    {"name": "Wattbike Training Studio", "description": "Dedicated indoor cycling training."},
                    {"name": "Bicycle Service & Repair", "description": "Maintenance and accessories."},
                ]},
                {"field": "menu_items", "value": []},
            ],
        },
        "approved_brand": {
            "sourceUrls": ["https://pitstoplagos.com/"],
            "values": {
                "primary_color": "#1a3a2a",
                "secondary_color": "#de5e28",
            },
        },
        "approved_assets": [],
    }


class Phase26RealDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_lead_identity_and_classification_preserved(self):
        lead = pitstop_lead_payload()
        self.assertEqual(lead["lead_id"], "lead-0002")
        self.assertEqual(lead["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(lead["candidate"]["businessName"], "Pitstop Lagos")
        self.assertFalse(lead["demo"])

    def test_draft_generated_only_for_qualified_lead(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        unqualified = pitstop_lead_payload()
        unqualified["research"]["qualificationStatus"] = "NOT_QUALIFIED"
        res = builder.build(unqualified)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Only research records with qualificationStatus QUALIFIED", res.errors[0])

    def test_fictional_and_real_business_data_remain_isolated(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        self.assertEqual(res.status, "COMPLETED")
        html = Path(res.output_location).read_text(encoding="utf-8")
        self.assertIn("Pitstop Lagos", html)
        self.assertIn("PL", html)  # Brand initials
        self.assertNotIn("LF", html)  # Lagos Fresh brand mark must not appear
        self.assertNotIn("Lagos table · fictional test kitchen", html)
        self.assertNotIn("Fictional demonstration website", html)
        self.assertIn("Uncommissioned preview draft prepared for internal evaluation", html)

    def test_unverified_facts_omitted_and_no_menu_items_invented(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        self.assertEqual(res.status, "COMPLETED")
        html = Path(res.output_location).read_text(encoding="utf-8")
        self.assertNotIn("#menu", html)  # No menu section when menu_items is empty
        self.assertNotIn("Signature dishes", html)
        self.assertNotIn("₦", html)  # No invented prices

    def test_suspicious_source_content_not_copied_into_page(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        html = Path(res.output_location).read_text(encoding="utf-8")
        self.assertNotIn("casino", html.casefold())
        self.assertNotIn("vc_row", html)
        self.assertNotIn("rodich_", html)
        self.assertNotIn("-9999px", html)

    def test_hidden_external_spam_anchors_and_shortcodes_rejected_by_validator(self):
        clean_html = "<html><header></header><main><h1>Title</h1><p>Text</p></main><footer></footer></html>"
        # Test spam keyword
        spam_html = "<html><header></header><main><h1>Title</h1><p>Visit our online casino roulette</p></main><footer></footer></html>"
        val = validate_rendered_html(spam_html, "Title", real_build=True)
        self.assertFalse(val.checks["claim_safety"])
        self.assertTrue(any(e.code == "SPAM_CONTENT_DETECTED" for e in val.findings))

        # Test unparsed shortcodes
        sc_html = "<html><header></header><main><h1>Title</h1><p>[vc_row][vc_column]</p></main><footer></footer></html>"
        val_sc = validate_rendered_html(sc_html, "Title", real_build=True)
        self.assertFalse(val_sc.checks["build_integrity"])
        self.assertTrue(any(e.code == "UNPARSED_SHORTCODE_DETECTED" for e in val_sc.findings))

        # Test hidden offscreen style
        offscreen_html = '<html><header></header><main><h1>Title</h1><div style="position:absolute;left:-9999px;">Spam</div></main><footer></footer></html>'
        val_off = validate_rendered_html(offscreen_html, "Title", real_build=True)
        self.assertFalse(val_off.checks["build_integrity"])
        self.assertTrue(any(e.code == "HIDDEN_OFFSCREEN_SPAM_DETECTED" for e in val_off.findings))

    def test_internal_links_resolve_to_valid_targets(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        self.assertTrue(res.validation["checks"]["links"])
        self.assertEqual(len(res.validation["errors"]), 0)

    def test_fictional_demo_artifacts_rejected_by_validator_in_real_build(self):
        base_html = "<html><header></header><main><h1>Pitstop Lagos</h1><p>Wellness cafe</p></main><footer></footer></html>"
        for leak in ("Lagos Fresh Kitchen", "The Lagos Kitchen", "Signature Rice Bowl", "Made for the table", "From the test kitchen", "₦0 demo price"):
            leaked_html = base_html.replace("<p>Wellness cafe</p>", f"<p>Wellness cafe - {leak}</p>")
            val = validate_rendered_html(leaked_html, "Pitstop Lagos", real_build=True)
            self.assertFalse(val.checks["placeholder_scan"])
            self.assertTrue(any(e.code == "FICTIONAL_DEMO_DATA_IN_REAL_BUILD" for e in val.findings))

    def test_verified_offerings_rendered_and_food_illustration_omitted(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        self.assertEqual(res.status, "COMPLETED")
        html = Path(res.output_location).read_text(encoding="utf-8")
        # Verified services rendered as offerings cards
        self.assertIn("Offerings", html)
        self.assertIn("Healthy-Inspired Cafe", html)
        self.assertIn("Cycling Community Rides", html)
        self.assertIn("Wattbike Training Studio", html)
        self.assertIn("Bicycle Service", html)
        # Remnants of fictional shared meal illustration strictly omitted
        self.assertNotIn("Made for the table", html)
        self.assertNotIn("Decorative illustration of a shared meal", html)
        self.assertNotIn("<div class=\"hero-art\"", html)
        self.assertNotIn("<path class=\"art-rice\"", html)

    def test_verified_contacts_and_address_present_in_output(self):
        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        self.assertEqual(res.status, "COMPLETED")
        html = Path(res.output_location).read_text(encoding="utf-8")
        self.assertIn("tel:+234 807 749 8324", html)
        self.assertIn("info@pitstoplagos.com", html)
        self.assertIn("92A Younis Bashorun Street, Victoria Island, Lagos", html)
        self.assertIn("https://www.instagram.com/pitstoplagos/", html)

    def test_validator_detects_missing_verified_email(self):
        html_without_email = "<html><header></header><main><h1>Pitstop Lagos</h1><a href='tel:+2348077498324'>Call</a></main><footer></footer></html>"
        val = validate_rendered_html(
            html_without_email,
            "Pitstop Lagos",
            real_build=True,
            expected_contact={"phone": "+2348077498324", "email": "info@pitstoplagos.com"},
        )
        self.assertFalse(val.checks["contact_cta"])
        self.assertTrue(any(e.code == "MISSING_VERIFIED_EMAIL" for e in val.findings))

    def test_real_build_output_does_not_modify_lead_0001_directory(self):
        lead1_dir = self.root / "output" / "lead-0001" / "build-001"
        lead1_dir.mkdir(parents=True)
        (lead1_dir / "index.html").write_text("Lead 1 Content", encoding="utf-8")

        builder = WebsiteBuilder(output_root=self.root / "output")
        res = builder.build(pitstop_lead_payload())
        self.assertEqual(res.lead_id, "lead-0002")
        self.assertIn("lead-0002", res.output_location)
        # Verify lead-0001 was untouched
        self.assertEqual((lead1_dir / "index.html").read_text(encoding="utf-8"), "Lead 1 Content")

    def test_tracking_lifecycle_transitions_and_pending_review(self):
        store = TrackingStore(self.root / "state.json")
        store.create_lead({
            "business_name": "Pitstop Lagos",
            "city": "Lagos",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        proj = store.create_project("lead-0001")
        self.assertEqual(proj["current_stage"], "QUALIFIED")
        self.assertEqual(proj["review_status"], "PENDING")

        builder = WebsiteBuilder(output_root=self.root / "output")
        payload = pitstop_lead_payload()
        payload["lead_id"] = "lead-0001"
        res = builder.build(payload)

        store.record_build(proj["project_id"], res.to_dict())
        self.assertEqual(proj["current_stage"], "VALIDATING")

        store.record_validation(proj["project_id"], res.validation)
        self.assertEqual(proj["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(proj["review_status"], "PENDING")  # Human review mandatory

    def test_historical_fictional_artifacts_and_audits_remain_intact(self):
        store = TrackingStore()
        lead1 = store.state["leads"]["lead-0001"]
        proj1 = store.state["projects"]["project-0001"]
        self.assertEqual(lead1["data_classification"], "TEST_FIXTURE")
        self.assertEqual(proj1["current_stage"], "READY_FOR_REVIEW")

        p25_audit = Path("data/leads/phase25-lagos-restaurants-discovery-and-qualification-20261012.json")
        p26_audit = Path("data/leads/phase26-pitstop-lagos-website-draft-20261012.json")
        self.assertTrue(p25_audit.exists())
        self.assertTrue(p26_audit.exists())


if __name__ == "__main__":
    unittest.main()
