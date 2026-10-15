"""Small recoverable job ledger and provider-neutral worker contract.

Jobs live in TrackingStore so n8n is never the only record of work.  This module
does not discover, build, deploy, or contact anybody by itself: a caller supplies
a worker for approved work and receives a structured outcome.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from .observability import log_event, recorded_duration_seconds, sanitize_diagnostic


#: Stable codes for the reasons a job cannot run. Codes are machine-readable and never
#: replace the human-readable message; both are persisted so an operator can act on one and
#: still read the other.
JOB_TYPES = {"SCOUT", "RESEARCH", "QUALIFY", "BUILD", "VALIDATE", "DEPLOY", "PREPARE_OUTREACH"}
JOB_STATUSES = {"PENDING", "QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "BLOCKED"}
PRIORITIES = {"HIGH", "NORMAL", "LOW"}
NEXT_TYPES = {"SCOUT": "RESEARCH", "RESEARCH": "QUALIFY", "QUALIFY": "BUILD", "BUILD": "VALIDATE", "VALIDATE": "DEPLOY"}

#: Statuses that mean "a human has to do something", as opposed to "the system will retry".
OPERATOR_ATTENTION_STATUSES = {"NEEDS_REVIEW", "BLOCKED"}
ATTENTION_STATUSES = OPERATOR_ATTENTION_STATUSES | {"FAILED"}

#: What an operator should do next, keyed by stable failure code. Unknown codes fall back to
#: the recorded message instead of inventing an instruction.
NEXT_ACTIONS = {
    "LEAD_MISSING": "Reference the lead this job belongs to (lead_id) or ingest it first: python3 -m tracking.cli ingest-lead.",
    "LEAD_NOT_QUALIFIED": "Run QUALIFY for this lead; a BUILD job requires a QUALIFIED lead.",
    "PROJECT_MISSING": "Create the project for the lead (TrackingStore.create_project) and re-create the job with its project_id.",
    "BUILD_MISSING": "Run BUILD for this project; a build record must exist for the assigned build id.",
    "VALIDATION_MISSING": "Run VALIDATE for the assigned build and record its result.",
    "VALIDATION_NOT_PASSED": "Fix the findings and re-validate the same build; a non-PASS validation never authorizes deployment.",
    "STAGE_NOT_REVIEWABLE": "Return the project to READY_FOR_REVIEW (rebuild or re-validate) before deploying.",
    "RETRY_LIMIT_REACHED": "Inspect the recorded failure, correct the cause, then retry explicitly with reset_attempts.",
    "WORKER_UNAVAILABLE": "Start or register a capable worker (orchestration.cli or the worker registry).",
    "LEASE_EXPIRED": "Confirm whether the claiming worker is still running its job; recover the stale job (python3 -m orchestration.cli recover-stale) before another worker or a retry takes it.",
    "WORKER_FAULT": "Read the recorded error, correct the input or environment, then retry the job.",
    "WORKER_FAILED": "Read the recorded worker error and correct its cause; the job stays retryable only within the attempt budget.",
    "TRANSIENT_FAILURE": "The worker reported a transient fault; it will be requeued automatically until the attempt budget runs out.",
    "HUMAN_REVIEW_REQUIRED": "A human decision is required on the recorded output; retry only after the decision is recorded.",
    "HEARTBEAT_EXPIRED": "Check whether the worker is still alive; recover the job (recover-stale) before retrying.",
    "OUTREACH_NOT_READY": "Complete the human preview review; outreach requires an APPROVED decision for OUTREACH_PREPARATION.",
    "PUBLISH_STATE_UNCERTAIN": "Inspect the provider project for an accepted publish, then re-run deliberately with "
    "resolve_uncertain_publish (--resolve-uncertain-publish on the deployment CLI). Never re-publish blind: a second "
    "accepted publish is not undoable by this system.",
    "DEPLOYMENT_RECORD_FAILED": "Record the published preview against the project (TrackingStore.record_deployment) so the audit trail matches the provider.",
    "REAL_PREVIEW_TARGET_MISMATCH": "Use the project's configured isolated_preview_project as the deployment target.",
    "REAL_PREVIEW_TARGET_REQUIRED": "Configure an isolated preview target: python3 -m tracking.cli configure-preview <project_id> --project-name <slug>.",
    "REAL_APPROVAL_REQUIRED": "Record the pre-deployment approval: python3 -m tracking.cli review <project_id> --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT --notes \"...\" --confirm <project_id>:<build_id>:APPROVED_FOR_PREVIEW.",
    "APPROVAL_MISSING": "Read the draft and record the decision: python3 -m tracking.cli review {project_id} --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT --notes \"...\" --confirm {project_id}:{build_id}:APPROVED_FOR_PREVIEW. The publish stays blocked until then; no retry can substitute for it.",
    "APPROVAL_UNREADABLE": "The recorded approval capability cannot be parsed. Re-record the decision against the current build; do not hand-edit the capability file.",
    "APPROVAL_TAMPERED": "The recorded approval does not match its digest. Re-record the decision after confirming the build and artifact digests.",
    "APPROVAL_REVOKED": "The approval was revoked deliberately. Record a new decision for the current build if the change is intended.",
    "APPROVAL_INVALID_DECISION": "The stored decision is not a recognized approval value. Re-record with --decision APPROVED_FOR_PREVIEW.",
    "APPROVAL_WRONG_ACTION": "The approval covers a different action than preview deployment. Record a decision with --action PREVIEW_DEPLOYMENT.",
    "APPROVAL_BUILD_MISMATCH": "The approval covers another build. Rebuild the decision for the currently assigned build: python3 -m deployment.cli --project-id {project_id} (dry run) to read the expected confirmation.",
    "APPROVAL_LEAD_MISMATCH": "The approval belongs to a different lead record. Re-record it against this project's lead.",
    "APPROVAL_VALIDATION_MISMATCH": "The approval was recorded against a different validation. Re-validate the assigned build, then re-record the decision.",
    "APPROVAL_ARTIFACT_STALE": "The artifact changed after approval. Re-review the current artifact so the approval digest matches.",
    "APPROVAL_SUPERSEDED": "A newer decision replaced this approval. Review the current build again.",
    "REVIEW_REJECTED": "The human review rejected this build. Correct the draft, rebuild, re-validate, then re-review.",
    "REVIEW_CHANGES_REQUESTED": "The reviewer requested changes. Apply them, rebuild, re-validate, then re-review before deploying.",
    "STAGE_NOT_READY": "Advance the project to READY_FOR_REVIEW by finishing the pending stage; deployment is only possible from a reviewable stage.",
    "DEPLOYMENT_UNAUTHORIZED": "Read python3 -m deployment.cli --project-id {project_id} (dry run) for the unmet condition, then satisfy it deliberately.",
    "UNKNOWN_PROJECT": "Reference an existing project id; create it from the lead first if it does not exist yet.",
    "BUILD_NOT_ASSIGNED": "Assign a build to the project (record_build) before validating or deploying.",
    "BUILD_NOT_FOUND": "The recorded build id does not exist; rebuild and re-validate.",
    "BUILD_NOT_COMPLETED": "The assigned build has not completed successfully; finish or replace it first.",
    "ARTIFACT_NOT_RECORDED": "Record the build artifact durably (build record with output_location and digest) before publishing.",
    "REAL_ISOLATED_TARGET_REQUIRED": "Configure an isolated preview project for real data; the shared default target must never receive a real business site.",
    "STORE_CANNOT_AUTHORIZE": "Upgrade the tracking store: the deployment authorization gate is unavailable, so no publish may proceed.",
}


def next_action_for(code: str | None, *, project_id: str | None = None, build_id: str | None = None, job_type: str | None = None) -> str | None:
    """Concrete, copy-pasteable next step for a recorded failure code (None when unknown)."""
    template = NEXT_ACTIONS.get(str(code or ""))
    if not template:
        return None
    return template.format(project_id=project_id or "<project_id>", build_id=build_id or "<build_id>")


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
    error_code: str | None = None
    next_action: str | None = None
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
               input: dict[str, Any] | None = None, dry_run: bool = True,
               idempotency_key: str | None = None) -> dict[str, Any]:
        if job_type not in JOB_TYPES or priority not in PRIORITIES:
            raise ValueError("Unsupported job type or priority.")
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()
        effective_key = str(idempotency_key or job_id).strip()
        old = self.store.state["jobs"].get(job_id)
        if not old:
            old = next(
                (j for j in self.store.state["jobs"].values() if j.get("idempotency_key") == effective_key),
                None,
            )
        if old:
            if (
                old["job_type"] != job_type
                or (lead_id is not None and old.get("lead_id") is not None and old.get("lead_id") != lead_id)
                or (project_id is not None and old.get("project_id") is not None and old.get("project_id") != project_id)
                or (build_id is not None and old.get("build_id") is not None and old.get("build_id") != build_id)
            ):
                raise ValueError("Idempotency key is already associated with a different job.")
            return old
        job = {"job_id": job_id, "idempotency_key": effective_key, "job_type": job_type, "lead_id": lead_id,
               "project_id": project_id, "build_id": build_id, "parent_job_id": parent_job_id,
               "status": "PENDING", "priority": priority, "attempts": 0,
               "max_attempts": self.max_attempts, "input": input or {}, "output": None,
               "artifacts": [], "error": None, "created_at": _now(), "started_at": None,
               "updated_at": _now(), "heartbeat_at": None, "completed_at": None,
               "dry_run": dry_run, "next_job_types": []}
        self.store.state["jobs"][job_id] = job
        self.store._save()
        if job_id not in self.store.state["jobs"]:
            merged_existing = next(
                (j for j in self.store.state["jobs"].values() if j.get("idempotency_key") == effective_key),
                None,
            )
            if merged_existing is not None:
                return merged_existing
        return self.store.state["jobs"].get(job_id, job)

    def get(self, job_id: str) -> dict[str, Any]:
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()
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
        failure = self.gate_failure(job)
        if failure:
            code, reason = failure
            # A gated job never started, so no execution record is opened: the ledger says what
            # was attempted, and this records that nothing was.
            return self._finish(job, "BLOCKED", error=reason, code=code)
        if job["attempts"] >= job["max_attempts"]:
            return self._finish(job, "FAILED", error="Retry limit reached.", code="RETRY_LIMIT_REACHED")
        job.update({"status": "RUNNING", "attempts": job["attempts"] + 1,
                    "started_at": job["started_at"] or _now(), "updated_at": _now(), "heartbeat_at": _now()})
        self._begin_execution(job, worker)
        self.store._save()
        try:
            result = worker.execute(job, self.context_for(job))
        except Exception as error:  # Worker faults remain recoverable state, not lost exceptions.
            result = WorkerResult("FAILED", error=str(error), retryable=False,
                                  error_type=type(error).__name__, error_code="WORKER_FAULT")
        if result.status == "COMPLETED":
            self._close_execution(job, "COMPLETED", result=result)
            job = self._finish(job, "COMPLETED", output=result.output, artifacts=result.artifacts)
            job["next_job_types"] = self.suggest_next(job)
            self.store._save()
            return job
        if result.status == "NEEDS_REVIEW":
            self._close_execution(job, "NEEDS_REVIEW", result=result)
            return self._finish(job, "NEEDS_REVIEW", output=result.output, artifacts=result.artifacts,
                               error=result.error, code=result.error_code or "HUMAN_REVIEW_REQUIRED")
        if result.retryable and job["attempts"] < job["max_attempts"]:
            self._close_execution(job, "QUEUED", result=result)
            return self._finish(job, "QUEUED", output=result.output, artifacts=result.artifacts,
                                error=result.error, code=result.error_code or "TRANSIENT_FAILURE")
        self._close_execution(job, "FAILED", result=result)
        return self._finish(job, "FAILED", output=result.output, artifacts=result.artifacts,
                            error=result.error or "Worker failed.", code=result.error_code or "WORKER_FAILED")

    # -- per-attempt execution records ----------------------------------------------------
    def executions_for(self, job_id: str) -> list[dict[str, Any]]:
        """Every attempt this system recorded for one job, oldest first, with derived duration."""
        job = self.get(job_id)
        records = [
            dict(record)
            for record in (self.store.state.get("executions") or {}).values()
            if isinstance(record, dict) and record.get("job_id") == job_id
        ]
        # Ordered by the recorded start time, with the ledger's own insertion order as the
        # tie-breaker: that is the order the system actually wrote them, and it survives an
        # `attempts` reset (a deliberate retry restarts the counter, so it is not a sequence).
        records.sort(key=lambda record: str(record.get("started_at") or ""))
        for index, record in enumerate(records, start=1):
            record.setdefault("sequence", index)
            record["duration_seconds"] = recorded_duration_seconds(record.get("started_at"), record.get("completed_at"))                 if record.get("duration_seconds") is None else record["duration_seconds"]
            record["job_status"] = job.get("status")
            if not record.get("next_action") and job.get("next_action"):
                record["next_action"] = job["next_action"]
        return records

    def retry_eligibility(self, job_id: str) -> dict[str, Any]:
        """Whether this job may be retried, from recorded state only - never a guess."""
        return self._eligibility_for(self.get(job_id))

    def _eligibility_for(self, job: dict[str, Any]) -> dict[str, Any]:
        """Eligibility from an already-resolved record (never re-reads: no refresh side effects)."""
        status = str(job.get("status") or "")
        attempts = int(job.get("attempts") or 0)
        maximum = int(job.get("max_attempts") or self.max_attempts)
        attempts_remaining = max(0, maximum - attempts)
        eligibility = {"job_id": job.get("job_id"), "status": status, "attempts": attempts, "max_attempts": maximum,
                       "attempts_remaining": attempts_remaining, "error_code": job.get("error_code"),
                       "last_error_code": job.get("last_error_code"), "next_action": job.get("next_action"),
                       "eligible": False, "reason": None}
        if status == "COMPLETED":
            eligibility["reason"] = "Job already completed; re-running a completed job is a no-op by design."
        elif status in {"RUNNING", "PENDING", "QUEUED"}:
            eligibility["reason"] = f"Job is {status}; only a FAILED, QUEUED-after-failure, or NEEDS_REVIEW job is retried." if status != "QUEUED" else "Job is already queued for a worker."
        elif status == "CANCELLED":
            eligibility["reason"] = "Cancelled jobs are not retried; create a new job deliberately."
        elif attempts_remaining <= 0:
            eligibility.update({
                "eligible": True,
                "reason": "Attempt budget exhausted: a retry requires reset_attempts so the extra attempt is deliberate.",
                "requires_reset_attempts": True,
            })
        else:
            eligibility.update({"eligible": True, "reason": "Retryable within the remaining attempt budget."})
        if eligibility.get("requires_reset_attempts") or (attempts_remaining <= 0 and status in ATTENTION_STATUSES):
            eligibility["next_action"] = next_action_for("RETRY_LIMIT_REACHED") or eligibility["next_action"]
        if eligibility["next_action"] is None and status in ATTENTION_STATUSES:
            eligibility["next_action"] = "Read the recorded failure, correct its cause, then retry explicitly."
        if status in OPERATOR_ATTENTION_STATUSES:
            eligibility["gate"] = (
                "Retrying re-evaluates every workflow prerequisite and the deployment approval gate; "
                "an unapproved publish stays blocked."
            )
        return eligibility

    def _begin_execution(self, job: dict[str, Any], worker: JobWorker) -> dict[str, Any]:
        """Open the durable record for this attempt (same shape the worker registry writes)."""
        executions = self.store.state.setdefault("executions", {})
        stamp = _now()
        execution_id = f"execution-{uuid4().hex}"
        record = {
            "execution_id": execution_id,
            "job_id": job["job_id"],
            "worker_id": None,
            "worker_name": getattr(worker, "name", None),
            "attempt_number": int(job.get("attempts") or 0),
            "job_type": job.get("job_type"),
            "project_id": job.get("project_id"),
            "lead_id": job.get("lead_id"),
            "build_id": job.get("build_id"),
            "status": "RUNNING",
            "started_at": stamp,
            "completed_at": None,
            "result": None,
            "error_type": None,
            "error_code": None,
            "error_message": None,
            "artifacts": [],
            "commit_sha": None,
            "logs_reference": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        executions[execution_id] = record
        job["execution_id"] = execution_id
        job["heartbeat_at"] = stamp
        return record

    def _close_execution(self, job: dict[str, Any], status: str, *, result: WorkerResult) -> None:
        record = (self.store.state.get("executions") or {}).get(job.get("execution_id") or "")
        if not isinstance(record, dict):
            return
        stamp = _now()
        output = dict(result.output or {})
        record.update({
            "status": status,
            "completed_at": stamp,
            "updated_at": stamp,
            "duration_seconds": recorded_duration_seconds(record.get("started_at"), stamp),
            "result": {"summary": result.summary, "output": output, "warnings": list(result.warnings or []),
                       "metadata": dict(result.metadata or {})} if result else None,
            "error_type": result.error_type,
            "error_code": result.error_code or (None if status == "COMPLETED" else "WORKER_FAILED"),
            "error_message": sanitize_diagnostic(result.error),
            "artifacts": list(result.artifacts or []),
            "commit_sha": result.commit_sha,
        })
        if output.get("deployment_id") or output.get("validation_id") or output.get("build_id"):
            record["trace"] = {key: output.get(key) for key in
                              ("lead_id", "project_id", "build_id", "validation_id", "deployment_id", "outcome")
                              if output.get(key) is not None}

    def heartbeat(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] != "RUNNING":
            raise ValueError("Only RUNNING jobs may heartbeat.")
        job["heartbeat_at"] = job["updated_at"] = _now()
        self.store._save()
        return job

    def recover_stale(self, *, older_than_seconds: int = 900, now: datetime | None = None) -> list[dict[str, Any]]:
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()
        current = now or datetime.now(timezone.utc)
        recovered = []
        for job in list(self.store.state["jobs"].values()):
            if job["status"] != "RUNNING" or not job.get("heartbeat_at"):
                continue
            heartbeat = datetime.fromisoformat(job["heartbeat_at"].replace("Z", "+00:00"))
            if (current - heartbeat).total_seconds() > older_than_seconds:
                self._close_execution(job, "NEEDS_REVIEW", result=WorkerResult(
                    "NEEDS_REVIEW", error="Worker heartbeat expired; inspect before retrying.",
                    error_type="StaleLease", error_code="HEARTBEAT_EXPIRED",
                ))
                recovered.append(self._finish(job, "NEEDS_REVIEW", error="Worker heartbeat expired; inspect before retrying.",
                                              code="HEARTBEAT_EXPIRED"))
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
        if input is not None:
            if not isinstance(input, dict):
                raise ValueError("Retry input must be a JSON object.")
            merged = dict(job.get("input") or {})
            merged.update(input)
            job["input"] = merged
        # Computed from the record already in hand: calling `retry_eligibility(job_id)` here would
        # re-read through `get()`, which refreshes from the database and discards the merge above.
        eligibility = self._eligibility_for(job)
        if not eligibility["eligible"]:
            raise ValueError(f"Job {job_id} is not eligible for retry: {eligibility['reason']}")
        if eligibility.get("requires_reset_attempts") and not reset_attempts:
            raise ValueError(
                f"Retry limit reached for {job_id} ({job['attempts']}/{job['max_attempts']} attempts). "
                "Re-run with reset_attempts only after the recorded failure has been addressed."
            )
        job["last_error_code"] = job.get("error_code") or job.get("last_error_code")
        job["last_error_at"] = job.get("completed_at") or job.get("updated_at")
        job["last_error_message"] = job.get("error") or job.get("last_error_message")
        job.update({
            "status": "QUEUED",
            "error": None,
            "error_code": None,
            "next_action": None,
            "requires_operator_action": False,
            "completed_at": None,
            "updated_at": _now(),
            "retry_count": int(job.get("retry_count") or 0) + 1,
            "retried_at": _now(),
            "retry_of_attempt": int(job.get("attempts") or 0),
        })
        log_event(severity="INFO", operation="job.retry", code="JOB_RETRIED", message=job.get("job_type"),
                  job_id=job_id, attempt=int(job.get("attempts") or 0))
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
        """Human-readable reason this job must not run yet (``None`` when it may run)."""
        failure = self.gate_failure(job)
        return failure[1] if failure else None

    def gate_failure(self, job: dict[str, Any]) -> tuple[str, str] | None:
        """Same gate as :meth:`prerequisite_failure`, as a stable ``(code, message)`` pair.

        Codes let an operator (and the attention/alert views) group and act on blocked work
        without parsing prose. Every code here means "not attempted": no side effect has happened.
        """
        store = self.store
        lead = store.state["leads"].get(job.get("lead_id")) if job.get("lead_id") else None
        project = store.state["projects"].get(job.get("project_id")) if job.get("project_id") else None
        if job["job_type"] in {"RESEARCH", "QUALIFY", "BUILD"} and not lead:
            return ("LEAD_MISSING", "Lead reference is required and was not found.")
        if job["job_type"] == "BUILD" and lead.get("qualification_status") != "QUALIFIED":
            return ("LEAD_NOT_QUALIFIED", "BUILD requires a QUALIFIED lead.")
        if job["job_type"] in {"VALIDATE", "DEPLOY", "PREPARE_OUTREACH"} and not project:
            return ("PROJECT_MISSING", f"{job['job_type']} requires a project reference.")
        if job["job_type"] == "VALIDATE" and not self._build(job, project):
            return ("BUILD_MISSING", "VALIDATE requires a known build for this project.")
        if job["job_type"] == "DEPLOY":
            build = self._build(job, project)
            validation = self._latest_validation(project)
            if not build:
                return ("BUILD_MISSING", "DEPLOY requires a completed, non-failed build assigned to this project.")
            if not validation:
                return ("VALIDATION_MISSING", "DEPLOY requires a recorded validation for the assigned build.")
            if validation.get("status") != "PASS":
                return ("VALIDATION_NOT_PASSED", "DEPLOY requires PASS validation.")
            if project.get("review_status") in {"REJECTED", "CHANGES_REQUESTED"} or project.get("current_stage") not in {"READY_FOR_REVIEW", "PREVIEW_DEPLOYED"}:
                return ("STAGE_NOT_REVIEWABLE", "DEPLOY requires a project build in READY_FOR_REVIEW (not rejected or awaiting rebuild).")
            if not job.get("dry_run", True):
                # A real publish must be covered by an explicit human approval for this exact build;
                # a dry run is not a deployment and needs none. The gate is checked here as well as at
                # the publish boundary so every execution path that can run a DEPLOY job is covered -
                # including POST /jobs/:id/retry?run and POST /run, which bypass the create-time
                # job-type allowlist. It reports authorization; it does not grant it.
                authorize = getattr(store, "authorize_preview_deployment", None)
                if not callable(authorize):
                    return ("STORE_CANNOT_AUTHORIZE", "DEPLOY cannot be executed: the tracking store cannot verify deployment authorization.")
                gate = authorize(project["project_id"], build_id=(build or {}).get("build_id")) or {}
                if gate.get("skip_publish"):
                    return None  # already published: the worker reuses the recorded deployment, never re-publishes
                if not gate.get("authorized"):
                    code = str(gate.get("code") or "APPROVAL_MISSING")
                    return (code, f"DEPLOY requires explicit human approval ({code}): {gate.get('reason')}")
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
                    return ("REAL_PREVIEW_TARGET_MISMATCH", "DEPLOY project_name does not match project's configured isolated_preview_project.")
                target_project = req_project or cfg_project
                if not job_input.get("approve_real_preview") or not target_project or target_project == "ai-website-agent-preview":
                    return ("REAL_PREVIEW_TARGET_REQUIRED", "DEPLOY for REAL_PUBLIC_DATA requires explicit approve_real_preview and an isolated preview project target.")
                from tracking.store import validate_preview_project_name
                try:
                    validate_preview_project_name(str(target_project), real_business=True)
                except ValueError as err:
                    return ("REAL_PREVIEW_TARGET_INVALID", str(err))
                if (
                    project.get("review_status") != "APPROVED_FOR_PREVIEW"
                    or project.get("approved_build_id") != project.get("assigned_build_id")
                    or project.get("approved_action") != "PREVIEW_DEPLOYMENT"
                ):
                    return ("REAL_APPROVAL_REQUIRED", "DEPLOY for REAL_PUBLIC_DATA requires explicit pre-deployment human review approval (APPROVED_FOR_PREVIEW for action PREVIEW_DEPLOYMENT) bound to the current assigned build.")
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
                return ("OUTREACH_NOT_READY", "PREPARE_OUTREACH requires approved human review and a ready project.")
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
                artifacts: list[str] | None = None, error: str | None = None,
                code: str | None = None, next_action: str | None = None) -> dict[str, Any]:
        """Close one job transition with a sanitized, coded, actionable failure record."""
        detail = sanitize_diagnostic(error)
        # A success or an in-flight job has no failure code. A job requeued for retry is not an error
        # state, but the transient cause it is retrying stays recorded so the reason is not lost.
        resolved_code = None if status in {"COMPLETED", "RUNNING"} else (code or ("TRANSIENT_FAILURE" if status == "QUEUED" else None))
        updates = {
            "status": status,
            "output": output if output is not None else job.get("output"),
            "artifacts": artifacts if artifacts is not None else job.get("artifacts", []),
            "error": detail,
            "updated_at": _now(),
            "error_code": resolved_code,
            "next_action": next_action or (next_action_for(resolved_code, project_id=job.get("project_id"),
                                                            build_id=job.get("build_id"), job_type=job.get("job_type"))
                                           if resolved_code else None),
            "requires_operator_action": status in OPERATOR_ATTENTION_STATUSES
            or (status == "FAILED" and int(job.get("attempts") or 0) >= int(job.get("max_attempts") or self.max_attempts)),
        }
        if status == "COMPLETED":
            updates["failure"] = None
        elif detail or resolved_code:
            updates["failure"] = {"code": resolved_code, "message": detail, "stage": status,
                                  "attempt": int(job.get("attempts") or 0), "recorded_at": _now()}
        job.update(updates)
        if status in {"COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "BLOCKED"}:
            job["completed_at"] = _now()
        self.store._save()
        if status != "COMPLETED":
            log_event(
                severity="CRITICAL" if status == "BLOCKED" and not detail else "ERROR" if status == "FAILED" else "WARNING",
                operation=f"job.{job.get('job_type', 'unknown').lower()}",
                code=resolved_code,
                message=detail,
                job_id=job.get("job_id"),
                lead_id=job.get("lead_id"),
                project_id=job.get("project_id"),
                build_id=job.get("build_id"),
                attempt=job.get("attempts"),
                execution_id=job.get("execution_id"),
            )
        return job
