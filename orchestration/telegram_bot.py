"""Telegram as the operator's console for this agent - commands, approvals, and notifications.

Position of this module in the architecture: the Bot API is a *transport and a UI*. Everything it can do, it does
by calling the same application objects the CLI and the loopback API already use - ``JobService``,
``EngagementLedger``, ``InboundIntake``, ``OutreachService``, ``TrackingStore``. There is no second ledger here,
no second approval store, and no business rule living in the bot: a command either maps onto an interface that
already exists, or it says so and names what is missing.

Four rules the code enforces rather than promises:

* **Authorization first.** The allow-list is checked before a command is parsed and before any service is
  touched. An unauthorized update gets one fixed reply - it does not reveal whether an id exists, what a lead is
  called, or why a refusal happened. An empty allow-list authorizes nobody, so a half-configured bot is inert
  rather than open.
* **A message is never an approval.** Approvals arrive only as a button press bound to one record *and* to the
  state that was on screen when the button was rendered (see :func:`draft_fingerprint`), or as an explicit
  ``/approve <id> --confirm <code>`` naming the same fingerprint. Free text - "yes go ahead", "approved" - is
  answered with where the real button is. A stale fingerprint is refused rather than honoured.
* **Nothing is invented.** No command fabricates a result when the application cannot produce one: live
  discovery requires an explicit ``--live`` (and still passes the deployment's own live-lookup gate), an
  unmatchable inbound event stays unmatchable, and ``/package`` references the artifact and the command instead
  of assembling a new one.
* **Text is bounded and scrubbed.** Outbound text is contact-masked and secret-refused before it leaves, because
  a Telegram thread lives on a phone and in Telegram's servers, and a notification that quotes a customer's
  message would publish it there. The bot sends ids, statuses, counts, and URLs - never private message bodies.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Iterable

from .controls import hold_view, release_hold, set_hold
from .jobs import JobService
from .observability import log_event, sanitize_diagnostic
from .telegram_limits import attention_identity, clip_list, mask_address, safe_outbound_text
from integrations.telegram import ProviderError, describe_error as describe_provider_error, inline_keyboard
from tracking.engagement import (RELATIONSHIP_STAGES, EngagementLedger, LedgerError, redact_contact_details)

BOT_CHANNEL = "TELEGRAM"
#: How many rows one answer may carry. Telegram is a small screen; the durable answer is the CLI/API.
DEFAULT_ROWS = 8
MAX_ROWS = 20
#: `/find` bounds. `count` is a request to the scout, not a licence to enumerate a city.
FIND_COUNT_DEFAULT = 5
FIND_COUNT_MAX = 10
LOCATION_LIMIT = 80
CATEGORY_LIMIT = 60
#: Per-chat pacing, so a loop in an operator's client cannot turn into a burst against Telegram or the queue.
COMMANDS_PER_MINUTE_PER_CHAT = 20
NOTIFICATIONS_PER_CYCLE = 6

DECISION_BUTTONS = {
    "ap": ("APPROVED", "approved for sending"),
    "rj": ("REJECTED", "rejected"),
    "ch": ("CHANGES_REQUESTED", "sent back for changes"),
}
CONFIRM_WORD_LIMIT = 24


class TelegramBotError(ValueError):
    """A refusal the operator should see, with a stable code."""

    def __init__(self, message: str, *, code: str = "COMMAND_REFUSED") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------------------------------------
# command registry: one list that drives dispatch, /help, and /start, so no surface can drift from another
# ---------------------------------------------------------------------------------------------------------

#: name -> (short help, argument line, whether the command may change anything).
COMMANDS: dict[str, tuple[str, str, bool]] = {
    "start": ("What this bot is and what it will not do", "", False),
    "help": ("List the commands this build supports", "", False),
    "find": ("Submit a discovery request", "<location> <category> [count] [--live | --source-file <path>] [--country <name>]", True),
    "status": ("Job status, or the queue's shape", "[job_id]", False),
    "leads": ("Prospects, bounded", "[relationship_stage | QUALIFIED | ...]", False),
    "project": ("One project: build, validation, deployment, review state", "<project_id>", False),
    "inbox": ("Inbound responses a person must resolve", "[limit]", False),
    "followups": ("Follow-ups due or overdue", "[limit]", False),
    "proposals": ("Proposals awaiting a decision or a presentation", "[limit]", False),
    "attention": ("Everything the operator owes the pipeline", "[limit]", False),
    "review": ("An outreach draft with approve / reject / changes buttons", "[outreach_id]", False),
    "approve": ("Approve one draft, twice confirmed", "<outreach_id> [--note ...] --confirm <code>", True),
    "reject": ("Reject one draft, twice confirmed", "<outreach_id> [--note ...] --confirm <code>", True),
    "changes": ("Send one draft back for changes, twice confirmed", "<outreach_id> [--note ...] --confirm <code>", True),
    "package": ("Demo and review-package references for a project", "<project_id>", False),
    "pause": ("Hold automated dispatch", "[reason]", True),
    "resume": ("Release the hold", "", True),
    "hold": ("Is the system held, and by whom", "", False),
}


class TelegramOperatorBot:
    """The operator-facing command surface. Constructed with the application's own services.

    ``api`` is anything with ``send_message`` / ``edit_message_text`` / ``answer_callback_query`` /
    ``get_updates`` - :class:`integrations.telegram.TelegramBotApi` in production, a recorder in tests. With
    ``api=None`` the bot answers without transporting anything, which is how a dry run and the unit tests work.
    """

    def __init__(self, api: Any = None, *, store: Any, jobs: JobService | None = None,
                 ledger: EngagementLedger | None = None, intake: Any = None, outreach: Any = None,
                 allowed_user_ids: Iterable[int] = (), allowed_chat_ids: Iterable[int] = (),
                 actor: str = "telegram", notification_seen: set[str] | None = None,
                 row_limit: int = DEFAULT_ROWS) -> None:
        self.api = api
        self.store = store
        self.jobs = jobs or JobService(store)
        self.ledger = ledger or EngagementLedger(store, source=f"telegram:{actor}")
        self._intake = intake
        self.outreach = outreach
        self.allowed_user_ids = {int(value) for value in allowed_user_ids if str(value).strip()}
        self.allowed_chat_ids = {int(value) for value in allowed_chat_ids if str(value).strip()}
        self.actor = str(actor or "telegram")[:120]
        self.row_limit = max(1, min(int(row_limit or DEFAULT_ROWS), MAX_ROWS))
        #: In-process only, by design: the durable record of a condition is the job/project/inbound row itself,
        #: and a second store of "things we already told you" would be a second source of truth.
        self.seen_notifications = notification_seen if notification_seen is not None else set()
        self._recent_commands: dict[int, list[float]] = {}
        self.last_poll_offset: int | None = None
        self._seen_loaded = False

    # -- lazily built, because a bot that only answers /status must not need the engagement surface -----
    @property
    def intake(self):
        if self._intake is None:
            from tracking.inbound import InboundIntake

            self._intake = InboundIntake(self.ledger)
        return self._intake

    def _outreach(self):
        if self.outreach is None:
            from outreach.service import OutreachService

            self.outreach = OutreachService(self.store)
        return self.outreach

    # ------------------------------------------------------------------ entry points

    def handle_update(self, update: dict[str, Any], *, deliver: bool = True) -> dict[str, Any]:
        """Process one Bot API update. Returns a report; sends the reply when told to and able to."""
        if not isinstance(update, dict):
            return {"handled": False, "code": "UPDATE_MALFORMED"}
        if isinstance(update.get("callback_query"), dict):
            return self._handle_callback_update(update["callback_query"], deliver=deliver)
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            # Channel posts, joins, poll votes, and anything else Telegram can send are not operator commands.
            return {"handled": False, "code": "UPDATE_NOT_ACTIONABLE"}
        chat_id = _int((message.get("chat") or {}).get("id"))
        user_id = _int((message.get("from") or {}).get("id"))
        text = str(message.get("text") or "").strip()
        if not self._authorized(user_id=user_id, chat_id=chat_id):
            return self._refuse_unauthorized(chat_id=chat_id, user_id=user_id, update_id=_int(update.get("update_id")))
        if not text.startswith("/"):
            # Deliberate: an arbitrary Telegram message is not an approval, a reply, or a command.
            return self._reply(chat_id, "I only act on commands. /help lists them - and I never treat a typed "
                                         "message as an approval; use the buttons on /review.",
                               deliver=deliver, code="MESSAGE_NOT_A_COMMAND")
        return self.dispatch(text, user_id=user_id, chat_id=chat_id,
                             message_id=_int(message.get("message_id")), deliver=deliver)

    def _handle_callback_update(self, query: dict[str, Any], *, deliver: bool) -> dict[str, Any]:
        chat_id = _int(((query.get("message") or {}).get("chat") or {}).get("id"))
        user_id = _int((query.get("from") or {}).get("id"))
        message_id = _int((query.get("message") or {}).get("message_id"))
        data = str(query.get("data") or "")
        if not self._authorized(user_id=user_id, chat_id=chat_id):
            return self._refuse_unauthorized(chat_id=chat_id, user_id=user_id, update_id=_int(query.get("id")))
        result = self.handle_callback(data, user_id=user_id, chat_id=chat_id, message_id=message_id,
                                       callback_query_id=str(query.get("id") or ""))
        if deliver and self.api is not None and message_id and result.get("edit_text"):
            try:
                self.api.edit_message_text(chat_id, message_id, result["edit_text"],
                                           reply_markup=result.get("edit_markup"))
            except Exception as error:  # noqa: BLE001 - an unedited prompt must not lose the decision.
                result["edit_delivery"] = {"delivered": False, "error": sanitize_diagnostic(error)}
        return result

    def dispatch(self, text: str, *, user_id: int | None, chat_id: int | None,
                 message_id: int | None = None, deliver: bool = True) -> dict[str, Any]:
        """Authorize, rate-limit, parse, run. The only path any surface uses to make the bot act."""
        if not self._authorized(user_id=user_id, chat_id=chat_id):
            return self._refuse_unauthorized(chat_id=chat_id, user_id=user_id, update_id=None)
        if not self._within_rate_limit(chat_id):
            return self._reply(chat_id, "Slow down: too many commands from this chat in the last minute. "
                                        "Try again shortly.", deliver=deliver, code="RATE_LIMITED")
        name, _, argument_text = text.partition(" ")
        name = name.lstrip("/").split("@", 1)[0].strip().lower()  # /status@MyBot is /status for this bot
        handler = getattr(self, f"_command_{name}", None)
        if name not in COMMANDS or handler is None:
            return self._reply(chat_id, f"I do not know /{name}. /help lists the {len(COMMANDS)} commands this "
                                        "build supports.", deliver=deliver, code="COMMAND_UNSUPPORTED")
        try:
            answer = handler(argument_text.strip(), user_id=user_id, chat_id=chat_id or 0,
                             message_id=message_id)
        except TelegramBotError as error:
            answer = {"text": f"Refused ({error.code}): {error}", "code": error.code}
        except LedgerError as error:
            answer = {"text": f"The ledger refused that ({getattr(error, 'code', 'LEDGER_ERROR')}): {error}",
                      "code": getattr(error, "code", "LEDGER_ERROR")}
        except (ProviderError, ValueError) as error:
            answer = {"text": f"Refused: {safe_outbound_text(str(error))}", "code": "OPERATION_REFUSED"}
        except Exception as error:  # noqa: BLE001 - an internal fault is reported as a fault, never as a stack.
            log_event(severity="ERROR", operation="telegram.command", code="COMMAND_FAULT",
                      message=f"/{name} faulted", detail=sanitize_diagnostic(error))
            answer = {"text": f"Something went wrong running /{name}, so nothing was changed by it. "
                              f"The fault was recorded as COMMAND_FAULT.", "code": "COMMAND_FAULT"}
        answer.setdefault("command", name)
        return self._reply(chat_id, answer.pop("text", ""), reply_markup=answer.pop("reply_markup", None),
                           deliver=deliver, **answer)

    def handle_callback(self, data: str, *, user_id: int | None, chat_id: int | None,
                        message_id: int | None = None, callback_query_id: str = "") -> dict[str, Any]:
        """A button press. Authorization is re-checked here, because a callback is its own update."""
        if not self._authorized(user_id=user_id, chat_id=chat_id):
            return self._refuse_unauthorized(chat_id=chat_id, user_id=user_id, update_id=callback_query_id)
        prefix, _, remainder = str(data or "").partition(":")
        if prefix not in DECISION_BUTTONS:
            self._ack(callback_query_id, text="That button is not one of mine.", alert=True)
            return {"handled": False, "code": "CALLBACK_UNKNOWN"}
        outreach_id, _, fingerprint = remainder.rpartition(":")
        answer = self._record_decision(DECISION_BUTTONS[prefix][0], outreach_id.strip(), fingerprint.strip(),
                                      user_id=user_id)
        self._ack(callback_query_id, text=answer["notice"], alert=answer.get("alert", False))
        return {"handled": True, "code": answer["code"], "notice": answer["notice"],
                "edit_text": answer.get("edit_text"), "edit_markup": answer.get("edit_markup"),
                "outreach_id": outreach_id}

    def _ack(self, callback_query_id: str, *, text: str, alert: bool = False) -> None:
        if self.api is not None and callback_query_id:
            try:
                self.api.answer_callback_query(callback_query_id, text=text[:200], show_alert=alert)
            except Exception as error:  # noqa: BLE001 - the decision is already recorded; the toast is cosmetic.
                log_event(severity="WARNING", operation="telegram.callback", code="CALLBACK_ACK_FAILED",
                          message="button acknowledgement failed",
                          detail=sanitize_diagnostic(error))

    # ------------------------------------------------------------------ authorization

    def _authorized(self, *, user_id: int | None, chat_id: int | None) -> bool:
        """Both allow-lists must pass when they are populated; an empty pair authorizes nobody.

        Chat ids and user ids are separate on purpose: an operator may add the bot to a private group, and the
        owner may want to bind the bot to one chat *and* one account inside it. A message satisfies both, so a
        forwarded message from somebody else in that group is not an instruction.
        """
        if not self.allowed_user_ids and not self.allowed_chat_ids:
            return False
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            return False
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            return False
        return True

    def _refuse_unauthorized(self, *, chat_id: int | None, user_id: int | None, update_id: Any) -> dict[str, Any]:
        # One fixed sentence, no mention of what exists, and no echo of the text that arrived.
        log_event(severity="WARNING", operation="telegram.auth", code="AUTHORIZATION_REFUSED",
                  message="telegram update from an unlisted operator",
                  chat_id=chat_id, telegram_user_id=user_id, telegram_update_id=update_id)
        refusal = "This bot does not accept instructions from this account."
        delivered = False
        if self.api is not None and chat_id:
            try:
                self.api.send_message(chat_id, refusal)
                delivered = True
            except Exception:  # noqa: BLE001 - a refusal must not become an error loop with the provider.
                pass
        return {"handled": False, "code": "AUTHORIZATION_REFUSED", "reply": refusal, "delivered": delivered}

    def _within_rate_limit(self, chat_id: int | None) -> bool:
        now = time.monotonic()
        window = self._recent_commands.setdefault(int(chat_id or 0), [])
        window[:] = [stamp for stamp in window if now - stamp < 60.0]
        if len(window) >= COMMANDS_PER_MINUTE_PER_CHAT:
            return False
        window.append(now)
        return True

    # ------------------------------------------------------------------ replies

    def _reply(self, chat_id: int | None, text: str, *, reply_markup: dict[str, Any] | None = None,
               deliver: bool = True, **extra: Any) -> dict[str, Any]:
        body = safe_outbound_text(text)
        report = {"reply": body, "delivered": False, **extra}
        if reply_markup is not None:
            # `buttons` rather than the raw markup: an answer that says which actions it offered is what a test,
            # a log line, or an operator's screenshot needs; the payload itself already went to the transport.
            report["buttons"] = [[button["text"] for button in row] for row in reply_markup["inline_keyboard"]]
        if deliver and self.api is not None and chat_id:
            try:
                sent = self.api.send_message(chat_id, body, reply_markup=reply_markup)
                report["delivered"] = True
                report["message_id"] = (sent or {}).get("message_id")
                if (sent or {}).get("clipped"):
                    report["clipped"] = True
            except ProviderError as error:
                report["delivery"] = {"delivered": False, "error_code": error.code,
                                      "retryable": bool(error.retryable)}
                log_event(severity="WARNING", operation="telegram.send", code=error.code,
                          message="reply could not be delivered", context={"chat_id": chat_id})
            except Exception as error:  # noqa: BLE001 - the answer was computed; transport failure is reported.
                report["delivery"] = {"delivered": False, "error_code": "TRANSPORT_FAILED",
                                      "error": sanitize_diagnostic(error)}
        return report

    # ------------------------------------------------------------------ long polling

    def poll_once(self, *, timeout: float = 20.0, limit: int = 25,
                  deliver: bool = True) -> dict[str, Any]:
        """One getUpdates cycle: fetch, act on each update, advance the durable offset.

        The offset is persisted *after* the batch, so a crash mid-batch replays those updates. Replays are safe
        by construction here: job creation is idempotent, and an approval whose state already moved is refused as
        stale instead of applied twice.
        """
        if self.api is None:
            return {"handled": False, "code": "NO_TRANSPORT",
                    "next_action": "Construct the bot with api=TelegramBotApi(token) to poll."}
        offset = self._stored_offset()
        try:
            updates = self.api.get_updates(offset=offset, limit=limit, timeout=timeout)
        except ProviderError as error:
            return {"handled": False, "code": error.code, "retryable": bool(error.retryable),
                    **({"retry_after_seconds": error.retry_after} if error.retry_after else {}),
                    "next_action": "Keep polling: getUpdates is safe to retry, no update was lost."}
        processed = []
        for update in updates:
            result = self.handle_update(update, deliver=deliver)
            processed.append({"update_id": update.get("update_id"), "code": result.get("code") or "OK",
                              "handled": bool(result.get("handled"))})
        if updates:
            highest = max(_int(item.get("update_id")) or 0 for item in updates)
            if highest:
                self._store_offset(highest + 1)
        notified = self.notify_once(deliver=deliver) if self.notifier_enabled and updates else {"notified": 0}
        return {"handled": True, "fetched": len(updates), "notified": notified.get("notified"),
                "notification_code": notified.get("code"), "results": processed,
                "offset": self.last_poll_offset, "unauthorized": sum(1 for item in processed
                                                                     if item["code"] == "AUTHORIZATION_REFUSED")}

    @property
    def notifier_enabled(self) -> bool:
        """Whether the bot may push on its own initiative - transport plus one known chat."""
        return self.api is not None and bool(self.allowed_chat_ids)

    def _offset_key(self) -> str:
        return "telegram_poll"

    def _stored_offset(self) -> int | None:
        record = (self.store.state.get("automation_controls") or {}).get(self._offset_key())
        if isinstance(record, dict):
            value = _int(record.get("update_offset"))
            self.last_poll_offset = value
            return value
        return None

    def _store_offset(self, offset: int) -> None:
        self.store.state.setdefault("automation_controls", {})[self._offset_key()] = {
            "update_offset": int(offset), "updated_at": _now()}
        try:
            self.store._save()  # noqa: SLF001 - the same durable save every other control record uses
        except Exception as error:  # noqa: BLE001 - a lost offset costs a replay, not an update
            log_event(severity="WARNING", operation="telegram.poll", code="POLL_OFFSET_UNPERSISTED",
                      message="the getUpdates offset could not be persisted", detail=sanitize_diagnostic(error))
        self.last_poll_offset = int(offset)

    def run_forever(self, *, cycle_limit: int | None = None, timeout: float = 20.0,
                    backoff_seconds: float = 15.0) -> dict[str, Any]:
        """Poll until asked to stop. ``cycle_limit`` bounds a supervised run so a container can drain cleanly."""
        cycles = 0
        outcome: dict[str, Any] = {"cycles": 0, "updates": 0, "stopped_reason": None}
        try:
            while cycle_limit is None or cycles < int(cycle_limit):
                cycles += 1
                report = self.poll_once(timeout=timeout)
                outcome["updates"] += int(report.get("fetched") or 0)
                if report.get("retryable") and not report.get("handled"):
                    time.sleep(min(float(backoff_seconds), 300.0))
                outcome["last_report"] = report
                if cycle_limit is None:
                    # A service loop yields to the provider between cycles; long polling does the waiting.
                    continue
        except KeyboardInterrupt:  # pragma: no cover - operator interruption
            outcome["stopped_reason"] = "INTERRUPTED"
        outcome["cycles"] = cycles
        return outcome

    # ------------------------------------------------------------------ notifications

    def notify_once(self, *, chat_id: int | None = None, limit: int = NOTIFICATIONS_PER_CYCLE,
                    deliver: bool = True, persist_seen: bool = True) -> dict[str, Any]:
        """Push what needs a person, once per recorded condition.

        Dedupe identity is ``kind + code + record ids + recorded_at`` - the same field set
        :mod:`orchestration.alerts` uses - so re-polling the same unchanged record sends nothing. A record that
        genuinely changes (a new failure at a new timestamp) is a new condition and is reported again.
        """
        if self.api is None:
            return {"notified": 0, "code": "NO_TRANSPORT"}
        target = int(chat_id) if chat_id is not None else sorted(self.allowed_chat_ids)[0]
        if persist_seen and not self._seen_loaded:
            # Seed from the durable record so a one-shot run (cron, `telegram-notify`) does not re-page a
            # condition a long-lived bot already mentioned. In-process memory is a cache, not the truth.
            self.seen_notifications |= load_attention_seen(self.store)
            self._seen_loaded = True
        items = self._attention_items(limit=100)
        fresh = []
        for item in items:
            key = attention_identity(item)
            if key in self.seen_notifications:
                continue
            self.seen_notifications.add(key)
            fresh.append(item)
        if not fresh:
            return {"notified": 0, "suppressed_duplicates": len(items), "code": "NOTHING_NEW", "chat_id": target}
        sent, clipped = [], 0
        for item in fresh[:max(1, min(int(limit), NOTIFICATIONS_PER_CYCLE))]:
            text = safe_outbound_text(_attention_line(item, index=len(sent) + 1))
            if len(text) > 900:
                clipped += 1
            try:
                self.api.send_message(target, text)
                sent.append({"kind": item.get("kind"), "code": item.get("code")})
            except ProviderError as error:
                # A message Telegram would not take is *not* marked as seen: the condition is still unreported.
                self.seen_notifications.discard(attention_identity(item))
                return {"notified": len(sent), "code": error.code, "retryable": bool(error.retryable),
                        "next_action": "Retry on the next cycle; nothing was lost and nothing was double-sent."}
        if persist_seen:
            save_attention_seen(self.store, self.seen_notifications)
        remainder = max(0, len(fresh) - len(sent))
        if remainder:
            try:
                self.api.send_message(target, f"{remainder} more recorded condition(s) are waiting - /attention "
                                              "for the list. Not repeated here to keep this thread readable.")
            except ProviderError:
                pass
        return {"notified": len(sent), "suppressed_duplicates": len(items) - len(fresh),
                "deferred": remainder, "clipped": clipped, "chat_id": target, "code": "NOTIFIED"}

    def alert_sink(self, *, chat_id: int | None = None) -> "TelegramAlertSink":
        """The bot as a sink for the existing alert pipeline, so ``check_alerts --notify`` can reach a phone."""
        return TelegramAlertSink(self.api, chat_id=chat_id, fallback_chats=sorted(self.allowed_chat_ids))

    def _attention_items(self, *, limit: int = 100) -> list[dict[str, Any]]:
        from .health import operator_attention

        try:
            report = operator_attention(self.store, limit=max(1, min(int(limit), 100)))
        except Exception as error:  # noqa: BLE001 - the report is a convenience, not a dependency of a command.
            log_event(severity="WARNING", operation="telegram.attention", code="ATTENTION_UNREADABLE",
                      message="attention report could not be built", detail=sanitize_diagnostic(error))
            return []
        return list(report.get("items") or [])

    # ------------------------------------------------------------------ read-only commands

    def _command_start(self, argument: str, **_: Any) -> dict[str, Any]:
        return {"text": "AI Website Agent operator console.\n\n" + _command_index() + "\n\n"
                        "What this bot will not do: it never sends a message to a business, never approves "
                        "anything from free text, never marks a delivery or an outcome, and never invents a "
                        "result the application cannot produce. Approvals need the buttons on /review (or the "
                        "matching --confirm code), and every decision is recorded through the existing review "
                        "mechanism with you named as the reviewer.",
                "code": "INTRODUCTION"}

    def _command_help(self, argument: str, **_: Any) -> dict[str, Any]:
        lines = ["/" + name + (f" {spec[1]}" if spec[1] else "") + f" - {spec[0]}"
                 + (" (changes state)" if spec[2] else "") for name, spec in COMMANDS.items()]
        return {"text": "Commands this build supports:\n" + "\n".join(lines) +
                        "\n\nAnything else is refused rather than guessed at. /hold reports whether automated "
                        "processing is on hold.", "code": "HELP"}

    def _command_status(self, argument: str, **_: Any) -> dict[str, Any]:
        if argument:
            job_id = argument.split()[0]
            try:
                job = self.jobs.get(job_id)
            except ValueError:
                return {"text": f"No job '{job_id}' in this ledger. /status lists what is there.",
                        "code": "JOB_NOT_FOUND"}
            return {"text": _job_lines(job, self.store), "code": "JOB_STATUS"}
        jobs = list(self.store.state.get("jobs", {}).values())
        by_status: dict[str, int] = {}
        for job in jobs:
            if isinstance(job, dict):
                status = str(job.get("status"))
                by_status[status] = by_status.get(status, 0) + 1
        recent = sorted((j for j in jobs if isinstance(j, dict)),
                        key=lambda j: str(j.get("updated_at") or ""), reverse=True)[:self.row_limit]
        lines = [f"{len(jobs)} job(s) recorded; by status: " +
                 (", ".join(f"{key}={value}" for key, value in sorted(by_status.items())) or "none")]
        for job in recent:
            lines.append(_job_line(job))
        if recent:
            lines.append("Ask for one with /status <job_id>.")
        return {"text": "\n".join(lines), "code": "QUEUE_SUMMARY", "counts": by_status}

    def _command_leads(self, argument: str, **_: Any) -> dict[str, Any]:
        token = argument.split()[0].upper() if argument else ""
        limit = self.row_limit
        if token in RELATIONSHIP_STAGES:
            report = self.ledger.pipeline(stage=token, limit=limit)
            rows = report.get("items") or []
            lines = [f"Stage {token}: {len(rows)} of {report.get('total', len(rows))} lead(s) the ledger knows."]
            for row in rows:
                lines.append(f"• {row.get('lead_id')} - {mask_address(str(row.get('business_name') or ''))} | "
                             f"next: {row.get('next_action') or 'nothing recorded'}")
            lines.append(report.get("meaning") or "")
            return {"text": "\n".join(line for line in lines if line), "code": "PIPELINE_STAGE",
                    "count": len(rows), "stage": token}
        leads = [row for row in self.store.state.get("leads", {}).values() if isinstance(row, dict)]
        if token:
            leads = [row for row in leads if str(row.get("qualification_status") or "").upper() == token]
        leads.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        total = len(leads)
        window = leads[:limit]
        lines = [f"{total} lead(s){f' with qualification {token}' if token else ''}"
                 f"{f'; showing the newest {limit}' if total > limit else ''}."]
        for row in window:
            lines.append(f"• {row.get('lead_id')} - {_name(row)} | {row.get('business_type') or 'category unrecorded'} "
                         f"| {row.get('city') or 'location unrecorded'} | website: "
                         f"{row.get('website_status') or 'unknown'} | {row.get('qualification_status')}")
        if window:
            lines.append("Per-lead pipeline state: /leads <relationship_stage>; project detail: /project <project_id>.")
        return {"text": "\n".join(lines), "code": "LEADS", "count": min(total, limit)}

    def _command_project(self, argument: str, **_: Any) -> dict[str, Any]:
        if not argument:
            return {"text": "Name a project: /project project-0002", "code": "PROJECT_ID_REQUIRED"}
        from tracking.store import validate_record_id

        try:
            project_id = validate_record_id(argument.split()[0], kind="project_id")
        except ValueError as error:
            return {"text": f"'{argument.split()[0]}' is not a project id: {error}", "code": "PROJECT_ID_INVALID"}
        if project_id not in (self.store.state.get("projects") or {}):
            return {"text": f"No project '{project_id}' here.", "code": "PROJECT_NOT_FOUND"}
        return {"text": _project_lines(self.store, project_id), "code": "PROJECT_STATE"}

    def _command_inbox(self, argument: str, **_: Any) -> dict[str, Any]:
        limit = _rows_argument(argument, default=self.row_limit)
        report = self.intake.operator_backlog(limit=limit)
        items = report.get("items") or []
        counts = report.get("counts_by_disposition") or {}
        lines = [f"{report.get('total_matching', len(items)) if 'total_matching' in report else len(items)} "
                 f"inbound record(s) need a person." if items else
                 "Nothing is waiting in the inbound intake."]
        if counts:
            lines.append("By disposition: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
        for row in items[:limit]:
            lines.append(f"• {row.get('received_at')} | {row.get('provider')} | "
                         f"{row.get('error_code') or row.get('disposition')} | "
                         f"{('lead ' + str(row.get('lead_id'))) if row.get('lead_id') else 'no lead linked'} | "
                         f"{mask_address(str(row.get('sender') or 'sender unrecorded'))} | "
                         f"{row.get('next_action') or 'review'}")
        if items:
            lines.append(report.get("meaning") or "Nothing here changes a stage; these are intake problems to fix.")
        return {"text": "\n".join(lines), "code": "INBOX", "count": min(len(items), limit)}

    def _command_followups(self, argument: str, **_: Any) -> dict[str, Any]:
        limit = _rows_argument(argument, default=self.row_limit)
        report = self.ledger.outstanding_work(limit=limit)
        rows = report.get("due_follow_ups") or []
        lines = [f"{report.get('due_follow_up_count', len(rows))} follow-up(s) due"
                 + (f"; {len(rows)} shown" if len(rows) else "") + "." if rows else "No follow-up is due."]
        for row in rows[:limit]:
            lines.append(f"• {row.get('follow_up_id')} | {row.get('lead_id')} | due {row.get('due_at')} | "
                         f"{row.get('status')} | {_first(row.get('next_action'))} | "
                         f"evidence: {row.get('evidence_basis') or 'none recorded'}")
        if report.get("suppressed_by_opt_out"):
            lines.append(f"{report['suppressed_by_opt_out']} scheduled action(s) are suppressed by an opt-out and "
                         "stay suppressed.")
        lines.append(report.get("meaning") or "")
        return {"text": "\n".join(line for line in lines if line), "code": "FOLLOW_UPS", "count": len(rows)}

    def _command_proposals(self, argument: str, **_: Any) -> dict[str, Any]:
        limit = _rows_argument(argument, default=self.row_limit)
        report = self.ledger.outstanding_work(limit=limit)
        awaiting = report.get("proposals_awaiting_decision") or []
        presentation = report.get("proposals_awaiting_presentation") or []
        if not awaiting and not presentation:
            return {"text": "No proposal is waiting on a decision or a presentation.", "code": "PROPOSALS",
                    "count": 0}
        lines = [f"{len(awaiting)} proposal(s) awaiting a decision; "
                 f"{len(presentation)} awaiting presentation."]
        for row in awaiting[:limit]:
            lines.append(f"• {row.get('proposal_id')} | {row.get('lead_id')} | {row.get('business_name')} | "
                         f"{row.get('status')} | presented {row.get('presented_at')} | "
                         "record the customer's answer in the ledger (a reply here changes nothing)")
        for row in presentation[:limit]:
            lines.append(f"• {row.get('proposal_id')} | {row.get('lead_id')} | {row.get('status')} | "
                         "not yet presented to the business")
        return {"text": "\n".join(lines), "code": "PROPOSALS", "count": len(awaiting) + len(presentation)}

    def _command_attention(self, argument: str, **_: Any) -> dict[str, Any]:
        limit = _rows_argument(argument, default=self.row_limit)
        items = self._attention_items(limit=max(limit, 40))
        if not items:
            return {"text": "Nothing in the attention report. The queue, the projects, and the intake are clear.",
                    "code": "ATTENTION", "count": 0}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            grouped.setdefault(str(item.get("kind") or "other"), []).append(item)
        lines = [f"{len(items)} recorded condition(s) need a person, across {len(grouped)} kind(s): "
                 + ", ".join(f"{key}={len(value)}" for key, value in sorted(grouped.items())) + "."]
        for kind, rows in sorted(grouped.items()):
            for row in rows[:max(1, limit // max(1, len(grouped))) + 1]:
                lines.append(_attention_line(row, kind=kind))
        lines.append("Nothing here was changed by this command; it reads the same report the API and CLI show.")
        return {"text": "\n".join(clip_list(lines, MAX_ROWS * 3)), "code": "ATTENTION", "count": len(items),
                "by_kind": {key: len(value) for key, value in grouped.items()}}

    def _command_hold(self, argument: str, **_: Any) -> dict[str, Any]:
        view = hold_view(self.store)
        if view["held"]:
            blocked = self._blocked_by_hold()
            return {"text": f"On hold since {view['set_at']} by {view['set_by']} - {view['reason']}\n"
                            f"{blocked} job(s) were marked BLOCKED with AUTOMATION_HELD while it was on."
                            + ("\nThey are re-run by an explicit retry after /resume, never by the hold lifting."
                               if blocked else ""),
                    "code": "HELD", "held": True, "blocked_jobs": blocked}
        released = view.get("released_at") or "never"
        by = view.get("released_by")
        tail = f" by {by}" if by else ""
        unreadable = (f" The control record is unreadable ({view.get('error_code')}) - inspect it."
                      if view.get("unreadable") else "")
        return {"text": f"Not held. Last released {released}{tail}.{unreadable}",
                "code": "NOT_HELD", "held": False}

    def _command_package(self, argument: str, **_: Any) -> dict[str, Any]:
        if not argument:
            return {"text": "Name a project: /package project-0002", "code": "PROJECT_ID_REQUIRED"}
        project_id = argument.split()[0]
        if project_id not in (self.store.state.get("projects") or {}):
            return {"text": f"No project '{project_id}' here.", "code": "PROJECT_NOT_FOUND"}
        snap = self.store.snapshot(project_id)
        build, deployment, validation = snap.get("latestBuild"), snap.get("latestDeployment"), snap.get("latestValidation")
        lines = [f"{project_id} - {_name(snap.get('lead') or {})}"]
        if build:
            lines.append(f"Build {build.get('build_id')}: {build.get('status')}, artifact "
                         f"{build.get('artifact_sha256') or 'unrecorded'}"
                         f"{'' if snap.get('artifact_exists') else ' (file not present on this host)'}")
            lines.append(f"Artifact path (repository-relative): {build.get('output_location') or 'unrecorded'}")
        else:
            lines.append("No assigned build recorded yet.")
        if validation:
            lines.append(f"Validation {validation.get('validation_id')}: {validation.get('status')}")
        else:
            lines.append("No validation recorded for this build.")
        if deployment and deployment.get("deployment_url"):
            lines.append(f"Demo: {deployment['deployment_url']} ({deployment.get('deployment_status')}, "
                         f"build {deployment.get('build_id')})")
        else:
            lines.append("No deployed demo to link - the demo only appears once a deployment is recorded.")
        project = snap.get("project") or {}
        lines.append(f"Review: {project.get('review_status')} at stage {project.get('current_stage')}; "
                     f"approved build {project.get('approved_build_id') or 'none'} for "
                     f"{project.get('approved_action') or 'no action'}.")
        lines.append("The review package is assembled on demand and not stored as a path; build and check it with "
                     f"`python3 -m tracking.cli review-package {project_id}` or GET "
                     f"/projects/{project_id}/review-package, which refuses to write outside the export root.")
        lines.append(snap.get("next_action") or "")
        return {"text": "\n".join(line for line in lines if line), "code": "REVIEW_PACKAGE_REFS"}

    # ------------------------------------------------------------------ state-changing commands

    def _command_find(self, argument: str, *, user_id: int | None, chat_id: int | None, **_: Any) -> dict[str, Any]:
        """`/find <location> <category> [count] [--live | --source-file <path>] [--country <name>]`.

        Location and category must both be stated, a source must be named, and the count is bounded. Every one of
        those refusals exists because an unbounded or implied discovery request is the one thing in this pipeline
        that can turn into a lot of unwanted network traffic and a pile of half-researched strangers.
        """
        flags, positional = _parse_flags(argument)
        count = FIND_COUNT_DEFAULT
        if positional and positional[-1].isdigit():
            count = int(positional.pop())
        if len(positional) < 2:
            raise TelegramBotError(
                "Location and category both have to be stated, e.g. `/find Port Harcourt restaurants 5 --live`. "
                "Nothing is discovered on an assumption, and never without a source you named.",
                code="FIND_ARGUMENTS_REQUIRED")
        if "category" in flags:
            category = str(flags.pop("category"))[:CATEGORY_LIMIT]
            location = " ".join(positional).strip()
            if not positional or len(category) < 2:
                raise TelegramBotError("``--category`` needs a value and the location still has to be stated, "
                                       "e.g. `/find Port Harcourt --category restaurants`.",
                                       code="FIND_ARGUMENTS_REQUIRED")
        else:
            if len(positional) == 2 and count == FIND_COUNT_DEFAULT:
                # Two bare words could be "city + category" or a two-word city with no category at all. The bot
                # refuses the coin flip: an agent that guesses here fills a queue with the wrong kind of business.
                raise TelegramBotError(
                    f"'{positional[0]} {positional[1]}' is ambiguous: is '{positional[1]}' the category or part of "
                    f"the place? Name it either way - `/find {positional[0]} {positional[1]} 5` (count makes it a "
                    f"category) or `/find {' '.join(positional)} --category <category>`.",
                    code="FIND_ARGUMENT_AMBIGUOUS")
            category = positional.pop()
            location = " ".join(positional).strip()
        if len(location) > LOCATION_LIMIT or len(category) > CATEGORY_LIMIT:
            raise TelegramBotError(f"Location (max {LOCATION_LIMIT}) and category (max {CATEGORY_LIMIT}) must be "
                                   "short and explicit.", code="FIND_ARGUMENT_TOO_LONG")
        if not re.search(r"[A-Za-z]{2}", location) or not re.search(r"[A-Za-z]{2}", category):
            raise TelegramBotError("Location and category must be words, not ids or numbers - the scout searches "
                                   "on names, and a record id in this position means the command was misread.",
                                   code="FIND_ARGUMENT_UNPARSABLE")
        # The bound is checked after the words, so `/find 123 456 --live` is reported as a misread command
        # rather than as a count problem - the operator's actual mistake comes first.
        if not 1 <= count <= FIND_COUNT_MAX:
            raise TelegramBotError(f"Prospect count must be between 1 and {FIND_COUNT_MAX}; the scout is bounded "
                                   "on purpose, and this bot will not raise its ceiling.",
                                   code="FIND_COUNT_OUT_OF_RANGE")
        supplied_file = flags.get("source-file")
        # `--source-file` with no value after it is a mistake, not an empty path: refuse it as such.
        source_file = None if supplied_file in (None, True, "") else str(supplied_file)[:200]
        live = "live" in flags
        if not live and not source_file:
            raise TelegramBotError(
                "Name a source, so the request cannot be mistaken for open-ended crawling: add --live for the "
                "public OpenStreetMap/Overpass source this build supports, or --source-file <path> for a fixture. "
                "Live lookups additionally require this deployment to permit them.",
                code="FIND_SOURCE_REQUIRED")
        if live and source_file:
            raise TelegramBotError("Choose one source: --live or --source-file, not both.",
                                   code="FIND_SOURCE_AMBIGUOUS")
        country = str(flags.get("country") or "Nigeria")[:60]
        city, nation = _split_location(location, country)
        job_input: dict[str, Any] = {
            "source": "openstreetmap_overpass" if live else "fixture",
            "country": nation, "city": city, "niche": category, "lead_limit": count,
            "create_leads": True,
        }
        if live:
            # An operator typing the command in their own authorized chat *is* the human approval this source
            # asks for; the deployment gate still applies, and the bot cannot lift it.
            job_input["approve_live_lookups"] = True
        else:
            job_input["source_file"] = str(source_file)
        source_file = flags.get("source-file")
        live = "live" in flags
        if not live and not source_file:
            raise TelegramBotError(
                "Name a source, so the request cannot be mistaken for open-ended crawling: add --live for the "
                "public OpenStreetMap/Overpass source this build supports, or --source-file <path> for a fixture. "
                "Live lookups additionally require this deployment to permit them.",
                code="FIND_SOURCE_REQUIRED")
        if live and source_file:
            raise TelegramBotError("Choose one source: --live or --source-file, not both.",
                                   code="FIND_SOURCE_AMBIGUOUS")
        country = str(flags.get("country") or "Nigeria")[:60]
        city, nation = _split_location(location, country)
        job_input: dict[str, Any] = {
            "source": "openstreetmap_overpass" if live else "fixture",
            "country": nation, "city": city, "niche": category, "lead_limit": count,
            "create_leads": True,
        }
        if live:
            # An operator typing the command in their own authorized chat *is* the human approval this source
            # asks for; the deployment gate still applies, and the bot cannot lift it.
            job_input["approve_live_lookups"] = True
        else:
            job_input["source_file"] = str(source_file)
        identity = hashlib.sha256(
            f"{city}|{nation}|{category}|{count}|{job_input['source']}|{job_input.get('source_file', '')}".encode()
        ).hexdigest()[:12]
        try:
            job = self.jobs.create("SCOUT", job_id=f"tg-{identity}", priority="NORMAL",
                                   input=job_input, dry_run=not live,
                                   idempotency_key=f"telegram:{chat_id}:{identity}")
        except ValueError as error:
            raise TelegramBotError(str(error), code="JOB_CREATE_REFUSED") from None
        replay = job.get("status") not in {"PENDING", "QUEUED"}
        log_event(severity="INFO", operation="telegram.command", code="DISCOVERY_REQUESTED",
                  message=f"/find created job {job['job_id']}", job_id=job["job_id"],
                  requested_limit=count, scout_source=job_input["source"], actor=f"telegram:{user_id}")
        lines = [f"Discovery request recorded as {job['job_id']} ({job['status']}): {city}, {nation} - "
                 f"{category}, up to {count} prospect(s), source {job_input['source']}."]
        if replay:
            lines.append("That is the same request as before, so no second job was created.")
        lines.append("It runs when a worker or scheduler takes it (`run-job`, or the queue's own loop), and the "
                     f"hold applies: /hold. Follow it with /status {job['job_id']}.")
        if live:
            lines.append("Live lookups only happen if this deployment sets AI_WEBSITE_AGENT_ALLOW_LIVE_DISCOVERY=1; "
                         "otherwise the job fails with LIVE_DISCOVERY_NOT_ENABLED and no request leaves the machine.")
        return {"text": "\n".join(lines), "code": "DISCOVERY_REQUESTED", "job_id": job["job_id"]}

    def _command_pause(self, argument: str, *, user_id: int | None, **_: Any) -> dict[str, Any]:
        view = set_hold(self.store, actor=f"telegram:{user_id}", reason=argument or "requested from Telegram")
        blocked = self._blocked_by_hold()
        return {"text": "Automated dispatch is on hold: nothing new runs through the job gate, and every path "
                        "(worker, run-job, POST /jobs/:id/run and /retry) is refused with AUTOMATION_HELD before "
                        "a provider is contacted. No approval was revoked and no ledger record changed. "
                        f"{blocked} job(s) currently BLOCKED by the hold will need an explicit retry after /resume.",
                "code": "HELD", "held": True, "blocked_jobs": blocked}

    def _command_resume(self, argument: str, *, user_id: int | None, **_: Any) -> dict[str, Any]:
        view = release_hold(self.store, actor=f"telegram:{user_id}", note=argument or None)
        blocked = self._blocked_by_hold()
        return {"text": "The hold is released: the next dispatch attempt passes the hold check. It re-queues "
                        f"nothing on its own - {blocked} job(s) are still marked BLOCKED by it and are re-run by "
                        "an explicit retry. Every other gate still applies exactly as before.",
                "code": "AUTOMATION_RESUMED", "held": False, "blocked_jobs": blocked}

    def _command_review(self, argument: str, **_: Any) -> dict[str, Any]:
        rows = [row for row in (self.store.state.get("outreach") or {}).values() if isinstance(row, dict)]
        rows.sort(key=lambda row: str(row.get("generated_at") or ""), reverse=True)
        if argument:
            target = argument.split()[0]
            record = (self.store.state.get("outreach") or {}).get(target)
            if not isinstance(record, dict):
                return {"text": f"No outreach record '{target}'. /review lists the drafts that exist.",
                        "code": "OUTREACH_NOT_FOUND"}
            return self._review_prompt(record)
        awaiting = [row for row in rows if row.get("status") == "AWAITING_REVIEW"]
        if not awaiting:
            return {"text": f"No outreach draft is awaiting review ({len(rows)} recorded in total). "
                            "Drafts come from PREPARE_OUTREACH for a project that has passed human review.",
                    "code": "REVIEW_EMPTY"}
        lines = [f"{len(awaiting)} draft(s) await your review:"]
        for row in awaiting[:self.row_limit]:
            lines.append(f"• {row.get('outreach_id')} | {_name(self._lead_for(row))} | {row.get('channel')} | "
                         f"{row.get('status')} | /review {row.get('outreach_id')}")
        lines.append("Open one to get the approve / reject / changes buttons; a press records your decision "
                     "through the same review path the CLI uses, and nothing is sent to the business by this bot.")
        return {"text": "\n".join(lines), "code": "REVIEW_LIST", "count": len(awaiting)}

    def _review_prompt(self, record: dict[str, Any]) -> dict[str, Any]:
        lead = self._lead_for(record)
        outreach_id = str(record.get("outreach_id"))
        fingerprint = draft_fingerprint(record)
        readiness = None
        try:
            readiness = self._outreach().readiness(str(record.get("project_id")))
        except Exception as error:  # noqa: BLE001 - readiness is context, not a gate on looking.
            readiness = f"unreadable ({sanitize_diagnostic(error)})"
        lines = [
            f"{outreach_id} - {_name(lead)} ({record.get('channel')})",
            f"To: {mask_address(record.get('recipient'))} · lead {record.get('lead_id')} · "
            f"project {record.get('project_id')}",
            f"Demo: {record.get('preview_url') or 'unrecorded'}",
            f"Status: {record.get('status')} · generated {record.get('generated_at')} · readiness: {readiness}",
            "Message body is not reproduced here: a Telegram thread is not where a prospect's contact details "
            "belong. Read the draft in the review package or with "
            f"`python3 -m tracking.cli outreach {record.get('project_id')} --outreach-id {outreach_id}`.",
        ]
        if record.get("review_notes"):
            lines.append(f"Last note: {mask_address(str(record['review_notes'])[:200])}")
        buttons = inline_keyboard([
            [("Approve for sending", f"ap:{outreach_id}:{fingerprint}"),
             ("Reject", f"rj:{outreach_id}:{fingerprint}")],
            [("Ask for changes", f"ch:{outreach_id}:{fingerprint}")],
        ])
        lines.append("Pressing a button records the decision against exactly this draft and this version of it. "
                     "If anything changed since this screen appeared, the press is refused as stale.")
        return {"text": "\n".join(lines), "reply_markup": buttons, "code": "REVIEW_PROMPT",
                "outreach_id": outreach_id, "fingerprint": fingerprint}

    def _command_approve(self, argument: str, **kwargs: Any) -> dict[str, Any]:
        return self._typed_decision("APPROVED", argument, **kwargs)

    def _command_reject(self, argument: str, **kwargs: Any) -> dict[str, Any]:
        return self._typed_decision("REJECTED", argument, **kwargs)

    def _command_changes(self, argument: str, **kwargs: Any) -> dict[str, Any]:
        return self._typed_decision("CHANGES_REQUESTED", argument, **kwargs)

    def _typed_decision(self, decision: str, argument: str, *, user_id: int | None,
                        chat_id: int | None = None, **_: Any) -> dict[str, Any]:
        """`/approve <id> [--note ...] --confirm <code>`: the same binding the buttons carry.

        The confirmation code is the fingerprint of the record *as the operator last saw it*. Without it the
        command only shows that record and its code, which is what makes an approval a considered act on a known
        state rather than a guess typed at whatever the queue is doing now.
        """
        tokens = argument.split()
        if not tokens:
            raise TelegramBotError(f"Name the record: /{decision.split('_')[0].lower()} outreach-0001 "
                                   "--confirm <code>.", code="OUTREACH_ID_REQUIRED")
        outreach_id = tokens[0]
        record = (self.store.state.get("outreach") or {}).get(outreach_id)
        if not isinstance(record, dict):
            return {"text": f"No outreach record '{outreach_id}'. /review lists what exists.",
                    "code": "OUTREACH_NOT_FOUND"}
        remainder, note = _split_note(" ".join(tokens[1:]))
        flags, _positional = _parse_flags(remainder)
        confirm = str(flags.get("confirm") or "").strip()
        if not confirm:
            prompt = self._review_prompt(record)
            prompt["text"] = ("Confirm against the state you are reading. To record this decision, re-send with "
                              f"--confirm {draft_fingerprint(record)}\n\n" + prompt["text"])
            prompt["code"] = "CONFIRMATION_REQUIRED"
            prompt.pop("reply_markup", None)
            return prompt
        if len(confirm) > CONFIRM_WORD_LIMIT:
            raise TelegramBotError(f"The confirmation code is at most {CONFIRM_WORD_LIMIT} characters.",
                                   code="CONFIRMATION_INVALID")
        answer = self._record_decision(decision, outreach_id, confirm, user_id=user_id, note=note or None)
        if answer["code"] == "APPROVAL_STALE":
            fresh = self._review_prompt(record)
            return {"text": answer["notice"] + "\n\nHere is the record as it is now:",
                    "code": "APPROVAL_STALE", "reply_markup": fresh.get("reply_markup")}
        return {"text": answer["notice"], "code": answer["code"]}

    def _record_decision(self, decision: str, outreach_id: str, fingerprint: str, *, user_id: int | None,
                         note: str | None = None) -> dict[str, Any]:
        """The one place a decision is made, from a button or a typed command alike."""
        record = (self.store.state.get("outreach") or {}).get(outreach_id)
        if not isinstance(record, dict):
            return {"code": "OUTREACH_NOT_FOUND", "notice": f"No outreach record '{outreach_id}'. Nothing changed.",
                    "alert": True}
        if not outreach_id or not fingerprint:
            return {"code": "APPROVAL_UNBOUND",
                    "notice": "That button did not name a record and its state, so it cannot be honoured.",
                    "alert": True}
        current = draft_fingerprint(record)
        if fingerprint != current:
            # The draft, its recipient, its channel, or its status moved since this prompt was rendered.
            return {"code": "APPROVAL_STALE",
                    "notice": f"{outreach_id} changed since that screen was drawn, so this press records nothing. "
                              f"Current state fingerprint {current}; /review {outreach_id} to decide on it.",
                    "alert": True}
        if record.get("status") == decision_status(decision):
            return {"code": "APPROVAL_REPLAY",
                    "notice": f"{outreach_id} already carries this decision ({record.get('status')}, "
                              f"by {record.get('reviewed_by') or 'unattributed'} at "
                              f"{record.get('reviewed_at') or 'an unrecorded time'}). Nothing was written twice.",
                    "edit_text": _decision_line(outreach_id, record, "already recorded")}
        try:
            reviewed = self._outreach().review(outreach_id, decision, notes=_note_for(note, user_id),
                                               reviewed_by=f"telegram:{user_id}",
                                               identity_source="telegram_operator_allowlist")
        except ValueError as error:
            return {"code": "APPROVAL_REFUSED_BY_LEDGER",
                    "notice": f"The review mechanism refused this decision: {error}", "alert": True}
        log_event(severity="INFO", operation="telegram.approval", code="OUTREACH_DECISION_RECORDED",
                  message=f"outreach {outreach_id} {reviewed.get('status')}",
                  outreach_id=outreach_id, lead_id=reviewed.get("lead_id"),
                  project_id=reviewed.get("project_id"), actor=f"telegram:{user_id}")
        return {"code": "DECISION_RECORDED",
                "notice": _decision_line(outreach_id, reviewed, "recorded"),
                "edit_text": _decision_line(outreach_id, reviewed, "recorded"),
                # The buttons are removed on purpose: a decision that is recorded must not stay pressable, and a
                # second press would only be refused as a replay. The record is the answer, not the keyboard.
                "edit_markup": {"inline_keyboard": []},
                "status": reviewed.get("status")}

    # ------------------------------------------------------------------ helpers

    def _lead_for(self, record: dict[str, Any]) -> dict[str, Any]:
        return (self.store.state.get("leads") or {}).get(str(record.get("lead_id") or "")) or {}

    def _blocked_by_hold(self) -> int:
        from .controls import blocked_by_hold

        return len(blocked_by_hold(self.store, limit=100))


class TelegramAlertSink:
    """Send the existing pipeline's critical alerts to the operator's chat. Implements ``AlertSink``.

    It is a sink, not a notifier of its own: :func:`orchestration.alerts.check_alerts` still decides what is
    critical, still dedupes by ``alert_id``, and still sanitizes what it hands over. Nothing here reads the
    ledger, and nothing here decides whether something deserves attention.
    """

    name = "telegram"

    def __init__(self, api: Any, *, chat_id: int | None = None, fallback_chats: Iterable[int] = ()) -> None:
        self.api = api
        self.chat_id = int(chat_id) if chat_id is not None else (sorted(fallback_chats)[0] if fallback_chats else None)

    def publish(self, alert: dict[str, Any]) -> dict[str, Any]:
        if self.api is None or self.chat_id is None:
            return {"delivered": False, "sink": self.name, "code": "SKIPPED_TRANSPORT_NOT_CONFIGURED",
                    "next_action": "Pass api=TelegramBotApi(...) and a chat id, or set "
                                   "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS."}
        lines = [f"⚠ {alert.get('title') or alert.get('code') or 'Critical workflow condition'}"]
        for key, label in (("code", "code"), ("job_id", "job"), ("project_id", "project"),
                           ("lead_id", "lead"), ("build_id", "build"), ("next_action", "next")):
            value = alert.get(key)
            if value:
                lines.append(f"{label}: {mask_address(str(value))}")
        if alert.get("message"):
            lines.append(mask_address(redact_contact_details(str(alert["message"])[:300]) or "") or "")
        try:
            self.api.send_message(self.chat_id, safe_outbound_text("\n".join(line for line in lines if line)))
        except ProviderError as error:
            return {"delivered": False, "sink": self.name, "code": error.code,
                    "retryable": bool(error.retryable)}
        except Exception as error:  # noqa: BLE001 - a failed delivery is reported, never swallowed.
            return {"delivered": False, "sink": self.name, "code": "DELIVERY_FAILED",
                    "error": sanitize_diagnostic(error)}
        return {"delivered": True, "sink": self.name, "code": "DELIVERED", "alert_id": alert.get("alert_id")}


# ---------------------------------------------------------------------------------------------------------
# pure helpers, kept module-level so they can be tested without a bot
# ---------------------------------------------------------------------------------------------------------

#: How long a reported condition stays suppressed. One day, not forever: a blocker nobody cleared deserves to
#: be raised again, and a permanent silence would eventually hide a real one behind an old acknowledgement.
SUPPRESSION_WINDOW_SECONDS = 24 * 3600
#: Channel bookkeeping shares the sync collection with the mailbox cursor: neither is a ledger record.
NOTIFY_SYNC_KEY = "telegram_notifications"


def _notification_record(store) -> dict[str, Any]:
    collection = (getattr(store, "state", {}) or {}).get("provider_sync")
    return collection if isinstance(collection, dict) else {}


def _load_seen(store, field: str) -> set[str]:
    """Keys reported inside the suppression window, from the record - never from a guess."""
    record = _notification_record(store).get(field)
    if not isinstance(record, dict):
        return set()
    from datetime import datetime

    fresh: list[str] = []
    for key, stamp in record.items():
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if abs((datetime.now(moment.tzinfo) - moment).total_seconds()) <= SUPPRESSION_WINDOW_SECONDS:
            fresh.append(str(key))
    return set(fresh)


def _save_seen(store, field: str, seen: set[str]) -> None:
    stamp = _now()
    keep = sorted(seen)[-400:]  # bounded: a suppression cache is not a history
    store.state.setdefault("provider_sync", {})[field] = {key: stamp for key in keep}
    try:
        store._save()  # noqa: SLF001 - the store's own durable write
    except Exception as error:  # noqa: BLE001 - losing the cache costs a repeat message, not a lost event
        log_event(severity="WARNING", operation="telegram.notify", code="SUPPRESSION_UNPERSISTED",
                  message="notification suppression could not be persisted", detail=sanitize_diagnostic(error))


def load_attention_seen(store) -> set[str]:
    return _load_seen(store, "attention_seen")


def save_attention_seen(store, seen: set[str]) -> None:
    _save_seen(store, "attention_seen", seen)


def load_alert_seen(store) -> set[str]:
    """The alert pipeline's `seen` set, for a one-shot `check-alerts --telegram` run."""
    return _load_seen(store, "alert_seen")


def save_alert_seen(store, seen: set[str]) -> None:
    _save_seen(store, "alert_seen", seen)


def draft_fingerprint(record: dict[str, Any]) -> str:
    """Bind an approval to one draft's content, recipient, channel, lead, and state.

    It is not a token or a secret: it is a checksum of what the operator was looking at. Recomputing it after a
    change is worthless to an attacker, because there is nothing on offer without the allow-listed account that
    pressed the button - and it makes staleness *decidable*, which is the whole point.
    """
    material = "\x1f".join(str(record.get(key) or "") for key in
                           ("outreach_id", "lead_id", "project_id", "channel", "recipient", "preview_url",
                            "draft_message", "status", "generated_at"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def decision_status(decision: str) -> str:
    return {"APPROVED": "READY_TO_SEND", "REJECTED": "REJECTED", "CHANGES_REQUESTED": "CHANGES_REQUESTED"}[decision]


def _decision_line(outreach_id: str, record: dict[str, Any], verb: str) -> str:
    return (f"{outreach_id} {verb}: {record.get('status')} by {record.get('reviewed_by') or 'unattributed'} at "
            f"{record.get('reviewed_at')} ({record.get('reviewer_identity_source') or 'identity source unrecorded'}). "
            "Recording a decision sends nothing; the ledger still needs someone to report an actual send.")


def _note_for(note: str | None, user_id: int | None) -> str:
    """Notes stay attributable, because "approved" without a who is not an audit trail."""
    text = str(note or "").strip()[:500]
    return f"telegram:{user_id}: {text}" if text else f"approved from Telegram by operator {user_id}"


def _attention_line(item: dict[str, Any], *, index: int | None = None, kind: str | None = None) -> str:
    prefix = f"{index}. " if index else ""
    label = kind or str(item.get("kind") or "condition")
    pieces = [f"{prefix}[{label.upper()}]", str(item.get("code") or "no code"), str(item.get("title") or "").strip()]
    for key in ("job_id", "project_id", "lead_id", "outreach_id", "proposal_id", "follow_up_id"):
        value = item.get(key)
        if value:
            pieces.append(f"{key}={value}")
    if item.get("message"):
        pieces.append(mask_address(redact_contact_details(str(item["message"])[:220]) or ""))
    if item.get("next_action"):
        pieces.append(f"next: {str(item['next_action'])[:200]}")
    return " | ".join(piece for piece in pieces if piece)


def _command_index() -> str:
    return "\n".join(f"/{name}{(' ' + spec[1]) if spec[1] else ''}" for name, spec in COMMANDS.items())


def _job_line(job: dict[str, Any]) -> str:
    pieces = [f"• {job.get('job_id')}", str(job.get("job_type")), str(job.get("status"))]
    if job.get("lead_id"):
        pieces.append(f"lead {job['lead_id']}")
    if job.get("project_id"):
        pieces.append(f"project {job['project_id']}")
    pieces.append(f"attempt {job.get('attempts', 0)}/{job.get('max_attempts', '?')}")
    if job.get("error_code"):
        pieces.append(f"code {job['error_code']}")
    return " | ".join(pieces)


def _job_lines(job: dict[str, Any], store: Any) -> str:
    lines = [_job_line(job)]
    if job.get("error"):
        lines.append(f"reason: {mask_address(redact_contact_details(str(job['error'])[:400]) or '')}")
    if job.get("execution_category"):
        lines.append(f"execution: {job['execution_category']}")
    if job.get("heartbeat_at"):
        lines.append(f"last heartbeat recorded: {job['heartbeat_at']}")
    artifacts = job.get("artifacts") or []
    if artifacts:
        lines.append("artifacts: " + ", ".join(mask_address(str(item))[:80] for item in artifacts[:3]))
    suggested = job.get("next_job_types") or []
    if suggested:
        lines.append(f"next types the pipeline suggests: {', '.join(suggested)}")
    if job.get("status") == "BLOCKED" and job.get("error_code") == "AUTOMATION_HELD":
        lines.append("This is the operator's hold, not a fault: /hold says who set it and /resume releases it.")
    if job.get("status") == "COMPLETED" and job.get("job_type") == "SCOUT":
        output = job.get("output") or {}
        if isinstance(output, dict) and output.get("lead_ids"):
            lines.append(f"leads from this run: {', '.join(str(x) for x in output['lead_ids'][:10])}")
    return "\n".join(lines)


def _project_lines(store: Any, project_id: str) -> str:
    snap = store.snapshot(project_id)
    project = snap.get("project") or {}
    build, validation, deployment, outreach = (snap.get("latestBuild"), snap.get("latestValidation"),
                                              snap.get("latestDeployment"), snap.get("latestOutreach"))
    lines = [f"{project_id} - {_name(snap.get('lead') or {})} | stage {project.get('current_stage')} | review "
             f"{project.get('review_status')} | {project.get('data_classification')}"]
    lines.append(f"build: {build.get('build_id') if build else 'none'} "
                 f"{('(' + str(build.get('status')) + ')') if build else ''}")
    lines.append(f"validation: {validation.get('status') if validation else 'none'}")
    lines.append(f"deployment: {deployment.get('deployment_status') if deployment else 'none'}"
                 + (f" at {deployment.get('deployment_url')}" if deployment and deployment.get("deployment_url") else ""))
    lines.append(f"outreach draft: {outreach.get('outreach_id')} ({outreach.get('status')})"
                 if outreach else "outreach draft: none")
    lines.append(f"next action: {snap.get('next_action') or 'nothing recorded'}")
    workflow = snap.get("workflow_state")
    if isinstance(workflow, dict) and workflow:
        lines.append("workflow: " + ", ".join(f"{key}={workflow[key]}" for key in sorted(workflow)[:6]))
    elif isinstance(workflow, str) and workflow:
        lines.append(f"workflow: {workflow}")
    return "\n".join(line for line in lines if line)


def _name(lead: dict[str, Any]) -> str:
    return mask_address(str(lead.get("business_name") or "business name unrecorded"))


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value[:3]) or "none recorded"
    return str(value or "none recorded")


def _split_location(location: str, country: str) -> tuple[str, str]:
    """`Port Harcourt, Nigeria` -> city and country; a bare place keeps the caller's default country."""
    parts = [piece.strip() for piece in str(location).split(",") if piece.strip()]
    if len(parts) >= 2:
        return parts[-1], parts[0] if len(parts) == 2 else ", ".join(parts[:-1])
    return (parts[0] if parts else str(location).strip())[:LOCATION_LIMIT], country


def _parse_flags(text: str) -> tuple[dict[str, Any], list[str]]:
    """A deliberately tiny ``--flag [value]`` parser: no nesting, no quoting rules to forget.

    ``--live`` is a bare flag; ``--source-file path`` and ``--country Nigeria`` take the next token. Anything that
    does not start with ``--`` is positional. That is all the syntax a chat message should carry.
    """
    tokens = str(text or "").split()
    flags: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            name = token[2:].strip()
            if not name:
                raise TelegramBotError("A flag needs a name (``--live``, ``--source-file <path>``).",
                                       code="FLAG_NAME_MISSING")
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if following is not None and not following.startswith("--"):
                flags[name] = following
                index += 2
            else:
                flags[name] = True
                index += 1
            continue
        positional.append(token)
        index += 1
    return flags, positional


def _split_note(text: str) -> tuple[str, str]:
    """Everything after ``--note `` is the note, to the end of the line. Notes are prose, not tokens."""
    match = re.search(r"--note[= ](.+)$", str(text or ""), flags=re.S)
    if not match:
        return str(text or "").strip(), ""
    note = match.group(1).strip()[:500]
    return str(text[:match.start()]).strip(), note


def _rows_argument(text: str, *, default: int) -> int:
    tokens = [chunk for chunk in str(text or "").split() if chunk.isdigit()]
    value = int(tokens[0]) if tokens else default
    return max(1, min(value, MAX_ROWS))


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    from tracking.store import now

    return now()
