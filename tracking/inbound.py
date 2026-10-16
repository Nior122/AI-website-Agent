"""Authorized inbound-response intake: the front door for a prospect's reply.

This module turns an external event (an email that came back, a contact-form submission, a provider webhook)
into a ledger record. It is deliberately *narrow*: it can record what the business said and nothing else. It
cannot send, cannot approve, cannot mark a proposal presented or accepted, and cannot record an outcome - a
caller that could do those things over an inbound feed would be able to declare its own success, which is the
failure mode the whole approval path exists to prevent.

Three rules shape every design choice here:

* **A provider's words are not the application's conclusions.** A caller may *suggest* a classification; it is
  stored as a hint with its provenance, never as fact. The only automated classification in this repository is
  the transparent rule baseline below, which reports which phrase matched and how certain it was - it is not a
  model, and it is labelled so.
* **Idempotency is durable.** The intake record and the ledger record land in the same store write, so a
  re-delivered event cannot double-count, and a *failed* event stays retryable instead of being silently
  swallowed by its own dedupe key.
* **Unmatched means unmatched.** A message that cannot be linked to a tracked lead through a recorded reference
  is parked for review. Guessing which business it came from would put a real statement in the wrong audit
  trail, which is worse than admitting it is unreadable.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from tracking.engagement import (
    CHANNELS,
    EngagementLedger,
    LedgerError,
    MAX_QUOTE_CHARS,
    find_secret_shaped_text,
    normalize_datetime,
    redact_contact_details,
)
from tracking.store import now, validate_bounded_text, validate_record_id

#: The provider-neutral intake contract. None of these names implies an integration that has been connected:
#: they describe the *shape* an adapter must produce, and the tests drive them with fictional events.
INBOUND_PROVIDERS = {
    "GENERIC_WEBHOOK": {"stable_event_id": False},
    "EMAIL_INBOUND": {"stable_event_id": True},
    "SMS_INBOUND": {"stable_event_id": True},
    "WHATSAPP_INBOUND": {"stable_event_id": True},
    "FORM_INBOUND": {"stable_event_id": False},
}

#: Providers whose API this repository has checked well enough to accept a *delivery receipt* for an
#: outbound message. It is empty on purpose, and it is the reason `ACCEPTED_BY_PROVIDER` and
#: `VERIFIED_DELIVERED` stay unreachable from here (the rule Phase 46 set): an inbound reply proves a
#: business answered, not that a provider delivered, and no webhook can be relabelled into a receipt. An
#: adapter may be added only with the provider's verified read-back documented in tracking/README.md - never
#: by accepting a caller's assertion in a payload field.
DELIVERY_RECEIPT_PROVIDERS: tuple[str, ...] = ()

#: Categories the rule baseline can recognise. They are recommendations about what a *human* should do next,
#: and `TRIAGE_NEXT_ACTIONS` says which - never a state transition by themselves.
TRIAGE_CATEGORIES = {
    "INTERESTED", "INFORMATION_REQUEST", "MEETING_REQUEST", "PROPOSAL_QUESTION",
    "NOT_INTERESTED", "OPT_OUT", "UNCLEAR",
}
TRIAGE_NEXT_ACTIONS = {
    "INTERESTED": "draft a reply and prepare a proposal - both need a human's approval before anything goes out",
    "INFORMATION_REQUEST": "answer the question: prepare a reply for review",
    "MEETING_REQUEST": "propose a meeting time; scheduling needs the operator's confirmation",
    "PROPOSAL_QUESTION": "prepare or update the proposal and answer from what is actually recorded",
    "NOT_INTERESTED": "record the decision as an outcome with the customer's words as evidence, or close it",
    "OPT_OUT": "record or confirm the opt-out; the send guard honours it once recorded",
    "UNCLEAR": "human review required: nothing here can be concluded from this message",
}
#: The ledger's own vocabulary for a reply, kept separate so the intake cannot invent a richer one over HTTP.
TRIAGE_TO_LEDGER_CLASSIFICATION = {
    "INTERESTED": "INTEREST",
    "INFORMATION_REQUEST": "QUESTION",
    "PROPOSAL_QUESTION": "QUESTION",
    "MEETING_REQUEST": "MEETING_REQUEST",
    "NOT_INTERESTED": "REJECTION",
    "OPT_OUT": "UNSUBSCRIBE",
    "UNCLEAR": "OTHER",
}

#: Fields that would let a caller assert something only a human (or a real provider receipt) may claim. They are
#: refused, not ignored: a silently dropped field is a surprise six months later.
FORBIDDEN_FIELDS = {
    "approved", "approval", "approval_ref", "provider_verified", "delivery_verified", "delivered",
    "identity_verified", "outcome", "outcome_status", "proposal_status", "status", "sent", "won",
    "revenue", "revenue_amount", "relationship_state", "next_action", "recorded_by",
}
#: Fields the intake does not understand. They are reported back rather than stored, so a misconfigured adapter
#: shows up in the response instead of quietly losing data.
KNOWN_FIELDS = FORBIDDEN_FIELDS | {
    "provider", "event_id", "occurred_at", "received_at", "channel", "lead_id", "project_id", "outreach_id",
    "conversation_id", "external_message_id", "in_reply_to", "summary", "body", "message_ref",
    "classification", "unsubscribe_confirmed", "idempotency_key", "signature_verified",
}

#: Header an adapter signs its delivery with. The value is ``sha256=<hex of HMAC-SHA256(body bytes)>``.
INBOUND_SIGNATURE_HEADER = "X-Inbound-Signature"
SIGNATURE_SCHEME = "sha256"
SIGNATURE_PREFIX = f"{SIGNATURE_SCHEME}="

def compute_inbound_signature(secret: str, raw_body: bytes | str) -> str:
    """The header value an adapter must send for these exact bytes.

    Signing is not a second login - the operator token already authorizes the caller. It answers a narrower
    question that a token cannot: *which bytes* did the provider actually send? A relay that is trusted to
    forward mail is not thereby trusted to rewrite it.
    """
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
    return SIGNATURE_PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def signature_mismatch(supplied: str) -> str | None:
    """Why this header value cannot be checked at all, or ``None`` when it is well-formed."""
    if not supplied:
        return (f"No {INBOUND_SIGNATURE_HEADER} header was sent, and this instance verifies inbound deliveries "
                "with a signing secret.")
    if not supplied.startswith(SIGNATURE_PREFIX):
        return f"A signature must be sent as '{SIGNATURE_PREFIX}<hex>'; this one is not."
    digest = supplied[len(SIGNATURE_PREFIX):].strip()
    if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
        return f"The digest after '{SIGNATURE_PREFIX}' must be exactly 64 hexadecimal characters."
    return None


def verify_inbound_signature(secret: str, supplied: str, raw_body: bytes | str) -> bool:
    """Compare the supplied digest with one recomputed over the received bytes, in constant time."""
    if signature_mismatch(supplied):
        return False
    return hmac.compare_digest(compute_inbound_signature(secret, raw_body), supplied.strip())


MAX_BODY_CHARS = 20_000
MAX_REF_CHARS = 300
MAX_EVENT_ID_CHARS = 200

#: Transparent phrase rules. Every entry is (category, phrase) and the phrase is stored with the result, so a
#: reader can see exactly why the baseline said what it said.
TRIAGE_RULES: tuple[tuple[str, str], ...] = (
    ("OPT_OUT", "unsubscribe"),
    ("OPT_OUT", "opt out"),
    ("OPT_OUT", "opt-out"),
    ("OPT_OUT", "remove me from your list"),
    ("OPT_OUT", "remove us from your list"),
    ("OPT_OUT", "stop contacting"),
    ("OPT_OUT", "stop emailing"),
    ("OPT_OUT", "do not contact"),
    ("OPT_OUT", "don't contact"),
    ("OPT_OUT", "no further emails"),
    ("MEETING_REQUEST", "come by"),
    ("MEETING_REQUEST", "call us"),
    ("MEETING_REQUEST", "book a"),
    ("MEETING_REQUEST", "schedule a"),
    ("MEETING_REQUEST", "let's meet"),
    ("MEETING_REQUEST", "set up a meeting"),
    ("PROPOSAL_QUESTION", "how much"),
    ("PROPOSAL_QUESTION", "what's the price"),
    ("PROPOSAL_QUESTION", "whats the price"),
    ("PROPOSAL_QUESTION", "quote"),
    ("PROPOSAL_QUESTION", "cost"),
    ("PROPOSAL_QUESTION", "payment terms"),
    ("INFORMATION_REQUEST", "send me"),
    ("INFORMATION_REQUEST", "send us"),
    ("INFORMATION_REQUEST", "more information"),
    ("INFORMATION_REQUEST", "more details"),
    ("INFORMATION_REQUEST", "can you show"),
    ("INFORMATION_REQUEST", "demo"),
    ("INTERESTED", "we'd like"),
    ("INTERESTED", "we would like"),
    ("INTERESTED", "interested"),
    ("INTERESTED", "let's go ahead"),
    ("INTERESTED", "works for us"),
    ("NOT_INTERESTED", "not interested"),
    ("NOT_INTERESTED", "no thank you"),
    ("NOT_INTERESTED", "we already have a website"),
    ("NOT_INTERESTED", "not looking"),
)
#: Phrases that make an opt-out reading *uncertain* - a hedged or partial request is a human's call.
OPT_OUT_HEDGES = ("unless", "except", "but keep", "only about", "this month", "right now", "for now")


class InboundEventError(ValueError):
    """A rejected inbound event. Carries a stable code so a caller can act on it without parsing prose."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", "replace"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:24]


def triage(text: str) -> dict[str, Any]:
    """Sort one message into a category with visible reasons, not a score from nowhere.

    The result states which phrases matched and whether they contradicted each other. It is intentionally not a
    confidence percentage: this repository has no labelled corpus behind a number, and an invented 0.83 would be
    read as evidence.
    """
    haystack = (text or "").lower()
    matched: dict[str, list[str]] = {}
    for category, phrase in TRIAGE_RULES:
        if phrase in haystack:
            matched.setdefault(category, []).append(phrase)
    hedged = [word for word in OPT_OUT_HEDGES if word in haystack]
    if not matched:
        return {"category": "UNCLEAR", "matched_phrases": [], "certainty": "none",
                "contradicts": [], "hedged_by": hedged, "source": "rule_baseline",
                "note": "no phrase rule matched; the baseline does not guess"}
    # Priority when several categories match: an explicit request to stop contact outranks a sale, because the
    # cost of ignoring it is real. A hedge makes the opt-out reading ambiguous instead.
    order = ["OPT_OUT", "NOT_INTERESTED", "MEETING_REQUEST", "PROPOSAL_QUESTION", "INFORMATION_REQUEST", "INTERESTED"]
    category = next(name for name in order if name in matched)
    contradicts = sorted(name for name in matched if name != category)
    certainty = "clear"
    if category == "OPT_OUT" and (hedged or contradicts):
        certainty = "ambiguous"
    elif len(matched) > 1:
        certainty = "mixed"
    return {
        "category": category,
        "matched_phrases": matched[category],
        "certainty": certainty,
        "contradicts": contradicts,
        "hedged_by": hedged,
        "source": "rule_baseline",
        "note": "phrase matching in tracking/inbound.py - not a model, and not a decision",
    }


def normalize_event(payload: dict[str, Any], *, received_at: str | None = None) -> dict[str, Any]:
    """Validate and normalize one inbound payload into the fields the intake actually uses.

    Raises :class:`InboundEventError` with a code for anything ambiguous, forbidden, or unsafe; the caller gets a
    structured refusal rather than a partially stored event.
    """
    if not isinstance(payload, dict):
        raise InboundEventError("The inbound event must be a JSON object.", code="EVENT_INVALID")
    # An empty field means "not supplied", never "the empty string": a relay that leaves a template blank must
    # not end up with a lead id of `""` or a timestamp it cannot parse.
    payload = {key: (None if isinstance(value, str) and not value.strip() else value)
               for key, value in payload.items()}
    forbidden = sorted(FORBIDDEN_FIELDS & set(payload))
    if forbidden:
        raise InboundEventError(
            f"These fields cannot be asserted by an inbound feed and are not accepted at all: {', '.join(forbidden)}. "
            "An inbound event records what the business said; approvals, outcomes, and delivery verification come "
            "from a human or a real provider receipt, never from here.",
            code="INBOUND_FORBIDDEN_FIELD",
        )
    unknown = sorted(set(payload) - KNOWN_FIELDS)
    provider = validate_bounded_text(payload.get("provider") or "GENERIC_WEBHOOK", field="provider", limit=40).upper()
    if provider not in INBOUND_PROVIDERS:
        raise InboundEventError(
            f"Unsupported inbound provider '{provider}'. Contracted adapters: {', '.join(sorted(INBOUND_PROVIDERS))}.",
            code="PROVIDER_UNSUPPORTED",
        )
    channel = payload.get("channel")
    if channel is None:
        raise InboundEventError("A channel is required (EMAIL, SMS, WHATSAPP, ...): the ledger records where a reply arrived.",
                                code="CHANNEL_REQUIRED")
    channel = validate_bounded_text(channel, field="channel", limit=20).upper()
    if channel not in CHANNELS:
        raise InboundEventError(f"Unsupported channel '{channel}'.", code="CHANNEL_UNSUPPORTED")

    event_id = payload.get("event_id") or payload.get("external_message_id")
    if event_id is not None:
        event_id = validate_bounded_text(event_id, field="event_id", limit=MAX_EVENT_ID_CHARS)
    elif INBOUND_PROVIDERS[provider]["stable_event_id"]:
        raise InboundEventError(
            f"'{provider}' is contracted to supply a stable message or event identifier, and none was given. "
            "Send it in `event_id` (or `external_message_id`); without one, a re-delivery cannot be told apart "
            "from a second reply.",
            code="EVENT_ID_REQUIRED",
        )

    body = payload.get("body")
    if body is not None:
        body = str(body)
        if len(body) > MAX_BODY_CHARS:
            raise InboundEventError(
                f"`body` is {len(body)} characters; the intake accepts at most {MAX_BODY_CHARS}. Store the message "
                "where it belongs and pass a bounded `message_ref` plus a short `summary`.",
                code="EVENT_BODY_TOO_LARGE",
            )
    summary = payload.get("summary")
    if summary is not None:
        summary = validate_bounded_text(summary, field="summary", limit=1200)
    if not body and not summary:
        raise InboundEventError(
            "An inbound event needs `body` or `summary`: a record with nothing said in it is not a response.",
            code="EVENT_EMPTY",
        )
    for field in ("summary", "body", "message_ref"):
        value = payload.get(field)
        if value and find_secret_shaped_text(str(value)):
            raise InboundEventError(
                f"`{field}` looks like it carries a credential or connection string. A message excerpt belongs in "
                "the ledger without secrets; reference where the full text lives instead.",
                code="DETAIL_MAY_CONTAIN_SECRET",
            )
    message_ref = payload.get("message_ref")
    if message_ref is not None:
        message_ref = validate_bounded_text(message_ref, field="message_ref", limit=MAX_REF_CHARS)

    received = normalize_datetime(received_at or now(), field="received_at")
    # The event's own time is preserved and kept distinct from the moment we accepted it; a caller that omits it
    # gets the application clock *labelled* as such rather than a silently assumed send time.
    supplied = payload.get("occurred_at") or payload.get("received_at")
    timestamp_source = "provider"
    timestamp_claim = None
    if supplied:
        try:
            occurred = normalize_datetime(supplied, field="occurred_at")
        except LedgerError as error:
            # The ledger's own rule is that a timestamp in the future is not history. A provider whose clock
            # runs a minute fast is ordinary, so this is not a reason to lose a real reply: the claim is kept
            # verbatim on the intake row, labelled as unaccepted, and the record is timestamped at receipt.
            if "future" not in str(error).lower():
                raise InboundEventError(str(error), code="EVENT_TIMESTAMP_INVALID") from None
            occurred = received
            timestamp_source = "rejected_future_claim"
            timestamp_claim = str(supplied)[:40]
    else:
        occurred = received
        timestamp_source = "fallback_received"

    linkage = {
        "lead_id": payload.get("lead_id"),
        "project_id": payload.get("project_id"),
        "outreach_id": payload.get("outreach_id"),
        "conversation_id": payload.get("conversation_id"),
        "external_message_id": payload.get("external_message_id"),
        "in_reply_to": payload.get("in_reply_to"),
    }
    for key, value in list(linkage.items()):
        if value is None or str(value).strip() == "":
            linkage[key] = None
        elif key in {"lead_id", "project_id", "outreach_id", "in_reply_to"}:
            try:
                linkage[key] = validate_record_id(value, kind=key)
            except ValueError as error:
                raise InboundEventError(f"'{value}' is not a valid {key}: {error}", code="LINKAGE_ID_INVALID") from None
        else:
            linkage[key] = validate_bounded_text(value, field=key, limit=MAX_REF_CHARS)

    hint = payload.get("classification")
    classification_hint = None
    if hint is not None:
        classification_hint = validate_bounded_text(hint, field="classification", limit=30).upper()

    signature = {
        "verified": bool(payload.get("signature_verified")),
        "scheme": SIGNATURE_SCHEME,
        "reason": ("a holder of the shared signing secret signed these exact bytes"
                   if payload.get("signature_verified") else
                   "no signing secret is configured on this instance, so the bearer token is the only "
                   "authentication this record can claim"),
    }
    text = f"{summary or ''}\n{body or ''}"
    baseline = triage(text)
    body_sha = _sha(body or "") if body else None
    # A stable provider id *is* the event's identity: the same id re-delivered is the same event, whatever else
    # the payload says, so the timestamp never enters that key. Without one, identity can only be inferred from
    # content and the minute of receipt - enough to squash a double-submit, and labelled as weaker.
    if event_id:
        key = payload.get("idempotency_key") or _sha(provider, str(event_id))
    else:
        key = payload.get("idempotency_key") or _sha(provider, channel, str(linkage["lead_id"] or ""),
                                                      occurred[:16], body_sha or (summary or "")[:200])
    return {
        "provider": provider,
        "event_id": event_id,
        "channel": channel,
        "occurred_at": occurred,
        "received_at": received,
        "timestamp_source": timestamp_source,
        "timestamp_claim": timestamp_claim,
        "summary": summary,
        "body_chars": len(body) if body else 0,
        "body_sha256": body_sha,
        "excerpt": redact_contact_details((body or summary or "")[:MAX_QUOTE_CHARS]) if (body or summary) else None,
        "message_ref": message_ref,
        "triage": baseline,
        "classification_hint": classification_hint,
        "unsubscribe_confirmed": bool(payload.get("unsubscribe_confirmed")),
        "ignored_fields": unknown,
        "signature": signature,
        "dedupe_key": f"inbound:{key}"[:120],
        "idempotency": "provider_event_id" if event_id else "derived_from_content",
        **linkage,
    }


class InboundIntake:
    """Turn a normalized inbound event into a ledger response record - or an honest refusal."""

    def __init__(self, ledger: EngagementLedger) -> None:
        self.ledger = ledger
        self.store = ledger.store
        self.store.state.setdefault("inbound_events", {})

    # ------------------------------------------------------------------ intake

    def process(self, payload: dict[str, Any], *, received_at: str | None = None,
                source: str = "api-inbound") -> dict[str, Any]:
        """Process one event. Idempotent on the provider's identity, retryable after a failure.

        Two kinds of refusal, on purpose. A payload that breaks the *contract* (a missing event id where the
        provider has one, a body that is too large, a field that would assert an approval) raises
        :class:`InboundEventError` and stores nothing: the caller can fix it and resend, and a bad integration
        cannot fill the state file with rejected junk. A valid event that could not be *processed* here (an
        unknown lead, a ledger refusal, a storage fault) is recorded as ``FAILED`` so it shows up in the
        operator's backlog and can be retried by id.
        """
        event = normalize_event(payload, received_at=received_at)
        existing = self.store.state["inbound_events"].get(event["dedupe_key"])
        if (isinstance(existing, dict)
                and existing.get("disposition") not in {None, "FAILED", "UNMATCHED"}):
            # Same event, already dealt with. The caller is told so, and the original record is returned -
            # unchanged, so a re-delivery cannot edit history. An UNMATCHED or FAILED row is the exception:
            # re-sending the same event id with a `lead_id` (or after a storage fault) is how that row gets
            # resolved, and refusing it would strand the message in the backlog forever.
            return {**existing, "duplicate": True, "attempts": int(existing.get("attempts") or 1)}
        lead_id, linkage_basis, link_error = self._resolve_lead(event)
        if link_error is not None:
            return self._record(event, disposition=link_error[0], error_code=link_error[1],
                                error_message=link_error[2], lead_id=None, source=source)
        # A supplied reference is checked whether or not it was the thing that identified the lead. A row that
        # points at a draft or project which does not exist is a broken link in the audit trail, not a detail:
        # it is better refused here than quietly recorded with a dangling id.
        for field, collection, code in (("outreach_id", "outreach", "UNKNOWN_OUTREACH"),
                                        ("project_id", "projects", "UNKNOWN_PROJECT")):
            if event[field] and not isinstance((self.store.state.get(collection) or {}).get(event[field]), dict):
                return self._record(event, disposition="FAILED", error_code=code,
                                    error_message=f"{field} '{event[field]}' is not a recorded reference; the "
                                                  "event was not recorded against the lead.",
                                    lead_id=lead_id, source=source)
        actions: list[str] = []
        try:
            with self.ledger._transaction(*self.ledger.OWNED_COLLECTIONS, "inbound_events", "outreach"):  # noqa: SLF001
                response = None
                if lead_id is not None:
                    response = self.ledger.record_response(
                        lead_id,
                        channel=event["channel"],
                        summary=event["summary"] or f"Inbound {event['provider']} message ({event['body_chars']} chars).",
                        quote=event["excerpt"],
                        received_at=event["occurred_at"],
                        evidence_kind="CUSTOMER_MESSAGE",
                        outreach_id=event["outreach_id"],
                        project_id=event["project_id"] if lead_id is None else None,
                        idempotency_key=f"{event['dedupe_key']}:response",
                        provider=event["provider"].lower(),
                        external_message_id=event["event_id"],
                        conversation_id=event["conversation_id"],
                        in_reply_to=event["in_reply_to"],
                        classification=TRIAGE_TO_LEDGER_CLASSIFICATION[event["triage"]["category"]],
                        needs_human_review=True,
                    )
                    actions.append(f"recorded as {response['engagement_id']}")
                record = self._row(event, lead_id=lead_id, linkage_basis=linkage_basis, source=source,
                                   engagement_id=None if response is None else response["engagement_id"])
                if lead_id is None:
                    record["disposition"] = "UNMATCHED"
                    record["next_action"] = ("link this event to its lead (the operator knows who wrote in) and "
                                             "resubmit with `lead_id`; nothing is guessed here. It stays listed "
                                             "by: python3 -m tracking.cli inbound-pending")
                    actions.append("parked as unmatched")
                else:
                    record["disposition"] = "RECORDED" if response else "NOTED"
                    record["next_action"] = TRIAGE_NEXT_ACTIONS[event["triage"]["category"]]
                opt_out = self._opt_out_decision(event, lead_id)
                record["opt_out"] = opt_out
                if opt_out["apply"] and lead_id is not None:
                    block = self.ledger.record_opt_out(
                        lead_id,
                        channel=event["channel"] if opt_out["scope"] == "channel" else None,
                        reason=redact_contact_details(opt_out["reason"])[:400],
                        evidence_note=event["excerpt"] or opt_out["reason"],
                        occurred_at=event["occurred_at"],
                    )
                    record["disposition"] = "OPT_OUT_RECORDED"
                    record["opt_out"]["block_id"] = block.get("block_id")
                    record["opt_out"]["cancelled_follow_ups"] = block.get("cancelled_follow_ups") or []
                    actions.append(f"opt-out applied ({block.get('block_id')})")
                elif opt_out["flagged"]:
                    record["disposition"] = "OPT_OUT_UNCERTAIN"
                    actions.append("flagged for review")
                self.store.state["inbound_events"][event["dedupe_key"]] = record
                self._supersede_earlier_unmatched(event, record)
                self.ledger._commit()  # noqa: SLF001 - one save for the response, the intake row, and any opt-out
            record["processing"] = actions
            record["duplicate"] = False
            return record
        except LedgerError as error:
            return self._record(event, disposition="FAILED", error_code=error.code,
                                error_message=str(error)[:500], lead_id=lead_id, source=source)

    # ------------------------------------------------------------------ helpers

    def _resolve_lead(self, event: dict[str, Any]) -> tuple[str | None, str | None, tuple[str, str, str] | None]:
        """Follow only recorded references: the named lead, its outreach draft, its project, or the same thread."""
        if event["lead_id"]:
            lead = (self.store.state.get("leads") or {}).get(event["lead_id"])
            if not isinstance(lead, dict):
                # A named-but-unknown lead is a caller fault and is refused outright: parking it as "unmatched"
                # would hide a broken integration inside a backlog of ordinary-looking messages.
                return None, None, ("FAILED", "UNKNOWN_LEAD",
                                    f"lead_id '{event['lead_id']}' is not a tracked lead; the event was not recorded.")
            return event["lead_id"], "lead_id", None
        if event["outreach_id"]:
            outreach = (self.store.state.get("outreach") or {}).get(event["outreach_id"])
            if not isinstance(outreach, dict):
                return None, None, ("FAILED", "UNKNOWN_OUTREACH",
                                    f"outreach_id '{event['outreach_id']}' is not a recorded draft.")
            return str(outreach.get("lead_id") or ""), "outreach_id", None
        if event["project_id"]:
            project = (self.store.state.get("projects") or {}).get(event["project_id"])
            if not isinstance(project, dict):
                return None, None, ("FAILED", "UNKNOWN_PROJECT",
                                    f"project_id '{event['project_id']}' is not a tracked project.")
            return str(project.get("lead_id") or ""), "project_id", None
        if event["conversation_id"]:
            for row in (self.store.state.get("engagements") or {}).values():
                if isinstance(row, dict) and row.get("conversation_id") == event["conversation_id"] and row.get("lead_id"):
                    return str(row["lead_id"]), "conversation_id", None
        return None, None, None

    def _opt_out_decision(self, event: dict[str, Any], lead_id: str | None) -> dict[str, Any]:
        """Decide whether this event *is* a request to stop being contacted, and act only when it clearly is."""
        baseline = event["triage"]
        provider_confirmed = event["unsubscribe_confirmed"]
        category_is_opt_out = baseline["category"] == "OPT_OUT"
        if lead_id is None:
            return {"apply": False, "flagged": False,
                    "reason": None, "basis": "not evaluated - the event is not linked to a lead"}
        if not category_is_opt_out and not provider_confirmed:
            return {"apply": False, "flagged": False, "reason": None, "basis": "no opt-out signal"}
        reason = (f"{event['provider']} event said: {event['excerpt'] or event['summary'] or 'no text recorded'}"[:400])
        if provider_confirmed and not category_is_opt_out:
            return {"apply": True, "flagged": False, "scope": "channel", "reason": f"provider-confirmed unsubscribe. {reason}",
                    "basis": "unsubscribe_confirmed (the provider reported the customer's own action)"}
        if category_is_opt_out and baseline["certainty"] == "clear":
            return {"apply": True, "flagged": False, "scope": "all",
                    "reason": f"the customer asked to stop being contacted. {reason}"[:400],
                    "basis": f"rule baseline matched {', '.join(baseline['matched_phrases']) or 'an opt-out phrase'}",
                    "note": "a written request not to be contacted is honoured on every channel, so the block is recorded "
                            "for all channels and future follow-ups of any kind are cancelled"}
        return {
            "apply": False, "flagged": True, "scope": None, "reason": None,
            "basis": f"opt-out language is hedged or contradicted ({', '.join(baseline.get('hedged_by') or baseline.get('contradicts') or ['unclear'])})",
            "note": "a human decides whether this was a request to stop; a guessed block silences a "
                     "conversation nobody asked to end, and a missed one keeps contacting someone who did",
        }

    def _row(self, event: dict[str, Any], *, lead_id: str | None, linkage_basis: str | None, source: str,
             engagement_id: str | None) -> dict[str, Any]:
        prior = self.store.state["inbound_events"].get(event["dedupe_key"]) or {}
        return {
            "dedupe_key": event["dedupe_key"],
            "provider": event["provider"],
            "provider_event_id": event["event_id"],
            "idempotency": event["idempotency"],
            "channel": event["channel"],
            "event_occurred_at": event["occurred_at"],
            "event_timestamp_source": event["timestamp_source"],
            "event_timestamp_claim": event["timestamp_claim"],
            "received_at": event["received_at"],
            "processed_at": now(),
            "lead_id": lead_id,
            "project_id": event["project_id"],
            "outreach_id": event["outreach_id"],
            "conversation_id": event["conversation_id"],
            "engagement_id": engagement_id,
            "linkage_basis": linkage_basis,
            "message_ref": event["message_ref"],
            "body_chars": event["body_chars"],
            "body_sha256": event["body_sha256"],
            "excerpt": event["excerpt"],
            "triage": event["triage"],
            "classification_hint": event["classification_hint"],
            "classification_agrees": (
                None if not event["classification_hint"]
                else event["classification_hint"] == TRIAGE_TO_LEDGER_CLASSIFICATION[event["triage"]["category"]]
            ),
            "ignored_fields": event["ignored_fields"],
            "signature": event["signature"],
            "attempts": int(prior.get("attempts") or 0) + 1,
            "source": source,
            "data_classification": self.ledger._classification(lead_id) if lead_id else "TEST_FIXTURE",  # noqa: SLF001
            "superseded_by": None,
            "meaning": "an intake record of what arrived; the ledger response record is the business history",
        }

    def _record(self, event: dict[str, Any], *, disposition: str, error_code: str | None,
                error_message: str | None, lead_id: str | None, source: str) -> dict[str, Any]:
        """Persist a non-ledger outcome (unmatched, refused, failed) so the operator can see it.

        Deliberately written *outside* the ledger transaction: a refusal must survive the rollback of everything
        else, and a durable failure record is what makes a retry safe instead of guesswork.
        """
        record = self._row(event, lead_id=lead_id, linkage_basis=None, source=source, engagement_id=None)
        record["disposition"] = disposition
        record["error_code"] = error_code
        record["error"] = error_message
        record["next_action"] = TRIAGE_NEXT_ACTIONS["UNCLEAR"] if disposition == "FAILED" else record.get("next_action")
        self.store.state["inbound_events"][event["dedupe_key"]] = record
        self.store._save()  # noqa: SLF001
        return {**record, "duplicate": False}

    def _supersede_earlier_unmatched(self, event: dict[str, Any], record: dict[str, Any]) -> None:
        """When a parked message is later linked, say so on the old row instead of leaving two live items."""
        if record.get("disposition") not in {"RECORDED", "OPT_OUT_RECORDED"}:
            return
        for key, prior in self.store.state["inbound_events"].items():
            if key == event["dedupe_key"] or not isinstance(prior, dict):
                continue
            if prior.get("disposition") != "UNMATCHED":
                continue
            same_thread = (event.get("conversation_id") and prior.get("conversation_id") == event.get("conversation_id")) or (
                event.get("event_id") and prior.get("provider_event_id") == event.get("event_id"))
            if same_thread:
                prior["superseded_by"] = event["dedupe_key"]
                prior["updated_at"] = now()

    # ------------------------------------------------------------------ reads

    #: What a bounded read of one intake event carries. Deliberately no body text: an excerpt, a hash, and
    #: the ids a human needs to open the original in the provider.
    PENDING_FIELDS = ("dedupe_key", "provider", "provider_event_id", "channel", "disposition", "error_code",
                      "lead_id", "engagement_id", "received_at", "excerpt", "next_action")

    def pending(self, *, dispositions: tuple[str, ...] = ("UNMATCHED", "FAILED", "OPT_OUT_UNCERTAIN"),
                 limit: int = 25) -> dict[str, Any]:
        """What a human has to look at in the intake, oldest first, bounded."""
        wanted = set(dispositions)
        rows = [
            row for row in self.store.state["inbound_events"].values()
            if isinstance(row, dict) and row.get("disposition") in wanted and not row.get("superseded_by")
        ]
        rows.sort(key=lambda row: str(row.get("received_at") or ""))
        counts: dict[str, int] = {}
        for row in self.store.state["inbound_events"].values():
            if isinstance(row, dict):
                key = str(row.get("disposition"))
                counts[key] = counts.get(key, 0) + 1
        bounded = max(1, min(int(limit or 25), 200))
        return {
            "as_of": now(),
            "count": min(len(rows), bounded),
            "total_matching": len(rows),
            "counts_by_disposition": counts,
            "items": [{key: row.get(key) for key in self.PENDING_FIELDS} for row in rows[:bounded]],
            "meaning": "unmatched and failed events are intake problems, not prospect behaviour: nothing here "
                       "changes a lead's history or an outcome because a message could not be routed",
        }

    def operator_backlog(self, *, limit: int = 25) -> dict[str, Any]:
        """Every inbound record a person has to resolve, in one bounded read.

        Two kinds of gap, kept apart on purpose. ``pending()`` covers events this side could not route or
        could not persist; the extra class here is an event that *was* recorded but whose triage is ambiguous
        or contradicted - a real reply whose meaning is not settled. Neither is an alarm about the business,
        and neither changes a stage: the item says what a human must look at.
        """
        bounded = max(1, min(int(limit or 25), 200))
        pending = self.pending(limit=bounded)
        unclear = [
            row for row in self.store.state["inbound_events"].values()
            if isinstance(row, dict) and row.get("disposition") in {"RECORDED", "NOTED", "OPT_OUT_RECORDED"}
            and ((row.get("triage") or {}).get("certainty") in {"none", "ambiguous", "mixed"}
                 # A caller that asserted a classification the rules contradict needs a human too: the hint
                 # is kept as a hint precisely so that disagreement stays visible instead of being smoothed over.
                 or row.get("classification_agrees") is False)
        ]
        unclear.sort(key=lambda row: str(row.get("received_at") or ""))
        items = []
        for row in unclear[:bounded]:
            item = {**{key: row.get(key) for key in self.PENDING_FIELDS},
                    "category": (row.get("triage") or {}).get("category"),
                    "certainty": (row.get("triage") or {}).get("certainty"),
                    "hint": row.get("classification_hint"),
                    "hint_disagrees": row.get("classification_agrees") is False}
            item["error_code"] = item["error_code"] or (
                "TRIAGE_HINT_CONTRADICTED" if item["hint_disagrees"] else "TRIAGE_UNCERTAIN")
            items.append(item)
        return {
            "as_of": now(),
            "unmatched": pending["counts_by_disposition"].get("UNMATCHED", 0),
            "failed": pending["counts_by_disposition"].get("FAILED", 0),
            "opt_out_uncertain": pending["counts_by_disposition"].get("OPT_OUT_UNCERTAIN", 0),
            "triage_uncertain": len(unclear),
            "total_matching": pending["total_matching"] + len(unclear),
            "intake_items": pending["items"],
            "triage_items": items,
            "meaning": "the intake could not finish these alone: a message it could not match to a lead, an event "
                       "that failed to persist, an opt-out it is not sure about, or a reply whose meaning is "
                       "contested. None of them is evidence about the prospect.",
        }
