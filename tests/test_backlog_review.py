import unittest

from opportunities.backlog import review_backlog


def candidate(*, status="unknown", sources=None, phone=None):
    return {
        "businessName": "Review Candidate",
        "niche": "Restaurants",
        "location": {"city": "Lagos", "country": "Nigeria"},
        "contact": {"phone": phone, "email": None, "whatsapp": None},
        "social": {"instagramUrl": None, "facebookUrl": None, "otherSocialUrls": []},
        "publicBusinessInformation": {"description": "Public restaurant description", "servicesOrProducts": [], "openingHours": None},
        "sourceUrls": sources or ["https://source.example.invalid/a"],
        "websiteStatus": status,
        "dataClassification": "REAL_PUBLIC_DATA",
    }


class BacklogReviewTests(unittest.TestCase):
    def test_existing_qualified_candidate_is_selected_once(self):
        record = {"candidate_id": "real-1", "candidate": candidate(status="outdated", sources=["https://source.example.invalid/a", "https://source.example.invalid/b"], phone="+234"), "evidence_event_ids": ["event-1", "event-1"]}
        result = review_backlog([record, record])
        self.assertEqual(result["status"], "QUALIFIED_OPPORTUNITY_SELECTED")
        self.assertEqual(result["selected_candidate"]["candidate_id"], "real-1")
        self.assertEqual(result["duplicate_candidate_ids"], ["real-1"])
        self.assertEqual(result["selected_candidate"]["evidence_event_ids"], ["event-1"])

    def test_cactus_style_unresolved_record_is_not_promoted(self):
        result = review_backlog([{"candidate_id": "osm-node-2330811676", "candidate": candidate(), "prior_decision": "NOT_YET_QUALIFIED", "evidence_event_ids": ["phase18"]}])
        self.assertEqual(result["status"], "NO_QUALIFIED_OPPORTUNITY_FOUND")
        self.assertEqual(result["reviews"][0]["decision"], "NOT_YET_QUALIFIED")

    def test_historical_ownership_block_vetoes_an_otherwise_qualified_input(self):
        record = {"candidate_id": "real-2", "candidate": candidate(status="basic", sources=["https://source.example.invalid/a", "https://source.example.invalid/b"], phone="+234"), "prior_decision": "NOT_YET_QUALIFIED"}
        result = review_backlog([record])
        self.assertEqual(result["status"], "NO_QUALIFIED_OPPORTUNITY_FOUND")
        self.assertEqual(result["reviews"][0]["assessment"]["status"], "QUALIFIED")
        self.assertFalse(result["reviews"][0]["selected"])

    def test_fictional_records_are_excluded_and_no_selection_creates_work(self):
        fictional = {"candidate_id": "test-1", "candidate": candidate(status="outdated", sources=["https://a.invalid", "https://b.invalid"], phone="+234")}
        fictional["candidate"]["dataClassification"] = "TEST_FIXTURE"
        result = review_backlog([fictional])
        self.assertEqual(result["status"], "NO_QUALIFIED_OPPORTUNITY_FOUND")
        self.assertEqual(result["reviews"], [])
        self.assertFalse(result["lead_created"])
        self.assertFalse(result["project_created"])
        self.assertFalse(result["build_started"])
        self.assertFalse(result["deployment_started"])
