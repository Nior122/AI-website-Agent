"""Tests for mobile operator dashboard, API control endpoints, background workers, and model generator."""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from builder.content_generator import DeterministicContentGenerator, ModelContentGenerator
from orchestration.api import (
    make_handler,
    start_background_job_worker,
)
from orchestration.controls import held, hold_view, release_hold, set_hold
from orchestration.jobs import JobService
from orchestration.workers import LocalPipelineWorker
from tracking.store import TrackingStore


class DashboardAndControlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"
        self.output_root = self.root / "output"
        self.export_root = self.root / "preview-export"
        self.store = TrackingStore(self.state_path)
        self.jobs = JobService(self.store, max_attempts=3)
        self.worker = LocalPipelineWorker(
            repository_root=Path("."),
            output_root=self.output_root,
        )
        self.token = "test-operator-bearer-token-123456"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.jobs,
                self.token,
                worker=self.worker,
                output_root=self.output_root,
                export_root=self.export_root,
                repository_root=Path("."),
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        self.tmp.cleanup()

    def _req(self, path: str, *, method: str = "GET", body: dict | None = None, auth: bool = True):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, data=data, headers=headers, method=method)
        return urlopen(req, timeout=5.0)

    def test_dashboard_route_serves_html(self) -> None:
        """GET / and GET /dashboard serve HTML without authentication."""
        for path in ["/", "/dashboard"]:
            req = Request(f"{self.base_url}{path}", method="GET")
            with urlopen(req, timeout=5.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type"))
                body = resp.read().decode("utf-8")
                self.assertIn("Operator Console", body)
                self.assertIn("Automation Hold", body)

    def test_controls_hold_and_release(self) -> None:
        """GET /controls/hold, POST /controls/hold, and POST /controls/release work centrally."""
        # Initial status: not held
        with self._req("/controls/hold") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data["held"])

        # Set hold via POST
        hold_payload = {"actor": "phone-tester", "reason": "Testing emergency brake"}
        with self._req("/controls/hold", method="POST", body=hold_payload) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["held"])
            self.assertEqual(data["reason"], "Testing emergency brake")
            self.assertEqual(data["set_by"], "phone-tester")

        # Verify hold is active via store
        self.assertTrue(held(self.store))

        # Check blocked jobs endpoint
        with self._req("/controls/blocked") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("blocked", data)

        # Release hold via POST
        release_payload = {"actor": "phone-tester", "note": "All checks passed"}
        with self._req("/controls/release", method="POST", body=release_payload) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data["held"])
            self.assertEqual(data["released_by"], "phone-tester")

        # Verify hold is released
        self.assertFalse(held(self.store))

    def test_background_job_worker_respects_hold(self) -> None:
        """Background worker thread does not dispatch when automation is held."""
        set_hold(self.store, actor="test-admin", reason="Pause automation")
        stop_event = threading.Event()
        bg_thread = start_background_job_worker(self.jobs, self.worker, stop_event, poll_interval=0.1)
        self.assertIsNotNone(bg_thread)

        try:
            # Create a pending job
            job = self.jobs.create(
                "SCOUT",
                job_id="job-bg-001",
                lead_id="lead-001",
                input={"location": "Lagos", "category": "restaurant"},
            )
            # Give background thread a moment
            stop_event.wait(0.3)
            # State should remain PENDING because hold is active
            current_job = self.jobs.get("job-bg-001")
            self.assertIn(current_job["status"], {"PENDING", "QUEUED"})
        finally:
            stop_event.set()
            if bg_thread:
                bg_thread.join(timeout=1.0)


class ModelContentGeneratorTests(unittest.TestCase):
    def test_fallback_when_no_credentials(self) -> None:
        """ModelContentGenerator falls back to DeterministicContentGenerator if no API key is set."""
        gen = ModelContentGenerator()
        facts = {
            "business_name": "Test Cafe",
            "city": "Lagos",
            "description": "Artisan coffee and pastries.",
            "services": [{"name": "Coffee"}, {"name": "Pastries"}],
            "whatsapp": "2348012345678",
        }
        res = gen.generate(facts, "restaurant")
        self.assertEqual(res["hero_title"], "Test Cafe")
        self.assertIn("Lagos", res["hero_subtitle"])
        self.assertEqual(res["cta"]["whatsapp"], "Chat on WhatsApp")

    def test_mock_model_provider_success(self) -> None:
        """ModelContentGenerator calls provider and formats valid structured content."""
        def fake_provider_request(url, **kwargs):
            return {
                "status": 200,
                "json": {
                    "model": "nvidia/nemotron-4-340b-instruct",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({
                                    "hero_title": "Exquisite Dining at Test Bistro",
                                    "hero_subtitle": "Serving authentic dishes in Victoria Island.",
                                    "about_title": "Our Story",
                                    "about_text": "A premier culinary destination.",
                                })
                            }
                        }
                    ],
                },
            }

        gen = ModelContentGenerator(request=fake_provider_request)
        # Mock environment
        import os
        old_key = os.environ.get("OPENROUTER_API_KEY")
        try:
            os.environ["OPENROUTER_API_KEY"] = "sk-test-mock-key"
            facts = {
                "business_name": "Test Bistro",
                "city": "Victoria Island",
                "description": "Culinary excellence.",
                "whatsapp": "2348000000000",
            }
            res = gen.generate(facts, "restaurant")
            self.assertEqual(res["hero_title"], "Exquisite Dining at Test Bistro")
            self.assertEqual(res["hero_subtitle"], "Serving authentic dishes in Victoria Island.")
            self.assertEqual(res["metadata"]["provider"], "openrouter")
        finally:
            if old_key is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old_key

    def test_mock_model_provider_rejects_hallucinated_claims(self) -> None:
        """ModelContentGenerator rejects prohibited marketing claims and falls back."""
        def fake_provider_request(url, **kwargs):
            return {
                "status": 200,
                "json": {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({
                                    "hero_title": "Test Bistro",
                                    "hero_subtitle": "The #1 award-winning restaurant in Africa!",
                                })
                            }
                        }
                    ],
                },
            }

        gen = ModelContentGenerator(request=fake_provider_request)
        import os
        old_key = os.environ.get("OPENROUTER_API_KEY")
        try:
            os.environ["OPENROUTER_API_KEY"] = "sk-test-mock-key"
            facts = {
                "business_name": "Test Bistro",
                "city": "Victoria Island",
            }
            res = gen.generate(facts, "restaurant")
            # Should have fallen back to deterministic generator!
            self.assertEqual(res["hero_title"], "Test Bistro")
            self.assertNotIn("award-winning", res["hero_subtitle"])
        finally:
            if old_key is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old_key

    def test_settings_api_lifecycle(self) -> None:
        """GET /settings returns defaults and POST /settings updates runtime configurations."""
        from orchestration.settings import get_runtime_settings, update_settings

        tmp = tempfile.TemporaryDirectory()
        try:
            store = TrackingStore(Path(tmp.name) / "state.json")
            # 1. Defaults
            initial = get_runtime_settings(store)
            self.assertEqual(initial["default_city"], "Lagos")
            self.assertEqual(initial["ai_provider"], "openrouter")

            # 2. Update via function
            updated = update_settings(
                store,
                {
                    "default_city": "Abuja",
                    "openrouter_api_key": "sk-or-v1-my-secret-key-12345",
                    "ai_provider": "deterministic",
                    "default_lead_limit": 10,
                },
                actor="test_op",
            )
            self.assertEqual(updated["default_city"], "Abuja")
            self.assertEqual(updated["ai_provider"], "deterministic")
            self.assertEqual(updated["default_lead_limit"], 10)
            self.assertTrue(updated["openrouter_api_key_configured"])
            self.assertTrue("..." in updated["openrouter_api_key"])

            # 3. Model generator uses the updated deterministic provider
            gen = ModelContentGenerator(store=store)
            res = gen.generate({"business_name": "Abuja Lounge"}, "restaurant")
            self.assertEqual(res["hero_title"], "Abuja Lounge")
            self.assertNotIn("metadata", res)  # Deterministic has no metadata
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
