"""Fact-grounded content generation interfaces and deterministic/model adapters."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from integrations.http import ProviderError, provider_request


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
        subtitle = (
            f"{', '.join(service_names[:3])} in {location}."
            if service_names and location
            else f"Find us in {location}."
            if location
            else "Contact us for current information."
        )
        return {
            "hero_title": facts.get("business_name") or "Local Business",
            "hero_subtitle": subtitle,
            "about_title": "About us" if facts.get("description") else None,
            "about_text": facts.get("description"),
            "cta": {
                "whatsapp": "Chat on WhatsApp" if facts.get("whatsapp") else None,
                "phone": "Call now" if facts.get("phone") else None,
            },
        }


class ModelContentGenerator:
    """Runtime model content generator using approved provider APIs (OpenRouter / NVIDIA).

    Always grounded strictly in verified facts. Falls back to DeterministicContentGenerator
    if no credentials are configured or if the provider call fails.
    """

    name = "model-content-generator"

    def __init__(
        self,
        *,
        request: Any = provider_request,
        fallback: ContentGenerator | None = None,
        production: bool | None = None,
        store: Any = None,
    ) -> None:
        self.request = request
        self.fallback = fallback or DeterministicContentGenerator()
        self.store = store
        if production is None:
            from tracking.database import is_production_mode
            production = is_production_mode()
        self.production = bool(production)

    def generate(self, facts: dict[str, Any], category: str) -> dict[str, Any]:
        business_name = str(facts.get("business_name") or "").strip()
        if not business_name:
            return self.fallback.generate(facts, category)

        try:
            from orchestration.settings import get_runtime_settings
            settings = get_runtime_settings(self.store)
        except Exception:
            settings = {}

        selected_provider = (settings.get("ai_provider") or "").strip().lower()
        if selected_provider == "deterministic":
            return self.fallback.generate(facts, category)

        raw_order = os.environ.get("AI_WEBSITE_AGENT_BUILDER_PROVIDERS", "openrouter,nvidia")
        order = [x.strip().lower() for x in raw_order.split(",") if x.strip()]
        if selected_provider in {"openrouter", "nvidia"}:
            order = [selected_provider] + [p for p in order if p != selected_provider]

        for provider in order:
            try:
                content = self._call_provider(provider, facts, category, settings=settings)
                if content:
                    return content
            except (ProviderError, ValueError, Exception):
                continue

        # Fallback if no provider succeeded
        return self.fallback.generate(facts, category)

    def _call_provider(
        self,
        provider: str,
        facts: dict[str, Any],
        category: str,
        *,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        business_name = facts.get("business_name", "")
        city = facts.get("city", "")
        description = facts.get("description", "")
        services = [s.get("name") for s in facts.get("services", []) if isinstance(s, dict) and s.get("name")]

        fact_lines = [
            f"Business Name: {business_name}",
            f"Category: {category}",
        ]
        if city:
            fact_lines.append(f"Location: {city}")
        if description:
            fact_lines.append(f"Description: {description}")
        if services:
            fact_lines.append(f"Services: {', '.join(services)}")
        if facts.get("whatsapp"):
            fact_lines.append(f"WhatsApp available: yes")
        if facts.get("phone"):
            fact_lines.append(f"Phone available: yes")

        cfg = settings or {}

        if provider == "openrouter":
            key = (
                cfg.get("openrouter_api_key")
                or os.environ.get("AI_WEBSITE_AGENT_OPENROUTER_API_KEY")
                or os.environ.get("OPENROUTER_API_KEY")
                or ""
            ).strip()
            if not key:
                raise ValueError("OpenRouter API key missing")
            model = (
                cfg.get("openrouter_model")
                or os.environ.get("AI_WEBSITE_AGENT_OPENROUTER_MODEL")
                or "openrouter/free"
            ).strip()
            endpoint = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": os.environ.get("AI_WEBSITE_AGENT_SITE_URL", "https://josephportifolio.vercel.app"),
                "X-Title": "AI Website Agent",
            }
        elif provider == "nvidia":
            key = (
                cfg.get("nvidia_api_key")
                or os.environ.get("AI_WEBSITE_AGENT_NVIDIA_API_KEY")
                or os.environ.get("NVIDIA_API_KEY")
                or ""
            ).strip()
            model = (
                cfg.get("nvidia_model")
                or os.environ.get("AI_WEBSITE_AGENT_NVIDIA_MODEL")
                or "meta/llama-3.1-70b-instruct"
            ).strip()
            if not key or not model:
                raise ValueError("NVIDIA key/model missing")
            if self.production and os.environ.get("AI_WEBSITE_AGENT_NVIDIA_PRODUCTION_LICENSE_CONFIRMED") != "1":
                raise ValueError("NVIDIA developer API license unconfirmed in production")
            endpoint = (
                os.environ.get("AI_WEBSITE_AGENT_NVIDIA_CHAT_COMPLETIONS_URL")
                or "https://integrate.api.nvidia.com/v1/chat/completions"
            ).strip()
            parts = urlsplit(endpoint)
            if parts.scheme != "https" or parts.hostname != "integrate.api.nvidia.com":
                raise ValueError("Invalid NVIDIA endpoint")
            headers = {"Authorization": f"Bearer {key}"}
        else:
            raise ValueError(f"Unknown provider {provider}")

        prompt = (
            "Generate structured JSON for a business website hero and about section. "
            "You MUST ONLY use the provided facts. NEVER invent or fabricate facts, claims, years of experience, or awards.\n"
            "Return JSON matching:\n"
            '{\n  "hero_title": "string",\n  "hero_subtitle": "string",\n  "about_title": "string",\n  "about_text": "string"\n}\n\n'
            + "\n".join(fact_lines)
        )

        response = self.request(
            endpoint,
            method="POST",
            json_body={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a professional web copywriter. Output only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 400,
                "response_format": {"type": "json_object"},
            },
            headers={**headers, "Content-Type": "application/json"},
            timeout=25.0,
            max_bytes=80000,
        )

        payload = response.get("json") if isinstance(response, dict) else None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        parsed = json.loads(message.get("content") or "{}") if isinstance(message, dict) else {}

        hero_title = str(parsed.get("hero_title") or business_name).strip()
        hero_subtitle = str(parsed.get("hero_subtitle") or "").strip()
        about_title = str(parsed.get("about_title") or "About Us").strip()
        about_text = str(parsed.get("about_text") or description).strip()

        # Check for banned claims
        for bad_claim in ("award-winning", "guaranteed", "number one", "#1", "certified"):
            if bad_claim in hero_subtitle.lower() or bad_claim in about_text.lower():
                raise ValueError(f"Model generated unverified claim '{bad_claim}'")

        return {
            "hero_title": hero_title,
            "hero_subtitle": hero_subtitle or (f"Welcome to {business_name} in {city}." if city else f"Welcome to {business_name}."),
            "about_title": about_title if about_text else None,
            "about_text": about_text or None,
            "cta": {
                "whatsapp": "Chat on WhatsApp" if facts.get("whatsapp") else None,
                "phone": "Call now" if facts.get("phone") else None,
            },
            "metadata": {
                "provider": provider,
                "model": model,
                "generator": "ModelContentGenerator",
            },
        }
