"""Single-file, append-history tracking with guarded lifecycle transitions."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import json, re
from urllib.parse import urlparse

STAGES={"DISCOVERED","RESEARCHING","QUALIFIED","BUILDING","VALIDATING","READY_FOR_REVIEW","PREVIEW_DEPLOYED","HUMAN_REVIEW","READY_FOR_OUTREACH","DISQUALIFIED","FAILED","BLOCKED","PAUSED","ARCHIVED"}
TRANSITIONS={"DISCOVERED":{"RESEARCHING","DISQUALIFIED","BLOCKED","PAUSED"},"RESEARCHING":{"QUALIFIED","DISQUALIFIED","FAILED","BLOCKED"},"QUALIFIED":{"BUILDING","PAUSED","ARCHIVED"},"BUILDING":{"VALIDATING","FAILED","PAUSED"},"VALIDATING":{"READY_FOR_REVIEW","FAILED","BUILDING"},"READY_FOR_REVIEW":{"PREVIEW_DEPLOYED","HUMAN_REVIEW","BUILDING","PAUSED"},"PREVIEW_DEPLOYED":{"HUMAN_REVIEW","FAILED"},"HUMAN_REVIEW":{"READY_FOR_OUTREACH","BUILDING","PAUSED","ARCHIVED"},"READY_FOR_OUTREACH":{"ARCHIVED","PAUSED"},"FAILED":{"BUILDING","RESEARCHING","ARCHIVED"},"BLOCKED":{"RESEARCHING","BUILDING","PAUSED"},"PAUSED":{"RESEARCHING","BUILDING","ARCHIVED"}}

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def norm(v): return re.sub(r"[^a-z0-9]","",str(v).casefold()) if v else None
def domain(v): return urlparse(v).netloc.casefold() if v else None

class TrackingStore:
    def __init__(self,path=Path("data/tracking/state.json")):
        self.path=Path(path); self.state=self._load()
    def _load(self):
        if self.path.exists(): return json.loads(self.path.read_text(encoding="utf-8"))
        return {"leads":{},"projects":{},"builds":{},"validations":{},"deployments":{},"outreach":{},"jobs":{},"qualifications":{}}
    def _save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(self.state,indent=2)+"\n",encoding="utf-8")
    def find_duplicate(self,lead):
        for existing in self.state["leads"].values():
            exact=any(a and a==b for a,b in [(domain(lead.get("existing_website")),domain(existing.get("existing_website"))),(norm(lead.get("phone")),norm(existing.get("phone"))),(lead.get("instagram_url"),existing.get("instagram_url")),(lead.get("facebook_url"),existing.get("facebook_url"))])
            same_place=norm(lead.get("city"))==norm(existing.get("city")) and norm(lead.get("country"))==norm(existing.get("country"))
            same_name=norm(lead.get("business_name"))==norm(existing.get("business_name"))
            if exact: return {"match":"EXACT_MATCH","lead_id":existing["lead_id"]}
            if same_name and same_place: return {"match":"POSSIBLE_MATCH","lead_id":existing["lead_id"]}
        return {"match":"NO_MATCH","lead_id":None}
    def create_lead(self,lead):
        dup=self.find_duplicate(lead)
        if dup["match"]=="EXACT_MATCH": raise ValueError(f"Duplicate lead: {dup['lead_id']}")
        ident=f"lead-{len(self.state['leads'])+1:04d}"; record={**lead,"lead_id":ident,"created_at":now(),"updated_at":now(),"possible_duplicate_of":dup["lead_id"] if dup["match"]=="POSSIBLE_MATCH" else None}
        self.state["leads"][ident]=record; self._save(); return record
    def create_project(self,lead_id):
        lead=self.state["leads"].get(lead_id)
        if not lead or lead.get("qualification_status")!="QUALIFIED": raise ValueError("Only QUALIFIED leads can have projects.")
        existing=next((project for project in self.state["projects"].values() if project["lead_id"]==lead_id and project.get("project_status")=="ACTIVE"),None)
        if existing: return existing
        ident=f"project-{len(self.state['projects'])+1:04d}"; p={"project_id":ident,"lead_id":lead_id,"current_stage":"QUALIFIED","project_status":"ACTIVE","review_status":"PENDING","review_notes":None,"created_at":now(),"updated_at":now()}; self.state["projects"][ident]=p; self._save(); return p
    def record_qualification(self, qualification):
        lead=self.state["leads"].get(qualification.get("lead_id"))
        if not lead: raise ValueError("Qualification requires an existing lead.")
        ident=qualification.get("qualification_id") or f"qualification-{len(self.state['qualifications'])+1:04d}"
        if ident in self.state["qualifications"]: return self.state["qualifications"][ident]
        qualification["qualification_id"]=ident
        record={**qualification,"qualification_id":ident,"created_at":qualification.get("created_at") or now()}
        self.state["qualifications"][ident]=record; lead["latest_qualification_id"]=ident; lead["qualification_status"]=record["status"]; lead["updated_at"]=now(); self._save(); return record
    def transition(self,project_id,to):
        p=self.state["projects"].get(project_id)
        if not p or to not in TRANSITIONS.get(p["current_stage"],set()): raise ValueError("Illegal project state transition.")
        p["current_stage"]=to; p["updated_at"]=now(); self._save(); return p
    def record_build(self,project_id,build):
        p=self.state["projects"].get(project_id)
        if not p or p["current_stage"] not in {"QUALIFIED","BUILDING"}: raise ValueError("Project is not ready for a build.")
        self.transition(project_id,"BUILDING") if p["current_stage"]=="QUALIFIED" else None
        if build.get("lead_id")!=p["lead_id"]: raise ValueError("Build lead does not match project lead.")
        self.state["builds"][build["build_id"]]={**build,"project_id":project_id}; p["assigned_build_id"]=build["build_id"]; self.transition(project_id,"VALIDATING"); return self.state["builds"][build["build_id"]]
    def record_validation(self,project_id,validation):
        build=self.state["builds"].get(validation.get("build_id")); p=self.state["projects"].get(project_id)
        if not build or not p or build["project_id"]!=project_id: raise ValueError("Validation does not belong to this project build.")
        self.state["validations"][validation["validation_id"]]={**validation,"project_id":project_id}; p["latest_validation_id"]=validation["validation_id"]
        self.transition(project_id,"READY_FOR_REVIEW" if validation.get("status")=="PASS" else "FAILED"); return validation
    def record_deployment(self,project_id,deployment):
        p=self.state["projects"].get(project_id); build=self.state["builds"].get(deployment.get("build_id"))
        if not p or not build or build["project_id"]!=project_id or deployment.get("validation_status")!="PASS" or deployment.get("deployment_status")!="DEPLOYED": raise ValueError("Deployment must be a successful PASS deployment for this project build.")
        self.state["deployments"][deployment["deployment_id"]]={**deployment,"project_id":project_id}; p["latest_deployment_id"]=deployment["deployment_id"]; self.transition(project_id,"PREVIEW_DEPLOYED"); return deployment
    def record_review(self,project_id,status,notes=None):
        if status not in {"PENDING","APPROVED","REJECTED","CHANGES_REQUESTED"}: raise ValueError("Invalid review status.")
        p=self.state["projects"].get(project_id)
        if not p: raise ValueError("Unknown project.")
        if status=="APPROVED" and p["current_stage"]!="PREVIEW_DEPLOYED": raise ValueError("Approval requires a deployed preview.")
        p.update({"review_status":status,"review_notes":notes,"reviewed_at":now(),"updated_at":now()})
        if status=="CHANGES_REQUESTED": self.transition(project_id,"BUILDING")
        elif status=="APPROVED":
            self.transition(project_id,"HUMAN_REVIEW"); self.transition(project_id,"READY_FOR_OUTREACH")
        self._save(); return p
    def snapshot(self,project_id):
        p=self.state["projects"].get(project_id)
        if not p: raise ValueError("Unknown project.")
        lead=self.state["leads"][p["lead_id"]]; b=self.state["builds"].get(p.get("assigned_build_id")); v=self.state["validations"].get(p.get("latest_validation_id")); d=self.state["deployments"].get(p.get("latest_deployment_id")); return {"lead":lead,"project":p,"latestBuild":b,"latestValidation":v,"latestDeployment":d,"next_action":self._next(p)}
    def _next(self,p):
        return {"QUALIFIED":"Build website","BUILDING":"Complete build","VALIDATING":"Fix validation errors","READY_FOR_REVIEW":"Review preview","PREVIEW_DEPLOYED":"Complete human review","HUMAN_REVIEW":"Approve or request changes","READY_FOR_OUTREACH":"Approve outreach","FAILED":"Investigate failure"}.get(p["current_stage"],"Review project state")
    def query_projects(self,**filters): return [p for p in self.state["projects"].values() if all(p.get(k)==v for k,v in filters.items())]
