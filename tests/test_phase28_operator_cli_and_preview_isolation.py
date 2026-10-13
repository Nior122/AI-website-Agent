"""Phase 28 tests for tracking/cli.py, safe operator review confirmation, and isolated preview target configuration."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from builder.website_builder import WebsiteBuilder
from deployment.cli import main as deployment_cli_main
from deployment.deployer import VercelDeploymentProvider
from tests.test_phase26_real_draft import pitstop_lead_payload
from tracking.cli import confirmation_token, run_cli
from tracking.store import TrackingStore


class Phase28OperatorCliAndPreviewIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"
        self.store = TrackingStore(self.state_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(self, args: list[str], *, stdin_text: str = "") -> tuple[int, dict | None, dict | None, str]:
        in_buf = io.StringIO(stdin_text)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        code = run_cli(["--state-path", str(self.state_path), *args], stdin=in_buf, stdout=out_buf, stderr=err_buf)
        out_raw = out_buf.getvalue().strip()
        err_raw = err_buf.getvalue().strip()
        out_json = json.loads(out_raw) if out_raw.startswith("{") else None
        err_json = None
        if err_raw:
            last_line = err_raw.splitlines()[-1]
            if last_line.startswith("{"):
                err_json = json.loads(last_line)
        return code, out_json, err_json, err_raw

    def _seed_two_projects(self) -> tuple[dict, dict, dict, dict]:
        lead1 = self.store.create_lead({
            "business_name": "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS",
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 800 000 0099",
            "source_urls": ["https://fixture.example.invalid/lagos-fresh-kitchen"],
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        proj1 = self.store.create_project(lead1["lead_id"])
        self.store.record_build(proj1["project_id"], {
            "build_id": "build-fictional-001",
            "lead_id": lead1["lead_id"],
            "status": "COMPLETED",
            "data_classification": "TEST_FIXTURE",
        })
        self.store.record_validation(proj1["project_id"], {
            "validation_id": "val-fictional-001",
            "build_id": "build-fictional-001",
            "status": "PASS",
        })
        self.store.record_review(proj1["project_id"], "PENDING", notes="Initial fictional review state.")

        lead2 = self.store.create_lead({
            "business_name": "Pitstop Lagos",
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 807 749 8324",
            "source_urls": ["https://pitstoplagos.com/", "https://www.instagram.com/pitstoplagos/"],
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        proj2 = self.store.create_project(lead2["lead_id"])
        self.store.record_build(proj2["project_id"], {
            "build_id": "build-pitstop-001",
            "lead_id": lead2["lead_id"],
            "status": "COMPLETED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        self.store.record_validation(proj2["project_id"], {
            "validation_id": "val-pitstop-001",
            "build_id": "build-pitstop-001",
            "status": "PASS",
        })
        self.store.record_review(proj2["project_id"], "PENDING", notes="Initial real-business draft pending review.")
        return lead1, proj1, lead2, proj2

    def test_1_project_status_and_snapshots(self) -> None:
        self._seed_two_projects()
        code, status_out, _, _ = self._run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(status_out["project_count"], 2)
        p2_summary = next(item for item in status_out["projects"] if item["project_id"] == "project-0002")
        self.assertEqual(p2_summary["business_name"], "Pitstop Lagos")
        self.assertEqual(p2_summary["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(p2_summary["assigned_build_id"], "build-pitstop-001")
        self.assertEqual(p2_summary["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(p2_summary["review_status"], "PENDING")
        self.assertIsNone(p2_summary["deployment_status"])

        code_snap, snap_out, _, _ = self._run_cli(["snapshot", "project-0002"])
        self.assertEqual(code_snap, 0)
        self.assertEqual(snap_out["project_id"], "project-0002")
        self.assertEqual(snap_out["lead_id"], "lead-0002")
        self.assertEqual(snap_out["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(snap_out["assigned_build_id"], "build-pitstop-001")
        self.assertEqual(snap_out["latestValidation"]["validation_id"], "val-pitstop-001")
        self.assertIsNone(snap_out["latestDeployment"])

    def test_2_review_history_ordering_and_contents(self) -> None:
        _, _, _, proj2 = self._seed_two_projects()
        token = confirmation_token("project-0002", "build-pitstop-001", "CHANGES_REQUESTED")
        code, _, _, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "CHANGES_REQUESTED",
            "--notes",
            "Refine the offerings section copy",
            "--confirm",
            token,
        ])
        self.assertEqual(code, 0)

        # Inject a legacy-formatted entry (with only status and reviewed_at) to verify backward compatibility
        reloaded = TrackingStore(self.state_path)
        reloaded.state["projects"][proj2["project_id"]]["review_history"].insert(0, {
            "review_id": "review-legacy",
            "project_id": "project-0002",
            "lead_id": "lead-0002",
            "build_id": "build-pitstop-000",
            "status": "PENDING",
            "review_notes": "Legacy entry",
            "reviewed_at": "2026-10-12T00:00:00Z",
        })
        reloaded._save()

        code_hist, hist_out, _, _ = self._run_cli(["history", "project-0002"])
        self.assertEqual(code_hist, 0)
        self.assertEqual(hist_out["review_count"], 3)
        history = hist_out["review_history"]
        timestamps = [entry["timestamp"] for entry in history]
        self.assertEqual(timestamps, sorted(timestamps))
        for entry in history:
            for required_key in ("project_id", "lead_id", "build_id", "decision", "notes", "timestamp"):
                self.assertIn(required_key, entry)
                self.assertIsNotNone(entry[required_key])
        self.assertEqual(history[0]["decision"], "PENDING")
        self.assertEqual(history[-1]["decision"], "CHANGES_REQUESTED")
        self.assertEqual(history[-1]["notes"], "Refine the offerings section copy")

    def test_3_invalid_project_ids_and_decisions(self) -> None:
        self._seed_two_projects()
        code_bad_proj, _, err_proj, _ = self._run_cli(["snapshot", "project-9999"])
        self.assertEqual(code_bad_proj, 1)
        self.assertIn("Unknown project: project-9999", err_proj["error"])

        code_bad_hist, _, err_hist, _ = self._run_cli(["history", "project-9999"])
        self.assertEqual(code_bad_hist, 1)
        self.assertIn("Unknown project: project-9999", err_hist["error"])

        code_bad_dec, _, err_dec, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "AUTO_APPROVE",
            "--notes",
            "Invalid decision test",
        ])
        self.assertEqual(code_bad_dec, 1)
        self.assertIn("Unsupported review decision", err_dec["error"])

        code_empty_notes, _, err_notes, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "PENDING",
            "--notes",
            "  ",
        ])
        self.assertEqual(code_empty_notes, 1)
        self.assertIn("meaningful --notes", err_notes["error"])

    def test_4_missing_or_mismatched_build_validation_or_deployment_prevents_approval(self) -> None:
        self._seed_two_projects()
        token = confirmation_token("project-0002", "build-pitstop-001", "APPROVED")

        # Undeployed project at READY_FOR_REVIEW cannot be approved even with valid confirmation token
        code_undeployed, _, err_undep, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "APPROVED",
            "--notes",
            "Attempt approval without preview deployment",
            "--confirm",
            token,
        ])
        self.assertEqual(code_undeployed, 1)
        self.assertIn("Approval requires a deployed preview", err_undep["error"])
        self.assertEqual(TrackingStore(self.state_path).snapshot("project-0002")["project"]["review_status"], "PENDING")

        # Tamper with validation status to FAIL and verify approval is blocked before deployment check
        tampered = TrackingStore(self.state_path)
        tampered.state["validations"]["val-pitstop-001"]["status"] = "FAIL"
        tampered._save()
        code_bad_val, _, err_val, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "APPROVED",
            "--notes",
            "Attempt approval with failed validation",
            "--confirm",
            token,
        ])
        self.assertEqual(code_bad_val, 1)
        self.assertIn("PASS validation matching the assigned build", err_val["error"])

    def test_5_rejected_and_changes_requested_drafts(self) -> None:
        self._seed_two_projects()
        reject_token = confirmation_token("project-0002", "build-pitstop-001", "REJECTED")
        code_rej, rej_out, _, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "REJECTED",
            "--notes",
            "The draft is not suitable yet",
            "--confirm",
            reject_token,
        ])
        self.assertEqual(code_rej, 0)
        self.assertEqual(rej_out["current_stage"], "BUILDING")
        self.assertEqual(rej_out["review_status"], "REJECTED")

        # Record a second build and request changes via interactive stdin confirmation
        store = TrackingStore(self.state_path)
        store.record_build("project-0002", {
            "build_id": "build-pitstop-002",
            "lead_id": "lead-0002",
            "status": "COMPLETED",
            "data_classification": "REAL_PUBLIC_DATA",
        })
        store.record_validation("project-0002", {
            "validation_id": "val-pitstop-002",
            "build_id": "build-pitstop-002",
            "status": "PASS",
        })
        changes_token = confirmation_token("project-0002", "build-pitstop-002", "CHANGES_REQUESTED")
        code_chg, chg_out, _, err_prompt = self._run_cli(
            [
                "review",
                "project-0002",
                "--decision",
                "CHANGES_REQUESTED",
                "--notes",
                "Improve the service descriptions",
            ],
            stdin_text=changes_token + "\n",
        )
        self.assertEqual(code_chg, 0)
        self.assertIn("build=build-pitstop-002", err_prompt)
        self.assertEqual(chg_out["current_stage"], "BUILDING")
        self.assertEqual(chg_out["review_status"], "CHANGES_REQUESTED")

    def test_6_confirmation_required_for_consequential_review_decisions(self) -> None:
        self._seed_two_projects()
        # Missing confirmation (empty stdin and no --confirm) must abort without changing state
        code_no_conf, _, err_no_conf, _ = self._run_cli(
            [
                "review",
                "project-0002",
                "--decision",
                "REJECTED",
                "--notes",
                "Attempt without confirmation",
            ],
            stdin_text="",
        )
        self.assertEqual(code_no_conf, 1)
        self.assertIn("Confirmation required", err_no_conf["error"])
        self.assertEqual(TrackingStore(self.state_path).snapshot("project-0002")["project"]["review_status"], "PENDING")

        # Wrong build_id in --confirm token must abort without changing state
        wrong_token = confirmation_token("project-0002", "build-wrong-999", "REJECTED")
        code_wrong, _, err_wrong, _ = self._run_cli([
            "review",
            "project-0002",
            "--decision",
            "REJECTED",
            "--notes",
            "Attempt with wrong build token",
            "--confirm",
            wrong_token,
        ])
        self.assertEqual(code_wrong, 1)
        self.assertIn("Confirmation required", err_wrong["error"])
        self.assertEqual(TrackingStore(self.state_path).snapshot("project-0002")["project"]["review_status"], "PENDING")

    def test_7_review_isolation_between_projects(self) -> None:
        self._seed_two_projects()
        # Deploy and approve project-0001 (fictional) and verify project-0002 (Pitstop Lagos) is completely untouched
        store = TrackingStore(self.state_path)
        store.record_deployment("project-0001", {
            "deployment_id": "dep-fictional-001",
            "build_id": "build-fictional-001",
            "validation_id": "val-fictional-001",
            "validation_status": "PASS",
            "deployment_status": "DEPLOYED",
            "deployment_url": "https://ai-website-agent-preview.vercel.app",
        })
        p1_token = confirmation_token("project-0001", "build-fictional-001", "APPROVED")
        code_p1, p1_out, _, _ = self._run_cli([
            "review",
            "project-0001",
            "--decision",
            "APPROVED",
            "--notes",
            "Approved fictional demonstration preview",
            "--confirm",
            p1_token,
        ])
        self.assertEqual(code_p1, 0)
        self.assertEqual(p1_out["review_status"], "APPROVED")
        self.assertEqual(p1_out["current_stage"], "READY_FOR_OUTREACH")

        # Verify project-0002 remains at READY_FOR_REVIEW and PENDING
        _, p2_snap, _, _ = self._run_cli(["snapshot", "project-0002"])
        self.assertEqual(p2_snap["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(p2_snap["review_status"], "PENDING")
        self.assertEqual(len(p2_snap["project"]["review_history"]), 1)

    def test_8_workspace_recovery_without_fabricating_records(self) -> None:
        empty_repo = self.root / "empty_repo"
        empty_repo.mkdir(parents=True)
        (empty_repo / "tests").mkdir()
        code_ws, ws_out, _, _ = self._run_cli(["check-workspace", "--repo-root", str(empty_repo)])
        self.assertEqual(code_ws, 0)
        self.assertEqual(ws_out["status"], "MISSING_LOCAL_STATE")
        self.assertFalse(ws_out["state_file_exists"])

        # Calling recover_local_state when no durable lead source exists must not fabricate artifacts or records
        self._seed_two_projects()
        store = TrackingStore(self.state_path)
        store.state["builds"]["build-pitstop-001"]["output_location"] = str(empty_repo / "output" / "lead-0002" / "build-pitstop-001" / "index.html")
        store._save()
        report = store.recover_local_state(repository_root=empty_repo)
        self.assertEqual(report["status"], "MISSING_LOCAL_STATE")
        self.assertEqual(report["recovered_builds"], [])
        self.assertIn("project-0002", report["unrecoverable_projects"])
        self.assertFalse((empty_repo / "output" / "lead-0002" / "build-pitstop-001" / "index.html").exists())

    def test_9_isolated_preview_target_configuration(self) -> None:
        self._seed_two_projects()
        code_cfg, cfg_out, _, _ = self._run_cli([
            "configure-preview",
            "project-0002",
            "--project-name",
            "pitstop-lagos-preview",
        ])
        self.assertEqual(code_cfg, 0)
        self.assertEqual(cfg_out["isolated_preview_project"], "pitstop-lagos-preview")
        self.assertEqual(cfg_out["review_status"], "PENDING")

        # Verify deployment provider accepts an explicitly authorized isolated preview target in dry-run
        builder = WebsiteBuilder(output_root=self.root / "output")
        real_build = builder.build(pitstop_lead_payload())
        build_dir = Path(real_build.output_location).parent
        vercel_dir = self.root / ".vercel"
        vercel_dir.mkdir(exist_ok=True)
        (vercel_dir / "project.json").write_text(
            json.dumps({"projectId": "prj_isolated_pitstop", "orgId": "team_1", "projectName": "pitstop-lagos-preview"}),
            encoding="utf-8",
        )
        res = VercelDeploymentProvider(self.root).deploy(
            build_dir,
            dry_run=True,
            approve_real_preview=True,
            project_name="pitstop-lagos-preview",
        )
        self.assertEqual(res.deployment_status, "PENDING")
        self.assertEqual(res.vercel_project_id, "prj_isolated_pitstop")
        self.assertEqual(res.metadata["project_name"], "pitstop-lagos-preview")

    def test_10_shared_fictional_preview_protection(self) -> None:
        self._seed_two_projects()
        # configure-preview rejects assigning ai-website-agent-preview to a real-business project
        code_shared, _, err_shared, _ = self._run_cli([
            "configure-preview",
            "project-0002",
            "--project-name",
            "ai-website-agent-preview",
        ])
        self.assertEqual(code_shared, 1)
        self.assertIn("cannot use the shared fictional preview project", err_shared["error"])

        # deployment.cli blocks real-business build by default (without --approve-real-preview)
        builder = WebsiteBuilder(output_root=self.root / "output")
        real_build = builder.build(pitstop_lead_payload())
        build_dir = Path(real_build.output_location).parent
        rc_default = deployment_cli_main(["--build", str(build_dir)])
        self.assertEqual(rc_default, 1)
        dep_manifest = json.loads((build_dir / "deployment.json").read_text(encoding="utf-8"))
        self.assertEqual(dep_manifest["deployment_status"], "NEEDS_REVIEW")

        # Even with --approve-real-preview, omitting --project-name or passing ai-website-agent-preview fails closed
        rc_fallback = deployment_cli_main(["--build", str(build_dir), "--approve-real-preview"])
        self.assertEqual(rc_fallback, 1)
        dep_fallback = json.loads((build_dir / "deployment.json").read_text(encoding="utf-8"))
        self.assertEqual(dep_fallback["deployment_status"], "FAILED")
        self.assertIn("cannot use or fall back to the shared fictional preview project", dep_fallback["error"])

    def test_11_production_deployment_rejection(self) -> None:
        self._seed_two_projects()
        for bad_target in ("pitstop-lagos-prod", "pitstop-lagos-production", "pitstop-lagos-live", "pitstop-lagos-site"):
            code_bad, _, err_bad, _ = self._run_cli([
                "configure-preview",
                "project-0002",
                "--project-name",
                bad_target,
            ])
            self.assertEqual(code_bad, 1, f"Expected {bad_target} to be rejected")
            self.assertIn("Refusing preview target", err_bad["error"])

        builder = WebsiteBuilder(output_root=self.root / "output")
        real_build = builder.build(pitstop_lead_payload())
        build_dir = Path(real_build.output_location).parent
        res_prod = VercelDeploymentProvider(self.root).deploy(
            build_dir,
            dry_run=True,
            approve_real_preview=True,
            project_name="pitstop-lagos-prod-preview",
        )
        self.assertEqual(res_prod.deployment_status, "FAILED")
        self.assertIn("production target", res_prod.error or "")


if __name__ == "__main__":
    unittest.main()
