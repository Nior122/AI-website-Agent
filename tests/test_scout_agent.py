import json
from pathlib import Path
import unittest

from scout.scout_agent import FixtureSource, Scout, Target


FIXTURE = Path(__file__).parent / "fixtures" / "scout_candidates.json"


class ScoutAgentTests(unittest.TestCase):
    def setUp(self):
        self.target = Target("Nigeria", "Port Harcourt", "Beauty salons", lead_limit=10)
        self.result = Scout(self.target, [FixtureSource(FIXTURE)]).run()

    def test_fixture_run_is_completed_and_offline(self):
        self.assertEqual(self.result.status, "completed")
        self.assertEqual(self.result.candidate_count, 5)
        self.assertEqual(self.result.deduplicated_count, 4)

    def test_duplicate_sources_are_merged_and_retained(self):
        ada = next(lead for lead in self.result.leads if lead["normalizedBusinessName"] == "fictionalbeautybyada")
        self.assertEqual(len(ada["sourceUrls"]), 3)
        self.assertEqual(ada["qualificationStatus"], "qualified")
        self.assertEqual(ada["websiteStatus"], "none_known")

    def test_qualification_categories_are_explainable(self):
        by_name = {lead["businessName"]: lead for lead in self.result.leads}
        self.assertEqual(by_name["Fictional Glow Studio"]["qualificationStatus"], "qualified")
        self.assertEqual(by_name["Fictional Modern Salon"]["qualificationStatus"], "skipped")
        self.assertEqual(by_name["Fictional Unverified Salon"]["qualificationStatus"], "skipped")
        self.assertIn("Insufficient", by_name["Fictional Unverified Salon"]["qualificationReason"])

    def test_result_is_json_serializable(self):
        json.dumps(self.result.to_dict())


if __name__ == "__main__":
    unittest.main()
