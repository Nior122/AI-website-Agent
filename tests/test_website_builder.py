import copy
import json
from pathlib import Path
import tempfile
import unittest

from builder.content_generator import ContentGenerator
from builder.website_builder import WebsiteBuilder


LEADS = json.loads((Path(__file__).parent / "fixtures" / "qualified_leads.json").read_text(encoding="utf-8"))


class UngroundedGenerator:
    name = "test-ungrounded"

    def generate(self, facts, category):
        return {"hero_title": facts["business_name"], "hero_subtitle": "Award-winning company with 10,000 customers.", "about_text": None, "cta": {}}


class WebsiteBuilderTests(unittest.TestCase):
    def build(self, lead, generator=None):
        self.tempdir = tempfile.TemporaryDirectory()
        return WebsiteBuilder(Path(self.tempdir.name), generator).build(copy.deepcopy(lead))

    def tearDown(self):
        if hasattr(self, "tempdir"):
            self.tempdir.cleanup()

    def test_salon_builds_local_artifact_without_images_or_logo(self):
        result = self.build(LEADS[0])
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.template_id, "salon-service-v1")
        self.assertTrue(Path(result.output_location).is_file())
        self.assertTrue(result.validation["passed"])

    def test_restaurant_without_whatsapp_builds_menu_template(self):
        result = self.build(LEADS[1])
        html = Path(result.output_location).read_text(encoding="utf-8")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.template_id, "restaurant-menu-v1")
        self.assertNotIn("wa.me", html)
        self.assertIn("Rice bowl", html)

    def test_professional_service_with_incomplete_optional_data_builds(self):
        result = self.build(LEADS[2])
        self.assertEqual(result.status, "COMPLETED")
        self.assertIn("Business advisory", Path(result.output_location).read_text(encoding="utf-8"))

    def test_long_name_and_many_services_do_not_break_build(self):
        lead = copy.deepcopy(LEADS[0])
        long_name = "Fictional Long Service Business " * 8
        lead["candidate"]["businessName"] = long_name
        lead["research"]["verifiedFacts"][0]["value"] = long_name
        lead["research"]["verifiedFacts"][3]["value"] = [f"Fictional service {index}" for index in range(30)]
        result = self.build(lead)
        self.assertEqual(result.status, "COMPLETED")
        self.assertIn("Fictional service 29", Path(result.output_location).read_text(encoding="utf-8"))

    def test_unqualified_record_fails_without_artifact(self):
        lead = copy.deepcopy(LEADS[0])
        lead["research"]["qualificationStatus"] = "REVIEW"
        result = self.build(lead)
        self.assertEqual(result.status, "FAILED")
        self.assertIsNone(result.output_location)
        self.assertIn("QUALIFIED", result.errors[0])

    def test_ungrounded_provider_content_is_rejected(self):
        result = self.build(LEADS[0], UngroundedGenerator())
        self.assertEqual(result.status, "FAILED")
        self.assertIn("not grounded", result.errors[0])


if __name__ == "__main__":
    unittest.main()
