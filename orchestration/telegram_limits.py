"""Redaction, masking, and bounding for text leaving through an operator channel.

A Telegram thread is stored on somebody else's servers and read on a device that may be lost. The pipeline's own
records already carry what an operator needs; the message only has to point at them. So this module is strict
about what crosses the boundary, and does it in one place so no surface can be more careful than another:

* :func:`safe_outbound_text` - bound the length, refuse to send a credential-shaped fragment (replaced, with a
  marker saying so, because dropping a whole notification silently is worse than sending a censored one), and
  strip absolute local paths, which map the operator's host onto a chat screen.
* :func:`mask_address` - show enough of an email to recognise a correspondent without publishing it.
* :func:`attention_identity` - the dedupe key for "we already told you about this", using the same field set as
  :mod:`orchestration.alerts`, so a repeated poll of an unchanged record sends nothing new.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from tracking.engagement import find_secret_shaped_text, redact_contact_details

from orchestration.observability import redact

#: Absolute filesystem roots that map the operator's machine. Deliberately narrower than
#: :func:`orchestration.observability.redact_local_paths`, whose job is HTTP error text: that helper also matches
#: a leading ``/word``, and in a chat about this bot that would erase the commands themselves (``/hold``,
#: ``/status <id>``) instead of the paths. A bot answer has to stay readable, so this list names real roots.
HOST_PATH_ROOTS = ("/home/", "/Users/", "/root/", "/var/", "/etc/", "/srv/", "/opt/", "/mnt/", "/media/", "/tmp/",
                   "/usr/", "/private/")
_WINDOWS_DRIVE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s,\"']+")
_PATH_MARKER = "[local path removed]"

#: Telegram's own limit is 4096; the bot's answers stay well under it so a long list is split or pointed at the
#: CLI instead of being cut mid-sentence.
OUTBOUND_LIMIT = 3600
SECRET_MARKER = "[credential removed from this message]"
MASKED_TAIL = 3

#: Phone-number shapes. A chat is read on a device that can be lost, so a number is never published whole - the
#: last four digits are enough for an operator to recognise which contact a record points at, and the ledger and
#: `python3 -m tracking.cli` remain the place to read the value itself. Dates are deliberately untouched: the
#: patterns need a long run of digits (or a leading `+`), which an ISO timestamp never forms.
_PHONE_RUN = re.compile(r"(?<![\dA-Za-z])(\d{9,})(?![\dA-Za-z])")
_INTERNATIONAL_PHONE = re.compile(r"(?<![\dA-Za-z])\+(\d[\d \-]{7,16}\d)(?![\dA-Za-z])")


def safe_outbound_text(value: Any, *, limit: int = OUTBOUND_LIMIT) -> str:
    """Bounded, credential-free, path-free text fit for an operator chat."""
    text = str(value or "")
    # Contact details first: the ledger redacts on write, and a notification quoting a record should not undo it.
    text = redact_contact_details(text) or ""
    # Whole-line replacement, deliberately: the pattern says "this line carries something credential-shaped", not
    # "only this substring is secret", and censoring the matched fragment alone would leave the value beside it.
    lines = [SECRET_MARKER if find_secret_shaped_text(line) is not None else line
             for line in text.split("\n")]
    text = "\n".join(lines)
    text = _strip_host_paths(redact(text))
    text = _mask_phone_numbers(text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[continued in the application's own reports]"


def safe_preview_text(value: Any, *, limit: int = OUTBOUND_LIMIT) -> str:
    """Safe rendering for an explicitly requested private draft preview.

    Unlike ordinary notifications this preserves recipient addresses and phone numbers so the operator can review
    the exact message. It still removes credential-shaped lines, absolute local paths, and secret-shaped fragments.
    Call only from allow-listed, explicit draft-review commands; never use for alerts or customer messages.
    """
    text = str(value or "")
    lines = [SECRET_MARKER if find_secret_shaped_text(line) is not None else line for line in text.split("\n")]
    text = "\n".join(lines)
    text = _strip_host_paths(redact(text))
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…[preview truncated; inspect the stored draft before approval]"


def _mask_phone_numbers(text: str) -> str:
    """Keep the tail of a phone number and hide the rest."""
    text = _INTERNATIONAL_PHONE.sub(lambda m: f"+{m.group(1)[0]}…{m.group(1)[-4:]}", text)
    return _PHONE_RUN.sub(lambda m: f"{m.group(1)[:2]}…{m.group(1)[-4:]}", text)


def _strip_host_paths(text: str) -> str:
    """Remove absolute local paths, leaving slash-prefixed words like ``/attention`` alone.

    Whole-URL and Windows forms go first: replacing a path piece by piece would leave the pieces that were not a
    root behind, and a half-erased path is still a hint about the host.
    """
    text = re.sub(r"file://[^\s,\"']+", _PATH_MARKER, text)
    text = _WINDOWS_DRIVE.sub(_PATH_MARKER, text)
    for root in HOST_PATH_ROOTS:
        pattern = re.compile(re.escape(root) + r"(?:[^\s,\"']*[^\s,\"'])")
        text = pattern.sub(_PATH_MARKER, text)
    return text


def mask_address(value: Any) -> str:
    """`aisha.bello@pitstop.ng` -> `ais…lo@pitstop.ng`. Enough to recognise, not enough to copy out.

    Anything that is not an email-shaped string is returned as bounded text: this is a masking helper for
    rendering, not a validator, and an unexpected value must not become a crash in a notification path.
    """
    text = str(value or "").strip()
    match = re.search(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text)
    if not match:
        return text[:120]
    local, domain = match.group(1), match.group(2)
    if len(local) <= MASKED_TAIL * 2:
        masked = (local[:1] or "?") + "…"
    else:
        masked = local[:MASKED_TAIL] + "…" + local[-MASKED_TAIL:]
    return text[:match.start()] + f"{masked}@{domain}" + text[match.end():][:120]


def attention_identity(item: dict[str, Any]) -> str:
    """Stable id for one recorded condition, so it can be reported once.

    Deliberately the same fields :func:`orchestration.alerts._dedupe_key` hashes (kind, code, job, project,
    recorded_at) plus the engagement record ids, because the notification surface and the alert surface must
    agree about when something is *the same* condition. Two surfaces with two notions of identity is how an
    operator ends up told twice about one fault and never about another.
    """
    identity = {key: item.get(key) for key in
                ("kind", "code", "job_id", "project_id", "lead_id", "outreach_id", "proposal_id", "follow_up_id",
                 "build_id", "event_id", "engagement_id", "recorded_at", "received_at", "due_at")}
    identity = {key: value for key, value in identity.items() if value not in (None, "")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{item.get('kind') or 'condition'}:{digest[:16]}"


def clip_list(lines: list[str], limit: int) -> list[str]:
    """Keep a list bounded and say so, rather than silently dropping rows an operator needed."""
    rows = [line for line in lines if line]
    if len(rows) <= limit:
        return rows
    kept = rows[:limit]
    kept.append(f"…{len(rows) - limit} more line(s) omitted here; the full list is in /attention or "
                "`python3 -m orchestration.cli attention`.")
    return kept
