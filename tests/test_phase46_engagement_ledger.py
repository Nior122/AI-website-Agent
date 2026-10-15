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
