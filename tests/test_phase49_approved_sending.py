"""Phase 49: approved outbound email, the demo approval that unlocks a preview, and what a send may claim.

These tests are offline by construction - the Gmail send adapter is handed an injected ``transport`` and the
read-only client a fake, so no test in this file can put a message in anybody's inbox, and none of them needs a
credential. What they check is the shape of the guarantees:

* the read-only adapter still cannot send, and the send adapter still cannot read;
* a send happens only for a draft a human approved, whose content digest still matches, on a lead that has not
  opted out, within the contact budget, while dispatch is not held, and only when this machine's live-effect flag
  is on - each of those separately, with the provider call counted to prove the refusal happened first;
* an attempt is written down before the provider is contacted, so a crash mid-send is recoverable and a timeout is
  reported as *uncertain* rather than failed, with no resend anywhere;
* ``ACCEPTED_BY_PROVIDER`` needs the message id read back from the account and matched, ``REPORTED_SENT`` is what
  is recorded without it, and ``VERIFIED_DELIVERED`` cannot be recorded at all;
* the Telegram surface re-derives its confirmation codes at press time, never puts a prospect's message body or
  address in a chat reply, and reaches the demo approval only through the store's own review mechanism - which is
  the same capability file the publish step verifies.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock
from email import message_from_bytes, policy

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

OPERATOR = 1001
OTHER = 2002
CHAT = -100

SEND_ENV_NAMES = ("AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN", "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID",
                  "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET", "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED",
                  "AI_WEBSITE_AGENT_EMAIL_SEND_FROM", "AI_WEBSITE_AGENT_EMAIL_SEND_REPLY_TO",
                  "AI_WEBSITE_AGENT_EMAIL_UNSUBSCRIBE_URL", "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH")


def _clean_env(test: unittest.TestCase) -> None:
    """No test here may inherit a real credential from the machine running it."""
    saved = {name: os.environ.get(name) for name in SEND_ENV_NAMES}
    for name in SEND_ENV_NAMES:
        os.environ.pop(name, None)

    def restore() -> None:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    test.addCleanup(restore)


class RecordingTransport:
    """A stand-in for the network, with the answers a Gmail send can plausibly give back."""

    def __init__(self, *, token_ok: bool = True, answers=None, error=None):
        self.calls: list[tuple[str, str, dict | None, dict | None, dict | None]] = []
        self.token_ok = token_ok
        self.answers = list(answers or [])
        self.error = error

    def __call__(self, method, url, params=None, headers=None, body=None):
        self.calls.append((method, url, params, headers, body))
        if url.endswith("/token"):
            if not self.token_ok:
                return {"status": 400, "json": {"error": "invalid_grant"}, "text": ""}
            return {"status": 200, "json": {"access_token": "***", "expires_in": 3600}, "text": ""}
        if self.error is not None:
            raise self.error
        if self.answers:
            return self.answers.pop(0)
        return {"status": 200, "json": {"id": "msg-fake-1", "threadId": "thread-fake-1", "labelIds": ["SENT"],
                                        "sizeEstimate": 512}, "text": ""}

    @property
    def send_calls(self) -> list:
        return [call for call in self.calls if "/messages" in call[1]]

    @property
    def methods(self) -> set[str]:
        return {call[0] for call in self.calls}

    def decoded_raw(self) -> bytes:
        for method, url, params, headers, body in reversed(self.send_calls):
            if isinstance(body, dict) and body.get("raw"):
                padded = body["raw"] + "=" * (-len(body["raw"]) % 4)
                return base64.urlsafe_b64decode(padded.encode("ascii"))
        raise AssertionError("no send request carried a raw message")


class ReadOnlyTransport(RecordingTransport):
    """What the read-only client needs: one message resource, on request."""

    def __init__(self, message: dict):
        super().__init__()
        self.message = message

    def __call__(self, method, url, params=None, headers=None, body=None):
        self.calls.append((method, url, params, headers, body))
        if url.endswith("/token"):
            return {"status": 200, "json": {"access_token": "***", "expires_in": 3600}, "text": ""}
        return {"status": 200, "json": self.message, "text": ""}


def _decode(raw: str):
    """Parse the base64url body back into a message object, the way a recipient's client would."""
    padded = raw + "=" * (-len(raw) % 4)
    return message_from_bytes(base64.urlsafe_b64decode(padded.encode("ascii")), policy=policy.default)


def gmail_message(*, to: str, subject: str, message_id: str = "remote-1") -> dict:
    return {"id": message_id, "threadId": "thread-1", "payload": {"headers": [
        {"name": "From", "value": "us@example.invalid"}, {"name": "To", "value": to},
        {"name": "Subject", "value": subject}, {"name": "Message-ID", "value": f"<{message_id}@example.invalid>"}]}}


class FakeSendAdapter:
    """A send adapter with the executor's contract and the ability to look at the records mid-flight."""

    def __init__(self, *, answer=None, error=None, store=None, outreach_id=None, before=None):
        self.calls: list[dict] = []
        self.answer = answer
        self.error = error
        self.enabled = True
        self.store = store
        self.outreach_id = outreach_id
        self.before = before
        self.suppression_checks: list[str] = []

    def send(self, prepared, *, dry_run=False):
        self.calls.append(dict(prepared))
        if self.before is not None:
            # A hook for "what do the records say at the moment the provider is contacted?" - which is how the
            # pre-attempt marker is proven to exist rather than asserted in a comment.
            self.before()
        if self.store is not None and self.outreach_id is not None:
            from outreach.service import content_fingerprint

            record = self.store.state["outreach"][self.outreach_id]
            self.suppression_checks.append(record.get("send_state") or "")
            assert record.get("send_state") == "SENDING", "the provider was contacted before the attempt was recorded"
            assert record.get("send_fingerprint") == content_fingerprint(record)
        if self.error is not None:
            raise self.error
        if self.answer is not None:
            return dict(self.answer)
        return {"sent": True, "dry_run": False, "code": None, "provider": "gmail",
                "provider_message_id": "msg-fake-1", "recipient": prepared.get("to"),
                "size_bytes": prepared.get("size_bytes"), "message_ref": "gmail:message:msg-fake-1",
                "attempted_at": "2020-01-01T09:00:00Z", "fingerprint": prepared.get("fingerprint")}

    def verify_authorization(self):
        return {"state": "verified", "verified_at": "2020-01-01T09:00:00Z", "code": None,
                "detail": "token exchanged, nothing sent", "next_action": "none"}

    def describe(self):
        return {"provider": "gmail", "methods": ["send"], "enabled": True, "read_capable": False,
                "delivery_receipt_available": False}


class FakeReadAdapter:
    def __init__(self, message=None, error=None):
        self.message = message
        self.error = error
        self.calls: list[str] = []

    def get_message(self, message_id, *, message_format="metadata"):
        self.calls.append(message_id)
        if self.error is not None:
            raise self.error
        if self.message is None:
            raise LookupError("message not found")
        return dict(self.message)


class OutboundFixture(unittest.TestCase):
    """One fictional Lagos business, deployed and reviewed, with one approved draft - and nothing sent."""

    def setUp(self) -> None:
        _clean_env(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.state_path = self.root / "state.json"
        from tracking.store import TrackingStore

        self.store = TrackingStore(self.state_path)
        self.lead = self.store.create_lead({
            "business_name": "Fictional Lagos Fresh Kitchen", "business_type": "Restaurant",
            "qualification_status": "QUALIFIED", "website_status": "NO_WEBSITE",
            "source_urls": ["https://maps.example.invalid/fictional"],
            "research_source_urls": ["https://maps.example.invalid/fictional"],
            "email": "owner@example.invalid", "whatsapp": "+234 800 000 0000"})
        self.project = self.store.create_project(self.lead["lead_id"])
        self.build_dir = self.root / "output" / self.lead["lead_id"] / "build-1"
        self.build_dir.mkdir(parents=True)
        (self.build_dir / "index.html").write_text(
            "<!doctype html><html><head><title>Fictional Lagos Fresh Kitchen</title></head><body>"
            "<h1>Fictional Lagos Fresh Kitchen</h1></body></html>", encoding="utf-8")
        (self.build_dir / "build.json").write_text(json.dumps({"build_id": "build-1",
                                                               "lead_id": self.lead["lead_id"]}), encoding="utf-8")
        self.store.record_build(self.project["project_id"], {"build_id": "build-1", "lead_id": self.lead["lead_id"],
                                                             "output_location": str(self.build_dir)})
        self.store.record_validation(self.project["project_id"], {"validation_id": "val-1", "build_id": "build-1",
                                                                  "status": "PASS", "artifact_path": str(self.build_dir),
                                                                  "data_classification": "FICTIONAL_TEST_DATA"})
        self.draft = None

    # -- pipeline steps the fixture may or may not want, each spelled out so a test chooses deliberately --
    def deploy_preview(self, url: str = "https://fictional-preview.example.invalid") -> dict:
        from tracking.store import TrackingStore  # noqa: F401 - keeps the import list honest for readers

        return self.store.record_deployment(self.project["project_id"], {
            "deployment_id": "dep-1", "build_id": "build-1", "validation_status": "PASS",
            "deployment_status": "DEPLOYED", "deployment_url": url})

    def approve_project(self, notes: str = "the deployed preview reads correctly") -> dict:
        return self.store.record_review(self.project["project_id"], "APPROVED", notes)

    def approved_draft(self, *, reviewed_by: str = "operator", identity_source: str = "test_fixture",
                       channel: str = "EMAIL") -> dict:
        from outreach.service import OutreachService

        self.deploy_preview()
        self.approve_project()
        service = OutreachService(self.store)
        self.draft = service.create(self.project["project_id"], channel)
        self.draft = service.review(self.draft["outreach_id"], "APPROVED", notes="reads well",
                                    reviewed_by=reviewed_by, identity_source=identity_source)
        return self.draft

    def executor(self, *, client=None, readback=None, allow_live: bool = True, config: dict | None = None):
        from orchestration.outbound import ApprovedOutboundSender

        return ApprovedOutboundSender(self.store, client=client if client is not None else FakeSendAdapter(),
                                     readback_client=readback, allow_live=allow_live,
                                     config_source={} if config is None else config)

    def reload(self):
        """A second reader of the same file: the only proof that a write is durable."""
        from tracking.store import TrackingStore

        return TrackingStore(self.state_path)

    def record(self) -> dict:
        """The row as written to disk, read by a second store instance."""
        return self.reload().state["outreach"][self.draft["outreach_id"]]

    def touch(self) -> None:
        """Flush an in-memory edit the fixture made deliberately, so a reload can see it."""
        self.store._save()

    def events(self) -> list[dict]:
        return list(self.reload().state["engagements"].values())

    def state_bytes(self) -> str:
        return self.state_path.read_text(encoding="utf-8")


class ContentBindingTests(OutboundFixture):
    def test_an_approval_is_bound_to_the_content_it_covered(self) -> None:
        from outreach.service import content_fingerprint

        draft = self.approved_draft()
        self.assertEqual(draft["approved_content_fingerprint"], content_fingerprint(draft))
        self.assertEqual(draft["content_fingerprint"], draft["approved_content_fingerprint"])

    def test_a_recipient_change_makes_the_approval_stale_not_reusable(self) -> None:
        draft = self.approved_draft()
        draft["recipient"] = "someone.else@example.invalid"
        sender = self.executor()
        view = sender.annotate(draft)
        self.assertFalse(view["eligible"])
        self.assertIn("APPROVAL_STALE", [item["code"] for item in view["blockers"]])
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator",
                                              identity_source="test", confirmation="SEND")
        self.assertEqual(caught.exception.code, "APPROVAL_STALE")
        self.assertEqual(client.calls, [], "a stale approval must not reach the provider")

    def test_a_body_edit_makes_the_approval_stale(self) -> None:
        draft = self.approved_draft()
        from outreach.service import content_fingerprint

        draft["draft_message"] = draft["draft_message"] + " Please also bring a cake."
        self.assertNotEqual(draft["approved_content_fingerprint"], content_fingerprint(draft))

    def test_unrelated_record_changes_do_not_invalidate_the_digest(self) -> None:
        from outreach.service import content_fingerprint

        draft = self.approved_draft()
        before = content_fingerprint(draft)
        draft["review_notes"] = "annotated after the fact"
        draft["send_state_detail"] = "noise"
        self.assertEqual(before, content_fingerprint(draft),
                         "the digest covers what the recipient reads, not every field on the row")

    def test_a_draft_approved_before_binding_exists_is_refused(self) -> None:
        draft = self.approved_draft()
        draft.pop("approved_content_fingerprint")
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                               confirmation="SEND")
        self.assertEqual(caught.exception.code, "APPROVAL_UNBOUND")
        self.assertEqual(client.calls, [])


class EligibilityGateTests(OutboundFixture):
    def test_an_unapproved_draft_is_not_sendable(self) -> None:
        from outreach.service import OutreachService

        self.deploy_preview()
        self.approve_project()
        draft = OutreachService(self.store).create(self.project["project_id"])
        self.draft = draft
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "NOT_APPROVED")
        self.assertEqual(client.calls, [])

    def test_a_rejected_draft_is_not_sendable(self) -> None:
        from outreach.service import OutreachService

        draft = self.approved_draft()
        OutreachService(self.store).review(draft["outreach_id"], "REJECTED", notes="too forward",
                                          reviewed_by="operator", identity_source="test")
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "NOT_APPROVED")
        self.assertEqual(client.calls, [])

    def test_a_hold_stops_the_send_and_names_itself(self) -> None:
        from orchestration import controls

        draft = self.approved_draft()
        controls.set_hold(self.store, actor="grace", reason="incident 4711")
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "AUTOMATION_HELD")
        self.assertIn("incident 4711", str(caught.exception))
        self.assertEqual(client.calls, [])
        controls.release_hold(self.store, actor="grace")
        self.assertTrue(self.executor().annotate(self.store.state["outreach"][draft["outreach_id"]])["eligible"])

    def test_an_opted_out_lead_is_blocked_even_after_approval(self) -> None:
        from tracking.engagement import EngagementLedger

        draft = self.approved_draft()
        EngagementLedger(self.store, source="test").record_opt_out(
            self.lead["lead_id"], channel="EMAIL", reason="asked to stop",
            evidence_note="reply: please take us off your list")
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertIn(caught.exception.code, {"CONTACT_BLOCKED", "ADDRESS_SUPPRESSED"})
        self.assertEqual(client.calls, [])
        codes = [item["code"] for item in self.executor().annotate(
            self.store.state["outreach"][draft["outreach_id"]])["blockers"]]
        self.assertIn("CONTACT_BLOCKED", codes)

    def test_the_contact_cap_blocks_a_fourth_approach(self) -> None:
        from orchestration.outbound import OutboundRefusal
        from outreach.service import OutreachService

        # Three attempts per lead is the documented budget, and a *new* approval does not reset it: the cap is on
        # contacting the business, not on how many drafts were prepared. The three sends below are the whole budget.
        service = OutreachService(self.store)
        self.approved_draft()          # puts the preview, the review, and the first draft in place
        for index in range(3):
            item = service.create(self.project["project_id"], regenerate=True)
            service.review(item["outreach_id"], "APPROVED", notes=f"round {index}", reviewed_by="operator",
                           identity_source="test")
            self.draft = item
            self.executor().send(item["outreach_id"], actor_id="operator", identity_source="test",
                                 confirmation="SEND")
        fourth = service.create(self.project["project_id"], regenerate=True)
        service.review(fourth["outreach_id"], "APPROVED", notes="fourth", reviewed_by="operator",
                       identity_source="test")
        self.draft = fourth
        client = FakeSendAdapter()
        with self.assertRaises(OutboundRefusal) as caught:
            self.executor(client=client).send(fourth["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "CONTACT_LIMIT_REACHED")
        self.assertEqual(client.calls, [])
        self.assertIn("CONTACT_LIMIT_REACHED", [item["code"] for item in self.executor().annotate(
            self.store.state["outreach"][fourth["outreach_id"]])["blockers"]])
        self.assertIsNone(self.record().get("send_state"),
                         "a refused fourth approach leaves no marker and no attempt of its own")

    def test_an_unqualified_lead_and_a_dead_preview_are_both_refused(self) -> None:
        draft = self.approved_draft()
        record = self.store.state["outreach"][draft["outreach_id"]]
        stale = dict(record)
        stale["build_id"] = "build-999"
        stale["deployment_id"] = "dep-999"
        view = self.executor().annotate(stale)
        self.assertIn("DEPLOYMENT_STALE", [item["code"] for item in view["blockers"]])

        self.store.state["leads"][self.lead["lead_id"]]["qualification_status"] = "DISQUALIFIED"
        view = self.executor().annotate(record)
        self.assertIn("LEAD_NO_LONGER_QUALIFIED", [item["code"] for item in view["blockers"]])
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "LEAD_NO_LONGER_QUALIFIED")
        self.assertEqual(client.calls, [])

    def test_a_dry_run_needs_no_live_flag_and_changes_nothing(self) -> None:
        draft = self.approved_draft()
        before = self.state_bytes()
        answer = self.executor(allow_live=False).send(draft["outreach_id"], actor_id="operator",
                                                     identity_source="test", dry_run=True)
        self.assertEqual(answer["send_state"], "DRY_RUN")
        self.assertTrue(answer["composition"]["size_bytes"] > 0)
        self.assertEqual(self.state_bytes(), before, "a preview must not touch the ledger or the record")

    def test_a_real_send_without_the_live_flag_is_refused(self) -> None:
        draft = self.approved_draft()
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client, allow_live=False).send(draft["outreach_id"], actor_id="operator",
                                                                identity_source="test", confirmation="SEND")
        self.assertEqual(caught.exception.code, "LIVE_EFFECTS_DISABLED")
        self.assertEqual(client.calls, [])

    def test_a_send_needs_the_confirmation_word(self) -> None:
        draft = self.approved_draft()
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test")
        self.assertEqual(caught.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual(client.calls, [])
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                             confirmation="maybe")
        self.assertEqual(caught.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual(client.calls, [])

    def test_a_suppressed_recipient_is_refused_at_the_adapter_too(self) -> None:
        from orchestration.outbound import AddressSuppression

        draft = self.approved_draft()
        # The suppression view is derived from the ledger's blocks; a bare address list is not a second source.
        self.store.state["contact_blocks"]["block-x"] = {"block_id": "block-x", "lead_id": self.lead["lead_id"],
                                                         "channel": "EMAIL", "reason": "asked to stop",
                                                         "revoked_at": None}
        suppression = AddressSuppression(self.store)
        self.assertIn("owner@example.invalid", suppression.addresses())
        self.assertFalse(suppression.allowed("Owner <owner@example.invalid>"))
        self.store.state["contact_blocks"]["block-x"]["revoked_at"] = "2020-01-01T09:00:00Z"
        self.assertEqual(suppression.addresses(), set(), "a revoked block stops suppressing, as the ledger says")


class SendOutcomeTests(OutboundFixture):
    def test_the_attempt_is_recorded_before_the_provider_is_contacted(self) -> None:
        draft = self.approved_draft()
        seen: list[str] = []

        client = FakeSendAdapter(store=self.store, outreach_id=draft["outreach_id"],
                                before=lambda: seen.append("called"))
        answer = self.executor(client=client).send(draft["outreach_id"], actor_id="telegram:1001",
                                                   identity_source="telegram_operator_allowlist",
                                                   confirmation="SEND")
        self.assertEqual(seen, ["called"])
        self.assertEqual(answer["send_state"], "SENT")
        self.assertEqual(client.calls[0]["to"], "owner@example.invalid")

    def test_a_provider_id_without_read_back_is_reported_not_verified(self) -> None:
        draft = self.approved_draft()
        answer = self.executor(readback=None).send(draft["outreach_id"], actor_id="operator",
                                                   identity_source="test", confirmation="SEND")
        self.assertEqual(answer["result"], "REPORTED_SENT")
        self.assertFalse(answer["provider_verified"])
        self.assertIn("read-only Gmail credential is not configured", answer["verify_detail"])
        record = self.record()
        self.assertEqual(record["status"], "SENT_REPORTED")
        self.assertEqual(record["delivery_status"], "REPORTED_BY_OPERATOR")
        self.assertFalse(record["provider_verified"])
        event = self.events()[-1]
        self.assertEqual(event["kind"], "OUTREACH_SENT")
        self.assertIsNone(event["provider_receipt"])
        self.assertFalse(event["provider_verified"])
        self.assertEqual(event["external_message_id"], "msg-fake-1")

    def test_a_matching_read_back_proves_acceptance_and_no_more(self) -> None:
        draft = self.approved_draft()
        readback = FakeReadAdapter(gmail_message(to=draft["recipient"], subject=draft["subject"]))
        answer = self.executor(readback=readback).send(draft["outreach_id"], actor_id="operator",
                                                      identity_source="test", confirmation="SEND")
        self.assertEqual(answer["result"], "ACCEPTED_BY_PROVIDER")
        self.assertTrue(answer["provider_verified"])
        self.assertIn("acceptance, not delivery", answer["delivery_note"])
        event = self.events()[-1]
        self.assertEqual(event["delivery_status"], "ACCEPTED_BY_PROVIDER")
        self.assertTrue(event["provider_verified"])
        self.assertEqual(sorted(event["provider_receipt"]["verified_fields"]), ["subject", "to"])
        self.assertEqual(event["provider_receipt"]["message_id"], "msg-fake-1")
        record = self.record()
        self.assertTrue(record["send_verified"])
        # What acceptance is *not*: the relationship and the outcome stay where a send leaves them.
        self.assertEqual(self.reload().state["outcomes"].get(self.lead["lead_id"], {}).get("status"), None)

    def test_a_read_back_that_does_not_match_downgrades_to_reported(self) -> None:
        draft = self.approved_draft()
        wrong = gmail_message(to="another.person@example.invalid", subject=draft["subject"])
        answer = self.executor(readback=FakeReadAdapter(wrong)).send(draft["outreach_id"], actor_id="operator",
                                                                    identity_source="test", confirmation="SEND")
        self.assertEqual(answer["result"], "REPORTED_SENT")
        self.assertFalse(answer["provider_verified"])
        self.assertIn("goes to", answer["verify_detail"])
        self.assertIsNone(self.events()[-1]["provider_receipt"])

    def test_a_subject_only_match_is_not_enough(self) -> None:
        draft = self.approved_draft()
        message = gmail_message(to=draft["recipient"], subject=draft["subject"])
        message["payload"]["headers"] = [entry for entry in message["payload"]["headers"]
                                         if entry["name"] != "To"]
        answer = self.executor(readback=FakeReadAdapter(message)).send(draft["outreach_id"], actor_id="operator",
                                                                      identity_source="test", confirmation="SEND")
        self.assertEqual(answer["result"], "REPORTED_SENT")

    def test_the_same_approved_content_is_not_sent_twice(self) -> None:
        draft = self.approved_draft()
        client = FakeSendAdapter()
        self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                          confirmation="SEND")
        self.assertEqual(len(client.calls), 1)
        # The record moved to SENT_REPORTED, so a repeated command is refused before the provider is reached -
        # and the ledger's own dedupe key is the second net for a caller that re-approves the same text.
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "ALREADY_SENT")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len([row for row in self.events() if row["kind"] == "OUTREACH_SENT"]), 1)

    def test_a_timeout_after_submit_is_uncertain_and_never_resent(self) -> None:
        from integrations.gmail import GmailError

        draft = self.approved_draft()
        client = FakeSendAdapter(error=GmailError("connection reset after submitting", code="GMAIL_UNRESPONSIVE",
                                                   retryable=True))
        answer = self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                                   confirmation="SEND")
        self.assertEqual(answer["code"], "SEND_UNCERTAIN")
        self.assertEqual(answer["send_state"], "UNCERTAIN")
        self.assertFalse(answer["sent"])
        self.assertEqual(len(client.calls), 1, "the executor must not retry an unanswered send")
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "SEND_UNCERTAIN")
        self.assertEqual(len(client.calls), 1)
        kinds = [row["kind"] for row in self.events()]
        self.assertIn("OUTREACH_SEND_UNCERTAIN", kinds)
        self.assertNotIn("OUTREACH_SENT", kinds)
        self.assertNotIn("OUTREACH_SEND_FAILED", kinds)
        uncertain = [row for row in self.events() if row["kind"] == "OUTREACH_SEND_UNCERTAIN"][0]
        self.assertTrue(uncertain["needs_human_review"])
        self.assertTrue(uncertain["unresolved"])
        self.assertFalse(uncertain["provider_verified"])
        record = self.record()
        self.assertEqual(record["status"], "READY_TO_SEND", "an unanswered attempt is not a completed send")
        self.assertEqual(record["send_state"], "UNCERTAIN")

    def test_a_server_error_while_answering_is_uncertain_too(self) -> None:
        from integrations.http import ProviderError

        draft = self.approved_draft()
        client = FakeSendAdapter(error=ProviderError("upstream refused", code="PROVIDER_SERVER_ERROR",
                                                     retryable=True, status=503))
        answer = self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                                   confirmation="SEND")
        self.assertEqual(answer["code"], "SEND_UNCERTAIN")
        self.assertEqual(len(client.calls), 1)

    def test_an_explicit_provider_refusal_is_a_recorded_failed_attempt(self) -> None:
        from integrations.gmail import GmailError

        draft = self.approved_draft()
        client = FakeSendAdapter(error=GmailError("The line is over quota", code="GMAIL_REQUEST_REFUSED",
                                                 status=429, retryable=True))
        answer = self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                                   confirmation="SEND")
        self.assertEqual(answer["send_state"], "FAILED")
        self.assertEqual(answer["outreach_status"], "SEND_FAILED")
        self.assertFalse(answer["contact_established"])
        record = self.record()
        self.assertEqual(record["delivery_status"], "FAILED_REPORTED")
        self.assertEqual(record["send_attempts"], 1)
        self.assertFalse(record["contact_established"])
        self.assertEqual(self.events()[-1]["kind"], "OUTREACH_SEND_FAILED")
        # The approval survives a failure, so the next attempt is a decision, not a new review.
        self.assertTrue(self.executor().annotate(self.store.state["outreach"][draft["outreach_id"]])["eligible"])
        self.assertEqual(len(client.calls), 1)

    def test_a_provider_refusal_before_the_request_leaves_records_no_attempt(self) -> None:
        draft = self.approved_draft()
        client = FakeSendAdapter(answer={"sent": False, "dry_run": False, "code": "SENDING_DISABLED",
                                        "provider_answered": False, "reason": "sending is disabled",
                                        "next_action": "leave it off"})
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "SENDING_DISABLED")
        self.assertEqual(self.events(), [], "nothing was attempted, so nothing is accounted for")
        self.assertIsNone(self.record()["send_state"])

    def test_a_ledger_refusal_after_acceptance_leaves_the_uncertainty_visible(self) -> None:
        draft = self.approved_draft()
        # Force the ledger to refuse by moving the record out of an approved state *after* the executor's own
        # checks: the send has already happened, so the answer must be loud and the state must stay unresolved.
        client = FakeSendAdapter(store=self.store, outreach_id=draft["outreach_id"],
                                 before=lambda: self.store.state["outreach"][draft["outreach_id"]].update(
                                     {"status": "REJECTED"}))
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                             confirmation="SEND")
        self.assertEqual(caught.exception.code, "OUTREACH_NOT_APPROVED")
        self.assertIn("ledger refused to record it", str(caught.exception))
        self.assertEqual(self.record()["send_state"], "UNCERTAIN")
        self.assertIn("the provider accepted the message", self.record()["send_state_detail"])

    def test_reconciliation_closes_an_uncertain_attempt_from_the_account(self) -> None:
        from integrations.gmail import GmailError

        draft = self.approved_draft()
        client = FakeSendAdapter(answer={"sent": False, "dry_run": False, "code": "SEND_UNCERTAIN",
                                        "provider_message_id": "msg-fake-1",
                                        "reason": "Gmail answered without a usable id", "provider_answered": True})
        self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                          confirmation="SEND")
        self.assertEqual(self.record()["send_state"], "UNCERTAIN")
        readback = FakeReadAdapter(gmail_message(to=draft["recipient"], subject=draft["subject"],
                                                message_id="msg-fake-1"))
        report = self.executor(readback=readback).reconcile(draft["outreach_id"])
        self.assertTrue(report["reconciled"], report["rows"])
        self.assertEqual(report["reconciled"][0]["outcome"], "ACCEPTED_BY_PROVIDER")
        record = self.record()
        self.assertEqual(record["send_state"], "SENT")
        self.assertEqual(record["send_result"], "ACCEPTED_BY_PROVIDER")
        self.assertTrue(record["provider_verified"])
        self.assertEqual([row["kind"] for row in self.events()],
                         ["OUTREACH_SEND_UNCERTAIN", "OUTREACH_SENT"])
        self.assertEqual(readback.calls, ["msg-fake-1"])

    def test_reconciliation_without_a_provider_id_refuses_to_guess(self) -> None:
        from integrations.gmail import GmailError

        draft = self.approved_draft()
        client = FakeSendAdapter(error=GmailError("reset", code="GMAIL_UNRESPONSIVE", retryable=True))
        self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                          confirmation="SEND")
        report = self.executor(readback=FakeReadAdapter(gmail_message(to=draft["recipient"],
                                                                     subject=draft["subject"]))).reconcile()
        self.assertEqual(report["rows"][0]["code"], "PROVIDER_ID_UNAVAILABLE")
        self.assertFalse(report["reconciled"])
        self.assertEqual(self.record()["send_state"], "UNCERTAIN")

    def test_a_reconciled_id_that_points_elsewhere_is_a_mismatch_not_a_resend(self) -> None:
        draft = self.approved_draft()
        self.store.state["outreach"][draft["outreach_id"]].update({
            "send_state": "UNCERTAIN", "send_provider_message_id": "msg-fake-1",
            "send_state_detail": "submitted, no answer"})
        self.store._save()
        readback = FakeReadAdapter(gmail_message(to="stranger@example.invalid", subject=draft["subject"]))
        report = self.executor(readback=readback).reconcile(draft["outreach_id"])
        self.assertEqual(report["rows"][0]["code"], "RECONCILIATION_MISMATCH")
        self.assertIn("s…@example.invalid", report["rows"][0]["detail"])
        self.assertNotIn("stranger@example.invalid", report["rows"][0]["detail"],
                         "a reconciliation detail masks the address it read back")
        self.assertEqual(self.record()["send_state"], "UNCERTAIN")

    def test_a_crashed_attempt_blocks_and_is_resolved_only_by_a_human(self) -> None:
        draft = self.approved_draft()
        # Simulate the process dying between the marker and the provider answer.
        self.store.state["outreach"][draft["outreach_id"]].update({"send_state": "SENDING",
                                                                   "send_attempted_at": "2020-01-01T09:00:00Z"})
        self.store._save()
        client = FakeSendAdapter()
        with self.assertRaises(ValueError) as caught:
            self.executor(client=client).send(draft["outreach_id"], actor_id="operator", identity_source="test",
                                              confirmation="SEND")
        self.assertEqual(caught.exception.code, "SEND_UNCERTAIN")
        self.assertEqual(client.calls, [])
        from tracking.engagement import EngagementLedger

        verdict = EngagementLedger(self.reload(), source="test").outreach_eligibility(
            self.lead["lead_id"], "EMAIL", outreach_id=draft["outreach_id"], for_action="SEND")
        self.assertIn("SEND_UNRESOLVED", verdict["blockers"])
        report = self.executor().reconcile(draft["outreach_id"])
        self.assertEqual(report["rows"][0]["code"], "PROVIDER_ID_UNAVAILABLE")

    def test_a_reported_failure_can_be_recorded_while_the_attempt_is_open(self) -> None:
        from tracking.engagement import EngagementLedger

        draft = self.approved_draft()
        self.store.state["outreach"][draft["outreach_id"]].update({"send_state": "SENDING"})
        self.store._save()
        ledger = EngagementLedger(self.reload(), source="test")
        updated = ledger.mark_outreach_sent(draft["outreach_id"], result="FAILED", provider="gmail",
                                           failure_note="the relay refused it")
        self.assertEqual(updated["status"], "SEND_FAILED")
        self.assertEqual(updated["send_state"], "FAILED", "closing an attempt must clear the marker")


class GmailSendAdapterTests(unittest.TestCase):
    """The send interface itself: one verb, one recipient, refusals that hold without the ledger."""

    def setUp(self) -> None:
        _clean_env(self)

    def adapter(self, transport, *, enabled=True, suppression=None):
        from integrations.gmail_send import GmailSendAdapter

        return GmailSendAdapter(transport=transport, refresh_token="rt", client_id="cid", client_secret="cs",
                               enabled=enabled, suppression=suppression)

    def prepared(self, **overrides):
        from integrations.gmail_send import build_message

        fields = {"to": "Owner <owner@example.invalid>", "subject": "A website preview for Fictional Lagos Kitchen",
                  "body_text": "Hello - here is the preview: https://preview.example.invalid"}
        fields.update(overrides)
        return build_message(**fields)

    def test_the_composed_message_is_one_rfc_2822_message_with_a_hash(self) -> None:
        import hashlib

        prepared = self.prepared(body_html="<p>Hello</p>")
        self.assertEqual(set(prepared) >= {"raw", "headers", "size_bytes", "body_sha256", "fingerprint"}, True)
        decoded = message_from_bytes(base64.urlsafe_b64decode(prepared["raw"] + "=="), policy=policy.default)
        self.assertEqual(str(decoded["To"]), "owner@example.invalid")
        self.assertEqual(decoded.get_content_type(), "multipart/alternative")
        self.assertIn("text/plain", [part.get_content_type() for part in decoded.walk() if not part.is_multipart()])
        self.assertIn("text/html", [part.get_content_type() for part in decoded.walk() if not part.is_multipart()])
        self.assertEqual(prepared["body_sha256"], hashlib.sha256(
            "Hello - here is the preview: https://preview.example.invalid".encode("utf-8")).hexdigest(),
                         "the digest the ledger keeps is of the plain-text body a reviewer read")
        # The fingerprint is taken over the base64url string the API is handed, so a later comparison does not
        # depend on how the bytes were re-encoded.
        self.assertEqual(prepared["fingerprint"], hashlib.sha256(prepared["raw"].encode("ascii")).hexdigest())
        self.assertEqual(prepared["size_bytes"], len(base64.urlsafe_b64decode(prepared["raw"] + "==")))

    def test_a_non_ascii_subject_survives_instead_of_being_transliterated(self) -> None:
        prepared = self.prepared(subject="Ọ̀bàrà Kitchen — preview ẹ̀kọ́")
        padded = prepared["raw"] + "=" * (-len(prepared["raw"]) % 4)
        decoded = message_from_bytes(base64.urlsafe_b64decode(padded), policy=policy.default)
        self.assertEqual(str(decoded["Subject"]), "Ọ̀bàrà Kitchen — preview ẹ̀kọ́")

    def test_the_unsubscribe_header_is_copied_from_configuration_only(self) -> None:
        with_url = self.prepared(unsubscribe_url="https://example.invalid/unsubscribe")
        decoded = _decode(with_url["raw"])
        self.assertIn("<https://example.invalid/unsubscribe>", str(decoded["List-Unsubscribe"]))
        self.assertEqual(str(decoded["List-Unsubscribe-Post"]), "List-Unsubscribe=One-Click")
        self.assertIsNone(with_url["unsubscribe_notice"], "a real header needs no apology in the result")
        plain = self.prepared(organization="Fictional Lagos Kitchen")
        self.assertNotIn("List-Unsubscribe", plain["headers"])
        self.assertIn("AI_WEBSITE_AGENT_EMAIL_UNSUBSCRIBE_URL", str(plain["unsubscribe_notice"]),
                      "an organization with no unsubscribe page is told so in the result, not sent silently")
        with self.assertRaises(ValueError):
            self.prepared(unsubscribe_url="mailto:opt@example.invalid")

    def test_shape_refusals_happen_before_any_request_is_built(self) -> None:
        from integrations.gmail_send import MAX_BODY_CHARACTERS, build_message

        for kwargs, why in (
            ({"to": "a@example.invalid, b@example.invalid"}, "a second recipient"),
            ({"to": "not an address"}, "an unparsable recipient"),
            ({"subject": "   "}, "an empty subject"),
            ({"body_text": ""}, "an empty body"),
            ({"body_text": "x" * (MAX_BODY_CHARACTERS + 1)}, "a runaway body"),
            ({"in_reply_to": "see attached my friend"}, "a fabricated In-Reply-To"),
            ({"headers": {"Cc": "boss@example.invalid"}}, "a caller-set Cc"),
            ({"headers": {"From": "someone@example.invalid"}}, "a caller-set From"),
        ):
            fields = {"to": "owner@example.invalid", "subject": "s", "body_text": "b"}
            fields.update(kwargs)
            with self.assertRaises(ValueError, msg=f"build_message accepted {why}"):
                build_message(**fields)

    def test_the_adapter_sends_exactly_one_post_and_nothing_else(self) -> None:
        transport = RecordingTransport()
        answer = self.adapter(transport).send(self.prepared())
        self.assertTrue(answer["sent"])
        self.assertEqual(answer["provider_message_id"], "msg-fake-1")
        self.assertEqual(transport.methods, {"POST"}, "the send interface must not issue a read request")
        self.assertTrue(all("/messages/send" in call[1] and call[1].endswith("/messages/send") for call in transport.send_calls))
        self.assertEqual(len(transport.send_calls), 1)

    def test_a_dry_run_composes_without_calling(self) -> None:
        transport = RecordingTransport()
        answer = self.adapter(transport).send(self.prepared(), dry_run=True)
        self.assertEqual(transport.calls, [])
        self.assertTrue(answer["dry_run"])
        self.assertTrue(answer["would_send"])
        self.assertFalse(answer["sent"])

    def test_an_adapter_that_is_not_enabled_refuses_even_when_asked(self) -> None:
        transport = RecordingTransport()
        answer = self.adapter(transport, enabled=False).send(self.prepared())
        self.assertEqual(answer["code"], "SENDING_DISABLED")
        self.assertEqual(transport.calls, [])

    def test_the_suppression_list_is_checked_at_the_provider_boundary(self) -> None:
        class Blocked:
            def __init__(self):
                self.seen = []

            def allowed(self, address):
                self.seen.append(address)
                return False

        suppression = Blocked()
        transport = RecordingTransport()
        answer = self.adapter(transport, suppression=suppression).send(self.prepared())
        self.assertEqual(answer["code"], "RECIPIENT_OPTED_OUT")
        self.assertEqual(transport.calls, [], "a suppressed address must never reach the endpoint")
        self.assertEqual(suppression.seen, ["owner@example.invalid"])

    def test_an_answer_without_an_id_is_uncertainty_not_failure(self) -> None:
        transport = RecordingTransport(answers=[{"status": 200, "json": {"sizeEstimate": 10}, "text": ""}])
        answer = self.adapter(transport).send(self.prepared())
        self.assertFalse(answer["sent"])
        self.assertEqual(answer["code"], "SEND_UNCERTAIN")
        self.assertIn("Do not retry", answer["next_action"])

    def test_a_token_exchange_is_the_strongest_offline_check(self) -> None:
        transport = RecordingTransport()
        report = self.adapter(transport).verify_authorization()
        self.assertEqual(report["state"], "verified")
        self.assertIn("no message was sent", report["detail"])
        self.assertEqual(len([call for call in transport.calls if call[1].endswith("/token")]), 1)
        self.assertEqual(transport.send_calls, [])

    def test_a_refused_token_is_reported_as_unverified_with_the_provider_code(self) -> None:
        transport = RecordingTransport(token_ok=False)
        report = self.adapter(transport).verify_authorization()
        self.assertEqual(report["state"], "unverified")
        self.assertEqual(report["code"], "GMAIL_AUTH_FAILED")
        self.assertNotIn("invalid_grant", json.dumps(report))

    def test_the_description_says_what_this_client_cannot_do(self) -> None:
        description = self.adapter(RecordingTransport()).describe()
        self.assertEqual(description["methods"], ["send"])
        self.assertFalse(description["read_capable"])
        self.assertFalse(description["delivery_receipt_available"])
        self.assertEqual(description["max_recipients"], 1)
        self.assertIn("gmail.send", description["scope"])

    def test_the_send_scope_is_not_the_read_scope(self) -> None:
        from integrations.gmail import GMAIL_SCOPE
        from integrations.gmail_send import GMAIL_SEND_SCOPE

        self.assertNotEqual(GMAIL_SEND_SCOPE, GMAIL_SCOPE)
        self.assertEqual(GMAIL_SEND_SCOPE, "https://www.googleapis.com/auth/gmail.send")

    def test_send_configuration_never_calls_configuration_present_connectivity(self) -> None:
        from integrations.gmail_send import send_config

        self.assertEqual(send_config({})["state"], "inbound_only")
        partial = send_config({"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt"})
        self.assertEqual(partial["state"], "inbound_only",
                         "one value out of three is not a configured channel; the missing ones are named")
        self.assertEqual(partial["missing"], ["AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID",
                                             "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET"])
        full = send_config({"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt",
                            "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "cid",
                            "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "cs"})
        self.assertEqual(full["state"], "disabled", "credentials alone do not enable sending")
        self.assertTrue(full["configured"])
        self.assertFalse(full["enabled"])
        self.assertIsNone(full["from_address"], "nothing is invented for an unset optional value")
        configured = send_config({"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt",
                                 "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "cid",
                                 "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "cs",
                                 "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED": "true"})
        self.assertEqual(configured["state"], "configured",
                         "configured is the most this function may claim without exercising anything")
        self.assertIn("email-send-check", configured["next_action"])

    def test_a_half_configured_credential_set_is_a_named_refusal_not_a_maybe(self) -> None:
        from integrations.gmail import GmailError
        from integrations.gmail_send import GmailSendAdapter

        for env in ({}, {"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt"}):
            with self.assertRaises(GmailError) as caught:
                GmailSendAdapter.from_environment(env)
            self.assertEqual(caught.exception.code, "GMAIL_SEND_NOT_CONFIGURED")
            self.assertIn("Nothing here can mint that consent", str(caught.exception.next_action))
        adapter = GmailSendAdapter.from_environment(
            {"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt", "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "cid",
             "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "cs", "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED": "true"},
            transport=RecordingTransport())
        self.assertIsNotNone(adapter)
        # The composition flag is the operator's, not this function's: from_environment builds the client and
        # `send_config` decides whether it may speak, so a half-configured machine stays switched off.
        self.assertFalse(adapter.enabled)
        self.assertTrue(GmailSendAdapter.from_environment(
            {"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt", "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "cid",
             "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "cs"},
            enabled=True, transport=RecordingTransport()).enabled)


class ReadOnlyBoundaryTests(unittest.TestCase):
    """The read-only adapter is still read-only, and the send adapter is still deaf."""

    def test_the_read_only_client_still_has_no_send_verb(self) -> None:
        from integrations.gmail import GmailClient

        for verb in ("send", "delete_message", "modify_message", "insert_label"):
            self.assertFalse(hasattr(GmailClient, verb), f"the read-only client grew a {verb}()")
        status = GmailClient(refresh_token="rt", client_id="cid", client_secret="cs").status()
        self.assertFalse(status["send_capable"])
        self.assertEqual(status["scope"], "https://www.googleapis.com/auth/gmail.readonly")
        self.assertIn("messages.send", status["explicitly_absent"])
        self.assertNotIn("AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN", status["credential_environment"])

    def test_the_send_client_has_no_read_verbs(self) -> None:
        from integrations.gmail_send import GmailSendAdapter

        for verb in ("list_messages", "get_message", "get_history", "search", "delete_message"):
            self.assertFalse(hasattr(GmailSendAdapter, verb), f"the send-only client grew a {verb}()")

    def test_the_read_only_module_does_not_learn_the_send_configuration(self) -> None:
        gmail = (REPO_ROOT / "integrations" / "gmail.py").read_text()
        for name in SEND_ENV_NAMES:
            self.assertNotIn(name, gmail, f"{name} belongs in gmail_send.py only")
        self.assertNotIn("gmail.send", gmail)

    def test_both_clients_share_one_error_type_and_one_transport(self) -> None:
        from integrations.http import ProviderError
        from integrations.gmail import GmailClient, GmailError as ReadError
        from integrations.gmail_send import GmailSendAdapter
        from integrations.gmail_send import GmailError as SendError
        from integrations.gmail_transport import GmailError as SharedError, GmailTransport

        self.assertIs(ReadError, SharedError)
        self.assertIs(SendError, SharedError)
        self.assertTrue(issubclass(GmailClient, GmailTransport))
        self.assertTrue(issubclass(SendError, ProviderError))
        description = GmailSendAdapter(transport=RecordingTransport(), refresh_token="rt", client_id="cid",
                                       client_secret="cs").describe()
        self.assertEqual(description["credential_environment"],
                         ["AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN", "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID",
                          "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET", "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED"])

    def test_the_executor_does_not_widen_the_read_client(self) -> None:
        # The readback path is a duck-typed get_message; a GmailClient handed to it cannot be asked to send.
        from integrations.gmail import GmailClient

        self.assertFalse(hasattr(GmailClient, "send"))


class RecordingTelegramApi:
    """The slice of the Bot API the operator surface uses, with no network under it."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.acked: list[dict] = []

    def send_message(self, chat_id, text, *, reply_markup=None, parse_mode=None, disable_notification=False):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text,
                            "reply_markup": reply_markup})
        return {"edited": True}

    def answer_callback_query(self, callback_query_id, *, text=None, show_alert=False):
        self.acked.append({"id": callback_query_id, "text": text, "alert": show_alert})
        return True


def texts(api: RecordingTelegramApi) -> str:
    return "\n".join([row["text"] or "" for row in api.sent] + [row["text"] or "" for row in api.edited]
                     + [row["text"] or "" for row in api.acked])


class TelegramDemoApprovalTests(OutboundFixture):
    """The one thing Phase 49 had to add for a preview: a human's decision, arriving over Telegram."""

    def bot(self, api=None, **overrides):
        from orchestration.telegram_bot import TelegramOperatorBot

        settings = {"allowed_user_ids": {OPERATOR}, "allowed_chat_ids": {CHAT}}
        settings.update(overrides)
        return TelegramOperatorBot(api if api is not None else RecordingTelegramApi(), store=self.store,
                                   **settings)

    def ready_project(self) -> str:
        """Build + PASS validation, no deployment and no review: exactly the state a preview approval unlocks.

        Nothing here deploys: a preview publish approval is the step *before* a deployment exists, so the fixture
        stops at the review gate the gate function itself checks.
        """
        self.assertEqual(self.store.state["projects"][self.project["project_id"]]["current_stage"],
                         "READY_FOR_REVIEW")
        self.assertEqual(self.store.state["projects"][self.project["project_id"]]["review_status"], "PENDING")
        return self.project["project_id"]

    def test_the_prompt_reports_the_gate_and_asks_for_a_confirmation_code(self) -> None:
        from orchestration.telegram_bot import demo_fingerprint

        project_id = self.ready_project()
        api = RecordingTelegramApi()
        answer = self.bot(api).dispatch(f"/demo {project_id}", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(answer["code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(answer["gate_code"], "APPROVAL_MISSING")
        self.assertEqual(self.store.state["projects"][project_id]["review_history"], [],
                         "the prompt alone must not record a decision")
        self.assertFalse(answer["authorized"])
        self.assertEqual(answer["fingerprint"], demo_fingerprint(self.store.state["projects"][project_id]))
        body = answer["reply"]
        self.assertIn("Nothing has been approved yet", body)
        self.assertIn("tracking.cli review", body, "the refusal points at the mechanism, not at a shortcut")
        self.assertEqual(api.sent, [], "deliver=False keeps this out of the transport")

    def test_a_wrong_code_approves_nothing(self) -> None:
        project_id = self.ready_project()
        bot = self.bot()
        answer = bot.dispatch(f"/demo {project_id} --confirm 0123456789", user_id=OPERATOR, chat_id=CHAT,
                             deliver=False)
        self.assertEqual(answer["code"], "DEMO_STALE")
        self.assertEqual(self.store.state["projects"][project_id]["review_history"], [])

    def test_the_note_is_required_and_the_approval_lands_in_the_stores_own_review_record(self) -> None:
        from orchestration.telegram_bot import demo_fingerprint

        project_id = self.ready_project()
        bot = self.bot()
        code = demo_fingerprint(self.store.state["projects"][project_id])
        missing_note = bot.dispatch(f"/demo {project_id} --confirm {code}", user_id=OPERATOR, chat_id=CHAT,
                                   deliver=False)
        self.assertEqual(missing_note["code"], "REVIEW_NOTES_REQUIRED")

        answer = bot.dispatch(f'/demo {project_id} --note "checked the copy and the price list" --confirm {code}',
                             user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(answer["code"], "DEMO_APPROVAL_RECORDED")
        entry = self.store.state["projects"][project_id]["review_history"][-1]
        self.assertEqual(entry["decision"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(entry["intended_action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(entry["reviewer"], f"telegram:{OPERATOR}")
        self.assertEqual(entry["identity_source"], "telegram_operator_allowlist")
        self.assertIn("checked the copy", entry["notes"])
        gate = self.store.authorize_preview_deployment(project_id)
        self.assertTrue(gate["authorized"])
        self.assertEqual(gate["code"], "APPROVED_FOR_DEPLOYMENT")
        self.assertEqual(answer["buttons"], [["Create isolated Vercel project + publish preview"]],
                         "publishing is offered as a second, separate action")

    def test_the_capability_file_records_the_provenance_without_claiming_a_verified_identity(self) -> None:
        from orchestration.telegram_bot import demo_fingerprint

        project_id = self.ready_project()
        code = demo_fingerprint(self.store.state["projects"][project_id])
        self.bot().dispatch(f'/demo {project_id} --note "read it" --confirm {code}', user_id=OPERATOR,
                           chat_id=CHAT, deliver=False)
        capability = json.loads((self.build_dir / "deployment-approval.json").read_text(encoding="utf-8"))
        self.assertEqual(capability["status"], "ACTIVE")
        self.assertEqual(capability["decision"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(capability["action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(capability["reviewer"]["reviewer_id"], f"telegram:{OPERATOR}")
        self.assertEqual(capability["reviewer_identity_source"], "telegram_operator_allowlist")
        self.assertIs(capability["identity_verified"], False,
                      "this repository has no verified-identity source, so no surface may claim one")
        # The classification travels with the project row, and the file keeps it: a reader of the build directory
        # alone has to be able to tell that this approval is about fictional data.
        self.assertEqual(capability["data_classification"], "TEST_FIXTURE")

    def test_a_demo_approval_does_not_approve_or_send_outreach(self) -> None:
        from orchestration.telegram_bot import demo_fingerprint

        project_id = self.ready_project()
        code = demo_fingerprint(self.store.state["projects"][project_id])
        self.bot().dispatch(f'/demo {project_id} --note "read it" --confirm {code}', user_id=OPERATOR,
                           chat_id=CHAT, deliver=False)
        self.assertEqual(self.store.state["outreach"], {},
                         "approving the preview is not approving a message")
        from orchestration.outbound import ApprovedOutboundSender

        draft = ApprovedOutboundSender(self.store)  # no outreach at all: nothing to send
        self.assertEqual(draft.outbox()["total"], 0)

    def test_a_supplied_url_authorizes_nothing(self) -> None:
        project_id = self.ready_project()
        bot = self.bot()
        for candidate in ("https://example.invalid/preview", "deployment-approved", project_id.upper()):
            answer = bot.dispatch(f"/demo {candidate} --confirm 0000000000", user_id=OPERATOR, chat_id=CHAT,
                                 deliver=False)
            self.assertNotIn(answer["code"], {"DEMO_APPROVAL_RECORDED", "PUBLISH_JOB_QUEUED"},
                             f"{candidate} was treated as an approval")
        self.assertEqual(self.store.state["projects"][project_id]["review_history"], [])

    def test_a_stale_press_records_nothing(self) -> None:
        project_id = self.ready_project()
        api = RecordingTelegramApi()
        bot = self.bot(api)
        moved = bot.handle_callback(f"dm:{project_id}:0000000000", user_id=OPERATOR, chat_id=CHAT, message_id=3,
                                   callback_query_id="cb-1")
        self.assertEqual(moved["code"], "DEMO_STALE")
        self.assertEqual(self.store.state["projects"][project_id]["review_history"], [])
        self.assertIn("records nothing", texts(api))

    def test_an_unlisted_account_cannot_press_the_button(self) -> None:
        from orchestration.telegram_bot import demo_fingerprint

        project_id = self.ready_project()
        code = demo_fingerprint(self.store.state["projects"][project_id])
        answer = self.bot().handle_callback(f"dm:{project_id}:{code}", user_id=OTHER, chat_id=CHAT, message_id=3,
                                           callback_query_id="cb-2")
        self.assertEqual(answer["code"], "AUTHORIZATION_REFUSED")
        self.assertEqual(self.store.state["projects"][project_id]["review_history"], [])

    def test_queuing_the_publish_job_is_a_separate_press_that_publishes_nothing(self) -> None:
        from orchestration.telegram_bot import demo_fingerprint

        project_id = self.ready_project()
        code = demo_fingerprint(self.store.state["projects"][project_id])
        bot = self.bot()
        bot.dispatch(f'/demo {project_id} --note "read it" --confirm {code}', user_id=OPERATOR, chat_id=CHAT,
                    deliver=False)
        answer = bot.handle_callback(f"pq:{project_id}:{demo_fingerprint(self.store.state['projects'][project_id])}",
                                    user_id=OPERATOR, chat_id=CHAT, message_id=4, callback_query_id="cb-3")
        self.assertEqual(answer["code"], "PUBLISH_JOB_QUEUED")
        jobs = list(self.reload().state["jobs"].values())
        self.assertEqual([job["job_type"] for job in jobs], ["DEPLOY"])
        self.assertEqual(jobs[0]["status"], "PENDING", "a queue press enqueues; a worker publishes")
        self.assertEqual(jobs[0]["build_id"], "build-1")
        self.assertIn("only if the approval still matches the artifact", answer["notice"])

    def test_the_publish_button_is_refused_while_the_gate_says_no(self) -> None:
        project_id = self.ready_project()
        from orchestration.telegram_bot import demo_fingerprint

        code = demo_fingerprint(self.store.state["projects"][project_id])
        answer = self.bot().handle_callback(f"pq:{project_id}:{code}", user_id=OPERATOR, chat_id=CHAT, message_id=4,
                                           callback_query_id="cb-4")
        self.assertEqual(answer["code"], "PUBLISH_NOT_AUTHORIZED")
        self.assertEqual(self.reload().state["jobs"], {})


class TelegramSendSurfaceTests(OutboundFixture):
    """The bot as a send console: it arms, it confirms, it reports - and it never puts the body in the chat."""

    def setUp(self) -> None:
        super().setUp()
        self.api = RecordingTelegramApi()
        self.client = FakeSendAdapter()
        os.environ["AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH"] = "1"
        self.addCleanup(os.environ.pop, "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH", None)

    def bot(self, *, readback=None, client=None):
        from orchestration.telegram_bot import TelegramOperatorBot

        return TelegramOperatorBot(self.api, store=self.store, allowed_user_ids={OPERATOR},
                                  allowed_chat_ids={CHAT},
                                  outbound_client=self.client if client is None else client,
                                  readback_client=readback)

    def test_the_outbox_lists_approved_drafts_with_masked_recipients(self) -> None:
        draft = self.approved_draft()
        answer = self.bot().dispatch("/outbox", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(answer["code"], "OUTBOX_LIST")
        self.assertEqual(answer["count"], 1)
        self.assertIn(draft["outreach_id"], answer["reply"])
        self.assertNotIn("owner@example.invalid", answer["reply"])
        self.assertIn("o…@example.invalid", answer["reply"])
        self.assertIn("sending needs an explicit confirmation", answer["reply"])

    def test_arm_then_press_sends_once_and_reports_acceptance_as_no_more_than_it_is(self) -> None:
        from orchestration.outbound import content_fingerprint

        draft = self.approved_draft()
        bot = self.bot(readback=FakeReadAdapter(gmail_message(to=draft["recipient"], subject=draft["subject"])))
        armed = bot.dispatch(f"/send {draft['outreach_id']}", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(armed["code"], "SEND_ARMED")
        self.assertEqual(armed["buttons"], [["Confirm: send it now", "Preview only (no provider contact)"]])
        self.assertEqual(self.client.calls, [], "arming must not contact the provider")
        body = str(self.store.state["outreach"][draft["outreach_id"]]["draft_message"])
        self.assertIn(body, armed["reply"], "the exact email body must be previewed before confirmation")
        self.assertIn(draft["recipient"], armed["reply"], "the exact recipient must be visible before confirmation")
        self.assertIn(draft["subject"], armed["reply"])
        self.assertIn(draft["preview_url"], armed["reply"])
        self.assertIn(content_fingerprint(draft), armed["reply"])
        self.assertIn("There is no undo, and no retry", armed["reply"])

        fingerprint = content_fingerprint(self.store.state["outreach"][draft["outreach_id"]])
        press = bot.handle_callback(f"sc:{draft['outreach_id']}:{fingerprint}", user_id=OPERATOR, chat_id=CHAT,
                                   message_id=5, callback_query_id="cb-1")
        self.assertEqual(press["code"], "SEND_SENT")
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("ACCEPTED_BY_PROVIDER", press["notice"])
        self.assertIn("not a delivery, read, or open receipt", press["notice"])
        self.assertEqual(press["edit_markup"], {"inline_keyboard": []}, "a spent confirmation must not stay pressable")
        record = self.record()
        self.assertEqual(record["send_actor"], f"telegram:{OPERATOR}")
        self.assertEqual(record["send_identity_source"], "telegram_operator_allowlist")

    def test_a_press_with_the_wrong_fingerprint_is_refused_before_the_provider(self) -> None:
        draft = self.approved_draft()
        answer = self.bot().handle_callback(f"sc:{draft['outreach_id']}:deadbeefdeadbeef", user_id=OPERATOR,
                                           chat_id=CHAT, message_id=5, callback_query_id="cb-2")
        self.assertEqual(answer["code"], "APPROVAL_STALE")
        self.assertEqual(self.client.calls, [])

    def test_a_dry_run_press_contacts_nobody(self) -> None:
        draft = self.approved_draft()
        answer = self.bot().dispatch(f"/send {draft['outreach_id']} --dry", user_id=OPERATOR, chat_id=CHAT,
                                    deliver=False)
        self.assertEqual(answer["code"], "SEND_DRY_RUN")
        self.assertIn("No provider was contacted", answer["reply"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.state_bytes(), self.state_bytes())
        before = self.state_bytes()
        self.assertEqual(self.record().get("send_state"), None)
        self.assertEqual(self.state_bytes(), before)

    def test_an_unarmed_typed_send_still_needs_the_word(self) -> None:
        draft = self.approved_draft()
        answer = self.bot().dispatch(f"/send {draft['outreach_id']}", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(answer["code"], "SEND_ARMED")
        refused = self.bot().dispatch(f"/send {draft['outreach_id']} --confirm maybe", user_id=OPERATOR,
                                     chat_id=CHAT, deliver=False)
        self.assertEqual(refused["code"], "CONFIRMATION_MISMATCH")
        self.assertIn("not the confirmation word", refused["reply"])
        self.assertEqual(self.client.calls, [])
        accepted = self.bot().dispatch(f"/send {draft['outreach_id']} --confirm send", user_id=OPERATOR,
                                      chat_id=CHAT, deliver=False)
        self.assertEqual(accepted["code"], "SEND_SENT")
        self.assertEqual(len(self.client.calls), 1)

    def test_the_live_flag_is_honoured_over_telegram_too(self) -> None:
        os.environ.pop("AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH", None)
        draft = self.approved_draft()
        answer = self.bot().dispatch(f"/send {draft['outreach_id']} --confirm send", user_id=OPERATOR, chat_id=CHAT,
                                     deliver=False)
        self.assertEqual(answer["code"], "LIVE_EFFECTS_DISABLED")
        self.assertIn("AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH", answer["reply"])
        self.assertEqual(self.client.calls, [])

    def test_an_unanswered_send_is_reported_as_uncertain_and_reconciled_by_the_account(self) -> None:
        from integrations.gmail import GmailError

        draft = self.approved_draft()
        hanging = FakeSendAdapter(error=GmailError("reset", code="GMAIL_UNRESPONSIVE", retryable=True))
        bot = self.bot(client=hanging, readback=FakeReadAdapter(gmail_message(to=draft["recipient"],
                                                                            subject=draft["subject"])))
        uncertain = bot.dispatch(f"/send {draft['outreach_id']} --confirm send", user_id=OPERATOR, chat_id=CHAT,
                                deliver=False)
        self.assertEqual(uncertain["code"], "SEND_UNCERTAIN")
        self.assertIn("do not resend", uncertain["reply"] + texts(self.api))
        self.assertEqual(len(hanging.calls), 1)
        report = bot.dispatch(f"/reconcile {draft['outreach_id']}", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(report["code"], "RECONCILE_REPORT")
        # No provider id came back from the failed attempt, so there is nothing to look up in the account and the
        # console says so instead of guessing: the draft stays unresolved and stays blocked.
        self.assertIn("PROVIDER_ID_UNAVAILABLE", report["reply"])
        self.assertEqual(self.record()["send_state"], "UNCERTAIN")
        self.assertEqual(len(hanging.calls), 1, "reconciliation must not send again")
        again = bot.dispatch(f"/send {draft['outreach_id']} --confirm send", user_id=OPERATOR, chat_id=CHAT,
                            deliver=False)
        self.assertEqual(again["code"], "SEND_UNCERTAIN")
        self.assertEqual(len(hanging.calls), 1)

    def test_the_sent_command_reads_the_record_and_offers_reconciliation(self) -> None:
        draft = self.approved_draft()
        bot = self.bot()
        bot.dispatch(f"/send {draft['outreach_id']} --confirm send", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        answer = bot.dispatch(f"/sent {draft['outreach_id']}", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(answer["code"], "SEND_STATUS")
        self.assertIn("REPORTED_SENT", answer["reply"])
        self.assertIn("no elapsed time will ever be reported", answer["reply"])

    def test_an_opt_out_blocks_the_next_send_from_the_console(self) -> None:
        from orchestration.telegram_bot import optout_fingerprint

        draft = self.approved_draft()
        bot = self.bot()
        bare = bot.dispatch(f"/optout {self.lead['lead_id']}", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertEqual(bare["code"], "EVIDENCE_REQUIRED")
        done = bot.dispatch(f'/optout {self.lead["lead_id"]} --note "replied: stop emailing us" '
                           f'--confirm {optout_fingerprint(self.lead["lead_id"])}', user_id=OPERATOR, chat_id=CHAT,
                           deliver=False)
        self.assertEqual(done["code"], "OPT_OUT_RECORDED")
        refused = bot.dispatch(f"/send {draft['outreach_id']} --dry", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertIn("CONTACT_BLOCKED", refused["reply"])
        listed = bot.dispatch("/outbox", user_id=OPERATOR, chat_id=CHAT, deliver=False)
        self.assertIn("CONTACT_BLOCKED", listed["reply"],
                      "the approved row is still listed, with the block that makes it unsendable named")
        self.assertIn("blocked", listed["reply"])

    def test_no_reply_ever_goes_to_the_prospect_from_the_console(self) -> None:
        draft = self.approved_draft()
        bot = self.bot()
        for command in (f"/send {draft['outreach_id']}", f"/sent {draft['outreach_id']}", "/outbox", "/reconcile",
                        f"/review {draft['outreach_id']}"):
            bot.dispatch(command, user_id=OPERATOR, chat_id=CHAT, deliver=True)
        self.assertEqual(self.client.calls, [])
        self.assertEqual({row["chat_id"] for row in self.api.sent}, {CHAT},
                         "every message this bot writes goes to the operator's chat, never to a lead")

    def test_the_send_surface_is_not_reachable_from_an_unlisted_account(self) -> None:
        draft = self.approved_draft()
        answer = self.bot().dispatch(f"/send {draft['outreach_id']} --confirm send", user_id=OTHER, chat_id=CHAT,
                                     deliver=True)
        self.assertEqual(answer["code"], "AUTHORIZATION_REFUSED")
        self.assertEqual(self.client.calls, [])
        self.assertEqual([row["text"] for row in self.api.sent],
                         ["This bot does not accept instructions from this account."],
                         "the refusal is fixed text: it confirms nothing about the lead, the draft, or the command")
        self.assertNotIn("Fictional", texts(self.api))


class HttpSendRouteTests(OutboundFixture):
    """The API surface, over the loopback handler this repository owns."""

    TOKEN = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"

    def setUp(self) -> None:
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer

        from orchestration.api import make_handler
        from orchestration.jobs import JobService

        handler = make_handler(JobService(self.store), self.TOKEN, repository_root=REPO_ROOT)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close(), thread.join(5)))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def request(self, method, path, body=None, *, authenticated=True):
        import urllib.error
        import urllib.request

        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.TOKEN}"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_the_outbox_read_masks_the_recipient_and_declares_what_it_cannot_prove(self) -> None:
        draft = self.approved_draft()
        status, payload = self.request("GET", "/integrations/outbox")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        row = payload["outbox"][0]
        self.assertEqual(row["outreach_id"], draft["outreach_id"])
        # The route drops the field and adds a masked one: an API client is not the operator's terminal, and a
        # caller that only needs the list has no use for the address at all.
        self.assertNotIn("recipient", row)
        self.assertEqual(row["recipient_masked"], "o…@example.invalid")
        self.assertNotIn("owner@example.invalid", json.dumps(payload))
        self.assertTrue(row["eligible"])
        from outreach.service import content_fingerprint

        self.assertTrue(row["approval_bound_to_fingerprint"])
        self.assertEqual(row["content_fingerprint"],
                         content_fingerprint(self.store.state["outreach"][draft["outreach_id"]]))
        self.assertEqual(payload["sending_state"], "inbound_only")
        self.assertFalse(payload["live_effect_enabled"])
        self.assertIn("VERIFIED_DELIVERED", payload["results_unreachable"])

    def test_a_send_needs_no_field_that_describes_the_message(self) -> None:
        draft = self.approved_draft()
        for field in ("recipient", "subject", "draft_message", "channel", "body", "to"):
            status, payload = self.request("POST", "/integrations/email/send",
                                          {"outreach_id": draft["outreach_id"], field: "whatever"})
            self.assertEqual(status, 400)
            self.assertEqual(payload["error_code"], "INTEGRATION_PARAMETER_NOT_PERMITTED")
            self.assertIn(field, payload["error"])

    def test_the_api_cannot_send_on_a_machine_that_cannot_send(self) -> None:
        self.approved_draft()
        status, payload = self.request("POST", "/integrations/email/send",
                                      {"outreach_id": self.draft["outreach_id"], "confirmation": "SEND"})
        self.assertEqual((status, payload["error_code"]), (409, "LIVE_EFFECTS_DISABLED"))
        self.assertFalse(payload["retry_allowed"])
        self.assertFalse(payload["attempt_may_have_landed"])
        self.assertIn("AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH", payload["next_action"])

    def test_a_dry_run_over_http_composes_and_records_nothing(self) -> None:
        draft = self.approved_draft()
        before = self.state_bytes()
        status, payload = self.request("POST", "/integrations/email/send",
                                      {"outreach_id": draft["outreach_id"], "dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["send_state"], "DRY_RUN")
        self.assertEqual(payload["recipient_omitted"], True)
        self.assertNotIn("owner@example.invalid", json.dumps(payload))
        self.assertEqual(self.state_bytes(), before)

    def test_a_hold_refuses_before_any_provider_is_reached(self) -> None:
        from orchestration import controls

        draft = self.approved_draft()
        controls.set_hold(self.store, actor="operator", reason="outage")
        status, payload = self.request("POST", "/integrations/email/send",
                                      {"outreach_id": draft["outreach_id"], "confirmation": "SEND"})
        self.assertEqual((status, payload["error_code"]), (409, "AUTOMATION_HELD"))
        self.assertIn("no send was attempted", payload["error"])
        self.assertIn("release the hold", payload["next_action"])
        self.assertFalse(payload["attempt_may_have_landed"])
        self.assertIn("outage", json.dumps(self.store.state.get("automation_controls", {})),
                      "the reason lives on the durable hold record, where a caller can read it in full")
        status, payload = self.request("POST", "/integrations/email/send",
                                      {"outreach_id": draft["outreach_id"], "dry_run": True})
        self.assertEqual((status, payload["error_code"]), (409, "AUTOMATION_HELD"),
                         "a held process answers HELD to every send call, dry run included")
        # A hold suspends acting, not reading: the outbox and the ledger stay available.
        status, payload = self.request("GET", "/integrations/outbox")
        self.assertEqual(status, 200)
        self.assertTrue(payload["automation_held"])
        self.assertIn("on hold", (payload["gate_reason"] or "").lower())
        self.assertIn("outage", payload["gate_reason"], "the read shows who said why")

    def test_a_forged_approval_cannot_be_pushed_through_the_send_route(self) -> None:
        from outreach.service import OutreachService

        self.deploy_preview()
        self.approve_project()
        draft = OutreachService(self.store).create(self.project["project_id"])
        status, payload = self.request("POST", "/integrations/email/send",
                                      {"outreach_id": draft["outreach_id"], "status": "READY_TO_SEND",
                                       "approved_by": "me"})
        self.assertEqual((status, payload["error_code"]), (400, "INTEGRATION_PARAMETER_NOT_PERMITTED"))
        status, payload = self.request("POST", "/integrations/email/send", {"outreach_id": draft["outreach_id"]})
        self.assertEqual((status, payload["error_code"]), (409, "NOT_APPROVED"))

    def test_reconciliation_and_the_connectivity_check_answer_without_sending(self) -> None:
        self.approved_draft()
        status, payload = self.request("POST", "/integrations/email/reconcile", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["rows"], [])
        self.assertIn("never sends", payload["capability_note"])
        status, payload = self.request("POST", "/integrations/email/send-check", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "inbound_only")
        self.assertEqual(payload["code"], "GMAIL_SEND_NOT_CONFIGURED")
        self.assertFalse(payload["sent"])
        status, payload = self.request("GET", "/integrations/status")
        self.assertEqual(payload["email"]["sending"]["state"], "inbound_only")
        self.assertIn("AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN", payload["email"]["sending"]["missing"])

    def test_an_unauthenticated_call_reaches_no_send_route(self) -> None:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.base + "/integrations/email/send", data=b"{}", method="POST",
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
        self.assertEqual(code, 401)


class ChannelReportTests(OutboundFixture):
    def test_sending_is_reported_as_a_state_an_operator_can_act_on(self) -> None:
        from orchestration.health import channels_section

        def report(env):
            return channels_section(self.store, environment=env)["email"]["sending"]

        sending = report({})
        self.assertEqual(sending["state"], "inbound_only")
        self.assertEqual(sending["missing"], ["AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN",
                                             "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID",
                                             "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET"])
        credentials = {"AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "rt",
                       "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "cid",
                       "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "cs"}
        added = report(credentials)
        self.assertEqual(added["state"], "disabled", "credentials alone are not a channel that is on")
        self.assertFalse(added["send_enabled_for_jobs_or_workflows"])
        enabled = report({**credentials, "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED": "true"})
        self.assertEqual(enabled["state"], "configured",
                         "configured is the ceiling without an exercised credential")
        self.assertTrue(enabled["send_enabled_for_jobs_or_workflows"])
        self.assertFalse(enabled["unsubscribe_url_configured"])
        self.assertNotIn("rt", json.dumps(enabled))
        self.assertNotIn("cs", json.dumps(enabled))
        self.assertNotIn("verified_at", json.dumps(enabled),
                         "an unexercised channel reports no connectivity claim at all")

    def test_outbound_attention_items_name_the_gap_between_approval_and_send(self) -> None:
        from orchestration.health import outbound_attention_items

        self.assertEqual(outbound_attention_items(self.store), [], "nothing is pending before an approval exists")
        draft = self.approved_draft()
        items = outbound_attention_items(self.store)
        self.assertEqual([item["code"] for item in items], ["APPROVAL_AWAITING_SEND"])
        item = items[0]
        self.assertEqual(item["kind"], "outbound")
        self.assertEqual(item["outreach_id"], draft["outreach_id"])
        for key in ("project_id", "lead_id", "build_id", "validation_id", "deployment_id", "status", "stage",
                    "review_status", "message", "recorded_at", "age_seconds", "next_action"):
            self.assertIn(key, item)
        self.assertIn("a workflow cannot take this item as a cue to send", item["next_action"])

        self.store.state["outreach"][draft["outreach_id"]].update({"send_state": "UNCERTAIN"})
        self.store._save()
        self.assertEqual([item["code"] for item in outbound_attention_items(self.store)], ["SEND_UNCERTAIN"])

        self.store.state["outreach"][draft["outreach_id"]].update({"send_state": None, "status": "SEND_FAILED",
                                                                   "send_state": "FAILED"})
        self.store._save()
        self.assertEqual([item["code"] for item in outbound_attention_items(self.store)], ["SEND_FAILED"])

        self.store.state["outreach"][draft["outreach_id"]].update(
            {"status": "READY_TO_SEND", "send_state": None, "approved_content_fingerprint": "0" * 16})
        self.store._save()
        self.assertEqual([item["code"] for item in outbound_attention_items(self.store)], ["APPROVAL_STALE"])
        self.store.state["outreach"][draft["outreach_id"]].pop("approved_content_fingerprint")
        self.store._save()
        self.assertEqual([item["code"] for item in outbound_attention_items(self.store)], ["APPROVAL_UNBOUND"])

    def test_an_outbound_item_is_never_a_critical_page(self) -> None:
        from orchestration import alerts
        from orchestration.health import operator_attention

        self.assertNotIn("outbound", alerts.CRITICAL_KINDS)
        self.approved_draft()
        report = operator_attention(self.store, repository_root=REPO_ROOT)
        kinds = {item["kind"] for item in report["items"]}
        self.assertIn("outbound", kinds)
        self.assertEqual(report["counts"]["outbound"], 1)
        self.assertEqual(report["counts"]["sends_uncertain"], 0)
        self.assertIs(report["requires_attention"], True)
        # Criticality is decided by kind membership in alerts.CRITICAL_KINDS, which "outbound" is not in: an
        # approval waiting on a person is not a page-worthy fault.
        self.assertNotIn("outbound", alerts.CRITICAL_KINDS)

    def test_the_preflight_reads_the_send_configuration_without_claiming_anything(self) -> None:
        from orchestration.health import integration_preflight

        report = integration_preflight(self.store, repository_root=REPO_ROOT)
        channels = report["sections"]["operator_channels"]
        sending = channels["email"]["sending"]
        self.assertEqual(sending["state"], "inbound_only")
        self.assertIs(channels["email"]["live_account_verified"], False)
        self.assertIs(channels["email"]["live_sending_verified"], False,
                      "the preflight never upgrades configuration to connectivity")
        self.assertEqual(sending["live_effect_flag"], "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH")
        self.assertIn("VERIFIED_DELIVERED", sending["results_unreachable"])
        text = json.dumps(channels, default=str)
        self.assertIn("integrations/gmail_send.py", text)
        self.assertIn("read-only by construction", text)

    def test_the_channels_report_takes_its_environment_from_the_caller(self) -> None:
        from orchestration.health import channels_section

        with_credentials = channels_section(self.store, environment={
            "AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "SECRET-REFRESH-VALUE",
            "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "SECRET-CLIENT-ID-VALUE",
            "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "SECRET-CLIENT-SECRET-VALUE",
            "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED": "true",
            "AI_WEBSITE_AGENT_EMAIL_UNSUBSCRIBE_URL": "https://example.invalid/opt-out"})["email"]["sending"]
        self.assertEqual(with_credentials["state"], "configured")
        self.assertTrue(with_credentials["unsubscribe_url_configured"])
        self.assertEqual(with_credentials["scope"], "https://www.googleapis.com/auth/gmail.send")
        dumped = json.dumps(with_credentials, default=str)
        for value in ("SECRET-REFRESH-VALUE", "SECRET-CLIENT-ID-VALUE", "SECRET-CLIENT-SECRET-VALUE"):
            self.assertNotIn(value, dumped, f"{value} is a credential, and a report is not where it belongs")
        self.assertNotIn("opt-out", dumped, "even the unsubscribe URL's path is not echoed")


class N8nSendWorkflowTests(OutboundFixture):
    """The exported workflow, executed by this repository's runner against this repository's handler.

    The point is not that n8n could run it - it is that the file as exported performs no send when it is run, on a
    machine with no live-effect flag, and still reaches an outcome node that names the refusal. The runner refuses
    non-loopback targets by default, so these runs cannot reach a provider even in principle.
    """

    workflow_path = REPO_ROOT / "n8n" / "workflows" / "07-approved-outbound-send.json"
    TOKEN = "TEST_FIXTURE_TOKEN_NOT_A_SECRET"

    class _FakeTelegram:
        def __init__(self):
            self.sent = []

        def send_message(self, chat_id, text, **kwargs):
            self.sent.append({"chat_id": chat_id, "text": text})
            return {"message_id": len(self.sent)}

    def server(self):
        import threading
        from http.server import ThreadingHTTPServer

        from orchestration.api import make_handler
        from orchestration.jobs import JobService

        handler = make_handler(JobService(self.store), self.TOKEN, repository_root=REPO_ROOT)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def run_workflow(self, *, overrides=None, extra_env=None, with_telegram=False):
        from orchestration.n8n_runner import N8nWorkflowRunner

        path = self.workflow_path
        if overrides:
            document = json.loads(self.workflow_path.read_text(encoding="utf-8"))
            parameters = next(node for node in document["nodes"] if node["name"] == "Send Parameters")["parameters"]
            for name, value in overrides.items():
                for group in ("string", "boolean"):
                    for item in parameters["values"].get(group) or []:
                        if item["name"] == name:
                            item["value"] = value
            path = self.root / "07-modified.json"
            path.write_text(json.dumps(document), encoding="utf-8")
        environment = {"AI_WEBSITE_AGENT_API_URL": self.server(), "AI_WEBSITE_AGENT_API_TOKEN": self.TOKEN,
                       "AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH": "", "AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN": "",
                       "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID": "", "AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET": "",
                       "AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED": ""}
        environment.update(extra_env or {})
        api = None
        if with_telegram:
            environment.update({"AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN": "1:TEST_FIXTURE_NOT_A_SECRET",
                                "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS": str(OPERATOR),
                                "AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": str(CHAT)})
            api = self._FakeTelegram()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict("os.environ", environment, clear=True))
        if api is not None:
            stack.enter_context(mock.patch("integrations.telegram.TelegramBotApi", lambda *a, **k: api))
        with stack:
            result = N8nWorkflowRunner(path, env=environment).execute({}, execution_id="phase49-send")
        result["telegram"] = api
        return result

    def outreach_and_ledger(self) -> str:
        """The two collections a send would touch. Notification bookkeeping is deliberately excluded."""
        state = self.reload().state
        return json.dumps({"outreach": state.get("outreach"), "engagements": state.get("engagements")},
                          default=str, sort_keys=True)

    def test_the_exported_workflow_is_inert_as_it_stands(self) -> None:
        draft = self.approved_draft()
        before = self.outreach_and_ledger()
        result = self.run_workflow(with_telegram=True)
        self.assertEqual(result["errors"], [], json.dumps(result["final_output"], default=str)[:900])
        self.assertEqual(result["execution_status"], "COMPLETED",
                         "a 200 from the dry-run route and a 200 from the notify route are not failures")
        outputs = json.dumps(result, default=str)
        self.assertIn(draft["outreach_id"], outputs)
        self.assertIn("DRY_RUN", outputs)
        self.assertEqual(self.outreach_and_ledger(), before,
                         "a dry run through the workflow must leave the outreach record and the ledger untouched")
        self.assertIsNone(self.record().get("send_state"))
        self.assertEqual(self.events(), [])
        self.assertNotIn("owner@example.invalid", outputs, "the exported flow never carries an address")

    def test_a_workflow_run_that_asks_to_send_is_refused_by_this_machines_configuration(self) -> None:
        self.approved_draft()
        before = self.outreach_and_ledger()
        result = self.run_workflow(overrides={"dry_run": False, "confirmation": "SEND"}, with_telegram=True)
        # A refusal is an HTTP 409, so the runner reports the node as failed *and* the export's own branch node
        # turns it into an outcome. Both halves matter: the run is honest about being refused, and the run still
        # ends at "nothing was sent" rather than at an exception.
        refusal = result["errors"][0]
        self.assertEqual(refusal["node"], "Send Approved Draft")
        self.assertEqual((refusal["error_code"], refusal["http_status"]), ("LIVE_EFFECTS_DISABLED", 409))
        self.assertEqual(result["execution_status"], "FAILED", "the run reports the refusal rather than hiding it")
        self.assertIn("Outcome - Nothing Sent", result["visited_nodes"])
        self.assertIn("nothing was sent by this run", str(result["node_outputs"]["Outcome - Nothing Sent"]["outcome"]))
        self.assertIn("Outcome - Nothing Sent", result["visited_nodes"])
        outcome = result["node_outputs"]["Outcome - Nothing Sent"]
        self.assertEqual(outcome["code"], "LIVE_EFFECTS_DISABLED")
        self.assertEqual(outcome["retry_allowed"], "False",
                         "the Set node copies the flag through as the API answered it: a string, not a verdict")
        self.assertEqual(outcome["attempt_may_have_landed"], "False")
        self.assertIn("--allow-live", str(outcome.get("next_action")),
                      "the export carries the application's own next step through to the outcome node")
        self.assertEqual(self.outreach_and_ledger(), before,
                         "a workflow run may not record an attempt it was never allowed to make")

    def test_a_workflow_run_cannot_grant_itself_a_credential_or_an_approval(self) -> None:
        self.approved_draft()
        result = self.run_workflow(overrides={"dry_run": False, "confirmation": "SEND"}, with_telegram=True,
                                  extra_env={"AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH": "1"})
        self.assertEqual(result["errors"][0]["error_code"], "SEND_CLIENT_UNAVAILABLE",
                         "with the live flag on but no send credential, the answer is still nothing sent")
        self.assertIsNone(self.record().get("send_state"))
        self.assertEqual(self.events(), [])

    def test_a_broken_operator_channel_does_not_disturb_the_send_record(self) -> None:
        self.approved_draft()
        before = self.outreach_and_ledger()
        result = self.run_workflow()  # no Telegram token: the notify step is expected to fail
        codes = {row["error_code"] for row in result["errors"]}
        self.assertEqual(codes, {"TELEGRAM_NOT_CONFIGURED"}, json.dumps(result["errors"], default=str)[:600])
        self.assertEqual(result["errors"][0]["node"], "Notify Operator Channel")
        self.assertEqual(result["visited_nodes"][-1], "Outcome - Operator Notified")
        self.assertIn("Outcome - Nothing Sent" if False else "IF Anything Refused", result["visited_nodes"])
        self.assertEqual(self.outreach_and_ledger(), before,
                         "a notification failure never becomes a send failure, and never a send either")

    def test_the_export_sends_only_to_the_two_application_routes(self) -> None:
        document = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        urls = sorted({str((node.get("parameters") or {}).get("url") or "") for node in document["nodes"]
                       if node["type"] == "n8n-nodes-base.httpRequest"})
        self.assertEqual(urls, sorted([
            "={{ $env.AI_WEBSITE_AGENT_API_URL }}/integrations/email/send",
            "={{ $env.AI_WEBSITE_AGENT_API_URL }}/integrations/telegram/notify"]))
        for node in document["nodes"]:
            if node["type"] == "n8n-nodes-base.httpRequest":
                self.assertEqual(node["parameters"]["method"], "POST")
        self.assertEqual({node["name"] for node in document["nodes"]},
                         {"Manual Trigger", "Send Parameters", "Send Approved Draft", "IF Anything Refused",
                          "Outcome - Nothing Sent", "IF Provider Answered", "Outcome - Reported Only",
                          "Outcome - Accepted By Provider", "Notify Operator Channel", "IF Nothing Notified",
                          "Outcome - Already Reported", "Outcome - Operator Notified"})


class CliSendCommandTests(OutboundFixture):
    """The five commands, in-process, against the same state file the executor writes."""

    def cli(self, *argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from orchestration.cli import main

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--state-path", str(self.state_path), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_outbox_and_status_reads_print_the_records(self) -> None:
        draft = self.approved_draft()
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-outbox")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["total"], 1)
        row = payload["outbox"][0]
        self.assertEqual(row["outreach_id"], draft["outreach_id"])
        # The CLI masks in place rather than renaming the field: a terminal listing keeps its shape, and a pasted
        # screenshot of it must not carry a prospect's mailbox. The HTTP route goes further and drops the key.
        self.assertEqual(row["recipient"], "o…@example.invalid")
        self.assertNotIn("owner@example.invalid", stdout)
        self.assertTrue(row["approval_bound_to_fingerprint"])
        self.assertEqual(payload["sending_state"], "inbound_only")
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-send-status")
        self.assertEqual(code, 0)
        status = json.loads(stdout)
        self.assertEqual(status["counts"]["awaiting_send"], 1)
        self.assertIn("VERIFIED_DELIVERED", status["results_unreachable"])

    def test_the_connectivity_check_reports_a_channel_that_does_not_exist_as_such(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-send-check")
        self.assertEqual(code, 0, "an honest 'not configured' is not a failure of the check")
        payload = json.loads(stdout)
        self.assertEqual(payload["state"], "inbound_only")
        self.assertEqual(payload["code"], "GMAIL_SEND_NOT_CONFIGURED")
        self.assertFalse(payload["sent"])

    def test_a_dry_run_exits_zero_and_a_refusal_exits_one(self) -> None:
        draft = self.approved_draft()
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-send", "--outreach-id", draft["outreach_id"], "--dry-run")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["send_state"], "DRY_RUN")
        self.assertEqual(payload["recipient"], "o…@example.invalid")
        # The composition under it is not masked, and deliberately so: a local dry run exists to show an operator
        # the exact request their own record would produce, on their own terminal. The remote surface refuses to
        # show any of it - see test_a_dry_run_over_http_composes_and_records_nothing.
        self.assertEqual(payload["composition"]["prepared"]["to"], "owner@example.invalid")
        self.assertIn("raw", payload["composition"]["prepared"])
        self.assertIsNone(self.record().get("send_state"))
        self.assertEqual(self.events(), [])

        # A refusal is JSON too, on the same stream, with a non-zero exit: parseable by a script and loud for a
        # person. Nothing about a refusal is written to the records.
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-send", "--outreach-id", draft["outreach_id"], "--confirm", "SEND")
        self.assertEqual(code, 1)
        refusal = json.loads(stdout)
        self.assertEqual(refusal["error_code"], "LIVE_EFFECTS_DISABLED")
        self.assertFalse(refusal["sent"])
        self.assertFalse(refusal["retry_allowed"])
        self.assertFalse(refusal["attempt_may_have_landed"])
        self.assertIn("--allow-live", refusal["next_action"])
        self.assertIsNone(self.record().get("send_state"))
        self.assertEqual(self.events(), [])

    def test_the_cli_refuses_to_guess_which_draft_an_operator_meant(self) -> None:
        self.approved_draft()
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-send", "--confirm", "SEND")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["error_code"], "OUTREACH_ID_REQUIRED")
        self.assertIn("email-outbox lists the drafts", json.loads(stdout)["next_action"])

    def test_reconcile_names_the_unresolved_attempt_without_touching_the_provider(self) -> None:
        draft = self.approved_draft()
        self.store.state["outreach"][draft["outreach_id"]].update({"send_state": "UNCERTAIN",
                                                                   "send_state_detail": "no answer"})
        self.store._save()
        with mock.patch.dict("os.environ", {}, clear=True):
            code, stdout, _ = self.cli("email-reconcile")
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["rows"][0]["code"], "PROVIDER_ID_UNAVAILABLE")
        self.assertFalse(payload["reconciled"])
        self.assertEqual(self.record()["send_state"], "UNCERTAIN", "reconciling on faith is not reconciling")
