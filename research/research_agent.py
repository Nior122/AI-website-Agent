"""Evidence-led offline research and website qualification for Scout candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
import json
import re


RESEARCH_STATES = {"pending", "running", "completed", "partial", "failed"}
WEBSITE_STATUSES = {"NO_WEBSITE", "WEBSITE_UNAVAILABLE", "WEBSITE_BASIC", "WEBSITE_OUTDATED", "WEBSITE_NEEDS_IMPROVEMENT", "WEBSITE_MODERN", "UNKNOWN"}
EVIDENCE_TIERS = (
    "PRESERVED_PUBLIC_OBSERVATION",   # 1. Preserved observations from actual public sources
    "SOURCE_URL_ONLY",                # 2. Source URLs without preserved supporting observations
    "GENERATED_CONTENT_COPY",         # 3. Facts copied into generated website content
    "TEST_FIXTURE_OR_DOCUMENTATION",  # 4. Test fixtures, synthetic records, and documentation
    "CONFLICTING_OR_UNVERIFIABLE",    # 5. Conflicting, stale, or unverifiable claims
)
VERIFICATION_STATUSES = {
    "VERIFIED",
    "SUPPORTED_BY_SOURCE_URL",
    "UNVERIFIED",
    "CONFLICTING",
    "WITHHELD",
    "STALE",
    "SYNTHETIC_FIXTURE",
}


def is_fixture_or_doc_source(url: Any) -> bool:
    """Return True if a source reference points to a test fixture, synthetic domain, or documentation."""
    if not isinstance(url, str) or not url.strip():
        return False
    lowered = url.strip().casefold()
    if "example.invalid" in lowered or lowered.startswith("fixture:") or lowered.startswith("test:"):
        return True
    if any(token in lowered for token in ("tests/fixtures", "test_phase", "changelog.md", "tasks.md", "readme.md", "architecture.md")):
        return True
    return False


def classify_evidence_tier(
    *,
    source_urls: list[str],
    data_classification: str,
    excerpt: str | None = None,
    observed_at: str | None = None,
    has_conflict: bool = False,
    verification_status: str | None = None,
    from_generated_content: bool = False,
) -> str:
    """Classify an evidence item into the 5-level evidence hierarchy without fabricating provenance."""
    if from_generated_content:
        return "GENERATED_CONTENT_COPY"
    if data_classification == "TEST_FIXTURE" or (source_urls and all(is_fixture_or_doc_source(u) for u in source_urls)):
        return "TEST_FIXTURE_OR_DOCUMENTATION"
    if has_conflict or verification_status in {"CONFLICTING", "UNVERIFIED", "STALE", "WITHHELD"}:
        return "CONFLICTING_OR_UNVERIFIABLE"
    valid_public_urls = [u for u in source_urls if isinstance(u, str) and u.strip() and not is_fixture_or_doc_source(u)]
    if not valid_public_urls:
        return "CONFLICTING_OR_UNVERIFIABLE"
    if _text(excerpt):
        return "PRESERVED_PUBLIC_OBSERVATION"
    return "SOURCE_URL_ONLY"


def _text(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _normal(value: Any) -> str | None:
    value = _text(value)
    return re.sub(r"[^a-z0-9]", "", value.casefold()) or None if value else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ResearchSource(Protocol):
    """An approved source returns public, source-backed evidence for one candidate."""

    name: str

    def research(self, candidate: dict[str, Any]) -> list[dict[str, Any]]: ...


@dataclass
class FixtureResearchSource:
    """Loads fictional evidence only; it makes no requests and has no credentials."""

    path: Path
    name: str = "fixture_research"

    def research(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        records = json.loads(self.path.read_text(encoding="utf-8"))
        key = _normal(candidate.get("businessName"))
        return [record for record in records if _normal(record.get("candidate_name")) == key]


@dataclass
class ResearchJobResult:
    job_id: str
    status: str
    started_at: str
    completed_at: str | None = None
    research_sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResearchAgent:
    """Enriches one Scout candidate using replaceable, evidence-returning sources."""

    def __init__(self, sources: list[ResearchSource]) -> None:
        if not sources:
            raise ValueError("at least one research source is required")
        self.sources = sources

    def run(self, candidate: dict[str, Any]) -> ResearchJobResult:
        started_at = _now()
        job = ResearchJobResult(job_id=f"research-{started_at.replace(':', '').replace('Z', 'z')}", status="running", started_at=started_at)
        evidence: list[dict[str, Any]] = []
        for source in self.sources:
            try:
                found = source.research(candidate)
                evidence.extend(found)
                if found:
                    job.research_sources.append(source.name)
            except Exception as error:
                job.errors.append(f"{source.name}: {error}")
        job.record = _build_record(candidate, evidence, started_at, job.errors)
        job.status = "partial" if job.errors and evidence else "failed" if job.errors and not evidence else "completed"
        job.completed_at = _now()
        job.record["research"]["completedAt"] = job.completed_at
        job.record["research"]["status"] = job.status
        return job


def _build_record(candidate: dict[str, Any], evidence: list[dict[str, Any]], checked_at: str, errors: list[str]) -> dict[str, Any]:
    data_classification = candidate.get("dataClassification") or "TEST_FIXTURE"
    facts, uncertainties = _facts_from_candidate(candidate)
    filtered_evidence: list[dict[str, Any]] = []
    for item in evidence:
        item_class = item.get("data_classification") or item.get("dataClassification")
        if data_classification == "REAL_PUBLIC_DATA" and (
            item_class == "TEST_FIXTURE"
            or item.get("demo") is True
            or is_fixture_or_doc_source(item.get("source_url"))
        ):
            uncertainties.append({
                "field": "evidence_source",
                "reason": f"Rejected synthetic/fixture/documentation source '{item.get('source_url')}' for REAL_PUBLIC_DATA candidate.",
            })
            continue
        filtered_evidence.append(item)
    identity = _verify_identity(candidate, filtered_evidence)
    matching = [item for item in filtered_evidence if item["identityStatus"] == "verified"]
    if identity["status"] == "mismatch":
        matching = []
    facts.extend(_facts_from_evidence(matching))
    reconciled_facts, conflicting_facts, evidence_records = _reconcile_facts_and_conflicts(
        facts,
        data_classification=data_classification,
        uncertainties=uncertainties,
    )
    website = _evaluate_website(candidate, matching)
    activity = _evaluate_activity(candidate, matching)
    qualification = _qualify(candidate, identity, reconciled_facts, website, activity, uncertainties, errors)
    sources = _unique([url for item in matching for url in [item.get("source_url")] if _text(url)])
    return {
        "data_classification": data_classification,
        "candidate": candidate,
        "research": {
            "status": "running",
            "startedAt": checked_at,
            "completedAt": None,
            "researchSources": sources,
            "verifiedFacts": reconciled_facts,
            "conflictingFacts": conflicting_facts,
            "evidenceRecords": evidence_records,
            "inferences": qualification.pop("inferences"),
            "uncertainties": uncertainties,
            "identity": identity,
            "websiteEvaluation": website,
            "businessActivity": activity,
            "opportunityFactors": qualification.pop("factors"),
            "qualificationStatus": qualification["status"],
            "qualificationReason": qualification["reason"],
            "qualificationConfidence": qualification["confidence"],
            "errors": errors,
        },
    }


def _has_meaningful_value(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        return any(_has_meaningful_value(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_meaningful_value(v) for v in value)
    return True


def _facts_from_candidate(candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    facts, uncertainties = [], []
    sources = candidate.get("fieldSources", {})
    fields = {"businessName": candidate.get("businessName"), "niche": candidate.get("niche"), "location": candidate.get("location"), "contact": candidate.get("contact"), "originalWebsite": candidate.get("originalWebsite"), "social": candidate.get("social"), "services": candidate.get("publicBusinessInformation", {}).get("servicesOrProducts"), "description": candidate.get("publicBusinessInformation", {}).get("description"), "openingHours": candidate.get("publicBusinessInformation", {}).get("openingHours")}
    for field, value in fields.items():
        if _has_meaningful_value(value):
            facts.append({"field": field, "value": value, "sourceUrls": sources.get(field, candidate.get("sourceUrls", []))})
        else:
            uncertainties.append({"field": field, "reason": "No source-backed value was available."})
    return facts, uncertainties


def _verify_identity(candidate: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    comparisons = []
    for item in evidence:
        signals = []
        if _normal(item.get("business_name")) == _normal(candidate.get("businessName")):
            signals.append("matching business name")
        location = candidate.get("location", {})
        if _normal(item.get("city")) == _normal(location.get("city")) and _normal(item.get("country")) == _normal(location.get("country")):
            signals.append("matching city and country")
        candidate_phone = _normal(candidate.get("contact", {}).get("phone"))
        if candidate_phone and candidate_phone == _normal(item.get("phone")):
            signals.append("matching phone")
        candidate_social = candidate.get("social", {}).get("instagramUrl")
        if candidate_social and candidate_social == item.get("instagram_url"):
            signals.append("matching Instagram URL")
        status = "verified" if len(signals) >= 2 else "review" if signals else "mismatch"
        item["identityStatus"] = status
        comparisons.append({"sourceUrl": item.get("source_url"), "status": status, "signals": signals})
    verified = [item for item in comparisons if item["status"] == "verified"]
    review = [item for item in comparisons if item["status"] == "review"]
    return {"status": "verified" if verified else "review" if review or not comparisons else "mismatch", "evidence": comparisons}


def _facts_from_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    facts = []
    fields = {
        "address": "address",
        "phone": "phone",
        "email": "email",
        "whatsapp": "whatsapp",
        "instagram_url": "instagram_url",
        "facebook_url": "facebook_url",
        "services": "services",
        "description": "description",
        "openingHours": "opening_hours",
        "publicPricing": "public_pricing",
        "assetUrls": "asset_urls",
    }
    for item in evidence:
        for field, key in fields.items():
            value = item.get(key)
            if value not in (None, [], ""):
                entry: dict[str, Any] = {"field": field, "value": value, "sourceUrls": [item["source_url"]]}
                if item.get("observed_at"):
                    entry["observed_at"] = item["observed_at"]
                if item.get("excerpt"):
                    entry["excerpt"] = item["excerpt"]
                facts.append(entry)
    return facts


def _scalar_entries_from_facts(facts: list[dict[str, Any]]) -> dict[str, list[tuple[Any, list[str], str | None, str | None]]]:
    """Extract scalar field values (including nested location/contact/social) to detect conflicts."""
    observed: dict[str, list[tuple[Any, list[str], str | None, str | None]]] = {}

    def _add(field_name: str, val: Any, urls: list[str], obs_at: str | None, exc: str | None) -> None:
        if not _has_meaningful_value(val) or isinstance(val, (dict, list)):
            return
        observed.setdefault(field_name, []).append((val, list(urls or []), obs_at, exc))

    for item in facts:
        f_name = item.get("field")
        f_val = item.get("value")
        urls = list(item.get("sourceUrls") or [])
        obs_at = item.get("observed_at") or item.get("observedAt")
        exc = item.get("excerpt")
        if f_name == "location" and isinstance(f_val, dict):
            _add("address", f_val.get("address"), urls, obs_at, exc)
            _add("city", f_val.get("city"), urls, obs_at, exc)
            _add("country", f_val.get("country"), urls, obs_at, exc)
        elif f_name == "contact" and isinstance(f_val, dict):
            _add("phone", f_val.get("phone"), urls, obs_at, exc)
            _add("email", f_val.get("email"), urls, obs_at, exc)
            _add("whatsapp", f_val.get("whatsapp"), urls, obs_at, exc)
        elif f_name == "social" and isinstance(f_val, dict):
            _add("instagram_url", f_val.get("instagramUrl") or f_val.get("instagram_url"), urls, obs_at, exc)
            _add("facebook_url", f_val.get("facebookUrl") or f_val.get("facebook_url"), urls, obs_at, exc)
        elif isinstance(f_name, str):
            _add(f_name, f_val, urls, obs_at, exc)
    return observed


def _reconcile_facts_and_conflicts(
    facts: list[dict[str, Any]],
    *,
    data_classification: str,
    uncertainties: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Detect conflicting scalar facts, withhold disputed values, and build structured evidence records."""
    scalar_map = _scalar_entries_from_facts(facts)
    conflicting_fields: dict[str, dict[str, Any]] = {}
    for field_name, entries in scalar_map.items():
        distinct_values: list[Any] = []
        all_urls: list[str] = []
        for val, urls, _, _ in entries:
            norm_val = _text(val) if isinstance(val, str) else val
            if norm_val and norm_val not in distinct_values:
                distinct_values.append(norm_val)
            all_urls.extend(urls)
        if len(distinct_values) > 1:
            reason = f"Conflicting values observed for '{field_name}': {distinct_values}; withheld until verified by a current primary source."
            conflicting_fields[field_name] = {
                "field": field_name,
                "values": distinct_values,
                "sourceUrls": _unique(all_urls),
                "observed_at": None,
                "excerpt": None,
                "verification_status": "CONFLICTING",
                "evidence_tier": "CONFLICTING_OR_UNVERIFIABLE",
                "reason": reason,
                "data_classification": data_classification,
            }
            uncertainties.append({"field": field_name, "reason": reason})

    reconciled: list[dict[str, Any]] = []
    evidence_records: list[dict[str, Any]] = []
    for item in facts:
        f_name = item.get("field")
        f_val = item.get("value")
        urls = [u for u in (item.get("sourceUrls") or []) if isinstance(u, str) and u.strip()]
        obs_at = item.get("observed_at") or item.get("observedAt")
        exc = item.get("excerpt")
        if data_classification == "REAL_PUBLIC_DATA":
            urls = [u for u in urls if not is_fixture_or_doc_source(u)]
            if not urls:
                uncertainties.append({
                    "field": str(f_name),
                    "reason": "Withheld fact lacking non-fixture public source URL.",
                })
                evidence_records.append({
                    "field": f_name,
                    "value": f_val,
                    "sourceUrls": list(item.get("sourceUrls") or []),
                    "observed_at": obs_at,
                    "excerpt": exc,
                    "evidence_tier": "TEST_FIXTURE_OR_DOCUMENTATION" if item.get("sourceUrls") else "CONFLICTING_OR_UNVERIFIABLE",
                    "verification_status": "UNVERIFIED",
                    "conflicts": [],
                    "uncertainty": "No non-fixture public source URL supports this fact.",
                    "data_classification": data_classification,
                })
                continue

        if f_name in conflicting_fields:
            continue

        if f_name == "location" and isinstance(f_val, dict):
            cleaned_loc = dict(f_val)
            if "address" in conflicting_fields:
                cleaned_loc.pop("address", None)
            if not _has_meaningful_value(cleaned_loc):
                continue
            f_val = cleaned_loc
        elif f_name == "contact" and isinstance(f_val, dict):
            cleaned_contact = dict(f_val)
            for sub in ("phone", "email", "whatsapp"):
                if sub in conflicting_fields:
                    cleaned_contact.pop(sub, None)
            if not _has_meaningful_value(cleaned_contact):
                continue
            f_val = cleaned_contact
        elif f_name == "social" and isinstance(f_val, dict):
            cleaned_social = dict(f_val)
            if "instagram_url" in conflicting_fields:
                cleaned_social.pop("instagramUrl", None)
                cleaned_social.pop("instagram_url", None)
            if "facebook_url" in conflicting_fields:
                cleaned_social.pop("facebookUrl", None)
                cleaned_social.pop("facebook_url", None)
            if not _has_meaningful_value(cleaned_social):
                continue
            f_val = cleaned_social

        tier = classify_evidence_tier(
            source_urls=urls,
            data_classification=data_classification,
            excerpt=exc,
            observed_at=obs_at,
            has_conflict=False,
        )
        ver_status = (
            "SYNTHETIC_FIXTURE"
            if data_classification == "TEST_FIXTURE"
            else "VERIFIED"
            if tier == "PRESERVED_PUBLIC_OBSERVATION"
            else "SUPPORTED_BY_SOURCE_URL"
        )
        fact_entry: dict[str, Any] = {
            "field": f_name,
            "value": f_val,
            "sourceUrls": urls,
        }
        if obs_at:
            fact_entry["observed_at"] = obs_at
        if exc:
            fact_entry["excerpt"] = exc
        reconciled.append(fact_entry)
        evidence_records.append({
            "field": f_name,
            "value": f_val,
            "sourceUrls": urls,
            "observed_at": obs_at,
            "excerpt": exc,
            "evidence_tier": tier,
            "verification_status": ver_status,
            "conflicts": [],
            "uncertainty": None,
            "data_classification": data_classification,
        })

    for conflict in conflicting_fields.values():
        evidence_records.append({
            "field": conflict["field"],
            "value": None,
            "sourceUrls": conflict["sourceUrls"],
            "observed_at": None,
            "excerpt": None,
            "evidence_tier": "CONFLICTING_OR_UNVERIFIABLE",
            "verification_status": "CONFLICTING",
            "conflicts": conflict["values"],
            "uncertainty": conflict["reason"],
            "data_classification": data_classification,
        })

    return reconciled, list(conflicting_fields.values()), evidence_records


def _evaluate_website(candidate: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    website = candidate.get("originalWebsite")
    observation = next((item.get("website_observation") for item in evidence if item.get("website_observation")), None)
    if not website:
        return {"status": "NO_WEBSITE", "observations": [], "sourceUrls": [], "uncertainties": ["No website was present in the reviewed candidate evidence."]}
    if not observation:
        return {"status": "UNKNOWN", "observations": [], "sourceUrls": [], "uncertainties": ["Website exists but was not inspected by an approved source."]}
    source = observation.get("source_url")
    if observation.get("load") in {"unavailable", "blocked", "timeout"}:
        return {"status": "WEBSITE_UNAVAILABLE", "observations": [f"Website observation: {observation['load']}."], "sourceUrls": [source] if source else [], "uncertainties": []}
    if observation.get("load") != "loaded" or observation.get("correct_business") is not True:
        return {"status": "UNKNOWN", "observations": [], "sourceUrls": [source] if source else [], "uncertainties": ["Loading or business identity could not be verified."]}
    problems = [key for key in ("poor_mobile", "unclear_navigation", "missing_services", "missing_contact", "missing_cta", "broken_links", "outdated_information", "untrustworthy") if observation.get(key) is True]
    if observation.get("outdated_information") is True:
        status = "WEBSITE_OUTDATED"
    elif problems:
        status = "WEBSITE_NEEDS_IMPROVEMENT"
    elif observation.get("mobile_friendly") is True and observation.get("clear_navigation") is True and observation.get("clear_services") is True and observation.get("easy_contact") is True and observation.get("clear_cta") is True:
        status = "WEBSITE_MODERN"
    else:
        status = "WEBSITE_BASIC"
    return {"status": status, "observations": [f"Observed issue: {item}." for item in problems], "sourceUrls": [source] if source else [], "uncertainties": []}


def _evaluate_activity(candidate: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    signals = []
    if candidate.get("social", {}).get("instagramUrl") or candidate.get("social", {}).get("facebookUrl"):
        signals.append("public social profile")
    if candidate.get("contact", {}).get("phone") or candidate.get("contact", {}).get("whatsapp"):
        signals.append("public contact channel")
    if candidate.get("publicBusinessInformation", {}).get("openingHours"):
        signals.append("published opening hours")
    signals.extend(item["activity_signal"] for item in evidence if _text(item.get("activity_signal")))
    return {"status": "active" if signals else "unknown", "signals": _unique(signals), "confidence": "medium" if signals else "low"}


def _qualify(candidate: dict[str, Any], identity: dict[str, Any], facts: list[dict[str, Any]], website: dict[str, Any], activity: dict[str, Any], uncertainties: list[dict[str, str]], errors: list[str]) -> dict[str, Any]:
    factors = {"businessLegitimacy": identity["status"], "businessActivity": activity["status"], "publicInformation": "sufficient" if len(facts) >= 4 else "limited", "website": website["status"], "socialPresence": bool(candidate.get("social", {}).get("instagramUrl") or candidate.get("social", {}).get("facebookUrl")), "previewReadiness": "sufficient" if len(facts) >= 5 else "limited"}
    inferences = []
    if identity["status"] == "mismatch":
        return {"status": "SKIP", "reason": "Research evidence appears to describe a different business.", "confidence": "high", "factors": factors, "inferences": inferences}
    if identity["status"] != "verified":
        return {"status": "REVIEW", "reason": "Business identity is not sufficiently verified across public sources.", "confidence": "low", "factors": factors, "inferences": inferences}
    if errors:
        return {"status": "REVIEW", "reason": "Research was incomplete because one or more sources failed.", "confidence": "low", "factors": factors, "inferences": inferences}
    if website["status"] == "WEBSITE_MODERN":
        return {"status": "SKIP", "reason": "Verified website observations indicate a modern site with core customer information.", "confidence": "high", "factors": factors, "inferences": inferences}
    if activity["status"] != "active":
        return {"status": "REVIEW", "reason": "Business activity could not be determined confidently.", "confidence": "low", "factors": factors, "inferences": inferences}
    if factors["publicInformation"] == "limited":
        return {"status": "REVIEW", "reason": "Public information is insufficient to prepare a reliable website preview.", "confidence": "medium", "factors": factors, "inferences": inferences}
    if website["status"] in {"NO_WEBSITE", "WEBSITE_UNAVAILABLE", "WEBSITE_OUTDATED", "WEBSITE_NEEDS_IMPROVEMENT"}:
        inferences.append({"statement": "A professional website may improve this business's online presence.", "basis": [website["status"], "active public signals", "sufficient source-backed information"]})
        return {"status": "QUALIFIED", "reason": f"Verified active business with {website['status']} and sufficient public information for a preview.", "confidence": "medium", "factors": factors, "inferences": inferences}
    return {"status": "REVIEW", "reason": "Website quality remains uncertain and needs human review.", "confidence": "medium", "factors": factors, "inferences": inferences}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
