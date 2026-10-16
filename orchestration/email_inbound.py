"""The email side of the inbound pipeline: fetch, match on recorded references, hand to the Phase 47 intake.

This module is deliberately thin, because everything hard about inbound responses is already decided elsewhere.
:class:`tracking.inbound.InboundIntake` owns idempotency, triage, opt-out evaluation, the refusal of
state-asserting fields, the durable transaction, and the review queue. What a mailbox adds is provider detail:
which messages are new, which one belongs to which lead, and where to resume next. So:

* **One processor.** Every message becomes a payload for :meth:`InboundIntake.process`; nothing here writes a
  ledger row, a stage, an approval, or an outcome directly, and there is no second copy of any rule. A second
  inbound processor would be a second source of truth about what a business said.
* **Matching only on what the records already prove.** Two bases qualify: the thread id of a message whose
  conversation the intake has already linked to a lead, and a sender address exactly equal to the address recorded
  on exactly one lead. A subject line naming the business, a similar domain, a phone number in the body - none of
  those link anything, and a guessed link is worse than a parked one because it writes into the wrong history.
* **A cursor that only moves on success.** ``historyId`` advances after a cycle that finished, so an interrupted
  run re-reads mail it may have half-handled. Re-reading is safe: the intake's identity is Gmail's own message id,
  so a repeat arrives as ``duplicate: true`` and changes nothing.
* **No write-back to the mailbox.** No label, no archive, no mark-as-read, no reply. A message that has been
  recorded is not "handled" - an operator decides that, in the ledger, where the decision is visible.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from orchestration.observability import log_event, sanitize_diagnostic
from tracking.engagement import EngagementLedger
from tracking.inbound import InboundEventError, InboundIntake

from integrations.gmail import (DEFAULT_FETCH_FORMAT, GmailError, describe_error, gmail_config,
                                message_to_inbound_event)

#: Cursor and cycle statistics live in their own collection: a sync position is neither a ledger record nor an
#: intake row, and putting it in either would make the ledger carry transport bookkeeping.
SYNC_COLLECTION = "provider_sync"
GMAIL_SYNC_KEY = "gmail_inbox"
#: An address, as it appears in a `From:` header possibly wrapped in a display name.
_ADDRESS = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAX_HISTORY_ROWS = 20


class EmailInboundPoller:
    """Poll Gmail for new inbound mail and submit each message through the application's intake.

    ``client`` is a :class:`integrations.gmail.GmailClient` (or any object with ``list_messages``, ``get_message``,
    ``get_history``, and ``latest_history_id``), which is what makes this testable offline and what lets an
    operator swap in a different mailbox later without touching a business rule.
    """

    def __init__(self, client: Any, *, store: Any, intake: InboundIntake | None = None,
                 ledger: EngagementLedger | None = None, query: str | None = None,
                 max_messages: int = 25, fetch_format: str = DEFAULT_FETCH_FORMAT,
                 use_history: bool = True, source: str = "gmail-adapter",
                 notifier: Callable[[], dict[str, Any]] | None = None) -> None:
        self.client = client
        self.store = store
        self.ledger = ledger or EngagementLedger(store, source=source)
        self.intake = intake or InboundIntake(self.ledger)
        self.query = str(query or gmail_config()["query"])[:500]
        self.max_messages = max(1, min(int(max_messages or 25), 100))
        self.fetch_format = str(fetch_format or DEFAULT_FETCH_FORMAT)
        self.use_history = bool(use_history)
        self.source = source
        #: Optional push to the operator (the Telegram bot's `notify_once`), injected so this module never
        #: imports a channel. With none, the report simply says notifications were not configured.
        self.notifier = notifier

    # ------------------------------------------------------------------ cycle

    def poll_once(self, *, dry_run: bool = False, notify: bool = True) -> dict[str, Any]:
        """One fetch-and-record cycle. Never raises for a provider fault: those become codes in the report."""
        record = self._record()
        started: dict[str, Any] = {"as_of": _now(), "dry_run": bool(dry_run), "query": self.query}
        try:
            messages, page_token, cursor = self._fetch(record)
        except (GmailError, ValueError) as error:
            answer = describe_error(error)
            self._save_error(answer)
            log_event(severity="WARNING", operation="email.poll", code=answer["error_code"],
                      message="mailbox could not be read", detail=answer.get("error"))
            return {"recorded": 0, "fetched": 0, **answer,
                    "retryable": bool(answer.get("retryable")), "next_action": answer.get("next_action")
                    or "Retry the cycle; no cursor moved, so nothing between here and the mailbox was skipped."}

        outcomes: list[dict[str, Any]] = []
        for message in messages:
            outcomes.append(self._submit(message, dry_run=dry_run))
        # A duplicate is counted only as a duplicate: a cycle that re-read the whole mailbox did not record
        # anything, and a report that says otherwise would make a stable integration look busy.
        duplicates = sum(1 for item in outcomes if item.get("duplicate"))
        recorded = sum(1 for item in outcomes if not item.get("duplicate")
                       and item.get("disposition") in {"RECORDED", "NOTED", "OPT_OUT_RECORDED", "UNMATCHED"})
        refusals = [item for item in outcomes if item.get("error_code")]
        needing_attention = [item for item in outcomes
                             if item.get("disposition") in {"UNMATCHED", "FAILED", "OPT_OUT_UNCERTAIN"}
                             or item.get("triage_certainty") in {"none", "ambiguous", "mixed"}]
        cursor_saved = None
        if not dry_run:
            cursor_saved = self._advance(cursor=cursor, page_token=page_token, fetched=len(messages),
                                         recorded=recorded, had_refusals=bool(refusals))
        report = {
            "as_of": _now(), "dry_run": bool(dry_run), "fetched": len(messages),
            "recorded": recorded, "duplicates": duplicates, "refused": len(refusals),
            "unmatched": sum(1 for item in outcomes if item.get("disposition") == "UNMATCHED"),
            "matched": sum(1 for item in outcomes if item.get("lead_id")),
            "needs_attention": len(needing_attention),
            "results": outcomes[: self.max_messages],
            "next_page_token": page_token,
            "cursor": cursor if cursor is not None else record.get("last_history_id"),
            "cursor_persisted": cursor_saved,
            "meaning": ("messages are recorded as evidence of what arrived; nothing here replies, approves, "
                        "or marks anything contacted"),
            "next_action": (f"Review what the mailbox could not route: python3 -m tracking.cli inbound-pending"
                            if needing_attention else "Nothing needs an operator from this cycle."),
        }
        if refusals:
            report["refusal_codes"] = sorted({str(item.get("error_code")) for item in refusals})
        if notify and needing_attention and not dry_run:
            report["notification"] = self._notify()
        return report

    def status(self) -> dict[str, Any]:
        """Where this mailbox cursor stands, from what was actually recorded - no estimates."""
        record = self._record()
        client_status = self.client.status() if hasattr(self.client, "status") else {}
        backlog = self.intake.pending(limit=25)
        return {"configured": bool(self.client is not None), "account": client_status.get("account"),
                "scope": client_status.get("scope"), "query": self.query, "fetch_format": self.fetch_format,
                "last_run_at": record.get("last_run_at"), "last_history_id": record.get("last_history_id"),
                "history_id_source": record.get("history_id_source"),
                "last_error": record.get("last_error"), "cycles": record.get("cycles"),
                "provider_counters": {key: value for key, value in client_status.items()
                                      if key in ("calls", "errors", "token_minted_at", "explicitly_absent")},
                "intake_backlog": {"total_matching": backlog.get("total_matching"),
                                   "by_disposition": backlog.get("counts_by_disposition")},
                "meaning": "the cursor advances only after a completed cycle, so an interrupted run re-reads "
                            "mail it may have half-handled and the intake's provider-id dedupe absorbs the repeat"}

    def retry_pending(self, *, limit: int = 25) -> dict[str, Any]:
        """Re-submit the events this side could not route or could not persist.

        This is the retry the mailbox needs and the one it must not invent: an unmatched message is re-offered so
        the operator's `lead_id` (recorded since) can finally link it, and a `FAILED` row is re-offered after a
        storage fault. Both are the intake's own semantics - this method only refetches and resubmits.
        """
        backlog = self.intake.pending(dispositions=("FAILED", "UNMATCHED"), limit=limit)
        outcomes = []
        seen_ids: set[str] = set()
        for row in backlog.get("items") or []:
            event_id = row.get("provider_event_id")
            if event_id:
                seen_ids.add(str(event_id))
            if not event_id:
                outcomes.append({"dedupe_key": row.get("dedupe_key"), "error_code": "EVENT_ID_UNAVAILABLE",
                                 "next_action": "this row has no provider id to refetch; record it by hand with "
                                                "`inbound-record` once the message is read"})
                continue
            outcomes.append(self._resubmit(str(event_id)))
        # A superseded row whose own message was never recorded is still unwritten work. Phase 47 takes a parked
        # row out of the backlog when a *later* event in the same thread gets linked, which is right for a human
        # queue and wrong for an adapter: the two messages are different messages, and Gmail's copy of this one
        # would otherwise never be read again.
        superseded = [
            {"provider_event_id": row.get("provider_event_id")}
            for row in (self.store.state.get("inbound_events") or {}).values()
            if isinstance(row, dict) and row.get("disposition") == "UNMATCHED" and row.get("superseded_by")
            and row.get("provider_event_id") and not row.get("engagement_id")
            and str(row.get("source") or "").startswith(self.source)
        ]
        for row in superseded[: max(0, int(limit))]:
            if row["provider_event_id"] in seen_ids:
                continue
            seen_ids.add(str(row["provider_event_id"]))
            outcomes.append(self._resubmit(str(row["provider_event_id"])))
        # Messages a persistence fault lost: they never reached the intake, so the backlog above cannot know
        # about them. They are re-offered from the sync record and dropped from it only once written.
        lost = [str(item) for item in (self._record().get("unwritten") or []) if str(item) and str(item) not in seen_ids]
        for event_id in lost[:max(0, int(limit))]:
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            result = self._resubmit(event_id)
            outcomes.append(result)
            if not result.get("error_code"):
                self._clear_unwritten(event_id)
        return {"retried": len(outcomes), "results": outcomes,
                "still_unwritten": [str(item) for item in (self._record().get("unwritten") or [])],
                "next_action": "Anything still unmatched needs its lead named on the event, not guessed here."}

    def _resubmit(self, event_id: str) -> dict[str, Any]:
        """Refetch one provider message and put it through the intake again - no other shortcut."""
        try:
            message = self.client.get_message(event_id, message_format=self.fetch_format)
        except (GmailError, ValueError) as error:
            return {"provider_event_id": event_id, **describe_error(error)}
        return self._submit(message, resubmitted=True)

    def _clear_unwritten(self, message_id: str) -> None:
        record = dict(self._record())
        remaining = [str(item) for item in (record.get("unwritten") or []) if str(item) != str(message_id)]
        record["unwritten"] = remaining
        self.store.state.setdefault(SYNC_COLLECTION, {})[GMAIL_SYNC_KEY] = record
        try:
            self.store._save()  # noqa: SLF001
        except Exception:  # noqa: BLE001 - a cleared marker is not worth failing a retry over.
            pass

    # ------------------------------------------------------------------ internals

    def _fetch(self, record: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None, int | None]:
        cursor = record.get("last_history_id")
        if self.use_history and cursor:
            try:
                history = self.client.get_history(int(cursor), max_results=self.max_messages)
            except GmailError as error:
                if error.code not in {"GMAIL_REQUEST_REFUSED", "GMAIL_UNRESPONSIVE", "GMAIL_RATE_LIMITED"}:
                    raise
                # A history id is not permanent. When Google refuses it, fall back to the query path this cycle
                # and re-anchor the cursor - which is exactly what the documented renewal procedure says to do.
                log_event(severity="WARNING", operation="email.history", code="HISTORY_ID_UNUSABLE",
                          message="stored history cursor refused; falling back to query polling",
                          detail=error.code)
                self._mark_cursor_unusable(error.code)
            else:
                ids = []
                for entry in history.get("history") or []:
                    for message in (entry.get("messages") or []):
                        if isinstance(message, dict) and message.get("id"):
                            ids.append(str(message["id"]))
                records = []
                for message_id in ids[: self.max_messages]:
                    try:
                        records.append(self.client.get_message(message_id, message_format=self.fetch_format))
                    except (GmailError, ValueError):
                        # A message that cannot be read now (deleted, or a transient fault) is reported and left
                        # for the retry path; it must not abort the rest of the cycle.
                        records.append({"id": message_id, "_unreadable": True})
                return records, None, history.get("history_id") or cursor
        listing = self.client.list_messages(self.query, max_results=self.max_messages,
                                           page_token=record.get("last_page_token"))
        messages = []
        for stub in listing.get("messages") or []:
            message_id = str(stub.get("id") or "")
            if not message_id:
                continue
            try:
                messages.append(self.client.get_message(message_id, message_format=self.fetch_format))
            except (GmailError, ValueError) as error:
                messages.append({"id": message_id, "_unreadable": True,
                                 "_unreadable_code": describe_error(error).get("error_code")})
        anchor = None
        if not record.get("last_history_id"):
            try:
                anchor = self.client.latest_history_id()
            except (GmailError, ValueError):
                anchor = None
        return messages, listing.get("next_page_token"), anchor or record.get("last_history_id")

    def _submit(self, message: dict[str, Any], *, dry_run: bool = False, resubmitted: bool = False) -> dict[str, Any]:
        message_id = str(message.get("id") or "") if isinstance(message, dict) else ""
        if message.get("_unreadable"):
            return {"provider_event_id": message_id, "error_code": "GMAIL_MESSAGE_UNREADABLE",
                    "retryable": True, "next_action": "re-run retry_pending; a message Google will not return "
                                                       "is left where it is rather than guessed at"}
        try:
            event = message_to_inbound_event(message, fetch_format=self.fetch_format)
        except (GmailError, ValueError) as error:
            answer = describe_error(error)
            return {"provider_event_id": message_id, **answer}
        lead_id, basis, note = self._match(event)
        if lead_id:
            event["lead_id"] = lead_id
        reply_to = self._resolve_reference(message)
        if reply_to:
            # A Message-ID we can trace to a draft we actually recorded: the only shape `in_reply_to` accepts.
            event["in_reply_to"] = reply_to
        # The basis is recorded as a claim by this adapter, and the intake stores it as such. Matching on a
        # recorded address is defensible; it is not proof that the writer owns the lead.
        event["match_basis"] = basis or "unmatched"
        if dry_run:
            from tracking.inbound import normalize_event

            try:
                normalized = normalize_event(event)
            except InboundEventError as error:
                return {"provider_event_id": message_id, "error_code": error.code, "error": str(error)}
            return {"provider_event_id": message_id, "would_record": True, "lead_id": normalized["lead_id"],
                    "linkage_basis": basis, "excerpt": normalized["excerpt"],
                    "triage_category": normalized["triage"]["category"]}
        try:
            result = self.intake.process(event, source=f"{self.source}{'-retry' if resubmitted else ''}")
        except InboundEventError as error:
            # A contract refusal stores nothing and is the adapter's bug to fix, so it is reported loudly rather
            # than buried: the next cycle will see the same message and refuse it again.
            log_event(severity="WARNING", operation="email.submit", code=error.code,
                      message="intake refused a mailbox event", event_ref=message_id)
            return {"provider_event_id": message_id, "error_code": error.code, "error": str(error)[:300]}
        except Exception as error:  # noqa: BLE001 - a persistence fault must not end the cycle silently.
            # Nothing was written and the cursor will move past this message, so the id is parked on the sync
            # record. Without that, a database blip would silently delete a customer's reply from the pipeline.
            self._save_error({"error_code": "PERSISTENCE_FAILED", "error": sanitize_diagnostic(error),
                              "retryable": True, "unwritten": message_id})
            return {"provider_event_id": message_id, "error_code": "PERSISTENCE_FAILED",
                    "error": sanitize_diagnostic(error), "retryable": True,
                    "next_action": "re-run with --retry-pending (or `email-poll --retry-pending`): the message id "
                                   "is parked on the sync record until it is written, and Gmail's id keeps the "
                                   "retry from double-counting"}
        triage = result.get("triage") or {}
        return {"provider_event_id": message_id, "dedupe_key": result.get("dedupe_key"),
                "disposition": result.get("disposition"), "lead_id": result.get("lead_id"),
                # What the *intake* says it linked on, first: the poller's basis is one input to that decision,
                # not a second verdict. `adapter_linkage_basis` keeps the adapter's reason visible beside it.
                "linkage_basis": result.get("linkage_basis") or basis, "adapter_linkage_basis": basis,
                "linkage_note": note, "engagement_id": result.get("engagement_id"),
                "duplicate": bool(result.get("duplicate")), "triage_category": triage.get("category"),
                "triage_certainty": triage.get("certainty"), "opt_out": (result.get("opt_out") or {}).get("apply"),
                "next_action": result.get("next_action")}

    def _resolve_reference(self, message: dict[str, Any]) -> str | None:
        """Map Gmail's In-Reply-To/References onto *our* record ids, via recorded provider ids.

        A reply to a mail we sent can only name our draft if some earlier record already ties that Message-ID to
        it. The intake rows carry ``message_ref``/``provider_event_id`` and, when linked, an ``outreach_id``, so
        the chain is: reference value -> a recorded event -> the draft that event answered. Anything shorter than
        that would be guessing, and a guessed draft id drags the wrong lead's approval into this message.
        """
        from integrations.gmail import header_map

        payload = message.get("payload") if isinstance(message, dict) else None
        headers = header_map(payload if isinstance(payload, dict) else {})
        text = " ".join(str(headers.get(key) or "") for key in ("In-Reply-To", "References"))
        tokens = [chunk.strip(" <>") for chunk in re.split(r"[\s,]+", text) if chunk.strip(" <>")]
        if not tokens:
            return None
        wanted = set(tokens)
        for row in (self.store.state.get("inbound_events") or {}).values():
            if not isinstance(row, dict):
                continue
            candidates = {str(row.get("provider_event_id") or ""), str(row.get("message_ref") or ""),
                          str(row.get("message_ref") or "").removeprefix("gmail:")}
            if wanted & candidates and row.get("outreach_id"):
                return str(row["outreach_id"])
        return None

    def _match(self, event: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        """Which lead this message belongs to, or none. Two bases, both recorded ones - and nothing else.

        Order matters: thread continuity is stronger than an address, because the intake row already says "this
        conversation belongs to this lead", and a business that writes from two mailboxes is ordinary.
        """
        thread = event.get("conversation_id")
        if thread:
            leads: set[str] = set()
            for row in (self.store.state.get("inbound_events") or {}).values():
                if isinstance(row, dict) and row.get("conversation_id") == thread and row.get("lead_id"):
                    leads.add(str(row["lead_id"]))
            if len(leads) == 1:
                return leads.pop(), "provider_thread_reference", None
            if len(leads) > 1:
                # The thread is claimed by more than one lead's history. Guessing which is a fabrication.
                return (None, None, f"thread is referenced by {len(leads)} leads' intake rows; refused rather "
                                    "than guessed")
        sender = _address_of(event.get("sender"))
        if sender:
            matches = [lead_id for lead_id, lead in (self.store.state.get("leads") or {}).items()
                       if isinstance(lead, dict) and _address_of(lead.get("email")) == sender]
            if len(matches) == 1:
                return matches[0], "sender_address_matches_recorded_contact", None
            if len(matches) > 1:
                return (None, None, f"{sender} is recorded on {len(matches)} leads "
                                    f"({', '.join(sorted(matches)[:4])}); an address alone cannot pick one")
        return None, None, ("nothing recorded connects this sender or thread to a lead; unmatched events stay in "
                            "the intake queue for an operator to link, and a business name in a subject line is "
                            "not a reference")

    def _notify(self) -> dict[str, Any]:
        if self.notifier is None:
            return {"notified": 0, "code": "NO_CHANNEL_CONFIGURED",
                    "next_action": "run the Telegram bot, or read `inbound-pending` / GET /engagement/inbound."}
        try:
            return dict(self.notifier() or {})
        except Exception as error:  # noqa: BLE001 - a failed notification must not lose a recorded event.
            log_event(severity="WARNING", operation="email.notify", code="NOTIFICATION_FAILED",
                      message="operator notification could not be delivered",
                      detail=sanitize_diagnostic(error))
            return {"notified": 0, "code": "NOTIFICATION_FAILED"}

    def _record(self) -> dict[str, Any]:
        collection = self.store.state.get(SYNC_COLLECTION)
        record = collection.get(GMAIL_SYNC_KEY) if isinstance(collection, dict) else None
        return record if isinstance(record, dict) else {}

    def _advance(self, *, cursor: Any, page_token: Any, fetched: int, recorded: int,
                 had_refusals: bool = False) -> bool:
        record = dict(self._record())
        history = [row for row in (record.get("history") or []) if isinstance(row, dict)][-MAX_HISTORY_ROWS:]
        history.append({"at": _now(), "fetched": fetched, "recorded": recorded})
        record.update({"last_run_at": _now(), "cycles": int(record.get("cycles") or 0) + 1,
                       "last_fetched": fetched, "last_recorded": recorded, "last_page_token": page_token,
                       # A cycle that refused something keeps its error note: overwriting it with None here would
                       # make a visible failure disappear from the record after the messages were already written.
                       "history": history[-MAX_HISTORY_ROWS:],
                       "last_error": record.get("last_error") if had_refusals else None,
                       "source": self.source})
        if cursor:
            try:
                record["last_history_id"] = int(cursor)
                record.setdefault("history_id_source", "provider")
            except (TypeError, ValueError):
                pass
        self.store.state.setdefault(SYNC_COLLECTION, {})[GMAIL_SYNC_KEY] = record
        try:
            self.store._save()  # noqa: SLF001 - the store's own durable write, same as every other surface
        except Exception as error:  # noqa: BLE001 - the events are written; a lost cursor is a re-read, not a loss.
            log_event(severity="WARNING", operation="email.cursor", code="CURSOR_PERSISTENCE_FAILED",
                      message="the mailbox cursor could not be persisted; the next cycle re-reads and dedupes",
                      detail=sanitize_diagnostic(error))
            return False
        return True

    def _save_error(self, answer: dict[str, Any]) -> None:
        record = dict(self._record())
        record.update({"last_run_at": _now(), "last_error": {"at": _now(), **{
            key: value for key, value in answer.items() if key in ("error_code", "error", "provider_status")}},
            "last_error_retryable": bool(answer.get("retryable"))})
        unwritten = answer.get("unwritten")
        if unwritten:
            pending_ids = [str(item) for item in (record.get("unwritten") or []) if str(item)]
            if str(unwritten) not in pending_ids:
                pending_ids.append(str(unwritten))
            record["unwritten"] = pending_ids[-MAX_HISTORY_ROWS:]
        self.store.state.setdefault(SYNC_COLLECTION, {})[GMAIL_SYNC_KEY] = record
        try:
            self.store._save()  # noqa: SLF001
        except Exception:  # noqa: BLE001 - an error note about an error must not replace the original
            pass

    def _mark_cursor_unusable(self, code: str) -> None:
        record = dict(self._record())
        record["history_id_source"] = f"refused_by_provider ({code}); re-anchored from the profile"
        self.store.state.setdefault(SYNC_COLLECTION, {})[GMAIL_SYNC_KEY] = record


def _address_of(value: Any) -> str | None:
    match = _ADDRESS.search(str(value or ""))
    return match.group(0).lower() if match else None


def _now() -> str:
    from tracking.store import now

    return now()
