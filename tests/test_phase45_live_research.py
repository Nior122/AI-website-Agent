"""Phase 45: bounded live research of the pages a record already names.

Every test here drives ``PublicWebsiteResearchSource`` through an injected transport, because the point of the
phase is what the source *does and refuses to do* with bytes it was given - not whether a particular host
happens to be reachable. Nothing in the suite opens a socket, and the refusals are asserted to be refusals:
`no request attempted`, rather than a request that failed politely.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orchestration.jobs import JobService
from orchestration.workers import LocalPipelineWorker
from research.cli import main as research_main
from research.live_source import (
    LiveResearchPolicy,
    LiveResearchRefused,
    LiveResearchUnavailable,
    PublicWebsiteResearchSource,
    permitted_public_target,
)
from research.research_agent import ResearchAgent
from tracking.store import TrackingStore

ALLOW_ROBOTS = b"User-agent: *\nDisallow:\n"
DISALLOW_ROBOTS = b"User-agent: *\nDisallow: /\n"

POOR_PAGE = (
    b"<html><head><title>Pitstop Lagos</title></head><body>"
    b"<h1>Pitstop Lagos</h1><p>Grill and drinks in Ikeja, Lagos, Nigeria.</p>"
    b"<a href='/page2'>More</a></body></html>"
)
MODERN_PAGE = (
    b'<html><head><title>Pitstop Lagos</title><meta name="viewport" content="width=device-width">'
    b'<meta property="og:site_name" content="Pitstop Lagos"></head><body><nav>'
    b'<a href="/menu">Menu</a><a href="/about">About</a><a href="/contact">Contact</a>'
    b'<a href="tel:+2348000000000">Call</a></nav><h1>Menu</h1><p>Jollof, suya, grills. Book a table today.</p>'
    b"<p>Lagos, Nigeria</p></body></html>"
)
OTHER_BUSINESS_PAGE = (
    b"<html><head><title>Some Unrelated Shop</title></head><body><p>Lagos, Nigeria</p></body></html>"
)
LOGIN_PAGE = (
    b"<html><head><title>Sign in</title></head><body><p>Please log in to continue.</p>"
    b'<form action="/login"><input name="user"></form></body></html>'
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.status = status
        self.headers = _Headers(headers or {"Content-Type": "text/html; charset=utf-8"})

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _Headers(dict):
    def get(self, key, default=None):  # noqa: ANN001
        for existing, value in self.items():
            if existing.lower() == str(key).lower():
                return value
        return default

    def items(self):  # noqa: D102
        return dict.items(self)


class FakeTransport:
    """Answers per URL in order of request, recording everything it was asked for."""

    def __init__(self, pages: dict[str, object]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001 - mirrors urllib.request.urlopen
        url = request.full_url
        self.calls.append((url, dict(request.headers)))
        if url not in self.pages:
            raise urllib.error.URLError("no fixture for this URL")
        outcome = self.pages[url]
        if isinstance(outcome, list):
            outcome = outcome[min(len(self.calls) - 1, len(outcome) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


def enabled_policy(**overrides) -> LiveResearchPolicy:  # noqa: ANN001 - test convenience
    base = dict(enabled=True, permission_source="test", max_requests=12, min_delay_seconds=0.0, respect_robots=False)
    base.update(overrides)
    return LiveResearchPolicy(**base)


def candidate(**overrides) -> dict:
    record = {
        "businessName": "Pitstop Lagos",
        "niche": "Restaurants",
        "location": {"city": "Lagos", "country": "Nigeria"},
        "contact": {"phone": None, "email": None, "whatsapp": None},
        "originalWebsite": "https://pitstop-lagos.example/",
        "social": {"instagramUrl": "https://instagram.com/pitstoplagos", "facebookUrl": None, "otherSocialUrls": []},
        "sourceUrls": ["https://www.openstreetmap.org/node/123"],
        "fieldSources": {},
        "publicBusinessInformation": {"servicesOrProducts": [], "description": None, "openingHours": None},
        "websiteStatus": "none_known",
        "dataClassification": "REAL_PUBLIC_DATA",
    }
    record.update(overrides)
    return record


def robots_url(url: str = "https://pitstop-lagos.example/") -> str:
    return url.split("?")[0].rsplit("/", 1)[0] + "/robots.txt"


class PolicyAndConsentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def settings(self, document: dict) -> Path:
        path = self.root / "system.settings.local.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_both_keys_are_required_and_neither_alone_opens_the_door(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH", None)
            config_only = LiveResearchPolicy.load(self.settings({"research": {"allowLiveResearch": True}}))
            self.assertFalse(config_only.enabled)
            self.assertIn("AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH", config_only.refusal_reason())
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            env_only = LiveResearchPolicy.load(self.settings({"research": {"allowLiveResearch": False}}))
            self.assertFalse(env_only.enabled)
            self.assertIn("allowLiveResearch", env_only.refusal_reason())
            both = LiveResearchPolicy.load(self.settings({"research": {"allowLiveResearch": True}}))
            self.assertTrue(both.enabled)
            self.assertIn("AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH", both.permission_source)

    def test_configuration_can_only_tighten_never_remove_a_bound(self) -> None:
        path = self.settings({"research": {"allowLiveResearch": True, "maxPagesPerCandidate": 99,
                                          "maxRequestsPerRun": 999, "requestTimeoutSeconds": 600,
                                          "maxResponseBytes": 99_000_000, "maxRetries": 50,
                                          "minDelaySeconds": -5}})
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            policy = LiveResearchPolicy.load(path)
        self.assertEqual((policy.max_pages, policy.max_requests, policy.timeout_seconds), (3, 12, 45))
        self.assertEqual(policy.max_bytes, 2_000_000)
        self.assertEqual(policy.max_retries, 2)
        self.assertEqual(policy.min_delay_seconds, 0.0)
        self.assertTrue(policy.respect_robots)

    def test_platform_profiles_stay_unfetched_unless_explicitly_allowlisted(self) -> None:
        blocked = permitted_public_target("https://instagram.com/pitstoplagos", kind="social_profile", policy=enabled_policy())
        self.assertIsNone(blocked)
        opened = permitted_public_target("https://instagram.com/pitstoplagos", kind="social_profile",
                                        policy=enabled_policy(fetch_platform_profiles=True, allow_hosts=("instagram.com",)))
        self.assertIsNotNone(opened)
        # A platform recorded as the business "website" is still refused: we do not read a login wall as a site.
        self.assertIsNone(permitted_public_target("https://facebook.com/shop", kind="business_website",
                                                  policy=enabled_policy(fetch_platform_profiles=True, allow_hosts=("facebook.com",))))

    def test_disabled_policy_refuses_construction_before_any_transport_exists(self) -> None:
        transport = FakeTransport({})
        with self.assertRaises(LiveResearchRefused) as raised:
            PublicWebsiteResearchSource(LiveResearchPolicy(), transport=transport)
        self.assertEqual(raised.exception.code, "LIVE_RESEARCH_NOT_ENABLED")
        self.assertEqual(transport.calls, [])


class TargetPermissionTests(unittest.TestCase):
    def test_only_plain_public_page_urls_are_permitted(self) -> None:
        for url in ("http://127.0.0.1/admin", "https://10.0.0.5/", "https://192.168.1.10/x.html",
                    "https://[::1]/", "https://localhost:3000/", "https://intranet.local/", "https://db.internal/",
                    "https://user:pass@pitstop.example/", "https://pitstop.example:8443/", "ftp://pitstop.example/x",
                    "pitstop.example/x", "", None, "   ", 42, "https://singlelabel/"):
            with self.subTest(url=url):
                self.assertIsNone(permitted_public_target(url))
        self.assertIsNotNone(permitted_public_target("https://www.pitstop-lagos.example/menu"))

    def test_a_name_that_resolves_privately_is_refused(self) -> None:
        with patch("research.live_source.socket.getaddrinfo", return_value=[(2, 1, 0, "", ("127.0.0.1", 443))]):
            self.assertIsNone(permitted_public_target("https://pitstop-lagos.example/"))

    def test_an_unavailable_resolver_adds_no_refusal_and_no_new_failure(self) -> None:
        import socket as _socket

        with patch("research.live_source.socket.getaddrinfo", side_effect=_socket.gaierror("resolver unavailable")):
            target = permitted_public_target("https://pitstop-lagos.example/")
        self.assertIsNotNone(target)


class ObservationTests(unittest.TestCase):
    def source(self, pages: dict[str, object], **policy_overrides) -> tuple[PublicWebsiteResearchSource, FakeTransport]:
        transport = FakeTransport(pages)
        return PublicWebsiteResearchSource(enabled_policy(**policy_overrides), transport=transport), transport

    def test_a_poor_page_becomes_traceable_observations_not_opinions(self) -> None:
        source, transport = self.source({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(POOR_PAGE)},
                                        respect_robots=True)
        items = source.research(candidate())
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(items), 1)
        item = items[0]
        observation = item["website_observation"]
        self.assertEqual(observation["load"], "loaded")
        self.assertIs(observation["correct_business"], True)
        self.assertIs(observation["poor_mobile"], True)
        self.assertIs(observation["missing_contact"], True)
        self.assertIs(observation["missing_services"], True)
        self.assertNotIn("untrustworthy", observation)
        self.assertNotIn("broken_links", observation)
        self.assertNotIn("outdated_information", observation)
        self.assertEqual(item["source_url"], "https://pitstop-lagos.example/")
        self.assertTrue(item["observed_at"].endswith("Z"))
        self.assertIn("viewport meta: absent", item["excerpt"])
        self.assertEqual(item["retrieval"]["status"], "FETCHED")
        self.assertEqual(item["retrieval"]["http_status"], 200)
        self.assertEqual(item["data_classification"], "REAL_PUBLIC_DATA")

    def test_the_agent_turns_observations_into_an_evaluated_website_and_evidence(self) -> None:
        source, _ = self.source({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(MODERN_PAGE)})
        with patch("research.live_source.socket.getaddrinfo", return_value=[(2, 1, 0, "", ("93.95.1.1", 443))]):
            result = ResearchAgent([source]).run(candidate())
        self.assertEqual(result.status, "completed")
        research = result.record["research"]
        self.assertEqual(research["websiteEvaluation"]["status"], "WEBSITE_MODERN")
        self.assertIn("https://pitstop-lagos.example/", research["researchSources"])
        # A modern, verified site is a reason to leave the business alone, not an opportunity to manufacture.
        self.assertEqual(research["qualificationStatus"], "SKIP")
        phone_facts = [fact for fact in research["verifiedFacts"] if fact["field"] == "phone"]
        self.assertEqual(phone_facts[0]["value"], "+2348000000000")
        self.assertEqual(phone_facts[0]["sourceUrls"], ["https://pitstop-lagos.example/"])
        self.assertTrue(phone_facts[0]["observed_at"])
        tiers = {record["evidence_tier"] for record in research["evidenceRecords"]}
        self.assertIn("PRESERVED_PUBLIC_OBSERVATION", tiers)

    def test_one_unreadable_page_is_never_reported_as_a_website_defect_or_absence(self) -> None:
        for name, pages, expected_status, expected_reason, extra in (
            ("robots", {robots_url(): FakeResponse(DISALLOW_ROBOTS)}, "ROBOTS_DISALLOWED", "not fetched (robots_disallowed)", {"respect_robots": True}),
            ("timeout", {robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": urllib.error.URLError("timed out")}, "UNREACHABLE", "transport failure", {"respect_robots": True}),
            ("blocked", {robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": urllib.error.HTTPError("https://pitstop-lagos.example/", 403, "Forbidden", None, None)}, "ACCESS_RESTRICTED", "no credentials or bypass attempted", {}),
            ("missing", {robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": urllib.error.HTTPError("https://pitstop-lagos.example/", 404, "Not Found", None, None)}, "NOT_FOUND_AT_URL", "HTTP 404", {}),
            ("login", {robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(LOGIN_PAGE)}, "LOGIN_WALL", "login wall (not bypassed)", {}),
        ):
            with self.subTest(case=name):
                source, transport = self.source(pages, **extra)
                with self.assertRaises(LiveResearchUnavailable) as raised:
                    source.research(candidate())
                self.assertIn(expected_reason, str(raised.exception))
                self.assertIn(expected_status, str(raised.exception))
                logged = next(entry for entry in source.attempt_log if entry.get("host") == "pitstop-lagos.example")
                self.assertEqual(logged["status"], expected_status)
                if expected_status == "LOGIN_WALL":
                    # The uncertainty sentence is retained even when the whole run fails, so the record can
                    # never be read as "we checked and there is no website".
                    self.assertIn("not evidence that the business lacks a website", logged["detail"])
                if name == "robots":
                    # The page was never requested at all: the restriction is obeyed, not negotiated with.
                    self.assertEqual(transport.urls, [robots_url()])
                # Nothing about this business was concluded: the failure is recorded as ours.
                self.assertTrue(source.attempt_log)
        # And through the agent, the same record reads UNKNOWN with an error, never NO_WEBSITE.
        source, _ = self.source({robots_url(): FakeResponse(DISALLOW_ROBOTS)}, respect_robots=True)
        result = ResearchAgent([source]).run(candidate())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.record["research"]["websiteEvaluation"]["status"], "UNKNOWN")
        self.assertNotIn("NO_WEBSITE", json.dumps(result.record["research"]["websiteEvaluation"]))
        self.assertTrue(result.errors)

    def test_a_page_that_does_not_name_the_business_is_not_attributed_to_it(self) -> None:
        source, _ = self.source({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(OTHER_BUSINESS_PAGE)})
        items = source.research(candidate())
        self.assertEqual(len(items), 1)
        self.assertNotIn("website_observation", items[0])
        self.assertIn("does not name this business", items[0]["retrieval"]["uncertainty"])
        self.assertEqual(items[0]["business_name"], "Some Unrelated Shop")
        result = ResearchAgent([source]).run(candidate())
        self.assertEqual(result.record["research"]["websiteEvaluation"]["status"], "UNKNOWN")

    def test_malformed_and_non_html_answers_are_recorded_not_interpreted(self) -> None:
        broken = b"<html><title>Unclosed <div class='x'><p>Pitstop Lagos"
        source, _ = self.source({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(broken)})
        items = source.research(candidate())
        self.assertEqual(items[0]["retrieval"]["status"], "FETCHED")
        self.assertIn("Pitstop Lagos", items[0]["excerpt"])

        json_only = {robots_url(): FakeResponse(ALLOW_ROBOTS),
                     "https://pitstop-lagos.example/": FakeResponse(b'{"status":"ok"}', headers={"Content-Type": "application/json"})}
        source, _ = self.source(json_only)
        with self.assertRaises(LiveResearchUnavailable) as raised:
            source.research(candidate())
        self.assertIn("non-HTML", str(raised.exception))

    def test_oversized_pages_are_cut_off_rather_than_read(self) -> None:
        huge = POOR_PAGE + b"<!-- " + b"x" * 200_000 + b" -->"
        source, _ = self.source({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(huge)},
                                max_bytes=1_000)
        with self.assertRaises(LiveResearchUnavailable) as raised:
            source.research(candidate())
        self.assertIn("byte cap", str(raised.exception))

    def test_robots_txt_is_read_first_and_an_exhausted_budget_is_recorded_not_skipped(self) -> None:
        instagram = "https://instagram.com/pitstoplagos"
        transport = FakeTransport({
            robots_url(): FakeResponse(ALLOW_ROBOTS),
            "https://pitstop-lagos.example/": FakeResponse(POOR_PAGE),
            "https://instagram.com/robots.txt": FakeResponse(ALLOW_ROBOTS),
        })
        policy = enabled_policy(max_requests=3, respect_robots=True, max_pages=3, fetch_platform_profiles=True,
                                allow_hosts=("instagram.com",))
        source = PublicWebsiteResearchSource(policy, transport=transport)
        items = source.research(candidate(originalWebsite="https://pitstop-lagos.example/", social={
            "instagramUrl": instagram, "facebookUrl": None, "otherSocialUrls": []}))
        self.assertEqual(len(items), 1)
        self.assertEqual(source._requests_made, 3)
        statuses = [entry["status"] for entry in source.attempt_log]
        self.assertIn("REQUEST_BUDGET_EXHAUSTED", statuses)
        self.assertEqual(len(transport.calls), 3)

    def test_rate_limited_pages_are_retried_once_within_the_recorded_budget(self) -> None:
        denied = urllib.error.HTTPError("https://pitstop-lagos.example/", 429, "Too Many", _Headers({"Retry-After": "0"}), None)
        transport = FakeTransport({robots_url(): FakeResponse(ALLOW_ROBOTS),
                                   "https://pitstop-lagos.example/": [denied, FakeResponse(POOR_PAGE)]})
        source = PublicWebsiteResearchSource(enabled_policy(max_retries=1, respect_robots=False), transport=transport)
        items = source.research(candidate())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["retrieval"]["attempts"], 2)
        self.assertEqual(items[0]["retrieval"]["status"], "FETCHED")

    def test_retries_can_be_switched_off_entirely(self) -> None:
        denied = urllib.error.HTTPError("https://pitstop-lagos.example/", 429, "Too Many", _Headers({"Retry-After": "0"}), None)
        transport = FakeTransport({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": denied})
        source = PublicWebsiteResearchSource(enabled_policy(max_retries=0, respect_robots=False), transport=transport)
        with self.assertRaises(LiveResearchUnavailable):
            source.research(candidate())
        self.assertEqual([url for url in transport.urls if "robots" not in url], ["https://pitstop-lagos.example/"])

    def test_the_user_agent_is_honest_and_no_credential_header_is_sent(self) -> None:
        transport = FakeTransport({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(POOR_PAGE)})
        source = PublicWebsiteResearchSource(enabled_policy(respect_robots=True), transport=transport)
        source.research(candidate())
        for _, headers in transport.calls:
            joined = {key.lower(): value for key, value in headers.items()}
            self.assertIn("AI-Website-Agent/0.1", joined.get("User-agent", joined.get("user-agent", "")))
            self.assertNotIn("authorization", joined)
            self.assertNotIn("cookie", joined)



class ScoutCliParityTests(unittest.TestCase):
    """The candidate shape Scout produces is what research must accept, without a hand-written shim."""

    def test_scout_candidate_flows_into_live_research(self) -> None:
        from scout.scout_agent import _normalize_candidate, _overpass_candidate, Target

        element = {"type": "node", "id": 7, "tags": {"name": "Pitstop Lagos", "amenity": "restaurant",
                                                    "website": "https://pitstop-lagos.example/",
                                                    "addr:city": "Lagos", "addr:country": "NG",
                                                    "contact:instagram": "https://instagram.com/pitstoplagos"}}
        raw = _overpass_candidate(element)
        self.assertIsNotNone(raw)
        normalized = _normalize_candidate(raw, Target("Nigeria", "Lagos", "Restaurants"))
        self.assertEqual(normalized["originalWebsite"], "https://pitstop-lagos.example/")
        transport = FakeTransport({robots_url(): FakeResponse(ALLOW_ROBOTS), "https://pitstop-lagos.example/": FakeResponse(POOR_PAGE)})
        source = PublicWebsiteResearchSource(enabled_policy(), transport=transport)
        items = source.research(normalized)
        self.assertEqual(len(items), 1)
        self.assertIs(items[0]["website_observation"]["correct_business"], True)


class ResearchCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.candidate_file = self.root / "candidate.json"
        self.candidate_file.write_text(json.dumps(candidate()), encoding="utf-8")
        self.settings = self.root / "settings.json"
        self.addCleanup(self._tmp.cleanup)

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = research_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_default_fixture_path_is_unchanged_and_offline(self) -> None:
        with patch("research.live_source.urlopen") as fake:
            code, stdout, _ = self._run(["--dry-run", "--candidate-file", "tests/fixtures/research_candidate.json",
                                         "--evidence-file", "tests/fixtures/research_evidence.json"])
        self.assertEqual(code, 0)
        fake.assert_not_called()
        document = json.loads(stdout)
        self.assertEqual(document["status"], "completed")
        self.assertEqual(document["record"]["candidate"]["businessName"], "Fictional Beauty by Ada")

    def test_fixture_source_still_requires_its_evidence_file(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self._run(["--dry-run", "--candidate-file", str(self.candidate_file)])
        self.assertEqual(raised.exception.code, 2)

    def test_live_source_cannot_be_enabled_from_the_command_line_alone(self) -> None:
        self.settings.write_text(json.dumps({"research": {"allowLiveResearch": False}}), encoding="utf-8")
        transport = FakeTransport({})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH", None)
            with patch("research.live_source.urlopen", transport):
                with self.assertRaises(SystemExit) as raised:
                    self._run(["--dry-run", "--candidate-file", str(self.candidate_file), "--source", "live",
                               "--settings", str(self.settings)])
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(transport.calls, [])

    def test_permitted_live_run_writes_evidence_and_reports_budget_on_stderr(self) -> None:
        self.settings.write_text(json.dumps({"research": {"allowLiveResearch": True, "respectRobotsTxt": False}}), encoding="utf-8")
        transport = FakeTransport({"https://pitstop-lagos.example/": FakeResponse(POOR_PAGE)})
        output = self.root / "research.json"
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            with patch("research.live_source.urlopen", transport):
                code, _, stderr = self._run(["--dry-run", "--candidate-file", str(self.candidate_file),
                                             "--source", "live", "--settings", str(self.settings),
                                             "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(transport.urls, ["https://pitstop-lagos.example/"])
        self.assertIn("1 of", stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["record"]["research"]["websiteEvaluation"]["status"], "WEBSITE_NEEDS_IMPROVEMENT")
        self.assertEqual(document["research_sources"], ["public_website_live"])

    def test_a_source_that_could_not_read_anything_is_not_reported_as_success(self) -> None:
        self.settings.write_text(json.dumps({"research": {"allowLiveResearch": True, "respectRobotsTxt": False}}), encoding="utf-8")
        transport = FakeTransport({"https://pitstop-lagos.example/": urllib.error.URLError("connection reset")})
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            with patch("research.live_source.urlopen", transport):
                code, stdout, _ = self._run(["--dry-run", "--candidate-file", str(self.candidate_file),
                                             "--source", "live", "--settings", str(self.settings)])
        self.assertEqual(code, 1)
        document = json.loads(stdout)
        self.assertEqual(document["status"], "failed")
        self.assertTrue(document["errors"])

    def test_the_page_budget_can_only_be_narrowed(self) -> None:
        self.settings.write_text(json.dumps({"research": {"allowLiveResearch": True}}), encoding="utf-8")
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            with self.assertRaises(SystemExit) as raised:
                self._run(["--dry-run", "--candidate-file", str(self.candidate_file), "--source", "live",
                           "--settings", str(self.settings), "--max-pages", "9"])
        self.assertEqual(raised.exception.code, 2)


class ResearchJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_path = self.root / "state.json"
        self.jobs = JobService(TrackingStore(self.state_path))
        self.worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output")
        self.settings_dir = self.root / "config"
        self.settings_dir.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def lead_with_candidate(self) -> str:
        store = TrackingStore(self.state_path)
        lead = store.create_lead({
            "business_name": "Pitstop Lagos", "business_type": "Restaurants", "city": "Lagos", "country": "Nigeria",
            "existing_website": "https://pitstop-lagos.example/", "data_classification": "REAL_PUBLIC_DATA",
            "qualification_status": "DISCOVERED", "website_status": "none_known",
            "source_urls": ["https://www.openstreetmap.org/node/123"],
            "discovered_candidate": candidate(),
        })
        return lead["lead_id"]

    def test_job_input_cannot_enable_live_research(self) -> None:
        lead_id = self.lead_with_candidate()
        job = self.jobs.create("RESEARCH", job_id="job-live-off", lead_id=lead_id, input={"live_research": True})
        transport = FakeTransport({})
        with patch("research.live_source.urlopen", transport):
            result = self.jobs.run(job["job_id"], self.worker)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "LIVE_RESEARCH_NOT_ENABLED")
        self.assertIn("AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH", result["next_action"])
        self.assertEqual(transport.calls, [])

    def test_live_research_is_not_run_when_it_was_not_asked_for(self) -> None:
        lead_id = self.lead_with_candidate()
        (self.settings_dir / "system.settings.local.json").write_text(
            json.dumps({"research": {"allowLiveResearch": True, "respectRobotsTxt": False}}), encoding="utf-8")
        transport = FakeTransport({"https://pitstop-lagos.example/": FakeResponse(POOR_PAGE)})
        job = self.jobs.create("RESEARCH", job_id="job-idle-permit", lead_id=lead_id, input={})
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            with patch("research.live_source.urlopen", transport):
                result = self.jobs.run(job["job_id"], self.worker)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "RESEARCH_INPUT_INVALID")
        self.assertEqual(transport.calls, [])

    def test_permitted_live_job_completes_without_touching_any_later_stage(self) -> None:
        lead_id = self.lead_with_candidate()
        (self.settings_dir / "system.settings.local.json").write_text(
            json.dumps({"research": {"allowLiveResearch": True, "respectRobotsTxt": False, "maxPagesPerCandidate": 1}}),
            encoding="utf-8")
        transport = FakeTransport({"https://pitstop-lagos.example/": FakeResponse(POOR_PAGE)})
        job = self.jobs.create("RESEARCH", job_id="job-live-ok", lead_id=lead_id, input={"live_research": True})
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            with patch("research.live_source.urlopen", transport):
                result = self.jobs.run(job["job_id"], self.worker)
        self.assertEqual(result["status"], "COMPLETED", result.get("error"))
        self.assertEqual(result["output"]["live_research"]["requests_made"], 1)
        self.assertEqual(result["next_job_types"], ["QUALIFY"])
        store = TrackingStore(self.state_path)
        lead = store.state["leads"][lead_id]
        self.assertTrue(lead["source_urls"])
        qualification = store.state["qualifications"]["qualification-0001"]
        self.assertIn("https://pitstop-lagos.example/", qualification["evidence"])
        self.assertEqual(qualification["website_status"], "WEBSITE_NEEDS_IMPROVEMENT")
        self.assertEqual(store.state["leads"][lead_id]["website_status"], "WEBSITE_NEEDS_IMPROVEMENT")
        # Ownership of that site is still not asserted anywhere: the scorer's own hedge survives untouched.
        self.assertEqual(qualification["website_ownership"], "WEBSITE_OWNERSHIP_UNCERTAIN")
        self.assertIn("unverified", " ".join(qualification["reasons"]))
        # Research must not be able to start the rest of the funnel by itself.
        self.assertEqual(store.state["projects"], {})
        self.assertEqual(store.state["builds"], {})
        self.assertEqual(store.state["deployments"], {})
        self.assertEqual(store.state["outreach"], {})
        self.assertEqual(store.state.get("approvals", {}), {})

    def test_a_failed_live_run_reports_a_code_instead_of_an_empty_success(self) -> None:
        lead_id = self.lead_with_candidate()
        (self.settings_dir / "system.settings.local.json").write_text(
            json.dumps({"research": {"allowLiveResearch": True, "respectRobotsTxt": True}}), encoding="utf-8")
        transport = FakeTransport({robots_url(): FakeResponse(DISALLOW_ROBOTS)})
        job = self.jobs.create("RESEARCH", job_id="job-live-blocked", lead_id=lead_id, input={"live_research": True})
        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH": "1"}):
            with patch("research.live_source.urlopen", transport):
                result = self.jobs.run(job["job_id"], self.worker)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["output"]["outcome"], "RESEARCH_FAILED")
        self.assertEqual(transport.urls, [robots_url()])
        store = TrackingStore(self.state_path)
        self.assertEqual(store.state["projects"], {})
        self.assertEqual(store.state["builds"], {})
        self.assertEqual(store.state["qualifications"], {})
        self.assertEqual(store.state["deployments"], {})
        # A blocked run must not smuggle a conclusion into the evidence ledger either.
        for entry in store.state["research_evidence"].values():
            self.assertNotIn("pitstop-lagos.example", json.dumps(entry))


if __name__ == "__main__":
    unittest.main()
