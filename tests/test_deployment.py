import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from deployment.deployer import VercelDeploymentProvider, stage_public_payload


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.build = self.root / "output" / "fictional-lead" / "build-001"
        self.build.mkdir(parents=True)
        (self.build / "index.html").write_text("<title>Fictional Studio</title>", encoding="utf-8")
        self.manifest = {"lead_id": "fictional-lead", "build_id": "build-001", "output_location": str(self.build / "index.html"), "validation": {"validation_id": "validation-001", "status": "PASS", "passed": True}}
        self._write_manifest()
        self.approved = False

    def approve_build(self, **overrides):
        """Record the build-local approval capability a real publish requires (deployment/authorization.py)."""
        from deployment.authorization import write_approval

        record = {
            "project_id": "proj-0001",
            "lead_id": self.manifest["lead_id"],
            "build_id": self.manifest["build_id"],
            "validation_id": self.manifest["validation"]["validation_id"],
            "review_id": "review-0001",
            "decision": "APPROVED_FOR_PREVIEW",
            "action": "PREVIEW_DEPLOYMENT",
            "reviewer": {"reviewer_type": "HUMAN", "reviewer_id": "operator:fixture", "identity_verified": True},
            "notes": "fixture approval so a real publish is authorized",
        }
        record.update(overrides)
        write_approval(self.build, record)
        self.approved = True

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_manifest(self):
        (self.build / "build.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def _link_project(self, project_name="ai-website-agent-preview"):
        linked = self.root / ".vercel"
        linked.mkdir(exist_ok=True)
        (linked / "project.json").write_text(json.dumps({"projectId": "prj_fixture", "orgId": "team_fixture", "projectName": project_name}), encoding="utf-8")

    def test_passed_build_is_allowed_in_dry_run(self):
        self._link_project()
        result = VercelDeploymentProvider(self.root).deploy(self.build)
        self.assertEqual(result.deployment_status, "PENDING")
        self.assertTrue((self.build / "deployment.json").is_file())

    def test_fail_validation_is_blocked(self):
        self._link_project(); self.manifest["validation"]["status"] = "FAIL"; self._write_manifest()
        result = VercelDeploymentProvider(self.root).deploy(self.build)
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertIn("validation status is FAIL", result.error)

    def test_needs_review_is_blocked_unless_explicitly_approved(self):
        self._link_project(); self.manifest["validation"]["status"] = "NEEDS_REVIEW"; self._write_manifest()
        blocked = VercelDeploymentProvider(self.root).deploy(self.build)
        approved = VercelDeploymentProvider(self.root).deploy(self.build, approve_needs_review=True)
        self.assertEqual(blocked.deployment_status, "NEEDS_REVIEW")
        self.assertEqual(approved.deployment_status, "PENDING")

    def test_missing_or_wrong_build_association_fails(self):
        self._link_project(); self.manifest["output_location"] = str(self.root / "wrong" / "index.html"); self._write_manifest()
        result = VercelDeploymentProvider(self.root).deploy(self.build)
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertIn("does not match", result.error)

    def test_missing_project_link_fails_without_cli_attempt(self):
        called = []
        def runner(*args, **kwargs):
            called.append(args); return subprocess.CompletedProcess(args[0], 0, "", "")
        result = VercelDeploymentProvider(self.root, runner).deploy(self.build)
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertIn("No linked Vercel project", result.error)
        self.assertEqual(called, [])

    def test_cli_failure_becomes_failed_with_redacted_error(self):
        self._link_project(); self.approve_build()
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 1, "", "token=super-secret-value")
        result = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertIn("[REDACTED]", result.error)
        self.assertNotIn("super-secret-value", result.error)

    def test_cli_success_normalizes_https_preview_url(self):
        self._link_project(); self.approve_build()
        def runner(*args, **kwargs):
            self.assertNotIn("--prod", args[0])
            return subprocess.CompletedProcess(args[0], 0, "https://fictional-studio-git-preview.vercel.app\n", "")
        result = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
        self.assertEqual(result.deployment_status, "DEPLOYED")
        self.assertEqual(result.deployment_url, "https://fictional-studio-git-preview.vercel.app")
        self.assertEqual(result.vercel_project_id, "prj_fixture")

    def test_unexpected_or_production_like_url_is_rejected(self):
        self._link_project(); self.approve_build()
        for url in ("https://vercel.app", "https://fictional.vercel.app.example.invalid", "http://fictional.vercel.app"):
            with self.subTest(url=url):
                def runner(*args, **kwargs):
                    return subprocess.CompletedProcess(args[0], 0, url, "")
                result = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
                self.assertEqual(result.deployment_status, "FAILED")
                self.assertIsNone(result.deployment_url)
                self.assertIn("did not return an HTTPS preview URL", result.error or "")

    def test_missing_authentication_fails_without_a_fabricated_url(self):
        self._link_project(); self.approve_build()
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 1, "", "Error: Not authenticated")
        result = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertIsNone(result.deployment_url)
        self.assertIn("Not authenticated", result.error or "")

    def test_staging_copies_only_html_and_referenced_local_assets(self):
        (self.build / "assets").mkdir()
        (self.build / "assets" / "site.css").write_text(".hero { background: url('logo.svg'); }", encoding="utf-8")
        (self.build / "assets" / "logo.svg").write_text("<svg></svg>", encoding="utf-8")
        (self.build / "index.html").write_text('<link rel="stylesheet" href="assets/site.css"><title>Fictional Studio</title>', encoding="utf-8")
        (self.build / "deployment.json").write_text('{"internal": true}', encoding="utf-8")
        (self.build / "unrelated-fixture.txt").write_text("not public", encoding="utf-8")
        staging = self.root / "staging"
        payload = stage_public_payload(self.build, staging)
        self.assertEqual(payload.files, ("assets/logo.svg", "assets/site.css", "index.html"))
        self.assertFalse((staging / "build.json").exists())
        self.assertFalse((staging / "deployment.json").exists())
        self.assertFalse((staging / "unrelated-fixture.txt").exists())

    def test_missing_or_unsafe_assets_fail_closed(self):
        (self.build / "index.html").write_text('<img src="assets/missing.png">', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Required local asset is missing"):
            stage_public_payload(self.build, self.root / "staging-missing")
        (self.build / "index.html").write_text('<script src="../outside.js"></script>', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe asset path"):
            stage_public_payload(self.build, self.root / "staging-traversal")

    def test_unsafe_symlink_and_secret_payload_are_rejected(self):
        assets = self.build / "assets"; assets.mkdir()
        outside = self.root / "outside.js"; outside.write_text("console.log('outside')", encoding="utf-8")
        link = assets / "outside.js"
        symlink_created = True
        try:
            os.symlink(outside, link)
        except OSError:
            symlink_created = False
        (self.build / "index.html").write_text('<script src="assets/outside.js"></script>', encoding="utf-8")
        if symlink_created:
            with self.assertRaisesRegex(ValueError, "escapes the build directory"):
                stage_public_payload(self.build, self.root / "staging-symlink")
        else:
            from unittest.mock import patch
            real_resolve = Path.resolve
            resolved_outside = real_resolve(outside)
            def fake_resolve(p, *args, **kwargs):
                if p.name == "outside.js":
                    return resolved_outside
                return real_resolve(p, *args, **kwargs)
            with patch.object(Path, "resolve", fake_resolve):
                with self.assertRaisesRegex(ValueError, "escapes the build directory"):
                    stage_public_payload(self.build, self.root / "staging-symlink")
        (self.build / "index.html").write_text('<script src="assets/key.js"></script>', encoding="utf-8")
        (assets / "key.js").write_text('const token="very-secret-value"', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Potential secret-like value"):
            stage_public_payload(self.build, self.root / "staging-secret")

    def test_church_link_is_refused_and_dedicated_project_is_targeted(self):
        self._link_project("grace-light-community-church"); self.approve_build()
        called = []
        def runner(*args, **kwargs):
            called.append(args[0]); return subprocess.CompletedProcess(args[0], 0, "https://fixture.vercel.app", "")
        blocked = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
        self.assertEqual(blocked.deployment_status, "FAILED")
        self.assertIn("not the dedicated preview project", blocked.error or "")
        self.assertEqual(called, [])
        (self.root / ".vercel" / "project.json").unlink()
        self._link_project()
        deployed = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
        self.assertEqual(deployed.deployment_status, "DEPLOYED")
        self.assertIn("--project", called[-1])
        self.assertIn("prj_fixture", called[-1])
        self.assertNotIn("--prod", called[-1])

    def test_real_business_publish_without_verified_project_id_is_refused(self):
        self._link_project("fictional-studio-preview")
        self.approve_build()
        manifest_path = self.build / "build.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data_classification"] = "REAL_PUBLIC_DATA"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        called = []
        def runner(*args, **kwargs):
            called.append(args[0])
            return subprocess.CompletedProcess(args[0], 0, "https://fictional-studio-preview.vercel.app", "")
        result = VercelDeploymentProvider(self.root, runner).deploy(
            self.build, dry_run=False, project_name="fictional-studio-preview",
            approve_real_preview=True,
        )
        self.assertEqual(result.deployment_status, "NEEDS_REVIEW")
        self.assertIn("verified Vercel project ID", result.error or "")
        self.assertEqual(called, [])

    def test_real_publish_without_human_approval_is_refused_before_any_cli_call(self):
        self._link_project()
        called = []
        def runner(*args, **kwargs):
            called.append(args[0]); return subprocess.CompletedProcess(args[0], 0, "https://fixture.vercel.app", "")
        self.assertFalse(self.approved)
        refused = VercelDeploymentProvider(self.root, runner).deploy(self.build, dry_run=False)
        self.assertEqual(refused.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_MISSING", refused.error or "")
        self.assertIsNone(refused.deployment_url)
        self.assertEqual(refused.metadata.get("denial_code"), "APPROVAL_MISSING")
        self.assertEqual(called, [], "a refused approval must not reach the provider CLI")
        self.assertNotIn("DEPLOYING", (self.build / "deployment-attempts.json").read_text(encoding="utf-8"))

    def test_approval_for_a_stale_artifact_is_refused(self):
        self._link_project(); self.approve_build()
        (self.build / "index.html").write_text("<title>Edited after approval</title>", encoding="utf-8")
        result = VercelDeploymentProvider(self.root).deploy(self.build, dry_run=False)
        self.assertEqual(result.deployment_status, "NEEDS_REVIEW")
        self.assertIn("APPROVAL_ARTIFACT_STALE", result.error or "")

    def test_repeat_publish_of_the_same_approved_artifact_is_refused(self):
        self._link_project(); self.approve_build()
        calls = []
        def runner(*args, **kwargs):
            calls.append(args[0]); return subprocess.CompletedProcess(args[0], 0, "https://fictional-studio-git-preview.vercel.app\n", "")
        provider = VercelDeploymentProvider(self.root, runner)
        first = provider.deploy(self.build, dry_run=False)
        second = provider.deploy(self.build, dry_run=False)
        self.assertEqual(first.deployment_status, "DEPLOYED")
        self.assertEqual(second.deployment_status, "FAILED")
        self.assertIn("DEPLOYMENT_ALREADY_RECORDED", second.error or "")
        self.assertEqual(len(calls), 1)


    def test_api_bound_project_deploys_without_workspace_link_and_passes_exact_team(self):
        self.approve_build()
        client = type("FakeVercelClient", (), {
            "token": "fixture-token",
            "team_id": "team_fixture",
            "get_project": lambda self, project_id: {
                "id": project_id, "name": "fictional-studio-preview",
            },
            "get_deployment_by_url": lambda self, url: {
                "id": "dpl_fixture123",
                "url": "fictional-studio-preview-abc.vercel.app",
                "project_id": "prj_managed123",
                "ready_state": "READY",
            },
        })()
        calls = []
        def runner(*args, **kwargs):
            calls.append((args[0], kwargs))
            return subprocess.CompletedProcess(args[0], 0, "https://fictional-studio-preview-abc.vercel.app\n", "")
        from unittest.mock import patch
        with patch("deployment.site_management.VercelProjectsClient", return_value=client):
            result = VercelDeploymentProvider(self.root, runner).deploy(
                self.build, dry_run=False, project_name="fictional-studio-preview",
                vercel_project_id="prj_managed123",
            )
        self.assertEqual(result.deployment_status, "DEPLOYED")
        self.assertEqual(result.vercel_project_id, "prj_managed123")
        self.assertEqual(result.vercel_deployment_id, "dpl_fixture123")
        self.assertEqual(result.metadata["provider_verification"]["status"], "VERIFIED")
        self.assertEqual(result.metadata["project_name"], "fictional-studio-preview")
        command, kwargs = calls[0]
        self.assertIn("--project", command)
        self.assertEqual(command[command.index("--project") + 1], "prj_managed123")
        self.assertEqual(kwargs["env"]["VERCEL_TOKEN"], "fixture-token")
        self.assertEqual(kwargs["env"]["VERCEL_ORG_ID"], "team_fixture")
        self.assertEqual(kwargs["env"]["VERCEL_PROJECT_ID"], "prj_managed123")
        self.assertNotIn("fixture-token", " ".join(command))
        self.assertFalse((self.root / ".vercel" / "project.json").exists())

    def test_api_bound_publish_is_not_marked_deployed_when_readback_targets_another_project(self):
        self.approve_build()
        target_url = "https://fictional-studio-preview-abc.vercel.app"
        client = type("FakeVercelClient", (), {
            "token": "fixture-token",
            "team_id": "team_fixture",
            "get_project": lambda self, project_id: {
                "id": project_id, "name": "fictional-studio-preview",
            },
            "get_deployment_by_url": lambda self, url: {
                "id": "dpl_wrongtarget123",
                "url": "fictional-studio-preview-abc.vercel.app",
                "project_id": "prj_someoneelse",
                "ready_state": "READY",
            },
        })()
        calls = []
        def runner(*args, **kwargs):
            calls.append((args[0], kwargs))
            return subprocess.CompletedProcess(args[0], 0, target_url + "\\n", "")
        from unittest.mock import patch
        with patch("deployment.site_management.VercelProjectsClient", return_value=client):
            result = VercelDeploymentProvider(self.root, runner).deploy(
                self.build, dry_run=False, project_name="fictional-studio-preview",
                vercel_project_id="prj_managed123",
            )
        self.assertEqual(result.deployment_status, "PUBLISH_UNCERTAIN")
        self.assertEqual(result.deployment_url, target_url)
        self.assertIsNone(result.vercel_deployment_id)
        self.assertEqual(result.metadata["provider_verification"]["status"], "UNVERIFIED")
        attempts = json.loads((self.build / "deployment-attempts.json").read_text(encoding="utf-8"))
        self.assertEqual(attempts[-1]["deployment_status"], "PUBLISH_UNCERTAIN")
        self.assertEqual(len(calls), 1, "an unverified provider result must not be auto-published again")

    def test_api_bound_project_name_mismatch_is_refused_before_cli_call(self):
        self.approve_build()
        client = type("FakeVercelClient", (), {
            "token": "fixture-token",
            "team_id": "team_fixture",
            "get_project": lambda self, project_id: {
                "id": project_id, "name": "someone-elses-preview",
            },
        })()
        calls = []
        def runner(*args, **kwargs):
            calls.append(args[0])
            return subprocess.CompletedProcess(args[0], 0, "https://fixture.vercel.app", "")
        from unittest.mock import patch
        with patch("deployment.site_management.VercelProjectsClient", return_value=client):
            result = VercelDeploymentProvider(self.root, runner).deploy(
                self.build, dry_run=False, project_name="fictional-studio-preview",
                vercel_project_id="prj_managed123",
            )
        self.assertEqual(result.deployment_status, "FAILED")
        self.assertEqual(result.metadata.get("denial_code"), "VERCEL_PROJECT_BINDING_MISMATCH")
        self.assertEqual(calls, [])

if __name__ == "__main__":
    unittest.main()
