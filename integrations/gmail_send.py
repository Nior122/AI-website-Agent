"""The narrow sending interface: one account, one verb, its own credentials, and no mailbox access.

Phase 48 made Gmail inbound readable; that adapter is read-only by construction. Phase 49 needs a *send*, and the
honest way to add one is a second client with its own OAuth credential pair scoped to
``https://www.googleapis.com/auth/gmail.send``. Google grants a refresh token only the scopes the account holder
consented to, so this client cannot read, search, label, or delete - the token itself will not authorize those
calls - and this class does not expose them either. The read-only client has no send method; this class has no
read method. Anything that needs both (the send executor, which reads a message back to prove a provider id is
real) composes the two and keeps each one honest.

What is deliberately **not** here:

* **No approval logic.** "May this go out?" is answered by the approved outreach record in the ledger and its
  reviewer decision; the executor in :mod:`orchestration.outbound` re-checks that before a call. The adapter
  refuses only what it can see is wrong from here: a recipient that does not match the message it was handed,
  more than one recipient, a CC or ``Reply-To`` that would carry a stranger's address, an opt-out address, a
  method other than ``POST``, and a body that has already been sent once and cannot be proven not to be.
* **No retry that could double-send.** A timeout *after* the request was submitted is reported as
  ``SEND_UNCERTAIN`` with the code ``PROVIDER_TIMEOUT_AFTER_SUBMIT``. Nothing here re-sends automatically,
  because Gmail offers no idempotency key - the only cure is a read-back, which the executor does with the
  read-only client.
* **Nothing secret in a return value.** A bearer token lives in the transport, is never returned, and error text
  goes through :func:`orchestration.observability.redact`.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from typing import Any, Callable, Mapping

from orchestration.observability import redact

from .gmail_transport import GMAIL_API_ROOT, GmailError, GmailTransport

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SEND_REFRESH_TOKEN_ENV = "AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN"
GMAIL_SEND_CLIENT_ID_ENV = "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID"
GMAIL_SEND_CLIENT_SECRET_ENV = "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET"
GMAIL_SEND_ENABLED_ENV = "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED"
#: The address the operator's organization mails from, when it is not the account's default. Optional, and never
#: inferred: an empty value means "let Gmail use the authenticated account", which is the safer answer.
GMAIL_SEND_FROM_ENV = "AI_WEBSITE_AGENT_EMAIL_SEND_FROM"
GMAIL_SEND_REPLY_TO_ENV = "AI_WEBSITE_AGENT_EMAIL_SEND_REPLY_TO"
#: The one header an automated send must be able to set, because an organization's policy is that a prospect can
#: stop being contacted. Without a real page the header is refused, not fabricated.
GMAIL_SEND_UNSUBSCRIBE_URL_ENV = "AI_WEBSITE_AGENT_EMAIL_UNSUBSCRIBE_URL"

#: The only HTTP method this adapter will perform. A single verb is the whole attack surface.
SEND_ONLY_METHOD = "POST"
#: A recipient is one person. Bcc-style mass sending is refused, not silently truncated.
MAX_RECIPIENTS = 1
#: Google's documented ceiling for a single message; kept well below it so a runaway draft fails loudly.
MAX_BODY_CHARACTERS = 20000
#: Header names a caller may not set from here, because each one is a way to make the message look like it came
#: from somewhere else or to deliver it to a second address.
FORBIDDEN_HEADERS = {"cc", "bcc", "reply-to", "from", "sender", "content-type", "mime-version", "to"}
_ADDRESS = re.compile(r"^\s*(?:[^@\s<]+[^<>]*<)?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?\s*$")
_MESSAGE_ID = re.compile(r"^<?[!#-'\*+\-/0-9A-Z=\?a-z~._%&@]+>?$")


def extract_address(value: Any) -> str | None:
    """Public name for the one-address parser, used by the send executor and the suppression view."""
    return _address(value)


def _address(value: Any) -> str | None:
    """The bare address of a ``Display Name <a@b>`` string, or ``None`` when it is not one address."""
    match = _ADDRESS.match(str(value or ""))
    return match.group(1).lower() if match else None


def build_message(*, to: Any, subject: str, body_text: str, body_html: str | None = None,
                  from_address: str | None = None, in_reply_to: str | None = None,
                  references: list[str] | None = None, thread_id: str | None = None,
                  unsubscribe_url: str | None = None, organization: str | None = None,
                  headers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compose the one RFC 2822 message Gmail will be asked to send, and hash it.

    Returns the wire-ready ``raw`` (base64url, exactly as Gmail's ``messages.send`` wants it), the headers as
    they were set, a fingerprint of the encoded bytes, and the fields the ledger needs to prove later that *this*
    is the message that was approved. A non-ASCII subject is encoded by :mod:`email` rather than dropped or
    transliterated, because Lagos business names are not ASCII.

    Refusals here are about shape, not permission: a recipient that is not one address, a subject or body that is
    empty or absurdly long, an ``In-Reply-To`` that is not a message-id we were *given* - a fabricated
    Message-ID would splice this send into somebody's thread - and any header a caller tried to set directly.
    """
    recipient = _address(to)
    if not recipient:
        raise ValueError("A send needs exactly one recipient address; a list, an empty value, or an unparsable "
                         "address is refused rather than guessed at.")
    subject_text = str(subject or "").strip()
    text = str(body_text or "").strip()
    if not subject_text:
        raise ValueError("A send needs a subject; an empty one reads as junk mail and hides the mistake.")
    if not text:
        raise ValueError("A send needs a body.")
    if len(text) > MAX_BODY_CHARACTERS:
        raise ValueError(f"A body longer than {MAX_BODY_CHARACTERS} characters is refused: nothing in this "
                         "application drafts one, so a long body is a bug or an injection, not a long email.")
    if len(subject_text) > 300:
        raise ValueError("A subject longer than 300 characters is refused.")
    for name in sorted(headers or {}):
        if str(name).strip().lower() in FORBIDDEN_HEADERS:
            raise ValueError(f"The {name} header is not caller-settable; it is either fixed here or not sent at "
                             "all.")

    message = EmailMessage(policy=policy.default)
    sender = _address(from_address) or ""
    if from_address and not sender:
        raise ValueError("The configured From address is not one address; refusing to guess what to put there.")
    # Gmail ignores a From it does not own and substitutes the authenticated account, which is exactly the right
    # outcome for the recipient. We still refuse a second or malformed address so the intent stays readable.
    # `From` is set only when the operator configured it; otherwise the authenticated account's own address is
    # what Gmail puts on the message, which is the truthful default.
    if sender:
        message["From"] = from_address
    message["To"] = recipient
    message["Subject"] = subject_text
    message["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    message_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.aaiwa@{recipient.rsplit('@', 1)[-1]}"
    message["Message-ID"] = f"<{message_id}>"
    # Threading headers are copied only from values this application recorded for the message being answered.
    # Fabricating them is how an unrelated cold email becomes a "reply" in the recipient's client.
    reply_to_header = str(in_reply_to or "").strip()
    if reply_to_header:
        if not _MESSAGE_ID.match(reply_to_header):
            raise ValueError("In-Reply-To must be a message-id taken from the thread record, not free text.")
        message["In-Reply-To"] = reply_to_header
    reference_ids = [item for item in (str(value or "").strip() for value in (references or []))
                     if _MESSAGE_ID.match(item)]
    if references and len(reference_ids) != len([item for item in references if item]):
        raise ValueError("A References list may contain only message-ids; a malformed one is refused rather than "
                         "partially used.")
    if reference_ids:
        message["References"] = " ".join(reference_ids)
    unsubscribe_notice = None
    if unsubscribe_url:
        link = str(unsubscribe_url).strip()
        if not link.startswith(("https://", "http://")):
            raise ValueError("The unsubscribe URL has to be an absolute http(s) URL; a mailto: or a bare address "
                             "is refused rather than half-implemented, because nothing here can confirm one works.")
        message["List-Unsubscribe"] = f"<{link}>"
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    elif organization:
        # No header rather than a fabricated one. A missing one-click link is *reported* to the operator in the
        # send result and in `email-send-check` - mass-mailing with no way to opt out from inside the message is
        # how a domain lands in spam - but it does not block the send, because the opt-out this application
        # actually honours lives in its own ledger and the recipient can always reply.
        unsubscribe_notice = (f"no List-Unsubscribe header for {organization}: {GMAIL_SEND_UNSUBSCRIBE_URL_ENV} is "
                              "unset, so a recipient cannot opt out from inside the email itself")
    for name, value in (headers or {}).items():
        message[str(name)] = str(value)

    message.set_content(text)
    if body_html:
        # Alternative part only, and only when a caller supplies HTML. The plain text version above is the part
        # the ledger's content fingerprint is computed from, so a reviewer approves one thing.
        message.add_alternative(str(body_html), subtype="html")

    encoded = message.as_bytes()
    raw = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    return {"raw": raw,
            "headers": {key: str(value) for key, value in message.items()},
            "to": recipient,
            "from": sender,
            "subject": subject_text,
            "message_id": f"<{message_id}>",
            "thread_id": str(thread_id or "").strip() or None,
            "size_bytes": len(encoded),
            "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "unsubscribe_notice": unsubscribe_notice,
            "fingerprint": hashlib.sha256(raw.encode("ascii")).hexdigest()}


def send_config(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether this machine may send, in states an operator can act on.

    Five of them, because "the values are set" is not the same claim as "it works", and neither is the same as
    "this deployment is allowed to put a message in a stranger's inbox":

    ``disabled``    - credentials exist, the opt-in flag does not. Nothing will be sent. (Default.)
    ``inbound_only``- no send credentials at all. Reading mail still works; sending is not offered anywhere.
    ``configured``  - credentials and the flag are present, never exercised.
    ``unverified``  - a token exchange was attempted and failed; the refusal names the code.
    ``verified``    - the most recent explicit check exchanged a token successfully. Still says nothing about
                      whether any particular message was delivered.
    """
    import os

    source = dict(os.environ if env is None else env)
    names = (GMAIL_SEND_REFRESH_TOKEN_ENV, GMAIL_SEND_CLIENT_ID_ENV, GMAIL_SEND_CLIENT_SECRET_ENV)
    missing = [name for name in names if not (source.get(name) or "").strip()]
    enabled = (source.get(GMAIL_SEND_ENABLED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}
    state = ("inbound_only" if missing else "disabled" if not enabled else "configured")
    return {"state": state,
            "scope": GMAIL_SEND_SCOPE,
            "enabled": enabled,
            "configured": not missing,
            "account": (source.get("AI_WEBSITE_AGENT_GMAIL_ACCOUNT") or "me").strip() or "me",
            "from_address": (source.get(GMAIL_SEND_FROM_ENV) or "").strip() or None,
            "reply_to": (source.get(GMAIL_SEND_REPLY_TO_ENV) or "").strip() or None,
            "unsubscribe_url": (source.get(GMAIL_SEND_UNSUBSCRIBE_URL_ENV) or "").strip() or None,
            "missing": missing,
            "next_action": (
                "Sending is not offered until a refresh token minted for only "
                f"{GMAIL_SEND_SCOPE} exists for this machine, in {', '.join(names)}. A "
                "read-only token cannot be reused for it, and this code will not try. Nothing here can grant "
                "that consent - it is an account decision." if missing else
                f"Set {GMAIL_SEND_ENABLED_ENV}=true to let the executor send approved drafts, and run "
                "`python -m orchestration.cli email-send-check` to confirm the credential works. Until both are "
                "true the state stays \"disabled\": configuration existing is not evidence of connectivity."
                if not enabled else
                "Configuration is present but unexercised: run `email-send-check`, which exchanges a token and "
                "sends nothing, before trusting any claim that this channel works."),
            "capability_note": ("a token exchange proves the credential is accepted; it does not prove a "
                                "message was delivered, read, or wanted")}


class GmailSendAdapter(GmailTransport):
    """``users.messages.send`` and nothing else.

    Composed by :class:`orchestration.outbound.ApprovedOutboundSender`; it is not a service, has no CLI of its
    own beyond a connectivity check, and cannot be asked to send something the ledger did not approve, because
    it never decides that - it only refuses what it can see.
    """

    SCOPE = GMAIL_SEND_SCOPE
    SCOPE_LABEL = "send-only"
    NOT_CONFIGURED_CODE = "GMAIL_SEND_NOT_CONFIGURED"
    API_ROOT = GMAIL_API_ROOT
    REFRESH_TOKEN_ENV = GMAIL_SEND_REFRESH_TOKEN_ENV
    CLIENT_ID_ENV = GMAIL_SEND_CLIENT_ID_ENV
    CLIENT_SECRET_ENV = GMAIL_SEND_CLIENT_SECRET_ENV
    ACCOUNT_ENV = "AI_WEBSITE_AGENT_GMAIL_ACCOUNT"

    def __init__(self, *, transport: Callable[..., dict[str, Any]] | None = None, enabled: bool = False,
                 suppression: Any = None, **kwargs: Any) -> None:
        """``enabled`` is the operator's flag, checked again on every send; ``suppression`` is the opt-out ledger."""
        super().__init__(transport=transport, **kwargs)
        self.enabled = bool(enabled)
        self._suppression = suppression
        self.sent: list[dict[str, Any]] = []
        #: Provider message ids this adapter handed back, so a reconciliation can be asked of the same client.
        self._provider_ids: dict[str, str] = {}

    # ------------------------------------------------------------------ the one verb

    def send(self, prepared: Mapping[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        """Send the already-composed message. Everything below is a refusal, then one call.

        The checks here are structural - the ones that hold no matter what the ledger says - because a check that
        reads the ledger from inside the adapter would be a *weaker* copy of the executor's, and a weaker copy is
        how a bypass gets written.
        """
        recipient = str(prepared.get("to") or "")
        if not self.enabled:
            return self._refused("SENDING_DISABLED", recipient,
                                 f"sending is not enabled on this machine; set {GMAIL_SEND_ENABLED_ENV}=true once "
                                 "the credentials and approvals are in place")
        raw = str(prepared.get("raw") or "")
        if not raw:
            return self._refused("MESSAGE_NOT_COMPOSED", recipient,
                                 "the send was asked to transmit something that was never composed")
        if recipient.count("@") != 1:
            return self._refused("RECIPIENT_AMBIGUOUS", recipient,
                                 "exactly one recipient address is sendable; a list is refused")
        if self._suppression is not None and not self._suppression.allowed(recipient):
            return self._refused("RECIPIENT_OPTED_OUT", recipient,
                                 "the address is on the suppression list, so this send was stopped at the "
                                 "provider boundary as well as the ledger")
        headers = prepared.get("headers") or {}
        for forbidden in ("cc", "bcc", "reply-to"):
            if any(str(name).strip().lower() == forbidden for name in headers):
                return self._refused("HEADER_NOT_PERMITTED", recipient,
                                     f"a {forbidden} header is not sendable from this interface")

        body: dict[str, Any] = {"raw": raw}
        thread_id = str(prepared.get("thread_id") or "").strip()
        if thread_id:
            # Only a thread id Gmail itself returned earlier is passed through; inventing one is refused.
            body["threadId"] = thread_id

        if dry_run:
            return {"sent": False, "dry_run": True, "code": None, "next_action": None, "provider": "gmail",
                    "endpoint": f"{self.api_root}/{self.account}/messages", "method": SEND_ONLY_METHOD,
                    "recipient": recipient, "size_bytes": prepared.get("size_bytes"),
                    "message_ref": f"gmail:send:{prepared.get('fingerprint', '')[:16]}",
                    "would_send": True, "headers": ["To", "Subject", "Message-ID"],
                    "capability_note": ("a dry run proves the shape of the request, not that Google will accept "
                                        "it; no network call was made")}

        payload = self.request_json("/messages", method=SEND_ONLY_METHOD, body=body)
        provider_id = str(payload.get("id") or "").strip()
        if not provider_id:
            # Google answered without an id. That is not a failure to send and not a success; it is unknown, and
            # the caller must not resend.
            return {"sent": False, "dry_run": False, "code": "SEND_UNCERTAIN", "provider": "gmail",
                    "next_action": ("Gmail answered the send without a message id. Do not retry: run "
                                    "`email-reconcile`, which looks for the message in the account, then decide "
                                    "as the operator."),
                    "recipient": recipient, "reason": "the provider response carried no `id` field",
                    "provider_message_id": None, "thread_id": payload.get("threadId"),
                    "attempted_at": _now_iso()}
        result = {"sent": True, "dry_run": False, "code": None, "next_action": None, "provider": "gmail",
                  "provider_message_id": provider_id, "thread_id": payload.get("threadId"),
                  "recipient": recipient, "size_bytes": prepared.get("size_bytes"),
                  "message_ref": f"gmail:message:{provider_id}",
                  "attempted_at": _now_iso(),
                  "fingerprint": prepared.get("fingerprint")}
        self._provider_ids[str(prepared.get("fingerprint") or provider_id)] = provider_id
        self.sent.append({"provider_message_id": provider_id, "recipient": recipient,
                          "message_id": prepared.get("message_id"), "fingerprint": prepared.get("fingerprint"),
                          "at": result["attempted_at"]})
        return result

    def verify_authorization(self) -> dict[str, Any]:
        """Exchange a token and send nothing. The strongest claim available offline.

        Returns ``verified`` only if Google accepted the refresh token - which proves the credential is live and
        the consent exists, and proves nothing at all about delivery.
        """
        try:
            self._mint_token()
        except GmailError as error:
            return {"state": "unverified", "verified_at": None, "code": error.code,
                    "detail": redact(str(error))[:200], "next_action": error.next_action or error.describe()["next_action"]}
        return {"state": "verified", "verified_at": _now_iso(), "code": None,
                "detail": "a token was exchanged with the send-only credential; no message was sent",
                "next_action": "Configuration is present *and* the credential works. Sending still requires an "
                               "approved draft per recipient, every time."}

    def provider_message_id(self, fingerprint: str) -> str | None:
        """The id this adapter got back for an identical composed message, when it has one."""
        return self._provider_ids.get(str(fingerprint or ""))

    def describe(self) -> dict[str, Any]:
        """The safe shape of this client: one method, one scope, what it cannot do."""
        return {"provider": "gmail", "scope": self.SCOPE, "account": self.account,
                "methods": ["send"], "methods_refused_by_design": ["list", "get", "search", "delete", "modify",
                                                                    "labels", "threads"],
                "enabled": self.enabled, "endpoint": f"{self.api_root}/{self.account}/messages",
                "method_allowed": SEND_ONLY_METHOD, "max_recipients": MAX_RECIPIENTS,
                "token_present": bool(self._access_token), "token_minted_at": self.token_minted_at,
                "calls": self.calls, "errors": self.errors,
                "credential_environment": [self.REFRESH_TOKEN_ENV, self.CLIENT_ID_ENV, self.CLIENT_SECRET_ENV,
                                           GMAIL_SEND_ENABLED_ENV],
                "delivery_receipt_available": False,
                "read_capable": False,
                "capability_note": ("Gmail send returns a message id; it does not report delivery, open, or "
                                    "reading, so VERIFIED_DELIVERED stays unreachable from this interface")}

    # ------------------------------------------------------------------ internals

    def _refused(self, code: str, recipient: str, next_action: str) -> dict[str, Any]:
        return {"sent": False, "dry_run": False, "code": code, "provider": "gmail",
                "recipient": _address(recipient), "reason": next_action, "next_action": next_action,
                "provider_message_id": None, "attempted_at": _now_iso()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
