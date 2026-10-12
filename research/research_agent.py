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
    facts, uncertainties = _facts_from_candidate(candidate)
    identity = _verify_identity(candidate, evidence)
    matching = [item for item in evidence if item["identityStatus"] == "verified"]
    if identity["status"] == "mismatch":
        matching = []
    facts.extend(_facts_from_evidence(matching))
    website = _evaluate_website(candidate, matching)
    activity = _evaluate_activity(candidate, matching)
    qualification = _qualify(candidate, identity, facts, website, activity, uncertainties, errors)
    sources = _unique([url for item in matching for url in [item.get("source_url")] if _text(url)])
    return {
        "candidate": candidate,
        "research": {
            "status": "running",
            "startedAt": checked_at,
            "completedAt": None,
            "researchSources": sources,
            "verifiedFacts": facts,
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


def _facts_from_candidate(candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    facts, uncertainties = [], []
    sources = candidate.get("fieldSources", {})
    fields = {"businessName": candidate.get("businessName"), "niche": candidate.get("niche"), "location": candidate.get("location"), "contact": candidate.get("contact"), "originalWebsite": candidate.get("originalWebsite"), "social": candidate.get("social"), "services": candidate.get("publicBusinessInformation", {}).get("servicesOrProducts"), "description": candidate.get("publicBusinessInformation", {}).get("description"), "openingHours": candidate.get("publicBusinessInformation", {}).get("openingHours")}
    for field, value in fields.items():
        if value not in (None, [], {}):
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
    fields = {"address": "address", "phone": "phone", "email": "email", "whatsapp": "whatsapp", "services": "services", "description": "description", "openingHours": "opening_hours", "publicPricing": "public_pricing", "assetUrls": "asset_urls"}
    for item in evidence:
        for field, key in fields.items():
            value = item.get(key)
            if value not in (None, [], ""):
                facts.append({"field": field, "value": value, "sourceUrls": [item["source_url"]]})
    return facts


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
