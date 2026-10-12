"""Fact-grounded content generation interfaces and deterministic fallback."""

from __future__ import annotations

from typing import Any, Protocol


class ContentGenerator(Protocol):
    """Return content structured for the template, grounded only in supplied facts."""

    name: str

    def generate(self, facts: dict[str, Any], category: str) -> dict[str, Any]: ...


class DeterministicContentGenerator:
    """No-provider fallback; it rearranges verified facts and makes no business claims."""

    name = "deterministic-fallback"

    def generate(self, facts: dict[str, Any], category: str) -> dict[str, Any]:
        services = facts.get("services", [])
        location = facts.get("city")
        service_names = [item.get("name") for item in services if item.get("name")]
        subtitle = f"{', '.join(service_names[:3])} in {location}." if service_names and location else f"Find us in {location}." if location else "Contact us for current information."
        return {
            "hero_title": facts["business_name"],
            "hero_subtitle": subtitle,
            "about_title": "About us" if facts.get("description") else None,
            "about_text": facts.get("description"),
            "cta": {
                "whatsapp": "Chat on WhatsApp" if facts.get("whatsapp") else None,
                "phone": "Call now" if facts.get("phone") else None,
            },
        }
