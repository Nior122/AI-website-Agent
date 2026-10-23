"""Prepare and review outreach drafts; sending is intentionally absent.

One record per project and channel holds the recipient, subject, body, and the evidence the wording rests on, plus
two digests: ``content_fingerprint`` (the text as generated) and ``approved_content_fingerprint`` (the text a
human approved). ``review`` is the only writer of an approval and ``readiness`` the only gate on drafting -
:meth:`orchestration.outbound.ApprovedOutboundSender` reads both and writes neither. ``_safe`` keeps fabricated
claims and non-https preview links out of a message that a stranger will read.

This module still cannot send. Recording ``READY_TO_SEND`` means a person approved specific content; it does not
put anything in anybody's mailbox, and it is not what the sender checks *alone* - the sender re-reads the lead, the
hold, the contact budget, the opt-out list, and this digest before it contacts a provider.
"""
from datetime import datetime, timezone
import re

from tracking.store import next_numeric_id

#: The fields that decide what a recipient would read. Deliberately excludes status and review metadata: an
#: approval covers *content*, and a status transition must not look like an edit (or an edit like a transition).
CONTENT_KEYS=("outreach_id","lead_id","project_id","build_id","deployment_id","channel","recipient","subject","preview_url","draft_message")
def content_fingerprint(record):
  """Hash the approved content. See telegram_bot.draft_fingerprint for the record-state checksum used by buttons.

  A caller cannot forge a match by re-hashing: the point is staleness, not authorization. Storing the digest at
  review time means "this human approved these words" survives any later edit of the same record.
  """
  import hashlib
  material="\x1f".join(str((record or {}).get(key) or "") for key in CONTENT_KEYS)
  return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

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
  ident=next_numeric_id(self.store.state.get("outreach") or {}, "outreach")
  generation_metadata={"provider":"deterministic-fallback","prompt_version":"deterministic-v1"}
  body=self._draft(lead,dep["deployment_url"],channel,tone,evidence)
  import os
  if channel=="EMAIL" and os.environ.get("AI_WEBSITE_AGENT_EMAIL_COPYWRITER_ENABLED","").strip().lower() in {"1","true","yes","on"}:
   from outreach.copywriter import MarketingEmailWriter
   generated=MarketingEmailWriter().generate(lead,dep["deployment_url"],evidence)
   body=generated["body"]; generation_metadata=generated["metadata"]
  self._safe(body,lead["business_name"],dep["deployment_url"])
  item={"outreach_id":ident,"project_id":project_id,"lead_id":lead["lead_id"],"build_id":dep["build_id"],"deployment_id":dep["deployment_id"],"channel":channel,"recipient":self._contact(lead,channel),"subject":f"A website preview for {lead['business_name']}" if channel=="EMAIL" else None,"draft_message":body,"personalization_points":evidence,"preview_url":dep["deployment_url"],"generation_metadata":generation_metadata,"status":"AWAITING_REVIEW","generated_at":now(),"review_notes":None,"content_fingerprint":None,"approved_content_fingerprint":None,"revision_of":existing[-1]["outreach_id"] if existing else None}
  item["content_fingerprint"]=content_fingerprint(item); self.store.state["outreach"][ident]=item; self.store._save(); return item
 def review(self,outreach_id,status,notes=None,reviewed_by=None,identity_source=None):
  """Record a human's decision on one draft. `reviewed_by` says who, `identity_source` says how we know.

  Attribution is stored beside the decision because "an operator approved this" is only auditable if the record
  says which operator and through what surface; it is never used to widen what the approval permits, and a
  missing attribution is not a reason to invent one.
  """
  x=self.store.state["outreach"].get(outreach_id)
  if not x or status not in {"APPROVED","REJECTED","CHANGES_REQUESTED"}: raise ValueError("Invalid outreach review.")
  self._safe(x["draft_message"],self.store.state["leads"][x["lead_id"]]["business_name"],x["preview_url"])
  who=str(reviewed_by).strip()[:120] if reviewed_by else None
  via=str(identity_source).strip()[:60] if identity_source else None
  # The approval is bound to the content it covered. A later change to the recipient, subject, body, or preview
  # URL makes the stored digest disagree with the record, which is what "stale" means for a send: not a new
  # opinion, a different message.
  x.update({"status":"READY_TO_SEND" if status=="APPROVED" else status,"review_notes":notes,"reviewed_at":now(),"reviewed_by":who,"reviewer_identity_source":via,"content_fingerprint":content_fingerprint(x),"approved_content_fingerprint":(content_fingerprint(x) if status=="APPROVED" else None)}); self.store._save(); return x
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
