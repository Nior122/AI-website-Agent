"""Deterministic and LLM-backed social media post generation engine.

Produces channel-tailored drafts respecting Nior brand voice, character limits,
and structural constraints for Twitter/X, LinkedIn, and Instagram.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from .brand import score_brand_voice, validate_brand_voice
from .models import (
    ContentArchetype,
    DraftStatus,
    SocialChannel,
    SocialDraft,
    VERIFIED_CHANNELS,
    normalize_channel,
)

#: Standard hashtags curated for Nior brand themes
CURATED_HASHTAGS: dict[str, list[str]] = {
    "tech_insight": ["#WebDev", "#Engineering", "#SoftwareArchitecture", "#Performance"],
    "case_study": ["#WebDesign", "#SmallBusiness", "#DigitalPresence", "#Conversion"],
    "operator_dispatch": ["#SystemsThinking", "#Automation", "#SiteReliability", "#DevOps"],
    "local_growth": ["#LocalBusiness", "#SmallBizTips", "#WebStrategy", "#Growth"],
}


class SocialPostGenerator:
    """Generates social media post drafts using deterministic templates or LLM adapters."""

    def __init__(self, *, default_author: str = "Nior Team") -> None:
        self.default_author = default_author

    def generate(
        self,
        topic: str,
        *,
        channel: str = "twitter",
        archetype: str = "tech_insight",
        media_url: str | None = None,
        extra_context: str | None = None,
        custom_id: str | None = None,
    ) -> SocialDraft:
        """Generate a validated SocialDraft for the given channel and topic."""
        norm_ch = normalize_channel(channel)
        if archetype not in [a.value for a in ContentArchetype]:
            archetype = ContentArchetype.TECH_INSIGHT.value

        caption, prompt, tags = self._render_content(
            topic=topic,
            channel=norm_ch,
            archetype=archetype,
            media_url=media_url,
            extra_context=extra_context,
        )

        # Generate a stable or random draft ID
        draft_id = custom_id or self._generate_draft_id(norm_ch, topic)
        score = score_brand_voice(caption, channel=norm_ch, media_url=media_url)

        return SocialDraft(
            id=draft_id,
            channel=norm_ch,
            topic=topic.strip(),
            caption=caption,
            archetype=archetype,
            media_url=media_url,
            media_prompt=prompt,
            hashtags=tags,
            status=DraftStatus.DRAFT.value,
            brand_voice_score=score,
            metadata={
                "generated_by": "deterministic_v1",
                "character_count": len(caption),
                "author": self.default_author,
            },
        )

    def _generate_draft_id(self, channel: str, topic: str) -> str:
        h = hashlib.sha256(f"{channel}:{topic}".encode("utf-8")).hexdigest()[:8]
        return f"soc-{channel[:2]}-{h}"

    def _render_content(
        self,
        *,
        topic: str,
        channel: str,
        archetype: str,
        media_url: str | None,
        extra_context: str | None,
    ) -> tuple[str, str | None, list[str]]:
        """Render channel-adapted copy, media prompt, and appropriate hashtags."""
        clean_topic = topic.strip()
        tags_pool = CURATED_HASHTAGS.get(archetype, ["#WebDevelopment", "#Tech"])
        spec = VERIFIED_CHANNELS[channel]
        selected_tags = tags_pool[: spec.max_hashtags]

        if channel in ("twitter", "x"):
            caption = self._build_twitter_post(clean_topic, archetype, selected_tags)
            prompt = None
        elif channel == "linkedin":
            caption = self._build_linkedin_post(clean_topic, archetype, selected_tags, extra_context)
            prompt = None
        elif channel == "instagram":
            caption, prompt = self._build_instagram_post(clean_topic, archetype, selected_tags)
        else:
            caption = f"{clean_topic}\n\n{' '.join(selected_tags)}"
            prompt = None

        return caption.strip(), prompt, selected_tags

    def _build_twitter_post(self, topic: str, archetype: str, tags: list[str]) -> str:
        tag_str = " ".join(tags)
        if archetype == ContentArchetype.TECH_INSIGHT.value:
            core = f"Most websites fail on core fundamentals: speed, mobile responsiveness, and clean semantic structure.\n\nHere is how to optimize {topic}: prioritize readable layouts, lightweight assets, and zero layout shifts."
        elif archetype == ContentArchetype.CASE_STUDY.value:
            core = f"Before/After breakdown on {topic}:\n\nA modern responsive site with clear call-to-actions consistently outperforms outdated directory listings. Simplicity wins every time."
        elif archetype == ContentArchetype.OPERATOR_DISPATCH.value:
            core = f"Ops Dispatch on {topic}:\n\nAutomation without deterministic safety gates is just faster failure. Build fail-closed verifications first, deploy second."
        else:
            core = f"Practical advice for local businesses tackling {topic}:\n\nYour website is your 24/7 digital storefront. Keep hours, location, and key services immediately visible."

        # Ensure within 280 character limit
        full = f"{core}\n\n{tag_str}"
        if len(full) > 280:
            allowed_len = 280 - len(tag_str) - 4
            core = core[:allowed_len].rstrip() + "..."
            full = f"{core}\n\n{tag_str}"
        return full

    def _build_linkedin_post(
        self, topic: str, archetype: str, tags: list[str], extra_context: str | None
    ) -> str:
        tag_str = " ".join(tags)
        context_line = f"\nContext: {extra_context.strip()}\n" if extra_context else ""

        if archetype == ContentArchetype.TECH_INSIGHT.value:
            return (
                f"Engineering a resilient digital presence requires focusing on architecture over aesthetic fads.\n\n"
                f"When addressing {topic}, three principles make the difference:\n"
                f"1. Performance First: Shaving milliseconds off load times directly impacts retention and search ranking.\n"
                f"2. Semantic & Accessible: Clean HTML structure ensures universal accessibility and reliable indexing.\n"
                f"3. Resilient Deployment: Continuous validation gates protect against regressions before users notice.\n"
                f"{context_line}\n"
                f"What is your highest-priority performance benchmark this quarter?\n\n"
                f"{tag_str}"
            )
        elif archetype == ContentArchetype.CASE_STUDY.value:
            return (
                f"Case Study: Rebuilding digital visibility for {topic}.\n\n"
                f"Many established businesses rely on outdated web pages or social profiles alone. The risks are clear:\n"
                f"- Fragmented customer discovery across unverified platforms.\n"
                f"- Poor mobile conversion due to slow, unoptimized interfaces.\n"
                f"- Lack of brand ownership and verified contact provenance.\n\n"
                f"By deploying a grounded, responsive site with clear service offerings and fast load times, businesses establish credible authority.\n"
                f"{context_line}\n"
                f"How does your current website represent your business quality today?\n\n"
                f"{tag_str}"
            )
        elif archetype == ContentArchetype.OPERATOR_DISPATCH.value:
            return (
                f"Operator Dispatch: System Reliability & Guarded Execution in {topic}.\n\n"
                f"In production automation, autonomy without verification is a liability. Our operational principles:\n"
                f"• Strict Idempotency: Every action carries a verifiable deduplication key.\n"
                f"• Human-in-the-Loop Gating: Drafts, proposals, and dispatches require explicit approval.\n"
                f"• Observable Audit Trails: Every state transition is recorded durably with timestamps and provenance.\n"
                f"{context_line}\n"
                f"Build safety into your foundational architecture, not as an afterthought.\n\n"
                f"{tag_str}"
            )
        else:
            return (
                f"Strategic Guide for Local Business Leaders: Navigating {topic}.\n\n"
                f"Customers make decisions in seconds when searching online. Three essentials every modern website must provide:\n"
                f"1. Immediate clarity on services, location, and verified hours.\n"
                f"2. Seamless, fast mobile browsing without clutter or intrusive popups.\n"
                f"3. Direct, frictionless contact methods (call, email, location map).\n"
                f"{context_line}\n"
                f"Make it effortless for your clients to choose you.\n\n"
                f"{tag_str}"
            )

    def _build_instagram_post(
        self, topic: str, archetype: str, tags: list[str]
    ) -> tuple[str, str]:
        tag_str = " ".join(tags)
        prompt = (
            f"Minimalist, modern editorial photograph representing {topic}. Clean lighting, architectural workspace, "
            f"sharp design aesthetics, high resolution, professional color grading."
        )

        caption = (
            f"Designing clarity into the digital experience: {topic}.\n\n"
            f"A high-performing website is not about decoration—it is about clear communication, verified trust, and frictionless interaction.\n\n"
            f"Swipe through for key architectural insights.\n\n"
            f"—\n"
            f"{tag_str}"
        )
        return caption, prompt
