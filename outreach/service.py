"""Prepare and review outreach drafts; sending is intentionally absent."""
from datetime import datetime, timezone
import re

from tracking.store import next_numeric_id

CHANNELS={"EMAIL","WHATSAPP","INSTAGRAM","FACEBOOK","LINKEDIN","SMS","OTHER"}
UNSAFE=("award-winning","guaranteed","customers","revenue","years in business","number one","certified")
def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

class OutreachService:
 def __init__(self,store): self.store=store; self.store.state.setdefault("outreach",{})
 def readiness(self,project_id):
  s=self.store.snapshot(project_id); p=s["project"]; assigned=p.get("assigned_build_id")
  if s["lead"].get("qualification_status")!="QUALIFIED": return "NOT_QUALIFIED"
  if not s["latestBuild"] or s["latestBuild"].get("status")=="FAILED": return "NO_BUILD"
  if not s["latestValidation"] or s["latestValidation"].get("build_id")!=assigned or s["latestValidation"].get("status")!="PASS": return "VALIDATION_FAILED"
  if not s["latestDeployment"] or s["latestDeployment"].get("build_id")!=assigned or s["latestDeployment"].get("deployment_status")!="DEPLOYED" or not s["latestDeployment"].get("deployment_url"): return "NO_PREVIEW"
  if p.get("review_status")!="APPROVED" or p["current_stage"]!="READY_FOR_OUTREACH" or (p.get("approved_build_id") and p.get("approved_build_id")!=assigned) or (p.get("approved_action") and p.get("approved_action")!="OUTREACH_PREPARATION"): return "AWAITING_HUMAN_REVIEW"
  return "READY"
 def create(self,project_id,channel="EMAIL",tone="professional",regenerate=False):
  if channel not in CHANNELS: raise ValueError("Unsupported outreach channel.")
  ready=self.readiness(project_id)
  if ready!="READY": raise ValueError(f"Outreach preparation blocked: {ready}")
  s=self.store.snapshot(project_id); lead=s["lead"]; dep=s["latestDeployment"]
  existing=[x for x in self.store.state["outreach"].values() if x["project_id"]==project_id and x["channel"]==channel and x.get("build_id")==dep["build_id"] and x.get("deployment_id")==dep["deployment_id"]]
  if existing and not regenerate: return existing[-1]
  evidence=self._evidence(lead)
  if not evidence: raise ValueError("INSUFFICIENT_RESEARCH")
  ident=next_numeric_id(self.store.state.get("outreach") or {}, "outreach"); body=self._draft(lead,dep["deployment_url"],channel,tone,evidence)
  self._safe(body,lead["business_name"],dep["deployment_url"])
  item={"outreach_id":ident,"project_id":project_id,"lead_id":lead["lead_id"],"build_id":dep["build_id"],"deployment_id":dep["deployment_id"],"channel":channel,"recipient":self._contact(lead,channel),"subject":f"A website preview for {lead['business_name']}" if channel=="EMAIL" else None,"draft_message":body,"personalization_points":evidence,"preview_url":dep["deployment_url"],"status":"AWAITING_REVIEW","generated_at":now(),"review_notes":None,"revision_of":existing[-1]["outreach_id"] if existing else None}
  self.store.state["outreach"][ident]=item; self.store._save(); return item
 def review(self,outreach_id,status,notes=None):
  x=self.store.state["outreach"].get(outreach_id)
  if not x or status not in {"APPROVED","REJECTED","CHANGES_REQUESTED"}: raise ValueError("Invalid outreach review.")
  self._safe(x["draft_message"],self.store.state["leads"][x["lead_id"]]["business_name"],x["preview_url"])
  x.update({"status":"READY_TO_SEND" if status=="APPROVED" else status,"review_notes":notes,"reviewed_at":now()}); self.store._save(); return x
 def _evidence(self,l):
  e=[]
  if l.get("website_status") in {"NO_WEBSITE","WEBSITE_POOR","WEBSITE_OUTDATED","WEBSITE_NEEDS_IMPROVEMENT","WEBSITE_BASIC"}: e.append({"claim":"Public research identified a website opportunity.","source_urls":l.get("research_source_urls") or l.get("source_urls",[]),"confidence":"high"})
  if l.get("business_type"): e.append({"claim":f"Business category: {l['business_type']}.","source_urls":l.get("source_urls",[]),"confidence":"medium"})
  return e
 def _draft(self,l,url,channel,tone,evidence):
  opening=f"Hi {l['business_name']},"
  observation="I noticed from your public information that there is an opportunity to strengthen your online presence."
  preview=f"I prepared a website concept for review: {url}"
  close="If it is useful, I would be glad to discuss it."
  return " ".join((opening,observation,preview,close)) if channel=="EMAIL" else " ".join((opening,observation,preview,close))
 def _contact(self,l,c): return l.get("email") if c=="EMAIL" else l.get("whatsapp") if c=="WHATSAPP" else l.get("instagram_url") if c=="INSTAGRAM" else l.get("facebook_url") if c=="FACEBOOK" else None
 def _safe(self,text,name,url):
  if name not in text or url not in text or any(x in text.casefold() for x in UNSAFE) or not url.startswith("https://"): raise ValueError("Outreach safety review blocked draft.")
