import unittest

from research.reverification import build_reverification_event


class ReverificationTests(unittest.TestCase):
    def test_preserves_prior_evidence_and_source_classification(self):
        event = build_reverification_event(
            candidate_id="osm-node-1",
            prior_source_urls=["https://www.openstreetmap.org/node/1"],
            evidence=[{
                "claim": "A public directory links to a social profile.",
                "source_url": "https://directory.example/candidate",
                "source_type": "directory",
                "confidence": "medium",
                "appears_official": False,
                "property_class": "DIRECTORY",
            }, {
                "claim": "A public social property is present.",
                "source_url": "https://social.example/candidate",
                "source_type": "social",
                "confidence": "medium",
                "appears_official": True,
                "property_class": "SOCIAL",
            }],
            website_status="UNKNOWN",
            decision="NEEDS_MORE_REVIEW",
        )
        self.assertEqual(event["previous_source_urls"], ["https://www.openstreetmap.org/node/1"])
        self.assertEqual(event["evidence"][0]["property_class"], "DIRECTORY")
        self.assertFalse(event["result"]["lead_created"])
        self.assertFalse(event["result"]["project_created"])

    def test_missing_osm_website_is_not_a_qualification_shortcut(self):
        with self.assertRaises(ValueError):
            build_reverification_event(
                candidate_id="osm-node-1",
                prior_source_urls=[],
                evidence=[],
                website_status="NO_WEBSITE",
                decision="QUALIFIED_WEBSITE_OPPORTUNITY",
            )

    def test_rejects_an_unknown_property_classification(self):
        with self.assertRaises(ValueError):
            build_reverification_event(
                candidate_id="osm-node-1",
                prior_source_urls=["https://www.openstreetmap.org/node/1"],
                evidence=[{
                    "claim": "Example",
                    "source_url": "https://example.test",
                    "source_type": "directory",
                    "confidence": "low",
                    "appears_official": False,
                    "property_class": "MAGIC",
                }],
                website_status="UNKNOWN",
                decision="NEEDS_MORE_REVIEW",
            )

    def test_social_or_ordering_presence_does_not_elevate_ownership(self):
        event = build_reverification_event(
            candidate_id="osm-node-1",
            prior_source_urls=["https://www.openstreetmap.org/node/1"],
            evidence=[{
                "claim": "A social profile is linked by a directory.",
                "source_url": "https://social.example/candidate",
                "source_type": "social",
                "confidence": "medium",
                "appears_official": False,
                "property_class": "SOCIAL",
            }, {
                "claim": "A platform menu URL is associated by one directory.",
                "source_url": "https://ordering.example/candidate/menu",
                "source_type": "ordering_platform",
                "confidence": "low",
                "appears_official": False,
                "property_class": "ORDERING_PLATFORM",
            }],
            website_status="WEBSITE_FOUND",
            decision="NOT_YET_QUALIFIED",
        )
        self.assertEqual([item["property_class"] for item in event["evidence"]], ["SOCIAL", "ORDERING_PLATFORM"])
        self.assertEqual(event["result"]["website_status"], "WEBSITE_FOUND")
        self.assertEqual(event["result"]["decision"], "NOT_YET_QUALIFIED")

    def test_unreachable_is_not_converted_to_no_website(self):
        event = build_reverification_event(
            candidate_id="osm-node-1",
            prior_source_urls=["https://www.openstreetmap.org/node/1"],
            evidence=[{
                "claim": "The public website could not be reached by the approved tool.",
                "source_url": "https://example.test",
                "source_type": "website",
                "confidence": "medium",
                "appears_official": False,
                "property_class": "UNKNOWN",
            }],
            website_status="WEBSITE_UNREACHABLE",
            decision="NEEDS_REVIEW",
        )
        self.assertEqual(event["result"]["website_status"], "WEBSITE_UNREACHABLE")


if __name__ == "__main__":
    unittest.main()
