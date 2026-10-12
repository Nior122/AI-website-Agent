"""Offline, non-mutating review of an evidence-backed opportunity backlog.

This module deliberately does not discover candidates, retrieve sources, or
create tracking records.  It provides a repeatable way to review records that
were already collected through an approved process.  The existing
``assess_opportunity`` function remains the qualification policy.
"""

from __future__ import annotations

from typing import Any

from .service import assess_opportunity


BLOCKING_DECISIONS = {"NOT_YET_QUALIFIED", "NEEDS_REVIEW", "NO_OPPORTUNITY", "DISQUALIFIED"}


def review_backlog(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Review unique real-data candidate records without changing their state.

    Each record must contain a stable ``candidate_id`` and a Scout-shaped
    ``candidate`` object.  A prior evidence decision is a safety veto: it may
    prevent selection, but never promotes a candidate.  The result is safe to
    rerun because it creates no persistence records or external actions.
    """
    reviewed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []

    for record in records:
        candidate_id = str(record.get("candidate_id") or "").strip()
        candidate = record.get("candidate")
        if not candidate_id or not isinstance(candidate, dict):
            raise ValueError("each backlog record requires candidate_id and candidate")
        if candidate_id in seen_ids:
            duplicate_ids.append(candidate_id)
            continue
        seen_ids.add(candidate_id)
        if candidate.get("dataClassification") != "REAL_PUBLIC_DATA":
            continue

        assessment = assess_opportunity(candidate)
        prior_decision = record.get("prior_decision")
        blocked = prior_decision in BLOCKING_DECISIONS
        selected = assessment["status"] == "QUALIFIED" and not blocked
        decision = "QUALIFIED" if selected else prior_decision or assessment["status"]
        reviewed.append(
            {
                "candidate_id": candidate_id,
                "business_name": candidate.get("businessName"),
                "assessment": assessment,
                "prior_decision": prior_decision,
                "decision": decision,
                "selected": selected,
                "evidence_event_ids": list(dict.fromkeys(record.get("evidence_event_ids") or [])),
                "external_actions": [],
            }
        )

    selected = [item for item in reviewed if item["selected"]]
    selected.sort(key=lambda item: (-item["assessment"]["score"], item["candidate_id"]))
    return {
        "status": "QUALIFIED_OPPORTUNITY_SELECTED" if selected else "NO_QUALIFIED_OPPORTUNITY_FOUND",
        "selected_candidate": selected[0] if selected else None,
        "reviews": reviewed,
        "duplicate_candidate_ids": duplicate_ids,
        "lead_created": False,
        "project_created": False,
        "build_started": False,
        "deployment_started": False,
    }
