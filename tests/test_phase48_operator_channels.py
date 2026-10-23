"""Phase 48: the Telegram operator console, the automation hold, and the first Gmail inbound adapter.

Everything here runs offline. Telegram and Gmail are represented by fakes that speak the same shapes as the real
APIs, and the only HTTP server involved is this repository's own loopback handler. No message is sent to a
business, no mailbox is read, and no credential exists in these fixtures: the tokens below are literals that
prove *which* value was used, never a working one.

The tests are written against the two properties this phase could easily get wrong:

* the bot must refuse before it acts, and must never turn a message into an approval; and
* an inbound email must be handled by the Phase 47 intake - so a duplicate, an unmatchable message, an opt-out,
  and a failed write behave exactly as they do when an operator types them in by hand.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from orchestration.controls import (HOLD_CODE, blocked_by_hold, hold_view, release_hold, set_hold)
from orchestration.email_inbound import EmailInboundPoller
from orchestration.jobs import JobService
from orchestration.telegram_bot import (COMMANDS, TelegramOperatorBot, draft_fingerprint,
                                              load_alert_seen, load_attention_seen)
from orchestration.telegram_limits import attention_identity, mask_address, safe_outbound_text
from tracking.store import TrackingStore
from integrations.gmail import (GmailError, gmail_config, header_map, internal_date_iso,
                                    message_to_inbound_event)
from integrations.http import ProviderError, check_endpoint
from integrations.telegram import (MAX_CALLBACK_DATA_BYTES, TelegramBotApi, clip_message, encode_callback_data,
                                        inline_keyboard, telegram_config)

_BUTTON_PREFIX = {"approve": "ap", "reject": "rj", "changes": "ch"}

OPERATOR = 1001
OTHER = 2002
CHAT = -100


def gmail_message(message_id="m1", *, thread="t77", sender="Aisha Bello <aisha@pitstop.ng>",
                  subject="Re: your website preview", snippet="Yes please, send me the details.",
                  internal_date="1760000000", in_reply_to=None):
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    return {"id": message_id, "threadId": thread, "snippet": snippet, "internalDate": internal_date,
            "payload": {"headers": headers}}


class FakeTelegramApi:
    """The Bot API surface the bot uses, with the failure modes a real one has and none of its network."""

    def __init__(self, *, updates=(), fail_on=None, send_error=None, max_offset=None):
        self.sent, self.edited, self.acked = [], [], []
        self._updates = list(updates)
        self.fail_on = set(fail_on or ())
        self.send_error = send_error

    def get_updates(self, *, offset=None, limit=25, timeout=None):
        if "getUpdates" in self.fail_on:
            raise ProviderError("getUpdates timed out", code="TELEGRAM_UNRESPONSIVE", retryable=True)
        return [item for item in self._updates if offset is None or int(item["update_id"]) >= int(offset)]

    def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None, disable_notification=False):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent), "clipped": len(text) > 4096}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                            "reply_markup": reply_markup})
        return {"edited": True, "clipped": False}

    def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.acked.append({"id": callback_query_id, "text": text, "show_alert": show_alert})
        return True


class BotTestCase(unittest.TestCase):
    """A store with one fictional lead, project, and approved-able draft, and a bot that cannot reach a network."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.store = TrackingStore(self.root / "state.json")
        self.lead = self.store.create_lead({
            "business_name": "Fictional Pitstop Test", "business_type": "Restaurants", "city": "Port Harcourt",
            "country": "Nigeria", "data_classification": "TEST_FIXTURE", "qualification_status": "QUALIFIED",
            "website_status": "WEBSITE_NEEDS_IMPROVEMENT", "email": "aisha@pitstop.ng",
        })
        self.project = self.store.create_project(self.lead["lead_id"])
        self.api = FakeTelegramApi()
        self.bot = self.make_bot()

    def make_bot(self, api=None, **overrides):
        settings = {"allowed_user_ids": {OPERATOR}, "allowed_chat_ids": {CHAT}}
        settings.update(overrides)
        return TelegramOperatorBot(self.api if api is None else api, store=self.store, **settings)

    def command(self, text, *, user_id=OPERATOR, chat_id=CHAT, api=None):
        bot = self.bot if api is None else api
        return bot.dispatch(text, user_id=user_id, chat_id=chat_id, deliver=False)

    def callback(self, outreach_id, action, fingerprint, *, update_id=700, message_id=1, user_id=OPERATOR):
        """A callback_query update as Telegram delivers it - the path the buttons actually take."""
        return {"update_id": update_id, "callback_query": {
            "id": f"cq-{update_id}", "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": CHAT}},
            "data": f"{_BUTTON_PREFIX[action]}:{outreach_id}:{fingerprint}"}}

    def draft(self, status="AWAITING_REVIEW", *, channel="EMAIL"):
        outreach_id = f"outreach-{len(self.store.state['outreach']) + 1:04d}"
        self.store.state["outreach"][outreach_id] = {
            "outreach_id": outreach_id, "project_id": self.project["project_id"], "lead_id": self.lead["lead_id"],
            "channel": channel, "status": status, "recipient": "aisha@pitstop.ng",
            "subject": "Your website preview is ready",
            "draft_message": "Hi Fictional Pitstop Test, a concept is ready: https://preview.example/demo",
            "preview_url": "https://preview.example/demo", "generated_at": "2026-10-16T00:00:00Z",
            "review_notes": None, "build_id": None, "deployment_id": None,
        }
        self.store._save()
        return outreach_id

    def poller(self, messages, **kwargs):
        return EmailInboundPoller(self.fake_gmail(messages), store=self.store, **kwargs)

    def fake_gmail(self, messages=(), *, list_error=None, history=None):
        return FakeGmailClient(list(messages), list_error=list_error, history=history or {})


class FakeGmailClient:
    def __init__(self, messages, *, list_error=None, history=None, unreadable=()):
        self.messages = {item["id"]: item for item in messages}
        self.order = [item["id"] for item in messages]
        self.list_error = list_error
        self.history = history or {}
        self.unreadable = set(unreadable)
        self.calls = 0
        self.errors = 0
        self.list_arguments = []

    def list_messages(self, query, *, max_results=25, page_token=None, include_spam_trash=False):
        self.calls += 1
        self.list_arguments.append({"query": query, "max_results": max_results, "page_token": page_token})
        if self.list_error is not None:
            raise self.list_error
        return {"messages": [{"id": item} for item in self.order[:max_results]], "next_page_token": None,
                "resultSizeEstimate": len(self.order)}

    def get_message(self, message_id, *, message_format="metadata"):
        self.calls += 1
        if message_id in self.unreadable:
            raise GmailError("gone", code="GMAIL_REQUEST_REFUSED", status=404)
        if message_id not in self.messages:
            raise GmailError("no such message", code="GMAIL_MESSAGE_MALFORMED")
        return self.messages[message_id]

    def get_history(self, history_id, *, label_id="INBOX", max_results=100):
        self.calls += 1
        if isinstance(self.history, Exception):
            raise self.history
        return self.history.get(int(history_id), {"history": [], "history_id": int(history_id) + 1})

    def latest_history_id(self):
        return 500

    def status(self):
        return {"account": "me", "scope": "https://www.googleapis.com/auth/gmail.readonly", "calls": self.calls,
                "errors": self.errors, "token_minted_at": None,
                "explicitly_absent": ["messages.send", "messages.delete", "messages.modify"]}


class AuthorizationTests(BotTestCase):
    def test_an_unlisted_account_is_refused_before_any_service_is_touched(self) -> None:
        answer = self.bot.handle_update({"update_id": 1, "message": {
            "from": {"id": OTHER}, "chat": {"id": CHAT}, "text": "/leads"}})
        self.assertEqual(answer["code"], "AUTHORIZATION_REFUSED")
        self.assertEqual(answer["handled"], False)
        self.assertNotIn("Fictional", json.dumps(answer))
        # The refusal is fixed text: it must not even confirm that a lead list exists.
        self.assertEqual(answer["reply"], "This bot does not accept instructions from this account.")

    def test_an_empty_allow_list_authorizes_nobody(self) -> None:
        bot = TelegramOperatorBot(None, store=self.store, allowed_user_ids=(), allowed_chat_ids=())
        answer = bot.dispatch("/help", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(answer["code"], "AUTHORIZATION_REFUSED")

    def test_a_listed_user_in_an_unlisted_chat_is_refused_and_vice_versa(self) -> None:
        self.assertEqual(self.bot.handle_update({"update_id": 2, "message": {
            "from": {"id": OPERATOR}, "chat": {"id": -999}, "text": "/status"}})["code"], "AUTHORIZATION_REFUSED")
        self.assertEqual(self.bot.handle_update({"update_id": 3, "message": {
            "from": {"id": OTHER}, "chat": {"id": CHAT}, "text": "/status"}})["code"], "AUTHORIZATION_REFUSED")
        self.assertEqual(self.command("/status")["code"], "QUEUE_SUMMARY")

    def test_free_text_is_never_treated_as_an_approval(self) -> None:
        outreach_id = self.draft()
        for text in ("approved", "yes go ahead and send it", "/approve", "👍"):
            answer = self.bot.handle_update({"update_id": 7, "message": {
                "from": {"id": OPERATOR}, "chat": {"id": CHAT}, "text": text}})
            self.assertIn(answer["code"], {"MESSAGE_NOT_A_COMMAND", "OUTREACH_ID_REQUIRED"}, text)
            self.assertEqual(self.store.state["outreach"][outreach_id]["status"], "AWAITING_REVIEW", text)

    def test_rate_limiting_refuses_a_burst_without_touching_the_pipeline(self) -> None:
        bot = self.make_bot()
        codes = [bot.dispatch("/status", user_id=OPERATOR, chat_id=CHAT, deliver=False)["code"]
                 for _ in range(30)]
        self.assertIn("RATE_LIMITED", codes)
        self.assertLess(codes.index("RATE_LIMITED"), 30)

    def test_a_callback_from_an_unlisted_user_records_nothing(self) -> None:
        outreach_id = self.draft()
        fingerprint = draft_fingerprint(self.store.state["outreach"][outreach_id])
        answer = self.bot.handle_callback(f"ap:{outreach_id}:{fingerprint}", user_id=OTHER, chat_id=CHAT,
                                         callback_query_id="cq1")
        self.assertEqual(answer["code"], "AUTHORIZATION_REFUSED")
        self.assertEqual(self.store.state["outreach"][outreach_id]["status"], "AWAITING_REVIEW")


class DiscoveryCommandTests(BotTestCase):
    def test_location_and_category_must_both_be_stated(self) -> None:
        self.assertEqual(self.command("/find")["code"], "FIND_ARGUMENTS_REQUIRED")
        self.assertEqual(self.command("/find restaurants")["code"], "FIND_ARGUMENTS_REQUIRED")
        # Two bare words are the one genuinely ambiguous case, and the bot refuses the coin flip.
        self.assertEqual(self.command("/find Port Harcourt")["code"], "FIND_ARGUMENT_AMBIGUOUS")
        self.assertEqual(self.command("/find Lagos hotels")["code"], "FIND_ARGUMENT_AMBIGUOUS")
        self.assertEqual(self.command("/find Lagos hotels 3")["code"], "FIND_SOURCE_REQUIRED")
        answer = self.command("/find Port Harcourt --category restaurants --live")
        self.assertEqual(answer["code"], "DISCOVERY_REQUESTED")
        job = self.store.state["jobs"][answer["job_id"]]
        self.assertEqual((job["input"]["city"], job["input"]["niche"]),
                         ("Port Harcourt", "restaurants"), "--category removes the guess entirely")

    def test_no_source_named_means_no_request_is_created(self) -> None:
        answer = self.command("/find Port Harcourt restaurants 3")
        self.assertEqual(answer["code"], "FIND_SOURCE_REQUIRED")
        self.assertEqual(self.store.state["jobs"], {}, "a refusal must not leave a job behind")

    def test_an_explicit_live_request_creates_one_bounded_scout_job(self) -> None:
        answer = self.command("/find Port Harcourt restaurants 4 --live")
        self.assertEqual(answer["code"], "DISCOVERY_REQUESTED")
        job = self.store.state["jobs"][answer["job_id"]]
        self.assertEqual(job["job_type"], "SCOUT")
        self.assertFalse(job["dry_run"], "an explicit --live request is not a rehearsal")
        self.assertEqual(job["input"]["lead_limit"], 4)
        self.assertEqual(job["input"]["city"], "Port Harcourt")
        self.assertEqual(job["input"]["niche"], "restaurants")
        self.assertTrue(job["input"]["approve_live_lookups"], "the operator's own command is the human approval")

    def test_the_count_is_bounded_and_a_bare_number_is_not_a_location(self) -> None:
        self.assertEqual(self.command("/find Lagos hotels 500 --live")["code"], "FIND_COUNT_OUT_OF_RANGE")
        self.assertEqual(self.command("/find Lagos hotels 0 --live")["code"], "FIND_COUNT_OUT_OF_RANGE")
        self.assertEqual(self.command("/find 123 restaurants 45 --live")["code"], "FIND_ARGUMENT_UNPARSABLE",
                         "an id or number in the place of a city is a misread command, not a search term")

    def test_two_sources_are_ambiguous_and_a_repeated_request_is_the_same_job(self) -> None:
        self.assertEqual(self.command("/find Lagos hotels 3 --live --source-file x.json")["code"],
                         "FIND_SOURCE_AMBIGUOUS")
        first = self.command("/find Lagos hotels 2 --live")["job_id"]
        again = self.command("/find Lagos hotels 2 --live")
        self.assertEqual(again["job_id"], first, "a double tap must not queue two discoveries")
        self.assertEqual(len(self.store.state["jobs"]), 1)

    def test_a_fixture_source_is_accepted_without_live_lookup_approval(self) -> None:
        answer = self.command("/find Lagos hotels 2 --source-file tests/fixtures/leads.json")
        job = self.store.state["jobs"][answer["job_id"]]
        self.assertEqual(job["input"]["source"], "fixture")
        self.assertNotIn("approve_live_lookups", job["input"],
                         "the bot must not hand out the live-lookup approval nobody asked for")


class ReadCommandTests(BotTestCase):
    def test_status_names_the_hold_instead_of_the_fault(self) -> None:
        self.command("/find Lagos hotels 2 --live")
        job_id = next(iter(self.store.state["jobs"]))
        set_hold(self.store, actor="test", reason="quiet")
        answer = self.command(f"/status {job_id}")
        self.assertEqual(answer["code"], "JOB_STATUS")
        # `run-job` is what turns a hold into a BLOCKED row; the status command only reports what is recorded.
        self.assertIn("PENDING", answer["reply"])
        answer = self.command("/status job-does-not-exist")
        self.assertEqual(answer["code"], "JOB_NOT_FOUND")

    def test_lists_are_bounded_and_mention_where_the_rest_lives(self) -> None:
        for index in range(12):
            self.store.create_lead({"business_name": f"Fictional Store {index:02d}", "business_type": "Restaurants",
                                   "city": "Lagos", "country": "Nigeria", "data_classification": "TEST_FIXTURE",
                                   "qualification_status": "QUALIFIED"})
        answer = self.command("/leads")
        self.assertEqual(answer["count"], self.bot.row_limit)
        self.assertLessEqual(answer["reply"].count("\n• "), self.bot.row_limit)
        self.assertIn("showing the newest", safe_outbound_text(answer["reply"]))
        self.assertIn("/leads ", answer["reply"], "the footer names the narrower command to run next")

    def test_inbox_followups_proposals_and_attention_read_the_existing_surfaces(self) -> None:
        from tracking.engagement import EngagementLedger

        ledger = EngagementLedger(self.store, source="test")
        ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="asked for a price",
                              quote="how much?", received_at="2026-10-15T09:00:00Z")
        # Already due, deliberately. ``allow_past_due`` is the ledger's own parameter for a schedule recorded
        # after the fact, so the fixture states a *condition* instead of betting on the wall clock: this line
        # used a fixed timestamp a few hours ahead of the day it was written, and it began to fail with
        # FOLLOW_UP_DUE_INVALID the moment the clock passed it. Nothing here relaxes the guard - a due
        # follow-up is exactly what /followups and the engagement attention item are supposed to read, and
        # the refusal for an accidental past date is asserted in tests/test_phase46_engagement_ledger.py.
        ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-10-16T08:00:00Z", channel="EMAIL",
                                 reason="answer the price question", allow_past_due=True)
        ledger.create_proposal(self.lead["lead_id"], summary="twelve thousand naira for the pilot",
                              terms_note="agreed scope in the ledger")
        follow_ups = self.command("/followups")
        self.assertEqual(follow_ups["code"], "FOLLOW_UPS")
        self.assertEqual(follow_ups["count"], 1, "the due follow-up is the row this surface exists to show")
        self.assertEqual(self.command("/proposals")["code"], "PROPOSALS")
        answer = self.command("/attention")
        self.assertEqual(answer["code"], "ATTENTION")
        self.assertGreaterEqual(answer["count"], 1)
        self.assertEqual(self.command("/inbox")["code"], "INBOX")

    def test_package_and_project_answer_with_references_the_operator_can_act_on(self) -> None:
        package = self.command(f"/package {self.project['project_id']}")
        self.assertEqual(package["code"], "REVIEW_PACKAGE_REFS")
        self.assertIn("No assigned build", package["reply"])
        project = self.command(f"/project {self.project['project_id']}")
        self.assertEqual(project["code"], "PROJECT_STATE")
        self.assertIn(self.lead["business_name"], project["reply"])
        self.assertEqual(self.command("/project project-9999")["code"], "PROJECT_NOT_FOUND")
        self.assertEqual(self.command("/package")["code"], "PROJECT_ID_REQUIRED")

    def test_publish_queue_binds_explicit_isolated_project_creation_consent(self) -> None:
        project = self.store.state["projects"][self.project["project_id"]]
        project["assigned_build_id"] = "build-fixture-approved"
        self.store._save()
        with mock.patch.object(
            self.store, "authorize_preview_deployment",
            return_value={"authorized": True, "code": "AUTHORIZED"},
        ) as gate:
            answer = self.bot._queue_publish(
                self.project["project_id"], user_id=OPERATOR, chat_id=CHAT,
            )

        self.assertEqual(answer["code"], "PUBLISH_JOB_QUEUED")
        gate.assert_called_once_with(self.project["project_id"], allow_provisioning=True)
        job = self.store.state["jobs"][answer["job_id"]]
        self.assertFalse(job["dry_run"])
        self.assertTrue(job["input"]["approve_real_preview"])
        self.assertIn("isolated Vercel project creation", job["input"]["note"])

    def test_help_is_generated_from_the_same_registry_the_bot_dispatches_from(self) -> None:
        reply = self.command("/help")["reply"]
        for name in COMMANDS:
            self.assertIn("/" + name, reply)
        self.assertEqual(self.command("/nope")["code"], "COMMAND_UNSUPPORTED")


class ApprovalTests(BotTestCase):
    def test_a_button_press_records_the_decision_through_the_existing_review_path(self) -> None:
        outreach_id = self.draft()
        prompt = self.command(f"/review {outreach_id}")
        self.assertEqual(prompt["code"], "REVIEW_PROMPT")
        self.assertEqual(prompt["buttons"], [["Approve for sending", "Reject"], ["Ask for changes"]])
        record = self.store.state["outreach"][outreach_id]
        answer = self.bot.handle_update(self.callback(outreach_id, "approve", draft_fingerprint(record)))
        self.assertEqual(answer["code"], "DECISION_RECORDED")
        stored = self.store.state["outreach"][outreach_id]
        self.assertEqual(stored["status"], "READY_TO_SEND")
        self.assertEqual(stored["reviewed_by"], f"telegram:{OPERATOR}")
        self.assertEqual(stored["reviewer_identity_source"], "telegram_operator_allowlist")
        self.assertEqual(self.api.edited[0]["reply_markup"], {"inline_keyboard": []},
                         "a recorded decision must not leave a pressable button behind")
        self.assertTrue(self.api.acked, "an unacknowledged callback spins in the client")

    def test_a_stale_button_is_refused_and_the_current_state_is_offered(self) -> None:
        outreach_id = self.draft()
        stale = draft_fingerprint(self.store.state["outreach"][outreach_id])
        self.store.state["outreach"][outreach_id]["draft_message"] = (
            "Hi Fictional Pitstop Test, edited copy: https://preview.example/demo")
        self.store._save()
        answer = self.bot.handle_callback(f"ap:{outreach_id}:{stale}", user_id=OPERATOR, chat_id=CHAT,
                                          message_id=1, callback_query_id="cq2")
        self.assertEqual(answer["code"], "APPROVAL_STALE")
        self.assertEqual(self.store.state["outreach"][outreach_id]["status"], "AWAITING_REVIEW")
        self.assertIsNone(self.store.state["outreach"][outreach_id].get("reviewed_by"))
        self.assertTrue(self.api.acked[-1]["show_alert"], "a stale press has to be seen, not noted quietly")

    def test_a_fingerprint_for_a_different_record_is_not_accepted(self) -> None:
        first = self.draft()
        second = self.draft()
        other_fingerprint = draft_fingerprint(self.store.state["outreach"][first])
        answer = self.bot.handle_callback(f"rj:{second}:{other_fingerprint}", user_id=OPERATOR, chat_id=CHAT,
                                          callback_query_id="cq3")
        self.assertEqual(answer["code"], "APPROVAL_STALE")
        self.assertEqual(self.store.state["outreach"][second]["status"], "AWAITING_REVIEW")

    def test_an_already_recorded_decision_is_reported_as_a_replay(self) -> None:
        outreach_id = self.draft(status="READY_TO_SEND")
        record = self.store.state["outreach"][outreach_id]
        record.update({"reviewed_by": f"telegram:{OPERATOR}", "reviewed_at": "2026-10-16T00:00:00Z",
                       "reviewer_identity_source": "telegram_operator_allowlist"})
        answer = self.bot.handle_callback(f"ap:{outreach_id}:{draft_fingerprint(record)}", user_id=OPERATOR,
                                          chat_id=CHAT, callback_query_id="cq4")
        self.assertEqual(answer["code"], "APPROVAL_REPLAY")

    def test_typed_commands_need_a_confirmation_bound_to_the_record_state(self) -> None:
        outreach_id = self.draft()
        ask = self.command(f"/approve {outreach_id}")
        self.assertEqual(ask["code"], "CONFIRMATION_REQUIRED")
        self.assertIn("--confirm", ask["reply"])
        self.assertEqual(self.store.state["outreach"][outreach_id]["status"], "AWAITING_REVIEW")
        fingerprint = draft_fingerprint(self.store.state["outreach"][outreach_id])
        done = self.command(f"/reject {outreach_id} --confirm {fingerprint} --note the copy promised a price")
        self.assertEqual(done["code"], "DECISION_RECORDED")
        stored = self.store.state["outreach"][outreach_id]
        self.assertEqual(stored["status"], "REJECTED")
        self.assertIn("the copy promised a price", stored["review_notes"])
        self.assertIn(f"telegram:{OPERATOR}", stored["review_notes"])

    def test_a_button_press_cannot_send_or_contact_anything(self) -> None:
        outreach_id = self.draft()
        fingerprint = draft_fingerprint(self.store.state["outreach"][outreach_id])
        self.bot.handle_update(self.callback(outreach_id, "approve", fingerprint))
        # Every outbound call the bot made went to the operator's chat - never to the lead's address.
        outbound = json.dumps({"sent": self.api.sent, "edited": self.api.edited}, default=str)
        targets = {str(item["chat_id"]) for item in [*self.api.sent, *self.api.edited]}
        self.assertEqual(targets, {str(CHAT)})
        self.assertNotIn("aisha@pitstop.ng", outbound, "a decision receipt is not a message to the business")
        for key in ("sent_at", "provider_message_id", "provider_response", "delivered_at"):
            self.assertIsNone(self.store.state["outreach"][outreach_id].get(key),
                              f"an approval pressed in chat creates no {key} evidence of any kind")
        row = self.store.state["outreach"][outreach_id]
        self.assertEqual(row["status"], "READY_TO_SEND", "READY_TO_SEND is not SENT, and the bot cannot make it so")

    def test_review_list_is_minimal_but_explicit_review_shows_full_email_preview(self) -> None:
        draft_id = self.draft()
        draft = self.store.state["outreach"][draft_id]
        answer = self.command("/review")
        self.assertEqual(answer["code"], "REVIEW_LIST")
        self.assertIn("await your review", answer["reply"])
        self.assertNotIn("a concept is ready", answer["reply"], "the list view stays minimal")
        answer = self.command(f"/review {draft_id}")
        prompt = answer["reply"]
        self.assertEqual(answer["buttons"], [["Approve for sending", "Reject"], ["Ask for changes"]])
        self.assertIn(draft["draft_message"], prompt, "explicit review must show the exact message body")
        self.assertIn(draft["recipient"], prompt, "the private operator preview must show the exact recipient")
        self.assertIn(draft["subject"], prompt)
        self.assertIn(draft["preview_url"], prompt)
        self.assertIn(answer["fingerprint"], prompt)


class NotificationTests(BotTestCase):
    def test_one_condition_is_reported_once_and_a_change_reports_again(self) -> None:
        self.store.state["jobs"]["job-1"] = {"job_id": "job-1", "job_type": "BUILD", "status": "FAILED",
                                           "error_code": "WORKER_FAILED", "error": "builder fault",
                                           "lead_id": self.lead["lead_id"], "project_id": None,
                                           "attempts": 3, "max_attempts": 3, "updated_at": "2026-10-16T00:00:00Z",
                                           "completed_at": "2026-10-16T00:00:00Z"}
        self.store._save()
        first = self.bot.notify_once()
        self.assertGreaterEqual(first["notified"], 1)
        second = self.bot.notify_once()
        self.assertEqual(second["code"], "NOTHING_NEW")
        self.assertEqual(second["notified"], 0)
        self.assertEqual(len(self.api.sent), 1, "the same unchanged record must not page twice")
        self.store.state["jobs"]["job-1"]["error_code"] = "RETRY_LIMIT_REACHED"
        self.store.state["jobs"]["job-1"]["completed_at"] = "2026-10-16T01:00:00Z"
        self.store._save()
        third = self.bot.notify_once()
        self.assertGreaterEqual(third["notified"], 1, "a genuinely new recorded condition is a new message")

    def test_suppression_survives_a_restart_because_it_is_read_from_the_store(self) -> None:
        self.store.state["jobs"]["job-2"] = {"job_id": "job-2", "job_type": "BUILD", "status": "FAILED",
                                            "error_code": "WORKER_FAILED", "error": "fault", "attempts": 3,
                                            "max_attempts": 3, "lead_id": None, "project_id": None,
                                            "updated_at": "2026-10-16T00:00:00Z",
                                            "completed_at": "2026-10-16T00:00:00Z"}
        self.store._save()
        self.assertEqual(self.bot.notify_once()["notified"], 1, "the condition has to be real for the restart to mean something")
        restarted = self.make_bot()
        self.assertEqual(restarted.notify_once()["notified"], 0)
        self.assertIn("attention_seen", self.store.state["provider_sync"])

    def test_a_notification_never_carries_a_private_message_or_a_credential(self) -> None:
        from tracking.inbound import InboundIntake

        intake = InboundIntake(self.bot.ledger)
        intake.process({"provider": "EMAIL_INBOUND", "event_id": "gmail-unmatched", "channel": "EMAIL",
                       "body": "Please stop. My password is hunter2hunter2hunter2 and call 08012345678",
                       "sender": "stranger@elsewhere.example"})
        report = self.bot.notify_once()
        self.assertGreaterEqual(report["notified"], 1)
        text = json.dumps(self.api.sent)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("08012345678", safe_outbound_text("call 08012345678 or write a@b.co"),
                         "a phone number is contact data, and a chat is not where it belongs")
        self.assertIn("5678", safe_outbound_text("call 08012345678"), "the tail stays recognisable")
        self.assertNotIn("08012345678", text)
        self.assertNotIn("Please stop", text, "the intake's queue item is enough; the message body is not")

    def test_a_failed_delivery_is_not_counted_as_reported(self) -> None:
        self.store.state["jobs"]["job-3"] = {"job_id": "job-3", "job_type": "BUILD", "status": "FAILED",
                                             "error_code": "WORKER_FAILED", "error": "fault", "attempts": 3,
                                             "max_attempts": 3, "lead_id": None, "project_id": None,
                                             "updated_at": "2026-10-16T00:00:00Z",
                                             "completed_at": "2026-10-16T00:00:00Z"}
        self.store._save()
        broken = self.make_bot(api=FakeTelegramApi(send_error=ProviderError("offline", code="TELEGRAM_UNRESPONSIVE",
                                                                           retryable=True)))
        report = broken.notify_once()
        self.assertEqual(report["code"], "TELEGRAM_UNRESPONSIVE")
        self.assertTrue(report["retryable"])
        self.assertNotIn(attention_identity({"kind": "job", "code": "WORKER_FAILED", "job_id": "job-3"}),
                         broken.seen_notifications, "an undelivered message must be retried, not marked seen")
        self.assertEqual(load_attention_seen(self.store), set())

    def test_the_alert_sink_is_another_sink_and_not_a_second_alert_system(self) -> None:
        from orchestration.alerts import check_alerts

        self.store.state["jobs"]["job-4"] = {"job_id": "job-4", "job_type": "DEPLOY", "status": "FAILED",
                                             "error_code": "RETRY_LIMIT_REACHED", "error": "publish uncertain",
                                             "attempts": 3, "max_attempts": 3,
                                             "updated_at": "2026-10-16T00:00:00Z"}
        self.store._save()
        sink = self.bot.alert_sink(chat_id=CHAT)
        seen = set()
        report = check_alerts(self.store, repository_root=self.root, sinks=[sink], seen=seen)
        self.assertGreaterEqual(report["notified"], 1)
        self.assertEqual(len(self.api.sent), 1)

        again = check_alerts(self.store, repository_root=self.root, sinks=[sink], seen=seen)
        self.assertEqual(again["notified"], 0, "the pipeline's own dedupe applies to this sink too")
        self.assertEqual(len(self.api.sent), 1)


class TransportFailureTests(BotTestCase):
    def test_a_provider_timeout_is_reported_as_retryable_and_keeps_the_offset(self) -> None:
        api = FakeTelegramApi(fail_on={"getUpdates"})
        bot = self.make_bot(api=api)
        report = bot.poll_once()
        self.assertEqual(report["code"], "TELEGRAM_UNRESPONSIVE")
        self.assertTrue(report["retryable"])
        self.assertIsNone(bot.last_poll_offset)

    def test_an_update_batch_advances_the_durable_offset_only_after_handling(self) -> None:
        api = FakeTelegramApi(updates=[{"update_id": 41, "message": {"from": {"id": OPERATOR},
                                                                    "chat": {"id": CHAT}, "text": "/status"}}])
        bot = self.make_bot(api=api)
        report = bot.poll_once()
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(bot.last_poll_offset, 42)
        self.assertEqual(self.store.state["automation_controls"]["telegram_poll"]["update_offset"], 42)

    def test_unauthorized_updates_are_counted_and_still_move_the_offset(self) -> None:
        api = FakeTelegramApi(updates=[{"update_id": 1, "message": {"from": {"id": OTHER}, "chat": {"id": CHAT},
                                                                   "text": "/leads"}}])
        bot = self.make_bot(api=api)
        report = bot.poll_once()
        self.assertEqual(report["unauthorized"], 1)
        self.assertEqual(report["results"][0]["code"], "AUTHORIZATION_REFUSED")

    def test_a_reply_that_cannot_be_sent_does_not_lose_the_answer(self) -> None:
        api = FakeTelegramApi(send_error=ProviderError("rate limited", code="TELEGRAM_RATE_LIMITED",
                                                        retryable=True, retry_after=30))
        answer = self.make_bot(api=api).dispatch("/status", user_id=OPERATOR, chat_id=CHAT)
        self.assertEqual(answer["delivered"], False)
        self.assertEqual(answer["delivery"]["error_code"], "TELEGRAM_RATE_LIMITED")
        self.assertIn("0 job", answer["reply"])


class HoldTests(BotTestCase):
    def test_the_hold_gates_every_dispatch_path_and_nothing_else(self) -> None:
        job = JobService(self.store).create("SCOUT", job_id="job-hold", input={"source": "fixture",
                                                                              "source_file": "x.json"})
        view = set_hold(self.store, actor="operator", reason="deploy freeze")
        self.assertTrue(view["held"])
        service = JobService(self.store)
        self.assertEqual(service.gate_failure(job)[0], HOLD_CODE)
        from orchestration.workers import LocalPipelineWorker

        result = service.run("job-hold", LocalPipelineWorker(repository_root=self.root,
                                                            output_root=self.root / "out"))
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error_code"], HOLD_CODE)
        self.assertEqual(result["attempts"], 0, "a held job was never attempted")
        self.assertEqual(blocked_by_hold(self.store)[0]["job_id"], "job-hold")
        self.assertEqual(self.command("/hold")["code"], "HELD")

    def test_release_reports_what_still_needs_a_human(self) -> None:
        set_hold(self.store, actor="operator")
        answer = self.command("/resume")
        self.assertEqual(answer["code"], "AUTOMATION_RESUMED")
        self.assertIn("re-run by an explicit retry", answer["reply"])
        self.assertFalse(hold_view(self.store)["held"])

    def test_a_hold_is_visible_in_attention_but_never_pages(self) -> None:
        from orchestration.alerts import CRITICAL_KINDS, critical_alerts

        set_hold(self.store, actor="operator", reason="quiet hours")
        report = hold_view(self.store)
        self.assertTrue(report["held"])
        from orchestration.health import operator_attention

        attention = operator_attention(self.store, repository_root=self.root)
        codes = {item.get("code") for item in attention["items"]}
        self.assertIn(HOLD_CODE, codes)
        self.assertTrue(attention["counts"]["automation_held"])
        self.assertEqual([alert for alert in critical_alerts(self.store, repository_root=self.root)
                          if alert.get("kind") == "control"], [])
        self.assertNotIn("control", CRITICAL_KINDS)

    def test_a_repeated_hold_is_a_replay_not_a_rewritten_clock(self) -> None:
        first = set_hold(self.store, actor="operator", reason="quiet")
        second = set_hold(self.store, actor="operator", reason="quiet")
        self.assertTrue(second["held"])
        self.assertEqual(second["set_at"], first["set_at"])
        self.assertIn("repeat_detected_at", self.store.state["automation_controls"]["dispatch"])

    def test_an_unreadable_control_record_says_so_instead_of_quietly_holding(self) -> None:
        self.store.state["automation_controls"]["dispatch"] = "not a record"
        view = hold_view(self.store)
        self.assertFalse(view["held"])
        self.assertTrue(view["unreadable"])
        self.assertEqual(view["error_code"], "AUTOMATION_CONTROL_UNREADABLE")
        from orchestration.health import operator_attention

        codes = {item.get("code") for item in operator_attention(self.store, repository_root=self.root)["items"]}
        self.assertIn("AUTOMATION_CONTROL_UNREADABLE", codes)

    def test_the_hold_does_not_revoke_a_decision_or_touch_ledger_state(self) -> None:
        outreach_id = self.draft(status="READY_TO_SEND")
        before = dict(self.store.state["outreach"][outreach_id])
        set_hold(self.store, actor="operator")
        self.assertEqual(self.store.state["outreach"][outreach_id], before)
        release_hold(self.store, actor="operator")
        self.assertEqual(self.store.state["outreach"][outreach_id], before)


class GmailAdapterTests(unittest.TestCase):
    def test_a_message_maps_onto_the_phase_47_contract_without_inventing_anything(self) -> None:
        event = message_to_inbound_event(gmail_message())
        self.assertEqual(event["provider"], "EMAIL_INBOUND")
        self.assertEqual(event["event_id"], "m1")
        self.assertEqual(event["channel"], "EMAIL")
        self.assertEqual(event["sender"], "aisha@pitstop.ng", "the display name is not an identity")
        self.assertEqual(event["conversation_id"], "t77")
        self.assertEqual(event["message_ref"], "gmail:m1")
        self.assertEqual(event["occurred_at"], internal_date_iso("1760000000"))
        self.assertNotIn("in_reply_to", event, "an RFC 822 Message-ID is not one of our record ids")
        self.assertNotIn("lead_id", event, "the adapter never decides who it belongs to")
        for field in ("approved", "sent", "outcome", "status", "provider_verified", "won"):
            self.assertNotIn(field, event)

    def test_a_malformed_message_is_refused_rather_than_half_recorded(self) -> None:
        with self.assertRaises(GmailError) as caught:
            message_to_inbound_event({"threadId": "t"})
        self.assertEqual(caught.exception.code, "GMAIL_MESSAGE_MALFORMED")

    def test_the_snippet_is_bounded_and_a_full_body_is_opt_in(self) -> None:
        long_message = gmail_message()
        long_message["snippet"] = "x" * 5000
        self.assertLessEqual(len(message_to_inbound_event(long_message)["body"]), 900)
        payload_full = {"id": "m2", "snippet": "", "internalDate": "1760000000",
                        "payload": {"headers": [{"name": "From", "value": "a@b.co"}],
                                    "parts": [{"mimeType": "text/plain",
                                               "body": {"data": "aGVsbG8="}}]}}
        self.assertEqual(message_to_inbound_event(payload_full, fetch_format="metadata")["body"],
                         "(no text Gmail returned for m2)")
        self.assertEqual(message_to_inbound_event(payload_full, fetch_format="full")["body"], "hello")

    def test_header_lookup_and_base64url_decoding_tolerate_real_shapes(self) -> None:
        headers = header_map({"headers": [{"name": "From", "value": "a@b.co"},
                                          {"name": "from", "value": "ignored-later"}]})
        self.assertEqual(headers["From"], "a@b.co")
        from integrations.gmail import extract_plain_text

        import base64

        unpadded = base64.urlsafe_b64encode(b"Hi-there\n\nquoted history that is not part of the reply").decode()
        self.assertEqual(extract_plain_text({"parts": [{"mimeType": "text/plain",
                                                        "body": {"data": unpadded.rstrip("=")}}]}), "Hi-there",
                         "Gmail sends base64url without padding, and the paragraph before the quote is the reply")

    def test_configuration_reports_names_and_never_values(self) -> None:
        with mock.patch.dict("os.environ", {"AI_WEBSITE_AGENT_GMAIL_REFRESH_TOKEN": "supersecret-refresh",
                                            "AI_WEBSITE_AGENT_GMAIL_CLIENT_ID": "client-id",
                                            "AI_WEBSITE_AGENT_GMAIL_CLIENT_SECRET": "client-secret"}, clear=True):
            report = gmail_config()
            self.assertTrue(report["configured"])
            self.assertEqual(report["scope"].rsplit("/", 1)[-1], "gmail.readonly")
            self.assertNotIn("supersecret-refresh", json.dumps(report))
            self.assertEqual(report["missing"], [])

    def test_the_adapter_exposes_no_way_to_write_to_the_mailbox(self) -> None:
        from integrations.gmail import GmailClient

        surface = {name for name in dir(GmailClient) if not name.startswith("_")}
        self.assertEqual(surface & {"send", "delete", "modify", "reply", "mark_read", "archive", "trash"}, set())
        status = GmailClient(refresh_token="r", client_id="c", client_secret="s").status()
        self.assertIn("messages.send", status["explicitly_absent"])

    def test_an_http_error_from_google_is_typed_and_never_leaks_the_bearer_token(self) -> None:
        from integrations.gmail import GmailClient

        def transport(method, url, params, headers, body):
            if "profile" in url or "/messages" in url:
                raise ProviderError(f"refused with Authorization: {headers.get('Authorization')}",
                                     code="PROVIDER_REQUEST_REFUSED", status=403)
            return {"status": 200, "json": {"access_token": "***", "expires_in": 3600}, "text": ""}

        client = GmailClient(refresh_token="r", client_id="c", client_secret="s", transport=transport)
        with self.assertRaises(GmailError) as caught:
            client.list_messages("label:INBOX")
        self.assertEqual(caught.exception.code, "GMAIL_AUTHORIZATION_REFUSED")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("secret-token", client.__repr__() if hasattr(client, "__repr__") else "")


class TelegramTransportTests(unittest.TestCase):
    def test_the_token_never_appears_in_a_report_or_a_repr(self) -> None:
        api = TelegramBotApi("123456:REAL-LOOKING-SECRET", transport=lambda method, payload: {"ok": True,
                                                                                              "result": {}})
        self.assertNotIn("REAL-LOOKING", repr(api))
        self.assertNotIn("REAL-LOOKING", json.dumps(telegram_config({"AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN":
                                                                    "123456:REAL-LOOKING-SECRET"})))
        report = telegram_config({"AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN": "123456:x",
                                 "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": "1, 2,3"})
        self.assertEqual(report["allowed_user_ids"], [1, 2, 3])
        self.assertTrue(report["configured"])

    def test_an_unconfigured_bot_is_inert_and_says_what_is_missing(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            report = telegram_config()
            self.assertFalse(report["configured"])
            self.assertIn("AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS", report["next_action"])

    def test_telegrams_own_limits_are_enforced_before_a_request_is_built(self) -> None:
        text, clipped = clip_message("y" * 5000)
        self.assertEqual(len(text), 4096)
        self.assertTrue(clipped)
        with self.assertRaises(ProviderError) as caught:
            encode_callback_data("ap:outreach-0001:" + "x" * 80)
        self.assertEqual(caught.exception.code, "TELEGRAM_CALLBACK_TOO_LARGE")
        keyboard = inline_keyboard([[("Approve", "ap:outreach-0001:abc123")]])
        self.assertLessEqual(len(keyboard["inline_keyboard"][0][0]["callback_data"].encode()),
                             MAX_CALLBACK_DATA_BYTES)

    def test_only_https_reaches_a_provider_and_loopback_plaintext_is_allowed_for_fakes(self) -> None:
        self.assertEqual(check_endpoint("http://127.0.0.1:9/healthz"), "http://127.0.0.1:9/healthz")
        for url, code in (("http://api.telegram.org/x", "PROVIDER_URL_INSECURE"),
                          ("ftp://host/x", "PROVIDER_URL_SCHEME_UNSUPPORTED"),
                          ("https://user:pw@host/x", "PROVIDER_URL_INVALID")):
            with self.assertRaises(ProviderError) as caught:
                check_endpoint(url)
            self.assertEqual(caught.exception.code, code)


class EmailMatchingTests(BotTestCase):

    def test_a_sender_address_recorded_on_one_lead_links_the_reply(self) -> None:
        report = self.poller([gmail_message()]).poll_once()
        self.assertEqual(report["recorded"], 1)
        result = report["results"][0]
        self.assertEqual(result["lead_id"], self.lead["lead_id"])
        self.assertEqual(result["disposition"], "RECORDED")
        self.assertIn("lead_id", result["linkage_basis"])

    def test_a_business_name_in_a_subject_line_links_nothing(self) -> None:
        message = gmail_message("m-stranger", thread="t999", sender="curious@elsewhere.example",
                               subject="Fictional Pitstop Test - quick question", snippet="Is the site ready?")
        report = self.poller([message]).poll_once()
        result = report["results"][0]
        self.assertEqual(result["disposition"], "UNMATCHED")
        self.assertIsNone(result["lead_id"])
        self.assertEqual(report["unmatched"], 1)
        self.assertEqual(report["matched"], 0)
        pending = self.poller([]).intake.pending()
        self.assertEqual(pending["items"][0]["provider_event_id"], "m-stranger")
        self.assertIn("inbound-pending", pending["items"][0]["next_action"])

    def test_a_shared_thread_links_by_recorded_conversation_but_an_ambiguous_one_does_not(self) -> None:
        from tracking.inbound import InboundIntake

        ledger = self.bot.ledger
        InboundIntake(ledger).process({"provider": "EMAIL_INBOUND", "event_id": "prior", "channel": "EMAIL",
                                       "conversation_id": "t77", "lead_id": self.lead["lead_id"],
                                       "body": "earlier message"})
        other = self.store.create_lead({"business_name": "Second Fictional Store", "business_type": "Bakeries",
                                        "city": "Lagos", "country": "Nigeria", "data_classification": "TEST_FIXTURE",
                                        "qualification_status": "QUALIFIED", "email": "owner@second.ng"})
        InboundIntake(ledger).process({"provider": "EMAIL_INBOUND", "event_id": "prior-2", "channel": "EMAIL",
                                       "conversation_id": "t77", "lead_id": other["lead_id"],
                                       "body": "a competing claim on the same thread"})
        report = self.poller([gmail_message("m-new", sender="anyone@nowhere.example")]).poll_once()
        result = report["results"][0]
        self.assertEqual(result["disposition"], "UNMATCHED", "two leads claim the thread; nothing is guessed")
        self.assertIn("referenced by 2 leads", result["linkage_note"])

    def test_two_leads_sharing_an_address_are_not_resolved_by_choosing_one(self) -> None:
        self.store.create_lead({"business_name": "Twin Fictional Store", "business_type": "Restaurants",
                               "city": "Lagos", "country": "Nigeria", "data_classification": "TEST_FIXTURE",
                               "qualification_status": "QUALIFIED", "email": "aisha@pitstop.ng"})
        result = self.poller([gmail_message()]).poll_once()["results"][0]
        self.assertEqual(result["disposition"], "UNMATCHED")
        self.assertIn("cannot pick one", result["linkage_note"])

    def test_a_reference_to_a_recorded_draft_links_the_outreach_id(self) -> None:
        from tracking.inbound import InboundIntake

        outreach_id = self.draft(status="READY_TO_SEND")
        InboundIntake(self.bot.ledger).process({"provider": "EMAIL_INBOUND", "event_id": "m0", "channel": "EMAIL",
                                                "lead_id": self.lead["lead_id"], "outreach_id": outreach_id,
                                                "body": "the first reply", "message_ref": "gmail:m0"})
        report = self.poller([gmail_message("m-reply", thread="t500", sender="whoever@elsewhere.example",
                                           in_reply_to="<m0>")]).poll_once()
        row = [row for row in self.store.state["inbound_events"].values()
               if row.get("provider_event_id") == "m-reply"][0]
        self.assertEqual(row["in_reply_to"], outreach_id, "a Message-ID resolves only through a record we hold")

    def test_redelivery_is_a_duplicate_and_never_a_second_response(self) -> None:
        poller = self.poller([gmail_message()])
        first = poller.poll_once()
        self.assertFalse(first["results"][0]["duplicate"])
        # Same provider id again (a re-poll, a cursor loss, or a manual resend).
        self.store.state["provider_sync"].pop("gmail_inbox", None)
        second = poller.poll_once()
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["recorded"], 0)
        rows = [row for row in self.store.state["inbound_events"].values()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.store.state["engagements"]), 1)

    def test_a_clear_opt_out_from_a_linked_sender_blocks_contact(self) -> None:
        from tracking.engagement import EngagementLedger

        message = gmail_message("m-stop", snippet="Please stop contacting me about this.")
        report = self.poller([message]).poll_once()
        self.assertTrue(report["results"][0]["opt_out"])
        ledger = EngagementLedger(self.store, source="test")
        self.assertFalse(ledger.outreach_eligibility(self.lead["lead_id"], channel="EMAIL")["eligible"])
        self.assertIsNotNone(ledger.is_blocked(self.lead["lead_id"], "WHATSAPP"),
                             "a clear written request stops every channel, not just the one that received it")

    def test_a_provider_unsubscribe_without_customer_words_covers_one_channel_only(self) -> None:
        from tracking.engagement import EngagementLedger

        # Gmail reports a list unsubscription the customer clicked, and the text itself says nothing about
        # contacting: the block therefore covers the channel the provider acted on, and no other.
        message = gmail_message("m-list", snippet="You are no longer on the marketing list.", in_reply_to=None)
        event = message_to_inbound_event(message)
        event.update({"lead_id": self.lead["lead_id"], "unsubscribe_confirmed": True})
        result = self.poller([]).intake.process(event)
        self.assertEqual(result["disposition"], "OPT_OUT_RECORDED")
        self.assertEqual(result["opt_out"]["scope"], "channel", "the provider confirmed one list, not every channel")
        ledger = EngagementLedger(self.store, source="test")
        self.assertIsNotNone(ledger.is_blocked(self.lead["lead_id"], "EMAIL"))
        self.assertIsNone(ledger.is_blocked(self.lead["lead_id"], "WHATSAPP"),
                          "a list unsubscribe belongs to the list, not to every channel there is")
        self.assertFalse(ledger.outreach_eligibility(self.lead["lead_id"], channel="EMAIL")["eligible"])
        # The customer's own words do reach every channel, with or without a provider header.
        words = gmail_message("m-words", snippet="Unsubscribe me from everything, please.", in_reply_to=None)
        second = self.poller([words]).poll_once()
        self.assertEqual(second["results"][0]["disposition"], "OPT_OUT_RECORDED")
        self.assertIsNotNone(ledger.is_blocked(self.lead["lead_id"], "WHATSAPP"))

    def test_a_hedged_message_blocks_nothing(self) -> None:
        message = gmail_message("m-maybe", snippet="I think maybe not interested, unless you can wait a while.")
        report = self.poller([message]).poll_once()
        self.assertFalse(report["results"][0]["opt_out"])
        self.assertIsNone(self.bot.ledger.is_blocked(self.lead["lead_id"], "EMAIL"))


class EmailCycleTests(BotTestCase):
    def test_dry_run_shows_what_would_be_recorded_and_records_nothing(self) -> None:
        before = self.store.state["inbound_events"]
        report = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store).poll_once(dry_run=True)
        row = report["results"][0]
        self.assertTrue(row["would_record"])
        self.assertEqual(row["lead_id"], self.lead["lead_id"])
        self.assertEqual(row["triage_category"], "INFORMATION_REQUEST")
        self.assertEqual(self.store.state["inbound_events"], before)
        self.assertEqual(self.store.state.get("provider_sync", {}).get("gmail_inbox", {}).get("cycles"), None)

    def test_the_cursor_moves_only_after_a_completed_cycle(self) -> None:
        error = GmailError("rate limited", code="GMAIL_RATE_LIMITED", retryable=True)
        poller = EmailInboundPoller(self.fake_gmail([], list_error=error), store=self.store)
        report = poller.poll_once()
        self.assertEqual(report["error_code"], "GMAIL_RATE_LIMITED")
        self.assertTrue(report["retryable"])
        record = self.store.state["provider_sync"]["gmail_inbox"]
        self.assertNotIn("last_history_id", record)
        self.assertEqual(record["last_error"]["error_code"], "GMAIL_RATE_LIMITED")
        # A later good cycle still reads the mail, because nothing was skipped.
        ok = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store)
        self.assertEqual(ok.poll_once()["recorded"], 1)

    def test_a_persistence_fault_keeps_the_event_retryable(self) -> None:
        from tracking.database import DatabasePersistenceError

        poller = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store)
        with mock.patch.object(self.store, "_save",
                              side_effect=DatabasePersistenceError("connection lost")):
            report = poller.poll_once()
        row = report["results"][0]
        self.assertEqual(row["error_code"], "PERSISTENCE_FAILED")
        self.assertTrue(row["retryable"])
        self.assertEqual(len(poller.intake.pending(dispositions=("FAILED",))["items"]), 0,
                         "the failed write rolled back with the row, so nothing was half-recorded")
        sync = self.store.state["provider_sync"]["gmail_inbox"]
        self.assertEqual(sync["unwritten"], ["m1"],
                         "the cursor moves past a message nothing was written for, so its id is parked here")
        self.assertTrue(sync["last_error_retryable"], "the record says this fault is worth another cycle")
        written = TrackingStore(self.store.path)
        self.assertEqual(len(written.state["engagements"]), 0,
                         "the commit failed, so the durable record holds no half-written response")
        self.assertEqual(len(written.state["inbound_events"]), 0)
        retry = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store).retry_pending()
        self.assertEqual(retry["retried"], 1)
        self.assertEqual(retry["results"][0]["disposition"], "RECORDED",
                         "the same message lands once storage works, with the same provider id")
        self.assertEqual(retry["still_unwritten"], [])
        self.assertEqual(len(self.store.state["engagements"]), 1, "and it lands exactly once")
        # A lost cursor (the save fault above, or a restarted process) must not become a second response: the
        # provider's own message id is what the dedupe rests on, not the cursor.
        self.store.state["provider_sync"].pop("gmail_inbox", None)
        after = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store).poll_once()
        self.assertEqual(after["fetched"], 1, "with no cursor the whole query is re-read")
        self.assertEqual(after["recorded"], 0, "a provider id already written is never written twice")
        self.assertEqual(after["duplicates"], 1)
        self.assertEqual(len(self.store.state["engagements"]), 1)

    def test_history_is_preferred_and_a_refused_cursor_falls_back_to_the_query(self) -> None:
        client = self.fake_gmail([gmail_message()])
        client.history = {"raise": None}
        def refusing_history(history_id, *, label_id="INBOX", max_results=100):
            raise GmailError("history expired", code="GMAIL_REQUEST_REFUSED", status=400)
        client.get_history = refusing_history
        self.store.state["provider_sync"]["gmail_inbox"] = {"last_history_id": 900}
        report = EmailInboundPoller(client, store=self.store).poll_once()
        self.assertEqual(report["fetched"], 1, "a stale history id must not cost the operator their mail")
        self.assertEqual(report["recorded"], 1)
        self.assertIn("refused_by_provider", self.store.state["provider_sync"]["gmail_inbox"]["history_id_source"])

    def test_a_message_google_will_not_return_is_reported_not_guessed(self) -> None:
        client = self.fake_gmail([gmail_message("m-gone")])
        client.unreadable = {"m-gone"}
        report = EmailInboundPoller(client, store=self.store).poll_once()
        self.assertEqual(report["results"][0]["error_code"], "GMAIL_MESSAGE_UNREADABLE")
        self.assertTrue(report["results"][0]["retryable"])
        self.assertEqual(report["recorded"], 0)

    def test_retry_pending_resubmits_unmatched_events_after_an_operator_links_them(self) -> None:
        from tracking.inbound import InboundIntake

        unmatched = self.poll_and_unmatch()
        self.assertEqual(unmatched["disposition"], "UNMATCHED")
        self.assertEqual(unmatched["lead_id"], None, "a stray message is parked, never attached to a guess")
        stray = gmail_message("m2", thread="t88", sender="whoever@nowhere.example", subject="hello",
                              snippet="quick question")
        result = EmailInboundPoller(self.fake_gmail([stray]), store=self.store).retry_pending()
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["results"][0]["disposition"], "UNMATCHED",
                         "retrying without new evidence changes nothing, which is the point")
        # The operator records the thread against a lead; the retry then links through that recorded reference.
        InboundIntake(self.bot.ledger).process({"provider": "EMAIL_INBOUND", "event_id": "operator-link",
                                               "channel": "EMAIL", "conversation_id": "t88",
                                               "lead_id": self.lead["lead_id"], "body": "same thread, linked by hand"})
        retry = EmailInboundPoller(self.fake_gmail([stray]), store=self.store).retry_pending()
        self.assertEqual(retry["results"][0]["disposition"], "RECORDED", retry["results"][0])
        self.assertEqual(retry["results"][0]["lead_id"], self.lead["lead_id"])
        parked = [row for row in self.store.state["inbound_events"].values()
                  if row.get("provider_event_id") == "m2"][0]
        self.assertEqual(parked["disposition"], "RECORDED",
                         "the retry wrote the message itself; the queue entry stops being a queue entry")
        self.assertEqual(parked["adapter_match_basis"], "provider_thread_reference",
                         "how the adapter decided is kept apart from what the intake was told")
        self.assertEqual(parked["linkage_basis"], "lead_id",
                         "from the intake's side the lead arrived named on the event, and it says so")
        self.assertEqual(parked["source"], "gmail-adapter-retry", "the row records that a retry wrote it")
        self.assertEqual(len(self.store.state["engagements"]), 2,
                         "the operator's record and this message are two events, not one doubled")
        self.assertEqual(self.poller([]).intake.pending()["total_matching"], 0,
                         "nothing is left sitting in the backlog")

    def poll_and_unmatch(self):
        message = gmail_message("m2", thread="t88", sender="whoever@nowhere.example", subject="hello",
                               snippet="quick question")
        report = EmailInboundPoller(self.fake_gmail([message]), store=self.store).poll_once()
        return report["results"][0]

    def test_the_intake_contract_is_used_verbatim_and_forbidden_fields_stay_refused(self) -> None:
        from tracking.inbound import InboundEventError

        intake = self.poller([]).intake
        for field in ("approved", "sent", "outcome", "status", "provider_verified", "identity_verified",
                      "next_action", "recorded_by"):
            payload = message_to_inbound_event(gmail_message("mf-" + field))
            payload[field] = True
            with self.assertRaises(InboundEventError, msg=field) as caught:
                intake.process(payload, source="gmail-adapter")
            self.assertEqual(caught.exception.code, "INBOUND_FORBIDDEN_FIELD", field)

    def test_a_provider_event_id_is_required_because_dedupe_depends_on_it(self) -> None:
        from tracking.inbound import InboundEventError

        event = message_to_inbound_event(gmail_message("mx"))
        event.pop("event_id")
        with self.assertRaises(InboundEventError) as caught:
            self.poller([]).intake.process(event, source="gmail-adapter")
        self.assertEqual(caught.exception.code, "EVENT_ID_REQUIRED")

    def test_the_cycle_report_and_the_intake_row_say_the_same_thing_about_linkage(self) -> None:
        report = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store).poll_once()
        result = report["results"][0]
        row = self.store.state["inbound_events"][result["dedupe_key"]]
        self.assertEqual(result["linkage_basis"], row["linkage_basis"])
        self.assertEqual(row["adapter_match_basis"], "sender_address_matches_recorded_contact")
        self.assertEqual(row["sender_identity"], "provider_asserted")
        self.assertEqual(row["provider"], "EMAIL_INBOUND")


class RedactionTests(unittest.TestCase):
    def test_outbound_text_drops_credentials_paths_and_contact_details(self) -> None:
        text = safe_outbound_text("call 08012345678 or write aisha@pitstop.ng\n"
                                  "dsn postgres://user:***@host/db\n"
                                  "file at /home/operator/private/x.html\n"
                                  "command /attention and GET /jobs/queue")
        self.assertNotIn("08012345678", text)
        self.assertNotIn("postgres://", text)
        self.assertNotIn("aisha@pitstop.ng", text)
        self.assertNotIn("/home/operator", text)
        self.assertIn("/attention", text, "a bot that cannot say what to type next is useless")
        self.assertIn("[credential removed", text)

    def test_masking_keeps_an_address_recognisable_without_publishing_it(self) -> None:
        masked = mask_address("aisha.bello@pitstop.ng")
        self.assertNotIn("aisha.bello@", masked, "the local part is what identifies a person")
        self.assertTrue(masked.endswith("@pitstop.ng"), "the domain stays, or the operator cannot place the contact")
        self.assertIn("…", masked)
        # A four-character local part cannot be masked 3+3 without printing the whole thing, so it collapses to
        # an initial: a mask that keeps most of the value is not a mask.
        self.assertEqual(mask_address("boss@pitstop.ng"), "b…@pitstop.ng")
        self.assertEqual(mask_address(None), "")
        self.assertEqual(mask_address("not an address"), "not an address")

    def test_attention_identity_ignores_everything_but_the_record(self) -> None:
        item = {"kind": "job", "code": "WORKER_FAILED", "job_id": "job-1", "recorded_at": "2026-10-16T00:00:00Z",
                "message": "one wording"}
        same = dict(item, message="different wording", title="another")
        self.assertEqual(attention_identity(item), attention_identity(same))
        self.assertNotEqual(attention_identity(item), attention_identity(dict(item, recorded_at="later")))


class IntegrationRouteTests(BotTestCase):
    """The loopback HTTP surface n8n drives: authenticated, bounded, and unable to leak correspondence."""

    def serve(self):
        import urllib.request  # noqa: F401 - keeps the import list honest for the helper below

        from http.server import ThreadingHTTPServer

        from orchestration.api import make_handler

        token = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"
        handler = make_handler(JobService(self.store), token, repository_root=self.root)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, *, method="GET", body=None, authenticated=True, headers=None):
            import urllib.error
            import urllib.request

            payload = None if body is None else json.dumps(body).encode()
            request = urllib.request.Request(base + path, data=payload, method=method,
                                            headers={**({"Authorization": f"Bearer {token}"} if authenticated else {}),
                                                     **({"Content-Type": "application/json"} if payload else {}),
                                                     **(headers or {})})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8")
                try:
                    return error.code, json.loads(raw)
                except json.JSONDecodeError:
                    return error.code, {"raw": raw}

        return call

    def test_status_reports_configuration_without_contacting_a_provider(self) -> None:
        call = self.serve()
        status, payload = call("/integrations/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["verified"])
        self.assertFalse(payload["telegram"]["configured"])
        self.assertEqual(payload["email"]["scope"].rsplit("/", 1)[-1], "gmail.readonly")
        self.assertFalse(payload["automation_hold"]["held"])
        unauthenticated = call("/integrations/status", authenticated=False)
        self.assertEqual(unauthenticated[0], 401, "the same bearer token gates this as every other route")

    def test_an_email_poll_needs_a_configured_mailbox_and_says_which_variable_is_missing(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            status, payload = self.serve()("/integrations/email/poll", method="POST", body={})
        self.assertEqual((status, payload["error_code"]), (400, "GMAIL_NOT_CONFIGURED"))
        self.assertIn("AI_WEBSITE_AGENT_GMAIL_REFRESH_TOKEN", payload["error"])

    def test_a_field_that_would_choose_which_mail_to_download_is_refused(self) -> None:
        call = self.serve()
        status, payload = call("/integrations/email/poll", method="POST",
                              body={"query": "label:UNREAD has:attachment"})
        self.assertEqual((status, payload["error_code"]), (400, "INTEGRATION_PARAMETER_NOT_PERMITTED"))
        self.assertIn("query", payload["error"])

    def test_a_held_system_contacts_no_mailbox(self) -> None:
        set_hold(self.store, actor="test", reason="freeze")
        with mock.patch.dict("os.environ", {}, clear=True):
            status, payload = self.serve()("/integrations/email/poll", method="POST", body={})
        self.assertEqual((status, payload["error_code"]), (409, "AUTOMATION_HELD"))

    def test_a_successful_cycle_answers_in_counts_and_codes_not_in_message_text(self) -> None:
        client = FakeGmailClient([gmail_message()])
        poller = EmailInboundPoller(client, store=self.store)
        with mock.patch("orchestration.api.build_email_poller", return_value=poller):
            status, payload = self.serve()("/integrations/email/poll", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertEqual(payload["recorded"], 1)
        self.assertEqual(payload["results"][0]["lead_id"], self.lead["lead_id"])
        self.assertNotIn("excerpt", json.dumps(payload))
        self.assertNotIn("send me the details", json.dumps(payload))

    def test_an_oversized_cycle_body_is_refused_before_it_is_read(self) -> None:
        from http.server import ThreadingHTTPServer
        import socket

        from orchestration.api import make_handler

        handler = make_handler(JobService(self.store), "TEST_FIXTURE_TOKEN", repository_root=self.root)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        port = httpd.server_address[1]
        # A declared body far larger than the limit, with only a few bytes actually sent: if the answer is 413
        # while the body is still in flight, the size check demonstrably ran before the read.
        raw = ("POST /integrations/email/poll HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Authorization: Bearer TEST_FIXTURE_TOKEN\r\nContent-Type: application/json\r\n"
               f"Content-Length: {3 * 1024 * 1024}\r\n\r\n{{}}").encode()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(raw)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65_536)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks).decode("utf-8", errors="replace")
        self.assertIn("413", response.splitlines()[0])
        self.assertIn("REQUEST_BODY_TOO_LARGE", response)
        payload = json.dumps({"max_messages": "8"})
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(("POST /integrations/email/poll HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                          "Authorization: Bearer TEST_FIXTURE_TOKEN\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(payload)}\r\n\r\n{payload}").encode())
            small = sock.recv(65_536).decode("utf-8", errors="replace")
        self.assertNotIn("413", small.splitlines()[0], "a small body reaches the handler normally")

    def test_the_notify_route_refuses_without_a_bot_and_reports_the_button_state_otherwise(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            status, payload = self.serve()("/integrations/telegram/notify", method="POST", body={})
        self.assertEqual((status, payload["error_code"]), (409, "TELEGRAM_NOT_CONFIGURED"))
        api = FakeTelegramApi()
        environment = {"AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN": "1:TEST",
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": str(OPERATOR),
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": str(CHAT)}
        self.store.state["jobs"]["job-9"] = {"job_id": "job-9", "job_type": "BUILD", "status": "FAILED",
                                             "error_code": "WORKER_FAILED", "error": "fault", "attempts": 3,
                                             "max_attempts": 3, "lead_id": None, "project_id": None,
                                             "updated_at": "2026-10-16T00:00:00Z",
                                             "completed_at": "2026-10-16T00:00:00Z"}
        self.store._save()
        with mock.patch.dict("os.environ", environment, clear=True), \
                mock.patch("integrations.telegram.TelegramBotApi", lambda *a, **k: api):
            status, payload = self.serve()("/integrations/telegram/notify", method="POST", body={"limit": 3})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["notified"], 1)
        self.assertEqual(len(api.sent), 1)
        self.assertNotIn("1:TEST", json.dumps(api.sent))


class N8nWorkflowTests(BotTestCase):
    workflow_path = Path("n8n/workflows/06-email-cycle-and-operator-notification.json")

    def read(self):
        return json.loads(self.workflow_path.read_text(encoding="utf-8"))

    def test_the_export_is_importable_inactive_and_credential_free(self) -> None:
        from orchestration.n8n_runner import validate_n8n_workflow_json

        document = self.read()
        self.assertEqual(validate_n8n_workflow_json(self.workflow_path).get("name"), document["name"])
        self.assertIs(document["active"], False)
        text = self.workflow_path.read_text(encoding="utf-8")
        self.assertNotIn("REPLACE_WITH", text)
        for secret in ("Bearer ey", "xoxb", "ya29", "AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN=",
                       "GMAIL_REFRESH_TOKEN"):
            self.assertNotIn(secret, text, secret)
        self.assertIn("{{ $env.AI_WEBSITE_AGENT_API_TOKEN }}", text)

    def test_the_graph_has_no_messaging_or_writing_node_and_only_known_types(self) -> None:
        document = self.read()
        types = {node["type"] for node in document["nodes"]}
        self.assertTrue(types <= {"n8n-nodes-base.manualTrigger", "n8n-nodes-base.set",
                                 "n8n-nodes-base.httpRequest", "n8n-nodes-base.if"}, types)
        for node in document["nodes"]:
            url = str((node.get("parameters") or {}).get("url") or "")
            self.assertTrue(url.endswith("/integrations/email/poll") or url.endswith("/integrations/telegram/notify")
                           or not url, url)
            self.assertNotIn("engagement/inbound", url, "this workflow schedules a cycle; it does not write replies")
            method = (node.get("parameters") or {}).get("method")
            if node["type"].endswith("httpRequest"):
                self.assertIn(method, {"POST"}, "the two application routes this workflow may call")

    def test_the_local_runner_executes_the_cycle_against_the_real_handler(self) -> None:
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from orchestration.api import make_handler
        from orchestration.n8n_runner import N8nWorkflowRunner

        client = FakeGmailClient([gmail_message()])
        poller = EmailInboundPoller(client, store=self.store)
        token = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"
        handler = make_handler(JobService(self.store), token, repository_root=self.root)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        api = FakeTelegramApi()
        environment = {"AI_WEBSITE_AGENT_API_URL": base, "AI_WEBSITE_AGENT_API_TOKEN": token,
                       "AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN": "1:TEST",
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": str(OPERATOR),
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": str(CHAT)}
        with mock.patch("orchestration.api.build_email_poller", return_value=poller), \
                mock.patch.dict("os.environ", environment, clear=True), \
                mock.patch("integrations.telegram.TelegramBotApi", lambda *a, **k: api):
            runner = N8nWorkflowRunner(self.workflow_path, env=environment)
            result = runner.execute({}, execution_id="phase48-1")
        self.assertEqual(result["errors"], [], json.dumps(result, indent=1)[:1200])
        outputs = json.dumps(result, default=str)
        self.assertEqual(result["final_output"]["outcome"], "operator notified about the new mail")
        self.assertGreaterEqual(len(api.sent), 1, "the operator was told once by the application, not by n8n")
        self.assertEqual(len(self.store.state["inbound_events"]), 1)
        self.assertNotIn("send me the details", outputs)


class CliTests(BotTestCase):
    def orchestration_cli(self, *argv):
        from orchestration.cli import main

        buffer, error_buffer = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", buffer), mock.patch("sys.stderr", error_buffer):
            code = main(["--state-path", str(self.store.path), *argv])
        return code, buffer.getvalue(), error_buffer.getvalue()

    def tracking_cli(self, *argv):
        from tracking.cli import run_cli

        out, err = io.StringIO(), io.StringIO()
        code = run_cli(["--state-path", str(self.store.path), *argv], stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_the_brake_is_operable_from_the_command_line(self) -> None:
        code, stdout, _ = self.orchestration_cli("hold-automation", "--actor", "grace", "--reason", "deploy window")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["held"])
        code, stdout, _ = self.orchestration_cli("automation-status")
        self.assertEqual(json.loads(stdout)["set_by"], "grace")
        code, stdout, _ = self.orchestration_cli("release-automation", "--actor", "grace")
        self.assertFalse(json.loads(stdout)["held"])
        with self.assertRaises(SystemExit) as raised:
            self.orchestration_cli("hold-automation", "--reason", "no actor")
        self.assertEqual(raised.exception.code, 2, "an unattributed hold is not a record anybody can undo")

    def test_checks_report_configuration_and_only_send_when_asked_to_probe(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.orchestration_cli("telegram-check")
            self.assertEqual(code, 1)
            report = json.loads(stdout)
            self.assertFalse(report["requests_sent"])
            self.assertEqual(len(report["commands_supported"]), len(COMMANDS))
            code, stdout, _ = self.orchestration_cli("email-check")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout)["code"], "GMAIL_NOT_CONFIGURED")

    def test_telegram_command_runs_one_command_offline(self) -> None:
        environment = {"AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": str(OPERATOR),
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": str(CHAT)}
        with mock.patch.dict("os.environ", environment, clear=True):
            code, stdout, _ = self.orchestration_cli("telegram-command", "--command-text", "/leads",
                                                      "--user-id", str(OPERATOR), "--chat-id", str(CHAT))
        self.assertEqual(code, 0, stdout)
        answer = json.loads(stdout)
        self.assertEqual(answer["code"], "LEADS")
        self.assertFalse(answer["delivered"], "no --telegram flag means nothing is sent")
        with mock.patch.dict("os.environ", environment, clear=True):
            code, stdout, _ = self.orchestration_cli("telegram-command", "--command-text", "/leads",
                                                      "--user-id", str(OTHER), "--chat-id", str(CHAT))
        self.assertEqual(code, 1, "an unlisted id is an error exit, not a silent no")
        self.assertEqual(json.loads(stdout)["code"], "AUTHORIZATION_REFUSED")
        with mock.patch.dict("os.environ", {"AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": "",
                                           "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": ""}, clear=True):
            code, stdout, err = self.orchestration_cli("telegram-command", "--command-text", "/status")
        self.assertEqual(code, 1, "an empty allow-list authorizes nobody, which is not the same as authorizing all")
        self.assertIn("allow-list", (stdout + err).lower())

    def test_alert_delivery_dedupe_is_persisted_for_a_one_shot_run(self) -> None:
        self.store.state["jobs"]["job-cli"] = {"job_id": "job-cli", "job_type": "DEPLOY", "status": "FAILED",
                                              "error_code": "RETRY_LIMIT_REACHED", "error": "exhausted",
                                              "attempts": 3, "max_attempts": 3,
                                              "updated_at": "2026-10-16T00:00:00Z"}
        self.store._save()
        api = FakeTelegramApi()
        environment = {"AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN": "1:TEST",
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": str(CHAT),
                       "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": str(OPERATOR)}
        with mock.patch.dict("os.environ", environment, clear=True), \
                mock.patch("orchestration.cli._telegram_api", lambda config: api):
            code, stdout, _ = self.orchestration_cli("check-alerts", "--telegram", "--notify")
            self.assertEqual(code, 1)
            report = json.loads(stdout)
            self.assertEqual(report["sinks"], ["log", "telegram"],
                             "--telegram adds a sink, it does not replace the durable log line")
            self.assertGreaterEqual(report["notified"], 1)
            self.assertEqual(report["suppression_persisted"], len(report["alerts"]))
            code, stdout, _ = self.orchestration_cli("check-alerts", "--telegram")
            self.assertEqual(json.loads(stdout)["notified"], 0,
                             "a second one-shot run must not re-page the same recorded condition")
        self.assertEqual(len(api.sent), 1)
        self.assertNotIn("1:TEST", json.dumps(api.sent))

    def test_the_inbound_cli_and_the_email_adapter_write_the_same_shape(self) -> None:
        payload = self.root / "event.json"
        payload.write_text(json.dumps({"provider": "EMAIL_INBOUND", "event_id": "cli-1", "channel": "EMAIL",
                                       "lead_id": self.lead["lead_id"], "sender": "aisha@pitstop.ng",
                                       "body": "Yes, interested in seeing the demo."}), encoding="utf-8")
        code, stdout, _ = self.tracking_cli("inbound-record", "--from-file", str(payload))
        self.assertEqual(code, 0)
        manual = json.loads(stdout)
        report = EmailInboundPoller(self.fake_gmail([gmail_message()]), store=self.store).poll_once()
        row = self.store.state["inbound_events"][report["results"][0]["dedupe_key"]]
        for key in ("disposition", "provider", "linkage_basis", "sender_identity", "excerpt"):
            self.assertIn(key, manual, key)
            self.assertIn(key, row, key)
        self.assertEqual(manual["disposition"], row["disposition"])


if __name__ == "__main__":
    unittest.main()
