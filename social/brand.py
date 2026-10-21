"""Nior Brand Voice guidelines, content quality checks, and tone validation.

Core Brand Voice Principles:
1. Pragmatic & Grounded: Real engineering and practical business facts over speculation.
2. Value-First: Every post offers a concrete takeaway, tip, framework, or lesson.
3. Zero Hype: No exaggerated claims, breathless buzzwords, or hustle-culture tropes.
4. Channel-Native: Concise punchiness for Twitter/X; structured clarity for LinkedIn;
   visual narrative with required imagery for Instagram.
"""
from __future__ import annotations

import re
from typing import Any

from .models import VERIFIED_CHANNELS, normalize_channel

#: Terms strictly prohibited across all Nior content
BANNED_BUZZWORDS = frozenset([
    "synergy",
    "rockstar",
    "ninja",
    "10x",
    "revolutionary",
    "game-changer",
    "game changer",
    "paradigm shift",
    "disrupting",
    "disruption",
    "guaranteed riches",
    "crypto moon",
    "insane results",
    "hustle 24/7",
    "crushing it",
    "unlock your potential",
    "magic bullet",
    "secret sauce",
    "growth hacking",
])

ALLOWED_UPPERCASE_ACRONYMS = frozenset([
    "API", "SEO", "HTML", "CSS", "URL", "HTTP", "HTTPS", "JSON", "SaaS",
    "SQL", "CLI", "SDK", "MCP", "REST", "DNS", "LLM", "AWS", "UI", "UX",
    "CTO", "CEO", "ROI", "B2B", "B2C", "SMS", "RFC", "RFC822",
])


def validate_brand_voice(text: str, *, channel: str = "twitter") -> tuple[bool, list[str]]:
    """Validate that text adheres to Nior brand voice standards.

    Returns (is_valid, list_of_violations).
    """
    violations: list[str] = []
    if not text or not text.strip():
        return False, ["Content is empty"]

    cleaned = text.strip()
    lowered = cleaned.lower()

    # 1. Check for banned buzzwords
    for buzzword in BANNED_BUZZWORDS:
        pattern = r"\b" + re.escape(buzzword) + r"\b"
        if re.search(pattern, lowered):
            violations.append(f"Prohibited buzzword or hype phrase: '{buzzword}'")

    # 2. Check for excessive exclamation marks
    if re.search(r"!{2,}", cleaned):
        violations.append("Excessive exclamation points (use maximum one at a time)")
    if cleaned.count("!") > 3:
        violations.append("Too many exclamation points overall (maximum 3 per post)")

    # 3. Check for ALL CAPS yelling
    words = re.findall(r"\b[A-Z]{3,}\b", cleaned)
    for w in words:
        if w not in ALLOWED_UPPERCASE_ACRONYMS:
            violations.append(f"Excessive capitalization/yelling: '{w}'")

    # 4. Check channel-specific length and requirements
    try:
        norm_ch = normalize_channel(channel)
        spec = VERIFIED_CHANNELS[norm_ch]
        if len(cleaned) > spec.max_length:
            violations.append(
                f"Content length ({len(cleaned)}) exceeds {norm_ch} limit ({spec.max_length})"
            )

        hashtags = re.findall(r"#\w+", cleaned)
        if len(hashtags) > spec.max_hashtags:
            violations.append(
                f"Hashtag count ({len(hashtags)}) exceeds {norm_ch} guideline (maximum {spec.max_hashtags})"
            )
    except ValueError as exc:
        violations.append(str(exc))

    return len(violations) == 0, violations


def score_brand_voice(text: str, *, channel: str = "twitter", media_url: str | None = None) -> float:
    """Calculate a 0.0 to 1.0 brand voice alignment score."""
    if not text or not text.strip():
        return 0.0

    score = 1.0
    is_valid, violations = validate_brand_voice(text, channel=channel)
    # Deduct 0.2 per brand violation
    score -= len(violations) * 0.2

    norm_ch = normalize_channel(channel)
    spec = VERIFIED_CHANNELS[norm_ch]

    # Instagram media check
    if spec.requires_media:
        if not media_url or not (media_url.startswith("http://") or media_url.startswith("https://")):
            score -= 0.3

    # Check structure (prefer clear paragraphs and bulleted structure for LinkedIn)
    if norm_ch == "linkedin" and "\n" not in text:
        score -= 0.15

    # Check minimum length (too short lacks substance)
    if norm_ch == "linkedin" and len(text) < 120:
        score -= 0.2
    elif norm_ch == "twitter" and len(text) < 30:
        score -= 0.2

    return max(0.0, min(1.0, round(score, 2)))
