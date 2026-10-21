import copy
import json
from pathlib import Path
import unittest

from templates.registry import select_template
from templates.renderer import render_site, validate_business_data


DEMO = Path(__file__).parents[1] / "templates" / "demo-data"


def load_demo(name):
    return json.loads((DEMO / name).read_text(encoding="utf-8"))


class TemplateTests(unittest.TestCase):
    def test_category_selects_expected_template(self):
        self.assertEqual(select_template("beauty salon").template_id, "salon-service-v1")
        self.assertEqual(select_template("restaurant").template_id, "restaurant-menu-v1")
        self.assertEqual(select_template("clinic").template_id, "clinic-healthcare-v1")
        self.assertEqual(select_template("healthcare").template_id, "clinic-healthcare-v1")
        self.assertEqual(select_template("dental clinic").template_id, "clinic-healthcare-v1")
        self.assertEqual(select_template("legal").template_id, "legal-practice-v1")
        self.assertEqual(select_template("law firm").template_id, "legal-practice-v1")
        self.assertEqual(select_template("solicitor").template_id, "legal-practice-v1")
        self.assertEqual(select_template("automotive").template_id, "automotive-service-v1")
        self.assertEqual(select_template("auto repair").template_id, "automotive-service-v1")
        self.assertEqual(select_template("mechanic").template_id, "automotive-service-v1")

    def test_clinic_renders_with_semantic_sections_and_safe_fictional_actions(self):
        from validation.quality_validator import validate_rendered_html
        data = load_demo("demo_clinic.json")
        html = render_site(data)
        self.assertIn('<main>', html)
        self.assertIn('Clinical Specialties &amp; Care', html)
        self.assertIn('Specialist Practitioners', html)
        self.assertIn('href="#services"', html)
        self.assertNotIn('tel:+234 800 000 0003', html)
        result = validate_rendered_html(html, data["business_name"], real_build=False, synthetic_contact=True)
        self.assertEqual(result.status, "PASS")

    def test_legal_renders_with_semantic_sections_and_safe_fictional_actions(self):
        from validation.quality_validator import validate_rendered_html
        data = load_demo("demo_legal.json")
        html = render_site(data)
        self.assertIn('<main>', html)
        self.assertIn('Practice Areas', html)
        self.assertIn('Legal Practitioners', html)
        self.assertIn('Corporate &amp; Commercial Law', html)
        self.assertIn('href="#practice_areas"', html)
        result = validate_rendered_html(html, data["business_name"], real_build=False, synthetic_contact=True)
        self.assertEqual(result.status, "PASS")

    def test_automotive_renders_with_semantic_sections_and_safe_fictional_actions(self):
        from validation.quality_validator import validate_rendered_html
        data = load_demo("demo_automotive.json")
        html = render_site(data)
        self.assertIn('<main>', html)
        self.assertIn('Workshop &amp; Repair Services', html)
        self.assertIn('Service Guarantee', html)
        self.assertIn('href="#services"', html)
        result = validate_rendered_html(html, data["business_name"], real_build=False, synthetic_contact=True)
        self.assertEqual(result.status, "PASS")

    def test_salon_renders_with_semantic_sections_and_safe_fictional_actions(self):
        html = render_site(load_demo("demo_salon.json"))
        self.assertIn('<main>', html)
        self.assertIn('href="#services"', html)
        self.assertNotIn('href="tel:+234 800 000 0001"', html)
        self.assertNotIn('https://wa.me/2348000000001', html)
        self.assertIn('Natural Hair Care', html)

    def test_optional_data_is_omitted_without_breaking_render(self):
        data = load_demo("demo_restaurant.json")
        for key in ("phone", "whatsapp", "address", "opening_hours"):
            data.pop(key, None)
        html = render_site(data)
        self.assertIn('Demo Riverside Kitchen', html)
        self.assertNotIn('Get in touch', html)
        self.assertNotIn('<img ', html)

    def test_long_business_and_service_names_are_escaped(self):
        data = load_demo("demo_salon.json")
        data["business_name"] = "A <Very> Long & Fictional Salon Name " * 4
        data["services"][0]["name"] = "A long service name " * 20
        html = render_site(data)
        self.assertIn('&lt;Very&gt;', html)
        self.assertNotIn('<Very>', html)

    def test_required_fields_are_reported(self):
        data = load_demo("demo_salon.json")
        data.pop("city")
        with self.assertRaisesRegex(ValueError, "Missing required field: city"):
            render_site(data)

    def test_unknown_category_is_not_silently_mapped(self):
        with self.assertRaisesRegex(ValueError, "No implemented template"):
            select_template("real_estate")

    def test_output_includes_mobile_layout_rules(self):
        html = render_site(load_demo("demo_salon.json"))
        self.assertIn('@media (max-width:48rem)', html)
        self.assertIn('grid-template-columns:repeat(3,minmax(0,1fr))', html)


if __name__ == "__main__":
    unittest.main()
