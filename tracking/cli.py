"""Safe operator-facing CLI for inspecting tracked projects, review history, and explicit review decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import TextIO

from .database import DatabasePersistenceError, MIGRATION_0001_PATH, MIGRATION_VERSION, RelationalStateBackend, configured_database_url
from .store import DEFAULT_STATE_PATH, PERMITTED_REVIEW_ACTIONS, REVIEW_DECISIONS, TrackingStore


CONSEQUENTIAL_DECISIONS = {"APPROVED_FOR_PREVIEW", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}


def confirmation_token(
    project_id: str,
    build_id: str | None,
    decision: str,
    action: str | None = None,
) -> str:
    norm_decision = str(decision).strip().upper()
    norm_action = str(action).strip().upper() if action else None
    if norm_decision == "APPROVED" and norm_action == "PREVIEW_DEPLOYMENT":
        norm_decision = "APPROVED_FOR_PREVIEW"
    base = f"{project_id}:{build_id or 'no-build'}:{norm_decision}"
    return f"{base}:{norm_action}" if norm_action else base


def _project_review_context(
    store: TrackingStore,
    project_id: str,
    decision: str,
    *,
    action: str | None = None,
    build_id: str | None = None,
) -> dict:
    snap = store.snapshot(project_id)
    proj = snap["project"]
    lead = snap["lead"]
    build = snap["latestBuild"]
    val = snap["latestValidation"]
    dep = snap["latestDeployment"]
    assigned_build_id = proj.get("assigned_build_id")
    if build_id is not None and build_id != assigned_build_id:
        raise ValueError(
            f"Review build_id '{build_id}' does not match project's assigned_build_id '{assigned_build_id}'."
        )
    return {
        "project_id": project_id,
        "lead_id": lead["lead_id"],
        "business_name": lead.get("business_name"),
        "data_classification": lead.get("data_classification") or proj.get("data_classification"),
        "current_stage": proj.get("current_stage"),
        "workflow_state": snap["workflow_state"],
        "current_review_status": proj.get("review_status"),
        "assigned_build_id": assigned_build_id,
        "build_status": build.get("status") if build else None,
        "validation_id": val.get("validation_id") if val else None,
        "validation_status": val.get("status") if val else None,
        "deployment_id": dep.get("deployment_id") if dep else None,
        "deployment_status": dep.get("deployment_status") if dep else None,
        "requested_decision": decision,
        "requested_action": action,
        "required_confirmation": confirmation_token(project_id, assigned_build_id, decision, action=action),
    }


def _validate_review_preconditions(
    store: TrackingStore,
    project_id: str,
    decision: str,
    notes: str | None,
    *,
    action: str | None = None,
    build_id: str | None = None,
) -> dict:
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"Unsupported review decision '{decision}'. Choose from: {', '.join(sorted(REVIEW_DECISIONS))}.")
    if action is not None and action not in PERMITTED_REVIEW_ACTIONS:
        raise ValueError(f"Unsupported review action '{action}'. Choose from: {', '.join(sorted(PERMITTED_REVIEW_ACTIONS))}.")
    if not isinstance(notes, str) or len(notes.strip()) < 3:
        raise ValueError("Review requires meaningful --notes (at least 3 non-whitespace characters).")
    context = _project_review_context(store, project_id, decision, action=action, build_id=build_id)
    if not context["assigned_build_id"] or not store.state["builds"].get(context["assigned_build_id"]):
        raise ValueError(f"Cannot record review for {project_id}: no valid assigned build exists.")
    return context


def run_cli(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    err_stream = stderr if stderr is not None else sys.stderr

    parser = argparse.ArgumentParser(
        prog="python3 -m tracking.cli",
        description="Inspect project state, review history, workspace health, and record explicit human review decisions.",
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH, help="Path to TrackingStore JSON file.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Summarize known projects, stages, review status, and deployment status.")

    snap_parser = subparsers.add_parser("snapshot", help="Display one project's lead, build, validation, deployment, and review state.")
    snap_parser.add_argument("project_id", help="Target project ID (e.g. project-0002).")

    hist_parser = subparsers.add_parser("history", help="Display a project's chronological review history.")
    hist_parser.add_argument("project_id", help="Target project ID (e.g. project-0002).")

    rev_parser = subparsers.add_parser("review", help="Record an explicit human review decision for a project.")
    rev_parser.add_argument("project_id", help="Target project ID (e.g. project-0002).")
    rev_parser.add_argument(
        "--decision",
        required=True,
        help="Explicit decision: PENDING, CHANGES_REQUESTED, REJECTED, APPROVED_FOR_PREVIEW (pre-deployment local draft approval; alias: --decision APPROVED --action PREVIEW_DEPLOYMENT), or APPROVED (post-deployment preview approval for outreach).",
    )
    rev_parser.add_argument("--notes", required=True, help="Meaningful operator review notes explaining the decision.")
    rev_parser.add_argument(
        "--action",
        default=None,
        help="Optional permitted action (HOLD, REBUILD, DISCARD, PREVIEW_DEPLOYMENT, OUTREACH_PREPARATION). Defaults to PREVIEW_DEPLOYMENT for APPROVED_FOR_PREVIEW and OUTREACH_PREPARATION for APPROVED.",
    )
    rev_parser.add_argument(
        "--build-id",
        default=None,
        help="Optional explicit build_id to verify before recording the review decision.",
    )
    rev_parser.add_argument("--reviewer", default="operator", help="Reviewer identifier (default: operator).")
    rev_parser.add_argument(
        "--confirm",
        default=None,
        help="Explicit non-interactive confirmation token matching '<project_id>:<build_id>:<DECISION>[:<ACTION>]'.",
    )

    target_parser = subparsers.add_parser("configure-preview", help="Configure an isolated Vercel preview project name for a project.")
    target_parser.add_argument("project_id", help="Target project ID (e.g. project-0002).")
    target_parser.add_argument("--project-name", required=True, help="Dedicated preview project slug (must include 'preview' and not be shared/prod).")

    ws_parser = subparsers.add_parser("check-workspace", help="Check local tracking state and artifact integrity.")
    ws_parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root directory.")

    pkg_parser = subparsers.add_parser("review-package", help="Build, verify, or locally serve a human-review package for a project.")
    pkg_parser.add_argument("project_id", help="Target project ID (e.g. project-0002).")
    pkg_parser.add_argument("--export-dir", type=Path, default=None, help="Optional directory to export index.html, review-package.json, and REVIEW_README.md.")
    pkg_parser.add_argument("--verify-dir", type=Path, default=None, help="Verify an existing exported review-package directory against TrackingStore.")
    pkg_parser.add_argument("--serve", action="store_true", help="Serve the exported review-package directory on 127.0.0.1 only.")
    pkg_parser.add_argument("--host", default="127.0.0.1", help="Loopback host interface for --serve (must be 127.0.0.1 or localhost).")
    pkg_parser.add_argument("--port", type=int, default=8080, help="Port for --serve (default: 8080).")
    pkg_parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root directory.")

    ingest_parser = subparsers.add_parser("ingest-lead", help="Ingest a research record (or run offline research) into TrackingStore.")
    ingest_parser.add_argument("--record", type=Path, default=None, help="Path to a ResearchAgent record JSON file.")
    ingest_parser.add_argument("--candidate-file", type=Path, default=None, help="Path to a Scout candidate JSON file.")
    ingest_parser.add_argument("--evidence-file", type=Path, default=None, help="Path to an evidence JSON file.")
    ingest_parser.add_argument("--create-project", action="store_true", help="Create or return the tracked project if the lead is QUALIFIED.")

    build_parser = subparsers.add_parser("build", help="Generate and validate a website draft for a tracked project via JobService + LocalPipelineWorker.")
    build_parser.add_argument("project_id", help="Target project ID (e.g. project-0001).")
    build_parser.add_argument("--output-root", type=Path, default=Path("output"), help="Output directory for generated build artifacts.")
    build_parser.add_argument("--job-id", default=None, help="Optional idempotency job ID prefix.")

    dep_check_parser = subparsers.add_parser(
        "deploy-check",
        help="Read-only: report whether a real preview deployment is currently authorized for a project (never deploys).",
    )
    dep_check_parser.add_argument("project_id", help="Target project ID (e.g. project-0001).")
    dep_check_parser.add_argument("--build-id", default=None, help="Assert the intended build_id instead of the project's assigned build.")

    prep_parser = subparsers.add_parser("prepare-preview", help="Run a safe dry-run deployment readiness check for a project's assigned build.")
    prep_parser.add_argument("project_id", help="Target project ID (e.g. project-0002).")
    prep_parser.add_argument("--project-name", default=None, help="Expected isolated Vercel preview project name.")
    prep_parser.add_argument("--approve-real-preview", action="store_true", help="Authorize dry-run check for a REAL_PUBLIC_DATA project.")
    prep_parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root directory.")

    out_parser = subparsers.add_parser("outreach", help="Prepare or review an outreach draft for an approved project (no sending).")
    out_parser.add_argument("project_id", help="Target project ID.")
    out_parser.add_argument("--channel", default="EMAIL", help="Outreach channel (EMAIL, WHATSAPP, INSTAGRAM, FACEBOOK).")
    out_parser.add_argument("--tone", default="professional", help="Draft tone.")
    out_parser.add_argument("--regenerate", action="store_true", help="Create a new revision if a draft already exists.")
    out_parser.add_argument("--outreach-id", default=None, help="Existing outreach_id to review instead of creating.")
    out_parser.add_argument("--decision", default=None, help="Review decision for --outreach-id (APPROVED, REJECTED, CHANGES_REQUESTED).")
    out_parser.add_argument("--notes", default=None, help="Review notes for --outreach-id.")

    rec_exp_parser = subparsers.add_parser(
        "recover-verified-exports",
        help="Recover project-0001 and project-0002 from committed fixtures and SHA-256 verified review packages.",
    )
    rec_exp_parser.add_argument("--repo-root", type=Path, default=Path("."), help="Repository root directory.")

    mig_parser = subparsers.add_parser(
        "migrate-db",
        help="Apply or verify versioned SQL migration 0001_supabase_postgres_schema.sql on DATABASE_URL.",
    )
    mig_parser.add_argument("--database-url", default=None, help="Database connection URL (defaults to DATABASE_URL env var).")
    mig_parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify that the migration and its SHA-256 checksum are already applied without running DDL.",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    try:
        if args.command == "migrate-db":
            db_url = configured_database_url(args.database_url)
            if not db_url:
                raise ValueError("DATABASE_URL or --database-url is required to run migrate-db.")
            if args.verify_only:
                backend = RelationalStateBackend(db_url, auto_migrate=False)
                verified = backend.verify_schema()
                out_stream.write(json.dumps(verified, indent=2) + "\n")
                return 0
            backend = RelationalStateBackend(db_url, auto_migrate=True)
            verified = backend.verify_schema()
            out_stream.write(
                json.dumps(
                    {
                        "status": "MIGRATED",
                        "migration_version": MIGRATION_VERSION,
                        "migration_file": str(MIGRATION_0001_PATH),
                        "checksum": verified["checksum"],
                        "dialect": backend.dialect,
                        "database": backend.redacted_url,
                    },
                    indent=2,
                )
                + "\n"
            )
            return 0

        store = TrackingStore(args.state_path)
        if args.command == "recover-verified-exports":
            report = store.recover_from_verified_exports(repository_root=args.repo_root)
            out_stream.write(json.dumps(report, indent=2) + "\n")
            return 0

        if args.command == "status":
            out_stream.write(json.dumps(store.status_summary(), indent=2) + "\n")
            return 0

        if args.command == "snapshot":
            snap = store.snapshot(args.project_id)
            payload = {
                "project_id": snap["project"]["project_id"],
                "lead_id": snap["lead"]["lead_id"],
                "business_name": snap["lead"].get("business_name"),
                "data_classification": snap["lead"].get("data_classification") or snap["project"].get("data_classification"),
                "current_stage": snap["project"].get("current_stage"),
                "workflow_state": snap["workflow_state"],
                "review_status": snap["project"].get("review_status"),
                "approved_build_id": snap["project"].get("approved_build_id"),
                "approved_action": snap["project"].get("approved_action"),
                "review_notes": snap["project"].get("review_notes"),
                "assigned_build_id": snap["project"].get("assigned_build_id"),
                "isolated_preview_project": snap["project"].get("isolated_preview_project"),
                "artifact_exists": snap["artifact_exists"],
                "next_action": snap["next_action"],
                "lead": snap["lead"],
                "project": snap["project"],
                "latestBuild": snap["latestBuild"],
                "latestValidation": snap["latestValidation"],
                "latestDeployment": snap["latestDeployment"],
            }
            out_stream.write(json.dumps(payload, indent=2) + "\n")
            return 0

        if args.command == "history":
            snap = store.snapshot(args.project_id)
            history = store.get_review_history(args.project_id)
            payload = {
                "project_id": args.project_id,
                "lead_id": snap["lead"]["lead_id"],
                "business_name": snap["lead"].get("business_name"),
                "data_classification": snap["lead"].get("data_classification") or snap["project"].get("data_classification"),
                "current_stage": snap["project"].get("current_stage"),
                "workflow_state": snap["workflow_state"],
                "review_status": snap["project"].get("review_status"),
                "review_count": len(history),
                "review_history": history,
            }
            out_stream.write(json.dumps(payload, indent=2) + "\n")
            return 0

        if args.command == "ingest-lead":
            if args.record is not None:
                rec = json.loads(args.record.read_text(encoding="utf-8"))
            elif args.candidate_file is not None and args.evidence_file is not None:
                from research.research_agent import FixtureResearchSource, ResearchAgent

                cand = json.loads(args.candidate_file.read_text(encoding="utf-8"))
                if isinstance(cand, list):
                    cand = cand[0]
                res = ResearchAgent([FixtureResearchSource(args.evidence_file)]).run(cand)
                if res.status != "completed":
                    raise ValueError("; ".join(res.errors) or "Research failed.")
                rec = res.record
            else:
                raise ValueError("Specify either --record or both --candidate-file and --evidence-file.")
            lead = store.ingest_research_record(rec)
            proj = None
            if args.create_project and lead.get("qualification_status") == "QUALIFIED":
                proj = store.create_project(lead["lead_id"])
            out_stream.write(
                json.dumps(
                    {
                        "lead_id": lead["lead_id"],
                        "business_name": lead.get("business_name"),
                        "data_classification": lead.get("data_classification"),
                        "qualification_status": lead.get("qualification_status"),
                        "website_status": lead.get("website_status"),
                        "project_id": proj["project_id"] if proj else None,
                    },
                    indent=2,
                )
                + "\n"
            )
            return 0

        if args.command == "build":
            from orchestration.jobs import JobService
            from orchestration.workers import LocalPipelineWorker

            snap = store.snapshot(args.project_id)
            lead_id = snap["lead"]["lead_id"]
            jobs = JobService(store)
            worker = LocalPipelineWorker(output_root=args.output_root)
            prefix = args.job_id or f"cli-build-{args.project_id}-{len(store.state.get('jobs', {})) + 1:04d}"
            b_job = jobs.create("BUILD", job_id=prefix, lead_id=lead_id, project_id=args.project_id)
            b_res = jobs.run(b_job["job_id"], worker)
            if b_res["status"] != "COMPLETED":
                raise ValueError(b_res.get("error") or f"BUILD job ended with status {b_res['status']}.")
            v_job = jobs.create_next(b_job["job_id"], next_job_id=f"{prefix}-validate")
            v_res = jobs.run(v_job["job_id"], worker)
            if v_res["status"] != "COMPLETED":
                raise ValueError(v_res.get("error") or f"VALIDATE job ended with status {v_res['status']}.")
            after = store.snapshot(args.project_id)
            out_stream.write(
                json.dumps(
                    {
                        "project_id": args.project_id,
                        "lead_id": lead_id,
                        "build_id": after["project"].get("assigned_build_id"),
                        "validation_id": (after["latestValidation"] or {}).get("validation_id"),
                        "validation_status": (after["latestValidation"] or {}).get("status"),
                        "current_stage": after["project"].get("current_stage"),
                        "workflow_state": after["workflow_state"],
                        "review_status": after["project"].get("review_status"),
                        "output_location": (after["latestBuild"] or {}).get("output_location"),
                    },
                    indent=2,
                )
                + "\n"
            )
            return 0

        if args.command == "deploy-check":
            from deployment.authorization import UNRESOLVED_ATTEMPT_STATES

            gate = store.authorize_preview_deployment(args.project_id, build_id=args.build_id)
            unresolved = str(gate.get("latest_attempt_status") or "") in UNRESOLVED_ATTEMPT_STATES
            payload = {
                key: value for key, value in gate.items() if key not in {"approval", "publish_attempts", "latest_attempt"}
            }
            payload.update({
                "publish_state_uncertain": unresolved,
                "note": (
                    "Nothing was published by this check. It only reports what a real deployment would be allowed to do."
                ),
                "prior_attempts": gate.get("prior_attempts"),
                # A blocked publish has different causes and different fixes; naming the wrong command
                # here would send an operator off to re-approve a build whose real problem is an
                # unresolved provider answer.
                "next_command": (
                    None
                    if gate.get("authorized")
                    else (
                        "python3 -m deployment.cli --build <build directory> --execute --project-id "
                        f"{args.project_id} --resolve-uncertain-publish --resolve-note \"<what you checked "
                        "with the provider and why retrying is safe>\" (records the decision, then retries "
                        "once)"
                        if gate.get("code") == "PUBLISH_STATE_UNCERTAIN"
                        else "python3 -m tracking.cli review "
                             f"{args.project_id} --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT "
                             "--notes \"...\" --confirm "
                             f"{args.project_id}:{gate.get('build_id')}:APPROVED_FOR_PREVIEW"
                    )
                ),
            })
            out_stream.write(json.dumps(payload, indent=2) + "\n")
            if not gate.get("authorized") and not gate.get("skip_publish"):
                err_stream.write(
                    "Deployment is not authorized. Nothing was published. Resolve the reported condition, "
                    "then re-run deploy-check before executing a deployment.\n"
                )
            return 0 if gate.get("authorized") else 1

        if args.command == "prepare-preview":
            from orchestration.jobs import JobService
            from orchestration.workers import LocalPipelineWorker

            snap = store.snapshot(args.project_id)
            lead_id = snap["lead"]["lead_id"]
            build_id = snap["project"].get("assigned_build_id")
            jobs = JobService(store)
            worker = LocalPipelineWorker(repository_root=args.repo_root)
            job_id = f"cli-prep-preview-{args.project_id}-{len(store.state.get('jobs', {})) + 1:04d}"
            d_job = jobs.create(
                "DEPLOY",
                job_id=job_id,
                lead_id=lead_id,
                project_id=args.project_id,
                build_id=build_id,
                input={
                    "project_name": args.project_name,
                    "approve_real_preview": bool(args.approve_real_preview),
                },
                dry_run=True,
            )
            d_res = jobs.run(d_job["job_id"], worker)
            out_stream.write(json.dumps(d_res, indent=2) + "\n")
            return 0 if d_res["status"] == "COMPLETED" else 1

        if args.command == "outreach":
            from outreach.service import OutreachService

            svc = OutreachService(store)
            if args.outreach_id:
                if not args.decision:
                    raise ValueError("--decision is required when reviewing --outreach-id.")
                reviewed = svc.review(args.outreach_id, args.decision.strip().upper(), notes=args.notes)
                out_stream.write(json.dumps(reviewed, indent=2) + "\n")
                return 0
            draft = svc.create(
                args.project_id,
                channel=args.channel.strip().upper(),
                tone=args.tone,
                regenerate=bool(args.regenerate),
            )
            out_stream.write(json.dumps(draft, indent=2) + "\n")
            return 0

        if args.command == "configure-preview":
            updated = store.configure_preview_target(args.project_id, args.project_name)
            out_stream.write(
                json.dumps(
                    {
                        "project_id": updated["project_id"],
                        "lead_id": updated["lead_id"],
                        "isolated_preview_project": updated["isolated_preview_project"],
                        "current_stage": updated["current_stage"],
                        "review_status": updated["review_status"],
                    },
                    indent=2,
                )
                + "\n"
            )
            return 0

        if args.command == "check-workspace":
            out_stream.write(json.dumps(store.check_local_state(repository_root=args.repo_root), indent=2) + "\n")
            return 0

        if args.command == "review-package":
            if args.verify_dir is not None:
                verification = store.verify_review_package(
                    args.project_id,
                    args.verify_dir,
                    repository_root=args.repo_root,
                )
                out_stream.write(json.dumps(verification, indent=2) + "\n")
                return 0 if verification["status"] == "VALID" else 1
            pkg = store.build_review_package(
                args.project_id,
                export_dir=args.export_dir,
                repository_root=args.repo_root,
            )
            out_stream.write(json.dumps(pkg, indent=2) + "\n")
            if args.serve:
                from builder.cli import serve_build

                target_dir = args.export_dir if args.export_dir is not None else Path(pkg["artifact_path"]).parent
                serve_build(target_dir, host=args.host, port=args.port)
            return 0

        if args.command == "review":
            decision = str(args.decision).strip().upper()
            action = str(args.action).strip().upper() if args.action is not None else None
            if decision == "APPROVED" and action == "PREVIEW_DEPLOYMENT":
                decision = "APPROVED_FOR_PREVIEW"
            elif decision == "APPROVED_FOR_PREVIEW" and action is None:
                action = "PREVIEW_DEPLOYMENT"
            context = _validate_review_preconditions(
                store,
                args.project_id,
                decision,
                args.notes,
                action=action,
                build_id=args.build_id,
            )
            expected = context["required_confirmation"]
            b_id = context["assigned_build_id"] or "no-build"
            valid_confirmations = {expected}
            if decision == "APPROVED_FOR_PREVIEW":
                valid_confirmations.update({
                    f"{args.project_id}:{b_id}:APPROVED_FOR_PREVIEW:PREVIEW_DEPLOYMENT",
                    f"{args.project_id}:{b_id}:APPROVED_FOR_PREVIEW",
                    f"{args.project_id}:{b_id}:APPROVED:PREVIEW_DEPLOYMENT",
                })
            elif decision == "APPROVED":
                valid_confirmations.update({
                    f"{args.project_id}:{b_id}:APPROVED",
                    f"{args.project_id}:{b_id}:APPROVED:OUTREACH_PREPARATION",
                })

            if decision in CONSEQUENTIAL_DECISIONS:
                if args.confirm is not None:
                    provided = args.confirm.strip()
                else:
                    err_stream.write(
                        f"Review target: project={context['project_id']} lead={context['lead_id']} "
                        f"business={context['business_name']!r} build={context['assigned_build_id']} "
                        f"decision={decision}"
                        + (f" action={action}" if action else "")
                        + "\n"
                        f"Type '{expected}' to confirm recording this review decision:\n"
                    )
                    err_stream.flush()
                    line = in_stream.readline() if in_stream is not None else ""
                    provided = line.strip()
                if provided not in valid_confirmations:
                    raise ValueError(
                        f"Confirmation required: expected exact token '{expected}' before recording {decision} on {args.project_id}."
                    )

            updated = store.record_review(
                args.project_id,
                decision,
                notes=args.notes.strip(),
                reviewer=args.reviewer,
                require_notes=True,
                build_id=args.build_id,
                action=action,
                identity_source="tracking-cli",
            )
            latest_entry = store.get_review_history(args.project_id)[-1]
            out_stream.write(
                json.dumps(
                    {
                        "target": context,
                        "deployment_authorization": store.authorize_preview_deployment(args.project_id),
                        "deployed": False,
                        "note": (
                            "Recording a review decision never publishes anything. An APPROVED_FOR_PREVIEW decision "
                            "authorizes one explicit, separately executed preview deployment of this exact build."
                        ),
                        "project_id": updated["project_id"],
                        "lead_id": updated["lead_id"],
                        "assigned_build_id": updated.get("assigned_build_id"),
                        "approved_build_id": updated.get("approved_build_id"),
                        "approved_action": updated.get("approved_action"),
                        "current_stage": updated["current_stage"],
                        "workflow_state": store.snapshot(args.project_id)["workflow_state"],
                        "review_status": updated["review_status"],
                        "recorded_review": latest_entry,
                    },
                    indent=2,
                )
                + "\n"
            )
            return 0

    except (ValueError, DatabasePersistenceError) as error:
        err_stream.write(json.dumps({"error": str(error)}) + "\n")
        return 1

    return 1


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
