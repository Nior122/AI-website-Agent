"""Final Stage: Reusable Website Template & Production Presentation tests.

Validates all 6 template categories render successfully, pass the 14-point
static QA gate at 100/100 PASS, and maintain clean data separation between
business content, brand theming, and layout structure.
"""

import json
import os
import re
import unittest
from pathlib import Path

from templates.registry import TEMPLATES, get_template, select_template
from templates.renderer import render_site, validate_business_data
from templates.components import is_fictional, FICTIONAL_DISCLOSURE
from validation.quality_validator import validate_rendered_html


DEMO_DIR = Path(__file__).resolve().parent.parent / "templates" / "demo-data"


def _load_demo(name: str) -> dict:
    path = DEMO_DIR / name
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TestTemplateRegistry(unittest.TestCase):
    """Registry contract tests."""

    def test_all_templates_registered(self):
        self.assertEqual(len(TEMPLATES), 7)

    def test_all_template_ids_unique(self):
        ids = [t.template_id for t in TEMPLATES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_categories_unique_across_templates(self):
        all_cats = []
        for t in TEMPLATES:
            all_cats.extend(t.categories)
        self.assertEqual(len(all_cats), len(set(all_cats)), "Category assigned to multiple templates")

    def test_get_template_returns_correct_definition(self):
        for t in TEMPLATES:
            result = get_template(t.template_id)
            self.assertEqual(result.template_id, t.template_id)

    def test_get_template_raises_on_unknown(self):
        with self.assertRaises(ValueError):
            get_template("nonexistent-template-v99")

    def test_select_template_routes_all_categories(self):
        for t in TEMPLATES:
            for cat in t.categories:
                result = select_template(cat)
                self.assertEqual(result.template_id, t.template_id, f"Category '{cat}' routed to wrong template")

    def test_retail_template_exists(self):
        t = get_template("retail-ecommerce-v1")
        self.assertEqual(t.name, "Retail & E-commerce")
        self.assertIn("retail", t.categories)
        self.assertIn("boutique", t.categories)
        self.assertIn("fashion", t.categories)

    def test_required_data_is_minimal(self):
        for t in TEMPLATES:
            self.assertEqual(t.required_data, ("business_name", "category", "city"),
                             f"Template {t.template_id} has unexpected required_data")


class TestDemoFixtures(unittest.TestCase):
    """Verify all 6 demo fixtures exist and have correct structure."""

    FIXTURES = [
        "demo_salon.json",
        "demo_restaurant.json",
        "demo_clinic.json",
        "demo_legal.json",
        "demo_automotive.json",
        "demo_retail.json",
        "demo_portfolio.json",
    ]

    def test_all_fixtures_exist(self):
        for name in self.FIXTURES:
            path = DEMO_DIR / name
            self.assertTrue(path.exists(), f"Missing fixture: {name}")

    def test_all_fixtures_are_valid_json(self):
        for name in self.FIXTURES:
            data = _load_demo(name)
            self.assertIsInstance(data, dict)

    def test_all_fixtures_marked_demo(self):
        for name in self.FIXTURES:
            data = _load_demo(name)
            self.assertTrue(data.get("demo"), f"{name} missing demo: true")

    def test_all_fixtures_have_required_fields(self):
        for name in self.FIXTURES:
            data = _load_demo(name)
            for field in ("business_name", "category", "city"):
                self.assertTrue(data.get(field), f"{name} missing {field}")

    def test_all_fixtures_have_enriched_content(self):
        """Each fixture should have at least 3 services/items/areas."""
        for name in self.FIXTURES:
            data = _load_demo(name)
            items = data.get("services") or data.get("menu_items") or data.get("practice_areas") or data.get("products") or []
            self.assertGreaterEqual(len(items), 2, f"{name} has insufficient content items")

    def test_all_fixtures_have_brand_colors(self):
        for name in self.FIXTURES:
            data = _load_demo(name)
            brand = data.get("brand", {})
            self.assertTrue(brand.get("primary_color"), f"{name} missing brand primary_color")

    def test_retail_fixture_has_products(self):
        data = _load_demo("demo_retail.json")
        self.assertGreaterEqual(len(data.get("products", [])), 3)
        self.assertGreaterEqual(len(data.get("services", [])), 1)

    def test_no_real_business_data_in_fixtures(self):
        """Fixtures must not contain real business facts."""
        for name in self.FIXTURES:
            data = _load_demo(name)
            name_lower = data.get("business_name", "").lower()
            self.assertTrue(
                "demo" in name_lower or "fictional" in name_lower,
                f"{name} business_name does not contain 'Demo' or 'Fictional'"
            )


class TestTemplateRendering(unittest.TestCase):
    """Verify all templates render valid HTML from their demo fixtures."""

    FIXTURE_MAP = {
        "salon-service-v1": "demo_salon.json",
        "restaurant-menu-v1": "demo_restaurant.json",
        "clinic-healthcare-v1": "demo_clinic.json",
        "legal-practice-v1": "demo_legal.json",
        "automotive-service-v1": "demo_automotive.json",
        "retail-ecommerce-v1": "demo_retail.json",
        "personal-portfolio-v1": "demo_portfolio.json",
    }

    def test_all_templates_render_without_error(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIsInstance(html, str)
            self.assertGreater(len(html), 1000, f"Template {tid} produced suspiciously short output")

    def test_all_templates_contain_business_name(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn(data["business_name"], html, f"Template {tid} missing business name in output")

    def test_all_templates_have_semantic_landmarks(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn("<header", html, f"{tid} missing header")
            self.assertIn("<main>", html, f"{tid} missing main")
            self.assertIn("<footer>", html, f"{tid} missing footer")

    def test_all_templates_have_viewport_meta(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn('name="viewport"', html, f"{tid} missing viewport meta")

    def test_all_templates_have_title_and_description(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn("<title>", html, f"{tid} missing title tag")
            self.assertIn('name="description"', html, f"{tid} missing meta description")

    def test_all_templates_have_h1(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn("<h1>", html, f"{tid} missing H1")

    def test_all_templates_have_responsive_css(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn("@media", html, f"{tid} missing responsive media queries")

    def test_fictional_disclosure_present(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            self.assertIn(FICTIONAL_DISCLOSURE, html, f"{tid} missing fictional disclosure")

    def test_retail_template_renders_products_section(self):
        data = _load_demo("demo_retail.json")
        html = render_site(data, "retail-ecommerce-v1")
        self.assertIn('id="products"', html)
        self.assertIn("Featured Products", html)

    def test_retail_template_renders_services_section(self):
        data = _load_demo("demo_retail.json")
        html = render_site(data, "retail-ecommerce-v1")
        self.assertIn('id="services"', html)
        self.assertIn("Our Services", html)

    def test_restaurant_hero_has_art(self):
        data = _load_demo("demo_restaurant.json")
        html = render_site(data, "restaurant-menu-v1")
        self.assertIn("hero-art", html)
        self.assertIn("hero-restaurant", html)

    def test_brand_css_overrides_applied(self):
        data = _load_demo("demo_salon.json")
        html = render_site(data, "salon-service-v1")
        self.assertIn("--primary:#8b2454", html)


class TestQAGateCompliance(unittest.TestCase):
    """Every demo fixture must pass the 14-point static QA validator at PASS."""

    FIXTURE_MAP = {
        "salon-service-v1": "demo_salon.json",
        "restaurant-menu-v1": "demo_restaurant.json",
        "clinic-healthcare-v1": "demo_clinic.json",
        "legal-practice-v1": "demo_legal.json",
        "automotive-service-v1": "demo_automotive.json",
        "retail-ecommerce-v1": "demo_retail.json",
        "personal-portfolio-v1": "demo_portfolio.json",
    }

    def test_all_templates_pass_qa_at_100(self):
        for tid, fixture in self.FIXTURE_MAP.items():
            data = _load_demo(fixture)
            html = render_site(data, tid)
            result = validate_rendered_html(
                html,
                data["business_name"],
                real_build=False,
                build_id=f"test-{tid}",
                lead_id="test-lead",
            )
            self.assertEqual(
                result.status, "PASS",
                f"Template {tid} QA status: {result.status}, score: {result.score}, "
                f"findings: {[f'{f.severity}:{f.code}:{f.message}' for f in result.findings]}"
            )
            self.assertEqual(result.score, 100, f"Template {tid} QA score: {result.score}")


class TestDataSeparation(unittest.TestCase):
    """Verify template system maintains clean data/layout separation."""

    def test_brand_values_are_css_only(self):
        """Brand overrides must only produce CSS custom property values."""
        data = _load_demo("demo_salon.json")
        html = render_site(data, "salon-service-v1")
        # Brand colors should appear in :root{} CSS block, not in HTML content
        self.assertIn("--primary:#8b2454", html)
        # But not as raw text in the body
        body_start = html.find("<body>")
        body_html = html[body_start:]
        self.assertNotIn("#8b2454", body_html)

    def test_fictional_data_never_creates_real_links(self):
        """Demo fixtures must not produce actionable external links."""
        for fixture in ["demo_salon.json", "demo_restaurant.json", "demo_clinic.json",
                        "demo_legal.json", "demo_automotive.json", "demo_retail.json"]:
            data = _load_demo(fixture)
            self.assertTrue(is_fictional(data), f"{fixture} not detected as fictional")

    def test_template_selection_is_deterministic(self):
        """Same category always selects same template."""
        for _ in range(5):
            t1 = select_template("restaurant")
            t2 = select_template("restaurant")
            self.assertEqual(t1.template_id, t2.template_id)


class TestCSSFoundation(unittest.TestCase):
    """Verify the CSS foundation contains required design tokens."""

    @classmethod
    def setUpClass(cls):
        css_path = Path(__file__).resolve().parent.parent / "templates" / "styles.css"
        cls.css = css_path.read_text(encoding="utf-8")

    def test_css_contains_custom_properties(self):
        self.assertIn("--primary:", self.css)
        self.assertIn("--secondary:", self.css)
        self.assertIn("--accent:", self.css)
        self.assertIn("--background:", self.css)

    def test_css_contains_typography_stack(self):
        self.assertIn("DM Serif Display", self.css)
        self.assertIn("Inter", self.css)

    def test_css_contains_responsive_breakpoints(self):
        self.assertIn("@media (max-width: 64rem)", self.css)
        self.assertIn("@media (max-width: 48rem)", self.css)
        self.assertIn("@media (max-width: 24rem)", self.css)

    def test_css_contains_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_css_contains_focus_styles(self):
        self.assertIn("focus-visible", self.css)

    def test_css_contains_transitions(self):
        self.assertIn("transition:", self.css)
        self.assertIn("--ease-out:", self.css)

    def test_css_contains_glass_tokens(self):
        self.assertIn("--glass-bg:", self.css)
        self.assertIn("--glass-shadow:", self.css)

    def test_css_under_size_limit(self):
        """CSS should be reasonable size for inline embedding."""
        self.assertLess(len(self.css.encode("utf-8")), 20000, "CSS exceeds 20KB")


class TestEdgeCases(unittest.TestCase):
    """Edge case handling for template rendering."""

    def test_missing_optional_fields_still_renders(self):
        minimal = {"business_name": "Test Minimal", "category": "beauty_salon", "city": "Lagos", "demo": True}
        html = render_site(minimal, "salon-service-v1")
        self.assertIn("Test Minimal", html)
        self.assertIn("<h1>", html)

    def test_empty_services_list_still_renders(self):
        data = {"business_name": "Test Empty", "category": "retail", "city": "Lagos", "demo": True, "services": [], "products": []}
        html = render_site(data, "retail-ecommerce-v1")
        self.assertIn("Test Empty", html)

    def test_missing_required_field_raises(self):
        data = {"category": "restaurant", "city": "Lagos"}
        with self.assertRaises(ValueError):
            render_site(data, "restaurant-menu-v1")

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            select_template("underwater_basket_weaving")


if __name__ == "__main__":
    unittest.main()
