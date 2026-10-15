"""Human-approval capability and publish-attempt ledger for preview deployments.

Why this module exists
----------------------
`TrackingStore` owns the audited review decision, but the operation that actually publishes a
website is `VercelDeploymentProvider.deploy()`, which is deliberately file-based and can be
reached from the operator CLI, a worker, or an n8n-triggered job. A gate that only lives in the
UI, an n8n condition, or one caller is not a security boundary. So the approved decision is
materialized as a build-local capability file (`deployment-approval.json`) that the publish
operation verifies itself, and every real publish attempt is recorded in an append-only attempt
ledger (`deployment-attempts.json`) so an interrupted publish is never assumed to have failed.

The capability file is derived from, and never replaces, the durable audit record in
`TrackingStore` (`project["review_history"]` and `state["reviews"]`). It carries no secrets.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

APPROVAL_FILENAME = "deployment-approval.json"
ATTEMPTS_FILENAME = "deployment-attempts.json"
DEPLOYMENT_ACTION = "PREVIEW_DEPLOYMENT"
APPROVED_DECISION = "APPROVED_FOR_PREVIEW"

# A publish attempt in one of these states may have reached the provider, so it must never be
# repeated automatically.
UNRESOLVED_ATTEMPT_STATES = {"PREPARING", "DEPLOYING", "PUBLISH_UNCERTAIN"}
TERMINAL_ATTEMPT_STATES = {"DEPLOYED", "FAILED", "NEEDS_REVIEW", "PENDING", "BLOCKED"}

#: An operator's explicit decision about an unresolved publish is recorded in the same ledger as the
#: attempt itself. It is an *annotation*: it states that a human chose how to proceed and who they were,
#: never that the provider accepted or rejected the publish. Read paths that ask "what did the provider
#: last report" must skip it (`latest_publish_attempt`), so resolving can never erase the uncertainty.
UNCERTAIN_RESOLUTION_STATE = "UNCERTAIN_PUBLISH_RESOLVED"

_SAFE_REASON = re.compile(r"[^\x20-\x7e]")


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(
        {k: v for k, v in sorted(record.items()) if k != "approval_sha256"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def approval_digest(record: dict[str, Any]) -> str:
    """Deterministic SHA-256 over every approval field except the digest itself."""
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def approval_path(build_dir: Path) -> Path:
    return Path(build_dir) / APPROVAL_FILENAME


def attempts_path(build_dir: Path) -> Path:
    return Path(build_dir) / ATTEMPTS_FILENAME


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_unreadable": True}
    return parsed if isinstance(parsed, dict) else {"_unreadable": True}


def artifact_sha256(build_dir: Path) -> str | None:
    index_path = Path(build_dir) / "index.html"
    if not index_path.is_file():
        return None
    return hashlib.sha256(index_path.read_bytes()).hexdigest()


def write_approval(build_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Persist a build-local approval capability bound to one exact build and artifact."""
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        key: value
        for key, value in record.items()
        if key
        in {
            "project_id",
            "lead_id",
            "build_id",
            "validation_id",
            "validation_status",
            "review_id",
            "decision",
            "action",
            "artifact_sha256",
            "reviewer",
            "reviewer_identity_source",
            "identity_verified",
            "notes",
            "decided_at",
            "data_classification",
        }
        and value is not None
    }
    payload.setdefault("action", DEPLOYMENT_ACTION)
    payload.setdefault("decision", APPROVED_DECISION)
    payload.setdefault("artifact_sha256", artifact_sha256(build_dir))
    payload.setdefault("decided_at", _now_iso())
    payload["status"] = "ACTIVE"
    payload["approval_sha256"] = approval_digest(payload)
    (build_dir / APPROVAL_FILENAME).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def revoke_approval(build_dir: Path, *, reason: str, revoked_by: str = "system") -> dict[str, Any] | None:
    """Mark an existing capability revoked; the file is kept so the revocation stays inspectable."""
    build_dir = Path(build_dir)
    existing = _read_json(build_dir / APPROVAL_FILENAME)
    if not existing or existing.get("_unreadable"):
        return None
    if existing.get("status") == "REVOKED":
        return existing
    updated = {k: v for k, v in existing.items() if k != "approval_sha256"}
    updated.update(
        {
            "status": "REVOKED",
            "revoked_reason": _SAFE_REASON.sub(" ", str(reason))[:400],
            "revoked_by": str(revoked_by)[:120],
            "revoked_at": _now_iso(),
        }
    )
    updated["revocation_sha256"] = approval_digest(updated)
    (build_dir / APPROVAL_FILENAME).write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def read_approval(build_dir: Path) -> dict[str, Any] | None:
    record = _read_json(Path(build_dir) / APPROVAL_FILENAME)
    if record is None or record.get("_unreadable"):
        return record
    return record


def verify_approval(build_dir: Path, *, manifest: dict[str, Any] | None = None) -> tuple[bool, str, str, dict[str, Any] | None]:
    """Return ``(authorized, code, reason, record)`` for a publish attempt of *build_dir*.

    Every denial is specific and actionable so an operator can fix the actual missing step rather
    than re-running the deployment and hoping for a different result.
    """
    build_dir = Path(build_dir)
    record = _read_json(build_dir / APPROVAL_FILENAME)
    if record is None:
        return (
            False,
            "APPROVAL_MISSING",
            "No explicit human approval is recorded for this build. Record a review decision "
            "(`APPROVED_FOR_PREVIEW` for action `PREVIEW_DEPLOYMENT`) with "
            "`python3 -m tracking.cli review <project_id> --decision APPROVED_FOR_PREVIEW "
            "--action PREVIEW_DEPLOYMENT --notes \"...\" --confirm <project_id>:<build_id>:APPROVED_FOR_PREVIEW` "
            "before deploying.",
            None,
        )
    if record.get("_unreadable"):
        return (
            False,
            "APPROVAL_UNREADABLE",
            f"{build_dir / APPROVAL_FILENAME} is not valid JSON; the approval capability may be damaged or partially written.",
            None,
        )
    if record.get("status") == "REVOKED":
        # A revocation legitimately edits the file, so it is honoured before the approval-digest
        # check: denial needs no integrity proof, while a stripped revocation would fail that check.
        return (
            False,
            "APPROVAL_REVOKED",
            "The recorded approval was revoked"
            + (f": {record.get('revoked_reason')}" if record.get("revoked_reason") else ".")
            + " A new build and a new explicit human approval are required.",
            record,
        )
    recorded_digest = record.get("approval_sha256")
    if not recorded_digest or approval_digest(record) != recorded_digest:
        return (
            False,
            "APPROVAL_TAMPERED",
            f"{APPROVAL_FILENAME} does not match its own SHA-256 digest; the file was modified outside TrackingStore.",
            record,
        )
    if record.get("decision") != APPROVED_DECISION:
        return (
            False,
            "APPROVAL_INVALID_DECISION",
            f"Deployment requires decision '{APPROVED_DECISION}' but the recorded decision is '{record.get('decision')}'.",
            record,
        )
    if str(record.get("action") or "").upper() != DEPLOYMENT_ACTION:
        return (
            False,
            "APPROVAL_WRONG_ACTION",
            f"The approval was recorded for action '{record.get('action')}', not '{DEPLOYMENT_ACTION}'.",
            record,
        )
    if manifest is not None:
        manifest_build_id = manifest.get("build_id")
        if not manifest_build_id:
            return (
                False,
                "BUILD_ID_UNRECORDED",
                "This build manifest has no build_id, so no approval can be bound to it. Rebuild with tracking first.",
                record,
            )
        if manifest_build_id and record.get("build_id") != manifest_build_id:
            return (
                False,
                "APPROVAL_BUILD_MISMATCH",
                f"The approval was recorded for build '{record.get('build_id')}', but this build directory is "
                f"'{manifest_build_id}'; a decision about an older build never authorizes a newer one.",
                record,
            )
        if manifest.get("lead_id") and record.get("lead_id") and manifest.get("lead_id") != record.get("lead_id"):
            return (
                False,
                "APPROVAL_LEAD_MISMATCH",
                "The approval belongs to a different lead than this build.",
                record,
            )
        validation = manifest.get("validation") or {}
        if record.get("validation_id") and validation.get("validation_id") and record["validation_id"] != validation.get("validation_id"):
            return (
                False,
                "APPROVAL_VALIDATION_MISMATCH",
                f"The approval references validation '{record.get('validation_id')}' but this build manifest "
                f"references '{validation.get('validation_id')}'.",
                record,
            )
        if validation.get("status") not in (None, "PASS"):
            return (
                False,
                "VALIDATION_NOT_PASSED",
                f"This build's validation status is '{validation.get('status')}', not PASS.",
                record,
            )
    current_sha = artifact_sha256(build_dir)
    if current_sha and record.get("artifact_sha256") and record["artifact_sha256"] != current_sha:
        return (
            False,
            "APPROVAL_ARTIFACT_STALE",
            "The artifact in this build directory changed after the approval was recorded "
            f"(approved {record.get('artifact_sha256')}, now {current_sha}); re-validate and re-approve.",
            record,
        )
    return (True, "APPROVED", "", record)


def append_attempt(build_dir: Path, attempt: dict[str, Any]) -> list[dict[str, Any]]:
    """Append one publish attempt; history is never rewritten.

    A ledger that exists but cannot be parsed is refused rather than replaced: silently dropping the
    unreadable entries would destroy the only record of an in-flight or interrupted publish.
    """
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    path = build_dir / ATTEMPTS_FILENAME
    history, problem = load_attempts(build_dir)
    if problem:
        raise ValueError(
            f"Refusing to append to the publish ledger because {problem} Preserve the file and reconcile "
            "the attempts by hand; overwriting it would erase the evidence of an unresolved publish."
        )
    history.append(dict(attempt))
    path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    return history


def load_attempts(build_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(entries, problem)`` for one build's publish ledger.

    ``problem`` is ``None`` when the ledger is absent or parseable. A file that exists but cannot be read
    is *not* the same as "no attempt was recorded": treating it that way would let a damaged audit trail
    masquerade as a clean slate and license a blind republish.
    """
    path = Path(build_dir) / ATTEMPTS_FILENAME
    if not path.is_file():
        return [], None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return [], f"{path} exists but could not be read ({error.__class__.__name__})."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        return [], f"{path} is not valid JSON ({error.msg} at line {error.lineno})."
    if not isinstance(parsed, list):
        return [], f"{path} must contain a JSON list of attempt records, got {type(parsed).__name__}."
    return [item for item in parsed if isinstance(item, dict)], None


def read_attempts(build_dir: Path) -> list[dict[str, Any]]:
    attempts, _problem = load_attempts(build_dir)
    return attempts


def latest_attempt(build_dir: Path) -> dict[str, Any] | None:
    history = read_attempts(build_dir)
    return history[-1] if history else None


def is_resolution_annotation(attempt: dict[str, Any]) -> bool:
    return str(attempt.get("deployment_status") or "") == UNCERTAIN_RESOLUTION_STATE


def is_bookkeeping_entry(attempt: dict[str, Any]) -> bool:
    """True for ledger lines that record a local decision rather than a provider outcome.

    Two kinds exist: an operator resolution annotation, and a gate refusal (``denial_code`` is set when
    the publish was blocked before anything was sent). Neither says anything about what the provider did,
    so neither may be read as the end of an in-flight publish. Treating a refusal as a terminal result was
    a real bypass: the refusal written for an uncertain attempt became "the latest attempt", the next run
    saw a resolved history, and the publish was retried with no ``--resolve-uncertain-publish`` at all.
    """
    return is_resolution_annotation(attempt) or bool(attempt.get("denial_code"))


def is_publish_report(attempt: dict[str, Any]) -> bool:
    return not is_bookkeeping_entry(attempt)


def latest_publish_attempt(build_dir: Path) -> dict[str, Any] | None:
    """Last ledger entry that reports what the provider was asked or answered.

    Operator resolutions and gate refusals are skipped: an uncertain publish stays uncertain in every
    "what is the current state" answer until the provider state is actually confirmed.
    """
    for attempt in reversed(read_attempts(build_dir)):
        if is_publish_report(attempt):
            return attempt
    return None


def operator_resolutions(build_dir: Path, *, limit: int = 5) -> list[dict[str, Any]]:
    """Most recent operator decisions recorded against this build's publish attempts."""
    resolutions = [attempt for attempt in read_attempts(build_dir) if is_resolution_annotation(attempt)]
    return list(reversed(resolutions[-limit:]))


def append_resolution_attempt(
    build_dir: Path,
    *,
    resolves: dict[str, Any] | None,
    resolved_by: str | None,
    note: str | None,
) -> dict[str, Any]:
    """Record an operator's explicit decision to proceed past an unresolved publish attempt.

    The entry says who decided, what they were deciding about, and that nothing has been confirmed with
    the provider. It never restates the publish outcome.
    """
    resolves = resolves or {}
    safe_note = _SAFE_REASON.sub(" ", " ".join(str(note or "").split())).strip()[:400]
    record: dict[str, Any] = {
        "deployment_status": UNCERTAIN_RESOLUTION_STATE,
        "resolves_attempt": resolves.get("attempt"),
        "resolves_status": resolves.get("deployment_status"),
        "deployment_id": resolves.get("deployment_id"),
        "build_id": resolves.get("build_id"),
        "lead_id": resolves.get("lead_id"),
        "artifact_sha256": resolves.get("artifact_sha256"),
        "vercel_project_id": resolves.get("vercel_project_id"),
        "resolved_by": _SAFE_REASON.sub(" ", " ".join(str(resolved_by or "unknown").split())).strip()[:120] or "unknown",
        "resolution_note": safe_note or "No operator note was supplied.",
        # Explicit, and deliberately not derived from the operator's intent: recording a resolution is
        # not evidence about the provider.
        "published": False,
        "provider_state_verified": False,
        "created_at": _now_iso(),
    }
    append_attempt(build_dir, record)
    return record


def classify_prior_attempt(
    build_dir: Path,
    *,
    artifact_sha: str | None,
    project_id: str | None,
    resolve_uncertain_publish: bool = False,
) -> tuple[bool, str, str, dict[str, Any] | None]:
    """Decide whether a previous attempt makes a new publish redundant or unsafe.

    Returns ``(proceed, code, reason, attempt)``. A completed deployment of the same artifact to
    the same project is a duplicate request. An attempt whose terminal state was never recorded may
    have reached the provider, so it is refused until an operator resolves it explicitly.
    """
    attempts, problem = load_attempts(build_dir)
    if problem:
        return (
            False,
            "PUBLISH_LEDGER_UNREADABLE",
            f"{problem} The publish history cannot be trusted, so no new publish is authorized: repair or "
            f"reconcile {ATTEMPTS_FILENAME} for this build first.",
            None,
        )
    attempt = next((item for item in reversed(attempts) if is_publish_report(item)), None)
    if not attempt:
        return (True, "NO_PRIOR_ATTEMPT", "", None)
    state = str(attempt.get("deployment_status") or "")
    resolutions = [item for item in attempts if is_resolution_annotation(item)]
    same_artifact = bool(artifact_sha) and attempt.get("artifact_sha256") == artifact_sha
    same_target = bool(project_id) and attempt.get("vercel_project_id") == project_id
    if state == "DEPLOYED" and same_artifact and same_target:
        return (
            False,
            "DEPLOYMENT_ALREADY_RECORDED",
            f"Deployment {attempt.get('deployment_id')} already published this exact artifact to this project "
            f"(recorded {attempt.get('created_at')}). A repeat publish requires a new build; inspect "
            f"{ATTEMPTS_FILENAME} and the project review history instead.",
            attempt,
        )
    if state in UNRESOLVED_ATTEMPT_STATES:
        where = (f"attempt {attempt.get('attempt')} of deployment {attempt.get('deployment_id') or '<unknown>'} "
                 f"for project {attempt.get('vercel_project_id') or '<unknown>'}")
        prior = (f" {len(resolutions)} operator resolution(s) are already recorded against this build; "
                 "re-reading them is cheaper than guessing." if resolutions else "")
        if resolve_uncertain_publish:
            # Proceeding is allowed, and the decision is what makes it auditable: the caller records an
            # annotation naming the operator and their stated reason before anything is sent.
            return (
                True,
                "PRIOR_ATTEMPT_RESOLUTION_RECORDED",
                f"Proceeding past the unresolved attempt ({where}). The resolution must be recorded in "
                f"{ATTEMPTS_FILENAME} with the operator id and note before the provider is contacted{prior}",
                attempt,
            )
        return (
            False,
            "PUBLISH_STATE_UNCERTAIN",
            f"The previous publish attempt ({where}, recorded {attempt.get('created_at')}) ended in state "
            f"'{state}' without a recorded result, so the provider may already have accepted it{prior}. "
            f"Read {ATTEMPTS_FILENAME} and check the preview project in the provider console, then either "
            "record the true outcome in the project history or re-run with --resolve-uncertain-publish "
            "--resolve-note '<what you checked and why retrying is safe>'; that records your decision and "
            "still publishes nothing by itself.",
            attempt,
        )
    return (True, "PRIOR_ATTEMPT_RESOLVED", "", attempt)


def publish_outcome(build_dir: Path) -> dict[str, Any]:
    """Classify what the recorded publish evidence for one build directory actually says.

    An operator asking "is my preview live?" needs the difference between *verified*,
    *uncertain*, *failed* and *never attempted* - those are not interchangeable, and only the first
    one means the provider accepted the artifact. The classification reads the append-only attempt
    ledger (and the deployment manifest the provider writes on success) and never contacts a
    provider, so it can be called from a health endpoint without side effects.
    """
    build_dir = Path(build_dir)
    attempts, ledger_problem = load_attempts(build_dir)
    manifest = _read_json(build_dir / "deployment.json")
    resolutions = [attempt for attempt in attempts if is_resolution_annotation(attempt)]
    # "What did the provider last report" ignores operator resolutions and gate refusals on purpose: they
    # record local decisions, so they can neither resolve a publish nor close an unresolved one.
    latest = attempts[-1] if attempts else None
    publish_latest = next((item for item in reversed(attempts) if is_publish_report(item)), None)
    publish_state = str((publish_latest or {}).get("deployment_status") or "")
    state = str((latest or {}).get("deployment_status") or "")
    published = [attempt for attempt in attempts if attempt.get("deployment_status") == "DEPLOYED"]
    shadowed_by_refusal = bool(publish_latest) and publish_latest is not latest
    outcome: tuple[str, str, str]
    if ledger_problem:
        outcome = (
            "EVIDENCE_UNREADABLE",
            "PUBLISH_LEDGER_UNREADABLE",
            f"{ledger_problem} This is reported instead of 'no publish attempted': the history has to be "
            "repaired before any publish decision can be made.",
        )
    elif published:
        last = published[-1]
        matches_manifest = bool(manifest) and str((manifest or {}).get("deployment_status")) == "DEPLOYED"
        outcome = (
            "VERIFIED_RECORDED" if matches_manifest or last.get("deployment_url") else "RECORDED_UNCONFIRMED",
            "PROVIDER_URL_RECORDED" if last.get("deployment_url") else "OUTCOME_RECORDED_WITHOUT_URL",
            "A successful publish was recorded for this build. Confirming the live URL is a separate, "
            "manual step: this report does not contact the provider.",
        )
    elif publish_state in UNRESOLVED_ATTEMPT_STATES:
        noted = (f" {len(resolutions)} resolution(s) are recorded for this build (most recent: "
                 f"{(resolutions[-1] or {}).get('resolved_by')}). ") if resolutions else " "
        if shadowed_by_refusal:
            noted += (" A later refusal was logged; it did not clear this attempt. ")
        outcome = (
            "UNCERTAIN",
            "PUBLISH_STATE_UNCERTAIN",
            f"The last provider-facing attempt is recorded as '{publish_state}' with no terminal result, so the provider may have "
            f"accepted the publish.{noted}Verify the preview project with the provider, then either record the "
            "confirmed outcome or re-run the deployment with --resolve-uncertain-publish plus --resolve-note, "
            "which records that operator decision before publishing. Never re-publish blindly.",
        )
    elif state:
        outcome = (
            "FAILED",
            str((latest or {}).get("denial_code") or f"DEPLOYMENT_{state}"),
            "The last recorded attempt did not publish. Read the attempt error and correct the cause before "
            "retrying; a repeat publish requires a new build and deployment id.",
        )
    elif manifest:
        outcome = (
            "RECORDED_UNCONFIRMED",
            "MANIFEST_WITHOUT_ATTEMPT",
            "A deployment manifest exists but no attempt was recorded in the ledger, so the publish history is "
            "incomplete. Reconcile the project record with the provider before relying on it.",
        )
    else:
        outcome = (
            "NOT_ATTEMPTED",
            "NO_PUBLISH_RECORD",
            "No publish has been attempted for this build. A dry-run check verifies the payload only; deploying "
            "requires the recorded human approval and an explicit --execute run.",
        )
    state_evidence = {
        "attempt_count": len(attempts),
        "ledger_readable": ledger_problem is None,
        "latest_publish_attempt": publish_latest,
        "ledger_problem": ledger_problem,
        "resolution_count": len(resolutions),
        "latest_resolution": {
            key: (resolutions[-1] or {}).get(key)
            for key in ("resolves_attempt", "resolved_by", "resolution_note", "created_at", "published",
                        "provider_state_verified")
            if (resolutions[-1] or {}).get(key) is not None
        } if resolutions else None,
        "latest_attempt": {
            key: (latest or {}).get(key)
            for key in ("attempt", "deployment_status", "deployment_id", "build_id", "deployment_url",
                        "denial_code", "error", "created_at", "completed_at")
            if (latest or {}).get(key) is not None
        } if latest else None,
        "ledger": str(build_dir / ATTEMPTS_FILENAME),
        "manifest_present": bool(manifest),
        "provider_contacted": False,
    }
    return {"state": outcome[0], "code": outcome[1], "next_action": outcome[2], **state_evidence}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
