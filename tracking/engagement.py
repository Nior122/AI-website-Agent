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
* free text is redacted of email addresses and phone numbers on the way in, because the ledger's subject is
  the *business*, not a person's contact details.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import hashlib
import re

from tracking.store import next_numeric_id, now, validate_bounded_text, validate_record_id

#: Collections this module owns. Kept in sync with ``tracking/database.py`` (persistence) and
#: ``TrackingStore._empty_state`` by ``tests/test_phase46_engagement_ledger.py``.
ENGAGEMENT_COLLECTIONS = ("engagements", "proposals", "follow_ups", "outcomes", "contact_blocks")

EVENT_KINDS = {
    "OUTREACH_PREPARED", "OUTREACH_SENT", "RESPONSE_RECEIVED", "FOLLOW_UP_SCHEDULED", "FOLLOW_UP_COMPLETED",
    "FOLLOW_UP_CANCELLED", "PROPOSAL_CREATED", "PROPOSAL_PRESENTED", "PROPOSAL_DECISION", "OUTCOME_RECORDED",
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


class EngagementLedger:
    """Write-only-by-invitation engagement records over ``TrackingStore`` state."""

    def __init__(self, store, *, source: str = "tracking-cli") -> None:
        self.store = store
        self.source = validate_bounded_text(source, field="source", limit=60)
        for collection in ENGAGEMENT_COLLECTIONS:
            self.store.state.setdefault(collection, {})

    # ------------------------------------------------------------------ shared internals

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

    def find_event(self, dedupe_key: str) -> dict[str, Any] | None:
        for record in self._collection("engagements").values():
            if record.get("dedupe_key") == dedupe_key:
                return record
        return None

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
        if kind in {"RESPONSE_RECEIVED", "OUTREACH_SENT", "PROPOSAL_PRESENTED", "PROPOSAL_DECISION", "OUTCOME_RECORDED", "OPT_OUT_RECORDED"} and not evidence_rows:
            raise LedgerError(
                f"'{kind}' asserts something a person said or did, so it needs at least one evidence item "
                "(--evidence-note or --evidence-url). An unverified claim is not recorded as a fact.",
                code="EVIDENCE_REQUIRED",
            )
        if outreach_id:
            self._known_outreach(outreach_id)

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
            "data_classification": self._classification(lead["lead_id"]),
            "contact_details_redacted": redact_contact_details(summary) != summary,
            "duplicate": False,
        }
        engagements[event_id] = record
        self.store._save()
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

    def is_blocked(self, lead_id: str, channel: str | None) -> dict[str, Any] | None:
        for block in (self._collection("contact_blocks") or {}).values():
            if block.get("lead_id") != lead_id or block.get("revoked_at"):
                continue
            if block.get("channel") in {channel, "*", None}:
                return block
        return None

    # ------------------------------------------------------------------ outreach sending status

    def mark_outreach_sent(
        self,
        outreach_id: str,
        *,
        occurred_at: Any = None,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        provider_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Record that a human actually sent an approved draft - and no more than that."""
        cleaned_id = validate_record_id(outreach_id, kind="outreach_id")
        outreach = self._known_outreach(cleaned_id)
        status = outreach.get("status")
        if status == "SENT_REPORTED":
            return {**outreach, "unchanged": True}
        if status != "READY_TO_SEND":
            raise LedgerError(
                f"Outreach '{cleaned_id}' is '{status}'; only a human-approved draft (READY_TO_SEND) can be reported "
                "as sent. Preparing a message is never sending it.",
                code="OUTREACH_NOT_APPROVED",
            )
        channel = str(outreach.get("channel") or "EMAIL")
        lead_id = str(outreach.get("lead_id") or "")
        block = self.is_blocked(lead_id, channel)
        if block is not None:
            raise LedgerError(
                f"Contact is blocked for lead '{lead_id}' on channel '{block.get('channel')}' "
                f"({block.get('reason') or 'no reason recorded'}); the send was not recorded.",
                code="CONTACT_BLOCKED",
            )
        counted = [
            record for record in self._collection("engagements").values()
            if record.get("kind") == "OUTREACH_SENT" and record.get("lead_id") == lead_id and record.get("channel") == channel
        ]
        if len(counted) >= MAX_SENDS_PER_LEAD_PER_CHANNEL:
            raise LedgerError(
                f"Lead '{lead_id}' already has {len(counted)} recorded sends on '{channel}' (the limit is "
                f"{MAX_SENDS_PER_LEAD_PER_CHANNEL}); a further approach needs a new outreach record and a human review decision.",
                code="CONTACT_LIMIT_REACHED",
            )
        evidence: list[dict[str, Any]] = []
        if evidence_note:
            evidence.append({"kind": "OPERATOR_NOTE", "note": evidence_note})
        if evidence_url:
            evidence.append({"kind": "URL", "source_url": evidence_url})
        if not evidence:
            raise LedgerError(
                "Reporting a send needs evidence that it went out (--evidence-note or --evidence-url); "
                "without it this would be an unverified claim recorded as fact.",
                code="EVIDENCE_REQUIRED",
            )
        if provider_message_id:
            evidence.append({
                "kind": "OPERATOR_NOTE",
                "note": f"Provider message id recorded verbatim, not verified against the provider: {provider_message_id}"[:MAX_NOTE_CHARS],
            })
        event = self.record_event(
            lead_id,
            kind="OUTREACH_SENT",
            direction="OUTBOUND",
            channel=channel,
            summary=f"Operator reported sending outreach draft {cleaned_id} for {outreach.get('project_id') or 'the project'}.",
            occurred_at=occurred_at or now(),
            evidence=evidence,
            project_id=outreach.get("project_id"),
            outreach_id=cleaned_id,
        )
        outreach.update({
            "status": "SENT_REPORTED",
            "sent_at": event["occurred_at"],
            "send_engagement_id": event["engagement_id"],
            "delivery_status": "REPORTED_BY_OPERATOR",
            "provider_verified": False,
        })
        self.store._save()
        return {**outreach, "send_event": event, "delivery_note": "Reported by an operator; no provider receipt was verified by this system."}

    # ------------------------------------------------------------------ inbound responses

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
    ) -> dict[str, Any]:
        """Store what the prospect actually said, and never what we hoped they would say."""
        evidence = [{"kind": evidence_kind, "note": evidence_note, "quote": quote, "observed_at": received_at}]
        if evidence_url:
            evidence.append({"kind": "URL", "source_url": evidence_url})
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
        )
        return event

    # ------------------------------------------------------------------ follow-ups

    def schedule_follow_up(
        self,
        lead_id: str,
        *,
        due_at: Any,
        reason: str,
        channel: str | None = None,
        proposal_id: str | None = None,
        allow_past_due: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        lead = self._lead(lead_id)
        channel_clean = self._channel(channel)
        block = self.is_blocked(lead["lead_id"], channel_clean)
        if block is not None:
            raise LedgerError(
                f"Follow-up not scheduled: contact blocked for '{lead['lead_id']}' on '{block.get('channel')}'.",
                code="CONTACT_BLOCKED",
            )
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
            "channel": channel_clean,
            "reason": cleaned_reason,
            "due_at": due,
            "status": "SCHEDULED",
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
        self.store._save()
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
            # Re-key so the original schedule and this one are not treated as the same pending item.
            record["dedupe_key"] = _sha(str(record["lead_id"]), str(record["due_at"])[:10], str(record.get("channel") or "-"), str(record["reason"])[:120])
        elif verb == "COMPLETE":
            record.update({"status": "COMPLETED", "completed_at": when, "completion_notes": redact_contact_details(notes or evidence_note), "evidence": evidence})
        else:
            record.update({"status": "CANCELLED", "cancellation_reason": redact_contact_details(notes or evidence_note or "cancelled"), "cancelled_at": when, "evidence": evidence})
        record["updated_at"] = now()
        self.store._save()
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
        return {
            "as_of": reference,
            "scheduled_count": len(scheduled),
            "due_count": len(due),
            "due": sorted(due, key=lambda item: str(item.get("due_at"))),
            "suppressed_by_opt_out": [record["follow_up_id"] for record in blocked],
            "note": "Overdue is a scheduling fact, not a decision by the prospect; nothing here was changed.",
        }

    # ------------------------------------------------------------------ proposals

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
            "data_classification": self._classification(lead["lead_id"]),
            "note": "A created proposal is not a presented one; presentation and decisions are recorded separately.",
            "duplicate": False,
        }
        proposals[proposal_id] = record
        self.store._save()
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

    def _known_proposal(self, proposal_id: str) -> dict[str, Any]:
        record = self._collection("proposals").get(validate_record_id(proposal_id, kind="proposal_id"))
        if not isinstance(record, dict):
            raise LedgerError(f"Unknown proposal '{proposal_id}'.", code="UNKNOWN_PROPOSAL")
        return record

    def update_proposal(
        self,
        proposal_id: str,
        *,
        status: str,
        evidence_note: str | None = None,
        evidence_url: str | None = None,
        occurred_at: Any = None,
        notes: str | None = None,
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
        evidence = self._evidence(
            ([{"kind": "OPERATOR_NOTE", "note": evidence_note}] if evidence_note else [])
            + ([{"kind": "URL", "source_url": evidence_url}] if evidence_url else [])
        )
        if target in {"PRESENTED", "ACCEPTED", "DECLINED"} and not evidence:
            raise LedgerError(
                f"'{target}' describes something that happened to a real prospect, so it needs evidence "
                "(--evidence-note and/or --evidence-url).",
                code="EVIDENCE_REQUIRED",
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
        self.store._save()
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
            self.store._save()
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
        self.store._save()
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
            self.store._save()
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
        self.store._save()
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
        self.store._save()
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
        responses = [row for row in events if row.get("kind") == "RESPONSE_RECEIVED"]
        return {
            "lead_id": linked_lead_id,
            "project_id": project_id or (events[0].get("project_id") if events else None),
            "outcome": outcome or {"status": "UNRESOLVED", "recorded": False},
            "outreach": {
                "prepared": len(outreach_rows),
                "approved_awaiting_send": sum(1 for row in outreach_rows if row.get("status") == "READY_TO_SEND"),
                "reported_sent": len(sends),
                "provider_verified_sends": 0,
                "statuses": sorted({str(row.get("status")) for row in outreach_rows}),
            },
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

