import unittest

from validation.quality_validator import validate_rendered_html


VALID_HTML = '''<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Fictional Studio</title><meta name="description" content="Fictional Studio in Port Harcourt."><style>@media (max-width: 38rem) { body { margin: 0; } }</style></head><body><header><nav><a href="#services">Services</a></nav></header><main><h1>Fictional Studio</h1><section id="services"><a href="tel:+2348000000001">Call</a><a href="https://wa.me/2348000000001">Chat</a></section></main><footer>Fictional Studio</footer></body></html>'''


class ValidationTests(unittest.TestCase):
    def validate(self, html=VALID_HTML, **kwargs):
        return validate_rendered_html(html, "Fictional Studio", real_build=True, expected_contact={"phone": "+2348000000001", "whatsapp": "+2348000000001"}, **kwargs)

    def codes(self, result):
        return {item.code for item in result.findings}

    def test_complete_fictional_site_passes(self):
        result = self.validate()
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.score, 100)

    def test_broken_internal_and_external_links_fail(self):
        html = VALID_HTML.replace('#services', '#missing').replace('https://wa.me/2348000000001', 'mailto:test@example.invalid')
        result = self.validate(html)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("BROKEN_INTERNAL_LINK", self.codes(result))
        self.assertIn("UNSUPPORTED_LINK_SCHEME", self.codes(result))

    def test_missing_alt_and_invalid_whatsapp_fail(self):
        html = VALID_HTML.replace('</section>', '<img src="https://image.example.invalid/a.jpg" alt=""><a href="https://wa.me/not-a-number">Bad</a></section>')
        result = self.validate(html)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("MISSING_IMAGE_ALT", self.codes(result))
        self.assertIn("INVALID_WHATSAPP_LINK", self.codes(result))

    def test_placeholder_and_secret_are_critical_or_error(self):
        html = VALID_HTML.replace('</main>', '<p>Lorem ipsum</p><script>api_key="REDACTEDVALUE123456"</script></main>')
        result = self.validate(html)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("PLACEHOLDER_CONTENT", self.codes(result))
        self.assertIn("POTENTIAL_SECRET", self.codes(result))
        self.assertNotIn("REDACTEDVALUE123456", str(result.to_dict()))

    def test_unsupported_claim_needs_review(self):
        result = self.validate(VALID_HTML.replace('</main>', '<p>Award-winning service.</p></main>'))
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertIn("UNSUPPORTED_CLAIM", self.codes(result))

    def test_missing_metadata_and_mobile_support_are_reported(self):
        html = VALID_HTML.replace('<meta name="description" content="Fictional Studio in Port Harcourt.">', '').replace('@media (max-width: 38rem) { body { margin: 0; } }', '')
        result = self.validate(html)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("MISSING_META_DESCRIPTION", self.codes(result))
        self.assertIn("MISSING_MOBILE_SUPPORT", self.codes(result))

    def test_missing_required_business_content_fails(self):
        html = VALID_HTML.replace("<h1>Fictional Studio</h1>", "")
        result = self.validate(html)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("MISSING_PRIMARY_HEADING", self.codes(result))

    def test_missing_optional_whatsapp_does_not_fail(self):
        html = VALID_HTML.replace('<a href="https://wa.me/2348000000001">Chat</a>', '')
        result = validate_rendered_html(html, "Fictional Studio", real_build=True, expected_contact={"phone": "+2348000000001"})
        self.assertEqual(result.status, "PASS")

    def test_missing_expected_contact_cta_needs_review(self):
        html = VALID_HTML.replace('<a href="https://wa.me/2348000000001">Chat</a>', '')
        result = self.validate(html)
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertIn("MISSING_WHATSAPP_CTA", self.codes(result))

    def test_synthetic_contact_mode_permits_safe_non_actionable_fixture_output(self):
        html = VALID_HTML.replace('<a href="tel:+2348000000001">Call</a>', '').replace('<a href="https://wa.me/2348000000001">Chat</a>', '')
        result = validate_rendered_html(
            html,
            "Fictional Studio",
            real_build=False,
            expected_contact={"phone": "+2348000000001", "whatsapp": "+2348000000001"},
            synthetic_contact=True,
        )
        self.assertEqual(result.status, "PASS")


if __name__ == "__main__":
    unittest.main()
