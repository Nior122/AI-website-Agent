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
    """Append one publish attempt; history is never rewritten."""
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    path = build_dir / ATTEMPTS_FILENAME
    history: list[dict[str, Any]] = []
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                history = [item for item in parsed if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            history = []
    history.append(dict(attempt))
    path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    return history


def read_attempts(build_dir: Path) -> list[dict[str, Any]]:
    path = Path(build_dir) / ATTEMPTS_FILENAME
    if not path.is_file():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def latest_attempt(build_dir: Path) -> dict[str, Any] | None:
    history = read_attempts(build_dir)
    return history[-1] if history else None


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
    attempt = latest_attempt(build_dir)
    if not attempt:
        return (True, "NO_PRIOR_ATTEMPT", "", None)
    state = str(attempt.get("deployment_status") or "")
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
    if state in UNRESOLVED_ATTEMPT_STATES and not resolve_uncertain_publish:
        return (
            False,
            "PUBLISH_STATE_UNCERTAIN",
            f"The previous publish attempt ended in state '{state}' without a recorded result, so the provider may "
            "already have accepted it. Inspect the preview project, then re-run with "
            "--resolve-uncertain-publish to record a deliberate retry.",
            attempt,
        )
    return (True, "PRIOR_ATTEMPT_RESOLVED", "", attempt)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
