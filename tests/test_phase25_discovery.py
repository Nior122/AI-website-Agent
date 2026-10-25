import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opportunities.backlog import review_backlog
from opportunities.service import assess_opportunity
from tracking.store import TrackingStore
from scout.scout_agent import OverpassRestaurantSource, Target


class Phase25DiscoveryAndQualificationTests(unittest.TestCase):
    def test_previously_investigated_candidates_not_selected_without_new_evidence(self):
        """Historical candidates with prior decisions are safely vetoed."""
        historical_cactus = {
            "candidate_id": "osm-node-2330811676",
            "candidate": {
                "businessName": "Cactus Restaurant",
                "niche": "Restaurants",
                "location": {"city": "Lagos", "country": "Nigeria"},
                "sourceUrls": ["https://cactuslagos.com/"],
                "websiteStatus": "unknown",
                "dataClassification": "REAL_PUBLIC_DATA",
            },
            "prior_decision": "NOT_YET_QUALIFIED",
            "evidence_event_ids": ["phase15", "phase18"],
        }
        res = review_backlog([historical_cactus])
        self.assertEqual(res["status"], "NO_QUALIFIED_OPPORTUNITY_FOUND")
        self.assertFalse(res["reviews"][0]["selected"])
        self.assertEqual(res["reviews"][0]["decision"], "NOT_YET_QUALIFIED")

    def test_stable_candidate_ids_and_branch_identity_preserved(self):
        """Candidate IDs and branch addresses are accurately preserved."""
        c1 = {
            "candidate_id": "phase25-search-yellow-chilli-vi",
            "candidate": {
                "businessName": "Yellow Chilli",
                "niche": "Restaurants",
                "location": {
                    "city": "Lagos",
                    "country": "Nigeria",
                    "address": "27 Oju-Olobun Close, Victoria Island",
                },
                "sourceUrls": ["https://yellowchilling.com/"],
                "websiteStatus": "professional",
                "dataClassification": "REAL_PUBLIC_DATA",
            },
        }
        c2 = {
            "candidate_id": "phase25-search-yellow-chilli-ikeja",
            "candidate": {
                "businessName": "Yellow Chilli",
                "niche": "Restaurants",
                "location": {
                    "city": "Lagos",
                    "country": "Nigeria",
                    "address": "35 Joel Ogunnaike St, GRA Ikeja",
                },
                "sourceUrls": ["https://yellowchilling.com/"],
                "websiteStatus": "professional",
                "dataClassification": "REAL_PUBLIC_DATA",
            },
        }
        res = review_backlog([c1, c2])
        reviewed_ids = [r["candidate_id"] for r in res["reviews"]]
        self.assertIn("phase25-search-yellow-chilli-vi", reviewed_ids)
        self.assertIn("phase25-search-yellow-chilli-ikeja", reviewed_ids)
        self.assertEqual(len(res["duplicate_candidate_ids"]), 0)

    def test_duplicate_discovery_results_do_not_create_duplicate_leads(self):
        """Duplicate candidates by website or phone trigger duplicate detection."""
        with tempfile.TemporaryDirectory() as td:
            store = TrackingStore(Path(td) / "state.json")
            lead1 = {
                "business_name": "Pitstop Lagos",
                "city": "Lagos",
                "country": "Nigeria",
                "existing_website": "https://pitstoplagos.com/",
                "phone": "+234 817 999 9005",
                "qualification_status": "QUALIFIED",
            }
            created = store.create_lead(lead1)
            self.assertEqual(created["lead_id"], "lead-0001")

            dup = store.find_duplicate({
                "business_name": "Pitstop Lagos",
                "city": "Lagos",
                "country": "Nigeria",
                "existing_website": "https://pitstoplagos.com/home",
            })
            self.assertEqual(dup["match"], "EXACT_MATCH")
            self.assertEqual(dup["lead_id"], "lead-0001")

            with self.assertRaises(ValueError):
                store.create_lead(lead1)

    def test_unknown_website_status_not_treated_as_confirmed_absence(self):
        """Missing or unverified website does not infer NO_WEBSITE or defect."""
        candidate = {
            "businessName": "Independent Cafe",
            "niche": "Restaurants",
            "location": {"city": "Lagos", "country": "Nigeria"},
            "websiteStatus": "none_known",
            "sourceUrls": ["https://osm.org/node/123"],
            "dataClassification": "REAL_PUBLIC_DATA",
        }
        qual = assess_opportunity(candidate)
        self.assertEqual(qual["website_status"], "WEBSITE_UNKNOWN")
        self.assertEqual(qual["status"], "NEEDS_REVIEW")
        self.assertNotEqual(qual["website_status"], "NO_WEBSITE")

    def test_unreachable_or_third_party_ordering_does_not_prove_ownership_or_defect(self):
        """Third-party ordering or lack of defect results in NOT_YET_QUALIFIED / NEEDS_REVIEW."""
        candidate = {
            "businessName": "Platform Restaurant",
            "niche": "Restaurants",
            "location": {"city": "Lagos", "country": "Nigeria"},
            "websiteStatus": "unknown",
            "sourceUrls": [
                "https://order.foodplatform.invalid/restaurant",
                "https://instagram.com/restaurant",
            ],
            "contact": {"phone": "+234 800 000 0000"},
            "dataClassification": "REAL_PUBLIC_DATA",
        }
        qual = assess_opportunity(candidate)
        self.assertEqual(qual["status"], "NEEDS_REVIEW")
        self.assertEqual(qual["website_status"], "WEBSITE_UNKNOWN")

    def test_unsupported_website_quality_claims_cannot_force_qualification(self):
        """Subjective dislikes or professional sites cannot force qualification."""
        candidate = {
            "businessName": "Healthy Website Restaurant",
            "niche": "Restaurants",
            "location": {"city": "Lagos", "country": "Nigeria"},
            "websiteStatus": "professional",
            "sourceUrls": [
                "https://healthyrestaurant.example.invalid/",
                "https://instagram.com/healthyrestaurant",
            ],
            "contact": {"phone": "+234 800 000 0000"},
            "dataClassification": "REAL_PUBLIC_DATA",
        }
        qual = assess_opportunity(candidate)
        self.assertEqual(qual["status"], "NEEDS_REVIEW")
        self.assertEqual(qual["website_status"], "WEBSITE_MODERN")

    def test_qualified_lead_requires_appropriate_evidence_and_defect(self):
        """A candidate with verified data, >= 2 sources, and source-backed defect qualifies."""
        candidate = {
            "businessName": "Pitstop Lagos",
            "niche": "Restaurants",
            "location": {
                "city": "Lagos",
                "country": "Nigeria",
                "address": "92b Younis Bashorun Street, Victoria Island",
            },
            "contact": {"phone": "+234 817 999 9005", "email": "info@pitstoplagos.com"},
            "social": {"instagramUrl": "https://www.instagram.com/pitstoplagos/"},
            "publicBusinessInformation": {
                "description": "Wellness and cycling themed cafe",
                "servicesOrProducts": ["Dine-in", "Takeaway"],
            },
            "websiteStatus": "poor",
            "sourceUrls": [
                "https://pitstoplagos.com/",
                "https://www.instagram.com/pitstoplagos/",
            ],
            "dataClassification": "REAL_PUBLIC_DATA",
        }
        qual = assess_opportunity(candidate, lead_id="lead-0002")
        self.assertEqual(qual["status"], "QUALIFIED")
        self.assertEqual(qual["website_status"], "WEBSITE_POOR")
        self.assertGreaterEqual(qual["score"], 60)
        self.assertEqual(qual["confidence"], "high")
        self.assertEqual(len(qual["content_brief"]["unknown"]), 0)

    def test_unqualified_candidates_do_not_create_active_website_projects(self):
        """Only QUALIFIED leads can have projects created in TrackingStore."""
        with tempfile.TemporaryDirectory() as td:
            store = TrackingStore(Path(td) / "state.json")
            unqualified_lead = store.create_lead({
                "business_name": "Unqualified Restaurant",
                "city": "Lagos",
                "country": "Nigeria",
                "qualification_status": "NEEDS_REVIEW",
            })
            with self.assertRaisesRegex(ValueError, "Only QUALIFIED leads"):
                store.create_project(unqualified_lead["lead_id"])

    def test_fictional_test_data_remains_separate_from_real_records(self):
        """TEST_FIXTURE records are skipped by review_backlog and separated in TrackingStore."""
        fictional_record = {
            "candidate_id": "fictional-candidate",
            "candidate": {
                "businessName": "Fictional Restaurant",
                "dataClassification": "TEST_FIXTURE",
            },
        }
        res = review_backlog([fictional_record])
        self.assertEqual(res["reviews"], [])
        self.assertEqual(res["status"], "NO_QUALIFIED_OPPORTUNITY_FOUND")

    def test_phase19_and_phase20_audit_records_remain_intact(self):
        """Phase 19, 20, and 25 audit documents are verified when present and never fabricated on fresh checkouts."""
        p19 = Path("data/leads/phase19-lagos-restaurants-backlog-review-20261010.json")
        p20 = Path("data/leads/phase20-calabar-kitchen-website-verification-20261011.json")
        p25 = Path("data/leads/phase25-lagos-restaurants-discovery-and-qualification-20261012.json")
        store = TrackingStore()
        lead2 = store.state["leads"]["lead-0002"]
        qual2 = store.state["qualifications"]["qualification-0002"]
        self.assertEqual(lead2["business_name"], "Pitstop Lagos")
        self.assertEqual(lead2["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(lead2["qualification_status"], "QUALIFIED")
        self.assertEqual(qual2["score"], 95)
        self.assertEqual(qual2["priority"], "P2")
        if p19.exists() and p20.exists() and p25.exists():
            d19 = json.loads(p19.read_text(encoding="utf-8"))
            d20 = json.loads(p20.read_text(encoding="utf-8"))
            d25 = json.loads(p25.read_text(encoding="utf-8"))

            self.assertEqual(d19["data_classification"], "REAL_PUBLIC_DATA")
            self.assertEqual(d20["data_classification"], "REAL_PUBLIC_DATA")
            self.assertEqual(d25["data_classification"], "REAL_PUBLIC_DATA")
            self.assertEqual(d25["result"]["qualification_result"], "QUALIFIED_OPPORTUNITY_FOUND")
            self.assertEqual(d25["result"]["selected_candidate_id"], "phase25-search-pitstop-lagos")
        else:
            ws_check = store.check_local_state()
            self.assertIn(p19.as_posix(), ws_check["irrecoverable_gitignored_audit_files"])
            self.assertIn(p20.as_posix(), ws_check["irrecoverable_gitignored_audit_files"])
            self.assertIn(p25.as_posix(), ws_check["irrecoverable_gitignored_audit_files"])

    def test_fictional_preview_deployment_and_project0001_remain_unaffected(self):
        """Lead-0001 and project-0001 maintain their deployed preview / review status."""
        store = TrackingStore()
        lead1 = store.state["leads"]["lead-0001"]
        proj1 = store.state["projects"]["project-0001"]
        self.assertEqual(lead1["data_classification"], "TEST_FIXTURE")
        self.assertEqual(proj1["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(proj1["review_status"], "PENDING")

    def test_overpass_fallback_endpoints_handle_transient_failure(self):
        """Overpass fallback endpoint is tried safely when primary fails."""
        source = OverpassRestaurantSource(
            endpoint="https://failing-endpoint.invalid/interpreter",
            fallback_endpoints=["https://working-endpoint.invalid/interpreter"],
            max_requests=2,
            min_delay_seconds=0,
        )
        payload = {"elements": [{"type": "node", "id": 999, "tags": {"name": "Fallback Restaurant", "amenity": "restaurant"}}]}
        
        class MockResp:
            status = 200
            def read(self): return json.dumps(payload).encode()
            def __enter__(self): return self
            def __exit__(self, *args): return False

        call_urls = []
        def mock_urlopen(req, timeout):
            call_urls.append(req.full_url)
            if "failing-endpoint" in req.full_url:
                raise TimeoutError("Simulated primary endpoint timeout")
            return MockResp()

        with patch("scout.scout_agent.urlopen", side_effect=mock_urlopen):
            res = source.discover(Target("Nigeria", "Lagos", "Restaurants", lead_limit=5))
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["business_name"], "Fallback Restaurant")
            self.assertEqual(len(call_urls), 2)
            self.assertIn("failing-endpoint", call_urls[0])
            self.assertIn("working-endpoint", call_urls[1])


if __name__ == "__main__":
    unittest.main()
