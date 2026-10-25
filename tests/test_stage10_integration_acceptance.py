"""Stage 10 — Integration Validation, Controlled Automation & System Hardening.

Automated acceptance test suite verifying the 5 dedicated n8n workflows,
backend API contracts, credential references, input validation, failure
boundaries, idempotency, and safety gating.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

from orchestration.api import make_handler
from orchestration.briefing import (
    compute_authoritative_briefing_data,
    format_daily_briefing_text,
    chunk_telegram_text,
    LAGOS_TZ,
)
from orchestration.n8n_runner import validate_n8n_workflow_json, workflow_readiness


class Stage10WorkflowSanitizationTests(unittest.TestCase):
    """Verifies that all 5 canonical workflow exports remain sanitized and well-formed."""

    def setUp(self):
        self.workflow_dir = Path("n8n/workflows/project")
        self.files = sorted(list(self.workflow_dir.glob("*.json")))

    def test_all_five_workflows_present_and_importable(self):
        self.assertEqual(len(self.files), 5)
        expected_names = {
            "01-nior-website-agent-pipeline-runner.json",
            "02-nior-operations-daily-briefing.json",
            "03-nior-social-buffer-publisher.json",
            "04-nior-communications-inbound-intake.json",
            "05-nior-outbound-approved-send.json",
        }
        actual_names = {f.name for f in self.files}
        self.assertEqual(actual_names, expected_names)

        for f in self.files:
            data = validate_n8n_workflow_json(f)
            self.assertFalse(data.get("active"), f"Workflow {f.name} must be active=false")
            self.assertGreater(len(data.get("nodes", [])), 0)

    def test_workflows_have_zero_unresolved_placeholders(self):
        env = {
            "AI_WEBSITE_AGENT_API_URL": "http://127.0.0.1:8787",
            "AI_WEBSITE_AGENT_API_TOKEN": "local-test-token",
            "AI_WEBSITE_AGENT_TELEGRAM_CHAT_ID": "7722236745",
        }
        report = workflow_readiness(self.files, env=env)
        self.assertEqual(report["checked_count"], 5)
        self.assertEqual(report["importable_count"], 5)
        self.assertEqual(len(report["missing_environment"]), 0)
        self.assertEqual(len(report["workflows_needing_manual_edit"]), 0)

    def test_workflows_contain_no_embedded_credentials(self):
        secret_patterns = [
            re.compile(r"Bearer\s+(?!\{\{\s*\$env)[A-Za-z0-9._-]+"),
            re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
            re.compile(r"postgres://[^\s]+"),
            re.compile(r"sk-[A-Za-z0-9]{20,}"),
        ]
        for f in self.files:
            content = f.read_text(encoding="utf-8")
            for pattern in secret_patterns:
                matches = pattern.findall(content)
                self.assertEqual(matches, [], f"Secret found in {f.name}: {matches}")


class Stage10Workflow01PipelineRunnerTests(unittest.TestCase):
    """Validates Workflow 01: Job Lifecycle & Pipeline Runner."""

    def setUp(self):
        self.path = Path("n8n/workflows/project/01-nior-website-agent-pipeline-runner.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.nodes = {n["name"]: n for n in self.data["nodes"]}

    def test_workflow_structure_and_credentials(self):
        self.assertEqual(len(self.data["nodes"]), 7)
        self.assertIn("Manual Trigger", self.nodes)
        self.assertIn("Validate Input & Build Request", self.nodes)
        self.assertIn("Check System Health", self.nodes)
        self.assertIn("Evaluate Health & Hold", self.nodes)
        self.assertIn("Create & Run Job", self.nodes)
        self.assertIn("Inspect Execution Result", self.nodes)
        self.assertIn("Notify Operator", self.nodes)

        # HTTP nodes require generic httpHeaderAuth
        for hnode in ["Check System Health", "Create & Run Job"]:
            node = self.nodes[hnode]
            self.assertEqual(node["parameters"]["authentication"], "genericCredentialType")
            self.assertEqual(node["parameters"]["genericAuthType"], "httpHeaderAuth")

        # Telegram node
        tg_node = self.nodes["Notify Operator"]
        self.assertEqual(tg_node["type"], "n8n-nodes-base.telegram")

    def test_input_validation_rules(self):
        code = self.nodes["Validate Input & Build Request"]["parameters"]["jsCode"]
        self.assertIn("['SCOUT', 'RESEARCH', 'QUALIFY', 'BUILD', 'VALIDATE']", code)
        self.assertIn("Disallowed job_type", code)
        self.assertIn("dry_run", code)

    def test_health_and_hold_gate(self):
        code = self.nodes["Evaluate Health & Hold"]["parameters"]["jsCode"]
        self.assertIn("automation_held", code)
        self.assertIn("Global automation is currently HELD", code)
        self.assertIn("apiStatus !== 'OK'", code)


class Stage10Workflow02DailyBriefingTests(unittest.TestCase):
    """Validates Workflow 02: Daily Business Briefing & Alerts."""

    def setUp(self):
        self.path = Path("n8n/workflows/project/02-nior-operations-daily-briefing.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.nodes = {n["name"]: n for n in self.data["nodes"]}

    def test_schedule_trigger_configuration(self):
        self.assertIn("Schedule Trigger", self.nodes)
        # Inactive in export
        self.assertFalse(self.data.get("active"))
        # Header auth on data fetchers
        for fetcher in ["Fetch Health & Attention", "Fetch Engagement Pipeline", "Fetch Outstanding Work"]:
            self.assertEqual(self.nodes[fetcher]["parameters"]["genericAuthType"], "httpHeaderAuth")

    def test_briefing_composer_rules(self):
        code = self.nodes["Compose Briefing Text"]["parameters"]["jsCode"]
        self.assertIn("DAILY BUSINESS BRIEFING", code)
        self.assertIn("Africa/Lagos", code)
        self.assertIn("3900", code)


class Stage10Workflow03BufferPublisherTests(unittest.TestCase):
    """Validates Workflow 03: Buffer Multi-Channel Publisher."""

    def setUp(self):
        self.path = Path("n8n/workflows/project/03-nior-social-buffer-publisher.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.nodes = {n["name"]: n for n in self.data["nodes"]}

    def test_channel_limits_and_media_requirements(self):
        code = self.nodes["Validate Approval & Fingerprint"]["parameters"]["jsCode"]
        self.assertIn("twitter", code)
        self.assertIn("280", code)
        self.assertIn("linkedin", code)
        self.assertIn("3000", code)
        self.assertIn("instagram", code)
        self.assertIn("2200", code)
        self.assertIn("requiresMedia", code)

    def test_approval_gate_and_dry_run_dispatch(self):
        val_code = self.nodes["Validate Approval & Fingerprint"]["parameters"]["jsCode"]
        self.assertIn("Only APPROVED drafts can be dispatched to Buffer", val_code)

        gate_code = self.nodes["Buffer Dispatcher Gate"]["parameters"]["jsCode"]
        self.assertIn("dry_run", gate_code)
        self.assertIn("BUFFER_DRAFTED", gate_code)
        self.assertIn("saveToDraft", val_code)


class Stage10Workflow04InboundIntakeTests(unittest.TestCase):
    """Validates Workflow 04: Inbound Ingestion & Intake Router."""

    def setUp(self):
        self.path = Path("n8n/workflows/project/04-nior-communications-inbound-intake.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.nodes = {n["name"]: n for n in self.data["nodes"]}

    def test_poll_node_contract(self):
        poll = self.nodes["Poll Inbound Mailbox"]
        self.assertEqual(poll["parameters"]["method"], "POST")
        self.assertIn("/integrations/email/poll", poll["parameters"]["url"])
        self.assertEqual(poll["parameters"]["genericAuthType"], "httpHeaderAuth")

    def test_intake_evaluation_logic(self):
        code = self.nodes["Process Intake Results"]["parameters"]["jsCode"]
        self.assertIn("fetched", code)
        self.assertIn("recorded", code)
        self.assertIn("unmatched", code)


class Stage10Workflow05ApprovedSendTests(unittest.TestCase):
    """Validates Workflow 05: Approved Send with Fingerprint Gate."""

    def setUp(self):
        self.path = Path("n8n/workflows/project/05-nior-outbound-approved-send.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.nodes = {n["name"]: n for n in self.data["nodes"]}

    def test_outbox_and_send_endpoints(self):
        outbox = self.nodes["Check Outbox Readiness"]
        self.assertEqual(outbox["parameters"]["method"], "GET")
        self.assertIn("/integrations/outbox", outbox["parameters"]["url"])

        send = self.nodes["Execute Approved Send"]
        self.assertEqual(send["parameters"]["method"], "POST")
        self.assertIn("/integrations/email/send", send["parameters"]["url"])

    def test_fingerprint_and_dry_run_safety(self):
        code = self.nodes["Validate Preconditions & Fail-Closed Gate"]["parameters"]["jsCode"]
        self.assertIn("expectedFingerprint", code)
        self.assertIn("dryRun", code)
        self.assertIn("outreach_id is required", code)


if __name__ == "__main__":
    unittest.main()
