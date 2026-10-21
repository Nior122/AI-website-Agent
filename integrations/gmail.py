"""Gmail inbound adapter: read-only fetch of new mail, mapped onto the application's inbound contract.

Why Gmail for the first live email source, and what was verified before choosing it:

* The account the operator already uses for outreach is where a reply lands, and Google's documented
  ``users.messages.list`` / ``users.messages.get`` / ``users.history/list`` endpoints are ordinary JSON over HTTPS,
  which this repository already speaks (``integrations/http.py``, the same conventions the Vercel provider and the
  Telegram adapter use). No new dependency is introduced, and none was assumed.
* ``gmail.readonly`` is a documented OAuth scope. **This client requests no other scope and exposes no send,
  delete, modify, label, or mark-as-read method** - not because the API lacks them, but so that a compromised
  token in this process cannot rewrite or destroy a mailbox, and so that no code path here can reply to a
  business from inside the mailbox. Auto-reply is a separate capability with separate consent.
* Incremental delivery is real: ``users.history/list`` from a stored ``historyId`` returns only changes since
  that point, which is what makes "poll cheaply, never twice" possible. Where it is not wanted, the query path
  (``label:INBOX newer_than:...``) works with no cursor at all.

The adapter stops at "one provider event, normalized". Matching to a prospect, dedupe, triage, opt-outs, and
persistence belong to :class:`tracking.inbound.InboundIntake` (see :mod:`orchestration.email_inbound`), and this
module never claims a sender's identity was verified: it reports what Gmail said, labelled as such, because
"the mailbox received this from this address" is weaker than "this person owns this lead".
"""
from __future__ import annotations

import base64
import re
import binascii
from datetime import datetime, timezone
from typing import Any

from orchestration.observability import redact

from .gmail_transport import GMAIL_API_ROOT, GmailError, GmailTransport


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
#: Header names as Gmail spells them in ``payload.headers``.
_ADDRESS_HEADERS = ("From", "Reply-To", "Sender")
_THREAD_HEADERS = ("Thread-Topic",)
_REFERENCE_HEADERS = ("In-Reply-To", "References")

GMAIL_REFRESH_TOKEN_ENV = "AI_WEBSITE_AGENT_GMAIL_REFRESH_TOKEN"
GMAIL_CLIENT_ID_ENV = "AI_WEBSITE_AGENT_GMAIL_CLIENT_ID"
GMAIL_CLIENT_SECRET_ENV = "AI_WEBSITE_AGENT_GMAIL_CLIENT_SECRET"
GMAIL_ACCOUNT_ENV = "AI_WEBSITE_AGENT_GMAIL_ACCOUNT"
GMAIL_QUERY_ENV = "AI_WEBSITE_AGENT_GMAIL_QUERY"
GMAIL_MAX_RESULTS_ENV = "AI_WEBSITE_AGENT_GMAIL_MAX_RESULTS"
GMAIL_FETCH_FORMAT_ENV = "AI_WEBSITE_AGENT_GMAIL_FETCH_FORMAT"
#: Default keeps the full body out of this process. ``full`` is opt-in and documented as such.
DEFAULT_FETCH_FORMAT = "metadata"
FETCH_FORMATS = {"minimal", "metadata", "full"}
#: Google's own ceiling for ``maxResults``; the smaller default is a privacy and cost decision, not a limit.
MAX_RESULTS_CEILING = 500
#: Characters of message text the intake is asked to keep. The message stays in Gmail; `message_ref` points home.
SNIPPET_LIMIT = 900


def gmail_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """What this machine knows about its Gmail setup. The secret values themselves are never reported."""
    import os

    source = os.environ if env is None else env
    refresh = (source.get(GMAIL_REFRESH_TOKEN_ENV) or "").strip()
    client_id = (source.get(GMAIL_CLIENT_ID_ENV) or "").strip()
    client_secret = (source.get(GMAIL_CLIENT_SECRET_ENV) or "").strip()
    missing = [name for name, value in ((GMAIL_REFRESH_TOKEN_ENV, refresh), (GMAIL_CLIENT_ID_ENV, client_id),
                                        (GMAIL_CLIENT_SECRET_ENV, client_secret)) if not value]
    fetch_format = (source.get(GMAIL_FETCH_FORMAT_ENV) or DEFAULT_FETCH_FORMAT).strip().lower()
    return {
        "account": (source.get(GMAIL_ACCOUNT_ENV) or "me").strip() or "me",
        "scope": GMAIL_SCOPE,
        "configured": not missing,
        "missing": missing,
        "query": (source.get(GMAIL_QUERY_ENV) or "label:INBOX").strip(),
        "max_results": _bounded_int(source.get(GMAIL_MAX_RESULTS_ENV), default=25),
        "fetch_format": fetch_format if fetch_format in FETCH_FORMATS else DEFAULT_FETCH_FORMAT,
        "fetch_format_requested": fetch_format,
        "credentials_present": {"refresh_token": bool(refresh), "client_id": bool(client_id),
                               "client_secret": bool(client_secret)},
        "code": None if not missing else "GMAIL_NOT_CONFIGURED",
        "next_action": None if not missing else (
            "Create an OAuth client, authorize this machine with only "
            f"{GMAIL_SCOPE}, and set {GMAIL_REFRESH_TOKEN_ENV}, {GMAIL_CLIENT_ID_ENV}, and "
            f"{GMAIL_CLIENT_SECRET_ENV}. Nothing here can mint that consent for you: it is an account decision."
        ),
    }


def _bounded_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, MAX_RESULTS_CEILING))


class GmailClient(GmailTransport):
    """Read-only Gmail access: the verbs below, and nothing else.

    The point of this class is what it does **not** expose - there is no ``send``, no ``delete``, no ``modify``,
    no label or thread mutation, and no way to widen the OAuth scope from here. The scope named in
    :func:`gmail_config` is ``gmail.readonly``, and :meth:`status` states that plus what is deliberately absent,
    so an operator can check the claim instead of trusting it. Phase 49's send path lives in
    :mod:`integrations.gmail_send`, with its own credentials, precisely so this file stays a list of reads.

    ``transport`` may be injected for tests or an operator's proxy: it receives ``(method, url, params, headers,
    body)`` and returns the same ``{"status", "json", "text"}`` shape as
    :func:`integrations.http.provider_request`. With none, calls go to Google over HTTPS.
    """

    SCOPE = GMAIL_SCOPE
    SCOPE_LABEL = "read-only"
    API_ROOT = GMAIL_API_ROOT
    REFRESH_TOKEN_ENV = GMAIL_REFRESH_TOKEN_ENV
    CLIENT_ID_ENV = GMAIL_CLIENT_ID_ENV
    CLIENT_SECRET_ENV = GMAIL_CLIENT_SECRET_ENV
    ACCOUNT_ENV = GMAIL_ACCOUNT_ENV

    # ------------------------------------------------------------------ the read-only surface, and nothing else

    def list_messages(self, query: str, *, max_results: int = 25, page_token: str | None = None,
                      include_spam_trash: bool = False) -> dict[str, Any]:
        body = self._request_json("/messages", params={"q": query, "maxResults": _bounded_int(
            max_results, default=25), "pageToken": page_token,
            "includeSpamTrash": "true" if include_spam_trash else None})
        return {"messages": [item for item in (body.get("messages") or []) if isinstance(item, dict)],
                "next_page_token": body.get("nextPageToken"),
                "result_size_estimate": body.get("resultSizeEstimate")}

    def get_message(self, message_id: str, *, message_format: str = DEFAULT_FETCH_FORMAT) -> dict[str, Any]:
        chosen = str(message_format or DEFAULT_FETCH_FORMAT).strip().lower()
        if chosen not in FETCH_FORMATS:
            raise GmailError(f"Unknown fetch format '{chosen}'. Permitted: {', '.join(sorted(FETCH_FORMATS))}.",
                             code="GMAIL_FORMAT_UNSUPPORTED")
        return dict(self._request_json(f"/messages/{message_id}", params={"format": chosen}))

    def get_history(self, history_id: int, *, label_id: str = "INBOX", max_results: int = 100) -> dict[str, Any]:
        body = self._request_json("/history", params={"startHistoryId": int(history_id), "labelId": label_id,
                                                      "maxResults": _bounded_int(max_results, default=100)})
        return {"history": [item for item in (body.get("history") or []) if isinstance(item, dict)],
                "history_id": body.get("historyId")}

    def latest_history_id(self) -> int | None:
        """``users.getProfile``'s cursor, for the first run only. ``None`` when Google does not answer with one."""
        profile = self._request_json("/profile")
        value = profile.get("historyId")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def status(self) -> dict[str, Any]:
        """The safe shape of this client for a report: capabilities and counters, never a secret."""
        return {"account": self.account, "scope": GMAIL_SCOPE, "capabilities": ["messages.list", "messages.get",
                                                                                "history.list", "profile.get"],
                "explicitly_absent": ["messages.send", "messages.delete", "messages.modify", "messages.trash",
                                      "labels", "threads.manage"],
                "credential_environment": [self.REFRESH_TOKEN_ENV, self.CLIENT_ID_ENV, self.CLIENT_SECRET_ENV],
                "token_present": bool(self._access_token), "token_minted_at": self.token_minted_at,
                "calls": self.calls, "errors": self.errors,
                "send_capable": False,
                "sending_is_a_different_interface": ("sending has its own client and its own credentials in "
                                                      "integrations/gmail_send.py, which cannot read mail")}


# ---------------------------------------------------------------------------------------------------------
# mapping a Gmail message onto the application's inbound event contract
# ---------------------------------------------------------------------------------------------------------

def message_to_inbound_event(message: dict[str, Any], *, fetch_format: str = DEFAULT_FETCH_FORMAT) -> dict[str, Any]:
    """Turn one Gmail message resource into a :class:`tracking.inbound.InboundIntake` payload.

    Nothing is decided here: no lead, no classification, no "is this a reply to our draft". Only what Gmail
    reported is carried across, and the fields that decide matching (sender address, thread id, provider event id)
    are labelled as provider assertions rather than proof.
    """
    if not isinstance(message, dict) or not message.get("id"):
        raise GmailError("A Gmail message resource without an `id` cannot be recorded.",
                         code="GMAIL_MESSAGE_MALFORMED")
    message_id = str(message["id"])
    headers = header_map(message.get("payload") or {})
    snippet = str(message.get("snippet") or "").strip()
    body_text = snippet
    body_source = "gmail_snippet"
    if fetch_format == "full":
        extracted = extract_plain_text(message.get("payload") or {})
        if extracted:
            body_text, body_source = extracted, "gmail_body_first_paragraph"
    if not body_text:
        body_text, body_source = f"(no text Gmail returned for {message_id})", "gmail_none"
    event: dict[str, Any] = {
        "provider": "EMAIL_INBOUND",
        # Gmail's message id is stable and unique per message: exactly what the intake wants as its identity,
        # so a re-poll of the same message cannot become a second response in a business's history.
        "event_id": message_id,
        "channel": "EMAIL",
        "occurred_at": internal_date_iso(message.get("internalDate")),
        "conversation_id": str(message.get("threadId") or "")[:120] or None,
        "sender": first_address(headers),
        "summary": (headers.get("Subject") or "no subject recorded")[:1200],
        "body": body_text[:SNIPPET_LIMIT],
        "message_ref": f"gmail:{message_id}",
    }
    # `In-Reply-To` / `References` are deliberately not copied into the event's `in_reply_to`: that field of the
    # intake contract names *a record we created* (an outreach draft id), and an RFC 822 Message-ID is not one.
    # Sending it would be refused as an invalid id, and minting a record to satisfy it would be inventing history.
    # The poller resolves the reference against recorded events instead, where a real link can be proven.
    if not event["occurred_at"]:
        # No InternalDate is unusual, not fatal: the intake stamps receipt and labels the gap itself.
        event.pop("occurred_at")
    return event


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    """``payload.headers`` -> a lookup, first occurrence wins, header names as Gmail capitalizes them."""
    result: dict[str, str] = {}
    for entry in (payload.get("headers") or []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        value = str(entry.get("value") or "")
        if name and name not in result:
            result[name] = value
    return result


def first_address(headers: dict[str, str]) -> str | None:
    """The bare address of the first present sender header.

    The display name is deliberately dropped: it is text the sender chose ("PayPal Security <a@b.co>") and it has
    no business travelling into a record that an operator reads as an identity.
    """
    for name in _ADDRESS_HEADERS:
        value = headers.get(name)
        if not value:
            continue
        match = re.search(r"<([^<>]+@[^<>]+)>", str(value))
        address = (match.group(1) if match else str(value)).strip().strip("<>").lower()
        return address or None
    return None


def extract_plain_text(payload: dict[str, Any], *, max_chars: int = 6000) -> str:
    """First plain-text part, decoded from Gmail's base64url, truncated to one paragraph.

    Multipart bodies are walked depth-first because a reply is usually the first part and the quoted history is
    the last; the truncation then keeps the reply and drops the archive, which is both cheaper and kinder to the
    ledger.
    """
    for part in _parts(payload):
        if str(part.get("mimeType") or "").lower().startswith("text/plain"):
            encoded = str(((part.get("body") or {}).get("data")) or "")
            if not encoded:
                continue
            decoded = _decode(encoded)
            if not decoded:
                continue
            paragraph = decoded.strip().split("\n\n", 1)[0].strip()
            return paragraph[:max_chars]
    return ""


def _decode(encoded: str) -> str:
    # Gmail sends base64url without padding. The padding has to be computed on the string that will actually be
    # decoded - padding the raw value and then mutating it shifts every byte after the first multiple of four.
    for candidate in (encoded.replace("-", "+").replace("_", "/"), encoded):
        cleaned = "".join(ch for ch in candidate if ch.isalnum() or ch in "+/").rstrip()
        cleaned = cleaned.rstrip("=")
        try:
            decoded = base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        return decoded.decode("utf-8", errors="replace")
    return ""


def _parts(payload: dict[str, Any]):
    stack = list(payload.get("parts") or [])
    if payload.get("mimeType") and not stack:
        stack.append(payload)
    while stack:
        part = stack.pop(0)
        if isinstance(part, dict):
            yield part
            stack = list(part.get("parts") or []) + stack


def internal_date_iso(value: Any) -> str | None:
    """Gmail's ``internalDate`` is epoch *seconds* as a string; the intake wants ISO-8601."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def describe_error(error: BaseException) -> dict[str, Any]:
    """The safe, bounded shape of a Gmail fault for a record or an operator answer."""
    if isinstance(error, GmailError):
        return {"error_code": error.code, "error": redact(str(error))[:400], "retryable": bool(error.retryable),
                **({"provider_status": error.status} if error.status else {}),
                **({"next_action": error.next_action} if error.next_action else {})}
    if isinstance(error, ProviderError):
        return {"error_code": error.code, "error": redact(str(error))[:400], "retryable": bool(error.retryable)}
    return {"error_code": "GMAIL_FAULT", "error": redact(f"{type(error).__name__}: {error}")[:400],
            "retryable": True}
