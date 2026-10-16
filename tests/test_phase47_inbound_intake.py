"""Phase 47: authorized inbound communications - the intake, its safeguards, and its hand-offs.

These tests describe what a business's reply may and may not do to this system. Everything here is fictional
`TEST_FIXTURE` data in a temporary directory; no mailbox, webhook, or social account is connected, no message
is sent to anyone, and no live provider API is called.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestration.jobs import JobService  # noqa: E402
from tracking.database import RelationalStateBackend  # noqa: E402
from tracking.engagement import EngagementLedger, LedgerError  # noqa: E402
from tracking.inbound import (  # noqa: E402
    TRIAGE_NEXT_ACTIONS,
    DELIVERY_RECEIPT_PROVIDERS,
    FORBIDDEN_FIELDS,
    INBOUND_PROVIDERS,
    INBOUND_SIGNATURE_HEADER,
    MAX_BODY_CHARS,
    InboundEventError,
    InboundIntake,
    compute_inbound_signature,
    normalize_event,
    signature_mismatch,
    triage,
    verify_inbound_signature,
)
from tracking.store import TrackingStore, _empty_state  # noqa: E402

SENTENCE = "Hi, thanks for the quote. How much would a one-page site cost, and could we launch before December?"


class InboundTestCase(unittest.TestCase):
    """One fictional lead, one tracked draft, one intake over the same store the ledger uses."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_path = self.root / "state.json"
        self.store = TrackingStore(self.state_path)
        self.lead = self.store.create_lead({
            "business_name": "Fictional Pitstop Test",
            "business_type": "Motors",
            "city": "Lagos",
            "country": "Nigeria",
            "data_classification": "TEST_FIXTURE",
            "qualification_status": "QUALIFIED",
            "website_status": "WEBSITE_NEEDS_IMPROVEMENT",
            "source_urls": ["https://directory.example.invalid/pitstop"],
            "demo": True,
        })
        self.project = self.store.create_project(self.lead["lead_id"])
        self.ledger = EngagementLedger(self.store, source="pytest")
        self.intake = InboundIntake(self.ledger)

    def tearDown(self) -> None:
        self.root.cleanup() if hasattr(self.root, "cleanup") else shutil.rmtree(self.root, ignore_errors=True)

    def draft(self, outreach_id: str = "outreach-0001", *, status: str = "READY_TO_SEND", channel: str = "EMAIL") -> dict:
        record = {
            "outreach_id": outreach_id, "project_id": self.project["project_id"], "lead_id": self.lead["lead_id"],
            "build_id": None, "deployment_id": None, "channel": channel, "recipient": "hidden@test.invalid",
            "draft_message": "draft", "status": status, "generated_at": "2026-10-01T00:00:00Z",
        }
        self.store.state["outreach"][outreach_id] = record
        self.store._save()  # noqa: SLF001
        return record

    def event(self, **overrides) -> dict:
        payload = {"provider": "EMAIL_INBOUND", "event_id": "mid-4401", "channel": "EMAIL",
                   "lead_id": self.lead["lead_id"], "body": SENTENCE}
        payload.update(overrides)
        return {key: value for key, value in payload.items() if value is not None}

    def refusals(self, payload: dict) -> str:
        """Assert the intake refuses one payload and hand back the code it refused with."""
        with self.assertRaises(InboundEventError) as raised:
            self.intake.process(payload)
        return raised.exception.code or ""


class IntakeContractTests(InboundTestCase):
    def test_a_provider_event_becomes_a_ledger_response_with_its_own_provenance(self) -> None:
        self.draft()
        result = self.intake.process(self.event(occurred_at="2026-10-14T09:30:00Z", conversation_id="conv-55"),
                                     received_at="2026-10-14T09:34:00Z")
        self.assertEqual(result["disposition"], "RECORDED")
        self.assertEqual(result["lead_id"], self.lead["lead_id"])
        self.assertEqual(result["linkage_basis"], "lead_id")
        self.assertEqual(result["provider"], "EMAIL_INBOUND")
        # The business's send time and this server's receive time are different facts and stay different.
        self.assertEqual(result["event_occurred_at"], "2026-10-14T09:30:00Z")
        self.assertEqual(result["event_timestamp_source"], "provider")
        self.assertNotEqual(result["received_at"], result["event_occurred_at"])
        self.assertEqual(result["idempotency"], "provider_event_id")
        row = self.store.state["engagements"][result["engagement_id"]]
        self.assertEqual(row["kind"], "RESPONSE_RECEIVED")
        self.assertEqual(row["direction"], "INBOUND")
        # The ledger keeps the provider in its own lower-case vocabulary; nothing is invented about it.
        self.assertEqual(row["provider"], "email_inbound")
        self.assertEqual(row["external_message_id"], "mid-4401")
        self.assertEqual(row["conversation_id"], "conv-55")
        self.assertIsNone(row["outreach_id"], "this event arrived without a draft reference")
        self.assertFalse(row["provider_verified"], "an inbound feed cannot certify delivery")
        self.assertEqual(row["classification_source"], "operator_asserted")
        # No signing secret exists in this test, and the record says so instead of going silent about it.
        self.assertIs(result["signature"]["verified"], False)
        self.assertIn("no signing secret", result["signature"]["reason"])

    def test_repeated_delivery_of_one_event_cannot_double_count(self) -> None:
        self.intake.process(self.event(occurred_at="2026-10-14T09:30:00Z"))
        # A provider re-delivery is the same event id with different noise around it: a fresh timestamp, a
        # reworded summary. None of that may turn one reply into two.
        second = self.intake.process(self.event(occurred_at="2026-10-14T09:59:00Z", summary="same mail, resent"))
        self.assertIs(second["duplicate"], True)
        self.assertEqual(second["engagement_id"], self.intake.process(self.event())["engagement_id"])
        self.assertEqual(len(self.store.state["engagements"]), 1)
        self.assertEqual(len(self.store.state["inbound_events"]), 1)
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage"], "RESPONDED")

    def test_distinct_messages_in_one_thread_stay_distinct_records(self) -> None:
        first = self.intake.process(self.event(conversation_id="conv-9"))
        second = self.intake.process(self.event(event_id="mid-4402", conversation_id="conv-9",
                                                body="Also, do you handle hosting?"))
        self.assertNotEqual(first["engagement_id"], second["engagement_id"])
        self.assertEqual(len(self.store.state["engagements"]), 2)
        rows = sorted(self.store.state["engagements"].values(), key=lambda row: row["engagement_id"])
        self.assertEqual([row["conversation_id"] for row in rows], ["conv-9", "conv-9"])
        self.assertEqual([row["external_message_id"] for row in rows], ["mid-4401", "mid-4402"])

    def test_a_payload_cannot_assert_an_approval_an_outcome_or_a_delivery(self) -> None:
        before = json.dumps(self.store.state, sort_keys=True)
        for field in sorted(FORBIDDEN_FIELDS):
            with self.subTest(field=field):
                self.assertEqual(self.refusals(self.event(**{field: "WON"})), "INBOUND_FORBIDDEN_FIELD")
        self.assertEqual(json.dumps(self.store.state, sort_keys=True), before,
                         "a refused payload must not reach the state file at all")

    def test_a_classification_hint_is_kept_as_a_hint_and_never_decides(self) -> None:
        result = self.intake.process(self.event(classification="interested"))
        self.assertEqual(result["classification_hint"], "INTERESTED")
        # The rules read an enquiry about price and a launch date, and say so; the hint did not overwrite that.
        self.assertEqual(result["triage"]["category"], "PROPOSAL_QUESTION")
        self.assertIs(result["classification_agrees"], False)
        row = self.store.state["engagements"][result["engagement_id"]]
        # The hint did not choose the ledger's classification either: the rules' reading is what was recorded.
        self.assertEqual(row["classification"], "QUESTION")
        self.assertEqual(row["classification_source"], "operator_asserted")
        # ...and the intake cannot accept a proposal or advance the relationship by assertion.
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage"], "RESPONDED")

    def test_triage_reports_the_phrases_it_matched_and_admits_when_it_is_guessing(self) -> None:
        expected = {
            "we would like to go ahead": "INTERESTED",
            "send me the details please": "INFORMATION_REQUEST",
            "let's meet on thursday": "MEETING_REQUEST",
            "how much would the proposal cost": "PROPOSAL_QUESTION",
            "not interested thanks": "NOT_INTERESTED",
            "please stop emailing us": "OPT_OUT",
            "hello there": "UNCLEAR",
        }
        for text, category in expected.items():
            with self.subTest(text=text):
                report = triage(text)
                self.assertEqual(report["category"], category)
                self.assertIn(report["certainty"], {"clear", "ambiguous", "mixed", "none"})
                # Provenance: the reader can see which phrase did the work, and that no model was involved.
                self.assertIsInstance(report["matched_phrases"], list)
                self.assertEqual(report["source"], "rule_baseline")
        unclear = triage("hello there")
        self.assertEqual(unclear["matched_phrases"], [])
        self.assertEqual(unclear["certainty"], "none")
        # A suggestion, never an action: no next action tells the system to contact anybody.
        for category, action in TRIAGE_NEXT_ACTIONS.items():
            for phrase in ("send it", "send a", "send the", "reply now", "auto"):
                self.assertNotIn(phrase, action.lower(), f"{category}: {action}")
        self.assertEqual(self.intake.process(self.event(body="hello there"))["next_action"],
                         TRIAGE_NEXT_ACTIONS["UNCLEAR"])

    def test_triage_offers_no_invented_confidence_number(self) -> None:
        report = triage(SENTENCE)
        self.assertNotIn("score", report)
        self.assertNotIn("confidence", report)
        self.assertIn(report["certainty"], {"clear", "ambiguous", "mixed", "none"})

    def test_the_provider_contract_is_enforced_at_the_door(self) -> None:
        cases = {
            # A provider with a stable message id must use it, or a re-delivery is indistinguishable.
            "event_id_missing": (self.event(event_id=None), "EVENT_ID_REQUIRED"),
            "unknown_provider": (self.event(provider="CARRIER_PIGEON"), "PROVIDER_UNSUPPORTED"),
            "no_channel": (self.event(channel=None), "CHANNEL_REQUIRED"),
            "invented_channel": (self.event(channel="CARRIER_PIGEON"), "CHANNEL_UNSUPPORTED"),
            "nothing_said": (self.event(body=None), "EVENT_EMPTY"),
            "oversized_body": (self.event(body="x" * (MAX_BODY_CHARS + 1)), "EVENT_BODY_TOO_LARGE"),
            "unreadable_timestamp": (self.event(occurred_at="not a date"), "EVENT_TIMESTAMP_INVALID"),
            "credential_in_detail": (self.event(body="my db is postgres://svc:sup3rs3cret@db.internal:5432/prod"),
                                     "DETAIL_MAY_CONTAIN_SECRET"),
            "malformed_lead_id": (self.event(lead_id="lead'); DROP", ), "LINKAGE_ID_INVALID"),
            "not_an_object": (self.event(event_id=12345), ""),
        }
        for name, (payload, code) in cases.items():
            with self.subTest(case=name):
                if code:
                    self.assertEqual(self.refusals(payload), code)
                else:
                    # A number for an id is still an identity; it is normalised, not refused.
                    self.assertEqual(self.intake.process(payload)["disposition"], "RECORDED")
        # Where the provider has no id, identity is derived and labelled as weaker - never presented as proof.
        derived = self.intake.process(self.event(provider="GENERIC_WEBHOOK", event_id=None))
        self.assertEqual(derived["idempotency"], "derived_from_content")
        self.assertEqual(derived["disposition"], "RECORDED")

    def test_a_provider_timestamp_ahead_of_this_clock_is_kept_as_a_claim_not_as_history(self) -> None:
        # Mail servers run fast all the time; that is no reason to lose a reply, and no reason to write a
        # 2099 event into a business's timeline either. Both facts are kept, labelled.
        result = self.intake.process(self.event(occurred_at="2099-01-01T00:00:00Z"),
                                     received_at="2026-10-14T09:00:00Z")
        self.assertEqual(result["event_timestamp_source"], "rejected_future_claim")
        self.assertEqual(result["event_timestamp_claim"], "2099-01-01T00:00:00Z")
        self.assertEqual(result["event_occurred_at"], "2026-10-14T09:00:00Z")
        row = self.store.state["engagements"][result["engagement_id"]]
        self.assertEqual(row["occurred_at"], "2026-10-14T09:00:00Z")

    def test_an_unmatchable_message_is_parked_as_unmatched_and_attached_to_nobody(self) -> None:
        self.draft()
        stray = self.event(lead_id=None, summary="someone wrote in about a website", conversation_id="conv-77")
        result = self.intake.process(stray)
        self.assertEqual(result["disposition"], "UNMATCHED")
        self.assertIsNone(result["lead_id"])
        self.assertIsNone(result["engagement_id"])
        self.assertEqual(self.store.state["engagements"], {},
                         "an unmatchable message must not enter any business's history")
        self.assertEqual(len(self.store.state["inbound_events"]), 1)
        backlog = self.intake.pending()
        self.assertEqual(backlog["total_matching"], 1)
        self.assertEqual(backlog["items"][0]["disposition"], "UNMATCHED")
        self.assertIn("inbound-pending", backlog["items"][0]["next_action"])
        # The same message, later sent with a real reference, links - and the parked row says it was superseded.
        # The same thread, re-sent with a real reference. The parked row is not deleted - the intake keeps
        # the fact that it arrived unlinked - but it stops being live work.
        linked = self.intake.process({**stray, "lead_id": self.lead["lead_id"], "event_id": "mid-4402"})
        self.assertEqual(linked["disposition"], "RECORDED")
        parked = self.store.state["inbound_events"][result["dedupe_key"]]
        self.assertEqual(parked["disposition"], "UNMATCHED")
        self.assertEqual(parked["superseded_by"], linked["dedupe_key"])
        # And re-delivering the original stray event is still a duplicate, not a second item in the backlog.
        self.assertEqual(self.intake.pending()["total_matching"], 0)
        self.assertEqual(len(self.store.state["engagements"]), 1)
        self.assertIn("inbound-pending", result["next_action"])

    def test_a_spoofed_provider_reference_cannot_move_another_leads_history(self) -> None:
        self.draft()
        other = self.store.create_lead({"business_name": "Other Fictional Shop", "city": "Abuja",
                                        "country": "Nigeria", "data_classification": "TEST_FIXTURE"})
        # An id that exists is honoured; one that does not is refused. Neither may be guessed at.
        result = self.intake.process(self.event(lead_id=other["lead_id"], body="we would like to meet next week"))
        self.assertEqual(result["lead_id"], other["lead_id"])
        self.assertEqual(self.ledger.ledger(other["lead_id"])["relationship"]["stage"], "RESPONDED")
        self.assertEqual([row for row in self.store.state["engagements"].values()
                           if row["lead_id"] == self.lead["lead_id"]], [])
        # A reference that does not exist is a *recorded* failure, not a raised one: the payload is legal, the
        # data behind it is not, and the operator has to be able to see that something arrived.
        broken = self.intake.process(self.event(event_id="mid-4450", outreach_id="outreach-9999"))
        self.assertEqual((broken["disposition"], broken["error_code"]), ("FAILED", "UNKNOWN_OUTREACH"))
        self.assertEqual(self.intake.pending()["counts_by_disposition"].get("FAILED"), 1)
        self.assertEqual(self.store.state["engagements"][result["engagement_id"]]["lead_id"], other["lead_id"])

    def test_no_outbound_channel_and_no_receipt_is_reachable_from_here(self) -> None:
        self.draft()
        source = Path("tracking/inbound.py").read_text(encoding="utf-8")
        for forbidden in ("urlopen", "subprocess", "socket", "import requests", "from outreach", "from deployment",
                          "from orchestration", "send_message", "mark_sent", "mark_outreach_sent",
                          "OUTREACH_SENT"):
            self.assertNotIn(forbidden, source, forbidden)
        # Delivery receipts stay where Phase 46 left them: nothing in this repository can produce one.
        self.assertEqual(DELIVERY_RECEIPT_PROVIDERS, ())
        self.assertTrue(all(not meta.get("delivery_receipts") for meta in INBOUND_PROVIDERS.values()))
        self.intake.process(self.event())
        for row in self.store.state["engagements"].values():
            self.assertFalse(row["provider_verified"])
        self.assertEqual(self.store.state["outreach"]["outreach-0001"]["status"], "READY_TO_SEND",
                         "a reply does not advance the draft it answers")


class ConsentTests(InboundTestCase):
    def test_a_clear_opt_out_applies_the_existing_policy_through_the_ledger(self) -> None:
        self.draft()
        follow_up = self.ledger.schedule_follow_up(self.lead["lead_id"], channel="EMAIL",
                                                   due_at="2026-11-02T09:00:00Z", reason="chase the quote")
        result = self.intake.process(self.event(body="Please stop contacting us and remove me from your list."))
        self.assertEqual(result["disposition"], "OPT_OUT_RECORDED")
        self.assertEqual(result["triage"]["category"], "OPT_OUT")
        self.assertEqual(result["triage"]["certainty"], "clear")
        self.assertIsNotNone(result["opt_out"]["block_id"])
        self.assertIn(follow_up["follow_up_id"], result["opt_out"]["cancelled_follow_ups"])
        self.assertIsNotNone(self.ledger.is_blocked(self.lead["lead_id"], "EMAIL"))
        self.assertIsNotNone(self.ledger.is_blocked(self.lead["lead_id"], "SMS"),
                            "an all-channel request blocks every channel, not just the one it arrived on")
        verdict = self.ledger.outreach_eligibility(self.lead["lead_id"], channel="EMAIL", for_action="SEND")
        self.assertFalse(verdict["eligible"])
        self.assertEqual(self.intake.pending()["total_matching"], 0,
                         "an honoured opt-out is not left sitting in the backlog")

    def test_an_ambiguous_opt_out_is_a_review_item_and_blocks_nothing(self) -> None:
        self.draft()
        result = self.intake.process(self.event(body="unsubscribe us for now, but keep me posted on design work"))
        self.assertEqual(result["disposition"], "OPT_OUT_UNCERTAIN")
        self.assertEqual(self.store.state["contact_blocks"], {})
        self.assertIsNone(self.ledger.is_blocked(self.lead["lead_id"], "EMAIL"))
        self.assertTrue(result["opt_out"]["flagged"])
        self.assertIn("hedged or contradicted", result["opt_out"]["basis"])
        item = self.intake.pending()["items"][0]
        self.assertEqual(item["disposition"], "OPT_OUT_UNCERTAIN")
        self.assertIn("confirm the opt-out", str(item["next_action"]))
        backlog = self.intake.operator_backlog()
        self.assertEqual(backlog["opt_out_uncertain"], 1)

    def test_a_provider_confirmed_unsubscribe_without_customer_words_covers_only_its_channel(self) -> None:
        self.draft()
        # No opt-out phrase of its own: only the provider's record of the customer's own click.
        result = self.intake.process(self.event(body="he said he clicked a link in our mail tool",
                                                 unsubscribe_confirmed=True))
        self.assertEqual(result["disposition"], "OPT_OUT_RECORDED")
        block = self.store.state["contact_blocks"][result["opt_out"]["block_id"]]
        self.assertEqual(block["channel"], "EMAIL", "the provider only knows about the channel it operates")
        self.assertIsNone(self.ledger.is_blocked(self.lead["lead_id"], "SMS"))
        self.assertIn("provider", str(result["opt_out"]["basis"]).lower())


class DurabilityTests(InboundTestCase):
    def test_a_persisted_failure_is_visible_and_stays_retryable(self) -> None:
        self.draft()
        payload = self.event()
        original = self.store._save  # noqa: SLF001
        state = {"fail": True}

        def flaky_save(*args, **kwargs):
            if state["fail"]:
                from tracking.database import DatabasePersistenceError

                raise DatabasePersistenceError("simulated storage fault (no connection string)")
            return original(*args, **kwargs)

        self.store._save = flaky_save  # noqa: SLF001
        self.addCleanup(setattr, self.store, "_save", original)
        with self.assertRaises(Exception) as raised:
            self.intake.process(payload)
        self.assertIn("DatabasePersistenceError", type(raised.exception).__name__)
        state["fail"] = False
        # The failure row has to survive the rollback of everything else, or a retry would look like a
        # first attempt and the operator would never know the event arrived at all.
        self.intake.process(payload)
        row = self.store.state["inbound_events"][self.intake.process(payload)["dedupe_key"]]
        self.assertEqual(row["disposition"], "RECORDED")
        self.assertGreaterEqual(row["attempts"], 1)
        self.assertEqual(len(self.store.state["engagements"]), 1, "the retry must not record a second response")

    def test_an_unroutable_event_is_recorded_as_failed_not_forgotten(self) -> None:
        result = self.intake.process(self.event(lead_id="lead-9999"))
        self.assertEqual(result["disposition"], "FAILED")
        self.assertEqual(result["error_code"], "UNKNOWN_LEAD")
        self.assertEqual(result["attempts"], 1)
        item = self.intake.pending()["items"][0]
        self.assertEqual(item["error_code"], "UNKNOWN_LEAD")
        again = self.intake.process(self.event(lead_id="lead-9999"))
        self.assertEqual(again["attempts"], 2, "a retry is counted, not hidden")

    def test_intake_rows_persist_and_are_declared_where_persistence_is_decided(self) -> None:
        result = self.intake.process(self.event())
        reloaded = TrackingStore(self.state_path)
        self.assertIn(result["dedupe_key"], reloaded.state["inbound_events"])
        self.assertEqual(reloaded.state["inbound_events"][result["dedupe_key"]]["disposition"], "RECORDED")
        self.assertEqual(len(reloaded.state["engagements"]), 1)
        self.assertIn("inbound_events", _empty_state())
        self.assertIn("inbound_events", RelationalStateBackend.KV_PERSISTED_COLLECTIONS)
        # It is intake accounting, not ledger history: the seventh collection stays out of the guard's scope.
        self.assertNotIn("inbound_events", EngagementLedger.OWNED_COLLECTIONS)
        self.assertEqual(len(EngagementLedger.OWNED_COLLECTIONS), 6)


class AttentionTests(InboundTestCase):
    def items(self):
        from orchestration.health import engagement_attention_items

        return engagement_attention_items(self.store)

    def test_operator_attention_lists_the_work_the_intake_cannot_finish(self) -> None:
        from orchestration.health import operator_attention

        self.draft()
        self.intake.process(self.event(lead_id=None, summary="a stranger wrote in"))
        self.intake.process(self.event(event_id="mid-1", body="unsubscribe us for now, but keep me posted"))
        self.intake.process(self.event(event_id="mid-2", lead_id="lead-9999"))
        self.intake.process(self.event(event_id="mid-3", body="maybe, we will see"))
        self.ledger.schedule_follow_up(self.lead["lead_id"], channel="EMAIL", due_at="2020-01-01T09:00:00Z",
                                       reason="overdue chase", allow_past_due=True)
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="one page, sixty thousand naira")
        self.ledger.update_proposal(proposal["proposal_id"], status="READY_FOR_PRESENTATION")
        self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="sent in the thread")
        codes = {item["code"] for item in self.items()}
        self.assertEqual(codes, {"INBOUND_UNMATCHED", "INBOUND_PROCESSING_FAILED", "OPT_OUT_NEEDS_REVIEW",
                                 "INBOUND_TRIAGE_UNCERTAIN", "PROPOSAL_AWAITING_DECISION", "FOLLOW_UP_DUE"})
        self.assertEqual({item["code"] for item in self.items() if item["kind"] == "inbound"},
                         {"INBOUND_UNMATCHED", "INBOUND_PROCESSING_FAILED", "OPT_OUT_NEEDS_REVIEW",
                          "INBOUND_TRIAGE_UNCERTAIN"})
        report = operator_attention(self.store)
        self.assertTrue(report["requires_attention"])
        self.assertEqual(report["counts"]["engagements"], len(self.items()))
        self.assertEqual(report["counts"]["inbound"], 4)
        self.assertTrue(any("nothing sends itself" in str(item["next_action"]) or "answer it yourself"
                            in str(item["next_action"]) or "Record what actually happened" in str(item["next_action"])
                            for item in report["items"]))
        self.assertIn("inbound", report["counts"]["by_kind"])
        base = {"kind", "code", "project_id", "lead_id", "build_id", "validation_id", "deployment_id",
                "status", "stage", "review_status", "message", "recorded_at", "age_seconds", "next_action"}
        for item in report["items"]:
            self.assertTrue(base <= set(item), sorted(base - set(item)))
            # The extras name the record the item is about, which is what makes the item actionable.
            self.assertTrue(set(item) - base <= {"dedupe_key", "follow_up_id", "proposal_id", "engagement_id"},
                            sorted(set(item) - base))

    def test_attention_is_bounded_deduped_and_not_an_alert(self) -> None:
        from orchestration.alerts import CRITICAL_JOB_CODES, CRITICAL_KINDS

        self.draft()
        for index in range(12):
            self.intake.process(self.event(event_id=None, provider="GENERIC_WEBHOOK", lead_id=None,
                                           body=f"stray message {index}", occurred_at=None))
        items = self.items()
        self.assertLessEqual(len(items), 40)
        self.assertEqual(len({item.get("dedupe_key") for item in items}), len(items),
                         "one event is one item, and the same event never appears twice")
        codes = {item["code"] for item in items}
        self.assertNotIn("RECORDED", codes)
        for code in codes:
            self.assertNotIn(code, CRITICAL_JOB_CODES,
                             "an unanswered enquiry is work for a human, not an outage page")
        self.assertNotIn("inbound", CRITICAL_KINDS)

    def test_attention_degrades_without_raising_when_the_ledger_cannot_be_read(self) -> None:
        from orchestration.health import engagement_attention_items

        with mock.patch.object(EngagementLedger, "outstanding_work", side_effect=RuntimeError("unreadable")):
            items = engagement_attention_items(self.store)
        self.assertEqual([item["code"] for item in items], ["ENGAGEMENT_LEDGER_UNREADABLE"])


def _start_api(store, token, root, env=None):
    from orchestration.api import make_handler

    handler = make_handler(JobService(store), token, repository_root=root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class HttpIntakeTests(InboundTestCase):
    """The route an external adapter would call, on loopback, with fictional data only."""

    token = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"

    def setUp(self) -> None:
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"AI_WEBSITE_AGENT_LOG_DIR": str(self.root / "logs")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def http(self, method, path, body=None, *, token=token, headers=None):
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                        headers={**({"Authorization": f"Bearer {token}"} if token else {}),
                                                 **({"Content-Type": "application/json"} if data else {}),
                                                 **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, {"error": raw}

    def serve(self):
        self.httpd, self.base = _start_api(self.store, self.token, self.root)
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))


class HttpRouteTests(HttpIntakeTests):
    def test_the_route_needs_the_operator_token_before_it_reads_anything(self) -> None:
        self.serve()
        before = self.state_path.read_text(encoding="utf-8")
        for headers, expected in (({"Authorization": "Bearer wrong"}, 401), ({}, 401)):
            status, payload = self.http("POST", "/engagement/inbound", self.event(),
                                        token=None if not headers else "wrong", headers=headers)
            self.assertEqual((status, payload["error"]), (expected, "Unauthorized"))
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        status, payload = self.http("POST", "/engagement/inbound", self.event(), token="TEST_FIXTURE_TOKEN_SHORT")
        self.assertEqual(status, 401)
        self.assertNotIn("TEST_FIXTURE_TOKEN_SHORT", json.dumps(payload), "a credential is never echoed back")

    def test_a_signed_delivery_is_accepted_and_an_unsigned_or_tampered_one_is_refused(self) -> None:
        secret = "TEST_FIXTURE_INBOUND_SIGNING_SECRET_0123456789"
        with mock.patch.dict(os.environ, {"AI_WEBSITE_AGENT_INBOUND_SIGNING_SECRET": secret}):
            self.serve()
            payload = self.event()
            raw = json.dumps(payload).encode("utf-8")
            unsigned = self.http("POST", "/engagement/inbound", raw)
            self.assertEqual((unsigned[0], unsigned[1]["error_code"]), (401, "INBOUND_SIGNATURE_MALFORMED"))
            wrong_key = self.http("POST", "/engagement/inbound", raw, headers={
                INBOUND_SIGNATURE_HEADER: compute_inbound_signature("other-secret-entirely", raw)})
            self.assertEqual((wrong_key[0], wrong_key[1]["error_code"]), (401, "INBOUND_SIGNATURE_INVALID"))
            tampered = json.dumps({**payload, "body": "we accept, please invoice us"}).encode("utf-8")
            edited = self.http("POST", "/engagement/inbound", tampered,
                               headers={INBOUND_SIGNATURE_HEADER: compute_inbound_signature(secret, raw)})
            self.assertEqual((edited[0], edited[1]["error_code"]), (401, "INBOUND_SIGNATURE_INVALID"))
            truncated = self.http("POST", "/engagement/inbound", raw,
                                  headers={INBOUND_SIGNATURE_HEADER: "sha256=deadbeef"})
            self.assertEqual(truncated[0], 401)
            good = self.http("POST", "/engagement/inbound", raw,
                             headers={INBOUND_SIGNATURE_HEADER: compute_inbound_signature(secret, raw)})
            self.assertEqual(good[0], 200)
            self.assertEqual(good[1]["disposition"], "RECORDED")
            self.assertTrue(good[1]["signature"]["verified"])
            self.assertNotIn(secret, json.dumps(good[1]))

    def test_a_refused_payload_stores_nothing_and_an_oversized_body_is_413(self) -> None:
        from orchestration.api import MAX_REQUEST_BYTES

        self.serve()
        status, payload = self.http("POST", "/engagement/inbound",
                                    {"provider": "EMAIL_INBOUND", "channel": "EMAIL", "body": "hello"})
        self.assertEqual((status, payload["error_code"]), (400, "EVENT_ID_REQUIRED"))
        self.assertEqual(self.store.state["inbound_events"], {})
        self.assertEqual(self.store.state["engagements"], {})
        status, payload = self.http("POST", "/engagement/inbound",
                                    {"provider": "EMAIL_INBOUND", "event_id": "mid-1", "channel": "EMAIL",
                                     "body": "x" * (MAX_REQUEST_BYTES + 10)})
        self.assertEqual((status, payload["error_code"]), (413, "REQUEST_BODY_TOO_LARGE"))
        status, payload = self.http("POST", "/engagement/inbound", b"not json", token=self.token)
        self.assertEqual(status, 400)
        # Nothing at all: the first thing the intake can say about an empty post is that it does not know
        # where the message came from, so `channel` is what it names.
        status, payload = self.http("POST", "/engagement/inbound", b"")
        self.assertEqual((status, payload["error_code"]), (400, "CHANNEL_REQUIRED"))
        status, payload = self.http("POST", "/engagement/inbound",
                                    {"provider": "GENERIC_WEBHOOK", "channel": "EMAIL"})
        self.assertEqual((status, payload["error_code"]), (400, "EVENT_EMPTY"))
        self.assertEqual(self.store.state["engagements"], {})

    def test_the_backlog_route_is_read_only_bounded_and_carries_no_message_bodies(self) -> None:
        self.serve()
        secret_line = "postgres://svc:sup3rs3cret@db.internal:5432/prod"
        self.intake.process(self.event(lead_id=None,
                                       body="please call me on +234 801 234 5678 or write ops@pitstop.test"))
        self.intake.process(self.event(lead_id="lead-9999", event_id="mid-77"))
        status, payload = self.http("GET", "/engagement/inbound")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total_matching"], 2)
        self.assertEqual(payload["counts_by_disposition"], {"UNMATCHED": 1, "FAILED": 1})
        for item in payload["items"]:
            self.assertNotIn("body", item)
            self.assertNotIn(secret_line, json.dumps(item))
            joined = json.dumps(item)
            self.assertNotIn("+234 801 234 5678", joined, "a phone number is redacted from the excerpt too")
            self.assertNotIn("ops@pitstop.test", joined, "so is an email address")
        status, payload = self.http("GET", "/engagement/inbound?limit=1")
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["total_matching"], 2, "the bound truncates the list, never the truth")
        before = self.state_path.read_text(encoding="utf-8")
        self.http("GET", "/engagement/inbound")
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        status, payload = self.http("POST", "/engagement/pipeline", {})
        self.assertEqual(status, 404, "the one engagement write stays the intake route")
        self.assertIn(payload.get("error_code"), ("ROUTE_NOT_FOUND", "LEDGER_ROUTE_NOT_FOUND"))

    def test_a_storage_fault_answers_503_and_the_event_stays_retryable(self) -> None:
        self.serve()
        original = self.store._save  # noqa: SLF001
        from tracking.database import DatabasePersistenceError

        fault = DatabasePersistenceError("connect failed for postgres://svc:sup3rs3cret@db.internal:5432/prod")
        with mock.patch.object(self.store, "_save", side_effect=fault):
            status, payload = self.http("POST", "/engagement/inbound", self.event())
        self.assertEqual((status, payload["error_code"]), (503, "STORAGE_UNAVAILABLE"))
        self.assertNotIn("sup3rs3cret", json.dumps(payload), "a connection string never rides an HTTP answer")
        self.store._save = original  # noqa: SLF001
        retry = self.http("POST", "/engagement/inbound", self.event())
        self.assertEqual(retry[0], 200)
        self.assertEqual(len(self.store.state["engagements"]), 1)

    def test_the_operational_log_carries_no_token_no_body_and_no_credential(self) -> None:
        self.serve()
        log_dir = self.root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = self.event(body="our budget is 200000 naira, api key: TEST_FIXTURE_AKIDEXAMPLEKEY123456")
        raw = json.dumps(payload).encode("utf-8")
        with mock.patch.dict(os.environ, {"AI_WEBSITE_AGENT_INBOUND_SIGNING_SECRET": "TEST_FIXTURE_SECRET_ABCDEF"}):
            self.http("POST", "/engagement/inbound", raw,
                      headers={INBOUND_SIGNATURE_HEADER: "sha256=" + "0" * 64})
        self.http("POST", "/engagement/inbound", payload)
        self.http("GET", "/engagement/inbound")
        lines = []
        for path in sorted(log_dir.glob("*.jsonl")) + sorted(log_dir.glob("*")):
            if path.is_file():
                lines.extend(path.read_text(encoding="utf-8").splitlines())
        self.assertTrue(lines, "the operational log is what an on-call human reads; it must exist to be judged")
        joined = "\n".join(lines)
        for forbidden in ("TEST_FIXTURE_TOKEN_NOT_A_SECRET", "TEST_FIXTURE_SECRET_ABCDEF", "Bearer",
                          "200000 naira", "budget", "AKIDEXAMPLEKEY123456"):
            self.assertNotIn(forbidden, joined, forbidden)
        for entry in (json.loads(line) for line in lines if line.startswith("{")):
            self.assertLessEqual(len(json.dumps(entry)), 2000)


class WorkflowAndExportTests(unittest.TestCase):
    """The exported n8n graph: importable, inert, and honest about what it does not decide."""

    workflow_path = REPO_ROOT / "n8n" / "workflows" / "05-inbound-response-intake.json"

    def test_the_exported_inbound_workflow_is_importable_and_inert(self) -> None:
        from orchestration.n8n_runner import validate_n8n_workflow_json, workflow_readiness

        data = validate_n8n_workflow_json(self.workflow_path)
        self.assertIs(data["active"], False, "an exported file is not a running workflow")
        report = workflow_readiness([self.workflow_path], env={"AI_WEBSITE_AGENT_API_URL": "http://127.0.0.1:8787",
                                                               "AI_WEBSITE_AGENT_API_TOKEN": "t"})
        entry = report["workflows"][0]
        self.assertTrue(entry["importable"], entry["problems"])
        self.assertEqual(entry["unresolved_placeholders"], [])
        self.assertEqual(entry["missing_environment"], [])
        self.assertFalse(report["requests_sent"])

    def test_the_workflow_writes_only_to_the_intake_route(self) -> None:
        """Coordination only: one HTTP node, one destination, and no messaging node anywhere in the graph."""
        data = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        http_nodes = [node for node in data["nodes"] if node.get("type") == "n8n-nodes-base.httpRequest"]
        self.assertEqual(len(http_nodes), 1, "an intake workflow that also wrote elsewhere is not an intake")
        for node in http_nodes:
            self.assertTrue(str(node["parameters"].get("url", "")).endswith("/engagement/inbound"), node["parameters"])
            self.assertIn("$env.AI_WEBSITE_AGENT_API_URL", node["parameters"]["url"])
        types = {node["type"] for node in data["nodes"]}
        allowed = {"n8n-nodes-base.manualTrigger", "n8n-nodes-base.set", "n8n-nodes-base.if",
                   "n8n-nodes-base.httpRequest"}
        self.assertTrue(types <= allowed, f"a webhook, email, or chat node here would be a sending "
                                          f"capability in disguise: {sorted(types - allowed)}")
        # The Set node is a field allow-list: only the intake's own contract may cross the boundary.
        fields = {item["name"] for item in data["nodes"][1]["parameters"]["values"].get("string", [])}
        self.assertFalse(fields & FORBIDDEN_FIELDS, fields & FORBIDDEN_FIELDS)
        self.assertTrue({"provider", "event_id", "channel", "body"} <= fields, fields)

    def test_the_exported_workflow_drives_the_real_intake_and_survives_a_re_run(self) -> None:
        from orchestration.n8n_runner import N8nWorkflowRunner

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = TrackingStore(root / "state.json")
        lead = store.create_lead({"business_name": "Fictional Cycle Cafe", "city": "Lagos", "country": "Nigeria",
                                  "data_classification": "TEST_FIXTURE", "qualification_status": "QUALIFIED",
                                  "website_status": "WEBSITE_NEEDS_IMPROVEMENT"})
        token = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"
        httpd, base = _start_api(store, token, root)
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        env = {"AI_WEBSITE_AGENT_API_URL": base, "AI_WEBSITE_AGENT_API_TOKEN": token}
        trigger = {"provider": "EMAIL_INBOUND", "event_id": "mid-n8n-1", "channel": "EMAIL", "lead_id": lead["lead_id"],
                   "body": "we would like to meet next week to talk about the site", "summary": "",
                   "occurred_at": "2026-10-14T09:30:00Z", "conversation_id": "", "outreach_id": "",
                   "project_id": "", "external_message_id": "", "message_ref": "", "signature_header": ""}
        runner = N8nWorkflowRunner(self.workflow_path, env=env)
        with redirect_stderr(io.StringIO()):
            first = runner.execute(trigger, execution_id="inbound-1")
        self.assertEqual(first["errors"], [], json.dumps(first["errors"]))
        self.assertEqual(first["final_output"]["disposition"], "RECORDED")
        self.assertEqual(first["final_output"]["lead_id"], lead["lead_id"])
        self.assertEqual(len(store.state["engagements"]), 1)
        with redirect_stderr(io.StringIO()):
            again = N8nWorkflowRunner(self.workflow_path, env=env).execute(trigger, execution_id="inbound-2")
        self.assertEqual(again["final_output"]["disposition"], "RECORDED")
        self.assertEqual(len(store.state["engagements"]), 1, "re-running the automation cannot double-count")
        # A stray message follows the other branch: operator attention, no invented linkage.
        stray = dict(trigger, event_id="mid-n8n-2", lead_id="", body="can you fix my website too?")
        with redirect_stderr(io.StringIO()):
            third = N8nWorkflowRunner(self.workflow_path, env=env).execute(stray, execution_id="inbound-3")
        self.assertEqual(third["final_output"]["disposition"], "UNMATCHED")
        self.assertIn("inbound-pending", third["final_output"]["operator_next_action"])
        self.assertEqual(len(store.state["engagements"]), 1)


class CliTests(InboundTestCase):
    def run_cli(self, *argv: str):
        from tracking.cli import run_cli

        out, err = io.StringIO(), io.StringIO()
        code = run_cli(["--state-path", str(self.state_path), *argv], stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_records_and_lists_inbound_events(self) -> None:
        self.draft()
        code, stdout, err = self.run_cli("inbound-record", "--provider", "EMAIL_INBOUND", "--event-id", "mid-cli-1",
                                         "--channel", "EMAIL", "--lead-id", self.lead["lead_id"], "--body", SENTENCE)
        self.assertEqual(code, 0, err)
        recorded = json.loads(stdout)
        self.assertEqual(recorded["disposition"], "RECORDED")
        code, stdout, err = self.run_cli("inbound-record", "--provider", "EMAIL_INBOUND", "--channel", "EMAIL",
                                         "--lead-id", self.lead["lead_id"], "--body", "hello")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.strip().splitlines()[-1])["error_code"], "EVENT_ID_REQUIRED")
        self.run_cli("inbound-record", "--provider", "GENERIC_WEBHOOK", "--channel", "OTHER", "--body", "stray")
        code, stdout, _ = self.run_cli("inbound-pending", "--limit", "5")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["total_matching"], 1)
        self.assertEqual(payload["items"][0]["disposition"], "UNMATCHED")
        # The CLI writes through the same intake, so a hand-recorded event is idempotent too.
        self.run_cli("inbound-record", "--provider", "EMAIL_INBOUND", "--event-id", "mid-cli-1", "--channel",
                     "EMAIL", "--lead-id", self.lead["lead_id"], "--body", SENTENCE)
        reloaded = TrackingStore(self.state_path)
        self.assertEqual(len(reloaded.state["engagements"]), 1,
                         "a hand-recorded event is idempotent against an adapter-recorded one")

    def test_cli_records_from_a_provider_event_file(self) -> None:
        self.draft()
        path = self.root / "event.json"
        path.write_text(json.dumps(self.event(occurred_at="2026-10-14T09:30:00Z")), encoding="utf-8")
        code, stdout, err = self.run_cli("inbound-record", "--from-file", str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(stdout)["event_occurred_at"], "2026-10-14T09:30:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
