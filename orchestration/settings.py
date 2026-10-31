"""Runtime system and operator settings for cloud and mobile operation.

Persists settings in TrackingStore under automation_controls["system_settings"].
Enables live configuration of AI models, discovery defaults, Telegram bot,
autonomous workers, and preview deployments directly from the mobile dashboard.
"""
from __future__ import annotations

import os
from typing import Any

from .observability import log_event, now_iso

SETTINGS_KEY = "system_settings"
CONTROLS_COLLECTION = "automation_controls"

DEFAULT_SETTINGS: dict[str, Any] = {
    # AI Content Generation
    "ai_provider": "openrouter",
    "openrouter_model": "openrouter/free",
    "nvidia_model": "meta/llama-3.1-70b-instruct",
    "openrouter_api_key": "",
    "nvidia_api_key": "",
    # Discovery Defaults
    "default_city": "Lagos",
    "default_country": "Nigeria",
    "default_category": "Restaurants",
    "default_lead_limit": 5,
    # Telegram Bot & Notifications
    "telegram_bot_token": "",
    "telegram_allowed_user_ids": "",
    "telegram_allowed_chat_ids": "",
    "notifications_enabled": True,
    # Autonomous Worker
    "auto_dispatch": True,
    "worker_poll_interval": 5,
    "stale_job_timeout": 900,
    # Deployment & Hosting
    "vercel_token": "",
    "vercel_team_id": "",
    "vercel_project_name": "ai-website-agent-preview",
    "preview_domain": "https://ai-website-agent-preview.vercel.app",
}

SECRET_KEYS = {
    "openrouter_api_key",
    "nvidia_api_key",
    "telegram_bot_token",
    "vercel_token",
}


def _mask_secret(val: str) -> str:
    if not val:
        return ""
    if len(val) >= 12:
        return f"{val[:4]}...{val[-4:]}"
    return "********"


def get_stored_settings(store: Any) -> dict[str, Any]:
    """Retrieve raw stored settings dict from store without env fallback."""
    if not hasattr(store, "state") or not isinstance(store.state, dict):
        return {}
    collection = store.state.get(CONTROLS_COLLECTION)
    if isinstance(collection, dict):
        settings = collection.get(SETTINGS_KEY)
        if isinstance(settings, dict):
            return dict(settings)
    return {}


def get_runtime_settings(store: Any = None) -> dict[str, Any]:
    """Get active runtime settings: stored values take priority, falling back to os.environ and defaults."""
    stored = get_stored_settings(store) if store is not None else {}
    runtime: dict[str, Any] = dict(DEFAULT_SETTINGS)
    runtime.update(stored)

    # Check environment fallbacks for secrets / models if not set in store
    if not runtime.get("openrouter_api_key"):
        runtime["openrouter_api_key"] = (
            os.environ.get("AI_WEBSITE_AGENT_OPENROUTER_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or ""
        ).strip()

    if not runtime.get("nvidia_api_key"):
        runtime["nvidia_api_key"] = (
            os.environ.get("AI_WEBSITE_AGENT_NVIDIA_API_KEY")
            or os.environ.get("NVIDIA_API_KEY")
            or ""
        ).strip()

    if not runtime.get("telegram_bot_token"):
        runtime["telegram_bot_token"] = (
            os.environ.get("AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
            or ""
        ).strip()

    if not runtime.get("telegram_allowed_user_ids"):
        runtime["telegram_allowed_user_ids"] = os.environ.get(
            "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS", ""
        ).strip()

    if not runtime.get("telegram_allowed_chat_ids"):
        runtime["telegram_allowed_chat_ids"] = os.environ.get(
            "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS", ""
        ).strip()

    if not runtime.get("vercel_token"):
        runtime["vercel_token"] = (
            os.environ.get("VERCEL_TOKEN")
            or os.environ.get("AI_WEBSITE_AGENT_VERCEL_TOKEN")
            or ""
        ).strip()

    return runtime


def get_masked_settings(store: Any = None) -> dict[str, Any]:
    """Get settings safe to send over HTTP to mobile dashboard (secrets masked)."""
    runtime = get_runtime_settings(store)
    masked: dict[str, Any] = {}
    for k, v in runtime.items():
        if k in SECRET_KEYS:
            masked[k] = _mask_secret(str(v)) if v else ""
            masked[f"{k}_configured"] = bool(v)
        else:
            masked[k] = v
    masked["updated_at"] = (
        get_stored_settings(store).get("updated_at")
        if store is not None
        else None
    )
    return masked


def update_settings(
    store: Any, updates: dict[str, Any], *, actor: str = "operator"
) -> dict[str, Any]:
    """Validate and persist updated settings into TrackingStore."""
    if not isinstance(updates, dict):
        raise ValueError("Settings payload must be a JSON object.")

    existing_stored = get_stored_settings(store)
    new_settings = dict(existing_stored)

    # Validations & assignments
    for key, val in updates.items():
        if key not in DEFAULT_SETTINGS:
            continue

        if key in SECRET_KEYS:
            s_val = str(val or "").strip()
            # If user provided a masked value (e.g. 'sk-o...1234') or empty, keep existing
            if "..." in s_val or (not s_val and key in existing_stored):
                continue
            new_settings[key] = s_val

        elif key == "ai_provider":
            provider = str(val or "openrouter").strip().lower()
            if provider not in {"openrouter", "nvidia", "deterministic"}:
                raise ValueError(f"Invalid AI provider '{provider}'. Must be 'openrouter', 'nvidia', or 'deterministic'.")
            new_settings[key] = provider

        elif key in {"default_city", "default_country", "default_category", "openrouter_model", "nvidia_model", "vercel_project_name", "preview_domain", "vercel_team_id"}:
            new_settings[key] = str(val or "").strip()

        elif key == "default_lead_limit":
            try:
                limit = int(val)
                if limit < 1 or limit > 50:
                    raise ValueError
                new_settings[key] = limit
            except (TypeError, ValueError):
                raise ValueError("default_lead_limit must be an integer between 1 and 50.")

        elif key == "worker_poll_interval":
            try:
                interval = max(1, min(60, int(val)))
                new_settings[key] = interval
            except (TypeError, ValueError):
                raise ValueError("worker_poll_interval must be an integer between 1 and 60 seconds.")

        elif key == "stale_job_timeout":
            try:
                timeout = max(60, min(7200, int(val)))
                new_settings[key] = timeout
            except (TypeError, ValueError):
                raise ValueError("stale_job_timeout must be an integer between 60 and 7200 seconds.")

        elif key in {"auto_dispatch", "notifications_enabled"}:
            new_settings[key] = bool(val)

        elif key in {"telegram_allowed_user_ids", "telegram_allowed_chat_ids"}:
            new_settings[key] = str(val or "").strip()

    new_settings["updated_at"] = now_iso()
    new_settings["updated_by"] = str(actor or "operator").strip()

    if hasattr(store, "state") and isinstance(store.state, dict):
        store.state.setdefault(CONTROLS_COLLECTION, {})[SETTINGS_KEY] = new_settings
        if hasattr(store, "_save"):
            store._save()
        elif hasattr(store, "save"):
            store.save()

    log_event(
        severity="INFO",
        operation="settings.update",
        code="SETTINGS_UPDATED",
        actor=actor,
        updated_fields=list(updates.keys()),
    )

    return get_masked_settings(store)
