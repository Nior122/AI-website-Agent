"""Comprehensive tests for the Authoritative Daily Business Briefing Engine.

Verifies schema compliance, plain text rules (no markdown), Africa/Lagos
time conversion, duplicate detection, chunking, and Telegram bot commands.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import re
import unittest
from unittest.mock import MagicMock, patch

from orchestration.briefing import (
    compute_authoritative_briefing_data,
    format_daily_briefing_text,
    chunk_telegram_text,
    DailyBriefingService,
    get_lagos_now,
    LAGOS_TZ,
)
from orchestration.telegram_bot import TelegramOperatorBot


class BriefingEngineTests(unittest.TestCase):
    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.state = {
            "leads": {
                "lead-001": {
                    "lead_id": "lead-001",
                    "business_name": "Lagos Dental Hub",
                    "status": "qualified",
                    "estimated_value": 750000.0,
                    "category": "dental_clinic",
                    "email": "contact@lagosdental.invalid",
                    "phone": "+2348011112222",
                    "next_follow_up_at": "2026-10-20T08:00:00Z",  # yesterday (overdue)
                },
                "lead-002": {
                    "lead_id": "lead-002",
                    "business_name": "Victoria Island Legal Chambers",
                    "status": "contacted",
                    "estimated_value": 1200000.0,
                    "category": "legal",
                    "email": "info@vilegal.invalid",
                    "phone": "+2348033334444",
                    "next_follow_up_at": "2026-10-21T09:00:00Z",  # today
                },
                "lead-003": {
                    "lead_id": "lead-003",
                    "business_name": "Lagos Dental Hub",  # duplicate candidate
                    "status": "new",
                    "estimated_value": 500000.0,
                    "category": "dental_clinic",
                    "email": "contact@lagosdental.invalid",  # same email!
                    "phone": "+2348011112222",
                },
            },
            "projects": {},
            "jobs": {
                "job-101": {
                    "id": "job-101",
                    "title": "Client Website QA Check",
                    "status": "PENDING",
                    "due_at": "2026-10-20T10:00:00Z",  # overdue
                    "priority": "HIGH",
                },
                "job-102": {
                    "id": "job-102",
                    "title": "Deploy Approved Preview",
                    "status": "IN_PROGRESS",
                    "due_at": "2026-10-21T12:00:00Z",  # today
                    "priority": "NORMAL",
                },
                "job-103": {
                    "id": "job-103",
                    "title": "Weekly Backlog Grooming",
                    "status": "PENDING",
                    "due_at": "2026-10-25T15:00:00Z",  # upcoming
                    "priority": "LOW",
                },
            },
        }

    def test_lagos_time_conversion(self):
        utc_dt = datetime(2026, 10, 21, 7, 0, 0, tzinfo=timezone.utc)
        lagos_dt = get_lagos_now(utc_dt)
        self.assertEqual(lagos_dt.hour, 8)
        self.assertEqual(lagos_dt.tzinfo, LAGOS_TZ)

    def test_authoritative_metrics_computation(self):
        ref_time = datetime(2026, 10, 21, 7, 30, 0, tzinfo=timezone.utc)
        data = compute_authoritative_briefing_data(store=self.mock_store, ref_time=ref_time)

        self.assertEqual(data["active_lead_count"], 3)
        self.assertEqual(data["qualified_lead_count"], 1)
        self.assertEqual(data["pipeline_value"], 2450000.0)

        # Overdue task check
        self.assertEqual(len(data["overdue_tasks"]), 1)
        self.assertEqual(data["overdue_tasks"][0]["id"], "job-101")

        # Due today task check
        self.assertEqual(len(data["tasks_due_today"]), 1)
        self.assertEqual(data["tasks_due_today"][0]["id"], "job-102")

        # Upcoming task check
        self.assertEqual(len(data["upcoming_tasks"]), 1)
        self.assertEqual(data["upcoming_tasks"][0]["id"], "job-103")

        # Past due follow up check
        self.assertGreaterEqual(data["past_due_lead_follow_up_count"], 1)
        self.assertEqual(data["past_due_lead_follow_ups"][0]["id"], "lead-001")

        # Duplicate detector check
        self.assertEqual(data["duplicate_review_count"], 1)
        pair = data["duplicate_pairs"][0]
        self.assertEqual(pair["lead1_id"], "lead-001")
        self.assertEqual(pair["lead2_id"], "lead-003")
        self.assertEqual(pair["classification"], "REVIEW")
        self.assertEqual(pair["confidence"], "HIGH")

    def test_formatting_plain_text_compliance(self):
        ref_time = datetime(2026, 10, 21, 7, 30, 0, tzinfo=timezone.utc)
        data = compute_authoritative_briefing_data(store=self.mock_store, ref_time=ref_time)
        briefing_text = format_daily_briefing_text(data)

        # Must have required section headers
        self.assertIn("📊 DAILY BUSINESS BRIEFING", briefing_text)
        self.assertIn("━━━━━━━━━━━━━━━━━━━━", briefing_text)
        self.assertIn("📅 DATE", briefing_text)
        self.assertIn("🔥 PRIORITY OPPORTUNITIES", briefing_text)
        self.assertIn("💰 PIPELINE", briefing_text)
        self.assertIn("🚨 OVERDUE", briefing_text)
        self.assertIn("📅 DUE TODAY", briefing_text)
        self.assertIn("🗓️ UPCOMING", briefing_text)
        self.assertIn("⚠️ NEEDS ATTENTION", briefing_text)
        self.assertIn("🎯 RECOMMENDED ACTIONS", briefing_text)

        # Strict plain text rules: NO markdown formatting allowed
        self.assertNotIn("**", briefing_text)
        self.assertNotIn("```", briefing_text)
        self.assertNotIn("##", briefing_text)
        self.assertNotIn("__", briefing_text)

        # Money values in Nigerian naira
        self.assertIn("₦2,450,000", briefing_text)

        # Every recommendation starts with "Recommendation:"
        rec_lines = [line.strip() for line in briefing_text.splitlines() if re.match(r"^\d+\.\s+", line.strip())]
        self.assertGreaterEqual(len(rec_lines), 3)
        for line in rec_lines:
            self.assertTrue(re.search(r"Recommendation:", line), f"Line missing 'Recommendation:': {line}")

    def test_chunking_large_text(self):
        short_text = "Short briefing text"
        chunks = chunk_telegram_text(short_text, max_length=100)
        self.assertEqual(chunks, [short_text])

        # Create text exceeding max_length
        long_paragraph = "This is a detailed paragraph of operational briefing content.\n\n" * 40
        chunks = chunk_telegram_text(long_paragraph, max_length=500)
        self.assertGreater(len(chunks), 1)
        for i, chunk in enumerate(chunks):
            self.assertLessEqual(len(chunk), 600)
            self.assertIn(f"Part {i + 1}/{len(chunks)}", chunk)

    def test_daily_briefing_service(self):
        service = DailyBriefingService(store=self.mock_store)
        res = service.generate_briefing()
        self.assertIn("📊 DAILY BUSINESS BRIEFING", res["text"])
        self.assertIsInstance(res["chunks"], list)
        self.assertGreaterEqual(len(res["chunks"]), 1)
        self.assertEqual(res["data"]["active_lead_count"], 3)

    def test_telegram_bot_briefing_command(self):
        bot = TelegramOperatorBot(
            api=None,
            store=self.mock_store,
            allowed_user_ids=[12345],
            allowed_chat_ids=[67890],
        )

        # Pin the service clock so this fixture keeps one overdue task as dates advance.
        with patch("orchestration.briefing.get_lagos_now",
                   return_value=datetime(2026, 10, 21, 8, 30, tzinfo=LAGOS_TZ)):
            # Test /briefing
            res1 = bot.dispatch("/briefing", user_id=12345, chat_id=67890, deliver=False)
            # Test /daily alias
            res2 = bot.dispatch("/daily", user_id=12345, chat_id=67890, deliver=False)
        self.assertEqual(res1.get("code"), "DAILY_BRIEFING")
        self.assertIn("📊 DAILY BUSINESS BRIEFING", res1.get("reply", ""))
        self.assertEqual(res1.get("active_leads"), 3)
        self.assertEqual(res1.get("overdue_tasks"), 1)

        self.assertEqual(res2.get("code"), "DAILY_BRIEFING")
        self.assertIn("📊 DAILY BUSINESS BRIEFING", res2.get("reply", ""))


if __name__ == "__main__":
    unittest.main()
