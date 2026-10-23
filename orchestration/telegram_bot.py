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
from .telegram_limits import attention_identity, clip_list, mask_address, safe_outbound_text, safe_preview_text
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
#: Every other button this bot understands, as ``prefix -> (action, button label)``. Each one carries the id and
#: the fingerprint of the record it was drawn for, and every press re-checks that pair before acting: a keyboard
#: in a chat thread is a *shortcut to a decision*, never the decision itself.
ACTION_BUTTONS = {
    "dm": ("demo_approve", "Approve for preview"),
    "pq": ("publish_queue", "Queue preview publish"),
    "pv": ("send_preview", "Show what would be sent"),
    "se": ("send_arm", "Send this"),
    "sc": ("send_confirm", "Confirm: send it now"),
    "rc": ("send_reconcile", "Ask the account what happened"),
}
#: The confirmation word a typed send command needs, beside the content fingerprint. A repeated command must not
#: mail the same prospect twice, so the second act has to be a different act.
SEND_CONFIRMATION_WORD = "SEND"
#: The machine-level switch a real send additionally needs. Kept out of the bot's own settings on purpose: an
#: operator who wants this deployment to be unable to send, whatever a chat says, can turn it off globally.
LIVE_OUTREACH_ENV = "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH"
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
    "demo": ("Approve the current build for preview publishing", "<project_id> [--note ...] [--confirm <code>]", True),
    "outbox": ("Approved drafts that have not been sent, and what blocks each", "[limit]", False),
    "send": ("Preview or perform one approved send, twice confirmed", "<outreach_id> [--dry | --confirm send]", True),
    "sent": ("What the provider actually said about one send", "<outreach_id>", False),
    "reconcile": ("Ask the account about a send that got no answer", "[outreach_id]", False),
    "optout": ("Record that a business asked not to be contacted", "<lead_id> --note ... [--confirm <code>]", True),
    "pause": ("Hold automated dispatch", "[reason]", True),
    "resume": ("Release the hold", "", True),
    "hold": ("Is the system held, and by whom", "", False),
    "social": ("Social channels, verified Buffer targets, and queue status", "", False),
    "post": ("Social media draft operations: list, new, view, approve, reject", "[list | new <channel> <topic> | view <id> | approve <id> | reject <id>]", True),
    "briefing": ("Daily business briefing: pipeline, opportunities, overdue tasks, and recommendations", "[--trigger | --status]", False),
    "daily": ("Alias for /briefing", "[--trigger | --status]", False),
    "sites": ("List deployed business websites and stable site IDs", "[--all]", False),
    "site": ("Show one deployed website and its Vercel project identity", "<site_id>", False),
    "deletesite": ("Request deletion review for one exact Vercel project; does not delete", "<site_id>", True),
    "confirmdelete": ("Confirm a pending Vercel project deletion by its one-time fingerprint", "<site_id> <fingerprint>", True),
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
                 row_limit: int = DEFAULT_ROWS, outbound_client: Any = None,
                 readback_client: Any = None) -> None:
        self.api = api
        self.store = store
        self.jobs = jobs or JobService(store)
        self.ledger = ledger or EngagementLedger(store, source=f"telegram:{actor}")
        self._intake = intake
        self.outreach = outreach
        self.allowed_user_ids = {int(value) for value in allowed_user_ids if str(value).strip()}
        self.allowed_chat_ids = {int(value) for value in allowed_chat_ids if str(value).strip()}
        self.actor = str(actor or "telegram")[:120]
        #: Injectable for tests and for an operator running the bot behind a proxy; never a second send path.
        self.outbound_client = outbound_client
        self.readback_client = readback_client
        self._outbound_service = None
        self._social_service = None
        self._briefing_service = None
        self.row_limit = max(1, min(int(row_limit or DEFAULT_ROWS), MAX_ROWS))
        #: In-process only, by design: the durable record of a condition is the job/project/inbound row itself,
        #: and a second store of "things we already told you" would be a second source of truth.
        self.seen_notifications = notification_seen if notification_seen is not None else set()
        self._recent_commands: dict[int, list[float]] = {}
        self.last_poll_offset: int | None = None
        self._seen_loaded = False

    def _social(self):
        if self._social_service is None:
            from social.service import SocialDraftService
            self._social_service = SocialDraftService()
        return self._social_service

    def _briefing(self):
        if self._briefing_service is None:
            from orchestration.briefing import DailyBriefingService
            self._briefing_service = DailyBriefingService(store=self.store, ledger=self.ledger, jobs=self.jobs)
        return self._briefing_service

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

    def _outbound(self):
        """The send executor, built on first use so a bot that only reads never needs Gmail credentials.

        ``allow_live`` is not the bot's decision: it mirrors the machine-level flag, so an operator who has not
        turned sending on for this deployment gets a refusal that names the flag instead of a message in a
        prospect's inbox. Everything else - approval, opt-out, budget, staleness - is the executor's and the
        ledger's, re-read at send time.
        """
        if self._outbound_service is None:
            from .outbound import ApprovedOutboundSender

            self._outbound_service = ApprovedOutboundSender(
                self.store, outreach=self._outreach(), ledger=self.ledger, allow_live=self.live_outreach_allowed(),
                client=self.outbound_client, readback_client=self.readback_client)
        return self._outbound_service

    def live_outreach_allowed(self) -> bool:
        import os

        return (os.environ.get(LIVE_OUTREACH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}

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
        preview_sensitive = bool(answer.pop("preview_sensitive", False))
        return self._reply(chat_id, answer.pop("text", ""), reply_markup=answer.pop("reply_markup", None),
                           deliver=deliver, preview_sensitive=preview_sensitive, **answer)

    def handle_callback(self, data: str, *, user_id: int | None, chat_id: int | None,
                        message_id: int | None = None, callback_query_id: str = "") -> dict[str, Any]:
        """A button press. Authorization is re-checked here, because a callback is its own update."""
        if not self._authorized(user_id=user_id, chat_id=chat_id):
            return self._refuse_unauthorized(chat_id=chat_id, user_id=user_id, update_id=callback_query_id)
        prefix, _, remainder = str(data or "").partition(":")
        if prefix not in DECISION_BUTTONS and prefix not in ACTION_BUTTONS:
            self._ack(callback_query_id, text="That button is not one of mine.", alert=True)
            return {"handled": False, "code": "CALLBACK_UNKNOWN"}
        outreach_id, _, fingerprint = remainder.rpartition(":")
        if prefix in ACTION_BUTTONS:
            return self._action_callback(ACTION_BUTTONS[prefix][0], outreach_id.strip(), fingerprint.strip(),
                                         user_id=user_id, chat_id=chat_id, message_id=message_id,
                                         callback_query_id=callback_query_id, prefix=prefix)
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
        preview_sensitive = bool(extra.pop("preview_sensitive", False))
        body = safe_preview_text(text) if preview_sensitive else safe_outbound_text(text)
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
        count_was_provided = bool(positional and positional[-1].isdigit())
        if count_was_provided:
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
            if len(positional) == 2 and not count_was_provided:
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

    def _managed_sites(self):
        from deployment.site_management import ManagedSiteService
        if getattr(self, "_managed_site_service", None) is None:
            self._managed_site_service = ManagedSiteService(self.store)
        return self._managed_site_service

    def _command_sites(self, argument: str, **_: Any) -> dict[str, Any]:
        include_deleted = "--all" in argument.split()
        rows = self._managed_sites().list_sites(include_deleted=include_deleted, limit=self.row_limit)
        if not rows:
            return {"text": "No successful Vercel deployments are recorded yet. A local draft or pending review is not a deployed website.", "code": "SITES_EMPTY", "count": 0}
        lines = [f"{len(rows)} website(s) shown. Older deployments remain in the tracking history."]
        for row in rows:
            lines.append(f"• {row.get('site_id')} | {row.get('business_name')} | {row.get('site_status')}\n  {row.get('deployment_url')}\n  Vercel project: {row.get('vercel_project_name') or 'unrecorded'} | history: {row.get('deployment_history_count', 0)}")
        lines.append("Use /site <site_id> for details. /deletesite <site_id> only prepares a deletion request; it does not delete anything.")
        return {"text": "\n".join(lines), "code": "SITES_LIST", "count": len(rows)}

    def _command_site(self, argument: str, **_: Any) -> dict[str, Any]:
        tokens = argument.split()
        if not tokens:
            raise TelegramBotError("Use /site <site_id> from /sites.", code="SITE_ID_REQUIRED")
        row = self._managed_sites().get_site(tokens[0], include_deleted=True)
        lines = [f"{row.get('site_id')} — {row.get('business_name')}", f"Status: {row.get('site_status')}",
                 f"URL: {row.get('deployment_url') or 'not recorded'}",
                 f"Vercel project: {row.get('vercel_project_name') or 'unrecorded'}",
                 f"Vercel project ID: {row.get('vercel_project_id') or 'unrecorded'}",
                 f"Lead: {row.get('lead_id') or 'unrecorded'}",
                 f"Deployment history count: {row.get('deployment_history_count', 0)}",
                 f"Last deployed: {row.get('last_deployed_at') or 'unrecorded'}"]
        pending = row.get("pending_deletion") or {}
        if pending: lines.append(f"Deletion state: {pending.get('status')}")
        if row.get("site_status") != "DELETED": lines.append("To review deletion, use /deletesite " + str(row.get("site_id")) + ".")
        return {"text": "\n".join(lines), "code": "SITE_DETAIL", "site_id": row.get("site_id")}

    def _command_deletesite(self, argument: str, *, user_id: int | None, **_: Any) -> dict[str, Any]:
        tokens = argument.split()
        if not tokens: raise TelegramBotError("Use /deletesite <site_id>. This only prepares a request.", code="SITE_ID_REQUIRED")
        result = self._managed_sites().request_delete(tokens[0], actor=f"telegram:{user_id}")
        site, request = result["site"], result["request"]
        lines = ["DELETION REVIEW — NOTHING HAS BEEN DELETED",
                 f"Business: {site.get('business_name')}", f"Site ID: {site.get('site_id')}",
                 f"Vercel project: {request.get('vercel_project_name')}",
                 f"Exact Vercel project ID: {request.get('vercel_project_id')}",
                 f"Current URL: {request.get('deployment_url')}", f"Request ID: {request.get('request_id')}",
                 f"One-time confirmation fingerprint: {request.get('fingerprint')}",
                 "This removes the Vercel project itself, not just one deployment. Attached deployments/domains may be affected.",
                 "To proceed, send /confirmdelete " + str(site.get("site_id")) + " " + str(request.get("fingerprint")),
                 "If unsure, do nothing. No resource is removed until the exact fingerprint is confirmed."]
        return {"text": "\n".join(lines), "code": "SITE_DELETE_AWAITING_CONFIRMATION",
                "site_id": site.get("site_id"), "request_id": request.get("request_id"),
                "fingerprint": request.get("fingerprint"), "deletion_performed": False}

    def _command_confirmdelete(self, argument: str, *, user_id: int | None, **_: Any) -> dict[str, Any]:
        tokens = argument.split()
        if len(tokens) != 2:
            raise TelegramBotError("Use /confirmdelete <site_id> <one-time-fingerprint> exactly as shown. Nothing is deleted without both.", code="SITE_DELETE_CONFIRMATION_REQUIRED")
        result = self._managed_sites().confirm_delete(tokens[0], tokens[1], actor=f"telegram:{user_id}")
        return {"text": f"Vercel project deletion confirmed by provider for {result.get('business_name')}."
                f"\nSite ID: {result.get('site_id')}\nProject: {result.get('vercel_project_name')}"
                f"\nFormer URL: {result.get('deployment_url')}\nStatus: DELETED"
                "\nHistoric deployment and deletion audit records are retained.",
                "code": "SITE_DELETED", "site_id": result.get("site_id"), "deletion_performed": True}
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
            f"Lead: {record.get('lead_id')} · project {record.get('project_id')}",
            f"Status: {record.get('status')} · generated {record.get('generated_at')} · readiness: {readiness}",
            "FULL EMAIL PREVIEW — review this exact recipient, subject, and body before approving:",
            f"To: {record.get('recipient') or '(recipient missing)'}",
            f"Subject: {record.get('subject') or '(none)'}",
            f"Preview URL: {record.get('preview_url') or '(preview URL missing)'}",
            "Body:",
            str(record.get('draft_message') or '(body missing)'),
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
                "outreach_id": outreach_id, "fingerprint": fingerprint, "preview_sensitive": True}

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
        rest, confirm_from_line = _split_confirm(" ".join(tokens[1:]))
        remainder, note = _split_note(rest)
        flags, _positional = _parse_flags(remainder)
        confirm = str(confirm_from_line or flags.get("confirm") or "").strip()
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

    # ------------------------------------------------------------------ demo approval (the gated preview publish)

    def _command_demo(self, argument: str, *, user_id: int | None, chat_id: int | None = None,
                      **_: Any) -> dict[str, Any]:
        """/demo <project_id> [--note ...] [--confirm <code>]: the approval a preview publish needs.

        The decision goes through the store's own review mechanism - :meth:`tracking.store.TrackingStore.record_review`
        with ``APPROVED`` + ``PREVIEW_DEPLOYMENT`` - because that is where a reviewer-authenticated decision is
        turned into the build-local capability the publish step verifies. Nothing in this command publishes, and
        supplying a URL or a project id is not an approval: the confirmation code is the project's state digest, so
        a press that arrives after the build, validation, or stage moved is refused as stale.
        """
        tokens = argument.split()
        if not tokens:
            raise TelegramBotError("Name the project: /demo project-0002 --note \"looked at the copy\" "
                                   "--confirm <code>.", code="PROJECT_ID_REQUIRED")
        project_id = tokens[0]
        project = (self.store.state.get("projects") or {}).get(project_id)
        if not isinstance(project, dict):
            return {"text": f"No project '{project_id}'. /leads and /status show what this system is working on.",
                    "code": "PROJECT_NOT_FOUND"}
        rest, confirm_from_line = _split_confirm(" ".join(tokens[1:]))
        remainder, note = _split_note(rest)
        flags, _positional = _parse_flags(remainder)
        confirm = str(confirm_from_line or flags.get("confirm") or "").strip()
        gate = self._demo_gate(project_id)
        prompt = self._demo_prompt(project_id, gate)
        if not confirm:
            prompt["text"] = ("Nothing has been approved yet. If the demo is right, re-send with "
                              f"--confirm {prompt['fingerprint']}\n\n" + prompt["text"])
            prompt["code"] = "CONFIRMATION_REQUIRED"
            prompt.pop("reply_markup", None)
            return prompt
        if len(confirm) > CONFIRM_WORD_LIMIT:
            raise TelegramBotError(f"A confirmation code is at most {CONFIRM_WORD_LIMIT} characters.",
                                   code="CONFIRMATION_INVALID")
        if confirm != prompt["fingerprint"]:
            prompt["text"] = ("That code does not match the project's current state, so nothing was approved "
                              f"(current code {prompt['fingerprint']}). The build, validation, deployment, or "
                              "stage moved since you were shown it.\n\n" + prompt["text"])
            prompt["code"] = "DEMO_STALE"
            return prompt
        return self._record_demo_approval(project_id, note=note or None, user_id=user_id, chat_id=chat_id)

    def _demo_gate(self, project_id: str) -> dict[str, Any]:
        """The answer the publish step itself will compute, read-only. ``None`` when the store refuses to say."""
        try:
            return self.store.authorize_preview_deployment(project_id)
        except Exception as error:  # noqa: BLE001 - a gate that cannot be read is a fact to report, not a crash
            return {"authorized": False, "code": "GATE_UNREADABLE",
                    "reason": sanitize_diagnostic(error).get("detail") or "the durable approval could not be read",
                    "next_action": "check the tracking state file; nothing publishes on an unreadable gate"}

    def _demo_prompt(self, project_id: str, gate: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.store.snapshot(project_id)
        project, build = snapshot.get("project") or {}, snapshot.get("latestBuild") or {}
        validation, deployment = (snapshot.get("latestValidation") or {},
                                  snapshot.get("latestDeployment") or {})
        lead = snapshot.get("lead") or {}
        fingerprint = demo_fingerprint(project)
        lines = [
            f"{project_id} - {_name(lead)}",
            f"Stage: {project.get('current_stage')} · review: {project.get('review_status')} · build "
            f"{project.get('assigned_build_id') or 'unassigned'} (status {build.get('status') or 'unrecorded'})",
            f"Validation: {(validation or {}).get('status') or 'none'} · deployment: "
            f"{(deployment or {}).get('deployment_status') or 'none'}",
            f"Gate right now: {gate.get('code') or 'UNKNOWN'} - {str(gate.get('reason') or 'authorized')[:220]}",
        ]
        if (deployment or {}).get("deployment_url"):
            lines.append(f"Published preview: {deployment['deployment_url']}")
        lines.append("Approving here records your decision through the same review mechanism the CLI uses, which "
                     "materializes the approval the publish step verifies. It does not publish anything by itself, "
                     "and no URL, job, or workflow can approve on your behalf.")
        buttons = inline_keyboard([[(ACTION_BUTTONS["dm"][1], f"dm:{project_id}:{fingerprint}")]])
        return {"text": "\n".join(lines), "reply_markup": buttons, "code": "DEMO_PROMPT", "project_id": project_id,
                "fingerprint": fingerprint, "gate_code": gate.get("code"), "authorized": bool(gate.get("authorized"))}

    def _record_demo_approval(self, project_id: str, *, note: str | None, user_id: int | None,
                              chat_id: int | None = None) -> dict[str, Any]:
        gate = self._demo_gate(project_id)
        snapshot = self.store.snapshot(project_id)
        project = snapshot.get("project") or {}
        assigned = project.get("assigned_build_id")
        if gate.get("code") == "ALREADY_DEPLOYED":
            return {"text": (f"{project_id} already has a published preview for build {assigned}; there is "
                             "nothing to approve for publishing. The URL is in /package "
                             f"{project_id}, and outreach preparation needs the post-deployment APPROVED decision "
                             "(that is a different action from this one)."),
                    "code": "DEMO_ALREADY_PUBLISHED", "project_id": project_id}
        if project.get("current_stage") != "READY_FOR_REVIEW":
            return {"text": (f"{project_id} is at {project.get('current_stage')}; a preview publish approval is "
                             "only accepted for a build that has finished validating and is waiting at "
                             "READY_FOR_REVIEW. /package shows what is waiting."),
                    "code": "DEMO_NOT_AT_REVIEW_GATE", "project_id": project_id}
        # The fallback note that keeps other decisions attributable is not enough here: a preview approval is the
        # step that lets a build be published, and "approved from Telegram" with nothing about what was read would
        # leave the next reader with a claim and no evidence. So this one command requires the note explicitly.
        if not str(note or "").strip():
            return {"text": ("A preview approval needs your note saying what you looked at, because the durable "
                             "record is what a later reader has to go on. Re-send with --note \"...\"."),
                    "code": "REVIEW_NOTES_REQUIRED", "alert": True}
        notes = _note_for(note, user_id)
        try:
            entry = self.store.record_review(project_id, "APPROVED", notes, reviewer=f"telegram:{user_id}",
                                             build_id=assigned, action="PREVIEW_DEPLOYMENT",
                                             identity_source="telegram_operator_allowlist")
        except ValueError as error:
            # The store's own gate is the authority; its reason is the operator's answer, verbatim but masked.
            return {"text": f"The review mechanism refused this approval: {safe_outbound_text(str(error))}",
                    "code": "DEMO_APPROVAL_REFUSED", "alert": True}
        log_event(severity="INFO", operation="telegram.demo_approval", code="DEMO_APPROVAL_RECORDED",
                  message=f"project {project_id} approved for preview publishing",
                  project_id=project_id, build_id=assigned, chat_id=chat_id, actor=f"telegram:{user_id}")
        fresh = self._demo_gate(project_id)
        lines = [f"Approval recorded for {project_id} (build {assigned}) by telegram:{user_id}.",
                 f"Publish gate now says: {fresh.get('code') or 'UNKNOWN'}.",
                 "Nothing has been published yet. The publish step re-verifies this approval itself, so a queue "
                 "that runs later still needs the artifact to be unchanged."]
        buttons = inline_keyboard([[(ACTION_BUTTONS["pq"][1],
                                    f"pq:{project_id}:{demo_fingerprint(self.store.state['projects'][project_id])}")]])
        lines.append("Queueing the publish job is a separate press; the job will not run while automated dispatch "
                     "is held, and a held attempt is recorded as blocked rather than dropped.")
        return {"text": "\n".join(lines), "reply_markup": buttons, "code": "DEMO_APPROVAL_RECORDED",
                "project_id": project_id, "review_id": (entry or {}).get("review_id"),
                "edit_text": f"Demo approved for {project_id} (build {assigned}) by telegram:{user_id}. "
                             "Publish gate: " + str(fresh.get("code")),
                "edit_markup": {"inline_keyboard": []}}

    def _queue_publish(self, project_id: str, *, user_id: int | None, chat_id: int | None) -> dict[str, Any]:
        """Enqueue the existing DEPLOY job for the approved build. Enqueue only - the worker publishes."""
        gate = self._demo_gate(project_id)
        if not gate.get("authorized") and gate.get("code") != "ALREADY_DEPLOYED":
            return {"text": (f"Refused: the durable gate says {gate.get('code') or 'UNKNOWN'}, so a publish job "
                             "would only fail. /demo shows what the gate needs."),
                    "code": "PUBLISH_NOT_AUTHORIZED", "alert": True}
        project = (self.store.state.get("projects") or {}).get(project_id) or {}
        assigned = project.get("assigned_build_id")
        job_id = f"job-deploy-{project_id}-{assigned}".replace(":", "-")
        try:
            job = self.jobs.create("DEPLOY", job_id=job_id, project_id=project_id, lead_id=project.get("lead_id"),
                                   build_id=assigned, dry_run=False,
                                   idempotency_key=f"telegram-demo-publish:{project_id}:{assigned}",
                                   input={"requested_by": f"telegram:{user_id}",
                                           "approval_source": "telegram_preview_approval",
                                           "note": "queued from Telegram after an explicit preview approval"})
        except ValueError as error:
            return {"text": f"The queue refused: {safe_outbound_text(str(error))}", "code": "PUBLISH_JOB_REFUSED",
                    "alert": True}
        log_event(severity="INFO", operation="telegram.publish_queue", code="PUBLISH_JOB_QUEUED",
                  message=f"DEPLOY job queued for {project_id}", job_id=job.get("job_id"), project_id=project_id,
                  chat_id=chat_id, actor=f"telegram:{user_id}")
        return {"text": (f"Queued {job.get('job_id')} (DEPLOY, build {assigned}, not a dry run). It runs when a "
                         "worker takes it, and only if the approval still matches the artifact; a hold stops it "
                         "and records the reason. /status {job} to watch it.").replace("{job}", str(job.get("job_id"))),
                "code": "PUBLISH_JOB_QUEUED", "job_id": job.get("job_id")}

    # ---------------------------------------------------------------- approved outbound sends

    def _command_outbox(self, argument: str, **_: Any) -> dict[str, Any]:
        """/outbox [limit]: the approved drafts, each with the reason it is or is not sendable now."""
        limit = _int((argument.split() or [None])[0]) or self.row_limit
        try:
            report = self._outbound().outbox(limit=min(max(1, limit), MAX_ROWS))
        except Exception as error:  # noqa: BLE001 - an unreadable outbox is reported, never guessed
            return {"text": f"The outbox could not be read: {safe_outbound_text(str(error))}",
                    "code": "OUTBOX_UNREADABLE"}
        rows = report.get("outbox") or []
        if not rows:
            return {"text": (f"No {report['status']} email draft exists. /review shows drafts awaiting a "
                             "decision; an approval is what puts a draft here."), "code": "OUTBOX_EMPTY"}
        lines = [f"{report['total']} approved draft(s); sending needs an explicit confirmation per draft."]
        for row in rows:
            state = row.get("send_state")
            blockers = [str(item.get("code")) for item in row.get("blockers") or []]
            lines.append(f"• {row['outreach_id']} | {_name({'business_name': row.get('business_name')})} | "
                         f"{mask_address(row.get('recipient') or '')} | {row.get('status')}"
                         + (f" | {state}" if state else "")
                         + (f" | blocked: {', '.join(blockers)}" if blockers else " | ready"))
        if report.get("automation_held"):
            lines.append(f"Hold in effect: {report['gate_reason']}")
        lines.append(f"Sending is {report['sending_state']} on this machine"
                     + ("" if report["sending_enabled"] else " - no message can leave until it is configured, "
                                                               "enabled, and checked")
                     + ("." if report["live_effect_enabled"] else
                        f"; {LIVE_OUTREACH_ENV} is not set, so a confirmed send is still refused"))
        lines.append("Open one to read it and arm a send: /send <outreach_id>. Nothing here sends anything.")
        return {"text": "\n".join(lines), "code": "OUTBOX_LIST", "count": len(rows),
                "sendable": report.get("sendable"), "sending_state": report.get("sending_state")}

    def _command_send(self, argument: str, *, user_id: int | None, chat_id: int | None = None,
                      **_: Any) -> dict[str, Any]:
        """/send <outreach_id> [--dry | --confirm send]. Two acts, never one.

        The first appearance arms it: it shows the recipient, the subject, the fingerprint, and the live
        eligibility answer - never the prospect's message body, which stays out of a chat thread like every other
        outbound body in this bot. The second act (the confirm button or ``--confirm send``) performs one send,
        and only if nothing moved in between.
        """
        tokens = argument.split()
        if not tokens:
            raise TelegramBotError("Name the draft: /send outreach-0001 --dry", code="OUTREACH_ID_REQUIRED")
        outreach_id = tokens[0]
        rest, confirm_from_line = _split_confirm(" ".join(tokens[1:]))
        remainder, note_from_line = _split_note(rest)
        flags, _positional = _parse_flags(remainder)
        note = note_from_line or (flags.get("note") if isinstance(flags.get("note"), str) else None)
        dry_run = "dry" in flags or str(flags.get("mode") or "").lower() == "dry"
        supplied = str(confirm_from_line or flags.get("confirm") or "").strip()
        if supplied.upper() == SEND_CONFIRMATION_WORD:
            return self._perform_send(outreach_id, user_id=user_id, chat_id=chat_id, dry_run=False, note=note)
        answer = self._arm_send(outreach_id, user_id=user_id, dry_run=dry_run)
        if supplied and answer.get("code") == "SEND_ARMED":
            # A word that is nearly right gets its own answer instead of the same prompt an unconfirmed command
            # produces: an operator should not have to guess which half of their command was ignored.
            answer["text"] = (f"That is not the confirmation word this build accepts, so nothing was sent. The word "
                              f"is {SEND_CONFIRMATION_WORD}, exactly - and pressing the button below does the same "
                              "thing as typing it.\n\n" + answer["text"])
            answer["code"] = "CONFIRMATION_MISMATCH"
        return answer

    def _arm_send(self, outreach_id: str, *, user_id: int | None, dry_run: bool = False) -> dict[str, Any]:
        try:
            view = self._outbound().preview(outreach_id)
        except Exception as error:  # noqa: BLE001 - a refusal from the executor is an operator-facing answer
            return self._outbound_refusal(error)
        blockers = [str(item.get("code")) for item in view.get("blockers") or []]
        composition = view.get("composition") or {}
        lines = [
            f"{outreach_id} - {_name({'business_name': view.get('business_name')})} ({view.get('channel')})",
            f"To: {mask_address(view.get('recipient') or '')} · lead {view.get('lead_id')} · project "
            f"{view.get('project_id')}",
            f"Subject: {view.get('subject') or '(none)'}",
            f"Approved: {view.get('reviewed_at') or 'unrecorded'} by {view.get('reviewed_by') or 'unattributed'} "
            f"({view.get('reviewer_identity_source') or 'identity source unrecorded'})",
            f"Content fingerprint: {view.get('content_fingerprint')}",
            "FULL EMAIL PREVIEW — read this exact content before sending:",
            f"To: {view.get('recipient') or '(recipient missing)'}",
            f"Subject: {view.get('subject') or '(none)'}",
            "Body:",
            str(view.get('draft_message') or '(body missing)'),
            f"Preview URL: {view.get('preview_url') or '(preview URL missing)'}",
        ]
        if view.get("status") == "SEND_FAILED":
            lines.append("This draft's last attempt failed; sending again reuses the same approval and counts as "
                         "another attempt.")
        if blockers:
            lines.append("Blocked: " + ", ".join(blockers))
            lines.append("Fix what the block names. A send is never forced past it - an opt-out outranks any "
                         "approval, and a stale approval is not re-used.")
        else:
            lines.append(f"Provider request shape: {composition.get('provider')} "
                         f"{composition.get('endpoint')} ({composition.get('size_bytes')} bytes, "
                         f"threading: {(composition.get('threading') or {}).get('basis')}, "
                         f"List-Unsubscribe: {'set' if composition.get('list_unsubscribe_present') else 'not set'})")
        if dry_run:
            answer = self._perform_send(outreach_id, user_id=user_id, dry_run=True)
            answer["text"] = "\n".join(lines) + "\n\n" + answer["text"]
            answer["preview_sensitive"] = True
            return answer
        if blockers:
            return {"text": "\n".join(lines), "code": "SEND_BLOCKED", "outreach_id": outreach_id,
                    "blockers": blockers}
        buttons = inline_keyboard([[(ACTION_BUTTONS["sc"][1],
                                    f"sc:{outreach_id}:{view.get('content_fingerprint')}"),
                                    ("Preview only (no provider contact)",
                                     f"pv:{outreach_id}:{view.get('content_fingerprint')}")]])
        lines.append("Pressing confirm sends one email to that address. There is no undo, and no retry: an "
                     "unanswered provider response is reconciled, never resent.")
        return {"text": "\n".join(lines), "reply_markup": buttons, "code": "SEND_ARMED",
                "outreach_id": outreach_id, "fingerprint": view.get("content_fingerprint"), "preview_sensitive": True}

    def _perform_send(self, outreach_id: str, *, user_id: int | None, chat_id: int | None = None,
                      dry_run: bool = False, note: str | None = None) -> dict[str, Any]:
        """One attempt, from a button or a typed command alike. The executor holds every gate."""
        try:
            answer = self._outbound().send(outreach_id, actor_id=f"telegram:{user_id}",
                                          identity_source="telegram_operator_allowlist", dry_run=dry_run,
                                          confirmation=SEND_CONFIRMATION_WORD if not dry_run else None,
                                          note=note)
        except Exception as error:  # noqa: BLE001 - OutboundRefusal and ledger faults both land here
            return self._outbound_refusal(error)
        if dry_run:
            composition = answer.get("composition") or {}
            return {"text": (f"Dry run for {outreach_id}: would compose {composition.get('size_bytes')} bytes to "
                             f"{mask_address(str(answer.get('would_contact') or ''))} via "
                             f"{composition.get('provider')}/{composition.get('endpoint')}. No provider was "
                             "contacted, no budget consumed, nothing recorded. Fingerprint "
                             f"{answer.get('content_fingerprint')}."),
                    "code": "SEND_DRY_RUN", "outreach_id": outreach_id}
        state = answer.get("send_state")
        lines = [f"{outreach_id}: {state}" + (f" ({answer.get('code')})" if answer.get("code") else "")]
        if state == "SENT":
            lines.append(f"Result: {answer.get('result')} · provider message id: "
                         f"{answer.get('provider_message_id') or 'none returned'} · verified: "
                         f"{bool(answer.get('provider_verified'))}")
            lines.append(str(answer.get("delivery_note") or ""))
            lines.append("That is the provider's answer to being handed the message. It is not a delivery, read, "
                         "or open receipt, and this system cannot claim one.")
        elif state == "UNCERTAIN":
            lines.append(f"Reason: {answer.get('reason')}")
            lines.append(str(answer.get("next_action") or "reconcile it; do not resend"))
        elif state == "FAILED":
            lines.append(f"The provider refused it: {answer.get('reason')}")
            lines.append("Recorded as a failure: the lead was not reached, the approval still stands, and the "
                         "attempt counted against the contact budget.")
        buttons = (inline_keyboard([[(ACTION_BUTTONS["rc"][1],
                                     f"rc:{outreach_id}:{answer.get('content_fingerprint') or ''}")]])
                   if state == "UNCERTAIN" else None)
        log_event(severity="INFO" if state == "SENT" else "WARNING", operation="telegram.outbound_send",
                  code=f"SEND_{str(state).upper()}", message=f"outreach {outreach_id} send attempt: {state}",
                  outreach_id=outreach_id, lead_id=answer.get("lead_id"), chat_id=chat_id,
                  actor=f"telegram:{user_id}")
        return {"text": "\n".join(piece for piece in lines if piece), "code": f"SEND_{str(state).upper()}",
                "outreach_id": outreach_id, "reply_markup": buttons,
                "edit_text": f"{outreach_id}: {state}" + (f" ({answer.get('result')})" if answer.get("result")
                                                           else ""), "edit_markup": {"inline_keyboard": []}}

    def _outbound_refusal(self, error: BaseException) -> dict[str, Any]:
        """Turn any executor or ledger refusal into an operator answer. Nothing here retries anything."""
        code = str(getattr(error, "code", "") or "SEND_REFUSED")
        detail = safe_outbound_text(str(error))
        next_action = getattr(error, "next_action", None)
        text = f"Refused ({code}): {detail}" + (f"\nNext: {next_action}" if next_action else "")
        return {"text": text[:1800], "code": code, "alert": True}

    def _command_sent(self, argument: str, **_: Any) -> dict[str, Any]:
        """/sent <outreach_id>: what the provider said about one draft, and what that does not prove."""
        tokens = argument.split()
        if not tokens:
            raise TelegramBotError("Name the draft: /sent outreach-0001", code="OUTREACH_ID_REQUIRED")
        try:
            report = self._outbound().status(tokens[0])
        except Exception as error:  # noqa: BLE001
            return self._outbound_refusal(error)
        row = (report.get("sends") or [{}])[0]
        lines = [f"{row.get('outreach_id')}: {row.get('status')}"
                 + (f" / send_state {row.get('send_state')}" if row.get("send_state") else ""),
                 f"result: {row.get('result') or 'none'} · provider_verified: {bool(row.get('verified'))} · "
                 f"delivery_status: {row.get('delivery_status') or 'unrecorded'} · attempts: "
                 f"{row.get('send_attempts')}",
                 f"provider message id: {row.get('provider_message_id') or 'none'} · asked by "
                 f"{row.get('actor') or 'unrecorded'} ({row.get('identity_source') or 'no source'})"]
        if row.get("detail"):
            lines.append(f"detail: {str(row['detail'])[:220]}")
        lines.append("An id that was read back proves the provider accepted this message. Nothing here can prove "
                     "delivery, reading, or a reply - and no elapsed time will ever be reported as one of those.")
        return {"text": "\n".join(lines), "code": "SEND_STATUS", "outreach_id": row.get("outreach_id"),
                "verified": bool(row.get("verified")), "send_state": row.get("send_state")}

    def _command_reconcile(self, argument: str, **_: Any) -> dict[str, Any]:
        """/reconcile [outreach_id]: look in the account for a send that got no definitive answer."""
        target = (argument.split() or [None])[0]
        try:
            report = self._outbound().reconcile(target) if target else self._outbound().reconcile()
        except Exception as error:  # noqa: BLE001
            return self._outbound_refusal(error)
        rows = report.get("rows") or []
        if not rows:
            return {"text": "No send attempt is unresolved, so there is nothing to reconcile. That is the good "
                            "answer; it does not mean every earlier send worked.", "code": "RECONCILE_EMPTY"}
        lines = [f"{len(rows)} unresolved attempt(s) looked at; {len(report.get('reconciled') or [])} resolved."]
        for row in rows:
            lines.append(f"• {row.get('outreach_id')} | {row.get('send_state')} | "
                         + (f"resolved as {row.get('outcome')}" if row.get("resolved")
                            else f"{row.get('code')}: {str(row.get('detail') or row.get('next_action'))[:160]}"))
        lines.append("A resolved attempt is recorded through the same ledger path as a send; an unresolved one "
                     "stays unresolved and keeps blocking that draft. Nothing here ever resends.")
        return {"text": "\n".join(lines), "code": "RECONCILE_REPORT",
                "resolved": len(report.get("reconciled") or []), "unresolved": len(report.get("unresolved") or [])}

    def _command_optout(self, argument: str, *, user_id: int | None, **_: Any) -> dict[str, Any]:
        """/optout <lead_id> --note "<what they said>" [--confirm <code>].

        A prospect's request not to be contacted outranks every approval in this system, so recording one takes
        one confirmation and no reason at all - but it takes *evidence*, because the ledger's opt-out row is what
        a later reader audits. Revoking one is not offered here at all: that belongs to the CLI, with a named
        reason, so a chat message can never re-open a blocked contact.
        """
        tokens = argument.split()
        if not tokens:
            raise TelegramBotError("Name the lead: /optout lead-0002 --note \"asked us to stop emailing\"",
                                   code="LEAD_ID_REQUIRED")
        lead_id = tokens[0]
        rest, confirm_from_line = _split_confirm(" ".join(tokens[1:]))
        remainder, note = _split_note(rest)
        flags, _positional = _parse_flags(remainder)
        confirm = str(confirm_from_line or flags.get("confirm") or "").strip()
        if not note:
            return {"text": ("An opt-out needs the words that say so, because it changes what the pipeline is "
                             'allowed to do. Re-send with --note "what they asked for".'),
                    "code": "EVIDENCE_REQUIRED", "alert": True}
        fingerprint = optout_fingerprint(lead_id)
        if confirm != fingerprint:
            return {"text": ("This blocks every open follow-up for that lead and stops future contact. Re-send "
                             f"with --confirm {fingerprint} to record it.\n\nLead {lead_id}: "
                             f"{_name((self.store.state.get('leads') or {}).get(lead_id) or {})}"),
                    "code": "CONFIRMATION_REQUIRED"}
        try:
            block = self.ledger.record_opt_out(lead_id, channel="EMAIL",
                                               reason="recorded by the operator from Telegram",
                                               evidence_note=f"telegram:{user_id}: {note}"[:400])
        except LedgerError as error:
            return {"text": f"The ledger refused that ({getattr(error, 'code', 'LEDGER_ERROR')}): {error}",
                    "code": getattr(error, "code", "LEDGER_ERROR"), "alert": True}
        except ValueError as error:
            return {"text": f"Refused: {safe_outbound_text(str(error))}", "code": "OPERATION_REFUSED",
                    "alert": True}
        log_event(severity="INFO", operation="telegram.opt_out", code="OPT_OUT_RECORDED",
                  message=f"opt-out recorded for {lead_id}", lead_id=lead_id, actor=f"telegram:{user_id}")
        return {"text": (f"Opt-out recorded for {lead_id} ({block.get('block_id')}). Sends and follow-ups for "
                         "that lead and channel are blocked from now on; the block is the ledger's, not the bot's, "
                         "so every path honours it. Lifting it takes a named reason in the CLI."),
                "code": "OPT_OUT_RECORDED", "lead_id": lead_id, "block_id": block.get("block_id")}

    def _command_social(self, argument: str, **_: Any) -> dict[str, Any]:
        """/social: list verified channels and buffer draft queue metrics."""
        from social.models import VERIFIED_CHANNELS
        service = self._social()
        drafts = service.list_drafts(limit=100)
        by_status: dict[str, int] = {}
        for d in drafts:
            by_status[d.status] = by_status.get(d.status, 0) + 1

        lines = [
            f"Social Media Engine: {len(drafts)} draft(s) recorded.",
            "Queue status: " + (", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) if by_status else "empty"),
            "\nVerified Buffer Channels (n8n zAd3P31ZrTm9Ax94):",
        ]
        for name, spec in VERIFIED_CHANNELS.items():
            if name == "x":
                continue
            media_note = " (media required)" if spec.requires_media else ""
            lines.append(f"• {name.upper()}: {spec.name} (id {spec.channel_id[:8]}..., max {spec.max_length} chars{media_note})")
        lines.append("\nUse /post new <channel> <topic> to generate a draft or /post list to review.")
        return {"text": "\n".join(lines), "code": "SOCIAL_STATUS", "count": len(drafts), "by_status": by_status}

    def _command_post(self, argument: str, *, user_id: int | None, chat_id: int, **_: Any) -> dict[str, Any]:
        """/post [list | new <channel> <topic> | view <id> | approve <id> | reject <id>]"""
        tokens = argument.strip().split()
        subcmd = tokens[0].lower() if tokens else "list"
        service = self._social()

        if subcmd == "list":
            channel = tokens[1].lower() if len(tokens) > 1 and tokens[1].lower() in {"twitter", "x", "linkedin", "instagram"} else None
            drafts = service.list_drafts(channel=channel, limit=self.row_limit)
            if not drafts:
                return {"text": "No social media drafts recorded. Generate one with /post new <channel> <topic>.", "code": "POSTS_EMPTY"}
            lines = [f"{len(drafts)} draft(s) listed:"]
            for d in drafts:
                lines.append(f"• {d.id} | {d.channel.upper()} | {d.status} | score: {d.brand_voice_score:.2f} | {d.topic[:30]}")
            lines.append("Use /post view <id> to inspect or /post approve <id> to schedule.")
            return {"text": "\n".join(lines), "code": "POSTS_LIST", "count": len(drafts)}

        elif subcmd == "new":
            if len(tokens) < 3:
                return {"text": "Usage: /post new <twitter|linkedin|instagram> <topic>", "code": "POST_ARGUMENTS_REQUIRED"}
            channel = tokens[1].lower()
            topic = " ".join(tokens[2:]).strip()
            try:
                draft = service.create_draft(topic=topic, channel=channel)
            except ValueError as error:
                return {"text": f"Refused: {safe_outbound_text(str(error))}", "code": "POST_GENERATION_FAILED"}
            lines = [
                f"Generated draft {draft.id} for {draft.channel.upper()} (score: {draft.brand_voice_score:.2f}):",
                f"\n\"{draft.caption}\"\n",
                f"Length: {len(draft.caption)} chars | Status: {draft.status}",
                f"Review with /post view {draft.id} or approve with /post approve {draft.id}",
            ]
            return {"text": "\n".join(lines), "code": "POST_GENERATED", "draft_id": draft.id}

        elif subcmd == "view":
            if len(tokens) < 2:
                return {"text": "Usage: /post view <draft_id>", "code": "DRAFT_ID_REQUIRED"}
            draft_id = tokens[1]
            draft = service.get_draft(draft_id)
            if not draft:
                return {"text": f"Draft '{draft_id}' not found.", "code": "DRAFT_NOT_FOUND"}
            lines = [
                f"Draft {draft.id} [{draft.channel.upper()}] - {draft.status}",
                f"Topic: {draft.topic} | Archetype: {draft.archetype}",
                f"Brand Voice Score: {draft.brand_voice_score:.2f} | Chars: {len(draft.caption)}",
            ]
            if draft.media_url:
                lines.append(f"Media URL: {draft.media_url}")
            elif draft.media_prompt:
                lines.append(f"Media Prompt: {draft.media_prompt}")
            lines.append(f"\n--- Caption ---\n{draft.caption}")
            if draft.operator_note:
                lines.append(f"\nNote: {draft.operator_note}")
            return {"text": "\n".join(lines), "code": "POST_VIEW", "draft_id": draft.id}

        elif subcmd == "approve":
            if len(tokens) < 2:
                return {"text": "Usage: /post approve <draft_id> [--note \"...\"]", "code": "DRAFT_ID_REQUIRED"}
            draft_id = tokens[1]
            note = " ".join(tokens[2:]).strip() if len(tokens) > 2 else f"Approved from Telegram by user {user_id}"
            try:
                draft = service.approve_draft(draft_id, operator_note=note)
            except Exception as error:
                code = getattr(error, "code", "APPROVE_REFUSED")
                return {"text": f"Approval refused ({code}): {safe_outbound_text(str(error))}", "code": code}
            lines = [
                f"Draft {draft.id} APPROVED for Buffer scheduling.",
                f"Channel: {draft.channel.upper()} | Target: {draft.channel}",
                f"Note: {draft.operator_note}",
                "Status in action_approvals: ready for execution by n8n Buffer Draft Executor (zAd3P31ZrTm9Ax94).",
            ]
            return {"text": "\n".join(lines), "code": "POST_APPROVED", "draft_id": draft.id}

        elif subcmd == "reject":
            if len(tokens) < 2:
                return {"text": "Usage: /post reject <draft_id> [reason]", "code": "DRAFT_ID_REQUIRED"}
            draft_id = tokens[1]
            reason = " ".join(tokens[2:]).strip() if len(tokens) > 2 else f"Rejected from Telegram by user {user_id}"
            try:
                draft = service.reject_draft(draft_id, reason=reason)
            except Exception as error:
                code = getattr(error, "code", "REJECT_REFUSED")
                return {"text": f"Rejection refused ({code}): {safe_outbound_text(str(error))}", "code": code}
            return {"text": f"Draft {draft.id} marked REJECTED. Reason: {reason}", "code": "POST_REJECTED", "draft_id": draft.id}

        return {"text": f"Unknown subcommand '{subcmd}'. Usage: /post [list | new <channel> <topic> | view <id> | approve <id> | reject <id>]", "code": "POST_UNKNOWN_SUBCOMMAND"}

    def _command_daily(self, argument: str, **kwargs: Any) -> dict[str, Any]:
        """/daily: alias for /briefing."""
        return self._command_briefing(argument, **kwargs)

    def _command_briefing(self, argument: str, *, chat_id: int, **_: Any) -> dict[str, Any]:
        """/briefing: generate and deliver authoritative daily business briefing."""
        service = self._briefing()
        result = service.generate_briefing()
        chunks = result["chunks"]
        data = result["data"]

        # If connected to a real live Telegram API and chat_id is present, send intermediate chunks
        if self.api is not None and chat_id and len(chunks) > 1:
            for chunk in chunks[:-1]:
                try:
                    self.api.send_message(chat_id, chunk)
                except Exception:
                    pass

        return {
            "text": chunks[-1] if (len(chunks) > 1 and self.api is not None and chat_id) else result["text"],
            "code": "DAILY_BRIEFING",
            "chunks": chunks,
            "active_leads": data.get("active_lead_count", 0),
            "pipeline_value": data.get("pipeline_value", 0.0),
            "overdue_tasks": len(data.get("overdue_tasks", [])),
        }

    def _action_callback(self, action: str, identifier: str, fingerprint: str, *, user_id: int | None,
                         chat_id: int | None, message_id: int | None, callback_query_id: str,
                         prefix: str) -> dict[str, Any]:
        """One press, one action, re-authorized and re-checked here.

        ``handle_callback`` already refused an unlisted account for this update; a press is not treated as
        inherited trust. The fingerprint is re-derived from the record as it is *now*, so a keyboard left in a
        thread from yesterday cannot approve or send anything whose content has moved.
        """
        if not identifier:
            self._ack(callback_query_id, text="That button did not name a record.", alert=True)
            return {"handled": False, "code": "CALLBACK_UNBOUND"}

        def result(answer: dict[str, Any]) -> dict[str, Any]:
            self._ack(callback_query_id, text=str(answer.get("text") or answer.get("notice") or "")[:200],
                      alert=bool(answer.get("alert")))
            return {"handled": True, "code": answer.get("code") or "ACTION_RECORDED",
                    "notice": answer.get("text") or answer.get("notice"), "reply_markup": answer.get("reply_markup"),
                    "edit_text": answer.get("edit_text"), "edit_markup": answer.get("edit_markup"),
                    "action": action, "identifier": identifier}

        if action == "demo_approve":
            project = (self.store.state.get("projects") or {}).get(identifier)
            if not isinstance(project, dict) or demo_fingerprint(project) != fingerprint:
                return result({"code": "DEMO_STALE", "alert": True,
                               "text": ("That project changed since this prompt was drawn, so the press records "
                                        "nothing. /demo " + identifier + " to decide on the current state.")})
            return result(self._record_demo_approval(identifier, note=None, user_id=user_id, chat_id=chat_id))
        if action == "publish_queue":
            project = (self.store.state.get("projects") or {}).get(identifier)
            if not isinstance(project, dict) or demo_fingerprint(project) != fingerprint:
                return result({"code": "DEMO_STALE", "alert": True,
                               "text": "The project state moved; nothing was queued. /demo " + identifier
                                       + " to see the gate again."})
            return result(self._queue_publish(identifier, user_id=user_id, chat_id=chat_id))
        record = (self.store.state.get("outreach") or {}).get(identifier)
        if not isinstance(record, dict):
            return result({"code": "OUTREACH_NOT_FOUND", "alert": True,
                           "text": f"No outreach record '{identifier}'. Nothing changed."})
        from .outbound import content_fingerprint

        if content_fingerprint(record) != fingerprint:
            return result({"code": "APPROVAL_STALE", "alert": True,
                           "text": (f"{identifier} changed since that screen was drawn, so this press acts on "
                                    f"nothing. Current fingerprint {content_fingerprint(record)}; /send "
                                    f"{identifier} to look again.")})
        if action in {"send_preview", "send_arm"}:
            view = self._outbound().preview(identifier)
            text = _send_prompt_text(view, dry=True)
            blockers = [str(item.get("code")) for item in view.get("blockers") or []]
            if blockers:
                return result({"code": "SEND_BLOCKED", "alert": True,
                               "text": text + "\nNothing was sent and nothing will be from this keyboard."})
            if action == "send_arm":
                armed = self._arm_send(identifier, user_id=user_id)
                return {"handled": True, "code": str(armed.get("code") or "SEND_ARMED"),
                        "notice": armed.get("text"), "reply_markup": armed.get("reply_markup"),
                        "edit_text": armed.get("text"), "edit_markup": armed.get("reply_markup"),
                        "action": action, "identifier": identifier}
            buttons = inline_keyboard([[("Send this (shows the full check first)",
                                        f"se:{identifier}:{view.get('content_fingerprint')}")]])
            return {"handled": True, "code": "SEND_PREVIEW", "notice": text, "reply_markup": buttons,
                    "edit_text": text, "edit_markup": buttons, "action": action, "identifier": identifier}
        if action == "send_confirm":
            return result(self._perform_send(identifier, user_id=user_id, chat_id=chat_id, dry_run=False))
        if action == "send_reconcile":
            answer = self._outbound().reconcile(identifier)
            row = (answer.get("rows") or [{}])[0]
            return result({"code": str(row.get("code") or ("RECONCILED" if row.get("resolved") else "RECONCILE_FAILED")),
                           "text": (f"{identifier}: " + (f"resolved as {row.get('outcome')} from "
                                                          f"{row.get('evidence')}" if row.get("resolved") else
                                                          f"{row.get('code')}: {row.get('next_action') or row.get('detail')}"))})
        return result({"code": "ACTION_UNSUPPORTED", "alert": True,
                       "text": f"I do not know the action '{action}'."})

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


def demo_fingerprint(record: dict[str, Any]) -> str:
    """The state a preview approval is about: build, validation, deployment, stage, and prior approval.

    Same purpose as :func:`draft_fingerprint` one level up the pipeline - make staleness *decidable* - and the
    same non-secret: it is a checksum of a record the operator was shown, not a token. A press whose code does not
    match means something moved under it, which is the only way a chat thread can turn into a wrong publish.
    """
    material = "\x1f".join(str((record or {}).get(key) or "") for key in
                           ("project_id", "lead_id", "assigned_build_id", "latest_validation_id",
                            "latest_deployment_id", "current_stage", "review_status", "approved_build_id",
                            "approved_action"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]


def optout_fingerprint(lead_id: str) -> str:
    """A short confirmation code bound to one lead id, so an opt-out is aimed at a named business."""
    return hashlib.sha256(f"opt-out\x1f{lead_id}".encode("utf-8")).hexdigest()[:8]


def decision_status(decision: str) -> str:
    return {"APPROVED": "READY_TO_SEND", "REJECTED": "REJECTED", "CHANGES_REQUESTED": "CHANGES_REQUESTED"}[decision]


def _send_prompt_text(view: dict[str, Any], *, dry: bool) -> str:
    """The one-draft summary a button shows. No body: contact details stay out of the chat thread."""
    lines = [f"{view.get('outreach_id')} - {view.get('business_name') or 'unknown business'}",
             f"To: {mask_address(view.get('recipient') or '')}",
             f"Subject: {view.get('subject') or '(none)'} · approved {view.get('reviewed_at') or 'unrecorded'}",
             f"Fingerprint: {view.get('content_fingerprint')}"]
    blockers = [str(item.get("code")) for item in view.get("blockers") or []]
    if blockers:
        lines.append("Blocked: " + ", ".join(blockers))
    else:
        lines.append("Sendable." + (" Nothing was sent by showing you this." if dry else
                                   " Confirm to send once."))
    if dry:
        composition = view.get("composition") or {}
        lines.append(f"Would compose {composition.get('size_bytes')} bytes for "
                     f"{composition.get('provider')}/{composition.get('endpoint')}; no provider contact.")
    return "\n".join(piece for piece in lines if piece)


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


def _split_confirm(text: str) -> tuple[str, str]:
    """Pull ``--confirm <code>`` out of a line before :func:`_split_note` swallows it into the note.

    The order the help text documents - ``/demo p --note "..." --confirm abc`` - otherwise lands the confirmation
    code inside the free-text note, because a note deliberately runs to the end of the line. Splitting the
    confirmation out first keeps both readable and neither ambiguous.
    """
    match = re.search(r"--confirm(?:=|\s+)(\S+)", str(text or ""))
    if not match:
        return str(text or "").strip(), ""
    remainder = (str(text[:match.start()]) + " " + str(text[match.end():])).strip()
    return remainder, match.group(1).strip()


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
