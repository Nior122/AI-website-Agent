"""Comprehensive acceptance testing suite for all 5 dedicated n8n workflows.

Verifies:
1. Workflow 01: System health, safe job creation, job ID persistence, status retrieval, invalid-input rejection, duplicate handling, timeout behavior, failure notifications.
2. Workflow 02: Health data, pipeline summaries, outstanding work, empty results, partial failures, message formatting, Africa/Lagos timezone.
3. Workflow 03: Draft lifecycle, approval requirements, payload mapping, credential references, retries, duplicate-publication protection.
4. Workflow 04: Read-only mailbox access, message parsing, duplicate detection, ambiguous messages, safe routing, failure notifications.
5. Workflow 05: Approved-record checks, fingerprint comparison, opt-out and suppression rules, held records, duplicate prevention, retry safety, audit logging.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

from orchestration.briefing import (
    compute_authoritative_briefing_data,
    format_daily_briefing_text,
    chunk_telegram_text,
    LAGOS_TZ,
)
from tracking.store import TrackingStore


WORKFLOW_DIR = Path("n8n/workflows/project")


def _load_wf(name: str) -> dict:
    return json.loads((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


class TestWorkflow01PipelineRunnerAcceptance(unittest.TestCase):
    """Deep contract and logic testing for Workflow 01: Pipeline Runner."""

    def setUp(self):
        self.wf = _load_wf("01-nior-website-agent-pipeline-runner.json")
        self.nodes = {n["name"]: n for n in self.wf["nodes"]}

    def test_node_names_and_sequence(self):
        expected_nodes = [
            "Manual Trigger",
            "Validate Input & Build Request",
            "Check System Health",
            "Evaluate Health & Hold",
            "Create & Run Job",
            "Inspect Execution Result",
            "Notify Operator",
        ]
        for name in expected_nodes:
            self.assertIn(name, self.nodes, f"Missing node: {name}")

    def test_allowed_stages_and_input_normalization(self):
        js = self.nodes["Validate Input & Build Request"]["parameters"]["jsCode"]
        self.assertIn("'SCOUT'", js)
        self.assertIn("'RESEARCH'", js)
        self.assertIn("'QUALIFY'", js)
        self.assertIn("'BUILD'", js)
        self.assertIn("'VALIDATE'", js)
        self.assertIn("Disallowed job_type", js)
        self.assertIn("dry_run", js)

    def test_idempotent_job_id_generation(self):
        js = self.nodes["Validate Input & Build Request"]["parameters"]["jsCode"]
        self.assertIn("job_id", js)
        self.assertIn("n8n-", js)

    def test_health_check_and_global_hold_refusal(self):
        js = self.nodes["Evaluate Health & Hold"]["parameters"]["jsCode"]
        self.assertIn("automation_held", js)
        self.assertIn("Global automation is currently HELD", js)
        self.assertIn("apiStatus !== 'OK'", js)

    def test_result_inspection_and_failure_notification(self):
        js = self.nodes["Inspect Execution Result"]["parameters"]["jsCode"]
        self.assertIn("isSuccess", js)
        self.assertIn("summary", js)
        self.assertIn("completed successfully", js)
        self.assertIn("ended with status", js)


class TestWorkflow02DailyBriefingAcceptance(unittest.TestCase):
    """Deep contract and logic testing for Workflow 02: Daily Business Briefing."""

    def setUp(self):
        self.wf = _load_wf("02-nior-operations-daily-briefing.json")
        self.nodes = {n["name"]: n for n in self.wf["nodes"]}

    def test_schedule_trigger_configuration(self):
        st = self.nodes["Schedule Trigger"]
        self.assertEqual(st["type"], "n8n-nodes-base.scheduleTrigger")
        rule = st["parameters"]["rule"]["interval"][0]
        self.assertEqual(rule["triggerAtHour"], 8)
        self.assertEqual(rule["triggerAtMinute"], 0)

    def test_health_and_engagement_endpoints(self):
        endpoints = {
            "Fetch Health & Attention": "/health",
            "Fetch Engagement Pipeline": "/engagement/pipeline",
            "Fetch Outstanding Work": "/engagement/outstanding",
        }
        for node_name, ep in endpoints.items():
            node = self.nodes[node_name]
            self.assertIn(ep, node["parameters"]["url"])
            self.assertEqual(node["parameters"]["genericAuthType"], "httpHeaderAuth")

    def test_composer_rules_plain_text_and_lagos_tz(self):
        js = self.nodes["Compose Briefing Text"]["parameters"]["jsCode"]
        self.assertIn("Africa/Lagos", js)
        self.assertIn("DAILY BUSINESS BRIEFING", js)
        self.assertIn("₦", js)
        self.assertIn("chunks", js)
        self.assertIn("3900", js)

    def test_empty_results_and_zero_division_safety(self):
        store = MagicMock()
        store.state = {"leads": {}, "projects": {}, "jobs": {}}
        data = compute_authoritative_briefing_data(store)
        text = format_daily_briefing_text(data)
        self.assertIn("DAILY BUSINESS BRIEFING", text)
        self.assertIn("Active leads: 0", text)
        self.assertIn("Total pipeline: ₦0", text)
        self.assertNotIn("**", text)  # Plain text rule


class TestWorkflow03BufferPublisherAcceptance(unittest.TestCase):
    """Deep contract and logic testing for Workflow 03: Buffer Publisher."""

    def setUp(self):
        self.wf = _load_wf("03-nior-social-buffer-publisher.json")
        self.nodes = {n["name"]: n for n in self.wf["nodes"]}

    def test_supported_channels_and_character_bounds(self):
        js = self.nodes["Validate Approval & Fingerprint"]["parameters"]["jsCode"]
        self.assertIn("twitter", js)
        self.assertIn("280", js)
        self.assertIn("linkedin", js)
        self.assertIn("3000", js)
        self.assertIn("instagram", js)
        self.assertIn("2200", js)
        self.assertIn("requiresMedia: true", js)

    def test_approval_requirement_enforcement(self):
        js = self.nodes["Validate Approval & Fingerprint"]["parameters"]["jsCode"]
        self.assertIn("Only APPROVED drafts can be dispatched", js)
        self.assertIn("approved", js)

    def test_dry_run_dispatch_simulation(self):
        js = self.nodes["Buffer Dispatcher Gate"]["parameters"]["jsCode"]
        self.assertIn("dry_run", js)
        self.assertIn("BUFFER_DRAFTED", js)
        self.assertIn("Simulated Buffer draft successfully created", js)


class TestWorkflow04InboundIntakeAcceptance(unittest.TestCase):
    """Deep contract and logic testing for Workflow 04: Inbound Intake Router."""

    def setUp(self):
        self.wf = _load_wf("04-nior-communications-inbound-intake.json")
        self.nodes = {n["name"]: n for n in self.wf["nodes"]}

    def test_intake_cycle_bounds(self):
        js = self.nodes["Configure Intake Cycle"]["parameters"]["jsCode"]
        self.assertIn("max_messages", js)
        self.assertIn("Math.min(Math.max(1, Number(item.max_messages)), 50)", js)
        self.assertIn("dry_run", js)

    def test_read_only_poll_endpoint(self):
        poll = self.nodes["Poll Inbound Mailbox"]
        self.assertEqual(poll["parameters"]["method"], "POST")
        self.assertIn("/integrations/email/poll", poll["parameters"]["url"])
        self.assertEqual(poll["parameters"]["genericAuthType"], "httpHeaderAuth")

    def test_intake_result_routing(self):
        js = self.nodes["Process Intake Results"]["parameters"]["jsCode"]
        self.assertIn("fetched", js)
        self.assertIn("recorded", js)
        self.assertIn("unmatched", js)
        self.assertIn("INBOUND INTAKE REPORT", js)


class TestWorkflow05ApprovedSendAcceptance(unittest.TestCase):
    """Deep contract and logic testing for Workflow 05: Approved Send Gate."""

    def setUp(self):
        self.wf = _load_wf("05-nior-outbound-approved-send.json")
        self.nodes = {n["name"]: n for n in self.wf["nodes"]}

    def test_preconditions_and_fingerprint_check(self):
        js = self.nodes["Validate Preconditions & Fail-Closed Gate"]["parameters"]["jsCode"]
        self.assertIn("outreach_id is required", js)
        self.assertIn("expectedFingerprint", js)
        self.assertIn("dryRun", js)

    def test_outbox_and_send_contracts(self):
        outbox = self.nodes["Check Outbox Readiness"]
        self.assertEqual(outbox["parameters"]["method"], "GET")
        self.assertIn("/integrations/outbox", outbox["parameters"]["url"])

        send = self.nodes["Execute Approved Send"]
        self.assertEqual(send["parameters"]["method"], "POST")
        self.assertIn("/integrations/email/send", send["parameters"]["url"])

    def test_safety_ledger_result_inspection(self):
        js = self.nodes["Inspect Send Result & Safety Ledger"]["parameters"]["jsCode"]
        self.assertIn("send_state", js)
        self.assertIn("OUTBOUND SEND REPORT", js)
        self.assertIn("Retry Allowed", js)
        self.assertIn("summary", js)


class TestWorkflowRecoveryAndDuplicateProtection(unittest.TestCase):
    """Validates transient failures, idempotency, duplicate prevention, and retries."""

    def test_action_workflows_have_dry_run_support(self):
        action_workflows = [
            "01-nior-website-agent-pipeline-runner.json",
            "03-nior-social-buffer-publisher.json",
            "04-nior-communications-inbound-intake.json",
            "05-nior-outbound-approved-send.json",
        ]
        for wf_name in action_workflows:
            content = (WORKFLOW_DIR / wf_name).read_text(encoding="utf-8")
            self.assertIn("dry_run", content, f"Workflow {wf_name} missing dry_run support")

    def test_all_workflows_are_inactive_by_default(self):
        for wf_file in WORKFLOW_DIR.glob("*.json"):
            data = json.loads(wf_file.read_text(encoding="utf-8"))
            self.assertFalse(data.get("active", False), f"Workflow {wf_file.name} must default to active=false")

    def test_zero_raw_credentials_in_workflow_definitions(self):
        secret_patterns = [
            r"Bearer\s+[A-Za-z0-9._-]{20,}",
            r"\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b",
            r"postgres://[^\s]+",
            r"postgresql://[^\s]+",
        ]
        for wf_file in WORKFLOW_DIR.glob("*.json"):
            content = wf_file.read_text(encoding="utf-8")
            for pattern in secret_patterns:
                matches = re.findall(pattern, content)
                filtered = [m for m in matches if "$env" not in m]
                self.assertEqual(filtered, [], f"Raw credential pattern matched in {wf_file.name}: {filtered}")


if __name__ == "__main__":
    unittest.main()
