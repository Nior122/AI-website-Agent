"""Conservative branch-identity comparison for public-evidence reviews.

This helper deliberately compares only supplied location strings.  It does not
fuzzy-match names, infer a relationship between branches, or make a website
ownership/qualification decision.
"""

from __future__ import annotations

import re


def compare_branch_location(recorded_locations: list[str], observed_location: str | None) -> str:
    """Return ``MATCHED``, ``CONFLICT``, or ``UNKNOWN`` without guesswork."""
    observed = _normalize(observed_location)
    expected = {_normalize(location) for location in recorded_locations if _normalize(location)}
    if not observed or not expected:
        return "UNKNOWN"
    if observed in expected:
        return "MATCHED"
    return "CONFLICT"


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
