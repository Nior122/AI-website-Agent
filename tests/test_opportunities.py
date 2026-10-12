import unittest, tempfile
from pathlib import Path
from opportunities.service import assess_opportunity
from tracking.store import TrackingStore

def candidate(**changes):
 base={"businessName":"Fictional Restaurant","niche":"Restaurants","location":{"city":"Lagos","country":"Nigeria"},"contact":{},"social":{"instagramUrl":None,"facebookUrl":None,"otherSocialUrls":[]},"publicBusinessInformation":{"description":"Fictional restaurant","servicesOrProducts":[],"openingHours":None},"sourceUrls":["https://fixture.example.invalid/a"],"websiteStatus":"none_known","dataClassification":"TEST_FIXTURE"}; base.update(changes); return base

class OpportunityTests(unittest.TestCase):
 def test_single_directory_source_is_review_not_false_no_website_claim(self):
  x=assess_opportunity(candidate()); self.assertEqual(x["status"],"NEEDS_REVIEW"); self.assertEqual(x["website_status"],"WEBSITE_UNKNOWN"); self.assertIn("CONTACT_INFORMATION_NEEDED",x["content_brief"]["contact_ctas"])
 def test_strong_source_backed_outdated_site_qualifies(self):
  x=assess_opportunity(candidate(websiteStatus="outdated",sourceUrls=["https://fixture.example.invalid/a","https://fixture.example.invalid/b"],contact={"phone":"+234","whatsapp":None,"email":None})); self.assertEqual(x["status"],"QUALIFIED"); self.assertEqual(x["priority"],"P2")
 def test_site_without_a_demonstrated_deficiency_does_not_qualify(self):
  x=assess_opportunity(candidate(websiteStatus="unknown",sourceUrls=["https://fixture.example.invalid/a","https://fixture.example.invalid/b"],contact={"phone":"+234","whatsapp":None,"email":None})); self.assertEqual(x["status"],"NEEDS_REVIEW")
 def test_history_and_active_project_creation_are_idempotent(self):
  temp=tempfile.TemporaryDirectory(); store=TrackingStore(Path(temp.name)/"s.json"); lead=store.create_lead({"business_name":"Fictional","qualification_status":"NEEDS_REVIEW"}); q=assess_opportunity(candidate(),lead_id=lead["lead_id"]); store.record_qualification(q); self.assertEqual(store.state["qualifications"][q["qualification_id"]]["status"],"NEEDS_REVIEW")
  lead["qualification_status"]="QUALIFIED"; first=store.create_project(lead["lead_id"]); self.assertEqual(first["project_id"],store.create_project(lead["lead_id"])["project_id"]); temp.cleanup()
