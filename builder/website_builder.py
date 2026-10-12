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
        while (target_dir / build_id).exists():
            build_id = f"{base_build_id}-{counter}"
            counter += 1
        result = BuildResult(build_id, lead_id, None, "ANALYZING", started_at)
        try:
            facts = _verified_template_data(lead)
            template = select_template(facts["category"])
            result.template_id = template.template_id
            result.status = "GENERATING_CONTENT"
            content = self.content_generator.generate(facts, facts["category"])
            _assert_content_grounded(content, facts)
            result.generated_content = content
            facts["content"] = content
            result.status = "BUILDING"
            html = render_site(facts, template.template_id)
            result.status = "VALIDATING"
            validation = validate_html(html, facts["business_name"], real_build=not bool(lead.get("demo")), build_id=build_id, lead_id=lead_id, expected_contact={"phone": facts.get("phone"), "whatsapp": facts.get("whatsapp")}, synthetic_contact=facts.get("data_classification") == "TEST_FIXTURE" or bool(facts.get("demo")))
            result.validation = validation
            if not validation["passed"]:
                result.status = "NEEDS_REVIEW"
                result.errors.extend(validation["errors"])
            else:
                artifact_dir = self.output_root / _safe_segment(lead_id) / build_id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                (artifact_dir / "index.html").write_text(html, encoding="utf-8")
                result.output_location = str(artifact_dir / "index.html")
                result.status = "COMPLETED"
                result.completed_at = _now()
                (artifact_dir / "build.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        except Exception as error:
            result.status = "FAILED"
            result.errors.append(str(error))
        if result.completed_at is None:
            result.completed_at = _now()
        return result


def _verified_template_data(lead: dict[str, Any]) -> dict[str, Any]:
    research = lead.get("research", {})
    if research.get("qualificationStatus") != "QUALIFIED":
        raise ValueError("Only research records with qualificationStatus QUALIFIED can be built.")
    candidate = lead.get("candidate", {})
    facts = {item.get("field"): item.get("value") for item in research.get("verifiedFacts", []) if item.get("field")}
    name = facts.get("businessName") or candidate.get("businessName")
    location = facts.get("location") or candidate.get("location", {})
    category = _category(candidate.get("niche") or lead.get("category"))
    if not name or not location.get("city") or not category:
        raise ValueError("Qualified record lacks verified business name, city, or supported category.")
    contact = facts.get("contact") or candidate.get("contact", {})
    social = facts.get("social") or candidate.get("social", {})
    info = candidate.get("publicBusinessInformation", {})
    service_values = facts.get("services") or info.get("servicesOrProducts") or []
    services = [{"name": value.get("name") if isinstance(value, dict) else str(value), "description": value.get("description", "") if isinstance(value, dict) else ""} for value in service_values]
    assets = lead.get("approved_assets", [])
    images = [{"url": asset["url"], "alt": asset["alt"]} for asset in assets if asset.get("approved") is True and asset.get("sourceUrls") and asset.get("url") and asset.get("alt")]
    menu_items = facts["menu_items"] if "menu_items" in facts else services
    return {"business_name": name, "category": category, "city": location["city"], "address": facts.get("address") or location.get("address"), "description": facts.get("description") or info.get("description"), "phone": contact.get("phone"), "whatsapp": contact.get("whatsapp"), "instagram_url": social.get("instagramUrl"), "facebook_url": social.get("facebookUrl"), "opening_hours": facts.get("openingHours") or info.get("openingHours"), "services": services, "menu_items": menu_items, "images": images, "brand": _approved_brand(lead), "data_classification": lead.get("data_classification") or candidate.get("dataClassification"), "demo": bool(lead.get("demo"))}


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
    aliases = {"beauty_salons": "beauty_salon", "beauty_salon": "beauty_salon", "restaurant": "restaurant", "food_business": "food_business", "professional_services": "professional_services", "professional_service": "professional_service"}
    return aliases.get(normalized)


def _safe_segment(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)


def _text(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
