"""Local, recoverable build coordinator for qualified research records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import re

from templates.registry import select_template
from templates.renderer import render_site
from research.research_agent import classify_evidence_tier, is_fixture_or_doc_source

from .content_generator import ContentGenerator, DeterministicContentGenerator
from .validator import validate_html


BUILD_STATES = {"PENDING", "ANALYZING", "GENERATING_CONTENT", "BUILDING", "VALIDATING", "COMPLETED", "FAILED", "NEEDS_REVIEW"}


@dataclass
class BuildResult:
    build_id: str
    lead_id: str
    template_id: str | None
    status: str
    started_at: str
    completed_at: str | None = None
    generated_content: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    output_location: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WebsiteBuilder:
    """Builds local artifacts from a qualified record; never deploys or contacts anyone."""

    _allocated_build_ids: set[str] = set()

    def __init__(self, output_root: Path = Path("output"), content_generator: ContentGenerator | None = None) -> None:
        self.output_root = output_root
        self.content_generator = content_generator or DeterministicContentGenerator()

    def build(self, lead: dict[str, Any]) -> BuildResult:
        started_at = _now()
        lead_id = _text(lead.get("lead_id")) or "unknown-lead"
        base_build_id = f"build-{started_at.replace(':', '').replace('Z', 'z')}"
        build_id = base_build_id
        target_dir = self.output_root / _safe_segment(lead_id)
        counter = 1
        while (
            build_id in self._allocated_build_ids
            or (target_dir / build_id).exists()
            or (self.output_root.exists() and any(self.output_root.glob(f"*/{build_id}")))
        ):
            build_id = f"{base_build_id}-{counter}"
            counter += 1
        self._allocated_build_ids.add(build_id)
        result = BuildResult(build_id, lead_id, None, "ANALYZING", started_at)
        try:
            facts = _verified_template_data(lead)
            result.metadata["data_classification"] = facts["data_classification"]
            result.metadata["business_name"] = facts["business_name"]
            if facts.get("fact_provenance"):
                result.metadata["fact_provenance"] = facts["fact_provenance"]
            if facts.get("disputed_facts"):
                result.metadata["disputed_facts"] = facts["disputed_facts"]
            if facts.get("withheld_fields"):
                result.metadata["withheld_fields"] = facts["withheld_fields"]
            template = select_template(facts["category"])
            result.template_id = template.template_id
            result.status = "GENERATING_CONTENT"
            content = self.content_generator.generate(facts, facts["category"])
            _assert_content_grounded(content, facts)
            result.generated_content = content
            facts["content"] = content
            result.status = "BUILDING"
            html = render_site(facts, template.template_id)
            is_fixture = _is_fixture(lead)
            artifact_dir = self.output_root / _safe_segment(lead_id) / build_id
            artifact_html_path = str(artifact_dir / "index.html")
            validation = validate_html(
                html,
                facts["business_name"],
                real_build=not is_fixture,
                build_id=build_id,
                lead_id=lead_id,
                expected_contact={"phone": facts.get("phone"), "whatsapp": facts.get("whatsapp"), "email": facts.get("email")},
                expected_facts=None if is_fixture else facts,
                synthetic_contact=is_fixture,
            )
            validation["artifact_path"] = artifact_html_path
            result.validation = validation
            if not validation["passed"]:
                result.status = "NEEDS_REVIEW"
                result.errors.extend(validation["errors"])
            else:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                (artifact_dir / "index.html").write_text(html, encoding="utf-8")
                result.output_location = artifact_html_path
                result.status = "COMPLETED"
                result.completed_at = _now()
                (artifact_dir / "build.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        except Exception as error:
            result.status = "FAILED"
            result.errors.append(str(error))
        if result.completed_at is None:
            result.completed_at = _now()
        return result


def _is_fixture(lead: dict[str, Any]) -> bool:
    candidate = lead.get("candidate", {})
    return (
        bool(lead.get("demo"))
        or lead.get("data_classification") == "TEST_FIXTURE"
        or candidate.get("dataClassification") == "TEST_FIXTURE"
        or str(lead.get("lead_id", "")).startswith("fictional-")
        or "FICTIONAL" in str(candidate.get("businessName", "")).upper()
    )


def _verified_template_data(lead: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(lead, dict):
        raise ValueError("Build input must be a dictionary.")
    if "research" not in lead and isinstance(lead.get("research_record"), dict):
        nested = lead["research_record"]
        lead = {
            **nested,
            "lead_id": lead.get("lead_id") or nested.get("lead_id"),
            "data_classification": lead.get("data_classification") or nested.get("data_classification"),
            "demo": lead.get("demo") if "demo" in lead else nested.get("demo"),
        }
    research = lead.get("research", {})
    if research.get("qualificationStatus") != "QUALIFIED":
        raise ValueError("Only research records with qualificationStatus QUALIFIED can be built.")
    candidate = lead.get("candidate", {})
    is_fixture = _is_fixture(lead)
    record_sources = [
        u
        for u in [
            *(candidate.get("sourceUrls") or []),
            *(research.get("researchSources") or []),
            *(lead.get("source_urls") or []),
        ]
        if isinstance(u, str) and u.strip()
    ]
    if not is_fixture:
        valid_record_sources = [u for u in record_sources if not is_fixture_or_doc_source(u)]
        fact_sources = [
            u
            for item in research.get("verifiedFacts", [])
            if isinstance(item, dict)
            for u in (item.get("sourceUrls") or [])
            if isinstance(u, str) and u.strip() and not is_fixture_or_doc_source(u)
        ]
        if not valid_record_sources and not fact_sources:
            raise ValueError("Real-business build requires preserved public sourceUrls (test fixtures and documentation cannot serve as evidence).")

    disputed_facts: dict[str, dict[str, Any]] = {}
    withheld_fields: list[str] = []

    def _mark_disputed(field_name: str, values: list[Any], urls: list[str], reason: str) -> None:
        canonical = {"instagramUrl": "instagram_url", "facebookUrl": "facebook_url"}.get(field_name, field_name)
        existing = disputed_facts.get(canonical, {"field": canonical, "values": [], "sourceUrls": [], "reason": reason})
        for v in values:
            if v is not None and v != "" and v not in existing["values"]:
                existing["values"].append(v)
        for u in urls:
            if isinstance(u, str) and u.strip() and u not in existing["sourceUrls"]:
                existing["sourceUrls"].append(u)
        disputed_facts[canonical] = existing
        if canonical not in withheld_fields:
            withheld_fields.append(canonical)

    for c_item in [*(research.get("conflictingFacts") or []), *(research.get("disputedFacts") or [])]:
        if isinstance(c_item, dict) and c_item.get("field"):
            vals = list(c_item.get("values") or ([c_item["value"]] if c_item.get("value") is not None else []))
            _mark_disputed(
                str(c_item["field"]),
                vals,
                list(c_item.get("sourceUrls") or record_sources),
                str(c_item.get("reason") or "Conflicting or disputed public values; withheld until verified."),
            )

    for e_item in research.get("evidenceRecords") or []:
        if not isinstance(e_item, dict) or not e_item.get("field"):
            continue
        v_status = e_item.get("verification_status") or e_item.get("verificationStatus")
        e_tier = e_item.get("evidence_tier") or e_item.get("evidenceTier")
        if v_status in {"CONFLICTING", "DISPUTED", "UNVERIFIED", "WITHHELD", "STALE"} or e_tier == "CONFLICTING_OR_UNVERIFIABLE":
            vals = list(e_item.get("conflicts") or ([e_item["value"]] if e_item.get("value") is not None else []))
            _mark_disputed(
                str(e_item["field"]),
                vals,
                list(e_item.get("sourceUrls") or []),
                str(e_item.get("uncertainty") or "Evidence record is conflicting or unverified."),
            )

    facts: dict[str, Any] = {}
    fact_provenance: dict[str, dict[str, Any]] = {}
    seen_scalar_values: dict[str, tuple[Any, list[str]]] = {}

    def _check_scalar_conflict(sub_field: str, val: Any, urls: list[str]) -> bool:
        if val in (None, "", [], {}) or isinstance(val, (dict, list)):
            return False
        canonical = {"instagramUrl": "instagram_url", "facebookUrl": "facebook_url"}.get(sub_field, sub_field)
        if canonical in disputed_facts:
            _mark_disputed(canonical, [val], urls, disputed_facts[canonical]["reason"])
            return True
        norm_val = _text(val) if isinstance(val, str) else val
        if canonical in seen_scalar_values:
            prev_val, prev_urls = seen_scalar_values[canonical]
            if prev_val != norm_val:
                _mark_disputed(
                    canonical,
                    [prev_val, norm_val],
                    [*prev_urls, *urls],
                    f"Conflicting values for '{canonical}' across research facts; withheld until verified.",
                )
                facts.pop(canonical, None)
                fact_provenance.pop(canonical, None)
                return True
        else:
            seen_scalar_values[canonical] = (norm_val, list(urls))
        return False

    for item in research.get("verifiedFacts", []):
        if not isinstance(item, dict) or not item.get("field"):
            continue
        f_name = str(item["field"])
        f_val = item.get("value")
        v_status = item.get("verificationStatus") or item.get("verification_status")
        e_tier = item.get("evidenceTier") or item.get("evidence_tier")
        raw_urls = [u for u in (item.get("sourceUrls") or []) if isinstance(u, str) and u.strip()]
        if not is_fixture:
            if item.get("data_classification") == "TEST_FIXTURE" or e_tier == "TEST_FIXTURE_OR_DOCUMENTATION" or v_status == "SYNTHETIC_FIXTURE":
                _mark_disputed(f_name, [f_val], raw_urls, "Rejected test-fixture or documentation fact in real-business build.")
                continue
            if raw_urls and all(is_fixture_or_doc_source(u) for u in raw_urls):
                _mark_disputed(f_name, [f_val], raw_urls, "Fact sourceUrls reference only test fixtures or documentation.")
                continue
            effective_urls = [u for u in (raw_urls or record_sources) if not is_fixture_or_doc_source(u)]
            if not effective_urls:
                _mark_disputed(f_name, [f_val], raw_urls, "Fact lacks non-fixture public sourceUrls.")
                continue
        else:
            effective_urls = raw_urls or record_sources

        if v_status in {"CONFLICTING", "DISPUTED", "UNVERIFIED", "WITHHELD", "STALE"} or e_tier == "CONFLICTING_OR_UNVERIFIABLE":
            vals = list(item.get("conflicts") or ([f_val] if f_val is not None else []))
            _mark_disputed(f_name, vals, effective_urls, str(item.get("reason") or "Fact is marked conflicting or unverified."))
            continue

        if f_name in disputed_facts:
            _mark_disputed(f_name, [f_val], effective_urls, disputed_facts[f_name]["reason"])
            continue

        if f_name == "location" and isinstance(f_val, dict):
            loc_copy = dict(f_val)
            for sub in ("address", "city", "country"):
                if sub in loc_copy and _check_scalar_conflict(sub, loc_copy.get(sub), effective_urls):
                    loc_copy.pop(sub, None)
            f_val = loc_copy
        elif f_name == "contact" and isinstance(f_val, dict):
            contact_copy = dict(f_val)
            for sub in ("phone", "email", "whatsapp"):
                if sub in contact_copy and _check_scalar_conflict(sub, contact_copy.get(sub), effective_urls):
                    contact_copy.pop(sub, None)
            f_val = contact_copy
        elif f_name == "social" and isinstance(f_val, dict):
            social_copy = dict(f_val)
            if "instagramUrl" in social_copy and _check_scalar_conflict("instagram_url", social_copy.get("instagramUrl"), effective_urls):
                social_copy.pop("instagramUrl", None)
            if "facebookUrl" in social_copy and _check_scalar_conflict("facebook_url", social_copy.get("facebookUrl"), effective_urls):
                social_copy.pop("facebookUrl", None)
            f_val = social_copy
        else:
            if _check_scalar_conflict(f_name, f_val, effective_urls):
                continue

        facts[f_name] = f_val
        tier = e_tier or classify_evidence_tier(
            source_urls=effective_urls,
            data_classification="TEST_FIXTURE" if is_fixture else "REAL_PUBLIC_DATA",
            excerpt=item.get("excerpt"),
            observed_at=item.get("observed_at") or item.get("observedAt"),
            has_conflict=False,
            verification_status=v_status,
        )
        fact_provenance[f_name] = {
            "sourceUrls": effective_urls,
            "evidence_tier": tier,
            "verification_status": v_status or ("SYNTHETIC_FIXTURE" if is_fixture else ("VERIFIED" if tier == "PRESERVED_PUBLIC_OBSERVATION" else "SUPPORTED_BY_SOURCE_URL")),
            "observed_at": item.get("observed_at") or item.get("observedAt"),
            "excerpt": item.get("excerpt"),
        }

    # Ensure any newly detected conflicts also purge nested location/contact/social subfields
    if isinstance(facts.get("location"), dict) and "address" in disputed_facts:
        facts["location"] = {k: v for k, v in facts["location"].items() if k != "address"}
    if isinstance(facts.get("contact"), dict):
        facts["contact"] = {k: v for k, v in facts["contact"].items() if k not in disputed_facts}
    if isinstance(facts.get("social"), dict):
        facts["social"] = {
            k: v
            for k, v in facts["social"].items()
            if ({"instagramUrl": "instagram_url", "facebookUrl": "facebook_url"}.get(k, k)) not in disputed_facts
        }

    name = (facts.get("businessName") or candidate.get("businessName")) if is_fixture else facts.get("businessName")
    location = (facts.get("location") or candidate.get("location", {})) if is_fixture else (facts.get("location") if isinstance(facts.get("location"), dict) else {})
    if not is_fixture and "city" in disputed_facts:
        location = {k: v for k, v in location.items() if k != "city"}
    category = _category(facts.get("category") or facts.get("niche") or candidate.get("niche") or lead.get("category"))
    if not name or not location.get("city") or not category:
        raise ValueError("Qualified record lacks verified business name, city, or supported category.")
    contact = (facts.get("contact") or candidate.get("contact", {})) if is_fixture else (facts.get("contact") if isinstance(facts.get("contact"), dict) else {})
    social = (facts.get("social") or candidate.get("social", {})) if is_fixture else (facts.get("social") if isinstance(facts.get("social"), dict) else {})
    info = candidate.get("publicBusinessInformation", {}) if is_fixture else {}
    service_values = (facts.get("services") or info.get("servicesOrProducts") or []) if is_fixture else (facts.get("services") if isinstance(facts.get("services"), list) else [])
    services = [
        {
            "name": value.get("name") if isinstance(value, dict) else str(value),
            "description": value.get("description", "") if isinstance(value, dict) else "",
            **({"price": value["price"]} if isinstance(value, dict) and value.get("price") and is_fixture else {}),
        }
        for value in service_values
        if (value.get("name") if isinstance(value, dict) else value)
    ]
    assets = lead.get("approved_assets", [])
    images = [{"url": asset["url"], "alt": asset["alt"]} for asset in assets if asset.get("approved") is True and asset.get("sourceUrls") and asset.get("url") and asset.get("alt")]
    if "menu_items" in facts and isinstance(facts["menu_items"], list):
        menu_items = [
            {
                "name": item.get("name") if isinstance(item, dict) else str(item),
                "description": item.get("description", "") if isinstance(item, dict) else "",
                **({"price": item["price"]} if isinstance(item, dict) and item.get("price") else {}),
            }
            for item in facts["menu_items"]
            if (item.get("name") if isinstance(item, dict) else item)
        ]
    elif is_fixture:
        menu_items = services
    else:
        menu_items = []
    email = None if "email" in disputed_facts else (facts.get("email") or contact.get("email"))
    phone = None if "phone" in disputed_facts else (facts.get("phone") or contact.get("phone"))
    whatsapp = None if "whatsapp" in disputed_facts else (facts.get("whatsapp") or contact.get("whatsapp"))
    instagram = None if "instagram_url" in disputed_facts else (facts.get("instagram_url") or social.get("instagramUrl"))
    facebook = None if "facebook_url" in disputed_facts else (facts.get("facebook_url") or social.get("facebookUrl"))
    address = None if "address" in disputed_facts else (facts.get("address") or location.get("address"))
    opening_hours = None if "openingHours" in disputed_facts else ((facts.get("openingHours") or info.get("openingHours")) if is_fixture else facts.get("openingHours"))
    description = None if "description" in disputed_facts else ((facts.get("description") or info.get("description")) if is_fixture else facts.get("description"))
    return {
        "business_name": name,
        "category": category,
        "city": location["city"],
        "address": address,
        "description": description,
        "phone": phone,
        "email": email,
        "whatsapp": whatsapp,
        "instagram_url": instagram,
        "facebook_url": facebook,
        "opening_hours": opening_hours,
        "services": services,
        "menu_items": menu_items,
        "images": images,
        "brand": _approved_brand(lead),
        "data_classification": "TEST_FIXTURE" if is_fixture else (lead.get("data_classification") or candidate.get("dataClassification") or "REAL_PUBLIC_DATA"),
        "demo": bool(lead.get("demo")),
        "fact_provenance": fact_provenance,
        "disputed_facts": disputed_facts,
        "withheld_fields": withheld_fields,
    }


def _approved_brand(lead: dict[str, Any]) -> dict[str, Any]:
    brand = lead.get("approved_brand", {})
    return brand.get("values", {}) if brand.get("sourceUrls") else {}


def _assert_content_grounded(content: dict[str, Any], facts: dict[str, Any]) -> None:
    allowed = {facts["business_name"], facts.get("description"), facts.get("city"), *[item["name"] for item in facts.get("services", [])]}
    for key in ("hero_title", "hero_subtitle", "about_text"):
        value = content.get(key)
        if value and key != "hero_title" and not any(str(item) in value for item in allowed if item):
            raise ValueError(f"Generated {key} is not grounded in verified data.")
    if content.get("hero_title") != facts["business_name"]:
        raise ValueError("Generated hero title must match the verified business name.")


def _category(value: Any) -> str | None:
    normalized = _text(value).casefold().replace(" ", "_") if _text(value) else None
    aliases = {
        "beauty_salons": "beauty_salon",
        "beauty_salon": "beauty_salon",
        "salon": "beauty_salon",
        "salons": "beauty_salon",
        "restaurant": "restaurant",
        "restaurants": "restaurant",
        "cafe": "restaurant",
        "cafes": "restaurant",
        "food_business": "food_business",
        "professional_services": "professional_services",
        "professional_service": "professional_service",
        "consulting": "professional_service",
        "clinic": "clinic",
        "clinics": "clinic",
        "healthcare": "healthcare",
        "medical_practice": "medical_practice",
        "medical": "medical_practice",
        "dental_clinic": "dental_clinic",
        "dentist": "dental_clinic",
        "dental": "dental_clinic",
        "optometry": "optometry",
        "wellness_center": "wellness_center",
        "wellness": "wellness_center",
        "pharmacy": "pharmacy",
        "legal": "legal",
        "law_firm": "law_firm",
        "lawyer": "legal",
        "law": "legal",
        "solicitor": "solicitor",
        "barrister": "barrister",
        "advocate": "advocate",
        "accounting": "accounting",
        "tax_advisory": "tax_advisory",
        "consulting_firm": "consulting_firm",
        "automotive": "automotive",
        "auto_repair": "auto_repair",
        "mechanic": "mechanic",
        "car_care": "car_care",
        "tire_service": "tire_service",
        "auto_body": "auto_body",
        "vehicle_service": "vehicle_service",
    }
    return aliases.get(normalized, normalized)


def _safe_segment(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)


def _text(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
