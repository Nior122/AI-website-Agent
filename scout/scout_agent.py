"""Offline-first candidate discovery, normalization, deduplication, and triage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
import json
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import time


JOB_STATES = {"pending", "running", "completed", "partial", "failed"}
WEBSITE_STATUSES = {"none_known", "unknown", "professional", "outdated", "poor", "basic"}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _normal(value: Any) -> str | None:
    value = _text(value)
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized or None


def _url(value: Any) -> str | None:
    value = _text(value)
    if value is None:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _url_host(value: str | None) -> str | None:
    return urlparse(value).netloc.casefold() if value else None


def _list_of_text(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in (_text(item) for item in value) if item))


@dataclass(frozen=True)
class Target:
    country: str
    city: str
    niche: str
    lead_limit: int = 10
    dry_run: bool = True

    def __post_init__(self) -> None:
        if not all(_text(value) for value in (self.country, self.city, self.niche)):
            raise ValueError("country, city, and niche are required")
        if not 1 <= self.lead_limit <= 100:
            raise ValueError("lead_limit must be between 1 and 100")


class DiscoverySource(Protocol):
    """A lawful, approved source adapter. Implementations return raw public candidates."""

    name: str

    def discover(self, target: Target) -> list[dict[str, Any]]: ...


@dataclass
class FixtureSource:
    """Loads intentional test data only; it never reaches the network."""

    path: Path
    name: str = "fixture"

    def discover(self, target: Target) -> list[dict[str, Any]]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("fixture source must contain a JSON array")
        return [item for item in payload if isinstance(item, dict)]


@dataclass
class OverpassRestaurantSource:
    """Small, public, read-only Overpass adapter for explicitly approved review runs.

    It deliberately supports only the configured Lagos restaurant pilot. It makes
    one bounded request, retains the public OSM element URL as evidence, and does
    not inspect business sites, send messages, or follow contact links.
    """

    endpoint: str = "https://overpass-api.de/api/interpreter"
    fallback_endpoints: list[str] = field(default_factory=list)
    timeout_seconds: int = 30
    max_requests: int = 1
    min_delay_seconds: float = 1.0
    name: str = "openstreetmap_overpass"
    _requests_made: int = field(default=0, init=False, repr=False)
    _last_request_at: float | None = field(default=None, init=False, repr=False)

    def discover(self, target: Target) -> list[dict[str, Any]]:
        if _normal(target.country) != "nigeria" or _normal(target.city) != "lagos" or _normal(target.niche) not in {"restaurant", "restaurants"}:
            raise ValueError("This pilot adapter is approved only for Lagos, Nigeria restaurants.")
        limit = min(target.lead_limit, 5)
        query = _overpass_lagos_restaurants_query(limit)
        endpoints = [self.endpoint, *self.fallback_endpoints]
        last_error = None
        for ep in endpoints:
            if self._requests_made >= self.max_requests:
                if last_error:
                    break
                raise RuntimeError("Overpass request limit reached for this source run.")
            if self._last_request_at is not None:
                remaining = self.min_delay_seconds - (time.monotonic() - self._last_request_at)
                if remaining > 0: time.sleep(remaining)
            request = Request(ep, data=query.encode("utf-8"), headers={"Content-Type":"application/x-www-form-urlencoded; charset=utf-8", "User-Agent":"AI-Website-Agent/0.1 review-only contact: local-operator"}, method="POST")
            try:
                self._requests_made += 1; self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    if response.status != 200: raise RuntimeError(f"Overpass returned HTTP {response.status}.")
                    payload = json.loads(response.read().decode("utf-8"))
                    elements = payload.get("elements", [])
                    mapped = [c for c in (_overpass_candidate(element) for element in elements) if c]
                    # Order first, then truncate: taking the first N elements is what made every run
                    # return the same businesses, because the API's order has nothing to do with priority.
                    mapped.sort(key=_candidate_sort_key)
                    return mapped[:limit]
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as error:
                last_error = error
                continue
        if last_error:
            raise RuntimeError(f"Overpass discovery failed safely: {type(last_error).__name__}: {last_error}") from last_error
        raise RuntimeError("Overpass request limit reached for this source run.")


def _overpass_lagos_restaurants_query(limit: int) -> str:
    """One bounded, read-only request over metropolitan Lagos restaurants.

    The union is what makes the run useful rather than merely legal. A single ``amenity=restaurant``
    query returns the same first ``limit`` elements of the box on every run, so discovery re-reported the
    same handful of businesses and yielded nothing new to review; the pilot needed candidates matching
    the actual business priority (publicly active on social media, no website on the record). Each clause
    uses only documented ``key="value"`` existence filters - no negation, no regular expressions - and the
    website-absence judgement is derived from the returned tags locally, because a missing OSM tag is
    missing evidence, never a fact about the business. The window is wider than ``limit`` precisely so
    that ranking can choose; the adapter still hands back at most ``limit`` candidates.
    """
    window = max(min(4 * limit, limit + 35), limit)
    box = "(6.40,3.20,6.70,3.60)"
    clauses = ";".join(
        f'nwr["amenity"="restaurant"][{tag}]{box}' for tag in ('"contact:facebook"', '"contact:instagram"')
    )
    return (
        f"[out:json][timeout:25];({clauses};nwr[\"amenity\"=\"restaurant\"]{box});"
        f"out center {window};"
    )



def _overpass_candidate(element: dict[str, Any]) -> dict[str, Any] | None:
    tags = element.get("tags") or {}; name = _text(tags.get("name"))
    if not name: return None
    kind, ident = element.get("type"), element.get("id")
    if kind not in {"node", "way", "relation"} or not isinstance(ident, int): return None
    website = _url(tags.get("website") or tags.get("contact:website"))
    phone = _text(tags.get("phone") or tags.get("contact:phone")); email = _text(tags.get("email") or tags.get("contact:email"))
    whatsapp = _text(tags.get("contact:whatsapp") or tags.get("whatsapp"))
    social = [url for url in (_url(tags.get("contact:instagram")), _url(tags.get("contact:facebook"))) if url]
    return {"candidate_id":f"osm-{kind}-{ident}","business_name":name,"niche":"Restaurants","city":"Lagos","country":"Nigeria","address":_text(tags.get("addr:full")) or _address(tags),"phone":phone,"email":email,"whatsapp":whatsapp,"website":website,"other_social_urls":social,"description":_text(tags.get("description")) or _text(tags.get("cuisine")),"source_url":f"https://www.openstreetmap.org/{kind}/{ident}","source_type":"OPENSTREETMAP_ELEMENT","data_classification":"REAL_PUBLIC_DATA","discovered_at":_now(),"raw_data":{"osm_type":kind,"osm_id":ident,"tags":tags}}


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int, str]:
    """Order raw public candidates by the pilot's stated priority: socially active, no website on record.

    A missing ``website`` tag ranks a candidate up but is recorded as ``none_known`` downstream, never as
    "has no website"; contact details and a name only break ties. Nothing here invents a fact.
    """
    tags = (candidate.get("raw_data") or {}).get("tags") or {}
    social = bool(candidate.get("other_social_urls")) or bool(tags.get("contact:facebook") or tags.get("contact:instagram"))
    return (
        0 if (social and not candidate.get("website")) else 1 if social else 2 if not candidate.get("website") else 3,
        0 if candidate.get("phone") or candidate.get("whatsapp") or candidate.get("email") else 1,
        0 if candidate.get("address") else 1,
        str(candidate.get("candidate_id") or ""),
    )


def _address(tags: dict[str, Any]) -> str | None:
    values=[_text(tags.get(key)) for key in ("addr:housenumber","addr:street","addr:city")]
    return ", ".join(value for value in values if value) or None


@dataclass
class ScoutJobResult:
    job_id: str
    status: str
    target: dict[str, Any]
    started_at: str
    completed_at: str | None = None
    source_counts: dict[str, int] = field(default_factory=dict)
    candidate_count: int = 0
    deduplicated_count: int = 0
    errors: list[str] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Scout:
    """Coordinates modular source adapters without coupling to a search provider."""

    def __init__(self, target: Target, sources: list[DiscoverySource]) -> None:
        if not sources:
            raise ValueError("at least one discovery source is required")
        self.target = target
        self.sources = sources

    def run(self) -> ScoutJobResult:
        now = _now()
        job = ScoutJobResult(
            job_id=f"scout-{now.replace(':', '').replace('+00:00', 'z')}",
            status="running",
            target=asdict(self.target),
            started_at=now,
        )
        candidates: list[dict[str, Any]] = []
        for source in self.sources:
            try:
                discovered = source.discover(self.target)
                job.source_counts[source.name] = len(discovered)
                for candidate in discovered:
                    candidate["_adapter_source"] = source.name
                    candidates.append(candidate)
            except Exception as error:  # Preserve successful source output for recovery.
                job.errors.append(f"{source.name}: {error}")
                job.source_counts[source.name] = 0
        job.candidate_count = len(candidates)
        normalized = [_normalize_candidate(candidate, self.target) for candidate in candidates]
        merged = _deduplicate(normalized)
        job.deduplicated_count = len(merged)
        qualified = [_qualify(lead, self.target, now) for lead in merged]
        qualified.sort(key=_lead_sort_key)
        job.leads = qualified[: self.target.lead_limit]
        job.status = "partial" if job.errors and job.leads else "failed" if job.errors else "completed"
        job.completed_at = _now()
        return job


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_candidate(raw: dict[str, Any], target: Target) -> dict[str, Any]:
    source_url = _url(raw.get("source_url"))
    social = {key: _url(raw.get(key)) for key in ("instagram_url", "facebook_url")}
    other_social = [url for url in (_url(value) for value in raw.get("other_social_urls", [])) if url]
    website = _url(raw.get("website"))
    status = _text(raw.get("website_status")) or ("none_known" if website is None else "unknown")
    status = status if status in WEBSITE_STATUSES else "unknown"
    sources = [source_url] if source_url else []
    for value in [website, *social.values(), *other_social]:
        if value and value not in sources:
            sources.append(value)
    return {
        "candidateId": _text(raw.get("candidate_id")),
        "dataClassification": _text(raw.get("data_classification")) or "TEST_FIXTURE",
        "sourceType": _text(raw.get("source_type")) or "FIXTURE",
        "businessName": _text(raw.get("business_name")),
        "normalizedBusinessName": _normalized_business_name(raw.get("business_name"), target),
        "niche": _text(raw.get("niche")),
        "location": {"city": _text(raw.get("city")), "country": _text(raw.get("country")), "address": _text(raw.get("address"))},
        "contact": {"phone": _text(raw.get("phone")), "email": _text(raw.get("email")), "whatsapp": _text(raw.get("whatsapp"))},
        "originalWebsite": website,
        "websiteStatus": status,
        "social": {"instagramUrl": social["instagram_url"], "facebookUrl": social["facebook_url"], "otherSocialUrls": other_social},
        "sourceUrls": sources,
        "discoverySources": [_text(raw.get("_adapter_source")) or "unknown"],
        "publicBusinessInformation": {"servicesOrProducts": _list_of_text(raw.get("services")), "description": _text(raw.get("description")), "openingHours": _text(raw.get("opening_hours")), "brandNotes": None, "assetReferences": []},
        "fieldSources": _field_sources(raw, source_url),
        "notes": _list_of_text(raw.get("notes")),
        "rawSourceRecord": raw.get("raw_data") if isinstance(raw.get("raw_data"), dict) else None,
    }


def _normalized_business_name(value: Any, target: Target) -> str | None:
    """Remove an exact requested city/country suffix before name normalization.

    This handles common directory labels such as "Business Name - Port Harcourt"
    without guessing at unrelated abbreviations or using fuzzy matching.
    """
    value = _text(value)
    if value is None:
        return None
    for place in (target.country, target.city):
        value = re.sub(rf"(?:\s*[-,|()]?\s*){re.escape(place)}\s*$", "", value, flags=re.IGNORECASE).strip()
    return _normal(value)


def _field_sources(raw: dict[str, Any], source_url: str | None) -> dict[str, list[str]]:
    if not source_url:
        return {}
    mapping = {
        "businessName": raw.get("business_name"), "niche": raw.get("niche"), "location": raw.get("city") or raw.get("country"),
        "contact": raw.get("phone") or raw.get("email") or raw.get("whatsapp"), "originalWebsite": raw.get("website"),
        "social": raw.get("instagram_url") or raw.get("facebook_url") or raw.get("other_social_urls"),
        "publicBusinessInformation": raw.get("services") or raw.get("description") or raw.get("opening_hours"),
    }
    return {field: [source_url] for field, value in mapping.items() if value}


def _deduplicate(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for lead in leads:
        matching = next((existing for existing in merged if _is_same_business(existing, lead)), None)
        if matching is None:
            merged.append(lead)
        else:
            _merge(matching, lead)
    return merged


def _is_same_business(left: dict[str, Any], right: dict[str, Any]) -> bool:
    same_name = left["normalizedBusinessName"] and left["normalizedBusinessName"] == right["normalizedBusinessName"]
    if not same_name:
        return False
    left_location, right_location = left["location"], right["location"]
    same_location = _normal(left_location["city"]) == _normal(right_location["city"]) and _normal(left_location["country"]) == _normal(right_location["country"])
    if same_location:
        return True
    return bool(_identifiers(left) & _identifiers(right))


def _identifiers(lead: dict[str, Any]) -> set[str]:
    contact, social = lead["contact"], lead["social"]
    values = [_normal(contact["phone"]), _normal(contact["whatsapp"]), _url_host(lead["originalWebsite"]), social["instagramUrl"], social["facebookUrl"]]
    return {value for value in values if value}


def _merge(destination: dict[str, Any], incoming: dict[str, Any]) -> None:
    for field in ("businessName", "niche", "originalWebsite"):
        destination[field] = destination[field] or incoming[field]
    for group in ("location", "contact", "social", "publicBusinessInformation"):
        for key, value in incoming[group].items():
            if isinstance(value, list):
                destination[group][key] = list(dict.fromkeys(destination[group][key] + value))
            elif destination[group].get(key) is None:
                destination[group][key] = value
    destination["sourceUrls"] = list(dict.fromkeys(destination["sourceUrls"] + incoming["sourceUrls"]))
    destination["discoverySources"] = list(dict.fromkeys(destination["discoverySources"] + incoming["discoverySources"]))
    destination["notes"] = list(dict.fromkeys(destination["notes"] + incoming["notes"]))
    for field, urls in incoming["fieldSources"].items():
        destination["fieldSources"][field] = list(dict.fromkeys(destination["fieldSources"].get(field, []) + urls))
    if destination["websiteStatus"] in {"unknown", "none_known"}:
        destination["websiteStatus"] = incoming["websiteStatus"]


def _qualify(lead: dict[str, Any], target: Target, checked_at: str) -> dict[str, Any]:
    reasons: list[str] = []
    active_signals = _active_signals(lead)
    name_present = bool(lead["businessName"])
    target_match = _normal(lead["location"]["city"]) == _normal(target.city) and _normal(lead["location"]["country"]) == _normal(target.country)
    niche_match = _normal(lead["niche"]) == _normal(target.niche)
    enough_information = bool(lead["sourceUrls"]) and bool(active_signals)
    if not name_present:
        result = "skipped"; reasons.append("Missing business name.")
    elif not target_match:
        result = "skipped"; reasons.append("Location does not match the requested target.")
    elif not niche_match:
        result = "skipped"; reasons.append("Niche does not match the requested target or is not source-backed.")
    elif not enough_information:
        result = "skipped"; reasons.append("Insufficient public, source-backed activity information.")
    else:
        reasons.append("Source-backed activity signals: " + ", ".join(active_signals) + ".")
        status = lead["websiteStatus"]
        outcomes = {"none_known": ("qualified", 4, "No professional website is known from the collected sources."), "poor": ("qualified", 3, "An approved source/verifier marked the website as poor."), "outdated": ("qualified", 3, "An approved source/verifier marked the website as outdated."), "basic": ("lower_priority", 1, "Website is functioning but marked basic; modernization may be useful."), "unknown": ("needs_review", 1, "Website existence is known but its quality has not been verified."), "professional": ("skipped", 0, "Website is marked professional; this is a low-priority opportunity.")}
        result, opportunity, reason = outcomes[status]
        reasons.append(reason)
        lead["websiteOpportunityScore"] = opportunity
    if "websiteOpportunityScore" not in lead:
        lead["websiteOpportunityScore"] = 0
    lead["onlinePresenceScore"] = min(5, len(active_signals))
    lead["qualificationStatus"] = result
    lead["qualificationReason"] = " ".join(reasons)
    lead["confidence"] = "high" if len(lead["sourceUrls"]) >= 2 and target_match else "medium" if enough_information else "low"
    lead["discoveredAt"] = None
    lead["lastCheckedAt"] = checked_at
    return lead


def _active_signals(lead: dict[str, Any]) -> list[str]:
    social = lead["social"]
    contact = lead["contact"]
    checks = [("social profile", social["instagramUrl"] or social["facebookUrl"] or social["otherSocialUrls"]), ("contact channel", contact["phone"] or contact["whatsapp"] or contact["email"]), ("services", lead["publicBusinessInformation"]["servicesOrProducts"]), ("opening hours", lead["publicBusinessInformation"]["openingHours"]), ("description", lead["publicBusinessInformation"]["description"])]
    return [label for label, value in checks if value]


def _lead_sort_key(lead: dict[str, Any]) -> tuple[int, int, int, str]:
    """Qualified first, then website opportunity, then how publicly active the business is, then name.

    The online-presence term is the point: two equally qualified candidates should surface the one with
    more evidence of an active audience first, because that is the profile the pilot is for.
    """
    priority = {"qualified": 0, "needs_review": 1, "lower_priority": 2, "skipped": 3}
    return (priority[lead["qualificationStatus"]], -lead["websiteOpportunityScore"], -lead["onlinePresenceScore"],
            lead["normalizedBusinessName"] or "")
