"""Data models and channel specifications for social media content generation and buffer integration.

All channel IDs, character limits, and media requirements match the verified live n8n
Buffer Draft Executor workflow (zAd3P31ZrTm9Ax94).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any


class SocialChannel(str, Enum):
    TWITTER = "twitter"
    X = "x"
    LINKEDIN = "linkedin"
    INSTAGRAM = "instagram"


class ContentArchetype(str, Enum):
    TECH_INSIGHT = "tech_insight"
    CASE_STUDY = "case_study"
    OPERATOR_DISPATCH = "operator_dispatch"
    LOCAL_GROWTH = "local_growth"


class DraftStatus(str, Enum):
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BUFFER_DRAFTED = "BUFFER_DRAFTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ChannelSpecification:
    """Channel constraints matching the n8n Buffer Draft Executor allowlist."""
    channel_id: str
    name: str
    service: str
    max_length: int
    requires_media: bool
    max_hashtags: int


#: Verified Buffer channel specifications matching n8n workflow zAd3P31ZrTm9Ax94
VERIFIED_CHANNELS: dict[str, ChannelSpecification] = {
    "twitter": ChannelSpecification(
        channel_id="6aa054f7cd8b9c702c2f1a35",
        name="Bigcherem",
        service="twitter",
        max_length=280,
        requires_media=False,
        max_hashtags=2,
    ),
    "x": ChannelSpecification(
        channel_id="6aa054f7cd8b9c702c2f1a35",
        name="Bigcherem",
        service="twitter",
        max_length=280,
        requires_media=False,
        max_hashtags=2,
    ),
    "linkedin": ChannelSpecification(
        channel_id="6a5f45a7e2638b94d7a8bc3e",
        name="Joseph Unaogu",
        service="linkedin",
        max_length=3000,
        requires_media=False,
        max_hashtags=5,
    ),
    "instagram": ChannelSpecification(
        channel_id="6acf807e778b240e2f07ec61",
        name="unaogujoseph",
        service="instagram",
        max_length=2200,
        requires_media=True,
        max_hashtags=10,
    ),
}


def normalize_channel(raw: str) -> str:
    cleaned = (raw or "").strip().lower()
    if cleaned in {"x", "twitter"}:
        return "twitter"
    if cleaned in VERIFIED_CHANNELS:
        return cleaned
    raise ValueError(f"Unsupported social channel '{raw}'. Supported: {sorted(list(VERIFIED_CHANNELS.keys()))}")


@dataclass
class SocialDraft:
    """Represents a generated social media draft post."""
    id: str
    channel: str
    topic: str
    caption: str
    archetype: str
    media_url: str | None = None
    media_prompt: str | None = None
    hashtags: list[str] = field(default_factory=list)
    status: str = DraftStatus.DRAFT.value
    brand_voice_score: float = 1.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    operator_note: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SocialDraft:
        clean_data = dict(data)
        return cls(**clean_data)

    def content_fingerprint(self) -> str:
        payload = f"{self.channel}:{self.caption}:{self.media_url or ''}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_buffer_approval_payload(self) -> dict[str, Any]:
        """Convert approved draft into the exact action_approvals row consumed by n8n zAd3P31ZrTm9Ax94."""
        spec = VERIFIED_CHANNELS[normalize_channel(self.channel)]
        return {
            "action_type": "buffer_publish",
            "status": "approved" if self.status == DraftStatus.APPROVED.value else "pending",
            "proposed_action": f"Buffer post to {spec.name} ({spec.service})",
            "prepared_content": self.caption,
            "metadata": {
                "service": spec.service,
                "channel_id": spec.channel_id,
                "media_url": self.media_url,
                "saveToDraft": True,
                "execution_authorized": "true" if self.status == DraftStatus.APPROVED.value else "false",
                "is_synthetic_test": "false",
                "draft_id": self.id,
                "archetype": self.archetype,
                "brand_voice_score": self.brand_voice_score,
                "fingerprint": self.content_fingerprint(),
            },
        }
