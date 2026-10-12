import tempfile, unittest
from pathlib import Path
from tracking.store import TrackingStore
from outreach.service import OutreachService
class OutreachTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory(); self.s=TrackingStore(Path(self.t.name)/"s.json"); self.l=self.s.create_lead({"business_name":"Fictional Studio","business_type":"Beauty salon","qualification_status":"QUALIFIED","website_status":"NO_WEBSITE","source_urls":["https://x.invalid"],"research_source_urls":["https://x.invalid"],"email":"hello@example.invalid"}); self.p=self.s.create_project(self.l["lead_id"]); self.o=OutreachService(self.s)
 def tearDown(self): self.t.cleanup()
 def ready(self):
  self.s.record_build(self.p["project_id"],{"build_id":"b1","lead_id":self.l["lead_id"]}); self.s.record_validation(self.p["project_id"],{"validation_id":"v1","build_id":"b1","status":"PASS"}); self.s.record_deployment(self.p["project_id"],{"deployment_id":"d1","build_id":"b1","validation_status":"PASS","deployment_status":"DEPLOYED","deployment_url":"https://fictional.vercel.app"}); self.s.record_review(self.p["project_id"],"APPROVED")
 def test_eligibility_and_revision_history(self):
  with self.assertRaisesRegex(ValueError,"NO_BUILD"): self.o.create(self.p["project_id"])
  self.ready(); a=self.o.create(self.p["project_id"]); b=self.o.create(self.p["project_id"],regenerate=True); self.assertEqual(b["revision_of"],a["outreach_id"])
 def test_review_and_safety(self):
  self.ready(); x=self.o.create(self.p["project_id"],"EMAIL"); self.assertEqual(self.o.review(x["outreach_id"],"APPROVED")["status"],"READY_TO_SEND")
  x["draft_message"]="Award-winning Fictional Studio https://fictional.vercel.app"
  with self.assertRaises(ValueError): self.o.review(x["outreach_id"],"APPROVED")
