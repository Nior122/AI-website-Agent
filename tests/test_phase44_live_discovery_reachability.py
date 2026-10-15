"""Phase 44: the one live discovery source the repository owns becomes reachable and prioritised.

Before this phase ``OverpassRestaurantSource`` existed, was safe, and was called by nothing: the Scout CLI
accepted only a fixture file, the SCOUT job injected ``FixtureSource``, and the live query returned the same
first handful of Lagos restaurant elements every run because the response was truncated *before* anything was
ranked. These tests pin the repaired funnel - opt-in live discovery from both entry points, priority-ordered
candidates, and the refusals that keep discovery from becoming contact, generation, or publication.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestration.jobs import JobService, NEXT_ACTIONS
from orchestration.workers import LocalPipelineWorker
from scout.cli import main as scout_main
from scout.scout_agent import OverpassRestaurantSource, Scout, Target, _overpass_lagos_restaurants_query
from tracking.store import TrackingStore


def _element(ident: int, name: str | None, **tags) -> dict:
    payload_tags = {"amenity": "restaurant"}
    if name is not None:
        payload_tags["name"] = name
    payload_tags.update(tags)
    return {"type": "node", "id": ident, "lat": 6.45, "lon": 3.4, "tags": payload_tags}


#: Deliberately ordered so the API's own order is *not* the priority order, and wider than the run's limit.
ELEMENTS = [
    _element(11, "Website First Bistro", website="https://bistro.example.test", **{"contact:facebook": "https://facebook.com/bistro"}),
    _element(14, "Facebook Kitchen", **{"contact:facebook": "https://facebook.com/fbk", "phone": "+234-800-000-0000", "cuisine": "Nigerian"}),
    _element(12, "No Social No Website Cafe", **{"addr:street": "Demo Street"}),
    _element(15, None, website="https://nameless.example.test"),
    _element(10, "Instagram Only Grill", **{"contact:instagram": "https://instagram.com/iog"}),
    _element(13, "Website Only Spot", website="https://website-only.example.test"),
]


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _RecordingUrlopen:
    """Stands in for the network so the tests never depend on outbound access."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.bodies: list[str] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001 - mirrors urllib.request.urlopen
        self.bodies.append(request.data.decode("utf-8"))
        return FakeResponse(self.payload)


class LiveSourceContractTests(unittest.TestCase):
    def test_one_bounded_request_covers_the_socially_active_pilot_scope(self) -> None:
        query = _overpass_lagos_restaurants_query(5)
        # The window is wider than the run's limit so ranking can choose; it never becomes an unbounded crawl.
        self.assertIn("out center 20;", query)
        self.assertEqual(query.count("nwr["), 3)
        for clause in ('"amenity"="restaurant"', '"contact:facebook"', '"contact:instagram"'):
            self.assertIn(clause, query)
        # Only documented existence filters: no negation, no regex, no follow-the-link semantics.
        self.assertNotIn("!~", query)
        self.assertNotIn("out center 0;", query)
        self.assertLessEqual(int(query.split("out center ")[1].rstrip(";")), 40)

    def test_small_limits_stay_small(self) -> None:
        self.assertIn("out center 4;", _overpass_lagos_restaurants_query(1))

    def test_scope_guard_still_refuses_any_other_target(self) -> None:
        source = OverpassRestaurantSource(max_requests=1)
        with self.assertRaisesRegex(ValueError, "approved only for Lagos, Nigeria restaurants"):
            source.discover(Target("Nigeria", "Abuja", "Salons", lead_limit=5))
        with patch("scout.scout_agent.urlopen") as fake:
            with self.assertRaisesRegex(ValueError, "approved only for Lagos"):
                source.discover(Target("Ghana", "Lagos", "Restaurants", lead_limit=5))
            fake.assert_not_called()

    def test_candidates_are_ranked_before_truncation_and_capped(self) -> None:
        recorder = _RecordingUrlopen({"elements": ELEMENTS})
        source = OverpassRestaurantSource(max_requests=1, min_delay_seconds=0)
        target = Target("Nigeria", "Lagos", "Restaurants", lead_limit=2)
        with patch("scout.scout_agent.urlopen", recorder):
            candidates = source.discover(target)
        self.assertEqual(len(recorder.bodies), 1)
        self.assertEqual([c["business_name"] for c in candidates], ["Facebook Kitchen", "Instagram Only Grill"])
        self.assertEqual(candidates[0]["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(candidates[0]["source_type"], "OPENSTREETMAP_ELEMENT")
        self.assertEqual(candidates[0]["website"], None)
        self.assertTrue(all("Website" not in c["business_name"] and "No Social" not in c["business_name"] for c in candidates))

    def test_uncertainty_about_a_missing_website_is_never_reported_as_a_fact(self) -> None:
        recorder = _RecordingUrlopen({"elements": ELEMENTS})
        source = OverpassRestaurantSource(max_requests=1, min_delay_seconds=0)
        with patch("scout.scout_agent.urlopen", recorder):
            result = Scout(Target("Nigeria", "Lagos", "Restaurants", lead_limit=3), [source]).run()
        self.assertEqual(result.status, "completed")
        top = result.leads[0]
        self.assertEqual(top["websiteStatus"], "none_known")
        self.assertIn("No professional website is known from the collected sources", top["qualificationReason"])
        self.assertEqual(top["websiteOpportunityScore"], 4)
        self.assertEqual(top["onlinePresenceScore"], 3)  # social profile, services, description-derived signals
        self.assertEqual(top["social"]["otherSocialUrls"], ["https://facebook.com/fbk"])
        self.assertTrue(all(isinstance(url, str) and url.startswith("https://") for url in top["sourceUrls"]))


class ScoutCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fixture = self.root / "candidates.json"
        self.fixture.write_text(json.dumps([{"business_name": "Fixture Only Cafe", "niche": "Restaurants",
                                              "city": "Lagos", "country": "Nigeria",
                                              "instagram_url": "https://instagram.com/fixture-only"}]), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = scout_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_live_source_refuses_without_approval_and_touches_no_network(self) -> None:
        with patch("scout.scout_agent.urlopen") as fake:
            with self.assertRaises(SystemExit) as raised:
                self._run(["--dry-run", "--source", "overpass"])
        self.assertEqual(raised.exception.code, 2)
        fake.assert_not_called()

    def test_fixture_remains_the_default_and_still_needs_its_file(self) -> None:
        with patch("scout.scout_agent.urlopen") as fake:
            with self.assertRaises(SystemExit) as raised:
                self._run(["--dry-run"])
        self.assertEqual(raised.exception.code, 2)
        fake.assert_not_called()

    def test_fixture_run_is_unchanged_offline_behaviour(self) -> None:
        output = self.root / "result.json"
        with patch("scout.scout_agent.urlopen") as fake:
            code, _, _ = self._run(["--dry-run", "--source-file", str(self.fixture), "--output", str(output)])
        self.assertEqual(code, 0)
        fake.assert_not_called()
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["status"], "completed")
        self.assertEqual(document["leads"][0]["businessName"], "Fixture Only Cafe")
        self.assertEqual(document["leads"][0]["websiteStatus"], "none_known")

    def test_approved_live_run_makes_one_bounded_request_and_reports_provenance(self) -> None:
        recorder = _RecordingUrlopen({"elements": ELEMENTS})
        output = self.root / "live.json"
        with patch("scout.scout_agent.urlopen", recorder):
            code, stdout, stderr = self._run([
                "--dry-run", "--source", "overpass", "--approve-live-lookups",
                "--lead-limit", "3", "--output", str(output),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(len(recorder.bodies), 1)
        self.assertIn('"contact:instagram"', recorder.bodies[0])
        # The operator-facing note goes to stderr so stdout and the written file stay machine-parseable.
        self.assertIn("no one is contacted", stderr)
        self.assertNotIn("no one is contacted", stdout)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["source_counts"], {"openstreetmap_overpass": 3})
        # Ranking plus qualification, working together: the two socially active candidates with no website
        # on the record lead, the one that already has a website is demoted to needs_review, and a business
        # whose only public tag is a street address is skipped for lack of source-backed activity evidence.
        self.assertEqual([lead["businessName"] for lead in document["leads"]],
                         ["Facebook Kitchen", "Instagram Only Grill", "Website First Bistro"])
        self.assertEqual([lead["qualificationStatus"] for lead in document["leads"]],
                         ["qualified", "qualified", "needs_review"])
        self.assertEqual(document["leads"][0]["dataClassification"], "REAL_PUBLIC_DATA")
        self.assertNotIn("No Social No Website Cafe", [lead["businessName"] for lead in document["leads"]])

    def test_request_budget_cannot_be_widened_from_the_command_line(self) -> None:
        with patch("scout.scout_agent.urlopen") as fake:
            with self.assertRaises(SystemExit) as raised:
                self._run(["--dry-run", "--source", "overpass", "--approve-live-lookups", "--max-requests", "40"])
        self.assertEqual(raised.exception.code, 2)
        fake.assert_not_called()

    def test_a_source_that_errors_is_not_reported_as_success(self) -> None:
        def explode(request, timeout=None):  # noqa: ANN001
            raise TimeoutError("simulated upstream timeout")

        out, err = io.StringIO(), io.StringIO()
        with patch("scout.scout_agent.urlopen", explode):
            with redirect_stdout(out), redirect_stderr(err):
                code = scout_main(["--dry-run", "--source", "overpass", "--approve-live-lookups"])
        self.assertEqual(code, 1)
        document = json.loads(out.getvalue())
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["candidate_count"], 0)
        self.assertIn("Overpass discovery failed safely", document["errors"][0])


class ScoutJobLiveSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_path = self.root / "state.json"
        self.jobs = JobService(TrackingStore(self.state_path))
        self.worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output")
        self.fixture = self.root / "candidates.json"
        self.fixture.write_text(json.dumps([{"business_name": "Fixture Cafe", "niche": "Restaurants",
                                              "city": "Lagos", "country": "Nigeria",
                                              "instagram_url": "https://instagram.com/fixture"}]), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _run(self, job_input: dict, *, env: dict | None = None, urlopen=None):
        environment = {"AI_WEBSITE_AGENT_ALLOW_LIVE_DISCOVERY": "1"}
        environment.update(env or {})
        job = self.jobs.create("SCOUT", job_id=f"job-{len(self.jobs.store.state['jobs']) + 1}", input=job_input)
        with patch.dict(os.environ, environment, clear=False):
            if urlopen is None:
                with patch("scout.scout_agent.urlopen") as fake:
                    result = self.jobs.run(job["job_id"], self.worker)
                    self.no_network_call = fake
            else:
                with patch("scout.scout_agent.urlopen", urlopen):
                    result = self.jobs.run(job["job_id"], self.worker)
        return result

    def test_fixture_job_behaviour_is_preserved(self) -> None:
        result = self._run({"source_file": str(self.fixture), "lead_limit": 1})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(result["output"]["new_lead_ids"]), 1)
        self.assertEqual(result["next_job_types"], ["RESEARCH"])
        self.no_network_call.assert_not_called()

    def test_missing_source_input_is_a_refusal_not_a_crash(self) -> None:
        result = self._run({})
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "SCOUT_INPUT_INVALID")
        self.no_network_call.assert_not_called()

    def test_unknown_source_is_refused(self) -> None:
        result = self._run({"source": "google-maps-scraper"})
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "SCOUT_SOURCE_UNKNOWN")
        self.no_network_call.assert_not_called()

    def test_live_job_without_the_operator_flag_never_requests(self) -> None:
        result = self._run({"source": "openstreetmap_overpass"})
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "LIVE_DISCOVERY_NOT_APPROVED")
        self.assertIn("approve_live_lookups", result["next_action"])
        self.no_network_call.assert_not_called()

    def test_live_job_without_the_deployment_permit_never_requests(self) -> None:
        result = self._run({"source": "openstreetmap_overpass", "approve_live_lookups": True},
                           env={"AI_WEBSITE_AGENT_ALLOW_LIVE_DISCOVERY": "0"})
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "LIVE_DISCOVERY_NOT_ENABLED")
        self.assertIn("AI_WEBSITE_AGENT_ALLOW_LIVE_DISCOVERY", result["next_action"])
        self.no_network_call.assert_not_called()

    def test_approved_live_job_creates_discovered_leads_and_nothing_else(self) -> None:
        recorder = _RecordingUrlopen({"elements": ELEMENTS})
        result = self._run({"source": "openstreetmap_overpass", "approve_live_lookups": True, "lead_limit": 2},
                           urlopen=recorder)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(len(recorder.bodies), 1)
        store = TrackingStore(self.state_path)
        self.assertEqual(len(store.state["leads"]), 2)
        lead = next(iter(store.state["leads"].values()))
        self.assertEqual(lead["qualification_status"], "DISCOVERED")
        self.assertEqual(lead["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(lead["website_status"], "none_known")
        # Public-data contact details stay in the source record; they are not copied into the lead book.
        self.assertIsNone(lead["phone"])
        self.assertIsNone(lead["instagram_url"])
        self.assertTrue(lead["source_urls"])
        # And discovery alone must not have started the rest of the funnel.
        self.assertEqual(store.state["projects"], {})
        self.assertEqual(store.state["builds"], {})
        self.assertEqual(store.state["outreach"], {})
        self.assertEqual(store.state["deployments"], {})
        # Approval records are created lazily, so absence is the honest thing to assert here.
        self.assertEqual(store.state.get("approvals", {}), {})
        self.assertEqual(store.state["reviews"], {})

    def test_new_refusal_codes_carry_a_remedy(self) -> None:
        for code in ("LIVE_DISCOVERY_NOT_APPROVED", "LIVE_DISCOVERY_NOT_ENABLED", "SCOUT_SOURCE_UNKNOWN", "SCOUT_INPUT_INVALID"):
            self.assertIn(code, NEXT_ACTIONS)
            self.assertTrue(NEXT_ACTIONS[code].strip())


if __name__ == "__main__":
    unittest.main()
