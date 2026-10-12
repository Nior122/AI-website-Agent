"""Small recoverable job ledger and provider-neutral worker contract.

Jobs live in TrackingStore so n8n is never the only record of work.  This module
does not discover, build, deploy, or contact anybody by itself: a caller supplies
a worker for approved work and receives a structured outcome.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


JOB_TYPES = {"SCOUT", "RESEARCH", "QUALIFY", "BUILD", "VALIDATE", "DEPLOY", "PREPARE_OUTREACH"}
JOB_STATUSES = {"PENDING", "QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "BLOCKED"}
PRIORITIES = {"HIGH", "NORMAL", "LOW"}
NEXT_TYPES = {"SCOUT": "RESEARCH", "RESEARCH": "QUALIFY", "QUALIFY": "BUILD", "BUILD": "VALIDATE", "VALIDATE": "DEPLOY"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WorkerResult:
    status: str
    summary: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    retryable: bool = False
    error_type: str | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    commit_sha: str | None = None


class JobWorker(Protocol):
    """A model/provider-neutral worker. It must update domain state explicitly."""
    name: str

    def execute(self, job: dict[str, Any], context: dict[str, Any]) -> WorkerResult: ...


class JobService:
    """Creates, gates, runs, retries, and recovers application-owned jobs."""

    def __init__(self, store, *, max_attempts: int = 3) -> None:
        self.store = store
        self.store.state.setdefault("jobs", {})
        self.max_attempts = max_attempts

    def create(self, job_type: str, *, job_id: str, lead_id: str | None = None,
               project_id: str | None = None, build_id: str | None = None,
               parent_job_id: str | None = None, priority: str = "NORMAL",
               input: dict[str, Any] | None = None, dry_run: bool = True) -> dict[str, Any]:
        if job_type not in JOB_TYPES or priority not in PRIORITIES:
            raise ValueError("Unsupported job type or priority.")
        old = self.store.state["jobs"].get(job_id)
        if old:
            if old["job_type"] != job_type or old.get("lead_id") != lead_id or old.get("project_id") != project_id:
                raise ValueError("Idempotency key is already associated with a different job.")
            return old
        job = {"job_id": job_id, "job_type": job_type, "lead_id": lead_id,
               "project_id": project_id, "build_id": build_id, "parent_job_id": parent_job_id,
               "status": "PENDING", "priority": priority, "attempts": 0,
               "max_attempts": self.max_attempts, "input": input or {}, "output": None,
               "artifacts": [], "error": None, "created_at": _now(), "started_at": None,
               "updated_at": _now(), "heartbeat_at": None, "completed_at": None,
               "dry_run": dry_run, "next_job_types": []}
        self.store.state["jobs"][job_id] = job
        self.store._save()
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.store.state["jobs"].get(job_id)
        if not job:
            raise ValueError("Unknown job.")
        return job

    def run(self, job_id: str, worker: JobWorker) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == "COMPLETED":
            return job
        if job["status"] in {"CANCELLED", "NEEDS_REVIEW"}:
            return job
        reason = self.prerequisite_failure(job)
        if reason:
            return self._finish(job, "BLOCKED", error=reason)
        if job["attempts"] >= job["max_attempts"]:
            return self._finish(job, "FAILED", error="Retry limit reached.")
        job.update({"status": "RUNNING", "attempts": job["attempts"] + 1,
                    "started_at": job["started_at"] or _now(), "updated_at": _now(), "heartbeat_at": _now()})
        self.store._save()
        try:
            result = worker.execute(job, self.context_for(job))
        except Exception as error:  # Worker faults remain recoverable state, not lost exceptions.
            result = WorkerResult("FAILED", error=str(error), retryable=False)
        if result.status == "COMPLETED":
            job = self._finish(job, "COMPLETED", output=result.output, artifacts=result.artifacts)
            job["next_job_types"] = self.suggest_next(job)
            self.store._save()
            return job
        if result.status == "NEEDS_REVIEW":
            return self._finish(job, "NEEDS_REVIEW", output=result.output, artifacts=result.artifacts, error=result.error)
        if result.retryable and job["attempts"] < job["max_attempts"]:
            return self._finish(job, "QUEUED", output=result.output, artifacts=result.artifacts, error=result.error)
        return self._finish(job, "FAILED", output=result.output, artifacts=result.artifacts, error=result.error or "Worker failed.")

    def heartbeat(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] != "RUNNING":
            raise ValueError("Only RUNNING jobs may heartbeat.")
        job["heartbeat_at"] = job["updated_at"] = _now()
        self.store._save()
        return job

    def recover_stale(self, *, older_than_seconds: int = 900, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        recovered = []
        for job in self.store.state["jobs"].values():
            if job["status"] != "RUNNING" or not job.get("heartbeat_at"):
                continue
            heartbeat = datetime.fromisoformat(job["heartbeat_at"].replace("Z", "+00:00"))
            if (current - heartbeat).total_seconds() > older_than_seconds:
                recovered.append(self._finish(job, "NEEDS_REVIEW", error="Worker heartbeat expired; inspect before retrying."))
        return recovered

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == "RUNNING":
            raise ValueError("A running job must be stopped by its worker before cancellation.")
        return self._finish(job, "CANCELLED")

    def prerequisite_failure(self, job: dict[str, Any]) -> str | None:
        lead = self.store.state["leads"].get(job.get("lead_id")) if job.get("lead_id") else None
        project = self.store.state["projects"].get(job.get("project_id")) if job.get("project_id") else None
        if job["job_type"] in {"RESEARCH", "QUALIFY", "BUILD"} and not lead:
            return "Lead reference is required and was not found."
        if job["job_type"] == "BUILD" and lead.get("qualification_status") != "QUALIFIED":
            return "BUILD requires a QUALIFIED lead."
        if job["job_type"] in {"VALIDATE", "DEPLOY", "PREPARE_OUTREACH"} and not project:
            return f"{job['job_type']} requires a project reference."
        if job["job_type"] == "VALIDATE" and not self._build(job, project):
            return "VALIDATE requires a known build for this project."
        if job["job_type"] == "DEPLOY":
            validation = self._latest_validation(project)
            if not validation or validation.get("status") != "PASS":
                return "DEPLOY requires PASS validation."
        if job["job_type"] == "PREPARE_OUTREACH":
            if not project or project.get("review_status") != "APPROVED" or project.get("current_stage") != "READY_FOR_OUTREACH":
                return "PREPARE_OUTREACH requires approved human review and a ready project."
        return None

    def context_for(self, job: dict[str, Any]) -> dict[str, Any]:
        project = self.store.state["projects"].get(job.get("project_id")) if job.get("project_id") else None
        return {"lead": self.store.state["leads"].get(job.get("lead_id")), "project": project,
                "snapshot": self.store.snapshot(job["project_id"]) if project else None,
                "dry_run": job["dry_run"], "tracking_store": self.store}

    def suggest_next(self, job: dict[str, Any]) -> list[str]:
        next_type = NEXT_TYPES.get(job["job_type"])
        if job["job_type"] == "VALIDATE" and (job.get("output") or {}).get("validation_status") != "PASS":
            return []
        return [next_type] if next_type else []

    def create_next(self, job_id: str, *, next_job_id: str, job_type: str | None = None, lead_id: str | None = None,
                    input: dict[str, Any] | None = None, dry_run: bool | None = None) -> dict[str, Any]:
        """Explicitly create one suggested successor; callers retain control of chaining."""
        parent = self.get(job_id)
        if parent["status"] != "COMPLETED":
            raise ValueError("Only completed jobs may create a successor.")
        selected = job_type or (parent.get("next_job_types") or [None])[0]
        if not selected or selected not in parent.get("next_job_types", []):
            raise ValueError("The requested successor is not currently eligible.")
        return self.create(selected, job_id=next_job_id, lead_id=lead_id or parent.get("lead_id"),
                           project_id=parent.get("project_id"), build_id=parent.get("build_id"),
                           parent_job_id=parent["job_id"], input=input,
                           dry_run=parent["dry_run"] if dry_run is None else dry_run)

    def _build(self, job: dict[str, Any], project: dict[str, Any] | None) -> dict[str, Any] | None:
        build_id = job.get("build_id") or (project or {}).get("assigned_build_id")
        build = self.store.state["builds"].get(build_id)
        return build if build and build.get("project_id") == job.get("project_id") else None

    def _latest_validation(self, project: dict[str, Any] | None) -> dict[str, Any] | None:
        return self.store.state["validations"].get((project or {}).get("latest_validation_id"))

    def _finish(self, job: dict[str, Any], status: str, *, output: dict[str, Any] | None = None,
                artifacts: list[str] | None = None, error: str | None = None) -> dict[str, Any]:
        job.update({"status": status, "output": output if output is not None else job.get("output"),
                    "artifacts": artifacts if artifacts is not None else job.get("artifacts", []),
                    "error": error, "updated_at": _now()})
        if status in {"COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "BLOCKED"}:
            job["completed_at"] = _now()
        self.store._save()
        return job
