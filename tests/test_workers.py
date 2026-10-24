from pathlib import Path
import tempfile, unittest
from orchestration.jobs import JobService, WorkerResult
from orchestration.workers import WorkerRegistry
from tracking.store import TrackingStore

class WorkerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.s=TrackingStore(Path(self.tmp.name)/"state.json"); self.j=JobService(self.s,max_attempts=2); self.r=WorkerRegistry(self.s,lease_seconds=30,heartbeat_seconds=60,preferences={"BUILD":["a","b"]}); self.l=self.s.create_lead({"business_name":"Fictional","qualification_status":"QUALIFIED"}); self.p=self.s.create_project(self.l["lead_id"])
  self.r.register("a",name="A",worker_type="LOCAL_AGENT",capabilities=["BUILD"],priority=1); self.r.register("b",name="B",worker_type="REMOTE_AGENT",capabilities=["BUILD"],priority=2); self.r.register("human",name="Human",worker_type="HUMAN",capabilities=["BUILD"])
 def tearDown(self): self.tmp.cleanup()
 def job(self,i="build",build=None): return self.j.create("BUILD",job_id=i,lead_id=self.l["lead_id"],project_id=self.p["project_id"],build_id=build)
 def test_router_disabled_busy_and_claim_exclusive(self):
  x=self.job(); self.assertEqual(self.r.candidates(x)[0]["worker_id"],"a"); claimed=self.r.claim("build","a"); self.assertEqual(claimed["status"],"RUNNING")
  with self.assertRaises(ValueError): self.r.claim("build","b")
  self.r.report("build","a",claimed["lease"]["lease_id"],WorkerResult("COMPLETED",summary="done")); self.r.set_enabled("b",False); self.assertNotIn("b",[x["worker_id"] for x in self.r.candidates(self.job("build2"))])
 def test_failover_retry_normalized_history_and_deterministic_stop(self):
  self.job(); x=self.r.claim("build","a"); self.r.report("build","a",x["lease"]["lease_id"],WorkerResult("FAILED",error="network",retryable=True,error_type="TRANSIENT")); self.assertEqual(self.s.state["jobs"]["build"]["status"],"QUEUED")
  y=self.r.claim("build","b"); self.r.report("build","b",y["lease"]["lease_id"],WorkerResult("COMPLETED",summary="done",artifacts=["output/a"],commit_sha="abc")); self.assertEqual(self.s.state["jobs"]["build"]["status"],"COMPLETED"); self.assertEqual(len(self.s.state["executions"]),2)
  bad=self.job("bad"); z=self.r.claim("bad","a"); self.r.report("bad","a",z["lease"]["lease_id"],WorkerResult("FAILED",error="invalid",error_type="DETERMINISTIC")); self.assertEqual(self.s.state["jobs"]["bad"]["status"],"FAILED")
 def test_lease_renewal_recovery_lock_version_and_human_takeover(self):
  self.job(); x=self.r.claim("build","a"); self.r.renew_lease("build","a",x["lease"]["lease_id"]); self.assertEqual(self.s.state["jobs"]["build"]["status"],"RUNNING")
  other=self.job("other")
  with self.assertRaises(ValueError): self.r.claim("other","b")
  self.s.state["jobs"]["build"]["lease"]["expires_at"]="2000-01-01T00:00:00Z"; self.r.recover_expired(); self.assertEqual(self.s.state["jobs"]["build"]["status"],"QUEUED")
  h=self.r.human_takeover("build","human"); self.assertEqual(h["lease"]["worker_id"],"human")
  self.s.state["projects"][self.p["project_id"]]["assigned_build_id"]="newer"; stale=self.job("stale",build="old")
  with self.assertRaises(ValueError): self.r.claim("stale","b")
if __name__=="__main__": unittest.main()
