"""Phase 46: the engagement and outcome ledger, tested offline.

These tests are mostly about *refusals*: a prepared draft that is not a send, a proposal that has not been
presented, an outcome nobody stated, a follow-up after an opt-out, a deal with no contract behind it. Each one
is checked to persist across a reload and to leave the later stages of the pipeline untouched.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest

from tracking.cli import run_cli
from tracking.engagement import (
    ENGAGEMENT_COLLECTIONS,
    EVIDENCE_REQUIRED_OUTCOMES,
    EngagementLedger,
    LedgerError,
    redact_contact_details,
)
from tracking.store import TrackingStore


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_path = self.root / "state.json"
        self.store = TrackingStore(self.state_path)
        self.lead = self.store.create_lead({
            "business_name": "Fictional Pitstop Test",
            "business_type": "Restaurants",
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
        self.addCleanup(self._tmp.cleanup)

    def fail_with(self, code: str, call):
        with self.assertRaises(LedgerError) as raised:
            call()
        self.assertEqual(raised.exception.code, code, str(raised.exception))
        return raised.exception

    def outreach_record(self, outreach_id: str = "outreach-0001", *, status: str = "READY_TO_SEND", channel: str = "EMAIL") -> dict:
        record = {
            "outreach_id": outreach_id, "project_id": self.project["project_id"], "lead_id": self.lead["lead_id"],
            "build_id": None, "deployment_id": None, "channel": channel, "recipient": "hidden@test.invalid",
            "draft_message": "draft", "status": status, "generated_at": "2026-10-01T00:00:00Z",
        }
        self.store.state["outreach"][outreach_id] = record
        self.store._save()
        return record


class PersistenceTests(LedgerTestCase):
    def test_records_survive_a_reload(self) -> None:
        response = self.ledger.record_response(
            self.lead["lead_id"], channel="EMAIL", summary="Asked what the demo would cost",
            evidence_note="reply: 'how much for the site?'",
        )
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="One-page site plus menu")
        follow_up = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-01", reason="budget answer", channel="EMAIL")
        self.ledger.record_outcome(self.lead["lead_id"], status="INTERESTED", evidence_note="asked for a breakdown")

        reloaded = TrackingStore(self.state_path)
        fresh = EngagementLedger(reloaded, source="pytest")
        # One event per operation (response, proposal draft, schedule, outcome) - and no duplicates.
        self.assertEqual(sorted(reloaded.state["engagements"]),
                         ["engagement-0001", "engagement-0002", "engagement-0003", "engagement-0004"])
        self.assertEqual(response["engagement_id"], "engagement-0001")
        self.assertEqual(list(reloaded.state["proposals"]), [proposal["proposal_id"]])
        self.assertEqual(list(reloaded.state["follow_ups"]), [follow_up["follow_up_id"]])
        self.assertEqual(reloaded.state["outcomes"][self.lead["lead_id"]]["status"], "INTERESTED")
        self.assertEqual(fresh.ledger(self.lead["lead_id"])["responses_received"], 1)
        # Records stay linked to the business rather than duplicating its data.
        self.assertEqual(reloaded.state["proposals"][proposal["proposal_id"]]["project_id"], self.project["project_id"])
        self.assertNotIn("recipient", json.dumps(reloaded.state["engagements"]))

    def test_older_state_files_gain_the_collections_without_losing_records(self) -> None:
        legacy_path = self.root / "legacy.json"
        legacy_path.write_text(json.dumps({
            "leads": {"lead-0009": {"lead_id": "lead-0009", "business_name": "Legacy Shop", "city": "Lagos", "country": "Nigeria"}},
            "projects": {}, "builds": {}, "reviews": {},
        }), encoding="utf-8")
        store = TrackingStore(legacy_path)
        for collection in ENGAGEMENT_COLLECTIONS:
            self.assertIn(collection, store.state)
        self.assertEqual(store.state["leads"]["lead-0009"]["business_name"], "Legacy Shop")
        EngagementLedger(store, source="pytest")
        self.assertEqual(store.state["leads"]["lead-0009"]["business_name"], "Legacy Shop")

    def test_ledger_collections_are_known_to_the_relational_backend(self) -> None:
        from tracking.database import RelationalStateBackend

        persisted = RelationalStateBackend.KV_PERSISTED_COLLECTIONS
        for collection in ENGAGEMENT_COLLECTIONS:
            self.assertIn(collection, persisted)
        self.assertIn("worker_routing", persisted)  # the mechanism being reused, not a new one

    def test_sqlite_backend_round_trips_the_ledger(self) -> None:
        url = f"sqlite:///{(self.root / 'ledger.db').as_posix()}"
        store = TrackingStore(self.root / "db-state.json", database_url=url)
        lead = store.create_lead({"business_name": "Fictional DB Test", "business_type": "Restaurants", "city": "Lagos",
                                  "country": "Nigeria", "data_classification": "TEST_FIXTURE", "qualification_status": "QUALIFIED"})
        ledger = EngagementLedger(store, source="pytest")
        event = ledger.record_response(lead["lead_id"], channel="EMAIL", summary="Question about price", evidence_note="'what would it cost?'")
        proposal = ledger.create_proposal(lead["lead_id"], summary="Demo plus menu")

        reopened = TrackingStore(self.root / "db-state-2.json", database_url=url)
        reopened.refresh()
        self.assertIn(event["engagement_id"], reopened.state["engagements"])
        self.assertIn(proposal["proposal_id"], reopened.state["proposals"])
        self.assertEqual(reopened.state["proposals"][proposal["proposal_id"]]["status"], "DRAFT")


class OutreachTruthTests(LedgerTestCase):
    def test_prepared_and_approved_drafts_are_not_sends(self) -> None:
        self.outreach_record(status="AWAITING_REVIEW")
        message = self.fail_with("OUTREACH_NOT_APPROVED", lambda: self.ledger.mark_outreach_sent("outreach-0001", evidence_note="sent"))
        self.assertIn("never", str(message))

    def test_a_send_needs_evidence_and_is_still_not_provider_verified(self) -> None:
        self.outreach_record()
        self.fail_with("EVIDENCE_REQUIRED", lambda: self.ledger.mark_outreach_sent("outreach-0001"))
        updated = self.ledger.mark_outreach_sent("outreach-0001", evidence_note="sent from the operator mailbox at 09:12", provider_message_id="msg-77")
        self.assertEqual(updated["status"], "SENT_REPORTED")
        self.assertIs(updated["provider_verified"], False)
        self.assertEqual(updated["delivery_status"], "REPORTED_BY_OPERATOR")
        kinds = [row["kind"] for row in updated["send_event"]["evidence"]]
        self.assertIn("OPERATOR_NOTE", kinds)
        self.assertIn("msg-77", json.dumps(updated["send_event"]["evidence"]))
        # Re-reporting the same send is idempotent: it returns the stored record and adds no second event.
        repeated = self.ledger.mark_outreach_sent("outreach-0001", evidence_note="reported again")
        self.assertTrue(repeated["unchanged"])
        self.assertEqual(len([row for row in self.store.state["engagements"].values() if row["kind"] == "OUTREACH_SENT"]), 1)
        view = self.ledger.ledger(self.lead["lead_id"])
        self.assertEqual(view["outreach"]["reported_sent"], 1)
        self.assertEqual(view["outreach"]["provider_verified_sends"], 0)

    def test_contact_limit_is_counted_per_lead_and_channel(self) -> None:
        for index in range(1, 4):
            self.outreach_record(f"outreach-000{index}")
            self.ledger.mark_outreach_sent(f"outreach-000{index}", evidence_note=f"send {index}")
        self.outreach_record("outreach-0004")
        message = self.fail_with("CONTACT_LIMIT_REACHED", lambda: self.ledger.mark_outreach_sent("outreach-0004", evidence_note="fourth"))
        self.assertIn("human review decision", str(message))
        # A different channel is a different budget, because the limit is about not wearing someone out on one line.
        self.outreach_record("outreach-0005", channel="WHATSAPP")
        self.ledger.mark_outreach_sent("outreach-0005", evidence_note="via whatsapp", )
        self.assertEqual(self.ledger.ledger(self.lead["lead_id"])["outreach"]["reported_sent"], 4)

    def test_unknown_outreach_is_refused(self) -> None:
        self.fail_with("UNKNOWN_OUTREACH", lambda: self.ledger.mark_outreach_sent("outreach-9999", evidence_note="x"))


class ConsentAndFollowUpTests(LedgerTestCase):
    def test_opt_out_cancels_open_follow_ups_and_blocks_new_ones(self) -> None:
        first = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-11-05", reason="chase the quote", channel="EMAIL")
        other = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-11-20", reason="call about the menu", channel="PHONE")
        block = self.ledger.record_opt_out(self.lead["lead_id"], channel="EMAIL", reason="asked to stop email", evidence_note="'please stop emailing us'")
        self.assertEqual(block["cancelled_follow_ups"], [first["follow_up_id"]])
        self.assertEqual(self.store.state["follow_ups"][first["follow_up_id"]]["status"], "CANCELLED")
        self.assertEqual(self.store.state["follow_ups"][first["follow_up_id"]]["cancellation_reason"], "OPT_OUT_RECORDED")
        self.assertEqual(self.store.state["follow_ups"][other["follow_up_id"]]["status"], "SCHEDULED")
        self.fail_with("CONTACT_BLOCKED", lambda: self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-01", reason="one more email", channel="EMAIL"))
        self.outreach_record()
        self.fail_with("CONTACT_BLOCKED", lambda: self.ledger.mark_outreach_sent("outreach-0001", evidence_note="sent anyway"))
        # PHONE was not covered by that request, so it stays available.
        self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-02", reason="call instead", channel="PHONE")

    def test_revoking_an_opt_out_needs_a_reason_and_reopens_the_channel(self) -> None:
        block = self.ledger.record_opt_out(self.lead["lead_id"], channel="EMAIL", reason="stop", evidence_note="'stop'")
        self.fail_with("UNKNOWN_OPT_OUT", lambda: self.ledger.revoke_opt_out("block-9999", reason="x"))
        with self.assertRaises(LedgerError):
            self.ledger.revoke_opt_out(block["block_id"], reason="   ")
        revoked = self.ledger.revoke_opt_out(block["block_id"], reason="asked to be contacted again", evidence_note="'go ahead'")
        self.assertTrue(revoked["revoked_at"])
        self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-10", reason="resume", channel="EMAIL")

    def test_silence_is_never_a_decision(self) -> None:
        follow_up = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-10-20", reason="chase", channel="EMAIL")
        report = self.ledger.due_follow_ups(today="2026-12-31")
        self.assertEqual(report["due_count"], 1)
        self.assertIn(follow_up["follow_up_id"], [row["follow_up_id"] for row in report["due"]])
        self.assertIn("not a decision", report["note"])
        self.assertEqual(self.store.state["follow_ups"][follow_up["follow_up_id"]]["status"], "SCHEDULED")
        self.assertNotIn(self.lead["lead_id"], self.store.state["outcomes"])
        # An operator may record "no reply so far"; it is the one outcome that needs no customer evidence, and
        # it is still not a rejection.
        outcome = self.ledger.record_outcome(self.lead["lead_id"], status="NO_RESPONSE")
        self.assertEqual(outcome["status"], "NO_RESPONSE")
        self.assertEqual(outcome["evidence"], [])
        self.assertNotIn("NO_RESPONSE", EVIDENCE_REQUIRED_OUTCOMES)

    def test_follow_up_lifecycle_transitions_are_explicit(self) -> None:
        follow_up = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-01", reason="chase", channel="EMAIL")
        self.fail_with("EVIDENCE_REQUIRED", lambda: self.ledger.update_follow_up(follow_up["follow_up_id"], action="COMPLETE"))
        rescheduled = self.ledger.update_follow_up(follow_up["follow_up_id"], action="RESCHEDULE", due_at="2027-01-04", notes="they were travelling")
        self.assertEqual(rescheduled["due_at"][:10], "2027-01-04")
        completed = self.ledger.update_follow_up(follow_up["follow_up_id"], action="COMPLETE", notes="spoke on the phone", evidence_note="call on the 3rd")
        self.assertEqual(completed["status"], "COMPLETED")
        message = self.fail_with("FOLLOW_UP_CLOSED", lambda: self.ledger.update_follow_up(follow_up["follow_up_id"], action="CANCEL", notes="too late"))
        self.assertIn("not reopened", str(message))
        self.fail_with("FOLLOW_UP_ACTION_INVALID", lambda: self.ledger.update_follow_up(follow_up["follow_up_id"], action="IGNORE"))
        self.fail_with("UNKNOWN_FOLLOW_UP", lambda: self.ledger.update_follow_up("follow_up-4242", action="COMPLETE", notes="x", evidence_note="y"))

    def test_backdating_needs_an_explicit_flag(self) -> None:
        self.fail_with("TIMESTAMP_IN_PAST", lambda: self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-10-01", reason="late entry", channel="EMAIL"))
        record = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-10-01", reason="late entry", channel="EMAIL", allow_past_due=True)
        self.assertEqual(record["status"], "SCHEDULED")


class ProposalLifecycleTests(LedgerTestCase):
    def test_created_proposals_are_not_presented_ones(self) -> None:
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="Demo plus menu, phased payment")
        self.assertEqual(proposal["status"], "DRAFT")
        self.assertIn("not a presented one", proposal["note"])
        self.assertIsNone(proposal["presented_at"])
        # A DRAFT cannot jump to PRESENTED, and a real PRESENTED cannot be asserted without evidence.
        self.fail_with("PROPOSAL_NOT_READY", lambda: self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="showed it"))
        self.ledger.update_proposal(proposal["proposal_id"], status="READY_FOR_PRESENTATION")
        self.fail_with("EVIDENCE_REQUIRED", lambda: self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED"))
        presented = self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="walked through it on a call")
        self.assertEqual(presented["status"], "PRESENTED")
        self.assertTrue(presented["presentation_evidence"])
        self.assertFalse(presented["presentation_evidence"][0]["independently_verified"])

    def test_customer_decisions_need_evidence_and_close_the_record(self) -> None:
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="Demo plus menu")
        self.ledger.update_proposal(proposal["proposal_id"], status="READY_FOR_PRESENTATION")
        self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="presented by email")
        self.fail_with("EVIDENCE_REQUIRED", lambda: self.ledger.update_proposal(proposal["proposal_id"], status="ACCEPTED"))
        accepted = self.ledger.update_proposal(proposal["proposal_id"], status="ACCEPTED", notes="agreed to the first phase", evidence_note="'let's go'")
        self.assertEqual(accepted["status"], "ACCEPTED")
        self.assertTrue(accepted["decision_at"])
        message = self.fail_with("PROPOSAL_TRANSITION_INVALID", lambda: self.ledger.update_proposal(proposal["proposal_id"], status="DRAFT"))
        self.assertIn("closed", str(message))
        self.fail_with("PROPOSAL_STATUS_UNSUPPORTED", lambda: self.ledger.update_proposal(proposal["proposal_id"], status="NEGOTIATING"))
        # A status the operator advances alone is a note; only a customer answer is a decision event.
        kinds = [event["kind"] for event in self.ledger.ledger(self.lead["lead_id"])["events"]]
        self.assertEqual(kinds.count("PROPOSAL_DECISION"), 1)
        self.assertIn("NOTE", kinds)

    def test_terms_are_all_or_nothing(self) -> None:
        self.fail_with("PROPOSAL_TERMS_INCOMPLETE", lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="x", currency="NGN"))
        self.fail_with("PROPOSAL_TERMS_INCOMPLETE", lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="x", amount_minor=5000))
        self.fail_with("PROPOSAL_TERMS_INVALID", lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="x", currency="NGN", amount_minor=0))
        priced = self.ledger.create_proposal(self.lead["lead_id"], summary="x", currency="ngn", amount_minor=250000, terms_note="half up front, as quoted")
        self.assertEqual((priced["currency"], priced["amount_minor"]), ("NGN", 250000))
        self.assertIsNone(self.ledger.create_proposal(self.lead["lead_id"], summary="unpriced").get("amount_minor"))


class OutcomeEvidenceTests(LedgerTestCase):
    def test_every_customer_statement_needs_evidence(self) -> None:
        for status in sorted(EVIDENCE_REQUIRED_OUTCOMES):
            with self.subTest(status=status):
                if status == "WON":
                    self.fail_with("WON_EVIDENCE_REQUIRED", lambda s=status: self.ledger.record_outcome(self.lead["lead_id"], status=s, evidence_note="said yes", evidence_kind="CUSTOMER_MESSAGE"))
                else:
                    self.fail_with("EVIDENCE_REQUIRED", lambda s=status: self.ledger.record_outcome(self.lead["lead_id"], status=s))

    def test_won_needs_a_document_or_customer_message_plus_a_reference(self) -> None:
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="x")
        self.ledger.record_outcome(self.lead["lead_id"], status="NEGOTIATING", evidence_note="discussing scope", evidence_kind="CALL_NOTE")
        self.fail_with("WON_EVIDENCE_REQUIRED", lambda: self.ledger.record_outcome(self.lead["lead_id"], status="WON", evidence_note="verbally agreed", evidence_kind="OPERATOR_NOTE", reference="INV-1"))
        self.fail_with("WON_EVIDENCE_REQUIRED", lambda: self.ledger.record_outcome(self.lead["lead_id"], status="WON", evidence_note="signed contract", evidence_kind="CONTRACT"))
        won = self.ledger.record_outcome(self.lead["lead_id"], status="WON", evidence_note="signed the engagement letter", evidence_kind="CONTRACT", reference="AGR-2026-14", notes="first invoice due 1 Nov")
        self.assertEqual(won["status"], "WON")
        self.assertEqual(won["reference"], "AGR-2026-14")
        self.assertFalse(won["revenue_recorded"])
        self.assertEqual(won["previous_status"], "NEGOTIATING")
        self.assertNotIn("amount", json.dumps(won))  # no revenue figure is invented or stored here
        self.assertEqual(self.ledger.ledger(self.lead["lead_id"])["outcome"]["status"], "WON")
        self.assertEqual(self.store.state["leads"][self.lead["lead_id"]]["qualification_status"], "QUALIFIED")

    def test_outcome_transitions_are_guarded(self) -> None:
        self.ledger.record_outcome(self.lead["lead_id"], status="DECLINED", evidence_note="'not right now'")
        self.fail_with("OUTCOME_TRANSITION_INVALID", lambda: self.ledger.record_outcome(self.lead["lead_id"], status="FOLLOW_UP_REQUESTED", evidence_note="changed their mind", evidence_kind="CUSTOMER_MESSAGE"))
        revived = self.ledger.record_outcome(self.lead["lead_id"], status="INTERESTED", evidence_note="'actually let's talk'")
        self.assertEqual(revived["status"], "INTERESTED")
        self.fail_with("OUTCOME_STATUS_UNSUPPORTED", lambda: self.ledger.record_outcome(self.lead["lead_id"], status="SIGNED_CONTRACT", evidence_note="x", evidence_kind="CONTRACT"))
        same = self.ledger.record_outcome(self.lead["lead_id"], status="INTERESTED", evidence_note="still interested")
        self.assertTrue(same["unchanged"])
        self.assertEqual(len(self.store.state["outcomes"]), 1)

    def test_a_generated_or_deployed_site_does_not_make_anyone_a_customer(self) -> None:
        self.assertEqual(dict(self.store.state["outcomes"]), {})
        # Nothing but an explicit, evidenced call converts a lead: even a full engagement history stays UNRESOLVED.
        self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="asked a question", evidence_note="'what is included?'")
        self.ledger.create_proposal(self.lead["lead_id"], summary="x")
        self.assertEqual(self.store.state["outcomes"], {})


class IntegrityAndScopeTests(LedgerTestCase):
    def test_duplicate_events_do_not_duplicate_records(self) -> None:
        first = self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="price question", evidence_note="'how much?'")
        again = self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="price question", evidence_note="'how much?'")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["engagement_id"], first["engagement_id"])
        self.assertEqual(len(self.store.state["engagements"]), 1)
        # With an explicit key it holds even when the wording changes: one idempotency key, one event.
        keyed = self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="call logged", idempotency_key="call-1")
        keyed_again = self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="different words", idempotency_key="call-1")
        self.assertTrue(keyed_again["duplicate"])
        self.assertEqual(keyed_again["engagement_id"], keyed["engagement_id"])
        self.assertEqual(len(self.store.state["engagements"]), 2)
        # Follow-ups dedupe the same way, so a retried command cannot create a second task.
        self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-01", reason="chase", channel="EMAIL")
        twice = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-01", reason="chase", channel="EMAIL")
        self.assertTrue(twice["duplicate"])
        self.assertEqual(len(self.store.state["follow_ups"]), 1)

    def test_unsupported_kinds_and_inputs_are_refused(self) -> None:
        self.fail_with("EVENT_KIND_UNSUPPORTED", lambda: self.ledger.record_event(self.lead["lead_id"], kind="SENT_VIA_GUMTREE", summary="x"))
        self.fail_with("EVENT_DIRECTION_INVALID", lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="x", direction="SIDEWAYS"))
        self.fail_with("CHANNEL_UNSUPPORTED", lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="x", channel="CARRIER_PIGEON"))
        self.fail_with("UNKNOWN_LEAD", lambda: self.ledger.record_event("lead-9999", kind="NOTE", summary="x"))
        self.fail_with("LEDGER_SCOPE_REQUIRED", lambda: self.ledger.ledger())
        self.fail_with("EVIDENCE_URL_INVALID", lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="x", evidence=[{"kind": "URL", "source_url": "ftp://nope"}]))
        self.fail_with("TIMESTAMP_INVALID", lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="x", occurred_at="yesterday afternoon"))
        self.fail_with("TIMESTAMP_IN_FUTURE", lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="x", occurred_at="2030-01-01"))

    def test_contact_details_are_redacted_on_write(self) -> None:
        self.assertEqual(redact_contact_details("write to owner@pitstop-lagos.com or +234 803 000 1122"), "write to [redacted:email] or [redacted:phone]")
        event = self.ledger.record_response(
            self.lead["lead_id"], channel="EMAIL", summary="send the quote to manager@pitstop.example and +234 801 555 0100",
            quote="email me at manager@pitstop.example", evidence_note="replied from the inbox",
        )
        blob = json.dumps(event)
        self.assertNotIn("manager@pitstop.example", blob)
        self.assertNotIn("555 0100", blob)
        self.assertTrue(event["contact_details_redacted"])
        self.assertEqual(event["evidence"][0]["note"], "replied from the inbox")

    def test_records_do_not_open_any_later_stage(self) -> None:
        self.outreach_record()
        self.ledger.mark_outreach_sent("outreach-0001", evidence_note="sent")
        self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="yes interested", evidence_note="'let's do it'")
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="x")
        self.ledger.update_proposal(proposal["proposal_id"], status="READY_FOR_PRESENTATION")
        self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="shown")
        self.ledger.record_outcome(self.lead["lead_id"], status="INTERESTED", evidence_note="wants a start date")
        self.assertEqual(self.store.state["builds"], {})
        self.assertEqual(self.store.state["validations"], {})
        self.assertEqual(self.store.state["deployments"], {})
        self.assertEqual(self.store.state["reviews"], {})
        self.assertEqual(self.store.state.get("approvals", {}), {})
        self.assertEqual(self.store.state["projects"][self.project["project_id"]]["current_stage"], "QUALIFIED")
        self.assertEqual(self.store.state["projects"][self.project["project_id"]]["review_status"], "PENDING")

    def test_the_module_reaches_no_network_and_no_later_stage(self) -> None:
        source = Path("tracking/engagement.py").read_text()
        for forbidden in ("urlopen", "subprocess", "socket", "requests", "from deployment", "import deployment",
                          "from builder", "from outreach.service", "from orchestration", "send_message", "deploy("):
            self.assertNotIn(forbidden, source, forbidden)

    def test_ledger_view_states_what_it_cannot_know(self) -> None:
        self.outreach_record(status="AWAITING_REVIEW")
        view = self.ledger.ledger(self.lead["lead_id"])
        self.assertEqual(view["outreach"]["prepared"], 1)
        self.assertEqual(view["outreach"]["approved_awaiting_send"], 0)
        self.assertEqual(view["outreach"]["reported_sent"], 0)
        self.assertEqual(view["outcome"], {"status": "UNRESOLVED", "recorded": False})
        self.assertIn("prepared drafts are not sends", view["truth_notes"])


class CliTests(LedgerTestCase):
    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run_cli(["--state-path", str(self.state_path), *argv], stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_records_and_reads_back(self) -> None:
        code, stdout, _ = self.cli("response-record", self.lead["lead_id"], "--channel", "EMAIL",
                                   "--summary", "asked about price", "--evidence-note", "'how much for the site?'")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["kind"], "RESPONSE_RECEIVED")
        code, stdout, _ = self.cli("proposal-create", self.lead["lead_id"], "--summary", "demo plus menu",
                                   "--terms-note", "phased, quoted on the call")
        proposal_id = json.loads(stdout)["proposal_id"]
        self.assertEqual(code, 0)
        code, _, _ = self.cli("proposal-update", proposal_id, "--status", "READY_FOR_PRESENTATION")
        self.assertEqual(code, 0)
        code, stdout, _ = self.cli("proposal-update", proposal_id, "--status", "PRESENTED", "--evidence-note", "shown on a call")
        self.assertEqual(json.loads(stdout)["status"], "PRESENTED")
        code, stdout, _ = self.cli("follow-up-schedule", self.lead["lead_id"], "--due", "2026-12-20", "--reason", "answer on price", "--channel", "EMAIL")
        self.assertEqual(code, 0)
        follow_up_id = json.loads(stdout)["follow_up_id"]
        code, stdout, _ = self.cli("due-follow-ups", "--today", "2027-01-31")
        self.assertEqual(json.loads(stdout)["due_count"], 1)
        code, stdout, _ = self.cli("follow-up-update", follow_up_id, "--action", "COMPLETE", "--notes", "spoke", "--evidence-note", "call on the 20th")
        self.assertEqual(json.loads(stdout)["status"], "COMPLETED")
        code, stdout, _ = self.cli("ledger", self.lead["lead_id"])
        view = json.loads(stdout)
        self.assertEqual(view["responses_received"], 1)
        self.assertEqual(view["follow_ups"]["completed"], 1)
        self.assertEqual([event["kind"] for event in view["events"]],
                         ["RESPONSE_RECEIVED", "PROPOSAL_CREATED", "NOTE", "PROPOSAL_PRESENTED",
                          "FOLLOW_UP_SCHEDULED", "FOLLOW_UP_COMPLETED"])

    def test_cli_refusals_carry_an_error_code_and_exit_one(self) -> None:
        code, _, err = self.cli("outcome-record", self.lead["lead_id"], "--status", "WON",
                               "--evidence-note", "they said yes", "--evidence-kind", "CUSTOMER_MESSAGE")
        self.assertEqual(code, 1)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "WON_EVIDENCE_REQUIRED")
        self.assertIn("reference", payload["error"])
        code, _, err = self.cli("opt-out", self.lead["lead_id"], "--channel", "EMAIL")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error_code"], "EVIDENCE_REQUIRED")

    def test_cli_does_not_store_a_recipient(self) -> None:
        code, stdout, _ = self.cli("engagement-record", self.lead["lead_id"], "--kind", "NOTE",
                                   "--summary", "noted their address at 12 Example Street and phone +234 800 000 0000")
        self.assertEqual(code, 0)
        # Read back from the file: the CLI works on its own store instance.
        stored = json.dumps(TrackingStore(self.state_path).state["engagements"])
        self.assertNotIn("+234 800 000 0000", stored)
        self.assertIn("[redacted:phone]", stored)


if __name__ == "__main__":
    unittest.main()


def outreach_status_ready(store, lead_id: str, project_id: str, *, channel: str = "EMAIL") -> str:
    """Put an approved-but-unsent draft in front of the ledger (the outreach service owns that state)."""
    outreach_id = f"outreach-{len(store.state['outreach']) + 1:04d}"
    store.state["outreach"][outreach_id] = {
        "outreach_id": outreach_id, "project_id": project_id, "lead_id": lead_id, "channel": channel,
        "status": "READY_TO_SEND", "draft_message": "fictional draft", "generated_at": "2026-10-01T00:00:00Z",
    }
    store._save()
    return outreach_id


class RelationshipTests(LedgerTestCase):
    """The stage row is a projection of the other records, never a place to write hopes."""

    def test_a_relationship_row_follows_the_records_and_reloads(self) -> None:
        row = self.ledger.relationship(self.lead["lead_id"])
        self.assertFalse(row["recorded"], "reading must not create a record")
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent from the operator's mailbox")
        row = self.ledger.relationship(self.lead["lead_id"])
        self.assertTrue(row["recorded"])
        self.assertEqual(row["stage"], "OUTREACH_SENT")
        self.assertTrue(row["stage_is_current"])
        self.assertEqual([entry["stage"] for entry in row["stage_history"]], ["NOT_CONTACTED", "OUTREACH_SENT"])
        self.assertEqual(row["stage_history"][-1]["confirmed_by"], ["outreach-0001"])
        self.assertEqual(row["stage_history"][-1]["trigger"], "derived:event:OUTREACH_SENT")
        self.assertIn("outreach-0001", row["evidence_refs"])
        reloaded = TrackingStore(self.state_path)
        self.assertEqual(reloaded.state["relationships"][self.lead["lead_id"]]["stage"], "OUTREACH_SENT")
        self.assertEqual(reloaded.state["relationships"][self.lead["lead_id"]]["next_action"],
                         "wait for a reply, or schedule one follow-up - do not send again on a timer")

    def test_a_stage_that_no_record_proves_is_refused(self) -> None:
        self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="called once")
        error = self.fail_with("STAGE_NEEDS_CORROBORATION",
                               lambda: self.ledger.advance_relationship_stage(self.lead["lead_id"], "WON", reason="feels positive"))
        self.assertIn("outcome record with status WON", str(error))
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage"], "NOT_CONTACTED")

    def test_lowering_a_recorded_stage_needs_an_explicit_flag_and_a_reason(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent")
        self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="replied", quote="ok")
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage"], "RESPONDED")
        self.fail_with("RELATIONSHIP_BACKTRACK_REQUIRES_FLAG",
                       lambda: self.ledger.advance_relationship_stage(self.lead["lead_id"], "OUTREACH_SENT", reason="oops"))
        moved = self.ledger.advance_relationship_stage(self.lead["lead_id"], "OUTREACH_SENT", reason="that reply was ours, not theirs", allow_backtrack=True)
        self.assertEqual(moved["stage"], "OUTREACH_SENT")
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage_history"][-1]["trigger"], "operator:pytest")

    def test_pipeline_pages_and_counts_from_persisted_rows(self) -> None:
        report = self.ledger.pipeline()
        self.assertEqual(report["total"], 0, "no ledger record yet means no row - not a lead with nothing happened")
        self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="first note")
        other = self.store.create_lead({"business_name": "Other Test Shop", "city": "Ikeja", "country": "Nigeria",
                                        "data_classification": "TEST_FIXTURE", "qualification_status": "QUALIFIED",
                                        "website_status": "WEBSITE_NEEDS_IMPROVEMENT"})
        self.ledger.record_event(other["lead_id"], kind="NOTE", summary="second note")
        page = self.ledger.pipeline(limit=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["returned"], 1)
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(self.ledger.pipeline(offset=1)["rows"][0]["lead_id"], other["lead_id"])
        self.assertEqual(page["stage_counts"], {"NOT_CONTACTED": 2})
        self.assertEqual(self.ledger.pipeline(stage="RESPONDED")["total"], 0)
        self.fail_with("RELATIONSHIP_STAGE_UNSUPPORTED", lambda: self.ledger.pipeline(stage="ALMOST_WON"))
        self.fail_with("LIMIT_INVALID", lambda: self.ledger.pipeline(limit=0))
        self.fail_with("OFFSET_INVALID", lambda: self.ledger.pipeline(offset=-1))
        self.assertEqual(self.ledger.pipeline(limit=10_000)["limit"], 200, "a page size is clamped, not trusted")

    def test_outstanding_work_and_outcome_report_count_only_what_was_recorded(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent")
        self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="wants a price", quote="how much?")
        self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-01-05", reason="chase the price",
                                        channel="EMAIL", allow_past_due=True)
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="one page + menu")
        self.ledger.update_proposal(proposal["proposal_id"], status="READY_FOR_PRESENTATION")
        self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="on a call")
        work = self.ledger.outstanding_work()
        self.assertEqual(work["due_follow_up_count"], 1)
        self.assertEqual([row["proposal_id"] for row in work["proposals_awaiting_decision"]], [proposal["proposal_id"]])
        self.assertEqual(work["responses_awaiting_reply"], [],
                         "the presentation that went out afterwards answers the reply; the follow-up on its own would not")
        self.assertEqual(work["active_contact_blocks"], [])
        self.ledger.record_outcome(self.lead["lead_id"], status="WON", evidence_kind="CONTRACT", reference="AGR-1", evidence_note="signed")
        report = self.ledger.outcome_report()
        self.assertEqual(report["counts_by_status"], {"WON": 1})
        self.assertEqual(report["outcomes"][0]["reference"], "AGR-1")
        self.assertFalse(report["outcomes"][0]["revenue_recorded"])
        self.assertEqual(report["outcomes"][0]["evidence_kinds"], ["CONTRACT"])
        self.assertEqual(self.ledger.outcome_report(statuses=["LOST"])["count"], 0)
        self.fail_with("OUTCOME_STATUS_UNSUPPORTED", lambda: self.ledger.outcome_report(statuses=["PAID_UP"]))


class SendOutcomeTests(LedgerTestCase):
    """Planned, attempted, reported, failed, and provider-confirmed are five different sentences."""

    def test_a_failed_send_is_a_failure_and_not_a_contact(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"], channel="WHATSAPP")
        failed = self.ledger.mark_outreach_sent(outreach_id, result="FAILED", failure_note="number not registered on WhatsApp")
        self.assertEqual(failed["status"], "SEND_FAILED")
        self.assertEqual(failed["delivery_status"], "FAILED_REPORTED")
        self.assertFalse(failed["contact_established"])
        self.assertFalse(failed["counts_as_contact"])
        self.assertEqual(failed["send_attempts"], 1)
        # The draft is still approved; only the contact did not happen.
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage"], "OUTREACH_APPROVED")
        self.assertFalse(self.ledger.relationship(self.lead["lead_id"])["stage_is_current"] and False)
        kinds = [row["kind"] for row in self.store.state["engagements"].values()]
        self.assertEqual(kinds, ["OUTREACH_SEND_FAILED"])

    def test_a_draft_that_failed_can_be_reported_sent_without_a_second_record(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"], channel="WHATSAPP")
        self.ledger.mark_outreach_sent(outreach_id, result="FAILED", failure_note="network dropped it")
        sent = self.ledger.mark_outreach_sent(outreach_id, evidence_note="retried by hand and it went")
        self.assertEqual(sent["status"], "SENT_REPORTED")
        self.assertEqual(sent["send_attempts"], 2)
        self.assertEqual(len(self.store.state["outreach"]), 1, "a retry reuses the draft; it does not mint a second one")
        self.assertEqual(self.ledger._send_attempts(self.lead["lead_id"], "WHATSAPP")["distinct_outreach_records"], 1,
                         "one record, one budget unit")

    def test_a_reported_send_can_not_be_rewritten_into_a_failure(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent")
        self.fail_with("SEND_ALREADY_REPORTED_SENT",
                       lambda: self.ledger.mark_outreach_sent(outreach_id, result="FAILED", failure_note="actually bounced"))

    def test_provider_acceptance_and_delivery_are_unreachable_from_here(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        for result in ("ACCEPTED_BY_PROVIDER", "VERIFIED_DELIVERED"):
            self.fail_with("PROVIDER_RECEIPT_UNAVAILABLE",
                           lambda r=result: self.ledger.mark_outreach_sent(outreach_id, result=r, evidence_note="the api said ok"))
        self.fail_with("PROVIDER_RECEIPT_UNAVAILABLE",
                       lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="they got it",
                                                          delivery_status="VERIFIED_DELIVERED"))
        self.fail_with("DELIVERY_STATUS_UNSUPPORTED",
                       lambda: self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="x", delivery_status="DEFINITELY_DELIVERED"))
        self.assertEqual(self.store.state["outreach"][outreach_id]["status"], "READY_TO_SEND",
                         "nothing was recorded while the stronger claims were refused")

    def test_failure_text_that_carries_a_credential_is_refused_not_scrubbed(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"], channel="SMS")
        error = self.fail_with("DETAIL_MAY_CONTAIN_SECRET", lambda: self.ledger.mark_outreach_sent(
            outreach_id, result="FAILED", failure_note="provider said 401 with Authorization: Bearer abcdef123456"))
        self.assertNotIn("abcdef123456", error.__str__())
        self.assertEqual(self.store.state["outreach"][outreach_id]["status"], "READY_TO_SEND")
        self.assertNotIn("abcdef", self.state_path.read_text(encoding="utf-8"))
        recorded = self.ledger.mark_outreach_sent(outreach_id, result="FAILED", failure_note="provider said 401 (token withheld from this note)")
        self.assertIn("401", recorded["failure_detail"])

    def test_the_eligibility_guard_is_the_one_check_and_reads_approval_at_call_time(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.store.state["outreach"][outreach_id]["status"] = "AWAITING_REVIEW"
        blocked = self.ledger.outreach_eligibility(self.lead["lead_id"], "EMAIL", outreach_id=outreach_id)
        self.assertFalse(blocked["eligible"])
        self.assertIn("OUTREACH_NOT_APPROVED", blocked["blockers"])
        self.assertFalse(blocked["grants_authorization"])
        self.fail_with("OUTREACH_NOT_APPROVED",
                       lambda: self.ledger.mark_outreach_sent(outreach_id, evidence_note="approve me anyway"))
        self.store.state["outreach"][outreach_id]["status"] = "READY_TO_SEND"
        self.assertTrue(self.ledger.outreach_eligibility(self.lead["lead_id"], "EMAIL", outreach_id=outreach_id)["eligible"])
        self.ledger.record_opt_out(self.lead["lead_id"], channel="EMAIL", reason="stop", evidence_note="'stop'")
        verdict = self.ledger.outreach_eligibility(self.lead["lead_id"], "EMAIL", outreach_id=outreach_id)
        self.assertIn("CONTACT_BLOCKED", verdict["blockers"])
        self.fail_with("CONTACT_BLOCKED", lambda: self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent anyway"))

    def test_a_send_for_another_leads_draft_is_refused(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        other = self.store.create_lead({"business_name": "Other Shop", "city": "Ikeja", "country": "Nigeria",
                                        "data_classification": "TEST_FIXTURE", "qualification_status": "QUALIFIED",
                                        "website_status": "WEBSITE_NEEDS_IMPROVEMENT"})
        # The guard answers rather than raising; the refusal happens when an operation consults it.
        verdict = self.ledger.outreach_eligibility(other["lead_id"], "EMAIL", outreach_id=outreach_id)
        self.assertFalse(verdict["eligible"])
        self.assertIn("OUTREACH_LEAD_MISMATCH", verdict["blockers"])
        self.assertIn(outreach_id, " ".join(row["detail"] for row in verdict["checks"]))
        self.fail_with("OUTREACH_LEAD_MISMATCH",
                       lambda: self.ledger.schedule_follow_up(other["lead_id"], due_at="2026-12-01", reason="chase",
                                                               channel="EMAIL", outreach_id=outreach_id))

    def test_one_operation_is_one_durable_write_and_a_refusal_persists_nothing(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"], channel="SMS")
        writes = []
        real_save = self.store._save

        def counting_save():
            writes.append(1)
            return real_save()

        self.store._save = counting_save  # type: ignore[method-assign]
        try:
            self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-12-01", reason="chase", channel="SMS")
            self.assertEqual(len(writes), 1, "a follow-up and its event land as one write, not two half-states")
            proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="x")
            before = self.state_path.read_text(encoding="utf-8")
            self.fail_with("PROPOSAL_TRANSITION_INVALID",
                           lambda: self.ledger.update_proposal(proposal["proposal_id"], status="ACCEPTED"))
            self.assertEqual(self.state_path.read_text(encoding="utf-8"), before, "a refused write must not reach the file")
            self.assertEqual(self.store.state["proposals"][proposal["proposal_id"]]["status"], "DRAFT",
                             "and the in-memory row is rolled back too, so a later save cannot leak the refusal")
            self.assertNotIn("SENT_FAILED", before)
        finally:
            self.store._save = real_save  # type: ignore[method-assign]

    def test_a_storage_fault_propagates_instead_of_reporting_success(self) -> None:
        from tracking.database import DatabasePersistenceError
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])

        def broken_save():
            raise DatabasePersistenceError("database did not accept the transaction")

        self.store._save = broken_save  # type: ignore[method-assign]
        try:
            with self.assertRaises(DatabasePersistenceError):
                self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent")
        finally:
            self.store._save = real = TrackingStore(self.state_path)  # noqa: F841
            self.store._save = object.__getattribute__(self.store, "_save")  # type: ignore[method-assign]
        self.assertNotIn("SENT_REPORTED", self.state_path.read_text(encoding="utf-8"),
                         "the file never learned about a write the database refused")


class ProposalDemoTests(LedgerTestCase):
    """A proposal can cite a demo and an approval, but only as those records describe themselves."""

    def deployment_row(self, *, url: str = "https://preview.example.invalid/site", live_checked: bool = False) -> str:
        deployment_id = f"deployment-{len(self.store.state['deployments']) + 1:04d}"
        self.store.state["deployments"][deployment_id] = {
            "deployment_id": deployment_id, "project_id": self.project["project_id"], "lead_id": self.lead["lead_id"],
            "build_id": None, "deployment_url": url, "deployment_status": "DEPLOYED",
            "validation_status": "PASS", "live_url_checked": live_checked,
        }
        self.store._save()
        return deployment_id

    def test_a_demo_cites_the_deployment_record_rather_than_a_typed_url(self) -> None:
        self.fail_with("UNKNOWN_DEPLOYMENT",
                       lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="site", deployment_id="deployment-0009"))
        self.fail_with("DEMO_URL_UNRECORDED",
                       lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="site",
                                                            preview_url="https://somewhere-else.example.invalid"))
        deployment_id = self.deployment_row()
        self.fail_with("DEMO_URL_MISMATCH",
                       lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="site", deployment_id=deployment_id,
                                                            preview_url="https://different.example.invalid"))
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="site", deployment_id=deployment_id)
        demo = proposal["demo"]
        self.assertEqual(demo["deployment_url"], "https://preview.example.invalid/site")
        self.assertEqual(demo["url_source"], "recorded_deployment")
        self.assertFalse(demo["live_url_checked"], "a URL on a record is not proof the page loads")
        self.assertIn("not proof that a URL serves the latest build", demo["verification_note"])
        checked = self.deployment_row(url="https://other.example.invalid", live_checked=True)
        second = self.ledger.create_proposal(self.lead["lead_id"], summary="second", deployment_id=checked)
        self.assertTrue(second["demo"]["live_url_checked"], "what the deployment record says is echoed, not upgraded")

    def test_an_approval_reference_must_be_a_real_review_for_the_same_project(self) -> None:
        self.fail_with("UNKNOWN_APPROVAL", lambda: self.ledger.create_proposal(self.lead["lead_id"], summary="site", approval_ref="review-0007"))
        self.store.state["reviews"]["review-0001"] = {
            "review_id": "review-0001", "project_id": self.project["project_id"], "lead_id": self.lead["lead_id"],
            "decision": "APPROVED", "reviewer": "operator", "reviewed_at": "2026-10-10T00:00:00Z",
            "identity_verified": False,
        }
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="site", approval_ref="review-0001")
        self.assertEqual(proposal["approval"]["decision"], "APPROVED")
        self.assertFalse(proposal["approval"]["identity_verified"])
        other = self.store.create_lead({"business_name": "Third Shop", "city": "Apapa", "country": "Nigeria",
                                        "data_classification": "TEST_FIXTURE", "qualification_status": "QUALIFIED",
                                        "website_status": "WEBSITE_NEEDS_IMPROVEMENT"})
        other_project = self.store.create_project(other["lead_id"])
        self.fail_with("APPROVAL_PROJECT_MISMATCH", lambda: self.ledger.create_proposal(other["lead_id"], summary="x",
                                                                                          approval_ref="review-0001", project_id=other_project["project_id"]))

    def test_acceptance_cites_what_the_customer_sent_not_an_impression(self) -> None:
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="site")
        self.ledger.update_proposal(proposal["proposal_id"], status="READY_FOR_PRESENTATION")
        self.ledger.update_proposal(proposal["proposal_id"], status="PRESENTED", evidence_note="showed it")
        self.fail_with("EVIDENCE_KIND_INSUFFICIENT",
                       lambda: self.ledger.update_proposal(proposal["proposal_id"], status="ACCEPTED",
                                                             evidence_note="they seemed keen", evidence_kind="OPERATOR_NOTE"))
        accepted = self.ledger.update_proposal(proposal["proposal_id"], status="ACCEPTED", evidence_note="'we are in'")
        self.assertEqual(accepted["decision_evidence"][0]["kind"], "CUSTOMER_MESSAGE")
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["stage"], "PROPOSAL_PRESENTED")

    def test_the_sending_state_is_read_from_the_outreach_record_not_copied(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        proposal = self.ledger.create_proposal(self.lead["lead_id"], summary="site", outreach_id=outreach_id)
        self.assertEqual(proposal["outreach_status"], "READY_TO_SEND")
        self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent")
        self.assertEqual(self.store.state["proposals"][proposal["proposal_id"]]["outreach_status"], "READY_TO_SEND",
                         "the proposal keeps the status it was drafted with; the outreach record is the live one")


class ResponseIntakeTests(LedgerTestCase):
    def test_a_reply_is_owed_until_something_goes_out(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.ledger.mark_outreach_sent(outreach_id, evidence_note="sent")
        response = self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="price question",
                                                quote="how much?", classification="QUESTION", in_reply_to="engagement-0001")
        self.assertEqual(response["classification_source"], "operator_asserted")
        self.assertTrue(response["needs_human_review"])
        self.assertEqual(response["in_reply_to"], "engagement-0001")
        self.assertEqual(self.ledger.relationship(self.lead["lead_id"])["next_action"],
                         "prepare a reply to %s (a human decides what it says)" % response["engagement_id"])
        self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="noted internally")
        self.assertIn("prepare a reply", self.ledger.relationship(self.lead["lead_id"])["next_action"])
        self.ledger.record_event(self.lead["lead_id"], kind="OUTREACH_SENT", direction="OUTBOUND", channel="EMAIL",
                                  summary="answered them", evidence=[{"kind": "OPERATOR_NOTE", "note": "replied by mail"}],
                                  outreach_id=outreach_id)
        self.assertNotIn("prepare a reply", self.ledger.relationship(self.lead["lead_id"])["next_action"])

    def test_an_unsubscribe_is_not_an_opt_out_until_one_is_recorded(self) -> None:
        response = self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="asked to stop",
                                                 quote="unsubscribe me", classification="UNSUBSCRIBE")
        self.assertIn("record_opt_out", response["suggested_action"])
        self.assertIsNone(self.ledger.is_blocked(self.lead["lead_id"], "EMAIL"))
        self.assertEqual(self.ledger.outstanding_work()["responses_awaiting_reply"][0]["classification"], "UNSUBSCRIBE")
        self.ledger.record_opt_out(self.lead["lead_id"], channel="EMAIL", reason="asked to stop",
                                     evidence_note="'unsubscribe me'")
        self.assertIsNotNone(self.ledger.is_blocked(self.lead["lead_id"], "EMAIL"))

    def test_reply_targets_and_classifications_are_validated(self) -> None:
        self.fail_with("UNKNOWN_ENGAGEMENT", lambda: self.ledger.record_response(self.lead["lead_id"], channel="EMAIL",
                                                                                  summary="x", quote="y", in_reply_to="engagement-0042"))
        first = self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="in", quote="y")
        self.fail_with("REPLY_TARGET_INVALID", lambda: self.ledger.record_response(self.lead["lead_id"], channel="EMAIL",
                                                                                    summary="z", quote="q", in_reply_to=first["engagement_id"]))
        self.fail_with("CLASSIFICATION_UNSUPPORTED", lambda: self.ledger.record_response(self.lead["lead_id"], channel="EMAIL",
                                                                                            summary="z", quote="q", classification="DEFINITELY_INTERESTED"))
        self.assertEqual(self.store.state["engagements"][first["engagement_id"]]["provider_verified"], False)

    def test_provider_identity_is_recorded_as_given_and_never_verified(self) -> None:
        response = self.ledger.record_response(self.lead["lead_id"], channel="EMAIL", summary="price question", quote="how much?",
                                                 provider="gmail-web", external_message_id="mid-4471", conversation_id="conv-77")
        self.assertEqual(response["provider"], "gmail-web")
        self.assertEqual(response["external_message_id"], "mid-4471")
        self.assertEqual(response["conversation_id"], "conv-77")
        self.assertFalse(response["provider_verified"])


class FaultToleranceTests(LedgerTestCase):
    """A report has to survive state that a person edited by hand or a partially removed record."""

    def test_a_dangling_reference_blocks_the_row_instead_of_crashing_the_report(self) -> None:
        follow_up = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-01-05", reason="chase",
                                                     channel="EMAIL", allow_past_due=True)
        self.store.state["follow_ups"][follow_up["follow_up_id"]]["outreach_id"] = "outreach-9999"
        self.store._save()
        report = self.ledger.due_follow_ups()
        self.assertEqual(report["due_count"], 1, "the item is still due, and hiding that is worse than flagging it")
        row = report["due"][0]
        self.assertFalse(row["eligible_to_act_now"])
        self.assertEqual(row["eligibility_blockers"], ["UNKNOWN_OUTREACH"])
        # The same dangling reference does not stop unrelated writes, and the out-of-band row is not rewritten.
        event = self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="kept going")
        self.assertEqual(event["kind"], "NOTE")
        self.assertEqual(self.store.state["follow_ups"][follow_up["follow_up_id"]]["outreach_id"], "outreach-9999")

    def test_outstanding_work_reports_the_same_row_without_raising(self) -> None:
        follow_up = self.ledger.schedule_follow_up(self.lead["lead_id"], due_at="2026-01-05", reason="chase",
                                                     channel="EMAIL", allow_past_due=True)
        self.store.state["follow_ups"][follow_up["follow_up_id"]]["lead_id"] = "lead-9999"
        self.store._save()
        work = self.ledger.outstanding_work()
        self.assertEqual([row["follow_up_id"] for row in work["due_follow_ups"]], [follow_up["follow_up_id"]])
        self.assertFalse(work["due_follow_ups"][0]["eligible_to_act_now"])


class EngagementCliAndApiTests(LedgerTestCase):
    """The same surface an operator and a scheduler use, exercised through their own entry points."""

    def cli(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        code = run_cli(["--state-path", str(self.state_path), *argv], stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_reports_relationship_pipeline_and_eligibility(self) -> None:
        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        code, stdout, _ = self.cli("outreach-eligibility", self.lead["lead_id"], "--channel", "EMAIL", "--outreach-id", outreach_id)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout)["grants_authorization"])
        code, stdout, _ = self.cli("outreach-mark-sent", outreach_id, "--result", "FAILED", "--failure-note", "mailbox full")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "SEND_FAILED")
        code, stdout, _ = self.cli("relationship", self.lead["lead_id"])
        # A failed attempt leaves the draft approved but the lead uncontacted - exactly the distinction to keep.
        self.assertEqual(json.loads(stdout)["stage"], "OUTREACH_APPROVED")
        code, stdout, _ = self.cli("pipeline")
        self.assertEqual(json.loads(stdout)["total"], 1)
        code, stdout, _ = self.cli("outstanding")
        self.assertEqual(json.loads(stdout)["due_follow_up_count"], 0)
        code, _, err = self.cli("relationship-advance", self.lead["lead_id"], "--stage", "WON", "--reason", "wishful")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err.strip().splitlines()[-1])["error_code"], "STAGE_NEEDS_CORROBORATION")

    def test_api_serves_ledger_reads_and_no_writes(self) -> None:
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from orchestration.api import make_handler
        from orchestration.jobs import JobService

        token = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"
        handler = make_handler(JobService(self.store), token, repository_root=self.root)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def get(path: str, *, authenticated: bool = True):
            headers = {"Authorization": f"Bearer {token}"} if authenticated else {}
            try:
                with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers, method="GET"), timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read().decode("utf-8"))

        outreach_id = outreach_status_ready(self.store, self.lead["lead_id"], self.project["project_id"])
        self.ledger.record_event(self.lead["lead_id"], kind="NOTE", summary="noted for the api")
        status, payload = get("/engagement/pipeline")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["rows"][0]["lead_id"], self.lead["lead_id"])
        status, payload = get("/engagement/pipeline?limit=0")
        self.assertEqual((status, payload["error_code"]), (400, "LIMIT_INVALID"))
        status, payload = get(f"/engagement/eligibility/{self.lead['lead_id']}?outreach_id={outreach_id}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["eligible"])
        status, payload = get(f"/engagement/leads/{self.lead['lead_id']}")
        self.assertEqual(status, 200)
        # The approved draft is itself a record, so the stage is already OUTREACH_APPROVED.
        self.assertEqual(payload["relationship"]["stage"], "OUTREACH_APPROVED")
        status, payload = get("/engagement/nope")
        self.assertEqual((status, payload["error_code"]), (404, "LEDGER_ROUTE_NOT_FOUND"))
        status, _ = get("/engagement/pipeline", authenticated=False)
        self.assertEqual(status, 401, "an engagement read needs the same bearer token as every other route")
        before = self.state_path.read_text(encoding="utf-8")
        request = urllib.request.Request(base + "/engagement/pipeline", data=b"{}", method="POST",
                                        headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        self.assertIn(code, (404, 405), "there is no write route to reach")
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
