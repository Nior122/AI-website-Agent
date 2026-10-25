"""The send executor: turn one *approved* outreach record into at most one provider call.

Nothing here decides whether a message should go out. The durable decision is the outreach record's
``READY_TO_SEND`` status and its reviewer attribution (written by :meth:`outreach.service.OutreachService.review`
from the Telegram buttons, the CLI, or the HTTP API - never by this file), and the durable accounting is
:meth:`tracking.engagement.EngagementLedger.mark_outreach_sent`. This module sits between them and the network;
its whole job is to make the gap between "a human said yes" and "Gmail accepted a message" auditable:

1. **Re-read everything at send time.** Lead, draft, project, deployment, hold, contact budget, opt-out list, and
   the draft's content digest. An approval is a statement about specific words sent to a specific person, so a
   recipient, subject, body, or preview URL that changed after the review makes it stale (``APPROVAL_STALE``)
   rather than re-usable - a fresh send needs a fresh human decision.
2. **Write the attempt down *before* contacting the provider.** That durable marker is what makes a crash or a
   killed container recoverable: the next run finds ``SENDING`` with no provider id, refuses to send again, and
   asks for reconciliation instead of cheerfully mailing the same prospect twice.
3. **Classify an unanswered provider honestly.** Gmail has no idempotency key, so a request that timed out *after*
   it was submitted is ``SEND_UNCERTAIN``, not ``FAILED`` - and the executor never resends one. Only an explicit
   refusal (a 4xx saying the message was not accepted) is a clean failure, and even that keeps the human approval
   so a deliberate retry reuses the same record.
4. **Claim exactly what the evidence supports.** A provider message id that the *read-only* adapter can retrieve
   and match to this draft's recipient and subject is recorded as ``ACCEPTED_BY_PROVIDER`` with
   ``provider_verified: true``. An id that cannot be read back is ``REPORTED_SENT`` with ``provider_verified:
   false``. ``VERIFIED_DELIVERED`` has no path here at all: Gmail issues no delivery, read, or open receipt.

The hold, the live-effect flag, and the recipient's opt-out are consulted on every real path, and a dry run
performs no network call at all, so "what would happen?" never costs anything or contacts anybody.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from orchestration import controls
from tracking.engagement import EngagementLedger, LedgerError

#: The results this executor can produce. Anything stronger is the ledger's to refuse, not ours to phrase.
OUTCOME_REPORTED = "REPORTED_SENT"
OUTCOME_ACCEPTED = "ACCEPTED_BY_PROVIDER"
OUTCOME_FAILED = "FAILED"
#: Named so a reader can see the claim is never made here, rather than hunting for where it is granted.
OUTCOME_UNREACHABLE = ("VERIFIED_DELIVERED",)
#: Faults that mean "we do not know". A timeout or DNS failure may have reached Google; so may a 5xx that arrived
#: after the message was accepted. A 4xx is an explicit "not accepted", which is the only case a retry is safe.
UNCERTAIN_CODES = {"GMAIL_UNRESPONSIVE", "GMAIL_CALL_FAILED", "PROVIDER_UNRESPONSIVE", "PROVIDER_CALL_FAILED",
                   "GMAIL_RESPONSE_MALFORMED", "GMAIL_REQUEST_MALFORMED"}
SEND_CHANNEL = "EMAIL"
#: The statuses an approved draft may be in when a send is attempted. `SEND_FAILED` is included because a
#: reported failure keeps the human approval by design - the approval is about the content, and a delivery
#: failure is a reason to try again deliberately, not a reason to start a new review.
SENDABLE_STATUSES = {"READY_TO_SEND", "SEND_FAILED"}
#: The state names on an outreach record's ``send_state`` field, owned by this module (plus the clearing writes
#: :meth:`tracking.engagement.EngagementLedger.mark_outreach_sent` makes once a send is accounted for).
SEND_STATES = ("DRY_RUN", "SENDING", "SENT", "FAILED", "UNCERTAIN")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def send_dedupe_key(outreach_id: str, fingerprint: str) -> str:
    """One logical send of one approved content version. A second call returns the stored event."""
    return f"outreach-send:{outreach_id}:{fingerprint}"


def content_fingerprint(record: Mapping[str, Any]) -> str:
    """The digest of what a reviewer was shown, from the same function the approval was bound to."""
    from outreach.service import content_fingerprint as fingerprint_of

    return fingerprint_of(record)


class AddressSuppression:
    """Addresses the ledger says not to contact, recomputed on demand.

    Deliberately not a store: it reads ``contact_blocks`` and the leads behind them, so there is one list of
    opt-outs in this application and it cannot drift. A revoked block disappears from it the moment the ledger
    records the revocation.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def addresses(self) -> set[str]:
        values: set[str] = set()
        blocks = self.store.state.get("contact_blocks") or {}
        leads = self.store.state.get("leads") or {}
        for block in blocks.values():
            if block.get("revoked_at"):
                continue
            if str(block.get("channel") or "*") not in {SEND_CHANNEL, "*"}:
                continue
            lead = leads.get(str(block.get("lead_id") or "")) or {}
            for candidate in (lead.get("email"), block.get("contact_detail"), block.get("address")):
                address = _address(candidate)
                if address:
                    values.add(address)
        return values

    def allowed(self, address: Any) -> bool:
        return not (_address(address) in self.addresses())


class OutboundRefusal(ValueError):
    """A refusal to send, with a code, a reason an operator can read, and the action that would change it."""

    def __init__(self, code: str, message: str, *, next_action: str | None = None, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_action = next_action
        self.detail = detail or {}

    def describe(self) -> dict[str, Any]:
        return {"code": self.code, "detail": str(self), "next_action": self.next_action, "sent": False,
                **({"data": self.detail} if self.detail else {})}


class ApprovedOutboundSender:
    """Sends approved outreach over Gmail and refuses everything else with a reason.

    ``client`` is a :class:`integrations.gmail_send.GmailSendAdapter` (or anything with that same one-method
    surface), built from the environment only when a real send or an explicit check is asked for - so listing an
    outbox never touches a credential. ``readback_client`` is the *read-only*
    :class:`integrations.gmail.GmailClient`, used solely to prove a returned message id is real. Both are
    injectable, which is how the tests stay offline and how an operator points them at a proxy.
    """

    def __init__(self, store: Any, *, outreach: Any = None, ledger: Any = None, client: Any = None,
                 readback_client: Any = None, config_source: Mapping[str, str] | None = None,
                 allow_live: bool = False, suppression: Any = None) -> None:
        self.store = store
        # The same ledger every other send path writes through, under its own source label so an audit can see
        # which surface recorded a row. Never a parallel store of sends.
        self.ledger = ledger if ledger is not None else EngagementLedger(store, source="outbound-send")
        self.outreach = outreach if outreach is not None else _default_outreach_service(store)
        self._client = client
        self._readback = readback_client
        self.config_source = config_source
        self.allow_live = bool(allow_live)
        self._suppression = suppression
        store.state.setdefault("outreach", {})

    # ------------------------------------------------------------------ what is approved and unsent

    def outbox(self, *, status: str = "READY_TO_SEND", limit: Any = 25) -> dict[str, Any]:
        """The approved-but-unsent list, each row annotated with why it is or is not sendable *now*.

        A row is never called sendable because its status says so: every one carries the live eligibility answer
        (opt-out, contact budget, hold) and whether its content still matches what a human approved. That is what
        makes the list safe to act on - and it is a reading of the ledger, not a queue this process drains.
        """
        wanted = str(status or "READY_TO_SEND").strip().upper()
        if wanted not in SENDABLE_STATUSES | {"AWAITING_REVIEW", "SENT_REPORTED"}:
            raise OutboundRefusal("OUTBOX_STATUS_UNSUPPORTED", f"no outbox view for status '{wanted}'",
                                  next_action="ask for READY_TO_SEND (approved, unsent), AWAITING_REVIEW, "
                                              "SEND_FAILED, or SENT_REPORTED")
        rows = [self.annotate(record) for record in self.store.state["outreach"].values()
                if record.get("channel") == SEND_CHANNEL and record.get("status") == wanted]
        rows.sort(key=lambda item: str(item.get("generated_at") or "") + str(item.get("outreach_id")))
        gate = controls.gate_reason(self.store)
        configuration = self.configuration()
        return {"status": wanted, "outbox": rows[:_bounded(limit)], "total": len(rows),
                "sendable": [row["outreach_id"] for row in rows if row["eligible"]],
                "automation_held": bool(gate), "gate_reason": gate[1] if gate else None,
                "sending_state": configuration["state"], "sending_enabled": configuration["state"] in {"configured", "verified"},
                "live_effect_enabled": self.allow_live,
                "results_provable": [OUTCOME_REPORTED, OUTCOME_ACCEPTED],
                "results_unreachable": list(OUTCOME_UNREACHABLE),
                "next_action": ("nothing sends from being listed; an operator names one draft and confirms it, "
                                "over Telegram or with `email-send`"),
                "capability_note": ("\"READY_TO_SEND\" is a human approval of specific content, not a delivery "
                                    "promise and not a schedule")}

    def annotate(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """One draft, judged now. Writes nothing, contacts nothing.

        The checks are the ones a human's approval cannot answer by itself - has this prospect opted out since, is
        dispatch held, is the preview still live, is the budget spent, did the content move - recomputed from the
        records that hold the answers.
        """
        outreach_id = str(record.get("outreach_id") or "")
        fingerprint = content_fingerprint(record)
        approved_fingerprint = record.get("approved_content_fingerprint")
        lead = self.store.state["leads"].get(str(record.get("lead_id") or "")) or {}
        project = self.store.state["projects"].get(str(record.get("project_id") or "")) or {}
        deployment = self.store.state["deployments"].get(str(record.get("deployment_id") or "")) or {}
        checks: list[dict[str, Any]] = []
        blockers: list[dict[str, str]] = []
        ledger_blockers: list[dict[str, str]] = []

        def check(name: str, passed: bool, detail: str) -> bool:
            checks.append({"check": name, "status": "CLEAR" if passed else "BLOCKED", "detail": detail})
            return passed

        check("APPROVAL", record.get("status") in SENDABLE_STATUSES,
              f"status {record.get('status')}; reviewed {record.get('reviewed_at') or 'never'} by "
              f"{record.get('reviewed_by') or 'unattributed'}"
              + ("" if record.get("status") != "SEND_FAILED" else
                 " - a reported failure keeps this approval, so a retry is a fresh operator decision on the same "
                 "content"))
        bound = check("APPROVAL_BOUND", bool(approved_fingerprint),
                      ("the approval carries a content digest" if approved_fingerprint else
                       "this draft was approved before approvals were bound to content, so no send is permitted "
                       "on it - re-approve the current text"))
        content_matches = check("CONTENT_STALENESS", bool(bound and approved_fingerprint == fingerprint),
                                ("content unchanged since the review" if bound and approved_fingerprint == fingerprint
                                 else "the recipient, subject, body, or preview URL changed after the review"))
        recipient = _address(record.get("recipient"))
        has_recipient = check("RECIPIENT", bool(recipient),
                              f"one parseable address ({_mask(recipient or '')})" if recipient
                              else "the lead's contact value is not one email address")
        block = None
        if self.ledger is not None and record.get("lead_id"):
            try:
                block = self.ledger.is_blocked(str(record["lead_id"]), SEND_CHANNEL)
            except LedgerError:
                block = None
        address_blocked = bool(recipient and recipient in self.suppression().addresses())
        opted_out = bool(block or address_blocked)
        check("OPT_OUT", not opted_out,
              ("no active opt-out for this lead and channel" if not opted_out else
               f"active block {block.get('block_id') if block else '(address on the suppression list)'}: an "
               "opt-out outranks any approval"))
        checks.append({"check": "OPT_OUT_SOURCE", "status": "CLEAR",
                       "detail": "blocks are read from the ledger's contact_blocks; this executor keeps no list "
                                 "of its own"})
        safety = _resafety(record, lead)
        check("DRAFT_SAFETY", safety is None, safety or "the draft still passes the outreach safety review")
        preview_live = check("PREVIEW", bool(deployment) and deployment.get("deployment_status") == "DEPLOYED"
                             and str(deployment.get("deployment_url") or "").startswith("https://"),
                             (f"build {record.get('build_id')} published at {deployment.get('deployment_url')}"
                              if deployment and str(deployment.get("deployment_url") or "").startswith("https://")
                              else "the preview the draft links to is not a deployed https URL"))
        build_current = check("BUILD_CURRENT", not project.get("assigned_build_id")
                              or record.get("build_id") == project.get("assigned_build_id"),
                              (f"approved build {record.get('build_id')} is still the assigned build"
                               if not project.get("assigned_build_id")
                               or record.get("build_id") == project.get("assigned_build_id") else
                               f"the assigned build is now {project.get('assigned_build_id')}; the approved one "
                               f"was {record.get('build_id')}"))
        lead_qualified = check("LEAD_QUALIFIED", lead.get("qualification_status") == "QUALIFIED",
                               f"qualification is {lead.get('qualification_status') or 'unrecorded'}")
        gate = controls.gate_reason(self.store)
        check("HOLD", not gate, "automated dispatch is not held" if not gate else gate[1])
        unresolved_state = str(record.get("send_state") or "")
        check("UNRESOLVED_SEND", unresolved_state not in {"SENDING", "UNCERTAIN"},
              ("no unresolved attempt on this draft" if unresolved_state not in {"SENDING", "UNCERTAIN"} else
               f"the last attempt is {unresolved_state.lower()} and unresolved: reconcile it rather than sending "
               "again"))
        already_sent = record.get("status") == "SENT_REPORTED" or unresolved_state == "SENT"
        check("NOT_ALREADY_SENT", not already_sent,
              "nothing has been accounted for on this approval yet" if not already_sent else
              "this approved content has already been reported on; a further approach needs a new draft and a "
              "new review")

        # The same eligibility answer the ledger applies to a manual send report, so a send through this executor
        # can never be *weaker* than one an operator types by hand: the contact budget, the opt-out block, and the
        # unresolved-attempt guard are read from the ledger rather than duplicated here.
        if record.get("lead_id"):
            try:
                verdict = self.ledger.outreach_eligibility(str(record["lead_id"]), SEND_CHANNEL,
                                                            outreach_id=outreach_id, for_action="SEND")
            except LedgerError as error:
                verdict = {"blockers": [error.code], "checks": [{"check": "LEDGER", "status": "BLOCKED",
                                                                 "detail": str(error)}]}
            checks.extend(verdict.get("checks") or [])
            ledger_blockers = [{
                "code": str(code),
                "reason": next((str(row.get("detail") or "") for row in verdict.get("checks") or []
                                if row.get("status") == "BLOCKED"), "") or f"the ledger reports {code}",
            } for code in verdict.get("blockers") or []]

        for passed, code, reason in (
            (not already_sent, "ALREADY_SENT", "this approval was already accounted for as sent"),
            (unresolved_state not in {"SENDING", "UNCERTAIN"}, "SEND_UNCERTAIN",
             "an earlier attempt to send this draft is unresolved"),
            (record.get("status") in SENDABLE_STATUSES, "NOT_APPROVED",
             f"the draft is {record.get('status')}; only a human-approved draft (READY_TO_SEND, or SEND_FAILED "
             "awaiting a deliberate retry) is sendable"),
            (bound, "APPROVAL_UNBOUND",
             "this draft carries no approval digest, so nothing proves which text the reviewer approved - it "
             "predates content binding and has to be approved again"),
            (bound and content_matches, "APPROVAL_STALE",
             "the content no longer matches the approval, so the approval no longer covers it"),
            (has_recipient, "RECIPIENT_UNPARSEABLE", "there is no single sendable address on the lead"),
            (not opted_out, "CONTACT_BLOCKED", "the recipient opted out; nothing here releases a block"),
            (not address_blocked, "ADDRESS_SUPPRESSED",
             "this address is attached to an opt-out, whoever the lead record says"),
            (safety is None, "DRAFT_UNSAFE", safety or "the draft failed the outreach safety review"),
            (preview_live, "PREVIEW_UNAVAILABLE", "the linked preview is not live over https"),
            (build_current, "DEPLOYMENT_STALE", "the approved build is no longer the assigned build"),
            (lead_qualified, "LEAD_NO_LONGER_QUALIFIED", "the lead is no longer qualified"),
            (not gate, controls.HOLD_CODE, gate[1] if gate else "automation is held"),
        ):
            if not passed:
                blockers.append({"code": code, "reason": reason})
        # This view's own names come first: "already sent" and "approval stale" tell an operator more than the
        # ledger's generic OUTREACH_NOT_APPROVED does, and the reason list is read top-down. Duplicated codes from
        # the ledger are dropped rather than repeated, because the same fact twice reads like two facts.
        for item in ledger_blockers:
            if item["code"] not in {existing.get("code") for existing in blockers}:
                blockers.append(item)
        return {"outreach_id": outreach_id, "project_id": record.get("project_id"),
                "lead_id": record.get("lead_id"), "business_name": lead.get("business_name"),
                "channel": record.get("channel"), "recipient": recipient, "subject": record.get("subject"),
                "status": record.get("status"), "generated_at": record.get("generated_at"),
                "content_fingerprint": fingerprint, "approval_bound_to_fingerprint": bool(approved_fingerprint),
                "reviewed_at": record.get("reviewed_at"), "reviewed_by": record.get("reviewed_by"),
                "reviewer_identity_source": record.get("reviewer_identity_source"),
                "send_state": record.get("send_state") or None,
                "send_state_detail": record.get("send_state_detail"),
                "provider_message_id": record.get("send_provider_message_id"),
                "delivery_status": record.get("delivery_status"),
                "provider_verified": bool(record.get("provider_verified")),
                "send_attempts": int(record.get("send_attempts") or 0),
                "checks": checks, "blockers": blockers, "eligible": not blockers,
                "grants_authorization": False,
                "capability_note": ("eligible means an operator may confirm one send of this draft; it is not "
                                    "permission for anything automatic, and it says nothing about delivery")}

    def preview(self, outreach_id: str) -> dict[str, Any]:
        """What the operator is about to authorize, in full, plus the live verdict on it.

        The body is included because a person approving a send has to read the words that will arrive; nothing
        else in this repository shows an outbound body on request.
        """
        record = self._record(outreach_id)
        annotation = self.preview_annotation(record)
        annotation.update({"draft_message": record.get("draft_message"), "subject": record.get("subject"),
                           "preview_url": record.get("preview_url"),
                           "personalization_points": record.get("personalization_points") or [],
                           "composition": self._composition(record, record.get("recipient"))})
        return annotation

    def preview_annotation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """The outbox verdict for a raw outreach record (shared with the Telegram operator view)."""
        return self.annotate(record)

    # ------------------------------------------------------------------ the one send path

    def send(self, outreach_id: str, *, actor_id: Any, identity_source: str, dry_run: bool = False,
             expected_fingerprint: str | None = None, confirmation: str | None = None,
             note: str | None = None) -> dict[str, Any]:
        """Attempt one send of one approved draft. One call, at most one provider request.

        ``actor_id`` and ``identity_source`` name the human who asked and are recorded beside the ledger event -
        they are not authorization. The authorization is ``READY_TO_SEND`` and its reviewer attribution, which
        this method verifies and never writes.
        """
        record = self._record(outreach_id)
        annotation = self.annotate(record)
        fingerprint = annotation["content_fingerprint"]
        if expected_fingerprint and str(expected_fingerprint) != fingerprint:
            raise OutboundRefusal("APPROVAL_STALE",
                                  "the draft changed between being shown and being sent, so nothing was sent",
                                  next_action="re-read the outbox and have a human approve the current content")
        if annotation["blockers"]:
            first = annotation["blockers"][0]
            raise OutboundRefusal(first["code"], first["reason"], next_action=_NEXT_ACTION.get(first["code"]))
        if not dry_run and not self.allow_live:
            raise OutboundRefusal("LIVE_EFFECTS_DISABLED",
                                  "an explicit live-effect flag is required before a message can leave this machine",
                                  next_action="run `email-send` with --allow-live, or set "
                                              "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH=1 for a scheduled run - and "
                                              "only once `email-send-check` says the credential works")
        composition = self._composition(record, annotation["recipient"])
        if composition.get("error"):
            raise OutboundRefusal("MESSAGE_NOT_COMPOSABLE", composition["error"],
                                  next_action="regenerate the draft and have a human approve the new content")
        prepared = composition["prepared"]
        dedupe_key = send_dedupe_key(outreach_id, fingerprint)

        if dry_run:
            # No provider contact at all: a preview cannot consume budget, mark a lead contacted, or mail anyone.
            return {"dry_run": True, "send_state": "DRY_RUN", "code": None, "outreach_id": outreach_id,
                    "content_fingerprint": fingerprint, "checks": annotation["checks"], "blockers": [],
                    "eligible": True, "composition": composition,
                    "would_contact": annotation["recipient"], "capability_note":
                    ("a dry run composes the exact request and proves nothing else: no provider was contacted, no "
                     "budget consumed, nothing recorded in the ledger")}

        if str(confirmation or "").strip().upper() != "SEND":
            raise OutboundRefusal("CONFIRMATION_REQUIRED",
                                  "a real send needs the explicit word SEND, because a repeated command must not "
                                  "mail the same prospect twice", next_action="re-run with --confirm=SEND")
        existing = self.ledger.find_event(dedupe_key)
        if existing:
            return {"dry_run": False, "send_state": "SENT", "code": "ALREADY_RECORDED",
                    "outreach_id": outreach_id, "engagement_id": existing.get("engagement_id"),
                    "content_fingerprint": fingerprint, "unchanged": True,
                    "detail": "this exact approved content was already sent and recorded",
                    "capability_note": ("the second call was answered from the ledger without contacting the "
                                        "provider")}

        adapter = self._adapter(build_if_missing=not dry_run)
        if adapter is None:
            raise OutboundRefusal("SEND_CLIENT_UNAVAILABLE",
                                  "this machine has no credential authorized for sending - no send-scope "
                                  "credentials exist here, which is not the same as having them switched off",
                                  next_action="authorize the send-only scope, set the three "
                                              "AI_WEBSITE_AGENT_GMAIL_SEND_* variables and the enable flag, then "
                                              "run `email-send-check`")

        # --- durable pre-attempt marker, written before the provider is contacted ------------------------
        self._write(outreach_id, {"send_state": "SENDING",
                                  "send_state_detail": f"attempted by {actor_id} via {identity_source} at "
                                                        f"{_now_iso()}",
                                  "send_attempted_at": _now_iso(), "send_actor": str(actor_id)[:120],
                                  "send_identity_source": str(identity_source)[:60],
                                  "send_fingerprint": fingerprint, "send_dedupe_key": dedupe_key,
                                  "send_provider": "gmail", "send_provider_message_id": None})
        try:
            outcome = adapter.send(prepared)
        except Exception as error:  # noqa: BLE001 - every fault below becomes a durable, typed answer
            outcome = self._classify_exception(error)

        provider_message_id = str(outcome.get("provider_message_id") or "") or None
        if outcome.get("code") == "SEND_UNCERTAIN":
            return self._record_uncertain(outreach_id, annotation, outcome, fingerprint,
                                           provider_message_id=provider_message_id)
        if not outcome.get("sent"):
            code = str(outcome.get("code") or "SEND_REFUSED_BY_ADAPTER")
            if outcome.get("provider_answered"):
                # Google said no. That is an attempt: it consumes a slot and keeps the approval, so the next
                # deliberate try reuses this record rather than starting a new review.
                self._write(outreach_id, {"send_provider_message_id": provider_message_id})
                try:
                    failed = self.ledger.mark_outreach_sent(outreach_id, result=OUTCOME_FAILED, provider="gmail",
                                                            failure_note=f"{code}: {str(outcome.get('reason') or '')[:160]}"
                                                            + (f" | asked by {actor_id}" if actor_id else ""),
                                                            idempotency_key=f"{dedupe_key}:failed:{int(record.get('send_attempts') or 0) + 1}")
                except LedgerError as error:
                    self._write(outreach_id, {"send_state": None,
                                              "send_state_detail": f"refused by the provider ({code}); the failure "
                                                                   f"could not be recorded: {error.code}"})
                    raise OutboundRefusal(error.code, f"the provider refused the send and the ledger refused the "
                                                 f"failure record: {error}",
                                          next_action="read both messages; the provider answer stands, the ledger "
                                                      "row is missing") from None
                self._write(outreach_id, {"send_state": "FAILED", "send_state_detail": f"refused by provider: {code}"})
                return {"dry_run": False, "send_state": "FAILED", "code": code, "outreach_id": outreach_id,
                        "engagement_id": (failed.get("send_event") or {}).get("engagement_id"),
                        "outreach_status": failed.get("status"), "delivery_status": failed.get("delivery_status"),
                        "contact_established": False, "content_fingerprint": fingerprint,
                        "reason": str(outcome.get("reason") or ""), "sent": False,
                        "next_action": str(outcome.get("next_action") or "fix what the refusal names"),
                        "capability_note": ("a refused send is recorded as a failure: the lead was not reached, "
                                            "the approval still stands, and the attempt counted")}
            self._write(outreach_id, {"send_state": None, "send_state_detail": f"not attempted: {code}",
                                     "send_attempted_at": None, "send_actor": None})
            raise OutboundRefusal(code, str(outcome.get("reason") or "the send adapter refused this message"),
                                  next_action=str(outcome.get("next_action") or "fix what the refusal names, then "
                                                  "send again"))

        # The id is recorded as soon as the provider returns one, so a ledger fault in the next line cannot make
        # this attempt look like a crashed, unknown-outcome send.
        self._write(outreach_id, {"send_provider_message_id": provider_message_id,
                                  "send_state_detail": "accepted by the provider; recording the ledger row"})
        verified, verify_detail, receipt = self._verify_against_account(provider_message_id, prepared,
                                                                          annotation["recipient"])
        result = OUTCOME_ACCEPTED if verified else OUTCOME_REPORTED
        # No `evidence_url`: the ledger requires an evidence URL to be a real http(s) page, and there is none -
        # the provider id itself is the durable reference (stored as `provider_message_id` and on the event as
        # `external_message_id`), which is quoted in the note so the two can always be tied together.
        evidence_note = (f"gmail:{provider_message_id} read back through the read-only adapter and matched on "
                         "recipient and subject" if verified else
                         f"gmail:{provider_message_id or 'unavailable'}; {verify_detail}")
        if note:
            evidence_note = f"{evidence_note} | operator note: {str(note)[:160]}"
        try:
            recorded = self.ledger.mark_outreach_sent(outreach_id, occurred_at=outcome.get("attempted_at"),
                                                      evidence_note=evidence_note,
                                                      provider_message_id=provider_message_id,
                                                      result=result, provider="gmail",
                                                      idempotency_key=dedupe_key,
                                                      provider_receipt=receipt)
        except LedgerError as error:
            # The worst failure mode here: the message went out and the ledger refused the accounting row. Loud,
            # durable, and never a reason to send again.
            self._write(outreach_id, {"send_state": "UNCERTAIN",
                                      "send_provider_message_id": provider_message_id,
                                      "send_state_detail": f"the provider accepted the message ({provider_message_id}) "
                                                           f"and the ledger refused the record: {error.code}"})
            raise OutboundRefusal(error.code,
                                  "the provider accepted the message and the ledger refused to record it "
                                  f"({error})",
                                  next_action="fix the ledger state; the message is already out, so do not send it "
                                              "again") from None
        self._write(outreach_id, {"send_state_detail": verify_detail, "send_sent_at": _now_iso(),
                                  "send_message_ref": outcome.get("message_ref"), "send_result": result,
                                  "send_verified": bool(verified), "send_content_fingerprint": fingerprint,
                                  "send_engagement_id": (recorded.get("send_event") or {}).get("engagement_id")})
        return {"dry_run": False, "send_state": "SENT", "code": None, "outreach_id": outreach_id,
                "lead_id": record.get("lead_id"), "engagement_id": (recorded.get("send_event") or {}).get("engagement_id"),
                "outreach_status": recorded.get("status"), "delivery_status": recorded.get("delivery_status"),
                "provider_message_id": provider_message_id, "result": result,
                "provider_verified": bool(recorded.get("provider_verified")), "verified": bool(verified),
                "verify_detail": verify_detail, "content_fingerprint": fingerprint,
                "contact_established": bool(recorded.get("contact_established")),
                "counts_as_contact": bool(recorded.get("counts_as_contact")),
                "delivery_note": recorded.get("delivery_note"),
                "next_action": ("report delivery only from a provider receipt, which Gmail does not provide; this "
                                "record stops at the answer above"),
                "capability_note": ("the provider accepted the message for sending; that is not proof of "
                                    "delivery, of being read, or of consent beyond the operator's approval")}

    # ------------------------------------------------------------------ reconciliation

    def reconcile(self, outreach_id: str | None = None, *, limit: Any = 25) -> dict[str, Any]:
        """Look for the messages unresolved attempts say went out, using the read-only client.

        This is the only cure for an uncertain send - not a retry. Finding the message with a matching recipient
        and subject resolves the attempt to accepted; finding nothing resolves it to not-sent, and *that* is when
        sending again becomes permissible, as its own operator decision.
        """
        rows: list[dict[str, Any]] = []
        wanted = ([outreach_id] if outreach_id else
                  [key for key, value in self.store.state["outreach"].items()
                   if value.get("send_state") in {"SENDING", "UNCERTAIN"}][:_bounded(limit, maximum=200)])
        for candidate in wanted:
            record = self._record(candidate)
            provider_id = str(record.get("send_provider_message_id") or "") or None
            detail: dict[str, Any] = {"outreach_id": candidate, "send_state": record.get("send_state"),
                                      "attempted_at": record.get("send_attempted_at"),
                                      "detail": record.get("send_state_detail"), "provider_message_id": provider_id,
                                      "resolved": False}
            if not provider_id:
                detail.update({"code": "PROVIDER_ID_UNAVAILABLE",
                               "next_action": ("check the Sent folder yourself and record what you find "
                                               "(`mark-send` or `--result REPORTED_SENT`); this tool will not guess "
                                               "between 'never sent' and 'sent twice'")})
            else:
                client = self._readback_client()
                if client is None:
                    detail.update({"code": "READBACK_UNAVAILABLE",
                                   "next_action": "the read-only Gmail credential is not configured, so the "
                                                  "provider id cannot be verified from here"})
                else:
                    try:
                        message = client.get_message(provider_id, message_format="metadata")
                    except Exception as error:  # noqa: BLE001 - a fault here is an answer of "unknown"
                        detail.update({"code": "READBACK_FAILED",
                                       "detail": f"{type(error).__name__}: {str(error)[:160]}"})
                    else:
                        headers = _headers(message)
                        found_to = _address(headers.get("to"))
                        if found_to == _address(record.get("recipient")) and str(headers.get("subject") or "") == \
                                str(record.get("subject") or ""):
                            self._resolve(record, found=OUTCOME_ACCEPTED, provider_message_id=provider_id,
                                          verified_fields=("to", "subject"))
                            detail.update({"resolved": True, "outcome": OUTCOME_ACCEPTED,
                                          "evidence": f"gmail:message:{provider_id}"})
                        elif not headers:
                            detail.update({"code": "READBACK_UNPARSABLE",
                                           "next_action": "Gmail answered with no headers we can read, so nothing "
                                                           "is confirmed either way; check manually"})
                        else:
                            detail.update({"code": "RECONCILIATION_MISMATCH",
                                           "detail": (f"the message behind that id goes to {_mask(found_to or '')} "
                                                       f"with subject {str(headers.get('subject') or '')[:60]!r}"),
                                           "next_action": ("a provider id that does not match this draft is a fault "
                                                           "in this process, not a clerical error: stop, look, and "
                                                           "send nothing further to that lead")})
            rows.append(detail)
        unresolved = [row for row in rows if not row.get("resolved")]
        return {"reconciled": [row for row in rows if row.get("resolved")], "unresolved": unresolved, "rows": rows,
                "next_action": ("each unresolved attempt needs the operator's own look; an unresolved attempt keeps "
                                "blocking new sends to that draft" if unresolved else None),
                "capability_note": ("reconciliation reads the account and never sends; an unresolved attempt stays "
                                    "unresolved rather than being guessed at either way")}

    def status(self, outreach_id: str | None = None, *, limit: Any = 25) -> dict[str, Any]:
        """Send state per draft: what we did, what the provider said, and what that proves."""
        records = ([self._record(outreach_id)] if outreach_id else
                   list(self.store.state["outreach"].values())[:_bounded(limit, maximum=200)])
        rows = [{"outreach_id": record.get("outreach_id"), "status": record.get("status"),
                 "send_state": record.get("send_state"), "result": record.get("send_result"),
                 "verified": bool(record.get("send_verified")),
                 "provider_message_id": record.get("send_provider_message_id"),
                 "delivery_status": record.get("delivery_status"),
                 "provider_verified": bool(record.get("provider_verified")),
                 "send_attempts": int(record.get("send_attempts") or 0),
                 "actor": record.get("send_actor"), "identity_source": record.get("send_identity_source"),
                 "attempted_at": record.get("send_attempted_at"), "sent_at": record.get("sent_at"),
                 "detail": record.get("send_state_detail"),
                 "engagement_id": record.get("send_engagement_id")} for record in records]
        unresolved = [row for row in rows if row.get("send_state") in {"SENDING", "UNCERTAIN"}]
        return {"sends": rows, "unresolved": unresolved,
                "counts": {"sent": sum(1 for row in rows if row.get("send_state") == "SENT"),
                           "failed": sum(1 for row in rows if row.get("send_state") == "FAILED"),
                           "uncertain": len(unresolved),
                           "awaiting_send": sum(1 for row in rows if row.get("status") == "READY_TO_SEND"
                                                 and not row.get("send_state"))},
                "results_unreachable": list(OUTCOME_UNREACHABLE),
                "next_action": ("run `email-reconcile` on the unresolved attempts before sending anything else to "
                                "those leads" if unresolved else None),
                "capability_note": ("`verified` means the provider id was read back from the account and matched "
                                    "this draft; it never means delivered")}

    # ------------------------------------------------------------------ configuration

    def configuration(self) -> dict[str, Any]:
        """The sending report in states an operator can act on, with the verification this process actually has."""
        from integrations.gmail_send import send_config

        configuration = send_config(self.config_source if self.config_source is not None else os.environ)
        verification = (self.store.state.get("provider_sync") or {}).get("gmail_send_verification") or {}
        configuration["verification"] = {"state": verification.get("state") or "unverified",
                                         "verified_at": verification.get("verified_at"),
                                         "code": verification.get("code")}
        if configuration["state"] == "configured" and verification.get("state") == "verified":
            configuration["state"] = "verified"
        elif configuration["state"] == "configured" and verification.get("state") == "unverified" and verification.get("code"):
            configuration["state"] = "unverified"
        configuration["live_effect_enabled"] = self.allow_live
        configuration["channel"] = SEND_CHANNEL
        return configuration

    def verify(self) -> dict[str, Any]:
        """Exchange the send credential's token and send nothing. The strongest claim available offline."""
        adapter = self._adapter(build_if_missing=True)
        if adapter is None:
            return {"state": "inbound_only", "verified_at": None, "code": "GMAIL_SEND_NOT_CONFIGURED",
                    "next_action": "this machine has no credential minted for the send scope, so sending is not "
                                   "offered; the read-only credential cannot be reused for it"}
        answer = adapter.verify_authorization()
        state = self.store.state.setdefault("provider_sync", {})
        state["gmail_send_verification"] = {"state": answer.get("state"), "verified_at": answer.get("verified_at"),
                                            "code": answer.get("code"),
                                            "detail": str(answer.get("detail") or "")[:200],
                                            "updated_at": _now_iso()}
        self.store._save()  # noqa: SLF001 - the store's own atomic write
        return answer

    def suppression(self) -> "AddressSuppression":
        """The set of addresses this application has been told not to contact.

        Derived from the ledger's own ``contact_blocks`` and the leads they belong to - there is no second
        suppression list to keep in sync, and a block that was revoked stops suppressing, exactly as the ledger
        treats it. The send adapter is handed this object so an opt-out is honoured at the provider boundary as
        well as in the eligibility check.
        """
        if self._suppression is None:
            self._suppression = AddressSuppression(self.store)
        return self._suppression

    # ------------------------------------------------------------------ internals

    def _record(self, outreach_id: str) -> dict[str, Any]:
        record = self.store.state["outreach"].get(str(outreach_id or "").strip())
        if not record:
            raise OutboundRefusal("OUTREACH_NOT_FOUND", f"no outreach record named {outreach_id}",
                                  next_action="list the outbox for the ids this process knows")
        return record

    def _write(self, outreach_id: str, changes: Mapping[str, Any]) -> None:
        """Update the outreach row's own ``send_*`` namespace, durably.

        The ledger-owned columns (``status``, ``delivery_status``, ``send_attempts``, ``provider_verified``,
        ``contact_established``) are not written here: :meth:`tracking.engagement.EngagementLedger.mark_outreach_sent`
        remains the only writer of the approved-state machine, from any path. That is why an attempt marker can be
        set and cleared without any chance of faking an approval or a send.
        """
        record = self.store.state["outreach"].get(str(outreach_id))
        if record is None:
            return
        record.update(dict(changes))
        self.store._save()  # noqa: SLF001 - durability before and after the provider call is the whole point

    def _composition(self, record: Mapping[str, Any], recipient: str | None) -> dict[str, Any]:
        """Compose the exact provider request. A fault here is a refusal, never a partially-sent message."""
        from integrations.gmail_send import (GMAIL_SEND_FROM_ENV, GMAIL_SEND_UNSUBSCRIBE_URL_ENV, build_message)

        source = dict(os.environ if self.config_source is None else self.config_source)
        thread_id, threading_basis = self._reply_thread(record)
        try:
            prepared = build_message(to=recipient or record.get("recipient"), subject=str(record.get("subject") or ""),
                                     body_text=str(record.get("draft_message") or ""),
                                     from_address=(source.get(GMAIL_SEND_FROM_ENV) or "").strip() or None,
                                     unsubscribe_url=(source.get(GMAIL_SEND_UNSUBSCRIBE_URL_ENV) or "").strip() or None,
                                     thread_id=thread_id)
        except Exception as error:  # noqa: BLE001 - an unparsable draft must read as a refusal with a reason
            return {"prepared": None, "error": _safe_text(error), "threading": threading_basis}
        return {"prepared": prepared, "error": None, "threading": threading_basis,
                "provider": "gmail", "endpoint": "users.messages.send",
                "size_bytes": prepared.get("size_bytes"), "headers": sorted(prepared.get("headers") or {}),
                "list_unsubscribe_present": bool((prepared.get("headers") or {}).get("List-Unsubscribe")),
                "body_sha256": prepared.get("body_sha256")}

    def _reply_thread(self, record: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Thread a continuation only onto a thread this application actually saw an inbound message in.

        ``RFC-MESSAGE-ID`` threading is deliberately not attempted: the inbound intake stores Gmail's own
        message ids and never keeps the RFC ``Message-ID`` header, so there is nothing truthful to put in
        ``In-Reply-To`` - inventing one would splice our cold email into somebody's thread.
        """
        lead_id = str(record.get("lead_id") or "")
        basis = {"basis": "new_conversation", "in_reply_to": None, "thread_id": None}
        if not lead_id or self.ledger is None:
            return None, basis
        candidates = [row for row in (self.store.state.get("engagements") or {}).values()
                      if row.get("lead_id") == lead_id and row.get("kind") == "RESPONSE_RECEIVED"
                      and row.get("direction") == "INBOUND" and row.get("conversation_id")]
        if not candidates:
            return None, basis
        latest = sorted(candidates, key=lambda row: str(row.get("occurred_at") or ""))[-1]
        thread_id = str(latest.get("conversation_id") or "")[:120] or None
        if not thread_id:
            return None, basis
        basis.update({"basis": f"gmail_thread_of_{latest.get('engagement_id')}", "thread_id": thread_id,
                      "note": ("the send joins the Gmail conversation the prospect wrote in, because that thread "
                                "id came from the provider's own record of their message")})
        return thread_id, basis

    def _adapter(self, *, build_if_missing: bool = True) -> Any:
        if self._client is not None:
            return self._client
        if not build_if_missing:
            return None
        try:
            from integrations.gmail_send import GmailSendAdapter, send_config

            source = dict(os.environ if self.config_source is None else self.config_source)
            configuration = send_config(source)
            if configuration["state"] == "inbound_only":
                return None
            # ``enabled`` is passed through rather than assumed: this process may not turn the operator's own
            # ``AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED`` flag on by deciding that credentials are present. A client
            # built here therefore refuses to send while the flag is off, and says which variable to set.
            self._client = GmailSendAdapter.from_environment(
                source, enabled=bool(configuration["enabled"]), suppression=self.suppression())
        except Exception:  # noqa: BLE001 - an unusable credential is a refusal the caller sees, not a stack trace
            return None
        return self._client

    def _readback_client(self) -> Any:
        if self._readback is not None:
            return self._readback
        try:
            from integrations.gmail import GmailClient, gmail_config

            source = dict(os.environ if self.config_source is None else self.config_source)
            if not gmail_config(source)["configured"]:
                return None
            self._readback = GmailClient.from_environment(source)
        except Exception:  # noqa: BLE001
            return None
        return self._readback

    def _verify_against_account(self, provider_message_id: str | None, prepared: Mapping[str, Any],
                                 recipient: str | None) -> tuple[bool, str, dict[str, Any] | None]:
        """Prove the provider's id is *this* message, or say precisely why it is not proven.

        Returns ``(verified, detail, receipt)``; the receipt is what the ledger needs before it will call
        anything "accepted by the provider", so a refusal here degrades the record to
        ``REPORTED_SENT``/``provider_verified: false`` instead of becoming a claim.
        """
        if not provider_message_id:
            return False, "the provider returned no message id", None
        client = self._readback_client()
        if client is None:
            return (False, "the read-only Gmail credential is not configured on this machine, so the sent message "
                    "cannot be read back", None)
        try:
            message = client.get_message(provider_message_id, message_format="metadata")
        except Exception as error:  # noqa: BLE001
            return False, f"reading the message back failed: {_safe_text(error)}", None
        headers = _headers(message)
        found_to = _address(headers.get("to"))
        verified_fields: list[str] = []
        if found_to == (recipient or "").lower():
            verified_fields.append("to")
        if str(headers.get("subject") or "") == str(prepared.get("subject") or ""):
            verified_fields.append("subject")
        if not verified_fields or "to" not in verified_fields:
            detail = (f"the message behind that id goes to {_mask(found_to or 'nobody')}" if found_to != (recipient
                                                                                                          or "") else
                      "the headers could not be read")
            return False, f"read-back mismatch: {detail}", None
        if "subject" not in verified_fields:
            return (False, "the recipient matches but the subject does not, so the id is not proven to be this "
                    "draft", None)
        receipt = {"source": "gmail:users.messages.get", "message_id": provider_message_id,
                   "read_back_at": _now_iso(), "verified_fields": verified_fields,
                   "detail": "recipient and subject read back from the authenticated account's own copy"}
        return True, "the provider id resolves to this recipient and subject in the sent account", receipt

    def _classify_exception(self, error: BaseException) -> dict[str, Any]:
        """Uncertain or clean refusal, decided by whether the provider could have taken the message."""
        code = str(getattr(error, "code", "") or "GMAIL_CALL_FAILED")
        status = getattr(error, "status", None)
        detail = _safe_text(error)
        status_number = status if isinstance(status, int) else None
        if code in UNCERTAIN_CODES or status_number is None:
            return {"sent": False, "code": "SEND_UNCERTAIN", "provider_answered": True,
                    "reason": f"no definitive answer ({code}): {detail}"[:300],
                    "next_action": "run `email-reconcile`; do not resend this draft"}
        if status_number is not None and status_number >= 500:
            return {"sent": False, "code": "SEND_UNCERTAIN", "provider_answered": True,
                    "reason": f"the provider failed while answering ({status_number}); it may have accepted the "
                              f"message: {detail}"[:300],
                    "next_action": "run `email-reconcile`; do not resend this draft"}
        return {"sent": False, "code": code, "provider_answered": True, "retryable": bool(getattr(error, "retryable",
                                                                                                   False)),
                "reason": detail, "provider_message_id": None,
                "next_action": getattr(error, "next_action", None) or "fix what the refusal names, then send again"}

    def _record_uncertain(self, outreach_id: str, annotation: Mapping[str, Any], outcome: Mapping[str, Any],
                          fingerprint: str, *, provider_message_id: str | None) -> dict[str, Any]:
        """Persist an unanswered attempt as its own event, and refuse to guess the rest."""
        record = self._record(outreach_id)
        reason = str(outcome.get("reason") or "the provider did not answer definitively")[:300]
        self._write(outreach_id, {"send_state": "UNCERTAIN", "send_state_detail": f"uncertain: {reason}",
                                  "send_uncertain_at": _now_iso(), "send_provider_message_id": provider_message_id,
                                  "send_content_fingerprint": fingerprint, "send_uncertain_reconciled": False})
        # The uncertain attempt gets its own event, so the next reader sees an attempt that needs reconciliation
        # rather than a silent gap. A refused event must not turn into a crash that hides it: the outreach row
        # already carries the uncertainty and blocks the next send on its own.
        try:
            self.ledger.record_event(record.get("lead_id"), kind="OUTREACH_SEND_UNCERTAIN",
                                     summary=(f"An approved email to {_mask(str(annotation.get('recipient') or ''))}"
                                              " was submitted and the provider gave no definitive answer."),
                                     direction="OUTBOUND", channel=SEND_CHANNEL, outreach_id=outreach_id,
                                     provider="gmail", external_message_id=provider_message_id,
                                     idempotency_key=f"{send_dedupe_key(outreach_id, fingerprint)}:uncertain",
                                     detail=reason[:200], needs_human_review=True,
                                     evidence=[{"kind": "OPERATOR_NOTE",
                                                "note": f"send submitted at {_now_iso()}; the provider answer was "
                                                        "not definitive, so the outcome is unknown"}])
        except LedgerError:
            pass
        return {"dry_run": False, "send_state": "UNCERTAIN", "code": "SEND_UNCERTAIN", "sent": False,
                "outreach_id": outreach_id, "reason": reason, "content_fingerprint": fingerprint,
                "provider_message_id": provider_message_id,
                "next_action": str(outcome.get("next_action") or "run `email-reconcile`; do not resend this draft"),
                "capability_note": ("an uncertain send is neither sent nor failed: Gmail has no idempotency key, "
                                    "so the only safe move is to look, not to try again")}

    def _resolve(self, record: Mapping[str, Any], *, found: str, provider_message_id: str,
                 verified_fields: Sequence[str]) -> None:
        """Close an unresolved attempt with evidence, through the same ledger path as a send."""
        outreach_id = str(record.get("outreach_id"))
        try:
            self.ledger.mark_outreach_sent(outreach_id,
                                           evidence_note=f"reconciled by reading gmail:{provider_message_id} back "
                                                         "from the authenticated account",
                                           provider_message_id=provider_message_id, result=OUTCOME_ACCEPTED,
                                           provider_receipt={"source": "gmail:users.messages.get",
                                                             "message_id": provider_message_id,
                                                             "read_back_at": _now_iso(),
                                                             "verified_fields": list(verified_fields),
                                                             "detail": "matched during reconciliation"},
                                           provider="gmail",
                                           idempotency_key=f"{record.get('send_dedupe_key') or send_dedupe_key(outreach_id, str(record.get('send_fingerprint') or ''))}:reconciled")
        except LedgerError as error:
            self._write(outreach_id, {"send_state_detail": f"reconciliation could not be recorded: {error.code}",
                                      "send_state": "UNCERTAIN"})
            return
        self._write(outreach_id, {"send_result": found, "send_verified": True,
                                  "send_state_detail": f"reconciled from the account: {found}",
                                  "send_uncertain_reconciled": True,
                                  "send_provider_message_id": provider_message_id})

    def client_description(self) -> dict[str, Any]:
        """The send surface as its own adapter describes it, for a status report or a preflight."""
        adapter = self._adapter(build_if_missing=False)
        return adapter.describe() if adapter is not None else {}


# ------------------------------------------------------------------------------------------------------------
# module helpers
# ------------------------------------------------------------------------------------------------------------

_NEXT_ACTION = {
    "NOT_APPROVED": "a human has to approve this draft first (outbox -> approve), and an approval sends nothing",
    "APPROVAL_STALE": "have a human review the current content again; the old approval is not re-used",
    "APPROVAL_UNBOUND": "re-approve the current draft so the approval is bound to this exact content",
    "CONTACT_BLOCKED": "nothing releases an opt-out from here; the block is lifted by its own command, with a reason",
    "RECIPIENT_UNPARSEABLE": "fix the lead's contact detail and re-approve the draft",
    "DRAFT_UNSAFE": "regenerate the draft; a body that fails the safety review is never sent",
    "PREVIEW_UNAVAILABLE": "redeploy the preview or withdraw the draft; a dead link in a first email is a bad look",
    "DEPLOYMENT_STALE": "prepare outreach against the current build and have a human approve that draft",
    "LEAD_NO_LONGER_QUALIFIED": "disqualification outranks approval; do not contact this business",
    "SEND_UNCERTAIN": "run `email-reconcile`; never resend a draft with an unresolved attempt",
    "SENDING_DISABLED": "the operator's send flag is off; that is a decision to leave it off, not a fault",
    "LIVE_EFFECTS_DISABLED": "set AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH=1 (or `--allow-live`) once the credential "
                             "check passes",
    "ALREADY_SENT": "this approval was already accounted for; a further approach needs a new draft and a new review",
}


def _default_outreach_service(store: Any) -> Any:
    from outreach.service import OutreachService

    return OutreachService(store)


def _address(value: Any) -> str | None:
    from integrations.gmail_send import extract_address

    return extract_address(value)


def _headers(message: Mapping[str, Any]) -> dict[str, str]:
    """The header map of a Gmail message resource, lower-cased. Never raises: an odd shape means "no headers"."""
    from integrations.gmail import header_map

    payload = message.get("payload") if isinstance(message, Mapping) else None
    if not isinstance(payload, Mapping):
        payload = message
    try:
        values = header_map(dict(payload))
    except Exception:  # noqa: BLE001
        values = {}
    return {str(key).lower(): str(value) for key, value in values.items()}


def _resafety(record: Mapping[str, Any], lead: Mapping[str, Any]) -> str | None:
    """Re-run the outreach service's own safety review, or return why it failed.

    Re-using :meth:`outreach.service.OutreachService._safe` - rather than a copy of its rules - is the point: if
    the drafting rules ever get stricter, the send gate gets stricter with them instead of lagging behind them.
    """
    from outreach.service import OutreachService

    try:
        OutreachService._safe(None, str(record.get("draft_message") or ""),
                              str(lead.get("business_name") or ""), str(record.get("preview_url") or ""))
    except Exception as error:  # noqa: BLE001 - a refusal is the answer, and its text is the reason
        return str(error) or "the draft failed the outreach safety review"
    return None


def _mask(address: str) -> str:
    local, _, domain = str(address or "").partition("@")
    if not domain:
        return "[no address]"
    return f"{(local[:1] or '?')}\u2026@{domain}"


def _safe_text(value: Any) -> str:
    from orchestration.observability import redact

    return redact(str(value))[:200]


def _bounded(limit: Any, *, default: int = 25, maximum: int = 100) -> int:
    try:
        value = int(str(limit).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))
