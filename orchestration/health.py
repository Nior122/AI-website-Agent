"""Non-secret local health and readiness reports; no network calls are made.

`system_health()` only inspects already-loaded state and configuration, so it never opens a
database connection. Connectivity is verified explicitly by `verify_storage()`, which the API
readiness endpoint uses so an unreachable database is reported as NOT_READY instead of READY.
"""
from __future__ import annotations
import os
from pathlib import Path

from tracking.database import is_production_mode, sanitize_db_error


def storage_report(store) -> dict:
    """Describe the configured persistence backend without contacting it."""
    db_backend = getattr(store, "db_backend", None)
    production = is_production_mode()
    report = {
        "backend": getattr(db_backend, "dialect", None) if db_backend is not None else "json",
        "database": getattr(db_backend, "redacted_url", None),
        "production_mode": production,
        "status": "healthy",
        "error": None,
    }
    if db_backend is None and production:
        report["status"] = "unavailable"
        report["error"] = (
            "Production mode requires the PostgreSQL/Supabase backend; refusing local JSON state."
        )
    return report


def verify_storage(store) -> dict:
    """Actively verify that the configured storage backend is reachable and correctly migrated."""
    report = storage_report(store)
    db_backend = getattr(store, "db_backend", None)
    if db_backend is None:
        # Side-effect-free writability check: never create the state file from a health probe,
        # because a missing file is what triggers verified workspace recovery on first use.
        state_path = getattr(store, "path", None)
        parent = Path(state_path).parent if state_path is not None else None
        if parent is not None and parent.exists() and not os.access(parent, os.W_OK):
            report["status"] = "unavailable"
            report["error"] = f"Local state directory {parent} is not writable."
        return report
    try:
        db_backend.verify_schema()
    except Exception as error:  # DatabasePersistenceError and driver-level faults alike.
        report["status"] = "unavailable"
        report["error"] = sanitize_db_error(error)
    return report


def system_health(store, registry=None, *, repository_root=Path(".")):
    workers=list((store.state.get("workers") or {}).values())
    available=[w for w in workers if w.get("enabled") and w.get("status")=="IDLE" and w.get("health_status")=="AVAILABLE"]
    linked=(Path(repository_root)/".vercel"/"project.json").is_file()
    local_state=store.check_local_state(repository_root=Path(repository_root)) if hasattr(store,"check_local_state") else {"status":"HEALTHY"}
    storage=storage_report(store)
    jobs=list((store.state.get("jobs") or {}).values())
    job_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "UNKNOWN")
        job_counts[status] = job_counts.get(status, 0) + 1
    attention = sorted(status for status in job_counts if status in {"FAILED", "NEEDS_REVIEW", "BLOCKED"})
    state_available = isinstance(store.state, dict) and storage["status"] == "healthy"
    return {"application":{"status":"healthy" if state_available else "degraded"},"source_of_truth":{"status":"healthy" if state_available else "unavailable","storage":storage,"local_state":local_state},"job_system":{"status":"degraded" if attention else "healthy","jobs":len(jobs),"by_status":job_counts,"attention":attention},"worker_system":{"status":"healthy" if available else "degraded","available_workers":len(available)},"n8n":{"status":"unavailable","reason":"Not activated or connected by this repository."},"deployment":{"status":"healthy" if linked else "degraded","reason":None if linked else "No approved Vercel project link."},"configuration":{"status":"healthy"}}


def readiness_for(store, job_service, job_id, registry=None):
    job=job_service.get(job_id); failure=job_service.prerequisite_failure(job)
    report={"job_id":job_id,"status":"READY" if not failure else "NOT_READY","reason":failure,"dry_run":job.get("dry_run",True)}
    if not failure and registry:
        candidates=registry.candidates(job)
        if not candidates: report.update({"status":"NOT_READY","reason":"No available capable worker."})
    return report
