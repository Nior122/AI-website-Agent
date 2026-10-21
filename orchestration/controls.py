"""The operator's brake: a durable hold on automated processing, read by the existing job gate.

Nothing in this repository could previously say "stop doing things for a while". ``PAUSED`` existed only as a
per-lead relationship stage and outcome status - one business's pipeline, not the machine's. An operator who
needed discovery, builds, or outreach preparation suspended had to stop the scheduler by hand and hope nothing
else was running.

This module adds that brake and nothing else:

* :func:`set_hold` / :func:`release_hold` write one small record (``automation_controls["dispatch"]``) through
  the store's ordinary durable save, so a hold survives a restart and is visible to every process reading the
  same state - the CLI, the API, a worker, and the Telegram bot all see the same one record.
* :func:`gate_reason` is consulted by :meth:`orchestration.jobs.JobService.gate_failure`, which every execution
  path already passes through. A held job is therefore refused with a named code before a worker is chosen, a
  provider is contacted, or an attempt is opened - the same shape as every other gate failure, and reversible by
  releasing the hold.

What a hold does **not** do, deliberately:

* It is not an authorization. Removing a hold never grants permission anything: a ``DEPLOY`` still needs the
  durable human approval, and the eligibility guard still refuses a blocked contact. Nor does a hold revoke an
  approval anyone already gave - it suspends dispatch, not standing.
* It does not stop a human acting in person. ``tracking.cli`` and :func:`orchestration.api.serve`'s review
  routes remain usable while held, because those are operator decisions, not automated processing.
* It does not park a queue. A dispatch attempt made while held becomes ``BLOCKED`` with ``AUTOMATION_HELD`` so it
  is visible in ``/status`` and the attention report; releasing the hold reports how many such jobs exist and
  leaves the explicit ``retry-job`` decision to the operator, because re-running work is a choice.

The record is read-only reporting for anything else: :func:`hold_view` is what the health, CLI, API, and bot
surfaces all render from, so no surface can disagree about whether the system is held.
"""
from __future__ import annotations

from typing import Any

from .observability import log_event, now_iso

#: State collection holding the operator's control records. One key per control, so a second brake later does
#: not need a new persistence path.
CONTROLS_COLLECTION = "automation_controls"

#: The one control that exists today: dispatch of automated work.
DISPATCH_KEY = "dispatch"

#: Code every gated job carries while the hold is on, so a blocked queue is searchable rather than mysterious.
HOLD_CODE = "AUTOMATION_HELD"

REASON_LIMIT = 300
ACTOR_LIMIT = 120


class ControlError(ValueError):
    """A control change that cannot be trusted. Carries a stable code for the caller's answer."""

    def __init__(self, message: str, *, code: str = "CONTROL_INVALID") -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, *, field: str, limit: int, required: bool = False) -> str | None:
    cleaned = str(value).strip() if value is not None else ""
    if not cleaned:
        if required:
            raise ControlError(f"{field} is required to record who changed automation control.",
                               code="CONTROL_ACTOR_REQUIRED")
        return None
    if len(cleaned) > limit:
        raise ControlError(f"{field} must be at most {limit} characters (got {len(cleaned)}).",
                           code="CONTROL_FIELD_TOO_LONG")
    return cleaned


def _state(store) -> dict[str, Any]:
    """The control record for dispatch, or an empty dict when nothing was ever written.

    A missing or malformed record is reported by :func:`hold_view` as unreadable rather than treated as a hold:
    this brake is operational, and the security-relevant gates (durable approvals, eligibility) are enforced
    independently of it. Silently halting every job because a control row would not parse would be a worse
    failure than a visible one.
    """
    collection = store.state.get(CONTROLS_COLLECTION)
    record = collection.get(DISPATCH_KEY) if isinstance(collection, dict) else None
    return record if isinstance(record, dict) else {}


def held(store) -> bool:
    """True when the operator has put automated dispatch on hold."""
    return _state(store).get("held") is True


def gate_reason(store) -> tuple[str, str] | None:
    """The gate answer for one dispatch attempt: ``(code, message)``, or ``None`` when free to run."""
    record = _state(store)
    if record.get("held") is not True:
        return None
    reason = str(record.get("reason") or "no reason recorded")
    set_at = str(record.get("set_at") or "an unknown time")
    return (HOLD_CODE,
            f"Automated processing is on hold (set {set_at} by {record.get('set_by') or 'an operator'}: {reason}). "
            f"Release the hold before this job can run; the hold suspends dispatch, it does not revoke any approval.")


def set_hold(store, *, actor: Any, reason: Any = None, note: Any = None) -> dict[str, Any]:
    """Put automated dispatch on hold. Idempotent, and the repeat is labelled rather than faked."""
    who = _text(actor, field="actor", limit=ACTOR_LIMIT, required=True)
    why = _text(reason, field="reason", limit=REASON_LIMIT) or "no reason recorded"
    comment = _text(note, field="note", limit=REASON_LIMIT)
    existing = _state(store)
    already = existing.get("held") is True
    record = dict(existing)
    record.update({"held": True, "reason": why, "set_by": who, "set_at": now_iso(),
                   "note": comment, "updated_at": now_iso(),
                   "hold_count": int(existing.get("hold_count") or 0) + (0 if already else 1)})
    if already and existing.get("reason") == why and existing.get("set_by") == who:
        # Same request twice is a replay of the same decision: the original `set_at` stays the record of when
        # the hold actually began, so an operator cannot rewind the clock on how long work has been stalled.
        record["repeat_detected_at"] = now_iso()
    else:
        record.pop("repeat_detected_at", None)
    store.state.setdefault(CONTROLS_COLLECTION, {})[DISPATCH_KEY] = record
    store._save()
    log_event(severity="WARNING", operation="control.hold", code=HOLD_CODE,
              message="automated dispatch placed on hold", actor=who, note=comment)
    return hold_view(store, changed=not (already and "repeat_detected_at" in record))


def release_hold(store, *, actor: Any, note: Any = None) -> dict[str, Any]:
    """Release the hold. Releases are counted so a flapping brake is visible in the record."""
    who = _text(actor, field="actor", limit=ACTOR_LIMIT, required=True)
    comment = _text(note, field="note", limit=REASON_LIMIT)
    existing = _state(store)
    already = existing.get("held") is not True
    releases = int(existing.get("release_count") or 0) + 1
    record = {key: value for key, value in existing.items()
              if key in ("set_at", "set_by", "reason", "hold_count", "release_count", "history")}
    record.update({"held": False, "released_by": who, "released_at": now_iso(), "updated_at": now_iso(),
                   "release_count": releases, "hold_count": int(existing.get("hold_count") or 0),
                   "release_note": comment, "last_reason": existing.get("reason")})
    if not already:
        record["hold_released_after_seconds"] = _held_for_seconds(existing)
    else:
        record["already_released"] = True
    record["hold_count"] = int(existing.get("hold_count") or 0)
    store.state.setdefault(CONTROLS_COLLECTION, {})[DISPATCH_KEY] = record
    store._save()
    log_event(severity="INFO", operation="control.release", code="AUTOMATION_RESUMED",
              message="automated dispatch hold released", actor=who, note=comment)
    return hold_view(store, changed=not already)


def _held_for_seconds(record: dict[str, Any]) -> int | None:
    """How long the hold was on, from the two timestamps that exist - never from a guess."""
    try:
        from datetime import datetime

        started = datetime.fromisoformat(str(record.get("set_at")).replace("Z", "+00:00"))
        return max(0, int((datetime.now(started.tzinfo) - started).total_seconds()))
    except (TypeError, ValueError):
        return None


def blocked_by_hold(store, *, limit: int = 25) -> list[dict[str, Any]]:
    """Jobs that a dispatch attempt refused *because of the hold*, so an operator knows what to re-run."""
    rows = []
    for job in store.state.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        if job.get("status") == "BLOCKED" and job.get("error_code") == HOLD_CODE:
            rows.append({"job_id": job.get("job_id"), "job_type": job.get("job_type"),
                         "lead_id": job.get("lead_id"), "project_id": job.get("project_id"),
                         "attempts": job.get("attempts"), "updated_at": job.get("updated_at")})
    rows.sort(key=lambda row: str(row.get("job_id") or ""))
    return rows[:max(1, min(int(limit or 25), 100))]


def hold_view(store, *, changed: bool = True) -> dict[str, Any]:
    """The one answer every surface renders: held or not, since when, by whom, and what it means."""
    record = _state(store)
    collection = store.state.get(CONTROLS_COLLECTION)
    unreadable = collection is not None and not isinstance(collection, dict)
    if not unreadable and record == {} and isinstance(collection, dict) and collection.get(DISPATCH_KEY) is not None:
        unreadable = True
    if unreadable:
        log_event(severity="ERROR", operation="control.read", code="AUTOMATION_CONTROL_UNREADABLE",
                  message="the automation control record is present but cannot be read; dispatch is not held by it")
    view = {
        "held": record.get("held") is True,
        "set_at": record.get("set_at"),
        "set_by": record.get("set_by"),
        "reason": record.get("reason"),
        "note": record.get("note"),
        "released_at": record.get("released_at"),
        "released_by": record.get("released_by"),
        "hold_count": int(record.get("hold_count") or 0) or None,
        "release_count": int(record.get("release_count") or 0) or None,
        "code": HOLD_CODE if record.get("held") is True else "AUTOMATION_RESUMED",
        "changed": bool(changed),
        "unreadable": unreadable,
        "scope": ("Execution started through JobService.run (the worker, `run-job`, `POST /jobs/:id/run`, and "
                  "`/jobs/:id/retry?run`) is refused with AUTOMATION_HELD before any provider is contacted."),
        "does_not": ("Revokes no approval, changes no ledger record, blocks no human action in the CLI or API, "
                     "and re-queues nothing: jobs a held dispatch attempt marked BLOCKED are re-run by an "
                     "explicit `retry-job` after the hold is released."),
        "next_action": ("Release with `python3 -m orchestration.cli release-automation --actor <you>` or "
                        "/resume in Telegram." if record.get("held") is True else
                        "Set with `python3 -m orchestration.cli hold-automation --actor <you> --reason ...` or "
                        "/pause in Telegram."),
    }
    if unreadable:
        view["error_code"] = "AUTOMATION_CONTROL_UNREADABLE"
        view["next_action"] = ("Inspect automation_controls.dispatch in the tracking state: it is present but is "
                              "not a readable record, so no answer here can be trusted beyond 'not held'.")
    return view
