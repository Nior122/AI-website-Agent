"""Persistent prospect-engagement and customer-outcome ledger.

This is the tracking layer for everything that happens *after* a message is prepared: what was sent, what came
back, when a follow-up is due, where a proposal stands, what the prospect decided, and how the opportunity
ended. It deliberately cannot speak, deploy, approve, or build anything - it records what an operator says
happened, together with the evidence they attach, and refuses to dress a weaker fact up as a stronger one.

Truth rules the code enforces rather than merely documents:

* an outreach draft is ``AWAITING_REVIEW``/``READY_TO_SEND`` until someone reports an actual send, and a
  reported send is stored as ``SENT_REPORTED`` with ``provider_verified: false`` - this repository has no
  delivery receipt to verify against, so it never writes "delivered";
* a proposal exists before it is presented, and ``PRESENTED`` requires the operator to say when and how;
* silence is never a decision: nothing here changes an outcome because time passed, and ``due_follow_ups``
  reports overdue work without mutating it;
* an outcome that claims something a customer said or agreed needs evidence, and ``WON`` needs a
  contract/invoice/payment reference as well;
* a recorded opt-out cancels open follow-ups for that channel and blocks new ones, including sends;
* a failed send is recorded as a failure: it never reads as a contact, and only a real provider receipt (which
  this repository cannot read) could ever move a record from "reported by an operator" to "accepted" or
  "delivered";
* the relationship stage a lead is at is not a free-text field someone sets: it is derived from the records above
  it, and a human can only move it by handing over the record that proves the step;
* free text is redacted of email addresses and phone numbers on the way in, because the ledger's subject is
  the *business*, not a person's contact details.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from typing import Any
import copy
import hashlib
import re

from tracking.store import next_numeric_id, now, validate_bounded_text, validate_record_id

#: Collections this module owns. Kept in sync with ``tracking/database.py`` (persistence) and
#: ``TrackingStore._empty_state`` by ``tests/test_phase46_engagement_ledger.py``.
ENGAGEMENT_COLLECTIONS = ("engagements", "proposals", "follow_ups", "outcomes", "contact_blocks", "relationships")

EVENT_KINDS = {
    "OUTREACH_PREPARED", "OUTREACH_SENT", "RESPONSE_RECEIVED", "FOLLOW_UP_SCHEDULED", "FOLLOW_UP_COMPLETED",
    "FOLLOW_UP_CANCELLED", "PROPOSAL_CREATED", "PROPOSAL_PRESENTED", "PROPOSAL_DECISION", "OUTCOME_RECORDED",
    "OUTREACH_SEND_FAILED", "RELATIONSHIP_STAGE_CHANGED",
    "OPT_OUT_RECORDED", "OPT_OUT_REVOKED", "NOTE",
}
DIRECTIONS = {"OUTBOUND", "INBOUND", "INTERNAL"}
CHANNELS = {"EMAIL", "WHATSAPP", "INSTAGRAM", "FACEBOOK", "LINKEDIN", "SMS", "PHONE", "IN_PERSON", "OTHER", "OTHER_DIGITAL"}
EVIDENCE_KINDS = {"CUSTOMER_MESSAGE", "EMAIL", "CHAT", "CALL_NOTE", "MEETING", "CONTRACT", "INVOICE", "PAYMENT", "SCREENSHOT", "URL", "OPERATOR_NOTE", "OTHER"}

PROPOSAL_STATUSES = {"DRAFT", "READY_FOR_PRESENTATION", "PRESENTED", "ACCEPTED", "DECLINED", "WITHDRAWN", "SUPERSEDED"}
PROPOSAL_TRANSITIONS = {
    "DRAFT": {"READY_FOR_PRESENTATION", "WITHDRAWN", "SUPERSEDED"},
    "READY_FOR_PRESENTATION": {"PRESENTED", "WITHDRAWN", "SUPERSEDED"},
    "PRESENTED": {"ACCEPTED", "DECLINED", "WITHDRAWN", "SUPERSEDED"},
    "ACCEPTED": set(),
    "DECLINED": set(),
    "WITHDRAWN": set(),
    "SUPERSEDED": set(),
}
#: An outcome is one per lead: the current state plus the reason and evidence that produced it.
OUTCOME_STATUSES = {"UNRESOLVED", "INTERESTED", "FOLLOW_UP_REQUESTED", "NEGOTIATING", "DECLINED", "LOST", "WON", "OPTED_OUT", "NO_RESPONSE", "PAUSED"}
OUTCOME_TRANSITIONS = {
    "UNRESOLVED": {"INTERESTED", "FOLLOW_UP_REQUESTED", "NEGOTIATING", "DECLINED", "LOST", "OPTED_OUT", "NO_RESPONSE", "PAUSED", "WON"},
    "INTERESTED": {"NEGOTIATING", "FOLLOW_UP_REQUESTED", "DECLINED", "LOST", "WON", "PAUSED", "OPTED_OUT", "NO_RESPONSE", "UNRESOLVED"},
    "FOLLOW_UP_REQUESTED": {"INTERESTED", "NEGOTIATING", "DECLINED", "LOST", "PAUSED", "OPTED_OUT", "NO_RESPONSE", "UNRESOLVED"},
    "NEGOTIATING": {"WON", "DECLINED", "LOST", "PAUSED", "OPTED_OUT", "INTERESTED", "UNRESOLVED"},
    "DECLINED": {"INTERESTED", "NEGOTIATING", "LOST", "WON", "UNRESOLVED", "PAUSED"},
    "LOST": {"INTERESTED", "NEGOTIATING", "UNRESOLVED", "WON"},
    "OPTED_OUT": {"UNRESOLVED", "INTERESTED"},
    "NO_RESPONSE": {"INTERESTED", "FOLLOW_UP_REQUESTED", "NEGOTIATING", "DECLINED", "LOST", "WON", "UNRESOLVED", "PAUSED", "OPTED_OUT"},
    "PAUSED": {status for status in OUTCOME_STATUSES if status != "PAUSED"},
    "WON": {"LOST", "DECLINED", "UNRESOLVED"},
}
#: Statuses that assert something a customer said or agreed to - each needs an evidence item.
#: "No reply so far" is an observation about our own inbox, not a customer statement, so it needs no
#: evidence - and it must never be derived from the passage of time by this module.
EVIDENCE_REQUIRED_OUTCOMES = OUTCOME_STATUSES - {"UNRESOLVED", "NO_RESPONSE"}
#: Only these evidence kinds can support "the customer agreed", because they are documents or the customer's
#: own words rather than an operator's optimism.
WON_EVIDENCE_KINDS = {"CONTRACT", "INVOICE", "PAYMENT", "CUSTOMER_MESSAGE"}
OUTCOME_DECISIVE = {"WON", "LOST", "DECLINED", "OPTED_OUT"}

#: What an operator may report about a send, in increasing order of strength. `REPORTED_SENT` and `FAILED` are
#: statements a human can make; the last two need a provider receipt and are therefore refused until an actual
#: provider integration can supply one - which is the whole difference between "the API took my request" and
#: "the message arrived".
SEND_RESULTS = {"REPORTED_SENT", "FAILED", "ACCEPTED_BY_PROVIDER", "VERIFIED_DELIVERED"}
PROVIDER_ONLY_RESULTS = {"ACCEPTED_BY_PROVIDER", "VERIFIED_DELIVERED"}
#: Planned is what an approved draft looks like before anyone acts; attempted is an action with no confirmed
#: result. Nothing in this module writes a delivered status without a provider receipt.
DELIVERY_STATUSES = {"PLANNED", "ATTEMPTED", "REPORTED_BY_OPERATOR", "FAILED_REPORTED", "ACCEPTED_BY_PROVIDER", "VERIFIED_DELIVERED"}
#: An inbound message can be sorted into one of these by the person reading it. No classifier exists in this
#: repository, so a classification is always `operator_asserted` and never machine-generated.
RESPONSE_CLASSIFICATIONS = {"QUESTION", "INTEREST", "REJECTION", "UNSUBSCRIBE", "REFERRAL", "MEETING_REQUEST", "AUTO_REPLY", "SPAM", "OTHER"}
#: A failed send still consumed an attempt, so it counts towards the contact budget; that is deliberate - a
#: retry loop that never trips the cap is how a prospect gets spammed by accident.
SEND_ATTEMPT_KINDS = {"OUTREACH_SENT", "OUTREACH_SEND_FAILED"}

#: Relationship stages carry `tracking.store.STAGES` past the point a project record stops (a project is
#: `READY_FOR_OUTREACH` and then stays there; the business relationship continues). The names deliberately reuse
#: the words the rest of the repository uses instead of inventing a parallel language.
RELATIONSHIP_STAGES = (
    "NOT_CONTACTED", "OUTREACH_APPROVED", "OUTREACH_SENT", "RESPONDED", "FOLLOW_UP_SCHEDULED",
    "PROPOSAL_OPEN", "PROPOSAL_PRESENTED", "NEGOTIATING", "WON", "LOST", "OPTED_OUT", "PAUSED",
)
RELATIONSHIP_TRANSITIONS = {
    "NOT_CONTACTED": {"OUTREACH_APPROVED", "PAUSED", "OPTED_OUT", "LOST"},
    "OUTREACH_APPROVED": {"OUTREACH_SENT", "PAUSED", "OPTED_OUT", "LOST", "NOT_CONTACTED"},
    "OUTREACH_SENT": {"RESPONDED", "FOLLOW_UP_SCHEDULED", "OPTED_OUT", "LOST", "PAUSED", "PROPOSAL_OPEN"},
    "RESPONDED": {"OUTREACH_SENT", "FOLLOW_UP_SCHEDULED", "PROPOSAL_OPEN", "PROPOSAL_PRESENTED", "NEGOTIATING", "WON", "LOST", "OPTED_OUT", "PAUSED"},
    "FOLLOW_UP_SCHEDULED": {"RESPONDED", "OUTREACH_SENT", "PROPOSAL_OPEN", "OPTED_OUT", "LOST", "PAUSED", "NEGOTIATING"},
    "PROPOSAL_OPEN": {"PROPOSAL_PRESENTED", "NEGOTIATING", "WON", "LOST", "OPTED_OUT", "PAUSED"},
    "PROPOSAL_PRESENTED": {"NEGOTIATING", "WON", "LOST", "OPTED_OUT", "PAUSED", "PROPOSAL_OPEN"},
    "NEGOTIATING": {"WON", "LOST", "OPTED_OUT", "PAUSED", "PROPOSAL_PRESENTED"},
    "WON": set(),
    "LOST": {"NOT_CONTACTED", "RESPONDED", "PAUSED"},
    "OPTED_OUT": {"PAUSED"},
    "PAUSED": set(RELATIONSHIP_STAGES) - {"PAUSED"},
}
#: Order used to decide when a derived stage may advance a recorded one (never the reverse).
_STAGE_RANK = {
    "NOT_CONTACTED": 0, "OUTREACH_APPROVED": 1, "OUTREACH_SENT": 2, "FOLLOW_UP_SCHEDULED": 2,
    "RESPONDED": 3, "PROPOSAL_OPEN": 4, "PROPOSAL_PRESENTED": 5, "OPTED_OUT": 6, "NEGOTIATING": 6,
    "PAUSED": 6, "WON": 7, "LOST": 7,
}
#: Which ledger record proves a stage. A stage listed here cannot be asserted by hand until that record exists,
#: which is what stops the relationship row from becoming a second, freer source of truth.
STAGE_CORROBORATION = {
    "OUTREACH_APPROVED": "an outreach draft for this lead in status READY_TO_SEND",
    "OUTREACH_SENT": "a reported send (OUTREACH_SENT event) for this lead",
    "RESPONDED": "an inbound RESPONSE_RECEIVED event for this lead",
    "FOLLOW_UP_SCHEDULED": "an open follow-up for this lead",
    "PROPOSAL_OPEN": "a proposal for this lead",
    "PROPOSAL_PRESENTED": "a proposal for this lead in PRESENTED",
    "NEGOTIATING": "an outcome record with status NEGOTIATING",
    "WON": "an outcome record with status WON and its contract/invoice/payment evidence",
    "LOST": "an outcome record with status LOST or DECLINED",
    "OPTED_OUT": "an active contact block for this lead",
    "PAUSED": "a stated reason",
}
#: An operator-supplied detail must not carry a credential. These shapes are refused, not silently scrubbed,
#: because silently removing text from an audit record is its own kind of loss.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:|bearer\s+[a-z0-9._\-]{8,}|x-api-key|api[_-]?key\s*[:=]|password\s*[:=]|secret\s*[:=]|token\s*[:=])"),
    re.compile(r"\b(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|npm_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,})\b"),
    re.compile(r"(?i)(postgres(ql)?|mysql|mongodb(\+srv)?|redis)://[^\s]+:[^\s]+@"),
)
MAX_DETAIL_CHARS = 500
MAX_SUMMARY_CHARS = 1200
MAX_NOTE_CHARS = 2000
MAX_QUOTE_CHARS = 300
#: Contact budget per lead per channel. A send beyond this needs a new outreach record and a human decision,
#: which keeps "one more nudge" from becoming an uncounted habit.
MAX_SENDS_PER_LEAD_PER_CHANNEL = 3

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_PATTERN = re.compile(r"(?:(?:\+|00)\d[\d\s\-().]{7,}|\b(?:\(\d{3,4}\)|\d{3,4})[-.\s]\d{3}[-.\s]\d{4}\b)")
_URL_PATTERN = re.compile(r"^https?://[^\s<>\"]+$", re.I)


class LedgerError(ValueError):
    """A refused ledger operation. Carries a stable code so CLI/API output stays machine-readable."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def redact_contact_details(value: str | None) -> str | None:
    """Remove email addresses and phone numbers from operator-supplied text.

    The engagement history is about a business's progress, and storing a person's private contact string in a
    durable, shareable audit file buys nothing. The redaction is applied on write, and the pattern set is
    deliberately narrow (an email shape and an international/NSN phone shape) rather than a general scrubber.
    """
    if value is None:
        return None
    text = str(value)
    text = _EMAIL_PATTERN.sub("[redacted:email]", text)
    text = _PHONE_PATTERN.sub("[redacted:phone]", text)
    return text


def find_secret_shaped_text(value: str | None) -> str | None:
    """Return the first credential-shaped fragment in text, or None.

    A ledger record outlives the incident it describes, so a paste that carries a bearer token, a DSN, or a JWT
    would leak the secret into every future export of the ledger. Refusing the write is chosen over silently
    rewriting it: an audit record whose text quietly changed is worse than a visible refusal to record it.
    """
    if not value:
        return None
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)[:60]
    return None


def sanitize_detail(value: Any, *, field: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """Bounded, contact-redacted, secret-refused free text for failure and provider details."""
    text = validate_bounded_text(value, field=field, limit=limit * 3)
    found = find_secret_shaped_text(text)
    if found is not None:
        raise LedgerError(
            f"'{field}' looks like it contains a credential or connection string ('{found}'). Store a reference "
            "to where the detail lives instead; ledger records are durable and shareable.",
            code="DETAIL_MAY_CONTAIN_SECRET",
        )
    return (redact_contact_details(text) or "")[:limit]


def normalize_datetime(
    value: Any,
    *,
    field: str,
    allow_past: bool = True,
    end_of_day: bool = False,
    allow_future: bool = False,
) -> str:
    """Accept ``YYYY-MM-DD`` or an ISO timestamp and return a UTC ISO-8601 string with ``Z``."""
    text = validate_bounded_text(value, field=field, limit=40)
    candidate = text.replace("Z", "+00:00")
    parsed: datetime | None = None
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    if date_only:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59)
    else:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            raise LedgerError(f"'{field}' must be a date (YYYY-MM-DD) or an ISO-8601 timestamp.", code="TIMESTAMP_INVALID") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    normalized = parsed.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    moment = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if moment > datetime.now(timezone.utc) and not allow_future:
        raise LedgerError(f"'{field}' is in the future; the ledger records what already happened.", code="TIMESTAMP_IN_FUTURE")
    if not allow_past:
        today = datetime.now(timezone.utc).date()
        if moment.date() < today:
            raise LedgerError(
                f"'{field}' is in the past; pass --allow-past-due to backfill a recorded date deliberately.",
                code="TIMESTAMP_IN_PAST",
            )
    return normalized


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _ledger_transaction(func):
    """Wrap a public write so its several in-memory edits become one durable save - or none.

    The snapshot is of the collections the ledger owns plus the outreach records it annotates, so a refusal half
    way through (an opt-out written, the follow-up cancel refused) cannot leave the state file agreeing with only
    part of the operation.
    """

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        with self._transaction(*ENGAGEMENT_COLLECTIONS, "outreach"):
            return func(self, *args, **kwargs)

    return wrapper


class EngagementLedger:
    """Write-only-by-invitation engagement records over ``TrackingStore`` state."""

    def __init__(self, store, *, source: str = "tracking-cli") -> None:
        self.store = store
        self.source = validate_bounded_text(source, field="source", limit=60)
        for collection in ENGAGEMENT_COLLECTIONS:
            self.store.state.setdefault(collection, {})
        # Ledger operations touch several collections at once (a send updates the outreach record *and* appends
        # an event *and* moves a relationship stage). They must land as one durable write or none at all, so an
        # operation defers saving until its last step and rolls its own in-memory edits back on a refusal.
        self._depth = 0
        self._dirty = False

    # ------------------------------------------------------------------ shared internals

    def _commit(self) -> None:
        """Mark state dirty inside a transaction, or save immediately outside one."""
        if self._depth:
            self._dirty = True
            return
        self.store._save()  # noqa: SLF001 - the store's own atomic write, single-sourced here

    @contextmanager
    def _transaction(self, *collections: str):
        """One durable write per operation, with rollback of in-memory edits on any refusal.

        Only the collections an operation can touch are snapshotted, which keeps the copy proportional to the
        work instead of to the size of the whole state file.
        """
        touched = {name: copy.deepcopy(self.store.state.get(name) or {}) for name in collections}
        was_dirty = self._dirty
        self._depth += 1
        try:
            yield
        except BaseException:
            for name, snapshot in touched.items():
                self.store.state[name] = snapshot
            self._dirty = was_dirty
            self._depth -= 1
            raise
        self._depth -= 1
        if self._depth == 0 and self._dirty:
            self._dirty = False
            self.store._save()  # noqa: SLF001 - the flush for the whole operation, once

    def _fresh(self) -> None:
        """Re-read authoritative state before a check that grants or refuses something.

        The store's own authorization path does this for reviews and deployments; the eligibility guard reuses
        the rule so a decision is never made against a snapshot a concurrent worker has already changed.
        """
        if self._dirty:
            # Uncommitted edits are in memory right now; re-reading would blend them with older rows.
            return
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()

    def _lead(self, lead_id: Any) -> dict[str, Any]:
        cleaned = validate_record_id(lead_id, kind="lead_id")
        lead = (self.store.state.get("leads") or {}).get(cleaned)
        if not isinstance(lead, dict):
            raise LedgerError(f"Unknown lead '{cleaned}': engagement records attach to a tracked lead.", code="UNKNOWN_LEAD")
        # A lead does not own its project id - the project points at the lead - so resolve it the same way the
        # rest of the store does rather than trusting a field that may be absent.
        project_id = lead.get("project_id")
        if not project_id:
            for candidate_project in (self.store.state.get("projects") or {}).values():
                if isinstance(candidate_project, dict) and candidate_project.get("lead_id") == cleaned:
                    project_id = candidate_project.get("project_id")
                    break
        return {"lead_id": cleaned, "business_name": lead.get("business_name"), "project_id": project_id}

    def _collection(self, name: str) -> dict[str, Any]:
        return self.store.state.setdefault(name, {})

    #: What a ledger-owned transaction is allowed to write. The inbound intake joins the same save so a
    #: response, its intake row, and any opt-out land together or not at all.
    OWNED_COLLECTIONS = ENGAGEMENT_COLLECTIONS

    def find_event(self, dedupe_key: str) -> dict[str, Any] | None:
        for record in self._collection("engagements").values():
            if record.get("dedupe_key") == dedupe_key:
                return record
        return None

    @_ledger_transaction

    def record_event(
        self,
        lead_id: Any,
        *,
        kind: str,
        summary: str,
        direction: str = "INTERNAL",
        channel: str | None = None,
        occurred_at: Any = None,
        evidence: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
        outreach_id: str | None = None,
        proposal_id: str | None = None,
        follow_up_id: str | None = None,
        idempotency_key: str | None = None,
        provider: str | None = None,
        external_message_id: str | None = None,
        conversation_id: str | None = None,
        delivery_status: str | None = None,
        detail: str | None = None,
        classification: str | None = None,
        needs_human_review: bool | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Append one auditable event. Repeats of the same logical event return the stored record."""
        lead = self._lead(lead_id)
        kind = validate_bounded_text(kind, field="kind", limit=40).upper()
        if kind not in EVENT_KINDS:
            raise LedgerError(f"Unsupported engagement kind '{kind}'. Permitted: {', '.join(sorted(EVENT_KINDS))}.", code="EVENT_KIND_UNSUPPORTED")
        if direction not in DIRECTIONS:
            raise LedgerError(f"Unsupported direction '{direction}'.", code="EVENT_DIRECTION_INVALID")
        channel = self._channel(channel, required=kind in {"OUTREACH_SENT", "RESPONSE_RECEIVED", "FOLLOW_UP_SCHEDULED"})
        cleaned_summary = redact_contact_details(validate_bounded_text(summary, field="summary", limit=MAX_SUMMARY_CHARS))
        occurred = normalize_datetime(occurred_at or now(), field="occurred_at")
        evidence_rows = self._evidence(evidence)
        if kind in {"RESPONSE_RECEIVED", "OUTREACH_SENT", "OUTREACH_SEND_FAILED", "PROPOSAL_PRESENTED",
                    "PROPOSAL_DECISION", "OUTCOME_RECORDED", "OPT_OUT_RECORDED"} and not evidence_rows:
            raise LedgerError(
                f"'{kind}' asserts something a person said or did, so it needs at least one evidence item "
                "(--evidence-note or --evidence-url). An unverified claim is not recorded as a fact.",
                code="EVIDENCE_REQUIRED",
            )
        if outreach_id:
            self._known_outreach(outreach_id)
        provider_clean = validate_bounded_text(provider, field="provider", limit=40).lower() if provider else None
        external_id = sanitize_detail(external_message_id, field="external_message_id", limit=120) if external_message_id else None
        conversation = validate_record_id(conversation_id, kind="conversation_id") if conversation_id else None
        delivery_clean = None
        if delivery_status:
            delivery_clean = validate_bounded_text(delivery_status, field="delivery_status", limit=40).upper()
            if delivery_clean not in DELIVERY_STATUSES:
                raise LedgerError(
                    f"Unsupported delivery status '{delivery_clean}'. Permitted: {', '.join(sorted(DELIVERY_STATUSES))}.",
                    code="DELIVERY_STATUS_UNSUPPORTED",
                )
            if delivery_clean in {"ACCEPTED_BY_PROVIDER", "VERIFIED_DELIVERED"}:
                raise LedgerError(
                    f"'{delivery_clean}' is a provider's own statement. This repository has no provider receipt "
                    "reader, so nothing can write it; record what the operator saw (REPORTED_BY_OPERATOR) instead.",
                    code="PROVIDER_RECEIPT_UNAVAILABLE",
                )
        detail_clean = sanitize_detail(detail, field="detail") if detail else None

        dedupe = idempotency_key or _sha(f"lead:{lead['lead_id']}", kind, channel or "-", occurred[:10], (cleaned_summary or "")[:200])
        existing = self.find_event(dedupe)
        if existing is not None:
            existing["duplicate_of_recorded_at"] = existing.get("recorded_at")
            return {**existing, "duplicate": True}

        engagements = self._collection("engagements")
        event_id = next_numeric_id(engagements, "engagement")
        record = {
            "engagement_id": event_id,
            "lead_id": lead["lead_id"],
            "business_name": lead["business_name"],
            "project_id": self._resolved_project(project_id, lead),
            "outreach_id": outreach_id,
            "proposal_id": proposal_id,
            "follow_up_id": follow_up_id,
            "kind": kind,
            "direction": direction,
            "channel": channel,
            "summary": cleaned_summary,
            "evidence": evidence_rows,
            "occurred_at": occurred,
            "recorded_at": now(),
            "recorded_by": self.source,
            "dedupe_key": dedupe,
            "provider": provider_clean,
            "external_message_id": external_id,
            "conversation_id": conversation,
            "delivery_status": delivery_clean,
            "detail": detail_clean,
            "provider_verified": False,
            "classification": classification,
            "classification_source": ("operator_asserted" if classification else "no_classifier_in_this_repository")
                                      if kind == "RESPONSE_RECEIVED" else None,
            "needs_human_review": needs_human_review,
            "in_reply_to": validate_record_id(in_reply_to, kind="in_reply_to") if in_reply_to else None,
            "data_classification": self._classification(lead["lead_id"]),
            "contact_details_redacted": redact_contact_details(summary) != summary,
            "duplicate": False,
        }
        engagements[event_id] = record
        self._touch_relationship(lead["lead_id"], trigger=f"event:{kind}")
        self._commit()
        return record

    def _channel(self, channel: str | None, *, required: bool = False) -> str | None:
        if channel in (None, "", "*"):
            if required:
                raise LedgerError("A channel is required for this record (EMAIL, WHATSAPP, INSTAGRAM, ...).", code="CHANNEL_REQUIRED")
            return None
        cleaned = validate_bounded_text(channel, field="channel", limit=20).upper()
        if cleaned not in CHANNELS:
            raise LedgerError(f"Unsupported channel '{cleaned}'. Permitted: {', '.join(sorted(CHANNELS))}.", code="CHANNEL_UNSUPPORTED")
        return cleaned

    def _evidence(self, evidence: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in evidence or []:
            if not isinstance(item, dict):
                raise LedgerError("Each evidence item must be an object with a kind and a note or URL.", code="EVIDENCE_INVALID")
            kind = str(item.get("kind") or "OPERATOR_NOTE").strip().upper()
            if kind not in EVIDENCE_KINDS:
                raise LedgerError(f"Unsupported evidence kind '{kind}'.", code="EVIDENCE_KIND_UNSUPPORTED")
            note = item.get("note")
            url = item.get("source_url")
            quote = item.get("quote")
            if note is None and not url and not quote:
                raise LedgerError("Evidence needs a note, a quoted message, or a source URL.", code="EVIDENCE_INVALID")
            row: dict[str, Any] = {"kind": kind, "recorded_at": now()}
            if note:
                row["note"] = redact_contact_details(validate_bounded_text(note, field="evidence.note", limit=MAX_NOTE_CHARS))
            if url:
                text = str(url).strip()
                if not _URL_PATTERN.match(text):
                    raise LedgerError("Evidence source URLs must be absolute http(s) URLs.", code="EVIDENCE_URL_INVALID")
                row["source_url"] = text[:500]
            if quote:
                row["quote"] = redact_contact_details(validate_bounded_text(quote, field="evidence.quote", limit=MAX_QUOTE_CHARS))
            if item.get("observed_at"):
                row["observed_at"] = normalize_datetime(item["observed_at"], field="evidence.observed_at")
            row["independently_verified"] = False
            rows.append(row)
        return rows

    def _known_project(self, project_id: str) -> dict[str, Any]:
        project = (self.store.state.get("projects") or {}).get(project_id)
        if not isinstance(project, dict):
            raise LedgerError(f"Unknown project '{project_id}'.", code="UNKNOWN_PROJECT")
        return project

    def _resolved_project(self, project_id: str | None, lead: dict[str, Any]) -> str | None:
        if project_id:
            self._known_project(project_id)
            return project_id
        if lead["project_id"] and lead["project_id"] in (self.store.state.get("projects") or {}):
            return str(lead["project_id"])
        return None

    def _known_outreach(self, outreach_id: str) -> dict[str, Any]:
        record = (self.store.state.get("outreach") or {}).get(outreach_id)
        if not isinstance(record, dict):
            raise LedgerError(f"Unknown outreach record '{outreach_id}'.", code="UNKNOWN_OUTREACH")
        return record

    def _classification(self, lead_id: str) -> str:
        return str((self.store.state.get("leads") or {}).get(lead_id, {}).get("data_classification") or "TEST_FIXTURE")

    # ------------------------------------------------------------------ the one eligibility guard

    def outreach_eligibility(
        self,
        lead_id: str,
        channel: str | None = None,
        *,
        outreach_id: str | None = None,
        for_action: str = "SEND",
    ) -> dict[str, Any]:
        """Ask the only question a scheduler is allowed to ask before it touches a prospect.

        The ledger's own refusals and any future send path read this same function, so there is no weaker
        duplicate check to slip past. It re-reads state first (a review decision made a moment ago must be
        visible), and it *answers* - it never grants anything: `grants_authorization` is false by construction,
        because eligibility and permission are different questions and only a human decides the second.
        """
        for_action = validate_bounded_text(for_action, field="for_action", limit=20).upper()
        if for_action not in {"SEND", "SCHEDULE"}:
            raise LedgerError("for_action must be SEND or SCHEDULE.", code="ELIGIBILITY_ACTION_INVALID")
        self._fresh()
        lead = self._lead(lead_id)
        cleaned_channel = self._channel(channel)
        checks: list[dict[str, Any]] = []
        blockers: list[str] = []

        block = self.is_blocked(lead["lead_id"], cleaned_channel)
        checks.append({
            "check": "OPT_OUT",
            "status": "BLOCKED" if block else "CLEAR",
            "detail": (f"active block {block.get('block_id')} on '{block.get('channel')}': {block.get('reason') or 'no reason recorded'}"
                       if block else "no active opt-out for this lead and channel"),
        })
        if block:
            blockers.append("CONTACT_BLOCKED")

        if for_action == "SEND":
            attempts = self._send_attempts(lead["lead_id"], cleaned_channel)
            over = attempts["distinct_outreach_records"] >= MAX_SENDS_PER_LEAD_PER_CHANNEL
            checks.append({
                "check": "CONTACT_CAP",
                "status": "BLOCKED" if over else "CLEAR",
                "detail": (f"{attempts['distinct_outreach_records']} outreach records have been reported on "
                           f"'{cleaned_channel or 'any channel'}' (limit {MAX_SENDS_PER_LEAD_PER_CHANNEL}); "
                           f"{attempts['failed']} of the attempts failed; a further approach needs a new "
                           "outreach record and a human review decision"),
            })
            if over:
                blockers.append("CONTACT_LIMIT_REACHED")

        if outreach_id is not None:
            outreach = self._known_outreach(validate_record_id(outreach_id, kind="outreach_id"))
            status = str(outreach.get("status") or "")
            # A draft whose last attempt failed keeps its human approval: the approval covers the content, and a
            # delivery failure is a reason to try again deliberately, not a reason to start a new review.
            approved = status in {"READY_TO_SEND", "SEND_FAILED"}
            if for_action == "SCHEDULE":
                approved = status in {"READY_TO_SEND", "SENT_REPORTED", "SEND_FAILED"}
            checks.append({
                "check": "HUMAN_APPROVAL",
                "status": "CLEAR" if approved else "BLOCKED",
                "detail": (f"outreach {outreach_id} is '{status}'; the outreach review decision is the approval "
                           "record, and a draft that is still AWAITING_REVIEW or was REJECTED is not permission "
                           "to contact anyone"),
            })
            if not approved:
                blockers.append("OUTREACH_NOT_APPROVED")
            if str(outreach.get("lead_id") or "") != lead["lead_id"]:
                blockers.append("OUTREACH_LEAD_MISMATCH")
                checks[-1]["status"] = "BLOCKED"
                checks[-1]["detail"] = f"outreach {outreach_id} belongs to lead '{outreach.get('lead_id')}', not '{lead['lead_id']}'."
        else:
            checks.append({
                "check": "HUMAN_APPROVAL",
                "status": "NO_DRAFT_REFERENCED",
                "detail": "no outreach record was named, so approval cannot be read from one; a send report always does",
            })
            if for_action == "SEND":
                blockers.append("APPROVAL_REFERENCE_REQUIRED")

        project_id = lead.get("project_id") or self._resolved_project(None, lead)
        project = (self.store.state.get("projects") or {}).get(str(project_id)) if project_id else None
        if isinstance(project, dict) and str(project.get("current_stage")) in {"DISQUALIFIED", "ARCHIVED"}:
            blockers.append("PROJECT_CLOSED")
            checks.append({"check": "PROJECT_STATE", "status": "BLOCKED",
                           "detail": f"project {project_id} is {project.get('current_stage')}"})
        else:
            checks.append({"check": "PROJECT_STATE", "status": "CLEAR",
                           "detail": f"project {project_id}: {project.get('current_stage')}" if project_id else "no project linked"})

        return {
            "lead_id": lead["lead_id"],
            "channel": cleaned_channel,
            "outreach_id": outreach_id,
            "for_action": for_action,
            "eligible": not blockers,
            "blockers": blockers,
            "checks": checks,
            "as_of": now(),
            "grants_authorization": False,
            "meaning": (
                "eligible means nothing here would refuse it; it is not an approval. Sending still needs the "
                "human who decided to send, and this function can never supply that."
            ),
        }

    def _send_attempts(self, lead_id: str, channel: str | None) -> dict[str, Any]:
        """Count what was actually attempted, per lead and channel, from the events - never from drafts."""
        outreach_ids: set[str] = set()
        reported_sent = 0
        failed = 0
        for row in self._collection("engagements").values():
            if row.get("lead_id") != lead_id or row.get("kind") not in SEND_ATTEMPT_KINDS:
                continue
            if channel is not None and row.get("channel") not in {channel, None}:
                continue
            outreach_ids.add(str(row.get("outreach_id") or row.get("engagement_id")))
            if row.get("kind") == "OUTREACH_SENT":
                reported_sent += 1
            else:
                failed += 1
        return {
            "distinct_outreach_records": len(outreach_ids),
            "reported_sent": reported_sent,
            "failed": failed,
            "limit": MAX_SENDS_PER_LEAD_PER_CHANNEL,
        }

    # ------------------------------------------------------------------ relationship stage

    def relationship(self, lead_id: str) -> dict[str, Any]:
        """The one-row-per-lead progress record, plus what the ledger says is next.

        Reading never creates a row: an unrecorded lead is reported as unrecorded, with the stage its existing
        events would support, rather than silently gaining a relationship entry because someone asked.
        """
        lead = self._lead(lead_id)
        record = self._collection("relationships").get(lead["lead_id"])
        if not isinstance(record, dict):
            derived_now = self._derive_stage(lead["lead_id"])
            return {
                "lead_id": lead["lead_id"], "business_name": lead.get("business_name"),
                "project_id": lead.get("project_id"), "recorded": False,
                "stage": "NOT_CONTACTED", "previous_stage": None, "stage_changed_at": None,
                "next_action": derived_now["next_action"], "next_action_basis": "no ledger record for this lead yet",
                "stage_history": [], "evidence_refs": [],
                "derived_stage": derived_now["stage"], "derived_next_action": derived_now["next_action"],
                "derived_from": derived_now["confirmed_by"], "stage_is_current": derived_now["stage"] == "NOT_CONTACTED",
            }
        derived = self._derive_stage(lead["lead_id"])
        return {**record, "recorded": True, "derived_stage": derived["stage"], "derived_next_action": derived["next_action"],
                "derived_from": derived["confirmed_by"], "stage_is_current": record.get("stage") == derived["stage"]}

    def _new_relationship(self, lead: dict[str, Any]) -> dict[str, Any]:
        relationships = self._collection("relationships")
        record = {
            "lead_id": lead["lead_id"],
            "business_name": lead.get("business_name"),
            "project_id": lead.get("project_id") or self._resolved_project(None, lead),
            "stage": "NOT_CONTACTED",
            "previous_stage": None,
            "stage_changed_at": now(),
            "next_action": "prepare outreach for the qualified lead",
            "next_action_basis": "no outreach record exists yet for this lead",
            "stage_history": [{"stage": "NOT_CONTACTED", "at": now(), "trigger": "created",
                               "reason": "the first ledger record for this lead", "confirmed_by": []}],
            "evidence_refs": [],
            "created_at": now(),
            "updated_at": now(),
            "data_classification": self._classification(lead["lead_id"]),
        }
        relationships[lead["lead_id"]] = record
        return record

    def _corroborated_stages(self, lead_id: str) -> dict[str, list[str]]:
        """Which stages the existing records actually prove, with the ids that prove them."""
        proven: dict[str, list[str]] = {}
        events = [row for row in self._collection("engagements").values() if row.get("lead_id") == lead_id]
        follow_ups = [row for row in self._collection("follow_ups").values() if row.get("lead_id") == lead_id]
        proposals = [row for row in self._collection("proposals").values() if row.get("lead_id") == lead_id]
        blocks = [row for row in self._collection("contact_blocks").values()
                  if row.get("lead_id") == lead_id and not row.get("revoked_at")]
        outcome = self._collection("outcomes").get(lead_id)
        outreach_rows = [row for row in (self.store.state.get("outreach") or {}).values() if row.get("lead_id") == lead_id]

        def add(stage: str, *ids: str) -> None:
            proven.setdefault(stage, []).extend([str(i) for i in ids if i])

        if any(row.get("status") == "READY_TO_SEND" for row in outreach_rows):
            add("OUTREACH_APPROVED", *[str(r.get("outreach_id")) for r in outreach_rows if r.get("status") == "READY_TO_SEND"])
        # A failed attempt is deliberately absent: it proved that we tried, not that they were reached.
        if any(row.get("status") == "SENT_REPORTED" for row in outreach_rows) or any(
                row.get("kind") == "OUTREACH_SENT" for row in events):
            add("OUTREACH_SENT", *[str(r.get("outreach_id")) for r in outreach_rows if r.get("status") == "SENT_REPORTED"])
        if any(row.get("kind") == "RESPONSE_RECEIVED" for row in events):
            add("RESPONDED", *[str(r.get("engagement_id")) for r in events if r.get("kind") == "RESPONSE_RECEIVED"])
        if any(row.get("status") == "SCHEDULED" for row in follow_ups):
            add("FOLLOW_UP_SCHEDULED", *[str(r.get("follow_up_id")) for r in follow_ups if r.get("status") == "SCHEDULED"])
        if proposals:
            add("PROPOSAL_OPEN", *[str(r.get("proposal_id")) for r in proposals])
        if any(row.get("status") in {"PRESENTED", "ACCEPTED", "DECLINED"} for row in proposals):
            add("PROPOSAL_PRESENTED", *[str(r.get("proposal_id")) for r in proposals if r.get("status") in {"PRESENTED", "ACCEPTED", "DECLINED"}])
        if isinstance(outcome, dict):
            status = str(outcome.get("status"))
            if status == "NEGOTIATING":
                add("NEGOTIATING", f"outcome:{lead_id}")
            elif status == "WON":
                add("WON", f"outcome:{lead_id}")
            elif status in {"LOST", "DECLINED"}:
                add("LOST", f"outcome:{lead_id}")
        if blocks:
            add("OPTED_OUT", *[str(r.get("block_id")) for r in blocks])
        return proven

    def _derive_stage(self, lead_id: str) -> dict[str, Any]:
        """Highest stage the records support, plus the next action they imply. Read-only."""
        proven = self._corroborated_stages(lead_id)
        stage, rank = "NOT_CONTACTED", -1
        for candidate, ids in proven.items():
            if _STAGE_RANK[candidate] > rank:
                stage, rank = candidate, _STAGE_RANK[candidate]
        next_action, basis = self._derive_next_action(lead_id, stage)
        return {"stage": stage, "confirmed_by": sorted({i for ids in proven.values() for i in ids}),
                "next_action": next_action, "next_action_basis": basis, "proven_stages": {k: sorted(v) for k, v in sorted(proven.items())}}

    def _derive_next_action(self, lead_id: str, stage: str) -> tuple[str, str]:
        due = self.due_follow_ups()["due"]
        mine = [row for row in due if row.get("lead_id") == lead_id]
        if mine:
            ids = ", ".join(str(row.get("follow_up_id")) for row in mine)
            return (f"action overdue follow-up {ids}", "a follow-up is due and nothing has been recorded against it")
        events = [row for row in self._collection("engagements").values() if row.get("lead_id") == lead_id]
        responses = [row for row in events if row.get("kind") == "RESPONSE_RECEIVED"]
        # Only something *sent to them* answers a message, and only if it went out afterwards - an internal
        # note, or a send that predates the reply, leaves the reply owed.
        # Two records can share a second, so "came after" compares (timestamp, id): ids are allocated in write
        # order, which makes the answer deterministic instead of dependent on how fast the operator typed.
        key = lambda row: (str(row.get("occurred_at") or ""), str(row.get("engagement_id")))
        last_in = max(responses, key=key) if responses else None
        answered = bool(last_in) and any(
            r.get("direction") == "OUTBOUND" and key(r) > key(last_in) for r in events)
        if last_in is not None and not answered:
            if last_in.get("needs_human_review"):
                return (f"prepare a reply to {last_in.get('engagement_id')} (a human decides what it says)",
                        "an inbound message needing a reply has no recorded response after it")
        proposals = [row for row in self._collection("proposals").values() if row.get("lead_id") == lead_id]
        open_proposals = [row for row in proposals if row.get("status") in {"DRAFT", "READY_FOR_PRESENTATION"}]
        if open_proposals:
            return (f"present proposal {open_proposals[-1].get('proposal_id')}", "a proposal exists that has not been presented")
        presented = [row for row in proposals if row.get("status") == "PRESENTED"]
        if presented:
            return (f"record the decision on proposal {presented[-1].get('proposal_id')}",
                    "a proposal was presented and no answer has been recorded")
        if stage == "OUTREACH_SENT":
            return ("wait for a reply, or schedule one follow-up - do not send again on a timer",
                    "a send was reported and nothing has come back")
        if stage == "WON":
            return ("hand over to onboarding; nothing in this ledger tracks delivery", "outcome recorded as WON")
        if stage in {"LOST", "OPTED_OUT"}:
            return ("nothing - this lead is closed on its recorded evidence", f"stage is {stage}")
        if stage in {"OUTREACH_SENT", "RESPONDED", "FOLLOW_UP_SCHEDULED"}:
            return ("wait for a reply, or schedule one follow-up - do not send again on a timer",
                    f"stage is {stage} and nothing further is due")
        return ("prepare an outreach draft for review", "no approved draft exists for this lead yet")

    def _touch_relationship(self, lead_id: str, *, trigger: str) -> dict[str, Any]:
        """Refresh the row after a write, advancing only along what the new records prove.

        Derived advancement is monotone: a follow-up or a proposal can move a lead forward because a record says
        so, and nothing here rewinds a stage because a later command happened to be run.
        """
        lead = self._lead(lead_id)
        record = self._collection("relationships").get(lead_id)
        if not isinstance(record, dict):
            record = self._new_relationship(lead)
        derived = self._derive_stage(lead_id)
        current = str(record.get("stage"))
        if str(record.get("stage_source") or "derived") == "operator" and record.get("stage") != derived["stage"]:
            # A human moved this deliberately (usually to step back over a bad record); re-deriving on every
            # later write would quietly undo that decision, so the difference is only reported - by
            # `stage_is_current`, which is what makes the divergence visible in a report.
            record["derived_stage"] = derived["stage"]
            record["next_action"] = derived["next_action"]
            record["next_action_basis"] = derived["next_action_basis"] + " (stage set by an operator; not re-derived)"
            record["updated_at"] = now()
            return record
        if _STAGE_RANK.get(derived["stage"], 0) > _STAGE_RANK.get(current, 0):
            record["previous_stage"] = current
            record["stage"] = derived["stage"]
            record["stage_changed_at"] = now()
            record.setdefault("stage_history", []).append({
                "stage": derived["stage"], "at": now(), "trigger": f"derived:{trigger}",
                "reason": STAGE_CORROBORATION.get(derived["stage"], "a ledger record supports it"),
                "confirmed_by": derived["proven_stages"].get(derived["stage"], []),
            })
            record["stage_source"] = "derived"
        elif record.get("stage_history"):
            # The record that proves a stage can land just after the stage is derived (the send event is written
            # before the outreach row flips to SENT_REPORTED), so corroboration is backfilled rather than left blank.
            latest = record["stage_history"][-1]
            if not latest.get("confirmed_by"):
                latest["confirmed_by"] = derived["proven_stages"].get(str(record.get("stage")), [])
        if record.get("stage_history"):
            record["evidence_refs"] = sorted({i for entry in record["stage_history"] for i in entry.get("confirmed_by") or []})[-40:]
        record["next_action"] = derived["next_action"]
        record["next_action_basis"] = derived["next_action_basis"]
        record["updated_at"] = now()
        return record

    def advance_relationship_stage(
        self,
        lead_id: str,
        stage: str,
        *,
        reason: str,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        allow_backtrack: bool = False,
        occurred_at: Any = None,
    ) -> dict[str, Any]:
        """Move the relationship row by hand - which still needs the record that proves it."""
        lead = self._lead(lead_id)
        target = validate_bounded_text(stage, field="stage", limit=30).upper()
        if target not in RELATIONSHIP_STAGES:
            raise LedgerError(
                f"Unsupported relationship stage '{target}'. Permitted: {', '.join(RELATIONSHIP_STAGES)}.",
                code="RELATIONSHIP_STAGE_UNSUPPORTED",
            )
        if not (reason and reason.strip()):
            raise LedgerError("Moving a relationship stage needs a reason.", code="REASON_REQUIRED")
        with self._transaction(*ENGAGEMENT_COLLECTIONS, "outreach"):
            record = self._touch_relationship(lead["lead_id"], trigger="refresh")
            current = str(record.get("stage"))
            if target == current:
                return {**record, "unchanged": True}
            proven = self._corroborated_stages(lead["lead_id"])
            if target not in proven:
                raise LedgerError(
                    f"'{target}' cannot be asserted before it is recorded: it needs {STAGE_CORROBORATION[target]}. "
                    "Record that first - a stage set by hand would be a second, freer account of the same history.",
                    code="STAGE_NEEDS_CORROBORATION",
                )
            if target not in RELATIONSHIP_TRANSITIONS.get(current, set()):
                raise LedgerError(
                    f"Illegal relationship transition '{current}' -> '{target}'. Permitted from '{current}': "
                    f"{', '.join(sorted(RELATIONSHIP_TRANSITIONS.get(current, set()))) or 'none (closed)'}",
                    code="RELATIONSHIP_TRANSITION_INVALID",
                )
            if _STAGE_RANK[target] < _STAGE_RANK[current] and not allow_backtrack:
                raise LedgerError(
                    f"Moving from '{current}' back to '{target}' lowers a recorded stage; pass --allow-backtrack "
                    "with a reason when the earlier record was itself wrong.",
                    code="RELATIONSHIP_BACKTRACK_REQUIRES_FLAG",
                )
            evidence = self._evidence(
                ([{"kind": "OPERATOR_NOTE", "note": evidence_note}] if evidence_note else [])
                + ([{"kind": "URL", "source_url": evidence_url}] if evidence_url else [])
            )
            when = normalize_datetime(occurred_at or now(), field="occurred_at")
            record.update({
                "previous_stage": current, "stage": target, "stage_changed_at": when,
                "next_action": self._derive_next_action(lead["lead_id"], target)[0],
                "next_action_basis": f"stage set to {target} by {self.source}",
                "updated_at": now(),
            })
            record["stage_source"] = "operator"
            record.setdefault("stage_history", []).append({
                "stage": target, "at": when, "trigger": f"operator:{self.source}",
                "reason": redact_contact_details(reason)[:MAX_NOTE_CHARS],
                "confirmed_by": proven[target], "evidence": evidence,
            })
            self._commit()
            event = self.record_event(
                lead["lead_id"],
                kind="RELATIONSHIP_STAGE_CHANGED",
                direction="INTERNAL",
                summary=redact_contact_details(f"Relationship stage '{current}' -> '{target}': {reason}"),
                occurred_at=when,
                evidence=evidence or [{"kind": "OPERATOR_NOTE", "note": f"Set by {self.source}."}],
                project_id=lead.get("project_id"),
            )
            return {**record, "change_engagement_id": event["engagement_id"], "corroborated_by": proven[target]}

    def is_blocked(self, lead_id: str, channel: str | None) -> dict[str, Any] | None:
        for block in (self._collection("contact_blocks") or {}).values():
            if block.get("lead_id") != lead_id or block.get("revoked_at"):
                continue
            if block.get("channel") in {channel, "*", None}:
                return block
        return None

    # ------------------------------------------------------------------ outreach sending status

    @_ledger_transaction

    def mark_outreach_sent(
        self,
        outreach_id: str,
        *,
        occurred_at: Any = None,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        provider_message_id: str | None = None,
        result: str = "REPORTED_SENT",
        provider: str | None = None,
        failure_note: str | None = None,
    ) -> dict[str, Any]:
        """Record what a human says happened to an approved draft - and stop exactly there.

        Three adjacent facts stay apart: a *prepared* draft is not a send, a *reported* send is not a delivery,
        and a *failure* is never written as a contact. `result="FAILED"` records the attempt honestly: the
        outreach record becomes `SEND_FAILED`, the lead is not marked as having been reached, and a later report
        on the same draft can still turn it into a send without duplicating the outreach record.
        """
        cleaned_id = validate_record_id(outreach_id, kind="outreach_id")
        self._fresh()
        outreach = self._known_outreach(cleaned_id)
        outcome = validate_bounded_text(result, field="result", limit=30).upper()
        if outcome not in SEND_RESULTS:
            raise LedgerError(
                f"Unsupported send result '{outcome}'. Permitted: {', '.join(sorted(SEND_RESULTS))}.",
                code="SEND_RESULT_UNSUPPORTED",
            )
        if outcome in PROVIDER_ONLY_RESULTS:
            raise LedgerError(
                f"'{outcome}' would quote a provider's own acceptance or delivery receipt. This repository has "
                "no provider receipt reader, so it cannot be recorded truthfully here; report what the operator "
                "observed (result=REPORTED_SENT) and keep the receipt where it lives.",
                code="PROVIDER_RECEIPT_UNAVAILABLE",
            )
        status = outreach.get("status")
        if outcome == "REPORTED_SENT" and status == "SENT_REPORTED":
            return {**outreach, "unchanged": True}
        if outcome == "FAILED" and status == "SENT_REPORTED":
            raise LedgerError(
                f"Outreach '{cleaned_id}' is already reported as sent; a later failure notice belongs on the "
                "record as a note, not as a rewrite of what was reported.",
                code="SEND_ALREADY_REPORTED_SENT",
            )
        allowed_now = status in {"READY_TO_SEND", "SEND_FAILED"} if outcome == "REPORTED_SENT" else status in {"READY_TO_SEND", "SEND_FAILED"}
        if not allowed_now:
            raise LedgerError(
                f"Outreach '{cleaned_id}' is '{status}'; only a human-approved draft (READY_TO_SEND), or one "
                "already reported as failed, can be reported on. Preparing a message is never sending it.",
                code="OUTREACH_NOT_APPROVED",
            )
        channel = str(outreach.get("channel") or "EMAIL")
        lead_id = str(outreach.get("lead_id") or "")
        # One guard for every send path: opt-out, contact budget, approval, project state - re-read now rather
        # than trusted from whatever the caller remembered.
        verdict = self.outreach_eligibility(lead_id, channel, outreach_id=cleaned_id, for_action="SEND")
        if not verdict["eligible"]:
            code = str(verdict["blockers"][0])
            detail = next((row["detail"] for row in verdict["checks"] if row["status"] == "BLOCKED"), "")
            raise LedgerError(f"Send not recorded for outreach '{cleaned_id}': {code} - {detail}", code=code)
        evidence: list[dict[str, Any]] = []
        if evidence_note:
            evidence.append({"kind": "OPERATOR_NOTE", "note": evidence_note})
        if evidence_url:
            evidence.append({"kind": "URL", "source_url": evidence_url})
        if failure_note:
            evidence.append({"kind": "OPERATOR_NOTE", "note": failure_note})
        if not evidence:
            raise LedgerError(
                ("A reported failure needs the note that says what failed (--failure-note or --evidence-note); "
                 "an error text belongs in the record so the retry can be reasoned about."
                 if outcome == "FAILED" else
                 "Reporting a send needs evidence that it went out (--evidence-note or --evidence-url); "
                 "without it this would be an unverified claim recorded as fact."),
                code="EVIDENCE_REQUIRED",
            )
        provider_clean = validate_bounded_text(provider, field="provider", limit=40).lower() if provider else str(outreach.get("provider") or "manual")
        if provider_message_id:
            evidence.append({
                "kind": "OPERATOR_NOTE",
                "note": f"Provider message id recorded verbatim, not verified against the provider: {provider_message_id}"[:MAX_NOTE_CHARS],
            })
        when = normalize_datetime(occurred_at or now(), field="occurred_at")
        attempts = int(outreach.get("send_attempts") or 0) + 1
        event = self.record_event(
            lead_id,
            kind="OUTREACH_SEND_FAILED" if outcome == "FAILED" else "OUTREACH_SENT",
            direction="OUTBOUND",
            channel=channel,
            summary=(f"Operator reported that sending outreach draft {cleaned_id} failed."
                     if outcome == "FAILED" else
                     f"Operator reported sending outreach draft {cleaned_id} for {outreach.get('project_id') or 'the project'}."),
            occurred_at=when,
            evidence=evidence,
            project_id=outreach.get("project_id"),
            outreach_id=cleaned_id,
            provider=provider_clean,
            external_message_id=provider_message_id,
            delivery_status="FAILED_REPORTED" if outcome == "FAILED" else "REPORTED_BY_OPERATOR",
            detail=sanitize_detail(failure_note or evidence_note, field="detail"),
        )
        outreach.update({
            "status": "SEND_FAILED" if outcome == "FAILED" else "SENT_REPORTED",
            "send_engagement_id": event["engagement_id"],
            "delivery_status": "FAILED_REPORTED" if outcome == "FAILED" else "REPORTED_BY_OPERATOR",
            "provider_verified": False,
            "provider": provider_clean,
            "provider_message_id": (validate_bounded_text(provider_message_id, field="provider_message_id", limit=120)
                                    if provider_message_id else None),
            "send_attempts": attempts,
            "contact_established": outcome != "FAILED",
        })
        if outcome == "FAILED":
            outreach["failed_at"] = when
            outreach["failure_detail"] = event["detail"]
        else:
            outreach["sent_at"] = when
        self._touch_relationship(lead_id, trigger="outreach_send")
        self._commit()
        return {
            **outreach,
            "send_event": event,
            "counts_as_contact": outcome != "FAILED",
            "delivery_note": (
                "Reported by an operator; no provider receipt was verified by this system."
                if outcome != "FAILED" else
                "A failed delivery is recorded as a failure: it is not a contact, and it does not make the lead "
                "unreachable - it makes the next attempt a decision."
            ),
        }

    # ------------------------------------------------------------------ inbound responses

    @_ledger_transaction

    def record_response(
        self,
        lead_id: str,
        *,
        channel: str,
        summary: str,
        received_at: Any = None,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        quote: str | None = None,
        evidence_kind: str = "CUSTOMER_MESSAGE",
        outreach_id: str | None = None,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        provider: str | None = None,
        external_message_id: str | None = None,
        conversation_id: str | None = None,
        in_reply_to: str | None = None,
        classification: str | None = None,
        needs_human_review: bool = True,
    ) -> dict[str, Any]:
        """Store what the prospect actually said, and never what we hoped they would say."""
        evidence = [{"kind": evidence_kind, "note": evidence_note, "quote": quote, "observed_at": received_at}]
        if evidence_url:
            evidence.append({"kind": "URL", "source_url": evidence_url})
        if in_reply_to:
            reply_to = self._collection("engagements").get(validate_record_id(in_reply_to, kind="in_reply_to"))
            if not isinstance(reply_to, dict):
                raise LedgerError(f"Unknown engagement '{in_reply_to}' to reply to.", code="UNKNOWN_ENGAGEMENT")
            if reply_to.get("direction") not in {"OUTBOUND", "INTERNAL"}:
                raise LedgerError(
                    f"'{in_reply_to}' is an inbound record; a reply can only answer something we sent.",
                    code="REPLY_TARGET_INVALID",
                )
        cleaned_classification = None
        if classification:
            cleaned_classification = validate_bounded_text(classification, field="classification", limit=30).upper()
            if cleaned_classification not in RESPONSE_CLASSIFICATIONS:
                raise LedgerError(
                    f"Unsupported response classification '{cleaned_classification}'. Permitted: "
                    f"{', '.join(sorted(RESPONSE_CLASSIFICATIONS))}.",
                    code="CLASSIFICATION_UNSUPPORTED",
                )
        event = self.record_event(
            lead_id,
            kind="RESPONSE_RECEIVED",
            direction="INBOUND",
            channel=channel,
            summary=summary,
            occurred_at=received_at or now(),
            evidence=evidence,
            outreach_id=outreach_id,
            project_id=project_id,
            idempotency_key=idempotency_key,
            provider=provider,
            external_message_id=external_message_id,
            conversation_id=conversation_id,
            in_reply_to=in_reply_to,
            classification=cleaned_classification,
            needs_human_review=bool(needs_human_review),
        )
        if event.get("duplicate"):
            return event
        # A request to stop is not an opt-out until someone records one; the ledger only says what to do next.
        stored = self._collection("engagements").get(str(event["engagement_id"]))
        if cleaned_classification == "UNSUBSCRIBE":
            stored["suggested_action"] = "record_opt_out (a request to stop is honoured by recording it, not by inference)"
            self._commit()
        return dict(stored)

    # ------------------------------------------------------------------ follow-ups

    @_ledger_transaction

    def schedule_follow_up(
        self,
        lead_id: str,
        *,
        due_at: Any,
        reason: str,
        channel: str | None = None,
        proposal_id: str | None = None,
        outreach_id: str | None = None,
        allow_past_due: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        lead = self._lead(lead_id)
        channel_clean = self._channel(channel)
        if outreach_id:
            self._known_outreach(outreach_id)
        # The same guard a future sender must consult, so a follow-up cannot be scheduled against a contact
        # that is blocked or out of budget - and scheduling grants nothing to whoever eventually sends.
        verdict = self.outreach_eligibility(
            lead["lead_id"], channel_clean, outreach_id=outreach_id, for_action="SCHEDULE"
        )
        if not verdict["eligible"]:
            code = str(verdict["blockers"][0])
            detail = next((row["detail"] for row in verdict["checks"] if row["status"] == "BLOCKED"), "")
            raise LedgerError(f"Follow-up not scheduled: {code} - {detail}", code=code)
        due = normalize_datetime(due_at, field="due_at", allow_past=allow_past_due, end_of_day=True, allow_future=True)
        if not allow_past_due and due <= now():
            raise LedgerError(
                "A follow-up due date must still be in the future; pass --allow-past-due to record a "
                "backfilled schedule deliberately.",
                code="FOLLOW_UP_DUE_INVALID",
            )
        cleaned_reason = redact_contact_details(validate_bounded_text(reason, field="reason", limit=MAX_SUMMARY_CHARS))
        dedupe = idempotency_key or _sha(lead["lead_id"], due[:10], channel_clean or "-", (cleaned_reason or "")[:120])
        for record in self._collection("follow_ups").values():
            if record.get("dedupe_key") == dedupe:
                return {**record, "duplicate": True}
        if proposal_id:
            self._known_proposal(proposal_id)
        follow_ups = self._collection("follow_ups")
        follow_up_id = next_numeric_id(follow_ups, "follow_up")
        record = {
            "follow_up_id": follow_up_id,
            "lead_id": lead["lead_id"],
            "project_id": lead["project_id"],
            "proposal_id": proposal_id,
            "outreach_id": outreach_id,
            "channel": channel_clean,
            "reason": cleaned_reason,
            "due_at": due,
            "status": "SCHEDULED",
            "attempts": 0,
            "requires_approval": True,
            "approval_state": "NOT_REQUESTED",
            "never_authorizes_send": True,
            "dedupe_key": dedupe,
            "created_at": now(),
            "updated_at": now(),
            "completed_at": None,
            "completion_notes": None,
            "cancellation_reason": None,
            "data_classification": self._classification(lead["lead_id"]),
            "duplicate": False,
        }
        follow_ups[follow_up_id] = record
        self._commit()
        self.record_event(
            lead["lead_id"],
            kind="FOLLOW_UP_SCHEDULED",
            direction="INTERNAL",
            channel=channel_clean,
            summary=f"Follow-up {follow_up_id} scheduled for {due[:10]}: {cleaned_reason}",
            occurred_at=now(),
            follow_up_id=follow_up_id,
            evidence=[{"kind": "OPERATOR_NOTE", "note": f"Scheduled by {self.source}."}],
            proposal_id=proposal_id,
        )
        return record

    @_ledger_transaction

    def update_follow_up(
        self,
        follow_up_id: str,
        *,
        action: str,
        notes: str | None = None,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        occurred_at: Any = None,
        due_at: Any = None,
        allow_past_due: bool = False,
    ) -> dict[str, Any]:
        cleaned_id = validate_record_id(follow_up_id, kind="follow_up_id")
        record = self._collection("follow_ups").get(cleaned_id)
        if not isinstance(record, dict):
            raise LedgerError(f"Unknown follow-up '{cleaned_id}'.", code="UNKNOWN_FOLLOW_UP")
        verb = validate_bounded_text(action, field="action", limit=20).upper()
        if verb not in {"COMPLETE", "CANCEL", "RESCHEDULE"}:
            raise LedgerError("Action must be COMPLETE, CANCEL, or RESCHEDULE.", code="FOLLOW_UP_ACTION_INVALID")
        if record["status"] != "SCHEDULED":
            raise LedgerError(
                f"Follow-up '{cleaned_id}' is already '{record['status']}'; a closed follow-up is not reopened by an update.",
                code="FOLLOW_UP_CLOSED",
            )
        evidence = self._evidence(
            [{"kind": "OPERATOR_NOTE", "note": n} for n in (notes, evidence_note) if n]
            + ([{"kind": "URL", "source_url": evidence_url}] if evidence_url else [])
        )
        if verb == "COMPLETE" and not evidence:
            raise LedgerError("Completing a follow-up needs a note about what actually happened.", code="EVIDENCE_REQUIRED")
        when = normalize_datetime(occurred_at or now(), field="occurred_at")
        if verb == "RESCHEDULE":
            if due_at is None:
                raise LedgerError("A reschedule needs a new --due date.", code="FOLLOW_UP_DUE_INVALID")
            if not (evidence_note or notes or evidence_url):
                raise LedgerError("A reschedule needs a note explaining why.", code="EVIDENCE_REQUIRED")
            record["due_at"] = normalize_datetime(
                due_at, field="due_at", allow_past=allow_past_due, end_of_day=True, allow_future=True
            )
            if record["due_at"] <= now():
                raise LedgerError("A rescheduled follow-up still needs a future due date.", code="FOLLOW_UP_DUE_INVALID")
            record["reason"] = redact_contact_details(validate_bounded_text(notes or evidence_note, field="reason", limit=MAX_SUMMARY_CHARS))
            record["attempts"] = int(record.get("attempts") or 0) + 1
            record["approval_state"] = "NOT_REQUESTED"
            # Re-key so the original schedule and this one are not treated as the same pending item.
            record["dedupe_key"] = _sha(str(record["lead_id"]), str(record["due_at"])[:10], str(record.get("channel") or "-"), str(record["reason"])[:120])
        elif verb == "COMPLETE":
            record.update({"status": "COMPLETED", "completed_at": when, "completion_notes": redact_contact_details(notes or evidence_note),
                           "attempts": int(record.get("attempts") or 0) + 1, "evidence": evidence})
        else:
            record.update({"status": "CANCELLED", "cancellation_reason": redact_contact_details(notes or evidence_note or "cancelled"), "cancelled_at": when, "evidence": evidence})
        record["updated_at"] = now()
        self._commit()
        self.record_event(
            record["lead_id"],
            kind="FOLLOW_UP_COMPLETED" if verb == "COMPLETE" else "FOLLOW_UP_CANCELLED" if verb == "CANCEL" else "NOTE",
            direction="INTERNAL",
            channel=record.get("channel"),
            summary=redact_contact_details(notes or evidence_note or f"Follow-up {cleaned_id} {verb.lower()}d."),
            occurred_at=when,
            evidence=evidence or [{"kind": "OPERATOR_NOTE", "note": f"Updated by {self.source}."}],
            follow_up_id=cleaned_id,
            proposal_id=record.get("proposal_id"),
        )
        return record

    def due_follow_ups(self, *, today: Any = None) -> dict[str, Any]:
        """A read-only view. Overdue work is reported, never reinterpreted as a lost opportunity."""
        # A report may be run for a future date on purpose; only the records themselves are past-only.
        reference = normalize_datetime(today, field="today", allow_future=True)[:10] if today else datetime.now(timezone.utc).date().isoformat()
        scheduled = [record for record in self._collection("follow_ups").values() if record.get("status") == "SCHEDULED"]
        due = [record for record in scheduled if str(record.get("due_at", ""))[:10] <= reference]
        blocked = [
            record for record in due
            if self.is_blocked(str(record.get("lead_id") or ""), record.get("channel")) is not None
        ]
        rows: list[dict[str, Any]] = []
        for record in sorted(due, key=lambda item: str(item.get("due_at"))):
            try:
                verdict = self.outreach_eligibility(
                    str(record.get("lead_id") or ""), record.get("channel"),
                    outreach_id=record.get("outreach_id"), for_action="SCHEDULE",
                )
            except LedgerError as error:
                # A report must not fall over because one row points at something that no longer exists: that is
                # itself the finding, and the writer that consults the same guard still refuses on it.
                verdict = {"eligible": False, "blockers": [error.code], "check_failure": str(error)[:200]}
            rows.append({**record, "eligible_to_act_now": verdict["eligible"], "eligibility_blockers": verdict["blockers"]})
        return {
            "as_of": reference,
            "scheduled_count": len(scheduled),
            "due_count": len(due),
            "due": rows,
            "suppressed_by_opt_out": [record["follow_up_id"] for record in blocked],
            "note": ("Overdue is a scheduling fact, not a decision by the prospect; nothing here was changed, and "
                     "a due follow-up is not authorization to send anything."),
        }

    # ------------------------------------------------------------------ proposals

    @_ledger_transaction

    def create_proposal(
        self,
        lead_id: str,
        *,
        summary: str,
        project_id: str | None = None,
        outreach_id: str | None = None,
        terms_note: str | None = None,
        currency: str | None = None,
        amount_minor: int | None = None,
        idempotency_key: str | None = None,
        deployment_id: str | None = None,
        preview_url: str | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        lead = self._lead(lead_id)
        cleaned_summary = redact_contact_details(validate_bounded_text(summary, field="summary", limit=MAX_SUMMARY_CHARS))
        resolved_project = self._resolved_project(project_id, lead)
        if outreach_id:
            self._known_outreach(outreach_id)
        if (currency is None) != (amount_minor is None):
            raise LedgerError("Provide both 'currency' and 'amount_minor', or neither; a half-priced proposal is not recorded.", code="PROPOSAL_TERMS_INCOMPLETE")
        if currency is not None:
            currency = validate_bounded_text(currency, field="currency", limit=3).upper()
            if not isinstance(amount_minor, int) or amount_minor <= 0:
                raise LedgerError("'amount_minor' must be a positive integer in minor units.", code="PROPOSAL_TERMS_INVALID")
        demo = self._demo_reference(deployment_id, preview_url, project_id=resolved_project)
        approval = self._approval_reference(approval_ref, project_id=resolved_project)
        dedupe = idempotency_key or _sha(lead["lead_id"], resolved_project or "-", (cleaned_summary or "")[:160])
        for record in self._collection("proposals").values():
            if record.get("dedupe_key") == dedupe:
                return {**record, "duplicate": True}
        proposals = self._collection("proposals")
        proposal_id = next_numeric_id(proposals, "proposal")
        record = {
            "proposal_id": proposal_id,
            "lead_id": lead["lead_id"],
            "project_id": resolved_project,
            "outreach_id": outreach_id,
            "summary": cleaned_summary,
            "terms_note": redact_contact_details(terms_note) if terms_note else None,
            "currency": currency,
            "amount_minor": amount_minor if currency is not None else None,
            "status": "DRAFT",
            "version": 1,
            "supersedes": None,
            "presented_at": None,
            "presentation_evidence": [],
            "decision_at": None,
            "decision_evidence": [],
            "decision_notes": None,
            "dedupe_key": dedupe,
            "created_at": now(),
            "updated_at": now(),
            "demo": demo,
            "approval": approval,
            "outreach_status": (self._collection("outreach").get(outreach_id) or {}).get("status") if outreach_id else None,
            "data_classification": self._classification(lead["lead_id"]),
            "note": "A created proposal is not a presented one; presentation and decisions are recorded separately.",
            "duplicate": False,
        }
        proposals[proposal_id] = record
        self._commit()
        self.record_event(
            lead["lead_id"],
            kind="PROPOSAL_CREATED",
            direction="INTERNAL",
            summary=f"Proposal {proposal_id} drafted for {lead['business_name'] or lead['lead_id']}.",
            occurred_at=now(),
            evidence=[{"kind": "OPERATOR_NOTE", "note": f"Drafted by {self.source}; not yet presented."}],
            project_id=resolved_project,
            proposal_id=proposal_id,
        )
        return record

    def _demo_reference(self, deployment_id: str | None, preview_url: str | None, *, project_id: str | None) -> dict[str, Any] | None:
        """Attach a demo to the deployment record that produced it - or refuse, never paraphrase.

        A URL on a proposal is a pointer to a record with its own verification state. It can never make the
        demo "deployed": if the deployment record says its live URL was never checked, this one says so too.
        """
        if not deployment_id and not preview_url:
            return None
        deployments = self.store.state.get("deployments") or {}
        record: dict[str, Any] | None = None
        if deployment_id:
            cleaned = validate_record_id(deployment_id, kind="deployment_id")
            record = deployments.get(cleaned)
            if not isinstance(record, dict):
                raise LedgerError(
                    f"Unknown deployment '{cleaned}': a proposal cites a recorded deployment, not a URL someone "
                    "typed in.", code="UNKNOWN_DEPLOYMENT")
            if project_id and record.get("project_id") and record["project_id"] != project_id:
                raise LedgerError(
                    f"Deployment '{cleaned}' belongs to project '{record.get('project_id')}', not '{project_id}'.",
                    code="DEMO_PROJECT_MISMATCH")
        elif preview_url:
            record = next((row for row in deployments.values() if str(row.get("deployment_url")) == str(preview_url).strip()), None)
            if not isinstance(record, dict):
                raise LedgerError(
                    "That preview URL is not on any recorded deployment, so it cannot be attached as a verified "
                    "demo. Cite --deployment-id instead; a string that looks like a URL proves nothing.",
                    code="DEMO_URL_UNRECORDED")
        assert record is not None
        claimed = str(preview_url or "").strip()
        if claimed and claimed != str(record.get("deployment_url")):
            raise LedgerError(
                f"The URL given ({claimed[:80]}) is not the one this deployment record holds "
                f"({str(record.get('deployment_url'))[:80]}).",
                code="DEMO_URL_MISMATCH")
        return {
            "deployment_id": record.get("deployment_id"),
            "project_id": record.get("project_id"),
            "build_id": record.get("build_id"),
            "deployment_url": record.get("deployment_url"),
            "deployment_status": record.get("deployment_status"),
            "url_source": "recorded_deployment",
            # Whatever the deployment record itself says about verification is echoed, not upgraded: this
            # repository has never loaded a public URL to confirm the page is live.
            "live_url_checked": bool(record.get("live_url_checked") or record.get("url_checked_at")),
            "verification_note": "A recorded deployment is a record of a publish, not proof that a URL serves the latest build.",
        }

    def _approval_reference(self, approval_ref: str | None, *, project_id: str | None) -> dict[str, Any] | None:
        """Point at the review record that authorized the work, if one is named."""
        if not approval_ref:
            return None
        cleaned = validate_record_id(approval_ref, kind="approval_ref")
        review = (self.store.state.get("reviews") or {}).get(cleaned)
        if not isinstance(review, dict):
            raise LedgerError(
                f"Unknown review record '{cleaned}'; a proposal cites an approval that exists, or none at all.",
                code="UNKNOWN_APPROVAL")
        if project_id and review.get("project_id") and review["project_id"] != project_id:
            raise LedgerError(
                f"Review '{cleaned}' belongs to project '{review.get('project_id')}', not '{project_id}'.",
                code="APPROVAL_PROJECT_MISMATCH")
        return {
            "review_id": cleaned,
            "decision": review.get("decision") or review.get("status"),
            "reviewer": review.get("reviewer"),
            "reviewed_at": review.get("reviewed_at") or review.get("timestamp"),
            "identity_verified": bool(review.get("identity_verified")),
            "source": "recorded_review",
        }

    def _known_proposal(self, proposal_id: str) -> dict[str, Any]:
        record = self._collection("proposals").get(validate_record_id(proposal_id, kind="proposal_id"))
        if not isinstance(record, dict):
            raise LedgerError(f"Unknown proposal '{proposal_id}'.", code="UNKNOWN_PROPOSAL")
        return record

    @_ledger_transaction

    def update_proposal(
        self,
        proposal_id: str,
        *,
        status: str,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        occurred_at: Any = None,
        notes: str | None = None,
        evidence_kind: str | None = None,
    ) -> dict[str, Any]:
        record = self._known_proposal(proposal_id)
        target = validate_bounded_text(status, field="status", limit=30).upper()
        if target not in PROPOSAL_STATUSES:
            raise LedgerError(f"Unsupported proposal status '{target}'.", code="PROPOSAL_STATUS_UNSUPPORTED")
        current = str(record.get("status"))
        if target == current:
            return {**record, "unchanged": True}
        if target == "PRESENTED" and current == "DRAFT":
            raise LedgerError("A DRAFT proposal must move to READY_FOR_PRESENTATION before it can be presented.", code="PROPOSAL_NOT_READY")
        if target not in PROPOSAL_TRANSITIONS.get(current, set()):
            raise LedgerError(
                f"Illegal proposal transition '{current}' -> '{target}'. Permitted from '{current}': "
                f"{', '.join(sorted(PROPOSAL_TRANSITIONS.get(current, set()))) or 'none (closed)'}",
                code="PROPOSAL_TRANSITION_INVALID",
            )
        # A decision is the customer's, so the default kind cites them; presenting is the operator's own act.
        row_kind = evidence_kind or ("CUSTOMER_MESSAGE" if target in {"ACCEPTED", "DECLINED"} else "OPERATOR_NOTE")
        evidence = self._evidence(
            ([{"kind": row_kind, "note": evidence_note}] if evidence_note else [])
            + ([{"kind": "URL", "source_url": evidence_url}] if evidence_url else [])
        )
        if target in {"PRESENTED", "ACCEPTED", "DECLINED"} and not evidence:
            raise LedgerError(
                f"'{target}' describes something that happened to a real prospect, so it needs evidence "
                "(--evidence-note and/or --evidence-url).",
                code="EVIDENCE_REQUIRED",
            )
        if target == "ACCEPTED":
            kinds = {row["kind"] for row in evidence}
            if not kinds & {"CUSTOMER_MESSAGE", "CONTRACT", "INVOICE", "EMAIL", "CHAT", "MEETING"}:
                raise LedgerError(
                    "An ACCEPTED proposal cites what the customer sent or signed (CUSTOMER_MESSAGE, CONTRACT, "
                    "INVOICE, EMAIL, CHAT, MEETING); an operator's note that it 'went well' is not acceptance.",
                    code="EVIDENCE_KIND_INSUFFICIENT",
                )
        when = normalize_datetime(occurred_at or now(), field="occurred_at")
        if target == "PRESENTED":
            record["presentation_evidence"] = list(record.get("presentation_evidence") or []) + evidence
            record["presented_at"] = when
        if target in {"ACCEPTED", "DECLINED", "WITHDRAWN", "SUPERSEDED"}:
            record["decision_at"] = when
            record["decision_evidence"] = list(record.get("decision_evidence") or []) + evidence
            record["decision_notes"] = redact_contact_details(notes)
        record["status"] = target
        record["updated_at"] = now()
        self._commit()
        self.record_event(
            str(record["lead_id"]),
            # Only a customer-side answer is a "decision"; a status the operator advances alone is a note.
            kind="PROPOSAL_PRESENTED" if target == "PRESENTED" else "PROPOSAL_DECISION" if target in {"ACCEPTED", "DECLINED"} else "NOTE",
            direction="OUTBOUND" if target == "PRESENTED" else "INBOUND" if target in {"ACCEPTED", "DECLINED"} else "INTERNAL",
            summary=redact_contact_details(notes or f"Proposal {proposal_id} is now {target}."),
            occurred_at=when,
            evidence=evidence or [{"kind": "OPERATOR_NOTE", "note": f"Status set by {self.source}."}],
            project_id=record.get("project_id"),
            proposal_id=proposal_id,
        )
        return record

    # ------------------------------------------------------------------ outcomes

    @_ledger_transaction

    def record_outcome(
        self,
        lead_id: str,
        *,
        status: str,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        evidence_kind: str = "CUSTOMER_MESSAGE",
        reference: str | None = None,
        occurred_at: Any = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        lead = self._lead(lead_id)
        target = validate_bounded_text(status, field="status", limit=30).upper()
        if target not in OUTCOME_STATUSES:
            raise LedgerError(f"Unsupported outcome '{target}'. Permitted: {', '.join(sorted(OUTCOME_STATUSES))}.", code="OUTCOME_STATUS_UNSUPPORTED")
        outcomes = self._collection("outcomes")
        record = outcomes.get(lead["lead_id"])
        current = str(record.get("status")) if isinstance(record, dict) else "UNRESOLVED"
        evidence = self._evidence(
            ([{"kind": evidence_kind, "note": evidence_note}] if evidence_note else [])
            + ([{"kind": "URL", "source_url": evidence_url}] if evidence_url else [])
        )
        if target in EVIDENCE_REQUIRED_OUTCOMES and not evidence:
            raise LedgerError(
                f"Outcome '{target}' states what the business decided, so it needs evidence of that statement; "
                "an unverified conclusion is not recorded.",
                code="EVIDENCE_REQUIRED",
            )
        if target == "WON":
            kinds = {row["kind"] for row in evidence}
            if not kinds & WON_EVIDENCE_KINDS or not (reference and reference.strip()):
                raise LedgerError(
                    "A deal is marked WON only with supporting evidence of the customer's agreement "
                    f"(evidence kind in {', '.join(sorted(WON_EVIDENCE_KINDS))}) plus a contract/invoice/payment --reference.",
                    code="WON_EVIDENCE_REQUIRED",
                )
        if target != current and target not in OUTCOME_TRANSITIONS.get(current, set()):
            raise LedgerError(
                f"Illegal outcome transition '{current}' -> '{target}'. Permitted from '{current}': "
                f"{', '.join(sorted(OUTCOME_TRANSITIONS.get(current, set())))}",
                code="OUTCOME_TRANSITION_INVALID",
            )
        when = normalize_datetime(occurred_at or now(), field="occurred_at")
        if target == current and isinstance(record, dict):
            record["evidence"] = list(record.get("evidence") or []) + evidence
            record["updated_at"] = now()
            record["unchanged"] = True
            self._commit()
            return record
        outcomes[lead["lead_id"]] = {
            "lead_id": lead["lead_id"],
            "business_name": lead["business_name"],
            "project_id": self._resolved_project(None, lead),
            "status": target,
            "previous_status": current,
            "recorded_at": now(),
            "updated_at": now(),
            "effective_at": when,
            "evidence": evidence,
            "reference": redact_contact_details(reference.strip()) if reference else None,
            "notes": redact_contact_details(notes) if notes else None,
            "data_classification": self._classification(lead["lead_id"]),
            "unchanged": False,
            "revenue_recorded": False,
        }
        self._commit()
        self.record_event(
            lead["lead_id"],
            kind="OUTCOME_RECORDED",
            direction="INBOUND" if target in OUTCOME_DECISIVE else "INTERNAL",
            summary=redact_contact_details(notes or f"Outcome recorded as {target}."),
            occurred_at=when,
            evidence=evidence or [{"kind": "OPERATOR_NOTE", "note": f"Set to {target} by {self.source}."}],
            project_id=outcomes[lead["lead_id"]]["project_id"],
        )
        return outcomes[lead["lead_id"]]

    # ------------------------------------------------------------------ opt-outs

    @_ledger_transaction

    def record_opt_out(
        self,
        lead_id: str,
        *,
        channel: str | None = None,
        reason: str | None = None,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        occurred_at: Any = None,
    ) -> dict[str, Any]:
        """Honour a request not to be contacted, and make it change behaviour, not just history."""
        lead = self._lead(lead_id)
        channel_clean = self._channel(channel) or "*"
        evidence = self._evidence(
            ([{"kind": "CUSTOMER_MESSAGE", "note": evidence_note}] if evidence_note else [])
            + ([{"kind": "URL", "source_url": evidence_url}] if evidence_url else [])
        )
        if not evidence:
            raise LedgerError(
                "An opt-out records something the business asked for, so it needs the message or page that said so "
                "(--evidence-note or --evidence-url).",
                code="EVIDENCE_REQUIRED",
            )
        blocks = self._collection("contact_blocks")
        existing = next(
            (block for block in blocks.values()
             if block.get("lead_id") == lead["lead_id"] and block.get("channel") == channel_clean and not block.get("revoked_at")),
            None,
        )
        when = normalize_datetime(occurred_at or now(), field="occurred_at")
        if existing is not None:
            existing["evidence"] = list(existing.get("evidence") or []) + evidence
            existing["updated_at"] = now()
            self._commit()
            return {**existing, "unchanged": True}
        block_id = next_numeric_id(blocks, "block")
        blocks[block_id] = {
            "block_id": block_id,
            "lead_id": lead["lead_id"],
            "channel": channel_clean,
            "reason": redact_contact_details(reason) if reason else None,
            "evidence": evidence,
            "recorded_at": now(),
            "effective_at": when,
            "revoked_at": None,
            "revoked_reason": None,
            "data_classification": self._classification(lead["lead_id"]),
        }
        cancelled: list[str] = []
        for record in self._collection("follow_ups").values():
            if record.get("status") != "SCHEDULED" or record.get("lead_id") != lead["lead_id"]:
                continue
            if record.get("channel") not in {channel_clean, None, "*"} and channel_clean != "*":
                continue
            record.update({"status": "CANCELLED", "cancellation_reason": "OPT_OUT_RECORDED", "cancelled_at": now(), "updated_at": now()})
            cancelled.append(str(record["follow_up_id"]))
        self._commit()
        self.record_event(
            lead["lead_id"],
            kind="OPT_OUT_RECORDED",
            direction="INBOUND",
            channel=channel_clean if channel_clean != "*" else None,
            summary=redact_contact_details(reason or f"Opt-out recorded for {'all channels' if channel_clean == '*' else channel_clean}."),
            occurred_at=when,
            evidence=evidence,
            project_id=lead["project_id"],
        )
        return {**blocks[block_id], "cancelled_follow_ups": cancelled}

    @_ledger_transaction

    def revoke_opt_out(self, block_id: str, *, reason: str, evidence_note: str | None = None) -> dict[str, Any]:
        cleaned_id = validate_record_id(block_id, kind="block_id")
        blocks = self._collection("contact_blocks")
        block = blocks.get(cleaned_id)
        if not isinstance(block, dict):
            raise LedgerError(f"Unknown opt-out record '{cleaned_id}'.", code="UNKNOWN_OPT_OUT")
        if block.get("revoked_at"):
            return {**block, "unchanged": True}
        try:
            cleaned_reason = redact_contact_details(validate_bounded_text(reason, field="reason", limit=MAX_NOTE_CHARS))
        except ValueError as error:
            raise LedgerError(str(error), code="REASON_REQUIRED") from None
        evidence = self._evidence([{"kind": "CUSTOMER_MESSAGE", "note": evidence_note or cleaned_reason}])
        block.update({"revoked_at": now(), "revoked_reason": cleaned_reason, "revocation_evidence": evidence, "updated_at": now()})
        self._commit()
        self.record_event(
            str(block["lead_id"]),
            kind="OPT_OUT_REVOKED",
            direction="INBOUND",
            channel=block.get("channel") if block.get("channel") != "*" else None,
            summary=f"Opt-out {cleaned_id} revoked: {cleaned_reason}",
            occurred_at=now(),
            evidence=evidence,
        )
        return block

    # ------------------------------------------------------------------ views

    @staticmethod
    def _bounded(limit: Any, *, default: int = 50, maximum: int = 200) -> int:
        """Clamp a caller's page size instead of trusting it, the way the other list commands do."""
        try:
            value = int(default if limit is None else limit)
        except (TypeError, ValueError):
            raise LedgerError("limit must be an integer.", code="LIMIT_INVALID") from None
        if value <= 0:
            raise LedgerError("limit must be at least 1.", code="LIMIT_INVALID")
        return min(value, maximum)

    @staticmethod
    def _offset(offset: Any) -> int:
        try:
            value = int(0 if offset is None else offset)
        except (TypeError, ValueError):
            raise LedgerError("offset must be an integer.", code="OFFSET_INVALID") from None
        if value < 0:
            raise LedgerError("offset cannot be negative.", code="OFFSET_INVALID")
        return value

    def pipeline(self, *, stage: str | None = None, limit: Any = 50, offset: Any = 0, today: Any = None) -> dict[str, Any]:
        """Every lead the ledger knows about, by relationship stage, one row at a time.

        Read-only and deliberately non-deriving: the stage shown is what the records proved when they were
        written, and `stage_is_current` says whether re-deriving it now would say the same thing - which is how
        a hand-edited state file becomes visible instead of quietly corrected.
        """
        limit_value = self._bounded(limit)
        offset_value = self._offset(offset)
        reference = normalize_datetime(today, field="today", allow_future=True)[:10] if today else datetime.now(timezone.utc).date().isoformat()
        wanted = None
        if stage:
            wanted = validate_bounded_text(stage, field="stage", limit=30).upper()
            if wanted not in RELATIONSHIP_STAGES:
                raise LedgerError(f"Unsupported relationship stage '{wanted}'.", code="RELATIONSHIP_STAGE_UNSUPPORTED")
        due_by_lead: dict[str, list[dict[str, Any]]] = {}
        for record in self.due_follow_ups(today=reference).get("due", []):
            due_by_lead.setdefault(str(record.get("lead_id")), []).append(record)
        rows: list[dict[str, Any]] = []
        stage_counts: dict[str, int] = {name: 0 for name in RELATIONSHIP_STAGES}
        for lead_id, lead in sorted((self.store.state.get("leads") or {}).items()):
            relationship_row = self._collection("relationships").get(lead_id)
            if not isinstance(relationship_row, dict):
                continue  # a lead the ledger has never been asked about is not part of this pipeline
            stage_name = str(relationship_row.get("stage"))
            stage_counts[stage_name] = stage_counts.get(stage_name, 0) + 1
            if wanted and stage_name != wanted:
                continue
            proposals = [row for row in self._collection("proposals").values() if row.get("lead_id") == lead_id]
            outcome = self._collection("outcomes").get(lead_id) or {}
            rows.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "project_id": relationship_row.get("project_id"),
                "stage": stage_name,
                "stage_changed_at": relationship_row.get("stage_changed_at"),
                "stage_is_current": relationship_row.get("stage") == self._derive_stage(lead_id)["stage"],
                "next_action": relationship_row.get("next_action"),
                "outcome_status": outcome.get("status") or "UNRESOLVED",
                "proposal_statuses": sorted(str(row.get("status")) for row in proposals),
                "due_follow_ups": [str(row.get("follow_up_id")) for row in due_by_lead.get(lead_id, [])],
                "contact_blocked": self.is_blocked(lead_id, None) is not None,
                "data_classification": relationship_row.get("data_classification"),
            })
        rows.sort(key=lambda row: (str(row.get("stage")), str(row.get("lead_id"))))
        page = rows[offset_value:offset_value + limit_value]
        return {
            "as_of": reference,
            "filters": {"stage": wanted},
            "total": len(rows),
            "returned": len(page),
            "offset": offset_value,
            "limit": limit_value,
            "next_offset": offset_value + len(page) if offset_value + len(page) < len(rows) else None,
            "stage_counts": {key: value for key, value in sorted(stage_counts.items()) if value},
            "rows": page,
            "note": "Rows are ledger facts only; a lead with no ledger record is absent, which is not the same as 'nothing happened'.",
        }

    def outstanding_work(self, *, today: Any = None, limit: Any = 50) -> dict[str, Any]:
        """Everything a human owes the pipeline, from the records - no timers, no inference."""
        limit_value = self._bounded(limit)
        due = self.due_follow_ups(today=today)
        awaiting_decision = [
            {k: row.get(k) for k in ("proposal_id", "lead_id", "business_name", "status", "presented_at", "project_id")}
            for row in self._collection("proposals").values() if row.get("status") == "PRESENTED"
        ]
        awaiting_presentation = [
            {k: row.get(k) for k in ("proposal_id", "lead_id", "status", "project_id")}
            for row in self._collection("proposals").values() if row.get("status") in {"DRAFT", "READY_FOR_PRESENTATION"}
        ]
        awaiting_reply: list[dict[str, Any]] = []
        by_lead: dict[str, list[dict[str, Any]]] = {}
        for row in self._collection("engagements").values():
            by_lead.setdefault(str(row.get("lead_id")), []).append(row)
        for lead_id, rows in by_lead.items():
            responses = [row for row in rows if row.get("kind") == "RESPONSE_RECEIVED"]
            if not responses or not any(row.get("needs_human_review") for row in responses):
                continue
            newest = max(responses, key=lambda row: (str(row.get("occurred_at") or ""), str(row.get("engagement_id"))))
            later = [row for row in rows if row.get("direction") == "OUTBOUND"
                     and (str(row.get("occurred_at") or ""), str(row.get("engagement_id")))
                     > (str(newest.get("occurred_at") or ""), str(newest.get("engagement_id")))]
            if later:
                continue
            awaiting_reply.append({
                "lead_id": lead_id,
                "engagement_id": newest.get("engagement_id"),
                "channel": newest.get("channel"),
                "received_at": newest.get("occurred_at"),
                "classification": newest.get("classification"),
            })
        blocks = [row for row in self._collection("contact_blocks").values() if not row.get("revoked_at")]
        return {
            "as_of": now(),
            "due_follow_ups": due["due"][:limit_value],
            "due_follow_up_count": due["due_count"],
            "suppressed_by_opt_out": due["suppressed_by_opt_out"],
            "proposals_awaiting_decision": sorted(awaiting_decision, key=lambda row: str(row.get("proposal_id")))[:limit_value],
            "proposals_awaiting_presentation": sorted(awaiting_presentation, key=lambda row: str(row.get("proposal_id")))[:limit_value],
            "responses_awaiting_reply": sorted(awaiting_reply, key=lambda row: str(row.get("lead_id")))[:limit_value],
            "active_contact_blocks": [
                {"block_id": row.get("block_id"), "lead_id": row.get("lead_id"), "channel": row.get("channel")}
                for row in sorted(blocks, key=lambda row: str(row.get("block_id")))[:limit_value]
            ],
            "meaning": "Work listed here is waiting on a person. Nothing in this report changes a record, a stage, or an outcome.",
        }

    def outcome_report(self, *, statuses: list[str] | None = None, limit: Any = 50) -> dict[str, Any]:
        """Won, lost, and the rest, with the evidence each one was recorded against."""
        limit_value = self._bounded(limit)
        wanted = None
        if statuses:
            wanted = set()
            for item in statuses:
                cleaned = validate_bounded_text(item, field="status", limit=30).upper()
                if cleaned not in OUTCOME_STATUSES:
                    raise LedgerError(f"Unsupported outcome status '{cleaned}'.", code="OUTCOME_STATUS_UNSUPPORTED")
                wanted.add(cleaned)
        rows = []
        counts: dict[str, int] = {}
        for lead_id, record in sorted((self._collection("outcomes") or {}).items()):
            status = str(record.get("status"))
            counts[status] = counts.get(status, 0) + 1
            if wanted and status not in wanted:
                continue
            rows.append({
                "lead_id": lead_id,
                "business_name": record.get("business_name"),
                "project_id": record.get("project_id"),
                "status": status,
                "previous_status": record.get("previous_status"),
                "effective_at": record.get("effective_at"),
                "reference": record.get("reference"),
                "evidence_kinds": sorted({str(row.get("kind")) for row in record.get("evidence") or []}),
                "revenue_recorded": bool(record.get("revenue_recorded")),
            })
        return {
            "count": len(rows[:limit_value]),
            "counts_by_status": counts,
            "outcomes": rows[:limit_value],
            "note": "An outcome states what was recorded, with its evidence kinds; no revenue figure is kept here at all.",
        }

    def ledger(self, lead_id: str | None = None, *, project_id: str | None = None) -> dict[str, Any]:
        """Everything known about one prospect, joined from the records that already exist."""
        if lead_id is None and project_id is None:
            raise LedgerError("Provide a lead_id or --project-id to view one prospect's ledger.", code="LEDGER_SCOPE_REQUIRED")
        if lead_id is not None:
            lead = self._lead(lead_id)
            lead_id = lead["lead_id"]

        def matches(record: dict[str, Any]) -> bool:
            if lead_id is not None:
                return record.get("lead_id") == lead_id
            return record.get("project_id") == project_id

        events = sorted((row for row in self._collection("engagements").values() if matches(row)),
                        key=lambda row: (str(row.get("occurred_at")), str(row.get("engagement_id"))))
        proposals = [row for row in self._collection("proposals").values() if matches(row)]
        follow_ups = [row for row in self._collection("follow_ups").values() if matches(row)]
        blocks = [row for row in self._collection("contact_blocks").values() if matches(row)]
        outcome = self._collection("outcomes").get(lead_id) if lead_id else None
        linked_lead_id = lead_id or next((str(row.get("lead_id")) for row in events if row.get("lead_id")), None)
        outreach_rows = [
            row for row in (self.store.state.get("outreach") or {}).values()
            if (lead_id is not None and row.get("lead_id") == lead_id) or (project_id is not None and row.get("project_id") == project_id)
        ]
        sends = [row for row in events if row.get("kind") == "OUTREACH_SENT"]
        failures = [row for row in events if row.get("kind") == "OUTREACH_SEND_FAILED"]
        responses = [row for row in events if row.get("kind") == "RESPONSE_RECEIVED"]
        attempts: dict[str, dict[str, int]] = {}
        for row in [*sends, *failures]:
            bucket = attempts.setdefault(str(row.get("channel") or "UNKNOWN"), {"sent": 0, "failed": 0})
            bucket["sent" if row.get("kind") == "OUTREACH_SENT" else "failed"] += 1
        relationship_row = self._collection("relationships").get(linked_lead_id or "")
        return {
            "lead_id": linked_lead_id,
            "project_id": project_id or (events[0].get("project_id") if events else None),
            "outcome": outcome or {"status": "UNRESOLVED", "recorded": False},
            "outreach": {
                "prepared": len(outreach_rows),
                "awaiting_review": sum(1 for row in outreach_rows if row.get("status") == "AWAITING_REVIEW"),
                "approved_awaiting_send": sum(1 for row in outreach_rows if row.get("status") == "READY_TO_SEND"),
                "planned": sum(1 for row in outreach_rows if row.get("status") == "READY_TO_SEND"),
                "attempted": sum(1 for row in outreach_rows if row.get("status") in {"SEND_FAILED", "SENT_REPORTED"}),
                "reported_sent": len(sends),
                "reported_failed": len(failures),
                "accepted_by_provider": 0,
                "delivered_verified": 0,
                "provider_verified_sends": 0,
                "statuses": sorted({str(row.get("status")) for row in outreach_rows}),
                "attempts_by_channel": attempts,
            },
            "relationship": relationship_row or {"stage": "NOT_CONTACTED", "recorded": False},
            "responses_received": len(responses),
            "proposals": {
                "count": len(proposals),
                "presented": sum(1 for row in proposals if row.get("status") in {"PRESENTED", "ACCEPTED", "DECLINED"}),
                "accepted": sum(1 for row in proposals if row.get("status") == "ACCEPTED"),
                "by_status": sorted(str(row.get("status")) for row in proposals),
            },
            "follow_ups": {
                "scheduled": sum(1 for row in follow_ups if row.get("status") == "SCHEDULED"),
                "completed": sum(1 for row in follow_ups if row.get("status") == "COMPLETED"),
                "cancelled": sum(1 for row in follow_ups if row.get("status") == "CANCELLED"),
            },
            "active_contact_blocks": [
                {"block_id": row.get("block_id"), "channel": row.get("channel"), "recorded_at": row.get("recorded_at")}
                for row in blocks if not row.get("revoked_at")
            ],
            "events": events,
            "truth_notes": [
                "prepared drafts are not sends",
                "reported sends are operator statements with provider_verified: false",
                "created proposals are not presented proposals",
                "an absence of reply changes nothing by itself",
                "outcomes require the customer-side evidence that was recorded",
            ],
        }

