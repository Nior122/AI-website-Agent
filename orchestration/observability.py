"""Structured, secret-free operational logging and diagnostic helpers.

Operational data is only useful if an operator can trust it and read it without
having to decode prose. This module therefore provides the small set of primitives the
orchestration layer uses to describe itself:

* :func:`redact` / :func:`sanitize_diagnostic` - stable scrubbing of credentials before any
  error text is persisted in a job, an execution record, a log line, or an HTTP response.
* :func:`log_event` - one JSON object per line with severity, operation, stable code, the
  identifiers that make the event traceable, and a sanitized message.
* :func:`recorded_age_seconds` / :func:`recorded_duration_seconds` - time arithmetic that
  works **only** from timestamps the system actually recorded, and returns ``None`` (never a
  guess) when the evidence is missing.

No network, no third-party dependency, and no log file is created unless the operator
explicitly configures ``AI_WEBSITE_AGENT_LOG_DIR``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

#: Identifiers an operator can use to trace an event back to the records it concerns.
TRACE_FIELDS = (
    "job_id", "lead_id", "project_id", "build_id", "validation_id",
    "review_id", "deployment_id", "execution_id", "worker_id", "attempt",
)

_REDACTIONS = (
    # Authorization headers / bearer values of any kind.
    (re.compile(r"(?i)\b(authorization|proxy-authorization|x-api-key)\b(\s*[:=]\s*)[^\r\n]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}"), r"\1 [REDACTED]"),
    # Named secrets in query strings, JSON fragments, or prose.
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?key|refresh[_-]?token|"
            r"id[_-]?token|access[_-]?token|auth[_-]?token|token)\b(\s*[:=]\s*)(\"?)([^\"'\s,&}]+)(\"?)"
        ),
        r"\1\2\3[REDACTED]\5",
    ),
    # URL user info (database and other connection strings): user and password alike, so a report
    # never reveals which account a failed connection used.
    (re.compile(r"://([^:/@\s]+:)([^@\s]+)(@)"), r"://[REDACTED]\3"),
    (re.compile(r"://([A-Za-z0-9_.\-]{1,64})(@)"), r"://[REDACTED]\2"),
    # Provider-style key material.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), "sk-[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"), "[REDACTED-JWT]"),
    # Vercel project IDs / org IDs are not secret, but a raw token in a CLI error is.
    (re.compile(r"(?i)(vercel[_-]?token|vci[_-]?token)(\s*[:=]\s*)\S+"), r"\1\2[REDACTED]"),
)

_TRUNCATION_NOTE = "…[truncated]"


def redact(text: Any) -> str:
    """Return *text* with credential-shaped values replaced, without altering the rest."""
    message = str(text)
    for pattern, replacement in _REDACTIONS:
        message = pattern.sub(replacement, message)
    return message


def _limit(message: str, limit: int) -> str:
    if limit <= 0 or len(message) <= limit:
        return message
    return message[: max(0, limit - len(_TRUNCATION_NOTE))].rstrip() + _TRUNCATION_NOTE


def sanitize_diagnostic(error: BaseException | str | None, *, limit: int = 1200) -> str | None:
    """Redact and bound an exception/message so it is safe to persist or return.

    The type name is preserved for fault classes (``ValueError: ...``) because the class is
    diagnostically useful and never sensitive.
    """
    if error is None:
        return None
    if isinstance(error, BaseException):
        text = str(error) or type(error).__name__
        if not text.startswith(type(error).__name__):
            text = f"{type(error).__name__}: {text}"
    else:
        text = str(error)
    try:  # Reuse the storage layer's own URL/password redaction first when available.
        from tracking.database import sanitize_db_error

        text = sanitize_db_error(text)
    except Exception:  # pragma: no cover - the redaction must never be fatal.
        pass
    return _limit(redact(text).replace("\n", " ").strip(), limit)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp this system recorded; ``None`` when there is no usable evidence."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recorded_age_seconds(value: Any, *, now: datetime | None = None) -> int | None:
    """Age in whole seconds of a recorded timestamp, or ``None`` when it was never recorded."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0, int((reference - parsed).total_seconds()))


def recorded_duration_seconds(start: Any, end: Any, *, now: datetime | None = None) -> int | None:
    """Duration between two recorded timestamps; falls back to "still running" against *now*."""
    started = parse_timestamp(start)
    if started is None:
        return None
    finished = parse_timestamp(end) or (now or datetime.now(timezone.utc))
    return max(0, int((finished - started).total_seconds()))


def log_file_path(env: dict[str, str] | None = None) -> Path | None:
    """Configured operational log location, or ``None`` when the operator did not enable it."""
    values = os.environ if env is None else env
    directory = str(values.get("AI_WEBSITE_AGENT_LOG_DIR") or "").strip()
    if not directory:
        return None
    return Path(directory) / str(values.get("AI_WEBSITE_AGENT_LOG_FILE") or "operations.jsonl").strip()


def min_severity(env: dict[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    level = str(values.get("AI_WEBSITE_AGENT_LOG_LEVEL") or "WARNING").strip().upper()
    return level if level in _SEVERITY_RANK else "WARNING"


def should_emit(severity: str, *, env: dict[str, str] | None = None) -> bool:
    configured = _SEVERITY_RANK[min_severity(env)]
    return _SEVERITY_RANK.get(str(severity).upper(), _SEVERITY_RANK["INFO"]) >= configured


def log_event(
    *,
    severity: str,
    operation: str,
    code: str | None = None,
    message: Any = None,
    stream=None,
    env: dict[str, str] | None = None,
    timestamp: str | None = None,
    **identifiers,
) -> dict[str, Any]:
    """Emit (and return) one structured operational event.

    Output goes to stderr by default so CLI/API JSON on stdout stays parseable; when
    ``AI_WEBSITE_AGENT_LOG_DIR`` is configured the same line is appended as JSON Lines.
    """
    record: dict[str, Any] = {
        "timestamp": timestamp or now_iso(),
        "severity": str(severity).upper() if str(severity).upper() in _SEVERITY_RANK else "INFO",
        "operation": str(operation),
    }
    if code:
        record["code"] = str(code)
    for field in TRACE_FIELDS:
        value = identifiers.get(field)
        if value is not None:
            record[field] = value
    detail = sanitize_diagnostic(message) if message is not None else None
    if detail:
        record["detail"] = detail
    extra = {key: value for key, value in identifiers.items() if key not in TRACE_FIELDS and value is not None}
    if extra:
        record["context"] = extra
    line = json.dumps(record, separators=(",", ":"), sort_keys=False, default=str)
    if should_emit(record["severity"], env=env):
        target = stream if stream is not None else sys.stderr
        try:
            target.write(line + "\n")
        except Exception:  # pragma: no cover - logging must never break the operation.
            pass
    path = log_file_path(env)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # A missing or unwritable log directory must not fail the operation itself.
    return record


_LOCAL_PATHS = (
    # POSIX absolute paths (including file:// URLs) and Windows drive/UNC paths.
    re.compile(r"file://[^\s,\"']+"),
    # A leading '/' that is not part of a URL (scheme would put ':' or '/' in front of it).
    re.compile(r"(?<![:/\w])/(?:[^/\s,\"']+/)*[A-Za-z0-9._\-]+"),
    # A Windows drive root only when the letter stands alone (so `https:/` is never mistaken for one).
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:[^\s,\"'\\/]+[\\/])*[^\s,\"'\\/]*"),
    re.compile(r"\\\\[^\s,\"']+"),
)


def redact_local_paths(text: Any) -> str:
    """Replace absolute local filesystem paths with a marker, for responses that must not map the host.

    Stored records legitimately carry *repository-relative* artifact paths (an operator needs them to
    find a file), so this is applied at the HTTP boundary rather than when data is persisted: it removes
    sandbox/home/container absolute paths that an exception or configuration message may have picked up,
    while leaving relative paths and URLs readable.
    """
    message = str(text)
    for pattern in _LOCAL_PATHS:
        message = pattern.sub("[LOCAL-PATH-REDACTED]", message)
    return message


def url_host(url: Any) -> str | None:
    """Host-only view of a URL, for operator reports that must not echo credentials or paths."""
    text = str(url or "").strip()
    if not text:
        return None
    try:
        host = urlparse(text).hostname
    except ValueError:
        return None
    return host or None
