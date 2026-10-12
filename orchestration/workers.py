"""Provider-neutral worker registry, routing, leases, execution audit, and mock worker."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from .jobs import JOB_TYPES, WorkerResult, _now

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
    def register(self, worker_id, *, name, worker_type="OTHER", capabilities, priority=0, enabled=True, cost_class="FREE", configuration=None):
        if worker_type not in WORKER_TYPES or not set(capabilities) <= JOB_TYPES: raise ValueError("Invalid worker type or capability.")
        old=self.store.state["workers"].get(worker_id); stamp=_now()
        record={"worker_id":worker_id,"name":name,"type":worker_type,"capabilities":sorted(set(capabilities)),"priority":priority,"enabled":enabled,"cost_class":cost_class,"status":"IDLE" if enabled else "DISABLED","health_status":"AVAILABLE" if enabled else "UNAVAILABLE","last_heartbeat":stamp if enabled else None,"current_job_id":None,"last_successful_job":None,"last_failed_job":None,"last_error":None,"configuration":configuration or {},"created_at":old.get("created_at",stamp) if old else stamp,"updated_at":stamp}
        if old and old.get("current_job_id"): raise ValueError("Cannot replace a busy worker record.")
        self.store.state["workers"][worker_id]=record; self.store._save(); return record
    def heartbeat(self, worker_id, *, status="IDLE", health_status="AVAILABLE"):
        worker=self._worker(worker_id); worker.update({"status":status,"health_status":health_status,"last_heartbeat":_now(),"updated_at":_now()}); self.store._save(); return worker
    def set_enabled(self, worker_id, enabled):
        worker=self._worker(worker_id)
        if worker.get("current_job_id"): raise ValueError("Release the active job before disabling a worker.")
        worker.update({"enabled":enabled,"status":"IDLE" if enabled else "DISABLED","health_status":"AVAILABLE" if enabled else "UNAVAILABLE","updated_at":_now()}); self.store._save(); return worker
    def candidates(self, job, *, exclude=()):
        required=job["job_type"]; now=datetime.now(timezone.utc)
        items=[]
        for w in self.store.state["workers"].values():
            fresh=w.get("last_heartbeat") and (now-_parse(w["last_heartbeat"])).total_seconds() <= self.heartbeat_seconds
            if w["worker_id"] not in exclude and w["enabled"] and fresh and w["status"]=="IDLE" and w["health_status"]=="AVAILABLE" and required in w["capabilities"]: items.append(w)
        preferred=self.preferences.get(required,[])
        return sorted(items,key=lambda w:(preferred.index(w["worker_id"]) if w["worker_id"] in preferred else 999,-w["priority"],w["worker_id"]))
    def claim(self, job_id, worker_id):
        job=self.store.state["jobs"].get(job_id); worker=self._worker(worker_id)
        if not job or job["status"] not in {"PENDING","QUEUED","BLOCKED"}: raise ValueError("Job is not claimable.")
        if worker not in self.candidates(job): raise ValueError("Worker is not eligible to claim this job.")
        if job.get("project_id") and job["job_type"] in MUTATING:
            lock=self.store.state["project_locks"].get(job["project_id"])
            if lock and _parse(lock["expires_at"])>datetime.now(timezone.utc): raise ValueError("Project is locked by another mutating job.")
        if job["job_type"] in {"VALIDATE","DEPLOY"} and job.get("build_id") and job["project_id"]:
            p=self.store.state["projects"].get(job["project_id"])
            if not p or p.get("assigned_build_id")!=job["build_id"]: raise ValueError("Job build version is stale for this project.")
        stamp=_now(); expiry=(datetime.now(timezone.utc)+timedelta(seconds=self.lease_seconds)).replace(microsecond=0).isoformat().replace("+00:00","Z"); lease=f"lease-{uuid4().hex}"
        job.update({"status":"RUNNING","attempts":job["attempts"]+1,"started_at":job.get("started_at") or stamp,"updated_at":stamp,"heartbeat_at":stamp,"lease":{"lease_id":lease,"worker_id":worker_id,"claimed_at":stamp,"heartbeat_at":stamp,"expires_at":expiry}})
        worker.update({"status":"BUSY","current_job_id":job_id,"updated_at":stamp})
        if job.get("project_id") and job["job_type"] in MUTATING: self.store.state["project_locks"][job["project_id"]]={"project_id":job["project_id"],"job_id":job_id,"worker_id":worker_id,"lease_id":lease,"expires_at":expiry}
        eid=f"execution-{uuid4().hex}"; self.store.state["executions"][eid]={"execution_id":eid,"job_id":job_id,"worker_id":worker_id,"attempt_number":job["attempts"],"status":"RUNNING","started_at":stamp,"completed_at":None,"result":None,"error_type":None,"error_message":None,"artifacts":[],"commit_sha":None,"logs_reference":None,"created_at":stamp}; job["execution_id"]=eid; self.store._save(); return job
    def renew_lease(self, job_id, worker_id, lease_id):
        job=self.store.state["jobs"].get(job_id); lease=(job or {}).get("lease",{})
        if lease.get("worker_id")!=worker_id or lease.get("lease_id")!=lease_id: raise ValueError("Invalid worker lease.")
        expiry=(datetime.now(timezone.utc)+timedelta(seconds=self.lease_seconds)).replace(microsecond=0).isoformat().replace("+00:00","Z"); lease.update({"heartbeat_at":_now(),"expires_at":expiry}); job["heartbeat_at"]=_now()
        lock=self.store.state["project_locks"].get(job.get("project_id"));
        if lock and lock["lease_id"]==lease_id: lock["expires_at"]=expiry
        self.store._save(); return job
    def report(self, job_id, worker_id, lease_id, result:WorkerResult):
        job=self.store.state["jobs"].get(job_id); lease=(job or {}).get("lease",{})
        if lease.get("worker_id")!=worker_id or lease.get("lease_id")!=lease_id: raise ValueError("Stale or unauthorized result rejected.")
        worker=self._worker(worker_id); execution=self.store.state["executions"][job["execution_id"]]; status=result.status
        normalized={"status":status,"summary":result.summary,"artifacts":result.artifacts,"warnings":result.warnings,"errors":[result.error] if result.error else [],"metadata":result.metadata,"commit_sha":result.commit_sha}
        final="COMPLETED" if status=="COMPLETED" else "NEEDS_REVIEW" if status in {"NEEDS_REVIEW","PARTIAL"} or result.error_type=="HUMAN_REVIEW" else "QUEUED" if result.retryable and job["attempts"]<job["max_attempts"] else "FAILED"
        job.update({"status":final,"output":normalized,"artifacts":result.artifacts,"error":result.error,"updated_at":_now(),"completed_at":_now() if final!="QUEUED" else None,"lease":None})
        execution.update({"status":final,"completed_at":_now(),"result":normalized,"error_type":result.error_type,"error_message":result.error,"artifacts":result.artifacts,"commit_sha":result.commit_sha})
        worker.update({"status":"IDLE","current_job_id":None,"updated_at":_now(),"last_successful_job":job_id if final=="COMPLETED" else worker.get("last_successful_job"),"last_failed_job":job_id if final in {"FAILED","NEEDS_REVIEW"} else worker.get("last_failed_job"),"last_error":result.error})
        self._unlock(job); self.store._save(); return job
    def recover_expired(self):
        now=datetime.now(timezone.utc); recovered=[]
        for job in list(self.store.state["jobs"].values()):
            lease=job.get("lease") or {}
            if job.get("status")=="RUNNING" and lease.get("expires_at") and _parse(lease["expires_at"])<now:
                worker=self._worker(lease["worker_id"]); worker.update({"status":"DEGRADED","health_status":"UNAVAILABLE","current_job_id":None,"last_error":"Lease expired."}); job.update({"status":"QUEUED" if job["attempts"]<job["max_attempts"] else "NEEDS_REVIEW","error":"Worker lease expired.","lease":None,"updated_at":_now()}); self._unlock(job); recovered.append(job)
        self.store._save(); return recovered
    def human_takeover(self, job_id, worker_id):
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
