"""Fictional, local end-to-end lifecycle. It never contacts or deploys to a real service."""
import copy, json, tempfile, unittest
from pathlib import Path
from builder.website_builder import WebsiteBuilder
from deployment.deployer import VercelDeploymentProvider
from orchestration.health import readiness_for, system_health
from orchestration.jobs import JobService
from orchestration.workers import WorkerRegistry
from outreach.service import OutreachService
from tracking.store import TrackingStore
from validation.quality_validator import validate_rendered_html
from scout.scout_agent import Scout, Target, FixtureSource
from research.research_agent import ResearchAgent, FixtureResearchSource

FIXTURE=Path(__file__).parent/"fixtures"/"qualified_leads.json"

class FullSystemTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.store=TrackingStore(self.root/"state.json"); self.jobs=JobService(self.store); self.registry=WorkerRegistry(self.store); self.registry.register("fictional-builder",name="Fictional test worker",capabilities=["BUILD"],worker_type="OTHER")
 def tearDown(self): self.tmp.cleanup()
 def test_fictional_lifecycle_stops_at_ready_to_send(self):
  candidates=[{"business_name":"Lagos Fresh Kitchen (TEST / FICTIONAL)","niche":"Restaurant","city":"Lagos","country":"Nigeria","phone":"+2348000000099","whatsapp":"+2348000000099","instagram_url":"https://instagram.example.invalid/lagos-fresh-kitchen","services":["Rice bowls","Grilled fish"],"description":"Fictional restaurant integration fixture.","opening_hours":"09:00-18:00","source_url":"https://fixture.example.invalid/lagos-fresh-kitchen"}]
  evidence=[{"candidate_name":"Lagos Fresh Kitchen (TEST / FICTIONAL)","business_name":"Lagos Fresh Kitchen (TEST / FICTIONAL)","city":"Lagos","country":"Nigeria","phone":"+2348000000099","instagram_url":"https://instagram.example.invalid/lagos-fresh-kitchen","services":["Rice bowls","Grilled fish"],"description":"Fictional restaurant integration fixture.","activity_signal":"fictional public fixture","source_url":"https://fixture.example.invalid/lagos-fresh-kitchen"}]
  candidate_path=self.root/"candidate.json"; evidence_path=self.root/"evidence.json"; candidate_path.write_text(json.dumps(candidates)); evidence_path.write_text(json.dumps(evidence))
  scout=Scout(Target("Nigeria","Lagos","Restaurant",lead_limit=1),[FixtureSource(candidate_path)]).run(); self.assertEqual(scout.status,"completed")
  researched=ResearchAgent([FixtureResearchSource(evidence_path)]).run(scout.leads[0]); self.assertEqual(researched.record["research"]["qualificationStatus"],"QUALIFIED")
  research=researched.record; research["lead_id"]="fictional-lagos-fresh-kitchen"; research["demo"]=True
  build=WebsiteBuilder(self.root/"output").build(research); self.assertEqual(build.status,"COMPLETED")
  html=Path(build.output_location).read_text(); validation=validate_rendered_html(html,"Lagos Fresh Kitchen (TEST / FICTIONAL)",real_build=False,build_id=build.build_id,lead_id=build.lead_id).to_dict(); self.assertEqual(validation["status"],"PASS")
  lead=self.store.create_lead({"business_name":"Lagos Fresh Kitchen (TEST / FICTIONAL)","business_type":"Restaurant","city":"Lagos","country":"Nigeria","qualification_status":"QUALIFIED","website_status":"NO_WEBSITE","source_urls":["https://fixture.example.invalid/lagos-fresh-kitchen"],"research_source_urls":["https://fixture.example.invalid/lagos-fresh-kitchen"],"phone":"+2348000000099","whatsapp":"+2348000000099"})
  project=self.store.create_project(lead["lead_id"]); job=self.jobs.create("BUILD",job_id="fictional-build",lead_id=lead["lead_id"],project_id=project["project_id"],build_id=build.build_id)
  self.assertEqual(readiness_for(self.store,self.jobs,"fictional-build",self.registry)["status"],"READY")
  tracked_build=build.to_dict(); tracked_build["lead_id"]=lead["lead_id"]
  self.store.record_build(project["project_id"],tracked_build); self.store.record_validation(project["project_id"],validation)
  # This is a fictional preview record, not a Vercel request or deployment.
  self.store.record_deployment(project["project_id"],{"deployment_id":"fictional-preview","build_id":build.build_id,"validation_status":"PASS","deployment_status":"DEPLOYED","deployment_url":"https://fictional-preview.example.invalid"})
  self.store.record_review(project["project_id"],"APPROVED","fictional human review")
  draft=OutreachService(self.store).create(project["project_id"],"WHATSAPP"); approved=OutreachService(self.store).review(draft["outreach_id"],"APPROVED")
  self.assertEqual(approved["status"],"READY_TO_SEND"); self.assertNotIn("send",OutreachService.__dict__)
  self.assertEqual(self.store.snapshot(project["project_id"])["latestDeployment"]["build_id"],build.build_id)
 def test_health_and_readiness_report_missing_external_configuration(self):
  job=self.jobs.create("SCOUT",job_id="health-check"); health=system_health(self.store,self.registry,repository_root=self.root)
  self.assertEqual(health["application"]["status"],"healthy"); self.assertEqual(health["deployment"]["status"],"degraded"); self.assertEqual(readiness_for(self.store,self.jobs,job["job_id"],self.registry)["status"],"NOT_READY")
if __name__=="__main__": unittest.main()
