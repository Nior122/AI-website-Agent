"""Offline tests for Tavily search enrichment and its safety gates."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scout.cli import main as scout_main
from scout.scout_agent import FixtureSource, Scout, Target
from scout.tavily_enrichment import TavilySearchEnricher


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "scout_candidates.json"


class FakeResponse:
    """Minimal urllib response for tests; never opens a real connection."""

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


def make_lead():
    """Create a small normalized candidate without any external dependencies."""
    return {
        "businessName": "The Place",
        "normalizedBusinessName": "theplace",
        "niche": "Restaurants",
        "location": {
            "city": "Lagos",
            "country": "Nigeria",
            "address": "Admiralty Way, Lekki, Lagos",
        },
        "contact": {
            "phone": None,
            "email": None,
            "whatsapp": None,
        },
        "originalWebsite": None,
        "websiteStatus": "none_known",
        "social": {
            "instagramUrl": None,
            "facebookUrl": None,
            "otherSocialUrls": [],
        },
        "sourceUrls": [
            "https://www.openstreetmap.org/node/4193719013"
        ],
        "discoverySources": ["openstreetmap_overpass"],
        "fieldSources": {
            "businessName": [
                "https://www.openstreetmap.org/node/4193719013"
            ]
        },
        "notes": [],
        "researchEvidence": [],
        "publicBusinessInformation": {
            "servicesOrProducts": [],
            "description": None,
            "openingHours": None,
            "brandNotes": None,
            "assetReferences": [],
        },
    }


class TavilySearchEnrichmentTests(unittest.TestCase):
    def test_search_sends_expected_bounded_request_without_network(self):
        """The API request is mocked and the result count remains capped."""
        payload = {
            "results": [
                {
                    "title": "The Place official website",
                    "url": "https://theplace.com.ng",
                    "content": "The Place restaurant in Lagos.",
                    "score": 0.9,
                },
                {
                    "title": "The Place restaurant listing",
                    "url": "https://directory.example/the-place",
                    "content": "Restaurant listing.",
                    "score": 0.8,
                },
                {
                    "title": "An extra result",
                    "url": "https://extra.example",
                    "content": "This result should fall outside the configured limit.",
                    "score": 0.7,
                },
            ]
        }

        enricher = TavilySearchEnricher(
            api_key="test-key",
            max_results=2,
        )

        with patch(
            "scout.tavily_enrichment.urlopen",
            return_value=FakeResponse(payload),
        ) as mocked_urlopen:
            results = enricher._search("The Place Lagos restaurants")

        self.assertEqual(len(results), 2)
        self.assertEqual(
            results[0]["url"],
            "https://theplace.com.ng",
        )

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.tavily.com/search",
        )
        self.assertEqual(request.get_method(), "POST")

        submitted_payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(submitted_payload["max_results"], 2)
        self.assertEqual(submitted_payload["search_depth"], "basic")
        self.assertFalse(submitted_payload["include_answer"])
        self.assertFalse(submitted_payload["include_raw_content"])

    def test_invalid_result_urls_are_rejected(self):
        """Search results with non-HTTP URLs are discarded."""
        payload = {
            "results": [
                {
                    "title": "Invalid result",
                    "url": "javascript:alert(1)",
                    "content": "Not an acceptable URL.",
                    "score": 0.5,
                },
                {
                    "title": "Valid result",
                    "url": "https://example.com/business",
                    "content": "A valid result.",
                    "score": 0.8,
                },
            ]
        }

        enricher = TavilySearchEnricher(api_key="test-key")

        with patch(
            "scout.tavily_enrichment.urlopen",
            return_value=FakeResponse(payload),
        ):
            results = enricher._search("test query")

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["url"],
            "https://example.com/business",
        )

    def test_missing_api_key_is_rejected(self):
        """An empty credential must fail before any request can be sent."""
        with self.assertRaisesRegex(ValueError, "API key is required"):
            TavilySearchEnricher(api_key="")

    def test_enrichment_records_matching_evidence_and_field_sources(self):
        """A matching result can enrich a candidate but remains unverified."""
        lead = make_lead()
        target = Target("Nigeria", "Lagos", "Restaurants", lead_limit=5)
        enricher = TavilySearchEnricher(api_key="test-key")

        enricher._search = Mock(
            return_value=[
                {
                    "title": "The Place | Official Website",
                    "url": "https://theplace.com.ng",
                    "content": (
                        "The Place restaurant in Lagos. "
                        "Contact us at hello@theplace.com.ng."
                    ),
                    "score": 0.9,
                }
            ]
        )

        enricher.enrich(lead, target)

        self.assertEqual(
            lead["originalWebsite"],
            "https://theplace.com.ng",
        )
        self.assertEqual(lead["websiteStatus"], "unknown")
        self.assertEqual(
            lead["contact"]["email"],
            "hello@theplace.com.ng",
        )
        self.assertIn(
            "https://theplace.com.ng",
            lead["sourceUrls"],
        )
        self.assertIn(
            "https://theplace.com.ng",
            lead["fieldSources"]["originalWebsite"],
        )
        self.assertIn(
            "https://theplace.com.ng",
            lead["fieldSources"]["contact.email"],
        )
        self.assertTrue(lead["researchEvidence"][0]["businessNameMatched"])
        self.assertTrue(
            any("Unverified possible website" in note for note in lead["notes"])
        )

    def test_unmatched_result_does_not_add_website_or_contact(self):
        """An unrelated search result cannot populate the candidate's fields."""
        lead = make_lead()
        target = Target("Nigeria", "Lagos", "Restaurants", lead_limit=5)
        initial_source_urls = list(lead["sourceUrls"])
        enricher = TavilySearchEnricher(api_key="test-key")

        enricher._search = Mock(
            return_value=[
                {
                    "title": "Another Lagos restaurant",
                    "url": "https://unrelated.example",
                    "content": (
                        "A different restaurant. "
                        "Contact unrelated@example.com."
                    ),
                    "score": 0.9,
                }
            ]
        )

        enricher.enrich(lead, target)

        self.assertIsNone(lead["originalWebsite"])
        self.assertIsNone(lead["contact"]["email"])
        self.assertEqual(lead["sourceUrls"], initial_source_urls)
        self.assertFalse(
            lead["researchEvidence"][0]["businessNameMatched"]
        )

    def test_tavily_approval_is_required_before_any_request(self):
        """Missing enrichment approval must stop execution before a request."""
        stderr = StringIO()

        with (
            patch("scout.tavily_enrichment.urlopen") as mocked_urlopen,
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as raised:
                scout_main(
                    [
                        "--source",
                        "overpass",
                        "--approve-live-lookups",
                        "--enrich-tavily",
                        "--dry-run",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--approve-enrichment", stderr.getvalue())
        mocked_urlopen.assert_not_called()

    def test_fixture_mode_refuses_tavily_enrichment(self):
        """Fixture runs remain offline even when enrichment is requested."""
        stderr = StringIO()

        with (
            patch("scout.tavily_enrichment.urlopen") as mocked_urlopen,
            redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as raised:
                scout_main(
                    [
                        "--source",
                        "fixture",
                        "--source-file",
                        str(FIXTURE),
                        "--enrich-tavily",
                        "--approve-enrichment",
                        "--dry-run",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Fixture data remains offline", stderr.getvalue())
        mocked_urlopen.assert_not_called()

    def test_scout_caps_enrichment_at_three_candidates(self):
        """The first enrichment pilot never queries more than three candidates."""
        class RecordingEnricher:
            name = "test_enricher"

            def __init__(self):
                self.names = []

            def enrich(self, lead, target):
                self.names.append(lead["businessName"])

        enricher = RecordingEnricher()
        target = Target(
            "Nigeria",
            "Port Harcourt",
            "Beauty salons",
            lead_limit=10,
        )

        result = Scout(
            target,
            [FixtureSource(FIXTURE)],
            enricher=enricher,
        ).run()

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(enricher.names), 3)


if __name__ == "__main__":
    unittest.main()