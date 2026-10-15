"""Phase 8 (preview-deployment milestone) tests: a provider that cannot be reached says so specifically.

Everything runs against fictional ``TEST_FIXTURE`` state in temporary directories. No provider is called,
nothing is published, and no real workspace file is written.

The defect being pinned is one that appeared while preparing the first real preview: a checkout with no
``.vercel/project.json`` refused the deployment correctly, but the recorded job said only
``WORKER_FAILED`` / "read the recorded worker error", and the same was true when the CLI itself is absent.
An operator following that advice would look for a defect in the build rather than run ``vercel link``, and
no human approval can fix a missing link. The refusal is now classified, kept distinct from an authorization
refusal, and given a copy-pasteable remedy.

The milestone also exposed a second misreport: the review package decided whether the page carried its
disclosure by searching for the *real-business* sentence, so every fictional build - the only kind this
project publishes in preview - was reported as missing a disclosure it actually has. The package now asks
for exactly what the renderer writes, using the renderer's own rule, and says which variant it expected.

Two properties matter and are asserted together:

1. **The code is specific but the failure is still a ``ValueError``** - every existing caller that catches
   ``ValueError`` around the deploy path keeps working, and an authorization refusal never borrows these
   codes (it is about the human decision, not the checkout).
2. **The code survives to the durable record**: the manifest written next to the artifact, the job row, the
   ``next_action`` the operator reads, and the attention list all name the same configuration blocker.

A successful verification path must stay untouched, so the dry run against a linked project is asserted to
carry no denial code at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deployment import authorization as authz
from deployment.deployer import (
    ProviderConfigurationError,
    VercelDeploymentProvider,
    _assert_project_linked,
    _assert_vercel_cli_available,
)
from orchestration.jobs import NEXT_ACTIONS, next_action_for
from tracking.store import TrackingStore

from tests.test_phase41_integration_and_recovery import (
    BUILD_ID,
    HTML,
    PROJECT_ID_JSON,
    PROJECT_NAME,
    VALIDATION_ID,
    _Disposable,
)

SALON_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "qualified_lead_salon.json"

PROVIDER_CONFIGURATION_CODES = {
    "PREVIEW_PROJECT_NOT_LINKED",
    "PREVIEW_PROJECT_LINK_INVALID",
    "PROVIDER_CLI_MISSING",
}


class ProviderConfigurationCodeTests(unittest.TestCase):
    """Each way the checkout can fail to reach the provider gets its own code, and none of them is generic."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _link(self, record: object) -> None:
        directory = self.root / ".vercel"
        directory.mkdir(exist_ok=True)
        (directory / "project.json").write_text(json.dumps(record), encoding="utf-8")

    def test_absent_link_is_named_as_a_missing_preview_project(self) -> None:
        with self.assertRaises(ProviderConfigurationError) as caught:
            _assert_project_linked(self.root, PROJECT_NAME)
        self.assertEqual(caught.exception.error_code, "PREVIEW_PROJECT_NOT_LINKED")
        self.assertIn(".vercel/project.json", str(caught.exception))

    def test_broken_or_wrong_link_records_are_named_as_invalid(self) -> None:
        cases = [
            "not json at all {",                                                   # unreadable
            {"projectName": PROJECT_NAME},                                        # no projectId
            {"projectId": "  ", "projectName": PROJECT_NAME},                     # blank projectId
            {"projectId": "prj_other", "projectName": "some-other-project"},      # wrong project
        ]
        for index, record in enumerate(cases):
            with self.subTest(case=index):
                for path in (self.root / ".vercel" / "project.json",):
                    if path.parent.exists():
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(exist_ok=True)
                    path.write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")
                with self.assertRaises(ProviderConfigurationError) as caught:
                    _assert_project_linked(self.root, PROJECT_NAME)
                self.assertEqual(caught.exception.error_code, "PREVIEW_PROJECT_LINK_INVALID")

    def test_a_valid_link_is_accepted_and_reports_the_project(self) -> None:
        self._link(PROJECT_ID_JSON)
        self.assertEqual(_assert_project_linked(self.root, PROJECT_NAME)["projectId"], PROJECT_ID_JSON["projectId"])

    def test_an_absent_provider_cli_is_a_configuration_error_not_an_authorization_refusal(self) -> None:
        with mock.patch("deployment.deployer.shutil.which", return_value=None):
            with self.assertRaises(ProviderConfigurationError) as caught:
                _assert_vercel_cli_available()
        self.assertEqual(caught.exception.error_code, "PROVIDER_CLI_MISSING")
        self.assertIsInstance(caught.exception, ValueError,
                              "callers that only catch ValueError keep refusing safely")

    def test_every_code_names_a_remedy_the_operator_can_run(self) -> None:
        for code in sorted(PROVIDER_CONFIGURATION_CODES):
            with self.subTest(code=code):
                self.assertIn(code, NEXT_ACTIONS, "a code used in a durable record needs a registered remedy")
                action = next_action_for(code)
                self.assertTrue(action and len(action) > 40, code)
        self.assertIn("vercel link", next_action_for("PREVIEW_PROJECT_NOT_LINKED"))
        self.assertIn("vercel login", next_action_for("PROVIDER_CLI_MISSING"))

    def test_configuration_codes_are_never_authorization_codes(self) -> None:
        # The distinction is the point: an approval cannot resolve these, and a refusal from the approval
        # gate must not be softened into a configuration problem.
        authorization = {code for code in NEXT_ACTIONS if code.startswith(("APPROVAL_", "PUBLISH_"))}
        self.assertTrue(authorization)
        self.assertFalse(PROVIDER_CONFIGURATION_CODES & authorization)


class DeployManifestCarriesTheCodeTests(unittest.TestCase):
    """The refusal is recorded on the artifact it concerns, not only in the raised exception."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build_dir = self.root / "output" / "lead-0001" / BUILD_ID
        self.build_dir.mkdir(parents=True)
        (self.build_dir / "index.html").write_text(HTML, encoding="utf-8")
        (self.build_dir / "build.json").write_text(json.dumps({
            "build_id": BUILD_ID, "lead_id": "lead-0001", "status": "COMPLETED",
            "data_classification": "TEST_FIXTURE", "output_location": str(self.build_dir / "index.html"),
            "validation": {"validation_id": VALIDATION_ID, "build_id": BUILD_ID, "status": "PASS"},
        }), encoding="utf-8")

    def _deploy(self, *, expect_link: bool) -> dict:
        if expect_link:
            (self.root / ".vercel").mkdir(exist_ok=True)
            (self.root / ".vercel" / "project.json").write_text(json.dumps(PROJECT_ID_JSON), encoding="utf-8")
        provider = VercelDeploymentProvider(self.root, runner=lambda *a, **k: None,
                                            expected_project_name=PROJECT_NAME)
        result = provider.deploy(self.build_dir, dry_run=True)
        return json.loads((self.build_dir / "deployment.json").read_text(encoding="utf-8")) | {
            "_status": result.deployment_status, "_metadata": dict(result.metadata),
        }

    def test_unlinked_checkout_records_the_code_and_publishes_nothing(self) -> None:
        recorded = self._deploy(expect_link=False)
        self.assertEqual(recorded["_status"], "FAILED")
        self.assertEqual(recorded["_metadata"]["denial_code"], "PREVIEW_PROJECT_NOT_LINKED")
        self.assertIsNone(recorded.get("deployment_url"))
        self.assertFalse((self.build_dir / authz.ATTEMPTS_FILENAME).exists(),
                         "a configuration refusal never reaches the publish ledger")

    def test_linked_checkout_dry_run_verifies_without_inventing_a_code(self) -> None:
        recorded = self._deploy(expect_link=True)
        self.assertEqual(recorded["_status"], "PENDING", "a verified dry run is still not a deployment")
        self.assertNotIn("denial_code", recorded["_metadata"])
        self.assertEqual(recorded.get("deployment_url"), None)


class PreviewJobRecordingTests(_Disposable):
    """The recorded job - what ``inspect-job``, ``attention``, and an n8n notification all read - is actionable."""

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        env.pop("AI_WEBSITE_AGENT_DATABASE_URL", None)
        env.pop("DATABASE_URL", None)
        env.pop("SUPABASE_DB_URL", None)
        for key in [k for k in env if k.startswith("AI_WEBSITE_AGENT_")]:
            env.pop(key, None)
        return subprocess.run([sys.executable, "-m", "tracking.cli", *argv], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=180)

    def _blocked_attempt(self) -> dict:
        """One approved fictional build, then the real ``prepare-preview`` command against an unlinked checkout."""
        (self.root / ".vercel" / "project.json").unlink()
        build_dir = self.build()
        self.approve(build_dir)
        result = self.run_cli("--state-path", str(self.state_path), "prepare-preview", self.project_id,
                              "--repo-root", str(self.root))
        self.assertEqual(result.returncode, 1, result.stderr)
        return json.loads(result.stdout)

    def test_prepare_preview_records_the_configuration_blocker_not_a_generic_worker_failure(self) -> None:
        payload = self._blocked_attempt()
        self.assertEqual(payload["error_code"], "PREVIEW_PROJECT_NOT_LINKED")
        self.assertIn("vercel link", payload["next_action"])
        self.assertIn(".vercel/project.json", payload["failure"]["message"])
        self.assertNotEqual(payload["error_code"], "WORKER_FAILED",
                            "a missing link is not a worker fault and must not be recorded as one")

        stored = next(iter(json.loads(self.state_path.read_text(encoding="utf-8"))["jobs"].values()))
        self.assertEqual(stored["error_code"], "PREVIEW_PROJECT_NOT_LINKED", "the durable job row keeps the code")
        self.assertIn("vercel link", stored["next_action"])
        self.assertFalse((self.root / "output" / self.lead_id / BUILD_ID / authz.ATTEMPTS_FILENAME).exists(),
                         "a configuration refusal never reaches the publish ledger")

    def test_the_operator_read_path_answers_with_the_same_code(self) -> None:
        payload = self._blocked_attempt()
        # `inspect-job` is the documented follow-up for a failed job; it has to say what to fix.
        inspected = json.loads(self.module("orchestration.cli", "--state-path", str(self.state_path),
                                          "inspect-job", "--job-id", payload["job_id"]).stdout)
        self.assertEqual(inspected["job"]["error_code"], "PREVIEW_PROJECT_NOT_LINKED")
        self.assertIn("vercel link", inspected["job"]["next_action"])
        # The deployment view reports the same root cause, so the two surfaces cannot disagree.
        status = json.loads(self.module("orchestration.cli", "--state-path", str(self.state_path),
                                       "deployment-status").stdout)
        self.assertFalse((status.get("configuration") or {}).get("project_linked", True))


class DisclosureAgreementTests(unittest.TestCase):
    """The review package and the renderer must agree, per classification - strictly, not "any sentence".

    A reviewer decides whether to authorise a preview from this document, so a disclosure check that
    reports "missing" for a page that carries the correct fictional-demo label is a defect in the gate's
    input, not a cosmetic reporting nit.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TrackingStore(self.root / "state.json")

    def _package_for(self, payload: dict, *, business_name: str, classification: str) -> dict:
        from builder.website_builder import WebsiteBuilder

        result = WebsiteBuilder(output_root=self.root / "output").build(payload)
        self.assertEqual(result.status, "COMPLETED", result.errors)
        html = Path(result.output_location)
        digest = __import__("hashlib").sha256(html.read_bytes()).hexdigest()
        candidate = payload.get("candidate") or {}
        location = candidate.get("location") or {}
        lead = self.store.create_lead({
            "business_name": business_name,
            "business_type": candidate.get("niche") or "salon",
            "city": location.get("city") or "Lagos",
            "country": location.get("country") or "Nigeria",
            "website_status": "WEBSITE_POOR",
            "qualification_status": "QUALIFIED",
            "data_classification": classification,
        })
        project = self.store.create_project(lead["lead_id"])
        self.store.record_build(project["project_id"], {
            "build_id": result.build_id, "lead_id": lead["lead_id"], "status": "COMPLETED",
            "template_id": result.template_id, "data_classification": classification,
            "output_location": str(html), "artifact_sha256": digest,
        })
        self.store.record_validation(project["project_id"], {
            "validation_id": f"validation-{result.build_id.removeprefix('build-')}",
            "build_id": result.build_id, "status": "PASS", "score": 100,
        })
        return self.store.build_review_package(project["project_id"], repository_root=self.root)

    def test_fictional_and_real_pages_are_checked_against_their_own_disclosure(self) -> None:
        from tests.test_phase26_real_draft import pitstop_lead_payload
        from templates.components import FICTIONAL_DISCLOSURE

        real = pitstop_lead_payload()
        salon = json.loads(SALON_FIXTURE.read_text(encoding="utf-8"))

        real_pkg = self._package_for(real, business_name="Pitstop Lagos", classification="REAL_PUBLIC_DATA")
        fictional_pkg = self._package_for(salon, business_name="Fictional Glow Studio",
                                          classification="TEST_FIXTURE")
        self.assertEqual(real_pkg["disclosure_variant"], "UNCOMMISSIONED_DRAFT")
        self.assertTrue(real_pkg["disclosure_present_in_html"], real_pkg["expected_disclosure"])
        self.assertIn("Pitstop Lagos", real_pkg["expected_disclosure"])

        self.assertEqual(fictional_pkg["disclosure_variant"], "FICTIONAL_DEMO")
        self.assertTrue(fictional_pkg["disclosure_present_in_html"], fictional_pkg["expected_disclosure"])
        self.assertEqual(fictional_pkg["expected_disclosure"], FICTIONAL_DISCLOSURE)
        self.assertNotEqual(fictional_pkg["artifact_sha256"], real_pkg["artifact_sha256"],
                            "the two classifications really do render different pages")

    def test_a_page_carrying_the_wrong_disclosure_is_still_refused(self) -> None:
        # Accepting "either sentence" would pass a real-business page that was mislabelled as a demo, so
        # the expectation is chosen from the lead's classification and nothing else.
        from tests.test_phase26_real_draft import pitstop_lead_payload
        from templates.components import FICTIONAL_DISCLOSURE

        payload = pitstop_lead_payload()
        package = self._package_for(payload, business_name="Pitstop Lagos", classification="REAL_PUBLIC_DATA")
        html = Path(package["artifact_path"])
        tampered = html.read_text(encoding="utf-8").replace(package["expected_disclosure"], FICTIONAL_DISCLOSURE)
        self.assertNotEqual(tampered, html.read_text(encoding="utf-8"))
        html.write_text(tampered, encoding="utf-8")
        project_id = package["project_id"]
        self.store.state["builds"][package["build_id"]]["artifact_sha256"] = None
        relabelled = self.store.build_review_package(project_id, repository_root=self.root)
        self.assertEqual(relabelled["disclosure_variant"], "UNCOMMISSIONED_DRAFT")
        self.assertFalse(relabelled["disclosure_present_in_html"],
                         "a draft page wearing the fictional label is a finding, not a pass")


if __name__ == "__main__":
    unittest.main()
