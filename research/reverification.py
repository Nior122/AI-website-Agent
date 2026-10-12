"""Small, dependency-free audit records for approved real-data re-verification.

This module deliberately does not retrieve web pages.  A human or approved
source adapter supplies public evidence; the module only validates and records
the resulting event.  Keeping that separation makes the record portable and
prevents an accidental live-research capability from appearing in the core
application.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


PROPERTY_CLASSES = {
    "OFFICIAL",
    "LIKELY_OFFICIAL",
    "THIRD_PARTY",
    "DIRECTORY",
    "SOCIAL",
    "ORDERING_PLATFORM",
    "UNKNOWN",
}

WEBSITE_STATUSES = {"NO_WEBSITE", "WEBSITE_FOUND", "WEBSITE_UNREACHABLE", "WEBSITE_UNKNOWN", "UNKNOWN"}
DECISIONS = {
    "QUALIFIED_WEBSITE_OPPORTUNITY",
    "NOT_YET_QUALIFIED",
    "NO_OPPORTUNITY",
    "NEEDS_REVIEW",
    # Retained for compatibility with earlier persisted research audits.
    "NEEDS_MORE_REVIEW",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_reverification_event(
    *,
    candidate_id: str,
    prior_source_urls: list[str],
    evidence: list[dict[str, Any]],
    website_status: str,
    decision: str,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Create a provenance-preserving event without changing a lead/project.

    Evidence needs a URL, public source type, claim, confidence, and whether
    the source itself appears official.  Property classifications stay
    explicit so a directory cannot silently become an official website.
    """
    if not candidate_id:
        raise ValueError("candidate_id is required")
    if not prior_source_urls:
        raise ValueError("prior source evidence is required")
    if not evidence:
        raise ValueError("at least one public evidence record is required")
    if website_status not in WEBSITE_STATUSES:
        raise ValueError(f"invalid website_status: {website_status}")
    if decision not in DECISIONS:
        raise ValueError(f"invalid decision: {decision}")
    for item in evidence:
        required = ("claim", "source_url", "source_type", "confidence")
        if any(not item.get(field) for field in required) or "appears_official" not in item:
            raise ValueError("each evidence record requires claim, URL, type, confidence, and officialness")
        if not isinstance(item["appears_official"], bool):
            raise ValueError("appears_official must be a boolean")
        property_class = item.get("property_class", "UNKNOWN")
        if property_class not in PROPERTY_CLASSES:
            raise ValueError(f"invalid property_class: {property_class}")

    return {
        "event_type": "TARGETED_PUBLIC_RESEARCH_REVERIFICATION",
        "candidate_id": candidate_id,
        "data_classification": "REAL_PUBLIC_DATA",
        "mode": "REVIEW_ONLY",
        "checked_at": _now(),
        "previous_source_urls": list(dict.fromkeys(prior_source_urls)),
        "evidence": evidence,
        "result": {
            "website_status": website_status,
            "decision": decision,
            "lead_created": False,
            "project_created": False,
        },
        "notes": notes or [],
    }
