"""Non-secret local health and readiness reports; no network calls are made.

The reports answer three different questions, and an operator must never have to infer which one
they are looking at:

* **Liveness** - this process is executing and can produce a report.
* **Readiness** - the source of truth is reachable and writable, so work can be trusted; verified
  by :func:`verify_storage`, which is the only function here that touches the database.
* **Operational state** - what the recorded jobs, projects and deployment attempts say: what is
  running, what is blocked, and what needs a human.

Everything else is derived from already-loaded state and local files, so ``system_health()`` never
opens a database connection or contacts a provider. Reports carry codes, sanitized messages and the
safe next action, and never carry credentials, connection strings or record content.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tracking.database import is_production_mode, sanitize_db_error

from .jobs import ATTENTION_STATUSES, NEXT_ACTIONS, OPERATOR_ATTENTION_STATUSES, next_action_for
from .observability import recorded_age_seconds, redact_local_paths, sanitize_diagnostic, url_host

#: A RUNNING job with no heartbeat newer than this is reported as needing operator attention.
#: It matches the default ``recover-stale`` threshold so the report and the recovery command agree.
STALE_RUNNING_SECONDS = 900
#: How many attention items a report carries; the totals stay accurate regardless.
ATTENTION_LIMIT = 40
#: Upper bound on build directories inspected for publish evidence in one report.
DEPLOYMENT_SCAN_LIMIT = 200


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


def _checked_at(now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    return reference.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def job_attention_items(store, *, now: datetime | None = None, limit: int = ATTENTION_LIMIT) -> tuple[list[dict], dict]:
    """Jobs whose recorded state says a human must act, plus accurate totals.

    Ages and durations are only reported when the job actually recorded the timestamp they come
    from; a missing ``heartbeat_at`` yields ``None`` instead of an assumed value.
    """
    jobs = [job for job in (store.state.get("jobs") or {}).values() if isinstance(job, dict)]
    items: list[dict] = []
    counts = {"running": 0, "stale_running": 0, "undated_running": 0, "needs_review": 0, "blocked": 0,
              "failed": 0, "retry_exhausted": 0}
    for job in jobs:
        status = str(job.get("status") or "UNKNOWN")
        attempts = int(job.get("attempts") or 0)
        maximum = int(job.get("max_attempts") or 0)
        exhausted = maximum > 0 and attempts >= maximum
        code = str(job.get("error_code") or job.get("last_error_code") or "")
        if status == "RUNNING":
            counts["running"] += 1
            age = recorded_age_seconds(job.get("heartbeat_at") or job.get("started_at"), now=now)
            if age is None:
                # Nothing recorded when the claim happened, so no staleness threshold can be applied
                # honestly - and recovery must not guess. That is exactly why the job belongs here: an
                # invisible RUNNING job would never be looked at, while `recover-stale` refuses it.
                counts["undated_running"] += 1
                items.append({
                    "kind": "undated_job",
                    "code": "HEARTBEAT_UNDATED",
                    "job_id": job.get("job_id"),
                    "job_type": job.get("job_type"),
                    "project_id": job.get("project_id"),
                    "lead_id": job.get("lead_id"),
                    "build_id": job.get("build_id"),
                    "execution_id": job.get("execution_id"),
                    "status": status,
                    "message": "RUNNING with no recorded heartbeat, started_at, or updated_at, so its age "
                               "cannot be established from the ledger.",
                    "attempts": attempts,
                    "max_attempts": maximum,
                    "age_seconds": None,
                    "recorded_at": None,
                    "next_action": NEXT_ACTIONS["HEARTBEAT_UNDATED"],
                })
                continue
            if age > STALE_RUNNING_SECONDS:
                counts["stale_running"] += 1
                # The same code choice `recover_stale` makes, from the same evidence: a job whose worker
                # never reached its first heartbeat is a different investigation from one that stopped
                # beating mid-run, and the next step an operator is told to take differs too.
                stale_code = "HEARTBEAT_EXPIRED" if job.get("heartbeat_at") else "HEARTBEAT_UNDATED"
                items.append({
                    "kind": "stale_job",
                    "code": stale_code,
                    "job_id": job.get("job_id"),
                    "job_type": job.get("job_type"),
                    "project_id": job.get("project_id"),
                    "lead_id": job.get("lead_id"),
                    "build_id": job.get("build_id"),
                    "execution_id": job.get("execution_id"),
                    "status": status,
                    "message": (f"No heartbeat recorded for {age}s (last: {job.get('heartbeat_at') or job.get('started_at')})."
                                if job.get("heartbeat_at") else
                                f"RUNNING for {age}s since it was claimed at {job.get('started_at')} with no "
                                "heartbeat ever recorded, so the worker is presumed to have died on claim."),
                    "attempts": attempts,
                    "max_attempts": maximum,
                    "age_seconds": age,
                    "recorded_at": job.get("heartbeat_at") or job.get("started_at"),
                    "next_action": NEXT_ACTIONS[stale_code],
                })
            continue
        # A failure with attempt budget left is the system's own business: it is still listed by
        # `job_system`, `list-jobs --status FAILED`, and the API filters. Operator attention means a
        # state that will not resolve without a human, or a permanent failure.
        if status not in OPERATOR_ATTENTION_STATUSES and not (
            status == "FAILED" and (exhausted or job.get("requires_operator_action"))
        ) and not job.get("requires_operator_action"):
            continue
        if status == "NEEDS_REVIEW":
            counts["needs_review"] += 1
        elif status == "BLOCKED":
            counts["blocked"] += 1
        elif status == "FAILED":
            counts["failed"] += 1
        if exhausted and status == "FAILED":
            counts["retry_exhausted"] += 1
        items.append({
            "kind": "job",
            "code": code or status,
            "job_id": job.get("job_id"),
            "job_type": job.get("job_type"),
            "project_id": job.get("project_id"),
            "lead_id": job.get("lead_id"),
            "build_id": job.get("build_id"),
            "execution_id": job.get("execution_id"),
            "status": status,
            "message": sanitize_diagnostic(job.get("error")),
            "attempts": attempts,
            "max_attempts": maximum,
            "retries_recorded": int(job.get("retry_count") or 0),
            "retry_exhausted": exhausted,
            "retry_eligible": status != "CANCELLED",
            "requires_reset_attempts": exhausted,
            "age_seconds": recorded_age_seconds(job.get("completed_at") or job.get("updated_at"), now=now),
            "recorded_at": job.get("completed_at") or job.get("updated_at"),
            "next_action": job.get("next_action") or next_action_for(code or None) or (
                NEXT_ACTIONS["RETRY_LIMIT_REACHED"] if exhausted else None
            ),
        })
    items.sort(key=lambda item: (0 if item["kind"] == "stale_job" else 1, str(item.get("recorded_at") or "")), reverse=False)
    counts["total"] = len(items)
    return items[:limit], counts


def project_attention_items(store, *, limit: int = ATTENTION_LIMIT) -> list[dict]:
    """Projects whose recorded stage, validation or review state stops the pipeline."""
    items: list[dict] = []
    for project in (store.state.get("projects") or {}).values():
        if not isinstance(project, dict):
            continue
        project_id = project.get("project_id")
        build = (store.state.get("builds") or {}).get(project.get("assigned_build_id")) or {}
        validation = (store.state.get("validations") or {}).get(project.get("latest_validation_id")) or {}
        deployment = (store.state.get("deployments") or {}).get(project.get("latest_deployment_id")) or {}
        stage = str(project.get("current_stage") or "")
        review = str(project.get("review_status") or "PENDING")

        def add(code: str, message: str, action: str, *, kind: str = "project") -> None:
            items.append({
                "kind": kind,
                "code": code,
                "project_id": project_id,
                "lead_id": project.get("lead_id"),
                "build_id": build.get("build_id"),
                "validation_id": validation.get("validation_id"),
                "deployment_id": deployment.get("deployment_id"),
                "status": stage or review,
                "stage": stage,
                "review_status": review,
                "message": message,
                "recorded_at": project.get("updated_at"),
                "age_seconds": recorded_age_seconds(project.get("updated_at")),
                "next_action": action,
            })

        if build and str(build.get("status") or "") == "FAILED":
            add("BUILD_FAILED", f"The assigned build {build.get('build_id')} is recorded as FAILED.",
                "Rebuild the site (BUILD job) and re-validate before deploying.")
        elif validation and str(validation.get("status") or "") not in {"", "PASS"}:
            add("VALIDATION_NOT_PASSED",
                f"Validation {validation.get('validation_id')} recorded {validation.get('status')} for the assigned build.",
                "Correct the findings, rebuild, and re-validate; a non-PASS validation never authorizes deployment.")
        if review in {"REJECTED", "CHANGES_REQUESTED"}:
            notes = str(project.get("review_notes") or "").strip()
            add(f"REVIEW_{review}",
                f"The last human decision for build {build.get('build_id')} was {review.replace('_', ' ').lower()}"
                + (f"; notes: {notes[:400]}" if notes else "; no notes recorded"),
                "Apply the reviewer's notes, rebuild and re-validate, then re-review; "
                f"notes: {project.get('review_notes') or 'none recorded'}.")
        elif stage == "READY_FOR_REVIEW" and review not in {"APPROVED", "APPROVED_FOR_PREVIEW"}:
            add("AWAITING_HUMAN_REVIEW",
                f"Build {build.get('build_id')} is awaiting a human decision (review_status {review or 'PENDING'}).",
                "Review the draft, then record the decision: python3 -m tracking.cli review "
                f"{project_id} --decision <APPROVED|REJECTED|CHANGES_REQUESTED> --notes \"...\" "
                f"--confirm {project_id}:{build.get('build_id')}:<decision>.")
        elif stage in {"AWAITING_APPROVAL", "BLOCKED", "FAILED"}:
            add(f"STAGE_{stage}", f"Project is recorded at stage {stage}.",
                "Inspect the blocking job and clear it before continuing the pipeline.")
        if deployment and deployment.get("build_id") and deployment.get("build_id") != project.get("assigned_build_id"):
            add("DEPLOYMENT_SUPERSEDED_BY_NEW_BUILD",
                f"Recorded deployment {deployment.get('deployment_id')} covers build {deployment.get('build_id')}, not the assigned build.",
                "Deploy the current assigned build; the published preview is stale.")
    items.sort(key=lambda item: str(item.get("recorded_at") or ""))
    return items[:limit]


def _relative_path(value: Any, root: Path) -> str | None:
    """Repository-relative form of a recorded path, or just its file name when it lies outside the workspace."""
    if not value:
        return None
    try:
        return Path(str(value)).resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return Path(str(value)).name


def deployment_report(store, *, repository_root: Path | str = ".", include_evidence: bool = True) -> dict:
    """Classify every project's publish state from recorded evidence only.

    ``VERIFIED_RECORDED`` means a successful publish with a URL was recorded for the *currently
    assigned* build. ``UNCERTAIN`` means an attempt ended without a terminal result - the provider
    may or may not have accepted it - which is deliberately not reported as failed or succeeded.
    Nothing here contacts the provider, so a live URL check is never implied.
    """
    root = Path(repository_root)
    outcomes: dict[str, int] = {}
    attention: list[dict] = []
    scanned = 0
    for project in (store.state.get("projects") or {}).values():
        if not isinstance(project, dict):
            continue
        build = (store.state.get("builds") or {}).get(project.get("assigned_build_id")) or {}
        deployment = (store.state.get("deployments") or {}).get(project.get("latest_deployment_id")) or {}
        entry: dict[str, Any] = {
            "project_id": project.get("project_id"),
            "lead_id": project.get("lead_id"),
            "build_id": build.get("build_id"),
            "deployment_id": deployment.get("deployment_id"),
            "deployment_status": deployment.get("deployment_status"),
            "deployment_url_host": url_host(deployment.get("deployment_url")),
            "dry_run": deployment.get("dry_run"),
            "recorded_at": deployment.get("created_at"),
            "provider_contacted": False,
        }
        if deployment.get("deployment_status") == "DEPLOYED" and deployment.get("build_id") == project.get("assigned_build_id"):
            state, code = "VERIFIED_RECORDED", "PROVIDER_URL_RECORDED" if deployment.get("deployment_url") else "OUTCOME_RECORDED_WITHOUT_URL"
            entry["state"] = state
            entry["code"] = code
            outcomes[state] = outcomes.get(state, 0) + 1
            continue
        ledger_state = None
        output_location = build.get("output_location")
        if include_evidence and output_location and scanned < DEPLOYMENT_SCAN_LIMIT:
            scanned += 1
            try:
                from deployment.authorization import publish_outcome

                build_dir = Path(output_location).parent
                if not build_dir.is_absolute():
                    build_dir = root / build_dir
                if build_dir.is_dir():
                    ledger_state = publish_outcome(build_dir)
            except Exception as error:  # A missing/unreadable ledger is reported, never guessed around.
                ledger_state = {"state": "EVIDENCE_UNREADABLE", "code": "PUBLISH_LEDGER_UNREADABLE",
                                "next_action": sanitize_diagnostic(error)}
        if ledger_state:
            latest = ledger_state.get("latest_attempt") or None
            if isinstance(latest, dict):
                latest = {
                    key: (url_host(value) if key.endswith("deployment_url") else sanitize_diagnostic(value) if key == "error" else value)
                    for key, value in latest.items()
                }
            entry.update({
                "state": ledger_state["state"],
                "code": ledger_state["code"],
                "attempt_count": ledger_state.get("attempt_count"),
                "latest_attempt": latest,
                # `redact_local_paths` here, not in `publish_outcome`: the same report is printed by the CLI
                # and served over HTTP, and a network answer must not map the host filesystem. The
                # repository-relative `ledger_path` below carries the actionable location in both surfaces.
                "next_action": redact_local_paths(ledger_state.get("next_action")),
                # Naming the file an operator has to read, in repository-relative form: this report is also
                # served over HTTP, where an absolute path would map the host filesystem.
                "ledger_path": _relative_path(ledger_state.get("ledger"), root),
            })
            # An operator decision is reported alongside the unresolved publish, never instead of it:
            # "someone decided to retry" is not the same fact as "the provider accepted the publish".
            if ledger_state.get("resolution_count"):
                entry["operator_resolution_count"] = ledger_state["resolution_count"]
                resolution = ledger_state.get("latest_resolution")
                if isinstance(resolution, dict):
                    entry["latest_operator_resolution"] = {
                        key: (url_host(value) if key.endswith("deployment_url") else
                              sanitize_diagnostic(value) if key in ("resolution_note",) else value)
                        for key, value in resolution.items()
                    }
            if ledger_state.get("ledger_readable") is False:
                # The problem text names a file; keep it repository-relative so a monitoring answer
                # describes the workspace without mapping the host filesystem.
                entry["ledger_problem"] = redact_local_paths(ledger_state.get("ledger_problem"))
        else:
            entry["state"] = "NOT_ATTEMPTED" if not deployment else "SUPERSEDED"
            entry["code"] = "NO_PUBLISH_RECORD" if not deployment else "DEPLOYMENT_SUPERSEDED_BY_NEW_BUILD"
            entry["next_action"] = NEXT_ACTIONS.get("PUBLISH_STATE_UNCERTAIN") if entry["state"] == "UNCERTAIN" else (
                "Deploy the current assigned build through an authorized, executed DEPLOY job." if entry["state"] == "SUPERSEDED"
                else "Run a deployment check first: python3 -m deployment.cli --project-id <id> (dry run, publishes nothing)."
            )
        outcomes[entry["state"]] = outcomes.get(entry["state"], 0) + 1
        if entry["state"] in {"UNCERTAIN", "FAILED", "RECORDED_UNCONFIRMED", "EVIDENCE_UNREADABLE", "SUPERSEDED"}:
            attention.append(entry)
    # Execution records that already reported an uncertain publish, even when no local build
    # directory survives to read a ledger from.
    reported_projects = {item.get("project_id") for item in attention}
    for execution in (store.state.get("executions") or {}).values():
        if not isinstance(execution, dict):
            continue
        # Every execution that reported an uncertain publish belongs here. Stopping after the first one
        # would understate the work an operator has to do, and a project whose build directory survives
        # is already covered by its ledger entry above.
        if execution.get("project_id") in reported_projects:
            continue
        if str(execution.get("error_code") or "") in {"PUBLISH_STATE_UNCERTAIN", "PUBLISH_UNCERTAIN"}:
            attention.append({
                "kind": "execution",
                "project_id": execution.get("project_id"),
                "job_id": execution.get("job_id"),
                "execution_id": execution.get("execution_id"),
                "state": "UNCERTAIN",
                "code": str(execution.get("error_code")),
                "message": execution.get("error_message"),
                "recorded_at": execution.get("completed_at"),
                "next_action": NEXT_ACTIONS["PUBLISH_STATE_UNCERTAIN"],
                "provider_contacted": True,
            })
            reported_projects.add(execution.get("project_id"))
    return {
        "status": "degraded" if attention else "healthy",
        "outcomes": outcomes,
        "attention": attention[:ATTENTION_LIMIT],
        "attention_count": len(attention),
        "build_directories_scanned": scanned,
        "evidence_sources": ["recorded deployment state", "per-build publish attempt ledger"],
        "live_url_checked": False,
        "reason": None,
    }


#: The four things an operator keeps confusing: the code works, the configuration names real things,
#: a service was actually reached, and a preview exists. Every preflight section states which of these
#: it answered, so "configured" is never printed as "connected".
ASSURANCE_LEVELS = ("code_readiness", "configuration_readiness", "connectivity", "preview_deployment")


def channels_section(store, *, environment: dict[str, str] | None = None) -> dict:
    """Whether an operator can *reach* this system (Telegram) and whether mail can reach it (Gmail).

    Configuration only, and labelled as such: no provider is contacted to answer this. That distinction matters
    more here than anywhere else in the report, because a token in the environment is not a working bot - the
    token could be revoked, the allow-list could name the wrong account, and the mailbox could refuse every call.
    The honest fields are therefore "configured" and "unverified", and ``telegram-check`` / ``email-check`` are
    the commands that ask the provider.
    """
    from integrations.gmail import gmail_config
    from integrations.gmail_send import send_config
    from integrations.telegram import telegram_config

    from .controls import hold_view

    source = os.environ if environment is None else environment
    telegram = telegram_config(source)
    gmail = gmail_config(source)
    sending = send_config(source)
    hold = hold_view(store)
    sync = dict((((getattr(store, "state", {}) or {}).get("provider_sync") or {}).get("gmail_inbox")) or {})
    return {
        "verified": False,
        "evidence": "read from this process's environment and the recorded control state; no provider was contacted",
        "telegram": {
            "configured": bool(telegram["configured"]),
            "token_present": bool(telegram["token_present"]),
            "token_length": telegram["token_length"],
            "allowed_user_ids": telegram["allowed_user_ids"],
            "allowed_chat_ids": telegram["allowed_chat_ids"],
            "authorization_mode": ("user id and chat id must both match" if telegram["allowed_user_ids"]
                                   and telegram["allowed_chat_ids"] else
                                   "user ids only" if telegram["allowed_user_ids"] else
                                   "chat ids only" if telegram["allowed_chat_ids"] else
                                   "nothing - an empty allow-list authorizes nobody"),
            "transport": "long polling via getUpdates; no webhook URL is exposed or required",
            "live_account_verified": False,
        },
        "email": {
            "configured": bool(gmail["configured"]),
            "missing": gmail["missing"],
            "account": gmail["account"],
            "scope": gmail["scope"],
            "query": gmail["query"],
            "fetch_format": gmail["fetch_format"],
            "capabilities": "inbound: read-only list/get/history; no send, delete, modify, or mark-as-read "
                            "exists in that adapter",
            "sync": {key: sync.get(key) for key in ("last_run_at", "last_history_id", "cycles", "last_error")
                     if sync.get(key) is not None} or None,
            "inbound_adapter": "integrations/gmail.py (read-only by construction)",
            # Separate because it is a separate interface with separate credentials: the read-only client cannot
            # send, and a send scope cannot read. One block per capability is what keeps that visible in a report.
            "sending": {
                "state": sending["state"],
                "scope": sending["scope"],
                "enabled": sending["enabled"],
                "configured": sending["configured"],
                "missing": sending["missing"],
                "account": sending["account"],
                "unsubscribe_url_configured": bool(sending["unsubscribe_url"]),
                "verification": "unverified unless `email-send-check` has run on this machine",
                "outbound_adapter": "integrations/gmail_send.py (one method: send)",
                "send_enabled_for_jobs_or_workflows": (sending["state"] in {"configured", "verified"}
                                                       and sending["enabled"]),
                "live_effect_flag": "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH",
                "live_effect_enabled": (source.get("AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH") or "").strip()
                                       in {"1", "true", "yes", "on"},
                "results_provable": ["REPORTED_SENT", "ACCEPTED_BY_PROVIDER"],
                "results_unreachable": ["VERIFIED_DELIVERED"],
                "delivery_receipt_available": False,
                "next_action": sending["next_action"],
            },
            "live_account_verified": False,
            "live_sending_verified": False,
        },
        "automation_hold": {"held": hold["held"], "set_at": hold.get("set_at"), "set_by": hold.get("set_by"),
                            "reason": hold.get("reason"), "unreadable": hold.get("unreadable", False),
                            "verified": True},
        "meaning": ("configured means this environment names the credential and the allow-list; whether the "
                    "provider accepts them is only known by asking, which these commands do not do. Receiving "
                    "and sending are reported separately because they are separately authorized: an inbound "
                    "mailbox that works says nothing about a send credential, and vice versa."),
    }


def integration_preflight(store, *, repository_root: Path | str = ".", host: str | None = None,
                          port: int | None = None, allow_non_loopback: bool = False,
                          project_id: str | None = None, now: datetime | None = None,
                          storage_error: str | None = None) -> dict:
    """One answer to "is this workspace ready to run a controlled preview", without touching anything.

    Composed from the same functions the health endpoints use, and deliberately read-only: no socket is
    bound, no DDL runs, no provider command executes, no network call is made. Each section carries
    ``verified`` (an answer from evidence gathered now) or is marked configuration-only, because the
    useful distinction for an operator is not pass/fail but *what still has to happen outside this
    repository*.
    """
    # Local import: api imports this module, so importing it at module scope would be circular.
    from .api import ALLOWED_API_JOB_TYPES, resolve_bind_config

    root = Path(repository_root)
    environment = os.environ
    sections: dict[str, Any] = {}
    blockers: list[str] = []
    manual_steps: list[str] = []
    next_actions: list[str] = []

    # -- 1. API liveness: what `serve` would do with this configuration, decided without binding.
    bind: dict[str, Any] = {"verified": False, "evidence": "configuration only; no socket was opened"}
    try:
        resolved_host, resolved_port, outside_loopback = resolve_bind_config(host, port, allow_non_loopback=allow_non_loopback)
        token = environment.get("AI_WEBSITE_AGENT_API_TOKEN")
        bind.update({
            "ok": True, "bind_host": resolved_host, "bind_port": resolved_port,
            "reachable_beyond_this_machine": outside_loopback,
            "liveness_route": "/healthz", "liveness_requires_token": False,
            "token_configured": bool(token),
            "token_strength": (None if not token else
                               "ok" if (len(token.strip()) >= 32 and outside_loopback) or not outside_loopback
                               else "too_short_for_public_bind"),
        })
        if not token:
            bind["ok"] = False
            blockers.append("AI_WEBSITE_AGENT_API_TOKEN is not set; `serve` refuses to start without it.")
            next_actions.append("Set AI_WEBSITE_AGENT_API_TOKEN (32+ characters for any non-loopback bind), then start the API.")
        elif bind["token_strength"] != "ok":
            # `serve` refuses this bind. A preflight that reported ok would send an operator to a server
            # that never starts, so the refusal is reproduced here rather than discovered later. The
            # token value itself is never copied into the report.
            bind["ok"] = False
            bind["would_be_refused_by_serve"] = True
            blockers.append(
                "AI_WEBSITE_AGENT_API_TOKEN is shorter than 32 characters while this bind would be "
                "reachable beyond this machine, so `serve` refuses to start."
            )
            next_actions.append("Replace the token with a 32+ character secret in the environment, then restart.")
    except Exception as error:  # a refused bind is the answer, not a crash
        bind.update({"ok": False, "error": sanitize_diagnostic(error)})
        blockers.append(f"Bind configuration refused: {sanitize_diagnostic(error)}")
    sections["api_liveness"] = bind

    # -- 4. Automation (n8n): importable files, required environment, manual edits, activation state.
    from .n8n_runner import workflow_readiness

    workflow_dir = root / "n8n" / "workflows"
    exported = sorted(workflow_dir.glob("*.json")) if workflow_dir.is_dir() else []
    automation = workflow_readiness(exported, env=dict(environment))
    automation["verified"] = bool(exported)
    automation["evidence"] = "parsed locally; no n8n runtime was contacted and no workflow was activated"
    automation["api_url_configured"] = bool(environment.get("AI_WEBSITE_AGENT_API_URL"))
    automation["expected_api_url"] = f"http://{resolved_host or '127.0.0.1'}:{resolved_port or 8787}" if bind.get("ok") else None
    sections["automation_n8n"] = automation
    if not exported:
        manual_steps.append("No workflow JSON found to check: n8n/workflows/ is missing or empty in this workspace.")
    if automation["missing_environment"]:
        blockers.append("n8n workflows read environment variables that are not set here: "
                        + ", ".join(automation["missing_environment"])
                        + " (an unset $env.AI_WEBSITE_AGENT_API_URL renders to an empty URL).")
        next_actions.append("Set AI_WEBSITE_AGENT_API_URL (and AI_WEBSITE_AGENT_API_TOKEN) in the environment n8n reads.")
    if automation["workflows_needing_manual_edit"]:
        manual_steps.append("Edit before importing in the n8n UI (starter placeholders): "
                            + ", ".join(Path(item).name for item in automation["workflows_needing_manual_edit"]))

    # -- 2. Storage readiness: actively probed when a backend exists, so this row is not a guess.
    # A store that could not even be built (production mode with no database URL) is the most important
    # thing this command can report, so it is reported as an answer rather than raised as a traceback.
    if store is None:
        # Everything that does not need tracking state is still answered, so a broken production
        # configuration is fixed in one pass rather than one error per run.
        blockers.insert(0, storage_error or "Tracking state is unavailable.")
        manual_steps.insert(0, "Storage is unavailable: the pipeline cannot read or record any project state.")
        next_actions.insert(0, "Configure AI_WEBSITE_AGENT_ENV/AI_WEBSITE_AGENT_STORAGE_BACKEND only with a "
                              "real DATABASE_URL - production mode refuses local JSON by design - then run "
                              "`python3 -m tracking.cli preflight-db` to identify the configured target.")
        return {
            "checked_at": (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "status": "blocked",
            "sections": {"api_liveness": bind,
                         "storage_readiness": {"status": "unavailable", "verified": False, "backend": None,
                                              "error": storage_error,
                                              "evidence": "the store could not be constructed",
                                              "json_fallback": "prohibited in production mode"},
                         "workflow_execution_readiness": {"ok": False, "verified": False,
                                                          "evidence": "not evaluated: storage is unavailable"},
                         "automation_n8n": sections["automation_n8n"],
                         "deployment_provider_configuration": {"ok": False, "verified": False,
                                                                "evidence": "not evaluated: storage is unavailable"}},
            "blockers": blockers,
            "manual_steps": manual_steps,
            "next_actions": next_actions,
            "assurance": {"code_readiness": "verified", "configuration_readiness": "verified",
                          "connectivity": "failed", "preview_deployment": "not_performed"},
            "actions_not_performed": {"network_calls": 0, "provider_commands_executed": 0,
                                      "ddl_executed": False, "socket_bound": False,
                                      "secrets_printed": False, "state_mutated": False},
            "exit_code": 1,
        }
    storage = verify_storage(store)
    storage["json_fallback"] = (
        "prohibited in production mode" if is_production_mode()
        else "permitted only outside production mode (single-process local JSON)"
    )
    storage["verified"] = getattr(store, "db_backend", None) is not None
    storage["evidence"] = ("verify_schema() was run against the configured database"
                           if storage["verified"] else "local JSON state file; no database was contacted")
    sections["storage_readiness"] = storage
    if storage["status"] != "healthy":
        blockers.append(f"Storage is {storage['status']}: {storage.get('error')}")
        next_actions.append("Run `python3 -m tracking.cli preflight-db` for the database identity, migration state, and next command.")

    # -- 3. Workflow execution readiness: can a job be claimed, executed, and recovered here.
    attention = operator_attention(store, now=now, repository_root=root, include_deployment=False)
    workers = dict((getattr(store, "state", {}) or {}).get("workers") or {})
    attention_items = list(attention.get("items") or [])
    workflow = {
        "ok": not attention.get("requires_attention"),
        "verified": True,
        "evidence": "read from the recorded job, worker, and project state in this store",
        # The combined human-actionable backlog, not just the job rows: two projects waiting for a
        # review decision is real work even when the queue is empty.
        "attention_count": len(attention_items),
        "attention_codes": sorted({str(item.get("code")) for item in attention_items if item.get("code")}),
        "counts": attention.get("counts", {}),
        "registered_workers": len(workers),
        "worker_execution": "LocalPipelineWorker runs in-process inside `serve`/`run-job`; no external queue exists",
        "job_types_reachable_over_http": sorted(ALLOWED_API_JOB_TYPES),
        "deployment_jobs_reachable_over_http": "DEPLOY" in ALLOWED_API_JOB_TYPES,
    }
    sections["workflow_execution_readiness"] = workflow
    if workflow["deployment_jobs_reachable_over_http"]:
        blockers.append("DEPLOY is reachable over the job API; the documented deployment path is the CLI.")
    if attention.get("requires_attention"):
        kinds = attention.get("counts", {}).get("by_kind") or {}
        detail = ", ".join(f"{name}: {count}" for name, count in sorted(kinds.items())) or "no detail recorded"
        first = attention_items[0] if attention_items else {}
        manual_steps.append(
            f"{len(attention_items)} item(s) need an operator before anything else is scheduled ({detail}); "
            f"first: {first.get('code', 'unknown')} on {first.get('project_id') or first.get('job_id') or 'unknown target'}"
            " - `python3 -m orchestration.cli attention --brief`")
        next_actions.append("Clear the attention backlog first: `python3 -m orchestration.cli recover-stale` then `attention --brief`.")

    # -- 4. Deployment provider: configuration only, and the approval gate's current answer.
    provider = provider_section(store, root, include_evidence=False)
    configuration = provider.get("configuration", {})
    sections["deployment_provider_configuration"] = {
        "verified": False,
        "evidence": "read from local files and PATH; no provider command was executed and no network call was made",
        "provider": configuration.get("provider", "vercel"),
        "cli_available": configuration.get("cli_available", False),
        "project_linked": configuration.get("project_linked", False),
        "link_error": configuration.get("link_error"),
        "expected_preview_project": configuration.get("expected_preview_project"),
        "target_is_isolated": configuration.get("target_is_isolated"),
        "production_publishing": "not executed by this repository's commands without --execute and an approval",
        "recorded_outcomes": {key: provider.get(key) for key in ("outcomes", "uncertain_count", "attention_count") if key in provider},
    }
    if not configuration.get("cli_available"):
        manual_steps.append("Install and authenticate the Vercel CLI in the environment that will publish, then link the "
                            "dedicated preview project (`vercel link` writes .vercel/project.json).")
    if not configuration.get("project_linked"):
        blockers.append("No linked Vercel preview project; a real publish is impossible from here by design.")
        next_actions.append("Link the preview project: `vercel link --project ai-website-agent-preview` (never the production project).")
    elif configuration.get("target_is_isolated") is False:
        blockers.append("The linked project is not the isolated preview project; publishing would target the wrong scope.")

    # -- 5. Operator channels: can a human reach the bot, can mail arrive, and is dispatch held.
    channels = channels_section(store, environment=environment)
    sections["operator_channels"] = channels
    if not channels["telegram"]["configured"]:
        manual_steps.append("Optional: set AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN and "
                            "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS for a phone-side operator console "
                            "(`python3 -m orchestration.cli telegram-check` asks Telegram whether the token works).")
    elif not channels["telegram"]["allowed_user_ids"] and not channels["telegram"]["allowed_chat_ids"]:
        blockers.append("A Telegram token is set with no allow-list, so the bot would refuse every operator. Name "
                        "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS (or _ALLOWED_CHAT_IDS) deliberately.")
    if not channels["email"]["configured"]:
        manual_steps.append("Optional: authorize a mailbox with the read-only Gmail scope and set "
                            "AI_WEBSITE_AGENT_GMAIL_REFRESH_TOKEN/_CLIENT_ID/_CLIENT_SECRET to receive replies "
                            f"(missing: {', '.join(channels['email']['missing']) or 'none'}).")
    if channels["automation_hold"]["held"]:
        manual_steps.append(f"Automated dispatch is on hold (set {channels['automation_hold']['set_at']} by "
                            f"{channels['automation_hold']['set_by']}); release it with "
                            "`python3 -m orchestration.cli release-automation --actor <you>` before scheduling work.")
    if channels["automation_hold"]["unreadable"]:
        blockers.append("The automation control record is present but unreadable; inspect "
                        "automation_controls.dispatch before trusting any hold answer.")

    # -- 6. Preview readiness for one project: the durable gate's own answer, unmodified.
    if project_id:
        try:
            gate = store.authorize_preview_deployment(project_id)
        except Exception as error:  # the gate is authoritative; a refusal is a reportable answer
            gate = {"code": "GATE_UNAVAILABLE", "reason": sanitize_diagnostic(error)}
        sections["preview_readiness"] = {
            "verified": True,
            "evidence": "store.authorize_preview_deployment() - the same durable gate the CLI and worker consult",
            "project_id": project_id,
            "authorized": gate.get("authorized"),
            "code": gate.get("code"),
            "reason": gate.get("reason"),
            "build_id": gate.get("build_id"),
            "action": gate.get("action"),
            "publish_state_uncertain": gate.get("publish_state_uncertain"),
        }
        if gate.get("authorized") is not True:
            # Quote the gate's own reason instead of inventing a next step: for a project that has no
            # build yet, "record the human review decision" would be advice nobody can act on.
            manual_steps.append(f"Project {project_id} is not publishable yet ({gate.get('code')}): "
                                f"{gate.get('reason')}")
        if gate.get("publish_state_uncertain"):
            blockers.append(f"Project {project_id} has an unresolved publish: `deployment.cli --resolve-uncertain-publish --resolve-note ...`")

    assurance = {
        "code_readiness": "verified",
        "configuration_readiness": "verified",
        "connectivity": "verified" if storage["verified"] else "not_tested",
        "preview_deployment": "not_performed - this command never publishes, and a recorded deployment is read back as a record",
    }
    return {
        "checked_at": (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "blocked" if blockers else ("pending_manual_steps" if manual_steps else "ready"),
        "sections": sections,
        "blockers": blockers,
        "manual_steps": manual_steps,
        "next_actions": next_actions,
        "assurance": assurance,
        "actions_not_performed": {
            "network_calls": 0, "provider_commands_executed": 0, "ddl_executed": False,
            "socket_bound": False, "secrets_printed": False, "state_mutated": False,
        },
        "exit_code": 1 if blockers else 0,
    }


def provider_section(store, repository_root: Path, *, include_evidence: bool = True) -> dict:
    """Deployment section for ``system_health``: provider configuration plus recorded outcomes."""
    try:
        from deployment.deployer import provider_configuration

        configuration = provider_configuration(Path(repository_root))
    except Exception as error:  # A missing provider package must degrade the report, not break it.
        configuration = {"provider": "vercel", "cli_available": False, "project_linked": False,
                         "error": sanitize_diagnostic(error)}
    report = deployment_report(store, repository_root=repository_root, include_evidence=include_evidence)
    linked = bool(configuration.get("project_linked"))
    report.update({
        "configuration": configuration,
        # Preserved from the earlier report: a project link is required before a publish can be
        # authorized, so an unlinked workspace is degraded regardless of recorded history.
        "status": "degraded" if (not linked or report["attention"]) else "healthy",
        "reason": None if linked else "No approved Vercel project link.",
    })
    return report


def configuration_section(store, repository_root: Path) -> dict:
    """Presence-only view of the operational configuration (names, never values)."""
    from .observability import log_file_path

    values = {key: os.environ.get(key) for key in (
        "AI_WEBSITE_AGENT_ENV", "AI_WEBSITE_AGENT_API_TOKEN", "AI_WEBSITE_AGENT_STORAGE_BACKEND",
        "AI_WEBSITE_AGENT_API_URL", "AI_WEBSITE_AGENT_LOG_DIR",
    )}
    missing = sorted(name for name, value in values.items() if name not in {
        "AI_WEBSITE_AGENT_ENV", "AI_WEBSITE_AGENT_LOG_DIR", "AI_WEBSITE_AGENT_API_URL"
    } and not str(value or "").strip())
    database_configured = getattr(store, "db_backend", None) is not None or any(
        str(os.environ.get(name) or "").strip() for name in ("DATABASE_URL", "AI_WEBSITE_AGENT_DATABASE_URL", "SUPABASE_DB_URL")
    )
    state_path = Path(getattr(store, "path", Path(repository_root) / "data/tracking/state.json"))
    report = {
        "status": "healthy",
        "environment": str(values.get("AI_WEBSITE_AGENT_ENV") or "development"),
        "production_mode": is_production_mode(),
        "api_token_configured": bool(str(values.get("AI_WEBSITE_AGENT_API_TOKEN") or "").strip()),
        "database_configured": database_configured,
        "state_path_writable_parent": bool(state_path.parent.is_dir() and os.access(state_path.parent, os.W_OK)) or state_path.parent == Path("."),
        "operational_log_enabled": log_file_path() is not None,
        "checks": {
            "secrets_reported": False,
            "note": "Only presence of configuration is reported; values are never read into a report.",
        },
    }
    if report["production_mode"] and (missing or not database_configured):
        report["status"] = "degraded"
        report["missing"] = missing if database_configured else sorted([*missing, "DATABASE_URL"])
    return report


def engagement_attention_items(store, *, today: Any = None, limit: int = ATTENTION_LIMIT) -> list[dict]:
    """What the engagement records are waiting on a person for, in the same shape as every other item.

    Phase 46 put responses, proposals, follow-ups and opt-outs in the ledger and Phase 47 added an inbound
    intake, but ``operator_attention()`` still only reported jobs, projects and publishes - so a business
    could reply to a draft and nothing surfaced anywhere. This closes that gap by reading, never deciding:
    due follow-ups, undecided proposals and unanswered replies come from the ledger's own
    :meth:`tracking.engagement.EngagementLedger.outstanding_work` (one source of truth, no second query),
    and the intake rows from :meth:`tracking.inbound.InboundIntake.operator_backlog`.

    Items are deduped by code and lead, so a twenty-message thread is one item and not twenty alerts, and
    the whole section is bounded by ``limit`` like the others. None of these codes is a *critical* alert:
    an unanswered enquiry is work, not an outage.
    """
    from tracking.engagement import EngagementLedger
    from tracking.inbound import InboundIntake

    ledger = EngagementLedger(store, source="health-attention")
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, message: str, action: str, *, kind: str = "engagement", lead_id: Any = None,
            project_id: Any = None, status: Any = None, stage: Any = None, recorded_at: Any = None,
            **extra: Any) -> None:
        # One item per (code, record), so the same work is never listed twice while two genuinely different
        # follow-ups, proposals, or inbound events for one lead stay two items.
        key = (code, str(extra.get("dedupe_key") or extra.get("follow_up_id") or extra.get("proposal_id")
                         or extra.get("engagement_id") or lead_id or project_id or ""))
        if key in seen:
            return
        seen.add(key)
        items.append({
            "kind": kind,
            "code": code,
            "project_id": project_id,
            "lead_id": lead_id,
            "build_id": None,
            "validation_id": None,
            "deployment_id": None,
            "status": status,
            "stage": stage,
            "review_status": None,
            "message": message,
            "recorded_at": recorded_at,
            "age_seconds": recorded_age_seconds(recorded_at),
            "next_action": action,
            **extra,
        })

    try:
        work = ledger.outstanding_work(today=today, limit=limit)
    except Exception as error:  # a broken read must degrade the report, not the health endpoint
        add("ENGAGEMENT_LEDGER_UNREADABLE",
            f"The engagement ledger could not be read for this report ({type(error).__name__}).",
            "Check storage; until it reads, engagement work is not visible in this report.",
            kind="engagement")
        return items[:limit]

    for row in work.get("due_follow_ups") or []:
        add("FOLLOW_UP_DUE",
            f"Follow-up {row.get('follow_up_id')} for lead {row.get('lead_id')} was due "
            f"{row.get('due_at') or 'at a recorded time'} on the {row.get('channel') or 'recorded'} channel. "
            "Nothing was sent automatically.",
            "Draft the follow-up and send it through the recorded approval path, or reschedule/cancel it with "
            "python3 -m tracking.cli follow-up-schedule / follow-up-cancel.",
            lead_id=row.get("lead_id"), status="DUE", recorded_at=row.get("due_at"),
            follow_up_id=row.get("follow_up_id"))
    for row in work.get("proposals_awaiting_decision") or []:
        add("PROPOSAL_AWAITING_DECISION",
            f"Proposal {row.get('proposal_id')} was presented to {row.get('business_name') or row.get('lead_id')} "
            "and no customer decision is recorded.",
            "Record what actually happened (python3 -m tracking.cli proposal-decision); silence is not a "
            "rejection and an unanswered proposal is not a lost deal.",
            lead_id=row.get("lead_id"), project_id=row.get("project_id"), status="PRESENTED",
            recorded_at=row.get("presented_at"), proposal_id=row.get("proposal_id"))
    for row in work.get("responses_awaiting_reply") or []:
        add("RESPONSE_AWAITING_REVIEW",
            f"{row.get('lead_id')} replied on {row.get('channel') or 'the recorded channel'} at "
            f"{row.get('received_at')} (recorded classification {row.get('classification') or 'unset'}); "
            "nothing has been sent since.",
            "Read the reply and answer deliberately - the ledger records a response, never a decision for you.",
            lead_id=row.get("lead_id"), status="AWAITING_REVIEW", recorded_at=row.get("received_at"),
            engagement_id=row.get("engagement_id"))
    if work.get("suppressed_by_opt_out"):
        add("FOLLOW_UP_SUPPRESSED_BY_OPT_OUT",
            f"{work['suppressed_by_opt_out']} due follow-up(s) are suppressed by a recorded opt-out and will "
            "not be raised as due work.",
            "Nothing to send. Confirm the opt-out is the customer's own recorded decision if it looks wrong.",
            status="SUPPRESSED", recorded_at=None)

    try:
        backlog = InboundIntake(ledger).operator_backlog(limit=limit)
    except Exception as error:
        add("INBOUND_INTAKE_UNREADABLE",
            f"The inbound intake backlog could not be read ({type(error).__name__}).",
            "Check that the intake collection is persisted; unreadable inbound is not the same as none.",
            kind="inbound")
        return items[:limit]
    for row in backlog.get("intake_items") or []:
        disposition = str(row.get("disposition"))
        if disposition == "UNMATCHED":
            # No message text here on purpose: this report is copied into log lines and alert digests, and an
            # inbound message can contain anything the sender typed. The bounded, contact-redacted excerpt is
            # one authenticated read away (GET /engagement/inbound, or `inbound-pending`).
            add("INBOUND_UNMATCHED",
                f"An inbound {row.get('channel') or 'message'} event from provider {row.get('provider')} "
                f"(received {row.get('received_at')}) could not be matched to a tracked lead, so it is not part "
                "of any business history.",
                "Say which lead it belongs to: python3 -m tracking.cli response-record --lead-id <id> "
                "--quote \"...\" (or resend the event with lead_id). It is never attached by guessing.",
                kind="inbound", recorded_at=row.get("received_at"), status="UNMATCHED",
                dedupe_key=row.get("dedupe_key"))
        elif disposition == "FAILED":
            add("INBOUND_PROCESSING_FAILED",
                f"An inbound event from {row.get('provider')} was accepted but could not be recorded "
                f"({row.get('error_code') or 'unknown reason'}).",
                "Fix the cause and resend the same event id - the intake keeps the failed row and re-delivery "
                "is safe.",
                kind="inbound", lead_id=row.get("lead_id"), recorded_at=row.get("received_at"),
                status="FAILED", dedupe_key=row.get("dedupe_key"))
        else:
            add("OPT_OUT_NEEDS_REVIEW",
                f"An opt-out-shaped message from {row.get('lead_id') or 'an unidentified sender'} is hedged or "
                "contradicted, so no block was applied.",
                "Ask the sender or check the provider's own unsubscribe record before blocking; an ambiguous "
                "phrase is not consent to stop, and not blocking keeps them contactable.",
                kind="inbound", lead_id=row.get("lead_id"), recorded_at=row.get("received_at"),
                status="OPT_OUT_UNCERTAIN", dedupe_key=row.get("dedupe_key"))
    for row in backlog.get("triage_items") or []:
        add("INBOUND_TRIAGE_UNCERTAIN",
            f"A reply from {row.get('lead_id') or 'an unidentified sender'} was recorded, but what it means is "
            f"not settled (read as {row.get('category') or 'unclear'}, certainty "
            f"{row.get('certainty') or 'unknown'}"
            + (f", and the caller's hint '{row.get('hint')}' contradicts that" if row.get("hint_disagrees") else "")
            + ").",
            "Re-read the message and classify it explicitly with python3 -m tracking.cli response-record; the "
            "suggestion is a reading aid, not a finding.",
            kind="inbound", lead_id=row.get("lead_id"), project_id=row.get("project_id"),
            recorded_at=row.get("received_at"), status=str(row.get("category") or "UNCLEAR").lower().upper(),
            engagement_id=row.get("engagement_id"), dedupe_key=row.get("dedupe_key"))
    return items[:limit]


def operator_attention(store, *, now: datetime | None = None, repository_root: Path | str = ".",
                       include_deployment: bool = True, include_deployment_evidence: bool = True,
                       limit: int = ATTENTION_LIMIT) -> dict:
    """The single source for "what needs an operator", used by health, API, CLI and alerts.

    It reports only what is recorded: job codes and messages, project stage/review/validation state,
    publish outcomes backed by an attempt ledger, stale heartbeats, and the engagement work a human owes
    (unanswered replies, undecided proposals, due follow-ups) including inbound events this side could not
    route. Nothing here grants authorization; it is a reading of the records.
    """
    job_items, counts = job_attention_items(store, now=now, limit=limit)
    project_items = project_attention_items(store, limit=limit)
    engagement_items = engagement_attention_items(store, limit=limit)
    outbound_items = outbound_attention_items(store, limit=limit)
    deployment_attention: list[dict] = []
    if include_deployment:
        deployment_attention = deployment_report(
            store, repository_root=repository_root, include_evidence=include_deployment_evidence
        ).get("attention", [])
    for item in deployment_attention:
        item.setdefault("kind", "deployment")
    # Order is deliberate: a broken job outranks a project that is merely waiting, which outranks an
    # enquiry a human has to answer. The cap is applied after the merge, so `counts.total` above stays
    # accurate to what each section actually found.
    from .controls import hold_view

    hold = hold_view(store)
    control_items: list[dict[str, Any]] = []
    if hold["held"] or hold.get("unreadable"):
        # A hold is not a fault: it is a decision with a name and a timestamp, listed so nobody schedules work
        # against a wall. It never pages, because CRITICAL_KINDS does not include "control".
        control_items.append({
            "kind": "control",
            "code": "AUTOMATION_CONTROL_UNREADABLE" if hold.get("unreadable") else "AUTOMATION_HELD",
            "title": ("The automation hold record cannot be read" if hold.get("unreadable")
                      else "Automated dispatch is on hold"),
            "message": (f"set_by={hold.get('set_by')} set_at={hold.get('set_at')} reason={hold.get('reason')}"
                        if hold["held"] else "automation_controls.dispatch is present but not a readable record"),
            "next_action": ("release with `python3 -m orchestration.cli release-automation --actor <you>` or "
                            "/resume in Telegram; jobs a held attempt marked BLOCKED stay until an operator "
                            "retries them" if hold["held"] else
                            "inspect the control record; dispatch is not held by it"),
            "recorded_at": hold.get("set_at"),
        })
    items = ([*job_items, *project_items, *deployment_attention, *control_items, *engagement_items,
              *outbound_items][:limit])
    grouped: dict[str, int] = {}
    for item in items:
        grouped[str(item.get("kind"))] = grouped.get(str(item.get("kind")), 0) + 1
    projects = sorted({str(item.get("project_id")) for item in items if item.get("project_id")})
    return {
        "checked_at": _checked_at(now),
        "requires_attention": bool(items),
        "counts": {**counts, "jobs": counts["total"], "projects": len(project_items),
                   "deployments": len(deployment_attention), "engagements": len(engagement_items),
                   "outbound": len(outbound_items),
                   "sends_uncertain": sum(1 for item in outbound_items if item.get("code") == "SEND_UNCERTAIN"),
                   "inbound": sum(1 for item in engagement_items if item.get("kind") == "inbound"),
                   "automation_held": hold["held"], "by_kind": grouped},
        "projects": projects,
        "items": items,
        "meaning": "Attention means a recorded state that a human must clear; the system will not retry it on its own.",
    }


def outbound_attention_items(store, *, limit: int = ATTENTION_LIMIT) -> list[dict]:
    """Approved sends waiting on a person, and attempts the provider left unresolved - same item shape as the rest.

    Four conditions, all read from records rather than inferred from the passage of time:
    ``APPROVAL_AWAITING_SEND`` (a human approved content nobody has sent - this is not urgent and must not become
    urgent by itself), ``APPROVAL_STALE`` or ``APPROVAL_UNBOUND`` (the approval no longer covers the draft's
    words), ``SEND_UNCERTAIN`` (submitted with no definitive answer: the one condition that must not be left
    alone, because resending could mail the same prospect twice), and ``SEND_FAILED`` (a reported failure awaiting
    a deliberate retry). Nothing here enqueues, sends, retries, or releases anything, and no address or body is
    named: the outreach id is the pointer, as it is everywhere else in this report.
    """
    from outreach.service import content_fingerprint

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, message: str, action: str, *, row: dict, status: Any, recorded_at: Any) -> None:
        key = (code, str(row.get("outreach_id") or ""))
        if key in seen:
            return
        seen.add(key)
        project = (store.state.get("projects") or {}).get(str(row.get("project_id") or "")) or {}
        items.append({
            "kind": "outbound",
            "code": code,
            "project_id": row.get("project_id"),
            "lead_id": row.get("lead_id"),
            "build_id": row.get("build_id"),
            "validation_id": None,
            "deployment_id": row.get("deployment_id"),
            "status": status,
            "stage": project.get("current_stage"),
            "review_status": project.get("review_status"),
            "message": message,
            "recorded_at": recorded_at,
            "age_seconds": recorded_age_seconds(recorded_at),
            "next_action": action,
            "outreach_id": row.get("outreach_id"),
        })

    for row in (store.state.get("outreach") or {}).values():
        if not isinstance(row, dict) or str(row.get("channel") or "") != "EMAIL":
            continue
        state = str(row.get("send_state") or "")
        status = str(row.get("status") or "")
        if state in {"SENDING", "UNCERTAIN"}:
            add("SEND_UNCERTAIN",
                f"Outreach {row.get('outreach_id')} was submitted to the provider for lead {row.get('lead_id')} "
                "and gave no definitive answer"
                + (f" (provider id {row.get('send_provider_message_id')})" if row.get("send_provider_message_id")
                   else " (no provider id was returned)") + f": {str(row.get('send_state_detail') or '')[:160]}",
                "run `python3 -m orchestration.cli email-reconcile` or /reconcile in Telegram; do not resend - "
                "Gmail has no idempotency key, so a retry could mail the same prospect twice",
                row=row, status=state or "SENDING", recorded_at=row.get("send_attempted_at") or row.get("reviewed_at"))
        elif state == "FAILED":
            add("SEND_FAILED",
                f"Outreach {row.get('outreach_id')} for lead {row.get('lead_id')} was reported as a failed send "
                f"({str(row.get('failure_detail') or row.get('send_state_detail') or '')[:160]}); the human "
                "approval still stands.",
                "decide deliberately: /sent shows the recorded answer, and a retry reuses this draft rather than "
                "starting a new review",
                row=row, status="SEND_FAILED", recorded_at=row.get("failed_at") or row.get("reviewed_at"))
        elif status == "READY_TO_SEND":
            approved_digest = row.get("approved_content_fingerprint")
            if not approved_digest:
                add("APPROVAL_UNBOUND",
                    f"Outreach {row.get('outreach_id')} was approved before approvals were bound to a content "
                    "digest, so no send path will act on it",
                    "have a human approve the current draft again (/review, then the approve button)",
                    row=row, status=status, recorded_at=row.get("reviewed_at") or row.get("generated_at"))
            elif approved_digest != content_fingerprint(row):
                add("APPROVAL_STALE",
                    f"The approved content of outreach {row.get('outreach_id')} no longer matches the draft: the "
                    "recipient, subject, body, or preview URL moved after the review",
                    "/review the draft and have a human decide on the current content; an approval is never "
                    "re-used for different words",
                    row=row, status=status, recorded_at=row.get("reviewed_at"))
            else:
                add("APPROVAL_AWAITING_SEND",
                    f"Outreach {row.get('outreach_id')} for lead {row.get('lead_id')} was approved "
                    f"{row.get('reviewed_at') or 'at a recorded time'} by "
                    f"{row.get('reviewed_by') or 'an unattributed reviewer'} and no send is recorded for it; "
                    "nothing sends from this notice",
                    "an operator names the draft and confirms it (/outbox then /send, or email-send with "
                    "--allow-live); a workflow cannot take this item as a cue to send",
                    row=row, status=status, recorded_at=row.get("reviewed_at") or row.get("generated_at"))
    items.sort(key=lambda item: (str(item.get("code")), str(item.get("outreach_id"))))
    return items[:max(1, int(limit))]


def workflow_execution_report(store, *, now: datetime | None = None) -> dict:
    """Workflow health from the job records and execution ledger the system already keeps."""
    jobs = [job for job in (store.state.get("jobs") or {}).values() if isinstance(job, dict)]
    by_status: dict[str, int] = {}
    oldest_running: dict[str, Any] | None = None
    oldest_pending: dict[str, Any] | None = None
    recent_failures: list[dict] = []
    for job in jobs:
        status = str(job.get("status") or "UNKNOWN")
        by_status[status] = by_status.get(status, 0) + 1
        entry = {
            "job_id": job.get("job_id"),
            "job_type": job.get("job_type"),
            "project_id": job.get("project_id"),
            "age_seconds": recorded_age_seconds(job.get("started_at") if status == "RUNNING" else job.get("created_at"), now=now),
            "recorded_at": job.get("started_at") if status == "RUNNING" else job.get("created_at"),
        }
        if status == "RUNNING":
            if oldest_running is None or (entry["age_seconds"] or 0) > (oldest_running.get("age_seconds") or 0):
                oldest_running = entry
        elif status in {"PENDING", "QUEUED"}:
            if oldest_pending is None or (entry["age_seconds"] or 0) > (oldest_pending.get("age_seconds") or 0):
                oldest_pending = entry
        if status in ATTENTION_STATUSES and job.get("error_code"):
            recent_failures.append({
                "job_id": job.get("job_id"), "status": status, "code": job.get("error_code"),
                "recorded_at": job.get("completed_at") or job.get("updated_at"),
            })
    executions = [record for record in (store.state.get("executions") or {}).values() if isinstance(record, dict)]
    recent_failures.sort(key=lambda item: str(item.get("recorded_at") or ""))
    attention = sorted(status for status in by_status if status in ATTENTION_STATUSES)
    return {
        "status": "degraded" if attention else "healthy",
        "jobs": len(jobs),
        "by_status": by_status,
        "attention": attention,
        "executions_recorded": len(executions),
        "oldest_running": oldest_running,
        "oldest_pending": oldest_pending,
        "recent_failures": recent_failures[-5:],
        "note": "Counts and ages come from recorded timestamps only; a missing timestamp is reported as null.",
    }


def worker_section(store, now: datetime | None = None) -> dict:
    workers = [worker for worker in (store.state.get("workers") or {}).values() if isinstance(worker, dict)]
    available = [w for w in workers if w.get("enabled") and w.get("status") == "IDLE" and w.get("health_status") == "AVAILABLE"]
    degraded = [w for w in workers if str(w.get("health_status") or "") not in {"", "AVAILABLE"}]
    section = {
        "status": "healthy" if available else "degraded",
        "available_workers": len(available),
        "registered_workers": len(workers),
        "degraded_workers": len(degraded),
        "heartbeat_threshold_seconds": STALE_RUNNING_SECONDS,
    }
    if not workers:
        section["reason"] = "No worker has registered; queued jobs will not be executed until one starts."
    return section


def n8n_section(repository_root: Path) -> dict:
    """Report the automation platform's wiring from local files and configuration only."""
    directory = Path(repository_root) / "n8n" / "workflows"
    workflows = sorted(path.name for path in directory.glob("*.json")) if directory.is_dir() else []
    active = False
    for name in workflows:
        try:
            if '"active": true' in (directory / name).read_text(encoding="utf-8"):
                active = True
                break
        except OSError:
            continue
    return {
        "status": "available" if active else "unavailable",
        "reason": None if active else "Not activated or connected by this repository.",
        "workflows_shipped": len(workflows),
        "activation": "active" if active else "inactive",
        "callback_api_configured": bool(str(os.environ.get("AI_WEBSITE_AGENT_API_URL") or "").strip()),
    }


def system_health(store, registry=None, *, repository_root=Path("."), verify_build_evidence: bool = True,
                  storage: dict | None = None):
    """Compose the full operational report; no network and no database connection.

    ``verify_build_evidence=False`` skips reading per-build publish ledgers. The readiness probe uses
    that cheaper form so a load balancer polling readiness does not turn into a filesystem scan;
    ``GET /health`` and the CLI commands report the full evidence.

    ``storage`` accepts a :func:`verify_storage` result so a caller that has actively verified the
    database reports the truth. Without it the storage section is explicitly configuration-only:
    ``source_of_truth.verification`` says so, because an unprobed backend is never claimed healthy.
    """
    now = datetime.now(timezone.utc)
    root = Path(repository_root)
    verified = storage is not None
    storage = storage if verified else storage_report(store)
    local_state = store.check_local_state(repository_root=root) if hasattr(store, "check_local_state") else {"status": "HEALTHY"}
    jobs = list((store.state.get("jobs") or {}).values())
    job_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "UNKNOWN")
        job_counts[status] = job_counts.get(status, 0) + 1
    attention = sorted(status for status in job_counts if status in {"FAILED", "NEEDS_REVIEW", "BLOCKED"})
    probed_or_local = verified or getattr(store, "db_backend", None) is None
    state_available = isinstance(store.state, dict) and storage["status"] == "healthy" and probed_or_local
    configuration = configuration_section(store, root)
    workers = worker_section(store, now)
    deployment = provider_section(store, root, include_evidence=verify_build_evidence)
    workflow = workflow_execution_report(store, now=now)

    blocking: list[dict] = []
    degradations: list[dict] = []
    if not probed_or_local and storage["status"] == "healthy":
        degradations.append({
            "code": "STORAGE_NOT_PROBED",
            "message": "A remote storage backend is configured but this report did not verify it.",
            "next_action": "Use GET /readiness or: python3 -m orchestration.cli health (both verify the backend actively).",
        })
    if storage["status"] != "healthy":
        blocking.append({
            "code": "STORAGE_UNAVAILABLE",
            "message": storage["error"] or "The configured storage backend is not usable.",
            "next_action": "Verify the database connection configuration and re-check: python3 -m orchestration.cli health",
        })
    if not isinstance(store.state, dict):
        blocking.append({"code": "STATE_UNLOADED", "message": "No state object is available to this process.",
                        "next_action": "Start the API against a verified workspace (python3 -m orchestration.cli check-workspace)."})
    if local_state.get("status") != "HEALTHY":
        if not local_state.get("state_file_exists"):
            blocking.append({"code": "STATE_NOT_PERSISTED", "message": "Neither a database backend nor a state file is available.",
                            "next_action": "Configure a database backend or run recovery before accepting work."})
        else:
            degradations.append({
                "code": "LOCAL_BUILD_ARTIFACTS_MISSING",
                "message": f"{len(local_state.get('missing_build_artifacts') or [])} recorded build artifact(s) are absent from this workspace.",
                "next_action": "Recover them from durable storage: python3 -m orchestration.cli recover-workspace",
            })
    if workers["status"] != "healthy":
        degradations.append({"code": workers.get("reason") and "NO_AVAILABLE_WORKER" or "WORKER_CAPACITY",
                            "message": workers.get("reason") or "No registered worker is currently available.",
                            "next_action": "Start a worker (python3 -m orchestration.cli serve) or check worker health."})
    if attention:
        degradations.append({"code": "JOBS_AWAITING_OPERATOR", "message": f"Job statuses needing review: {', '.join(attention)}.",
                            "next_action": "python3 -m orchestration.cli attention"})
    if deployment.get("status") != "healthy":
        degradations.append({"code": "DEPLOYMENT_STATE", "message": deployment.get("reason")
                            or f"{deployment.get('attention_count', 0)} project(s) need a deployment outcome review.",
                            "next_action": "python3 -m orchestration.cli deployment-status"})

    readiness_status = "NOT_READY" if blocking else ("DEGRADED" if degradations else "READY")
    return {
        "checked_at": _checked_at(now),
        "liveness": {"status": "ALIVE", "note": "Produced in-process: proves execution, not readiness."},
        "application": {"status": "healthy" if state_available else "degraded"},
        "readiness": {
            "status": readiness_status,
            "blocking": blocking,
            "degradations": degradations,
            "note": "Liveness, readiness and storage health are separate: an alive process is not necessarily ready to serve work.",
        },
        "source_of_truth": {
            # "unverified" is reserved for a configured remote backend nobody probed: an alive process
            # must never imply the source of truth was reached.
            "status": ("healthy" if state_available else
                       ("unverified" if probed_or_local is False and storage["status"] == "healthy" else "unavailable")),
            "storage": storage,
            "local_state": local_state,
            "verification": "verified" if verified else "configuration_only",
            "verification_note": (
                "An operator probe verified reachability and schema." if verified else
                "Configuration only: no connection was attempted. Use GET /readiness (or verify_storage) for an active check."
            ),
        },
        "job_system": {"status": "degraded" if attention else "healthy", "jobs": len(jobs),
                       "by_status": job_counts, "attention": attention},
        "workflow_execution": workflow,
        "worker_system": workers,
        "operator_attention": operator_attention(store, now=now, repository_root=root),
        "n8n": n8n_section(root),
        "deployment": deployment,
        "configuration": configuration,
    }


def readiness_for(store, job_service, job_id, registry=None):
    job = job_service.get(job_id)
    gate = job_service.gate_failure(job) if hasattr(job_service, "gate_failure") else None
    failure = gate[1] if gate else None
    report = {
        "job_id": job_id,
        "status": "READY" if not failure else "NOT_READY",
        "reason": failure,
        "dry_run": job.get("dry_run", True),
    }
    if gate:
        report["error_code"] = gate[0]
        report["next_action"] = job.get("next_action") or next_action_for(gate[0], project_id=job.get("project_id"),
                                                                          build_id=job.get("build_id"), job_type=job.get("job_type"))
    if not failure and registry:
        candidates = registry.candidates(job)
        if not candidates:
            report.update({"status": "NOT_READY", "reason": "No available capable worker.",
                           "error_code": "WORKER_UNAVAILABLE", "next_action": NEXT_ACTIONS["WORKER_UNAVAILABLE"]})
    return report
