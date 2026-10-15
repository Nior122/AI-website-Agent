"""Provider-neutral worker registry, routing, leases, execution audit, and mock worker."""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from .jobs import JOB_TYPES, OPERATOR_ATTENTION_STATUSES, WorkerResult, _now, next_action_for
from .observability import log_event, recorded_duration_seconds, sanitize_diagnostic

WORKER_TYPES={"LOCAL_AGENT","REMOTE_AGENT","API_AGENT","HUMAN","OTHER"}
WORKER_STATES={"OFFLINE","STARTING","IDLE","BUSY","DEGRADED","ERROR","DISABLED","UNKNOWN"}
FAILURE_TYPES={"TRANSIENT","RETRYABLE","DETERMINISTIC","SECURITY","HUMAN_REVIEW"}
MUTATING={"BUILD","DEPLOY","PREPARE_OUTREACH"}

def _parse(value): return datetime.fromisoformat(value.replace("Z","+00:00"))

class WorkerRegistry:
    """Persists worker metadata; records never contain credentials or provider secrets."""
    def __init__(self, store, *, lease_seconds=300, heartbeat_seconds=90, preferences=None):
        self.store=store; self.lease_seconds=lease_seconds; self.heartbeat_seconds=heartbeat_seconds; self.preferences=preferences or {}
        for key in ("workers","executions","project_locks"): self.store.state.setdefault(key,{})
    def _refresh(self):
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()
    def register(self, worker_id, *, name, worker_type="OTHER", capabilities, priority=0, enabled=True, cost_class="FREE", configuration=None):
        self._refresh()
        if worker_type not in WORKER_TYPES or not set(capabilities) <= JOB_TYPES: raise ValueError("Invalid worker type or capability.")
        old=self.store.state["workers"].get(worker_id); stamp=_now()
        record={"worker_id":worker_id,"name":name,"type":worker_type,"capabilities":sorted(set(capabilities)),"priority":priority,"enabled":enabled,"cost_class":cost_class,"status":"IDLE" if enabled else "DISABLED","health_status":"AVAILABLE" if enabled else "UNAVAILABLE","last_heartbeat":stamp if enabled else None,"current_job_id":None,"last_successful_job":None,"last_failed_job":None,"last_error":None,"configuration":configuration or {},"created_at":old.get("created_at",stamp) if old else stamp,"updated_at":stamp}
        if old and old.get("current_job_id"): raise ValueError("Cannot replace a busy worker record.")
        self.store.state["workers"][worker_id]=record; self.store._save(); return record
    def heartbeat(self, worker_id, *, status="IDLE", health_status="AVAILABLE"):
        self._refresh()
        worker=self._worker(worker_id); worker.update({"status":status,"health_status":health_status,"last_heartbeat":_now(),"updated_at":_now()}); self.store._save(); return worker
    def set_enabled(self, worker_id, enabled):
        self._refresh()
        worker=self._worker(worker_id)
        if worker.get("current_job_id"): raise ValueError("Release the active job before disabling a worker.")
        worker.update({"enabled":enabled,"status":"IDLE" if enabled else "DISABLED","health_status":"AVAILABLE" if enabled else "UNAVAILABLE","updated_at":_now()}); self.store._save(); return worker
    def candidates(self, job, *, exclude=()):
        self._refresh()
        required=job["job_type"]; now=datetime.now(timezone.utc)
        items=[]
        for w in self.store.state["workers"].values():
            fresh=w.get("last_heartbeat") and (now-_parse(w["last_heartbeat"])).total_seconds() <= self.heartbeat_seconds
            if w["worker_id"] not in exclude and w["enabled"] and fresh and w["status"]=="IDLE" and w["health_status"]=="AVAILABLE" and required in w["capabilities"]: items.append(w)
        preferred=self.preferences.get(required,[])
        return sorted(items,key=lambda w:(preferred.index(w["worker_id"]) if w["worker_id"] in preferred else 999,-w["priority"],w["worker_id"]))
    def claim(self, job_id, worker_id):
        self._refresh()
        job=self.store.state["jobs"].get(job_id); worker=self._worker(worker_id)
        if not job or job["status"] not in {"PENDING","QUEUED","BLOCKED"}: raise ValueError("Job is not claimable.")
        if worker not in self.candidates(job): raise ValueError("Worker is not eligible to claim this job.")
        if job.get("project_id") and job["job_type"] in MUTATING:
            lock=self.store.state["project_locks"].get(job["project_id"])
            if lock and _parse(lock["expires_at"])>datetime.now(timezone.utc): raise ValueError("Project is locked by another mutating job.")
        if job["job_type"] in {"BUILD","VALIDATE","DEPLOY","DEPLOY_PREVIEW"} and job.get("build_id") and job["project_id"]:
            p=self.store.state["projects"].get(job["project_id"])
            if not p or (job["job_type"]!="BUILD" and p.get("assigned_build_id")!=job["build_id"]) or (job["job_type"]=="BUILD" and p.get("assigned_build_id") and p.get("assigned_build_id")!=job["build_id"]): raise ValueError("Job build version is stale for this project.")
        stamp=_now(); expiry=(datetime.now(timezone.utc)+timedelta(seconds=self.lease_seconds)).replace(microsecond=0).isoformat().replace("+00:00","Z"); lease=f"lease-{uuid4().hex}"
        job.update({"status":"RUNNING","attempts":job["attempts"]+1,"started_at":job.get("started_at") or stamp,"updated_at":stamp,"heartbeat_at":stamp,"lease":{"lease_id":lease,"worker_id":worker_id,"claimed_at":stamp,"heartbeat_at":stamp,"expires_at":expiry}})
        worker.update({"status":"BUSY","current_job_id":job_id,"updated_at":stamp})
        if job.get("project_id") and job["job_type"] in MUTATING: self.store.state["project_locks"][job["project_id"]]={"project_id":job["project_id"],"job_id":job_id,"worker_id":worker_id,"lease_id":lease,"expires_at":expiry}
        eid=f"execution-{uuid4().hex}"; self.store.state["executions"][eid]={"execution_id":eid,"job_id":job_id,"worker_id":worker_id,"attempt_number":job["attempts"],"status":"RUNNING","started_at":stamp,"completed_at":None,"result":None,"error_type":None,"error_message":None,"artifacts":[],"commit_sha":None,"logs_reference":None,"created_at":stamp}; job["execution_id"]=eid; self.store._save(); return job
    def renew_lease(self, job_id, worker_id, lease_id):
        self._refresh()
        job=self.store.state["jobs"].get(job_id); lease=(job or {}).get("lease",{})
        if lease.get("worker_id")!=worker_id or lease.get("lease_id")!=lease_id: raise ValueError("Invalid worker lease.")
        expiry=(datetime.now(timezone.utc)+timedelta(seconds=self.lease_seconds)).replace(microsecond=0).isoformat().replace("+00:00","Z"); lease.update({"heartbeat_at":_now(),"expires_at":expiry}); job["heartbeat_at"]=_now()
        lock=self.store.state["project_locks"].get(job.get("project_id"));
        if lock and lock["lease_id"]==lease_id: lock["expires_at"]=expiry
        self.store._save(); return job
    def report(self, job_id, worker_id, lease_id, result:WorkerResult):
        self._refresh()
        job=self.store.state["jobs"].get(job_id); lease=(job or {}).get("lease",{})
        if lease.get("worker_id")!=worker_id or lease.get("lease_id")!=lease_id: raise ValueError("Stale or unauthorized result rejected.")
        worker=self._worker(worker_id); execution=self.store.state["executions"][job["execution_id"]]; status=result.status
        detail=sanitize_diagnostic(result.error)
        # `errors` is diagnostic text a worker may have copied from a provider response, so it is
        # redacted before it is stored on the job and echoed by the API.
        normalized={**(result.output or {}),"status":status,"summary":result.summary,"artifacts":result.artifacts,"warnings":[sanitize_diagnostic(item) for item in (result.warnings or [])],"errors":[detail] if result.error else [],"metadata":result.metadata,"commit_sha":result.commit_sha}
        final="COMPLETED" if status=="COMPLETED" else "NEEDS_REVIEW" if status in {"NEEDS_REVIEW","PARTIAL"} or result.error_type=="HUMAN_REVIEW" else "QUEUED" if result.retryable and job["attempts"]<job["max_attempts"] else "FAILED"
        next_types=[]
        if final=="COMPLETED":
            from .jobs import NEXT_TYPES
            nxt=NEXT_TYPES.get(job["job_type"])
            if nxt and not (job["job_type"]=="VALIDATE" and normalized.get("validation_status")!="PASS"):
                next_types=[nxt]
        # Same coded, sanitized failure vocabulary as the job service, so a worker-reported fault and a
        # service-run fault are readable and searchable identically.
        code=None if final=="COMPLETED" else (result.error_code or ("TRANSIENT_FAILURE" if final=="QUEUED" else "WORKER_FAILED"))
        stamp=_now()
        job.update({"status":final,"output":normalized,"artifacts":result.artifacts,"error":detail,"next_job_types":next_types,
                    "error_code":code,"next_action":next_action_for(code,project_id=job.get("project_id"),build_id=job.get("build_id"),job_type=job.get("job_type")),
                    "requires_operator_action":final in OPERATOR_ATTENTION_STATUSES or (final=="FAILED" and job["attempts"]>=job["max_attempts"]),
                    "updated_at":stamp,"completed_at":stamp if final!="QUEUED" else None,"lease":None})
        if final!="COMPLETED":
            job["failure"]={"code":code,"message":detail,"stage":final,"attempt":int(job.get("attempts") or 0),"recorded_at":stamp}
        else:
            job["failure"]=None
        execution.update({"status":final,"completed_at":stamp,"duration_seconds":recorded_duration_seconds(execution.get("started_at"),stamp),
                          "result":normalized,"error_type":result.error_type,"error_code":code,"error_message":detail,
                          "next_action":job.get("next_action"),"artifacts":result.artifacts,"commit_sha":result.commit_sha})
        worker.update({"status":"IDLE","current_job_id":None,"updated_at":stamp,"last_successful_job":job_id if final=="COMPLETED" else worker.get("last_successful_job"),"last_failed_job":job_id if final in {"FAILED","NEEDS_REVIEW"} else worker.get("last_failed_job"),"last_error":detail})
        if final!="COMPLETED":
            log_event(severity="ERROR" if final=="FAILED" else "WARNING",operation=f"worker.{job.get('job_type','unknown').lower()}",code=code,
                      message=detail,job_id=job_id,lead_id=job.get("lead_id"),project_id=job.get("project_id"),build_id=job.get("build_id"),
                      execution_id=job.get("execution_id"),worker_id=worker_id,attempt=job.get("attempts"))
        self._unlock(job); self.store._save(); return job
    def recover_expired(self):
        self._refresh()
        now=datetime.now(timezone.utc); recovered=[]
        for job in list(self.store.state["jobs"].values()):
            lease=job.get("lease") or {}
            if job.get("status")=="RUNNING" and lease.get("expires_at") and _parse(lease["expires_at"])<now:
                worker=self._worker(lease["worker_id"]); worker.update({"status":"DEGRADED","health_status":"UNAVAILABLE","current_job_id":None,"last_error":"Lease expired."})
                code="LEASE_EXPIRED"; detail="Worker lease expired before the result was reported."
                job.update({"status":"QUEUED" if job["attempts"]<job["max_attempts"] else "NEEDS_REVIEW","error":detail,"error_code":code,
                            "next_action":next_action_for(code,project_id=job.get("project_id"),job_type=job.get("job_type")),
                            "requires_operator_action":job["attempts"]>=job["max_attempts"],"lease":None,"updated_at":_now()})
                execution=(self.store.state.get("executions") or {}).get(job.get("execution_id") or "")
                if isinstance(execution,dict) and execution.get("status")=="RUNNING":
                    execution.update({"status":"NEEDS_REVIEW" if job["status"]=="NEEDS_REVIEW" else "QUEUED","completed_at":_now(),
                                      "error_code":code,"error_message":detail,"next_action":job.get("next_action")})
                log_event(severity="WARNING",operation="worker.lease",code=code,message=detail,job_id=job.get("job_id"),
                          worker_id=lease.get("worker_id"),project_id=job.get("project_id"),attempt=job.get("attempts"))
                self._unlock(job); recovered.append(job)
        self.store._save(); return recovered
    def human_takeover(self, job_id, worker_id):
        self._refresh()
        worker=self._worker(worker_id)
        if worker["type"]!="HUMAN": raise ValueError("Human takeover requires a HUMAN worker.")
        job=self.store.state["jobs"].get(job_id); job["status"]="QUEUED"; job["error"]=None; self.store._save(); return self.claim(job_id,worker_id)
    def _unlock(self,job):
        lock=self.store.state["project_locks"].get(job.get("project_id"))
        if lock and lock.get("job_id")==job["job_id"]: del self.store.state["project_locks"][job["project_id"]]
    def _worker(self,worker_id):
        if worker_id not in self.store.state["workers"]: raise ValueError("Unknown worker.")
        return self.store.state["workers"][worker_id]

class MockWorker:
    """Deterministic local worker for success, failures, timeout, unavailable, and review tests."""
    def __init__(self, results): self.results=list(results)
    def execute(self, job, context): return self.results.pop(0) if self.results else WorkerResult("COMPLETED",summary="mock success")


class LocalPipelineWorker:
    """Provider-neutral local application worker executing SCOUT ->PREPARE_OUTREACH against TrackingStore."""
    name = "local-pipeline-worker"

    def __init__(self, *, repository_root=".", output_root="output", deployment_provider=None):
        from pathlib import Path
        self.repository_root = Path(repository_root)
        self.output_root = Path(output_root)
        self.deployment_provider = deployment_provider

    def execute(self, job: dict[str, Any], context: dict[str, Any]) -> WorkerResult:
        from pathlib import Path
        from builder.validator import validate_html
        from builder.website_builder import WebsiteBuilder, _is_fixture, _verified_template_data
        from deployment.deployer import VercelDeploymentProvider
        from opportunities.service import assess_opportunity
        from outreach.service import OutreachService
        from research.research_agent import FixtureResearchSource, ResearchAgent
        from scout.scout_agent import FixtureSource, Scout, Target

        store = context["tracking_store"]
        job_type = job["job_type"]
        job_input = job.get("input") or {}

        if job_type == "SCOUT":
            source_file = job_input.get("source_file")
            if not source_file:
                return WorkerResult("FAILED", error="SCOUT job requires input.source_file.", retryable=False)
            target = Target(
                country=str(job_input.get("country") or "Nigeria"),
                city=str(job_input.get("city") or "Lagos"),
                niche=str(job_input.get("niche") or "Restaurants"),
                lead_limit=int(job_input.get("lead_limit") or 5),
                dry_run=bool(job.get("dry_run", True)),
            )
            scout_res = Scout(target, [FixtureSource(Path(source_file))]).run()
            if scout_res.status != "completed":
                return WorkerResult("FAILED", error="; ".join(scout_res.errors) or "Scout run failed.", retryable=False)
            lead_ids: list[str] = []
            new_lead_ids: list[str] = []
            duplicate_lead_ids: list[str] = []
            candidate_results: list[dict[str, Any]] = []
            existing_project_ids: list[str] = []
            already_ready_flags: list[bool] = []
            if job_input.get("create_leads", True):
                for cand in scout_res.leads:
                    loc = cand.get("location") or {}
                    contact = cand.get("contact") or {}
                    social = cand.get("social") or {}
                    classification = cand.get("dataClassification") or "TEST_FIXTURE"
                    lead_payload = {
                        "business_name": cand.get("businessName"),
                        "business_type": cand.get("niche"),
                        "city": loc.get("city"),
                        "country": loc.get("country"),
                        "address": None if classification == "REAL_PUBLIC_DATA" else loc.get("address"),
                        "existing_website": cand.get("originalWebsite"),
                        "qualification_status": "DISCOVERED",
                        "website_status": cand.get("websiteStatus") or "UNKNOWN",
                        "source_urls": list(cand.get("sourceUrls") or []),
                        "phone": None if classification == "REAL_PUBLIC_DATA" else contact.get("phone"),
                        "email": None if classification == "REAL_PUBLIC_DATA" else contact.get("email"),
                        "whatsapp": None if classification == "REAL_PUBLIC_DATA" else contact.get("whatsapp"),
                        "instagram_url": None if classification == "REAL_PUBLIC_DATA" else social.get("instagramUrl"),
                        "facebook_url": None if classification == "REAL_PUBLIC_DATA" else social.get("facebookUrl"),
                        "data_classification": classification,
                        "demo": classification == "TEST_FIXTURE",
                        "discovered_candidate": cand,
                    }
                    dup = store.find_duplicate(lead_payload)
                    is_dup = dup["match"] == "EXACT_MATCH" or (
                        dup["match"] == "POSSIBLE_MATCH"
                        and store.state["leads"][dup["lead_id"]].get("data_classification") == classification
                    )
                    if is_dup:
                        lead_rec = store.state["leads"][dup["lead_id"]]
                        lead_rec["discovered_candidate"] = cand
                        store._save()
                        duplicate_lead_ids.append(lead_rec["lead_id"])
                    else:
                        lead_rec = store.create_lead(lead_payload)
                        new_lead_ids.append(lead_rec["lead_id"])
                    existing_proj = next(
                        (
                            p
                            for p in store.state["projects"].values()
                            if p["lead_id"] == lead_rec["lead_id"] and p.get("project_status") == "ACTIVE"
                        ),
                        None,
                    )
                    is_ready = bool(
                        existing_proj
                        and existing_proj.get("current_stage") in {"READY_FOR_REVIEW", "PREVIEW_DEPLOYED", "HUMAN_REVIEW", "READY_FOR_OUTREACH"}
                        and existing_proj.get("assigned_build_id")
                    )
                    if existing_proj:
                        existing_project_ids.append(existing_proj["project_id"])
                    already_ready_flags.append(is_ready)
                    candidate_results.append({
                        "candidate_id": cand.get("candidateId"),
                        "business_name": cand.get("businessName"),
                        "duplicate_match": dup["match"],
                        "duplicate_of": dup["lead_id"] if is_dup else None,
                        "is_duplicate": is_dup,
                        "lead_id": lead_rec["lead_id"],
                        "existing_project_id": existing_proj["project_id"] if existing_proj else None,
                        "already_ready_for_review": is_ready,
                    })
                    lead_ids.append(lead_rec["lead_id"])
            first_lead_id = lead_ids[0] if lead_ids else None
            all_already_ready = bool(already_ready_flags) and all(already_ready_flags)
            all_duplicates = bool(lead_ids) and len(duplicate_lead_ids) == len(lead_ids)
            scout_outcome = (
                "NO_CANDIDATES"
                if not lead_ids
                else "DUPLICATE_ALREADY_READY_FOR_REVIEW"
                if all_already_ready
                else "DUPLICATE_BUSINESS"
                if (all_duplicates and job_input.get("skip_existing_duplicates"))
                else "DISCOVERED"
            )
            return WorkerResult(
                "COMPLETED",
                summary=f"Discovered {len(scout_res.leads)} candidate(s).",
                output={
                    "lead_id": first_lead_id,
                    "lead_ids": lead_ids,
                    "new_lead_ids": new_lead_ids,
                    "duplicate_lead_ids": duplicate_lead_ids,
                    "duplicate_count": len(duplicate_lead_ids),
                    "duplicate_detected": len(duplicate_lead_ids) > 0,
                    "duplicate_already_ready": all_already_ready,
                    "existing_project_id": existing_project_ids[0] if existing_project_ids else None,
                    "candidate_results": candidate_results,
                    "candidates": scout_res.leads,
                    "candidate_count": len(scout_res.leads),
                    "outcome": scout_outcome,
                },
            )

        if job_type == "RESEARCH":
            lead = store.state["leads"].get(job.get("lead_id"))
            candidate = job_input.get("candidate") or (lead or {}).get("discovered_candidate")
            if not candidate and lead:
                candidate = {
                    "candidateId": lead["lead_id"],
                    "dataClassification": lead.get("data_classification", "TEST_FIXTURE"),
                    "businessName": lead.get("business_name"),
                    "niche": lead.get("business_type"),
                    "location": {"city": lead.get("city"), "country": lead.get("country"), "address": lead.get("address")},
                    "contact": {"phone": lead.get("phone"), "email": lead.get("email"), "whatsapp": lead.get("whatsapp")},
                    "social": {"instagramUrl": lead.get("instagram_url"), "facebookUrl": lead.get("facebook_url"), "otherSocialUrls": []},
                    "publicBusinessInformation": {"description": None, "servicesOrProducts": [], "openingHours": None},
                    "websiteStatus": lead.get("website_status") or "unknown",
                    "originalWebsite": lead.get("existing_website"),
                    "sourceUrls": list(lead.get("source_urls") or []),
                }
            evidence_file = job_input.get("evidence_file")
            if not candidate or not evidence_file:
                return WorkerResult("FAILED", error="RESEARCH job requires candidate and input.evidence_file.", retryable=False)
            res = ResearchAgent([FixtureResearchSource(Path(evidence_file))]).run(candidate)
            if res.status != "completed":
                return WorkerResult(
                    "FAILED",
                    error="; ".join(res.errors) or "Research failed.",
                    output={"lead_id": job.get("lead_id"), "outcome": "RESEARCH_FAILED"},
                    retryable=bool(job_input.get("retryable", False)),
                )
            assessment = assess_opportunity(res.record, lead_id=lead["lead_id"] if lead else None, job_id=job["job_id"])
            ingested = store.ingest_research_record(
                res.record,
                assessment=assessment,
                lead_id=lead["lead_id"] if lead else job.get("lead_id"),
            )
            job["lead_id"] = ingested["lead_id"]
            r_obj = res.record.get("research") or {}
            r_qual = r_obj.get("qualificationStatus") or ingested.get("qualification_status")
            r_web_status = (r_obj.get("websiteEvaluation") or {}).get("status")
            r_sources = r_obj.get("researchSources") or []
            res_outcome = (
                "QUALIFIED_FOR_ASSESSMENT"
                if r_qual == "QUALIFIED"
                else "UNQUALIFIED_OPPORTUNITY"
                if (r_qual in {"SKIP_MODERN_WEBSITE", "DISQUALIFIED"} or (r_qual == "SKIP" and r_web_status == "WEBSITE_MODERN"))
                else "INSUFFICIENT_RESEARCH_EVIDENCE"
                if (r_qual == "INSUFFICIENT_EVIDENCE" or not r_sources)
                else "RESEARCH_REQUIRES_ADDITIONAL_WORK"
            )
            return WorkerResult(
                "COMPLETED",
                summary=f"Researched lead {ingested['lead_id']} ({ingested['qualification_status']}).",
                output={
                    "lead_id": ingested["lead_id"],
                    "qualification_id": ingested.get("latest_qualification_id"),
                    "qualification_status": ingested["qualification_status"],
                    "research_qualification_status": r_qual,
                    "website_status": ingested["website_status"],
                    "outcome": res_outcome,
                },
            )

        if job_type == "QUALIFY":
            lead = store.state["leads"].get(job.get("lead_id"))
            if not lead:
                return WorkerResult("FAILED", error="QUALIFY job requires an existing lead.", retryable=False)
            record = lead.get("research_record") or lead.get("discovered_candidate") or {
                "businessName": lead.get("business_name"),
                "niche": lead.get("business_type"),
                "location": {"city": lead.get("city"), "country": lead.get("country")},
                "contact": {"phone": lead.get("phone"), "email": lead.get("email"), "whatsapp": lead.get("whatsapp")},
                "social": {"instagramUrl": lead.get("instagram_url"), "facebookUrl": lead.get("facebook_url")},
                "sourceUrls": list(lead.get("source_urls") or []),
                "websiteStatus": lead.get("website_status") or "unknown",
                "dataClassification": lead.get("data_classification", "TEST_FIXTURE"),
            }
            assessment = assess_opportunity(record, lead_id=lead["lead_id"], job_id=job["job_id"])
            if isinstance(lead.get("research_record"), dict):
                r_obj = lead["research_record"].get("research") or {}
                r_status = r_obj.get("qualificationStatus")
                r_web = (r_obj.get("websiteEvaluation") or {}).get("status")
                r_sources = r_obj.get("researchSources") or []
                if r_status == "QUALIFIED":
                    assessment["status"] = "QUALIFIED"
                elif r_status in {"SKIP_MODERN_WEBSITE", "DISQUALIFIED"} or (r_status == "SKIP" and r_web == "WEBSITE_MODERN"):
                    assessment["status"] = "DISQUALIFIED"
                elif r_status == "INSUFFICIENT_EVIDENCE" or not r_sources or (r_obj.get("identity") or {}).get("status") == "mismatch":
                    assessment["status"] = "INSUFFICIENT_EVIDENCE"
                elif r_status == "REVIEW" and assessment["status"] == "QUALIFIED":
                    assessment["status"] = "NEEDS_REVIEW"
            qual = store.record_qualification(assessment)
            project_id = job.get("project_id")
            if qual["status"] == "QUALIFIED" and job_input.get("create_project", True):
                proj = store.create_project(lead["lead_id"])
                project_id = proj["project_id"]
                job["project_id"] = project_id
            qual_outcome = (
                "QUALIFIED"
                if qual["status"] == "QUALIFIED"
                else "INSUFFICIENT_RESEARCH_EVIDENCE"
                if qual["status"] == "INSUFFICIENT_EVIDENCE"
                else "UNQUALIFIED_OPPORTUNITY"
                if qual["status"] == "DISQUALIFIED"
                else "RESEARCH_REQUIRES_ADDITIONAL_WORK"
            )
            return WorkerResult(
                "COMPLETED" if qual["status"] == "QUALIFIED" else "NEEDS_REVIEW",
                summary=f"Qualification for {lead['lead_id']}: {qual['status']}.",
                output={
                    "lead_id": lead["lead_id"],
                    "project_id": project_id,
                    "qualification_id": qual["qualification_id"],
                    "qualification_status": qual["status"],
                    "outcome": qual_outcome,
                },
            )

        if job_type == "BUILD":
            lead_id = job["lead_id"]
            project_id = job.get("project_id")
            if not project_id:
                proj = store.create_project(lead_id)
                project_id = proj["project_id"]
                job["project_id"] = project_id
            payload = store.build_payload(lead_id)
            builder = WebsiteBuilder(output_root=self.output_root)
            build_res = builder.build(payload)
            store.record_build(project_id, build_res.to_dict())
            job["build_id"] = build_res.build_id
            if build_res.status != "COMPLETED":
                return WorkerResult(
                    "FAILED",
                    error="; ".join(build_res.errors) or "Website build failed.",
                    output={
                        "lead_id": lead_id,
                        "project_id": project_id,
                        "build_id": build_res.build_id,
                        "outcome": "BUILD_FAILURE",
                        "errors": build_res.errors,
                    },
                    retryable=False,
                )
            return WorkerResult(
                "COMPLETED",
                summary=f"Generated website build {build_res.build_id} for {project_id}.",
                output={
                    "lead_id": lead_id,
                    "project_id": project_id,
                    "build_id": build_res.build_id,
                    "template_id": build_res.template_id,
                    "output_location": build_res.output_location,
                    "outcome": "BUILD_COMPLETED",
                },
                artifacts=[build_res.output_location] if build_res.output_location else [],
            )

        if job_type == "VALIDATE":
            project_id = job["project_id"]
            p = store.state["projects"][project_id]
            build_id = job.get("build_id") or p.get("assigned_build_id")
            build = store.state["builds"][build_id]
            if not build.get("output_location"):
                return WorkerResult("FAILED", error=f"Build {build_id} has no output_location.", retryable=False)
            out_path = Path(build["output_location"])
            if not out_path.is_file() and build_id in (store.state.get("build_artifacts") or {}):
                try:
                    store.get_or_materialize_build_artifact(
                        build_id,
                        repository_root=self.repository_root,
                        verify_hash=True,
                    )
                except ValueError as err:
                    return WorkerResult("FAILED", error=str(err), retryable=False)
            if not out_path.is_file():
                return WorkerResult("FAILED", error=f"Build artifact does not exist: {out_path}", retryable=False)
            html = out_path.read_text(encoding="utf-8")
            payload = store.build_payload(p["lead_id"])
            facts = _verified_template_data(payload)
            is_fix = _is_fixture(payload)
            validation = validate_html(
                html,
                facts["business_name"],
                real_build=not is_fix,
                build_id=build_id,
                lead_id=p["lead_id"],
                expected_contact={"phone": facts.get("phone"), "whatsapp": facts.get("whatsapp"), "email": facts.get("email")},
                expected_facts=None if is_fix else facts,
                synthetic_contact=is_fix,
            )
            expected_sha = build.get("artifact_sha256")
            if expected_sha:
                import hashlib
                actual_sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
                if actual_sha != expected_sha:
                    validation["status"] = "FAIL"
                    validation["passed"] = False
                    validation.setdefault("findings", []).append({
                        "severity": "ERROR",
                        "code": "ARTIFACT_SHA256_MISMATCH",
                        "message": f"Build artifact SHA-256 mismatch for {build_id}: expected {expected_sha}, got {actual_sha}.",
                    })
            validation["artifact_path"] = str(out_path)
            recorded_val = store.record_validation(project_id, validation)
            v_status = recorded_val["status"]
            return WorkerResult(
                "COMPLETED" if v_status == "PASS" else ("NEEDS_REVIEW" if v_status == "NEEDS_REVIEW" else "FAILED"),
                summary=f"Validated {build_id}: {v_status} (score {recorded_val.get('score')}).",
                output={
                    "lead_id": p["lead_id"],
                    "project_id": project_id,
                    "build_id": build_id,
                    "validation_id": recorded_val["validation_id"],
                    "validation_status": v_status,
                    "score": recorded_val.get("score"),
                    "outcome": "VALIDATION_PASSED_READY_FOR_REVIEW" if v_status == "PASS" else "VALIDATION_FAILURE",
                },
                error=None if v_status == "PASS" else f"Validation status is {v_status}.",
                retryable=False,
            )

        if job_type == "DEPLOY":
            project_id = job["project_id"]
            p = store.state["projects"][project_id]
            build_id = job.get("build_id") or p.get("assigned_build_id")
            build = store.state["builds"][build_id]
            out_file = Path(build["output_location"])
            if not out_file.is_file() and build_id in (store.state.get("build_artifacts") or {}):
                try:
                    store.get_or_materialize_build_artifact(
                        build_id,
                        repository_root=self.repository_root,
                        verify_hash=True,
                    )
                except ValueError as err:
                    return WorkerResult("FAILED", error=str(err), retryable=False)
            build_dir = out_file.parent
            # A successful deployment for the assigned build is never repeated automatically: an
            # external publish is not idempotent, so a retried or re-delivered DEPLOY job reuses
            # the recorded result instead of publishing a second preview.
            recorded_dep = store.state["deployments"].get(p.get("latest_deployment_id"))
            if (
                recorded_dep
                and recorded_dep.get("build_id") == build_id
                and recorded_dep.get("project_id") == project_id
                and recorded_dep.get("deployment_status") == "DEPLOYED"
            ):
                return WorkerResult(
                    "COMPLETED",
                    summary=f"Preview {recorded_dep.get('deployment_id')} is already deployed for {build_id}; repeat deployment skipped.",
                    output={
                        **recorded_dep,
                        "lead_id": p["lead_id"],
                        "project_id": project_id,
                        "build_id": build_id,
                        "validation_id": p.get("latest_validation_id"),
                        "outcome": "ALREADY_DEPLOYED_REPEAT_SKIPPED",
                    },
                )
            dry_run = bool(job.get("dry_run", True))
            if not dry_run:
                # Server-side pre-publish gate. A dry run needs no authorization because it can never
                # publish; a real publish may only proceed for the assigned, PASS-validated build that
                # an explicit human approval still covers. The provider re-verifies this itself, so this
                # check only produces a clearer refusal earlier - it is not the boundary.
                gate = store.authorize_preview_deployment(project_id, build_id=build_id,
                                                         resolve_uncertain_publish=bool(job_input.get("resolve_uncertain_publish")))
                if gate.get("skip_publish"):
                    return WorkerResult(
                        "COMPLETED",
                        summary=f"Preview {gate.get('deployment_id')} already covers {build_id}; repeat publish skipped.",
                        output={
                            "lead_id": p["lead_id"], "project_id": project_id, "build_id": build_id,
                            "validation_id": p.get("latest_validation_id"), "deployment_id": gate.get("deployment_id"),
                            "deployment_url": gate.get("deployment_url"), "deployment_status": "DEPLOYED",
                            "outcome": "ALREADY_DEPLOYED_REPEAT_SKIPPED", "authorization": gate,
                        },
                        retryable=False,
                    )
                if not gate.get("authorized"):
                    return WorkerResult(
                        "NEEDS_REVIEW",
                        error=f"{gate.get('code')}: {gate.get('reason')}" if gate.get("code") else (gate.get("reason") or "Deployment is not authorized."),
                        output={
                            "lead_id": p["lead_id"], "project_id": project_id, "build_id": build_id,
                            "validation_id": p.get("latest_validation_id"), "outcome": (
                                # An uncertain prior publish is a distinct, explicitly recoverable state.
                                "PUBLISH_STATE_UNCERTAIN" if gate.get("code") == "PUBLISH_STATE_UNCERTAIN"
                                else "DEPLOYMENT_UNAUTHORIZED"
                            ),
                            "authorization_code": gate.get("code"), "dry_run": False,
                        },
                        retryable=False,
                    )
            provider = self.deployment_provider or VercelDeploymentProvider(self.repository_root)
            deploy_kwargs = {
                "dry_run": dry_run,
                "approve_needs_review": bool(job_input.get("approve_needs_review")),
                "approve_real_preview": bool(job_input.get("approve_real_preview")),
                "project_name": job_input.get("project_name") or p.get("isolated_preview_project"),
            }
            if not dry_run:
                deploy_kwargs["resolve_uncertain_publish"] = bool(job_input.get("resolve_uncertain_publish"))
                # A worker-driven resolution is attributed to the operator named in the job or in the
                # environment; it is recorded as a claim, never as verified identity.
                deploy_kwargs["resolution_note"] = job_input.get("resolve_note")
                deploy_kwargs["resolved_by"] = job_input.get("resolved_by") or os.environ.get("AI_WEBSITE_AGENT_OPERATOR_ID")
            dep_res = provider.deploy(build_dir, **deploy_kwargs)
            dep_out = {
                **dep_res.to_dict(),
                "lead_id": p["lead_id"],
                "project_id": project_id,
                "build_id": build_id,
                "validation_id": p.get("latest_validation_id"),
                "deployment_id": dep_res.deployment_id,
                "outcome": {
                    "DEPLOYED": "PREVIEW_DEPLOYED",
                    "PENDING": "DRY_RUN_VERIFIED_NOT_DEPLOYED",
                    "NEEDS_REVIEW": "DEPLOYMENT_REQUIRES_HUMAN_REVIEW",
                }.get(dep_res.deployment_status, "DEPLOYMENT_FAILED"),
            }
            if dep_res.deployment_status == "DEPLOYED":
                try:
                    store.record_deployment(project_id, dep_res.to_dict())
                except ValueError as err:
                    # The publish happened but the audit record was refused: surface it as needing
                    # human review with the live URL, never as a silent success or a retryable failure.
                    return WorkerResult(
                        "NEEDS_REVIEW",
                        error=f"Preview {dep_res.deployment_id} was published but could not be recorded: {err}",
                        output={**dep_out, "outcome": "DEPLOYMENT_RECORD_FAILED"},
                        retryable=False,
                    )
                return WorkerResult("COMPLETED", summary=f"Deployed preview {dep_res.deployment_id}.", output=dep_out)
            if dep_res.deployment_status == "PENDING":
                return WorkerResult("COMPLETED", summary=f"Verified dry-run deployment {dep_res.deployment_id}.", output=dep_out)
            if dep_res.deployment_status == "PUBLISH_UNCERTAIN":
                return WorkerResult(
                    "NEEDS_REVIEW",
                    error=dep_res.error,
                    output={**dep_out, "outcome": "PUBLISH_STATE_UNCERTAIN"},
                    retryable=False,
                )
            if dep_res.deployment_status == "NEEDS_REVIEW":
                return WorkerResult("NEEDS_REVIEW", error=dep_res.error, output=dep_out, retryable=False)
            # A provider that refused for a configuration reason already names it in the manifest; carrying
            # that code onto the job is what makes `inspect-job` and the attention list actionable.
            return WorkerResult("FAILED", error=dep_res.error or "Deployment failed.", output=dep_out,
                error_code=(dep_res.metadata or {}).get("denial_code"), retryable=False)

        if job_type == "PREPARE_OUTREACH":
            project_id = job["project_id"]
            channel = str(job_input.get("channel") or "EMAIL").upper()
            tone = str(job_input.get("tone") or "professional")
            draft = OutreachService(store).create(
                project_id,
                channel=channel,
                tone=tone,
                regenerate=bool(job_input.get("regenerate")),
            )
            return WorkerResult(
                "COMPLETED",
                summary=f"Prepared {channel} outreach draft {draft['outreach_id']}.",
                output=draft,
            )

        return WorkerResult("FAILED", error=f"Unsupported job type: {job_type}", retryable=False)
