"""Offline tests for AI email copywriting; no provider or mailbox is contacted."""
import os
import unittest
from unittest import mock

from outreach.copywriter import CopywriterError, MarketingEmailWriter


class CopywriterTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "AI_WEBSITE_AGENT_EMAIL_COPYWRITER_PROVIDERS": "openrouter",
            "AI_WEBSITE_AGENT_OPENROUTER_API_KEY": "test-key-not-a-real-secret",
            "AI_WEBSITE_AGENT_OPENROUTER_EMAIL_MODEL": "openrouter/free",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lead = {"business_name": "Pitstop Lagos", "business_type": "Automotive service"}
        self.url = "https://pitstop-lagos-preview.vercel.app"
        self.evidence = ["Business category: automotive service", "Located in Lagos"]

    def test_generates_grounded_subject_body_using_free_router(self):
        calls = []
        def request(url, **kwargs):
            calls.append((url, kwargs))
            return {"status": 200, "json": {"model": "test/free-model", "choices": [{
                "message": {"content": '{"subject":"A website concept for Pitstop Lagos","body":"Hello Pitstop Lagos, I prepared a website concept for your automotive service business. You can review it here: https://pitstop-lagos-preview.vercel.app. If useful, I would welcome your feedback."}'}
            }]}}
        result = MarketingEmailWriter(request=request, production=False).generate(self.lead, self.url, self.evidence)
        self.assertEqual(result["subject"], "A website concept for Pitstop Lagos")
        self.assertIn(self.url, result["body"])
        self.assertEqual(result["metadata"]["provider"], "openrouter")
        self.assertEqual(calls[0][0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(calls[0][1]["json_body"]["model"], "openrouter/free")

    def test_rejects_paid_openrouter_model_without_calling_provider(self):
        os.environ["AI_WEBSITE_AGENT_OPENROUTER_EMAIL_MODEL"] = "vendor/paid-model"
        called = []
        writer = MarketingEmailWriter(request=lambda *a, **k: called.append(True), production=False)
        with self.assertRaises(CopywriterError) as caught:
            writer.generate(self.lead, self.url, self.evidence)
        self.assertEqual(caught.exception.code, "COPYWRITER_PROVIDERS_UNAVAILABLE")
        self.assertEqual(called, [])

    def test_rejects_unapproved_preview_url(self):
        writer = MarketingEmailWriter(request=lambda *a, **k: None, production=False)
        with self.assertRaises(CopywriterError) as caught:
            writer.generate(self.lead, "http://unsafe.invalid", self.evidence)
        self.assertEqual(caught.exception.code, "COPYWRITER_INPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
