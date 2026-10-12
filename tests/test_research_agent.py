import copy
import json
from pathlib import Path
import unittest

from research.research_agent import FixtureResearchSource, ResearchAgent


ROOT = Path(__file__).parent
CANDIDATES = json.loads((ROOT / "fixtures" / "scout_candidates.json").read_text(encoding="utf-8"))
EVIDENCE = ROOT / "fixtures" / "research_evidence.json"


def candidate(name):
    source = next(item for item in CANDIDATES if item["business_name"] == name)
    return {
        "businessName": source["business_name"], "niche": source["niche"],
        "location": {"city": source["city"], "country": source["country"], "address": None},
        "contact": {"phone": source.get("phone"), "email": None, "whatsapp": None},
        "originalWebsite": source.get("website"),
        "social": {"instagramUrl": source.get("instagram_url"), "facebookUrl": source.get("facebook_url"), "otherSocialUrls": []},
        "sourceUrls": [source["source_url"]],
        "fieldSources": {"businessName": [source["source_url"]], "niche": [source["source_url"]], "location": [source["source_url"]], "contact": [source["source_url"]], "social": [source["source_url"]], "publicBusinessInformation": [source["source_url"]]},
        "publicBusinessInformation": {"servicesOrProducts": source.get("services", []), "description": source.get("description"), "openingHours": source.get("opening_hours")},
    }


class BrokenSource:
    name = "broken_fixture"

    def research(self, candidate):
        raise RuntimeError("intentional fixture failure")


class ResearchAgentTests(unittest.TestCase):
    def run_research(self, name):
        return ResearchAgent([FixtureResearchSource(EVIDENCE)]).run(candidate(name))

    def test_social_first_business_is_qualified_with_provenance(self):
        result = self.run_research("Fictional Beauty by Ada")
        research = result.record["research"]
        self.assertEqual(result.status, "completed")
        self.assertEqual(research["identity"]["status"], "verified")
        self.assertEqual(research["websiteEvaluation"]["status"], "NO_WEBSITE")
        self.assertEqual(research["qualificationStatus"], "QUALIFIED")
        self.assertTrue(all(fact["sourceUrls"] for fact in research["verifiedFacts"]))

    def test_observed_website_problem_is_qualified(self):
        research = self.run_research("Fictional Glow Studio").record["research"]
        self.assertEqual(research["websiteEvaluation"]["status"], "WEBSITE_NEEDS_IMPROVEMENT")
        self.assertEqual(research["qualificationStatus"], "QUALIFIED")
        self.assertIn("poor_mobile", " ".join(research["websiteEvaluation"]["observations"]))

    def test_modern_website_is_skipped(self):
        research = self.run_research("Fictional Modern Salon").record["research"]
        self.assertEqual(research["websiteEvaluation"]["status"], "WEBSITE_MODERN")
        self.assertEqual(research["qualificationStatus"], "SKIP")

    def test_mismatched_identity_is_not_merged(self):
        research = self.run_research("Fictional Unverified Salon").record["research"]
        self.assertEqual(research["identity"]["status"], "mismatch")
        self.assertEqual(research["qualificationStatus"], "SKIP")
        self.assertEqual(research["researchSources"], [])

    def test_source_failure_is_recoverable(self):
        result = ResearchAgent([FixtureResearchSource(EVIDENCE), BrokenSource()]).run(candidate("Fictional Beauty by Ada"))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.record["research"]["qualificationStatus"], "REVIEW")
        self.assertIn("intentional fixture failure", result.errors[0])

    def test_all_source_failure_is_recorded_as_failed(self):
        result = ResearchAgent([BrokenSource()]).run(candidate("Fictional Beauty by Ada"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.record["research"]["status"], "failed")
        self.assertEqual(result.record["research"]["qualificationStatus"], "REVIEW")

    def test_result_is_json_serializable(self):
        json.dumps(self.run_research("Fictional Beauty by Ada").to_dict())


if __name__ == "__main__":
    unittest.main()
