"""Non-secret local health and readiness reports; no network calls are made."""
from __future__ import annotations
from pathlib import Path

def system_health(store, registry=None, *, repository_root=Path(".")):
    workers=list((store.state.get("workers") or {}).values())
    available=[w for w in workers if w.get("enabled") and w.get("status")=="IDLE" and w.get("health_status")=="AVAILABLE"]
    linked=(Path(repository_root)/".vercel"/"project.json").is_file()
    local_state=store.check_local_state(repository_root=Path(repository_root)) if hasattr(store,"check_local_state") else {"status":"HEALTHY"}
    return {"application":{"status":"healthy"},"source_of_truth":{"status":"healthy" if isinstance(store.state,dict) else "unavailable","local_state":local_state},"job_system":{"status":"healthy","jobs":len(store.state.get("jobs",{}))},"worker_system":{"status":"healthy" if available else "degraded","available_workers":len(available)},"n8n":{"status":"unavailable","reason":"Not activated or connected by this repository."},"deployment":{"status":"healthy" if linked else "degraded","reason":None if linked else "No approved Vercel project link."},"configuration":{"status":"healthy"}}

def readiness_for(store, job_service, job_id, registry=None):
    job=job_service.get(job_id); failure=job_service.prerequisite_failure(job)
    report={"job_id":job_id,"status":"READY" if not failure else "NOT_READY","reason":failure,"dry_run":job.get("dry_run",True)}
    if not failure and registry:
        candidates=registry.candidates(job)
        if not candidates: report.update({"status":"NOT_READY","reason":"No available capable worker."})
    return report
