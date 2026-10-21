"""Social Media Draft and Campaign Management Service.

Provides lifecycle management (DRAFT -> PROPOSED -> APPROVED -> EXECUTED_DRAFT),
persisted locally in JSON or integrated with TrackingStore / n8n Action Approvals.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .brand import score_brand_voice, validate_brand_voice
from .generator import SocialPostGenerator
from .models import (
    DraftStatus,
    SocialDraft,
    VERIFIED_CHANNELS,
    normalize_channel,
)

DEFAULT_SOCIAL_DIR = Path("data/social")
DEFAULT_DRAFTS_FILE = DEFAULT_SOCIAL_DIR / "drafts.json"


class SocialServiceError(ValueError):
    """Refusal or domain error in the social service."""

    def __init__(self, message: str, *, code: str = "SOCIAL_SERVICE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class SocialDraftService:
    """Manages the lifecycle, storage, and approval gates for social media drafts."""

    def __init__(
        self,
        *,
        storage_path: Path | str | None = None,
        generator: SocialPostGenerator | None = None,
    ) -> None:
        self.storage_path = Path(storage_path) if storage_path else DEFAULT_DRAFTS_FILE
        self.generator = generator or SocialPostGenerator()
        self._drafts: dict[str, SocialDraft] = {}
        self._load()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        self._drafts[k] = SocialDraft.from_dict(v)
        except Exception:
            # Corrupted file is treated as empty; production should preserve backups
            self._drafts = {}

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = {k: v.to_dict() for k, v in self._drafts.items()}
        self.storage_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")

    def create_draft(
        self,
        topic: str,
        *,
        channel: str = "twitter",
        archetype: str = "tech_insight",
        media_url: str | None = None,
        extra_context: str | None = None,
    ) -> SocialDraft:
        """Generate and save a new draft."""
        draft = self.generator.generate(
            topic=topic,
            channel=channel,
            archetype=archetype,
            media_url=media_url,
            extra_context=extra_context,
        )
        self._drafts[draft.id] = draft
        self._save()
        return draft

    def get_draft(self, draft_id: str) -> SocialDraft | None:
        return self._drafts.get(draft_id)

    def list_drafts(
        self,
        *,
        channel: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[SocialDraft]:
        """List drafts filtered by channel and status."""
        results: list[SocialDraft] = []
        norm_ch = normalize_channel(channel) if channel else None

        for draft in self._drafts.values():
            if norm_ch and draft.channel != norm_ch:
                continue
            if status and draft.status != status:
                continue
            results.append(draft)

        # Sort newest first
        results.sort(key=lambda d: d.created_at, reverse=True)
        return results[:limit]

    def approve_draft(
        self,
        draft_id: str,
        *,
        operator_note: str | None = None,
        force: bool = False,
    ) -> SocialDraft:
        """Approve a draft for scheduling/publishing. Validates brand voice and channel rules."""
        draft = self._drafts.get(draft_id)
        if not draft:
            raise SocialServiceError(f"Draft '{draft_id}' not found.", code="DRAFT_NOT_FOUND")

        if draft.status == DraftStatus.APPROVED.value:
            return draft

        # Gate check: brand voice and channel compliance
        is_valid, violations = validate_brand_voice(draft.caption, channel=draft.channel)
        if not is_valid and not force:
            raise SocialServiceError(
                f"Draft failed brand validation: {'; '.join(violations)}",
                code="BRAND_VALIDATION_FAILED",
            )

        spec = VERIFIED_CHANNELS[draft.channel]
        if spec.requires_media and not draft.media_url and not force:
            raise SocialServiceError(
                f"Channel '{draft.channel}' requires a public media URL before approval.",
                code="MEDIA_URL_REQUIRED",
            )

        draft.status = DraftStatus.APPROVED.value
        draft.operator_note = operator_note
        draft.updated_at = datetime.now(timezone.utc).isoformat()
        draft.metadata["approved_at"] = draft.updated_at
        self._save()
        return draft

    def reject_draft(
        self,
        draft_id: str,
        *,
        reason: str | None = None,
    ) -> SocialDraft:
        """Reject a draft with an optional operator reason."""
        draft = self._drafts.get(draft_id)
        if not draft:
            raise SocialServiceError(f"Draft '{draft_id}' not found.", code="DRAFT_NOT_FOUND")

        draft.status = DraftStatus.REJECTED.value
        draft.operator_note = reason
        draft.updated_at = datetime.now(timezone.utc).isoformat()
        draft.metadata["rejected_at"] = draft.updated_at
        draft.metadata["rejection_reason"] = reason or "Operator rejected"
        self._save()
        return draft

    def update_draft_caption(self, draft_id: str, new_caption: str) -> SocialDraft:
        """Update draft copy and recompute brand voice score."""
        draft = self._drafts.get(draft_id)
        if not draft:
            raise SocialServiceError(f"Draft '{draft_id}' not found.", code="DRAFT_NOT_FOUND")

        if draft.status == DraftStatus.BUFFER_DRAFTED.value:
            raise SocialServiceError("Cannot edit a draft that has already been sent to Buffer.", code="DRAFT_LOCKED")

        draft.caption = new_caption.strip()
        draft.brand_voice_score = score_brand_voice(
            draft.caption, channel=draft.channel, media_url=draft.media_url
        )
        draft.updated_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return draft

    def export_buffer_payload(self, draft_id: str) -> dict[str, Any]:
        """Export action_approvals row payload for n8n Buffer Draft Executor."""
        draft = self._drafts.get(draft_id)
        if not draft:
            raise SocialServiceError(f"Draft '{draft_id}' not found.", code="DRAFT_NOT_FOUND")
        return draft.to_buffer_approval_payload()
