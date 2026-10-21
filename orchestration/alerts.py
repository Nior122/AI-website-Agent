"""Critical-condition alerts for the operator, from the same report every surface uses.

This is deliberately a *hook*, not a notification platform. The system already records what needs
attention (:func:`orchestration.health.operator_attention`), so this module only decides which of
those conditions are critical, gives each a stable id and a dedupe key, and hands it to a sink.

Two sinks are provided:

* :class:`LogAlertSink` - the default. Emits one structured ``CRITICAL`` log event per alert. It has
  no side effects beyond logging, so it is safe to run from cron or a container entrypoint.
* :class:`WebhookAlertSink` - a clean integration point for an external channel. It **never**
  contacts a network on its own: the caller must inject a ``transport(url, payload, timeout)``
  callable. With no transport configured, delivery is reported as
  ``SKIPPED_TRANSPORT_NOT_CONFIGURED`` and nothing is sent. That keeps this repository free of
  provider credentials and real messaging while still exercising the exact code path an operator
  would wire to Slack, email, or PagerDuty.

Nothing here sends messages to leads or customers: alerts describe workflow failures only, and every
message is the sanitized text the monitoring surfaces already show.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from .health import operator_attention
from .observability import log_event, now_iso

#: Recorded conditions considered critical: they either stop revenue work permanently or leave an
#: external side effect (a publish) whose outcome nobody can confirm.
CRITICAL_JOB_CODES = {
    "PUBLISH_STATE_UNCERTAIN",
    "PUBLISH_UNCERTAIN",
    "DEPLOYMENT_RECORD_FAILED",
    "RETRY_LIMIT_REACHED",
    "HEARTBEAT_EXPIRED",
    "STORE_CANNOT_AUTHORIZE",
}
CRITICAL_KINDS = {"stale_job", "deployment"}
DEFAULT_DEDUPE_WINDOW = 3600


@runtime_checkable
class AlertSink(Protocol):
    """Anything that can accept one already-sanitized alert dictionary."""

    def publish(self, alert: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - interface
        ...


def _dedupe_key(alert: dict[str, Any]) -> str:
    identity = {
        "kind": alert.get("kind"),
        "code": alert.get("code"),
        "job_id": alert.get("job_id"),
        "project_id": alert.get("project_id"),
        "recorded_at": alert.get("recorded_at"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def critical_alerts(store, *, repository_root: Path | str = ".", now: Any = None,
                    limit: int = 20) -> list[dict[str, Any]]:
    """Alerts for conditions that will not resolve themselves, derived from recorded state."""
    report = operator_attention(store, now=now, repository_root=repository_root, limit=100)
    alerts: list[dict[str, Any]] = []
    for item in report["items"]:
        kind = str(item.get("kind") or "")
        code = str(item.get("code") or "")
        critical = kind in CRITICAL_KINDS or code in CRITICAL_JOB_CODES or (
            kind == "job" and item.get("retry_exhausted") and item.get("status") == "FAILED"
        )
        if not critical:
            continue
        alert = {
            "severity": "CRITICAL",
            "kind": kind,
            "code": code,
            "title": _title_for(kind, code),
            "message": item.get("message"),
            "job_id": item.get("job_id"),
            "project_id": item.get("project_id"),
            "lead_id": item.get("lead_id"),
            "build_id": item.get("build_id"),
            "execution_id": item.get("execution_id"),
            "recorded_at": item.get("recorded_at"),
            "age_seconds": item.get("age_seconds"),
            "next_action": item.get("next_action"),
            "generated_at": now_iso(),
        }
        alert = {key: value for key, value in alert.items() if value is not None}
        alert["alert_id"] = _dedupe_key(alert)
        alerts.append(alert)
    return alerts[:limit]


def _title_for(kind: str, code: str) -> str:
    if kind == "stale_job":
        return "Job left running with no recorded heartbeat"
    if kind == "deployment":
        return "Deployment outcome needs manual resolution"
    if code == "RETRY_LIMIT_REACHED":
        return "Job failed permanently after exhausting its attempts"
    if code in {"PUBLISH_STATE_UNCERTAIN", "PUBLISH_UNCERTAIN"}:
        return "Publish outcome is uncertain"
    return f"Workflow condition needs attention ({code or kind})"


class LogAlertSink:
    """Default sink: one structured critical log event per alert, to stderr and/or the log file."""

    name = "log"

    def __init__(self, *, stream=None) -> None:
        self.stream = stream

    def publish(self, alert: dict[str, Any]) -> dict[str, Any]:
        log_event(
            severity="CRITICAL",
            operation=f"alert.{alert.get('kind', 'workflow')}",
            code=alert.get("code"),
            message=f"{alert.get('title')}: {alert.get('message') or 'no recorded detail'}",
            stream=self.stream,
            **{key: value for key, value in alert.items() if key in
               ("job_id", "lead_id", "project_id", "build_id", "execution_id", "next_action", "alert_id")},
        )
        return {"delivered": True, "sink": self.name, "code": "LOGGED"}


class WebhookAlertSink:
    """External-channel integration point that requires an injected transport.

    ``transport`` receives ``(url, payload, timeout)`` and returns an HTTP status-like value or a
    dict. Without it this sink never sends anything - by design, so that no credential or live
    message can be produced by this repository's own code.
    """

    name = "webhook"

    def __init__(self, url: str, *, transport: Callable[[str, dict, float], Any] | None = None,
                 timeout: float = 10.0) -> None:
        self.url = str(url or "").strip()
        self.transport = transport
        self.timeout = float(timeout)

    def publish(self, alert: dict[str, Any]) -> dict[str, Any]:
        if not self.url:
            return {"delivered": False, "sink": self.name, "code": "SKIPPED_URL_NOT_CONFIGURED"}
        if self.transport is None:
            # Nothing in this repository performs the network call: an operator wires the transport
            # that matches their channel and its credential handling.
            return {"delivered": False, "sink": self.name, "code": "SKIPPED_TRANSPORT_NOT_CONFIGURED",
                    "next_action": "Provide transport=callable(url, payload, timeout) when constructing WebhookAlertSink."}
        try:
            response = self.transport(self.url, {"alert": alert}, self.timeout)
        except Exception as error:  # A failed delivery is reported, never swallowed.
            return {"delivered": False, "sink": self.name, "code": "DELIVERY_FAILED", "error": str(error)[:300]}
        status = response.get("status") if isinstance(response, dict) else response
        return {"delivered": bool(str(status).startswith("2")) if status is not None else True,
                "sink": self.name, "code": "DELIVERED", "provider_status": status}


def check_alerts(store, *, repository_root: Path | str = ".", sinks: Iterable[Any] | None = None,
                 notify: bool = False, seen: set[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """Evaluate critical conditions and (optionally) deliver them to the supplied sinks.

    Duplicate suppression is in-process by default: ``seen`` may be supplied by a long-lived caller
    so repeated checks do not re-alert the same recorded event. It is intentionally not persisted -
    the durable record of the condition is the job/project state itself, and duplicating that here
    would create a second source of truth.
    """
    alerts = critical_alerts(store, repository_root=repository_root, limit=limit)
    observed = seen if seen is not None else set()
    targets = list(sinks) if sinks is not None else ([LogAlertSink()] if notify else [])
    delivered: list[dict[str, Any]] = []
    evaluated: list[dict[str, Any]] = []
    suppressed = 0
    for alert in alerts:
        key = str(alert.get("alert_id"))
        summary = {"alert_id": key, "code": alert.get("code"), "kind": alert.get("kind"),
                   "job_id": alert.get("job_id"), "project_id": alert.get("project_id")}
        if key in observed:
            suppressed += 1
            continue
        if not targets:
            # Nothing was configured to receive it, so nothing may be reported as notified, and the
            # alert stays un-suppressed so a later notified run still delivers it.
            evaluated.append(summary)
            continue
        observed.add(key)
        results = [target.publish(alert) for target in targets]
        delivered.append({**summary, "sinks": results, "notification_attempted": True})
    return {
        "checked_at": now_iso(),
        "critical_count": len(alerts),
        "notified": len(delivered),
        "evaluated_without_sink": len(evaluated),
        "evaluated_alerts": evaluated,
        "suppressed_duplicates": suppressed,
        "alerts": alerts,
        "delivery": delivered,
        "notification_enabled": notify,
        "note": "Alerts describe recorded workflow state only; no lead or customer contact is ever attempted.",
    }
