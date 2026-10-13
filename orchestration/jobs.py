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
            if (
                old["job_type"] != job_type
                or (lead_id is not None and old.get("lead_id") is not None and old.get("lead_id") != lead_id)
                or (project_id is not None and old.get("project_id") is not None and old.get("project_id") != project_id)
                or (build_id is not None and old.get("build_id") is not None and old.get("build_id") != build_id)
            ):
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

    def retry(
        self,
        job_id: str,
        *,
        input: dict[str, Any] | None = None,
        worker: JobWorker | None = None,
        reset_attempts: bool = False,
    ) -> dict[str, Any]:
        """Safely re-queue (and optionally run) an eligible failed, queued, or recovered job."""
        job = self.get(job_id)
        if job["status"] == "COMPLETED":
            return job
        if job["status"] in {"RUNNING", "CANCELLED"}:
            raise ValueError(f"Cannot retry job in status {job['status']}.")
        if reset_attempts:
            job["attempts"] = 0
        if job["attempts"] >= job["max_attempts"]:
            raise ValueError("Retry limit reached.")
        if input is not None:
            if not isinstance(input, dict):
                raise ValueError("Retry input must be a JSON object.")
            merged = dict(job.get("input") or {})
            merged.update(input)
            job["input"] = merged
        job.update({
            "status": "QUEUED",
            "error": None,
            "completed_at": None,
            "updated_at": _now(),
        })
        self.store._save()
        if worker is not None:
            return self.run(job_id, worker)
        return job

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
            build = self._build(job, project)
            validation = self._latest_validation(project)
            if not build or not validation or validation.get("status") != "PASS":
                return "DEPLOY requires PASS validation."
            if project.get("review_status") in {"REJECTED", "CHANGES_REQUESTED"} or project.get("current_stage") not in {"READY_FOR_REVIEW", "PREVIEW_DEPLOYED"}:
                return "DEPLOY requires a project build in READY_FOR_REVIEW (not rejected or awaiting rebuild)."
            is_real = (
                (lead or {}).get("data_classification") == "REAL_PUBLIC_DATA"
                or (project or {}).get("data_classification") == "REAL_PUBLIC_DATA"
                or (build or {}).get("data_classification") == "REAL_PUBLIC_DATA"
            )
            if is_real:
                job_input = job.get("input") or {}
                req_project = job_input.get("project_name")
                cfg_project = (project or {}).get("isolated_preview_project")
                if req_project and cfg_project and req_project != cfg_project:
                    return "DEPLOY project_name does not match project's configured isolated_preview_project."
                target_project = req_project or cfg_project
                if not job_input.get("approve_real_preview") or not target_project or target_project == "ai-website-agent-preview":
                    return "DEPLOY for REAL_PUBLIC_DATA requires explicit approve_real_preview and an isolated preview project target."
                from tracking.store import validate_preview_project_name
                try:
                    validate_preview_project_name(str(target_project), real_business=True)
                except ValueError as err:
                    return str(err)
                if (
                    project.get("review_status") != "APPROVED_FOR_PREVIEW"
                    or project.get("approved_build_id") != project.get("assigned_build_id")
                    or project.get("approved_action") != "PREVIEW_DEPLOYMENT"
                ):
                    return "DEPLOY for REAL_PUBLIC_DATA requires explicit pre-deployment human review approval (APPROVED_FOR_PREVIEW for action PREVIEW_DEPLOYMENT) bound to the current assigned build."
        if job["job_type"] == "PREPARE_OUTREACH":
            validation = self._latest_validation(project)
            deployment = self._latest_deployment(project)
            if (
                not project
                or project.get("review_status") != "APPROVED"
                or project.get("current_stage") != "READY_FOR_OUTREACH"
                or (project.get("approved_build_id") and project.get("approved_build_id") != project.get("assigned_build_id"))
                or (project.get("approved_action") and project.get("approved_action") != "OUTREACH_PREPARATION")
                or not validation
                or validation.get("status") != "PASS"
                or not deployment
            ):
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
                    project_id: str | None = None, build_id: str | None = None,
                    input: dict[str, Any] | None = None, dry_run: bool | None = None) -> dict[str, Any]:
        """Explicitly create one suggested successor; callers retain control of chaining."""
        parent = self.get(job_id)
        if parent["status"] != "COMPLETED":
            raise ValueError("Only completed jobs may create a successor.")
        selected = job_type or (parent.get("next_job_types") or [None])[0]
        if not selected or selected not in parent.get("next_job_types", []):
            raise ValueError("The requested successor is not currently eligible.")
        parent_out = parent.get("output") or {}
        resolved_lead_id = lead_id or parent_out.get("lead_id") or parent.get("lead_id")
        resolved_project_id = project_id or parent_out.get("project_id") or parent.get("project_id")
        project = self.store.state["projects"].get(resolved_project_id) if resolved_project_id else None
        resolved_build_id = build_id or parent_out.get("build_id") or parent.get("build_id") or (project.get("assigned_build_id") if project else None)
        return self.create(selected, job_id=next_job_id, lead_id=resolved_lead_id,
                           project_id=resolved_project_id, build_id=resolved_build_id,
                           parent_job_id=parent["job_id"], input=input,
                           dry_run=parent["dry_run"] if dry_run is None else dry_run)

    def _build(self, job: dict[str, Any], project: dict[str, Any] | None) -> dict[str, Any] | None:
        assigned = (project or {}).get("assigned_build_id")
        build_id = job.get("build_id") or assigned
        if not build_id or (assigned and build_id != assigned):
            return None
        build = self.store.state["builds"].get(build_id)
        return build if build and build.get("project_id") == job.get("project_id") and build.get("status") != "FAILED" else None

    def _latest_validation(self, project: dict[str, Any] | None) -> dict[str, Any] | None:
        val = self.store.state["validations"].get((project or {}).get("latest_validation_id"))
        return val if val and val.get("build_id") == (project or {}).get("assigned_build_id") else None

    def _latest_deployment(self, project: dict[str, Any] | None) -> dict[str, Any] | None:
        dep = self.store.state["deployments"].get((project or {}).get("latest_deployment_id"))
        return dep if dep and dep.get("build_id") == (project or {}).get("assigned_build_id") and dep.get("deployment_status") == "DEPLOYED" else None

    def _finish(self, job: dict[str, Any], status: str, *, output: dict[str, Any] | None = None,
                artifacts: list[str] | None = None, error: str | None = None) -> dict[str, Any]:
        job.update({"status": status, "output": output if output is not None else job.get("output"),
                    "artifacts": artifacts if artifacts is not None else job.get("artifacts", []),
                    "error": error, "updated_at": _now()})
        if status in {"COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "BLOCKED"}:
            job["completed_at"] = _now()
        self.store._save()
        return job
