import tempfile, unittest
from pathlib import Path
from tracking.store import TrackingStore

class TrackingTests(unittest.TestCase):
 def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.s=TrackingStore(Path(self.tmp.name)/"state.json"); self.lead=self.s.create_lead({"business_name":"Fictional Studio","city":"Port Harcourt","country":"Nigeria","existing_website":"https://studio.example.invalid","qualification_status":"QUALIFIED"}); self.p=self.s.create_project(self.lead["lead_id"])
 def tearDown(self): self.tmp.cleanup()
 def build(self,i): return {"build_id":f"build-{i}","lead_id":self.lead["lead_id"],"status":"COMPLETED"}
 def test_duplicate_and_possible_detection(self):
  self.assertEqual(self.s.find_duplicate({"business_name":"Other","existing_website":"https://studio.example.invalid"})["match"],"EXACT_MATCH")
  self.assertEqual(self.s.find_duplicate({"business_name":"Fictional Studio","city":"Port Harcourt","country":"Nigeria"})["match"],"POSSIBLE_MATCH")
 def test_history_validation_deployment_review_snapshot(self):
  self.s.record_build(self.p["project_id"],self.build(1)); self.s.record_validation(self.p["project_id"],{"validation_id":"val-1","build_id":"build-1","status":"PASS"})
  self.s.record_deployment(self.p["project_id"],{"deployment_id":"dep-1","build_id":"build-1","validation_status":"PASS","deployment_status":"DEPLOYED"})
  self.s.record_review(self.p["project_id"],"APPROVED","fictional approval"); snap=self.s.snapshot(self.p["project_id"])
  self.assertEqual(snap["project"]["current_stage"],"READY_FOR_OUTREACH"); self.assertEqual(snap["latestDeployment"]["deployment_id"],"dep-1")
 def test_two_builds_and_failed_validation_preserved(self):
  self.s.record_build(self.p["project_id"],self.build(1)); self.s.record_validation(self.p["project_id"],{"validation_id":"val-1","build_id":"build-1","status":"FAIL"})
  self.s.transition(self.p["project_id"],"BUILDING"); self.s.record_build(self.p["project_id"],self.build(2)); self.s.record_validation(self.p["project_id"],{"validation_id":"val-2","build_id":"build-2","status":"PASS"})
  self.assertEqual(len(self.s.state["builds"]),2); self.assertEqual(len(self.s.state["validations"]),2)
 def test_illegal_transition_and_unqualified_project_blocked(self):
  with self.assertRaises(ValueError): self.s.transition(self.p["project_id"],"READY_FOR_OUTREACH")
  bad=self.s.create_lead({"business_name":"Bad","qualification_status":"SKIP"})
  with self.assertRaises(ValueError): self.s.create_project(bad["lead_id"])
if __name__=="__main__": unittest.main()
