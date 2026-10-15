"""Safe operator-facing CLI for inspecting tracked projects, review history, and explicit review decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import TextIO

from .database import (DatabasePersistenceError, MIGRATION_0001_PATH, MIGRATION_VERSION, RelationalStateBackend,
                        configured_database_url, is_production_mode, supabase_project_ref)
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


def _postgres_driver_available() -> str | None:
    """Name an installed PostgreSQL driver, or ``None``.

    A schema migration over a URL the application cannot open is the one integration failure that looks
    like a database problem and is really a missing dependency, so the preflight checks it before
    anything tries to connect.
    """
    for module_name in ("psycopg", "psycopg2"):
        try:
            __import__(module_name)
        except ImportError:
            continue
        return module_name
    return None


def _local_state_report() -> dict:
    """The local side of persistence: where a JSON state file would live, and whether it may."""
    path = Path(DEFAULT_STATE_PATH)
    parent = path.parent
    production = is_production_mode()
    return {
        "path": str(path),
        "exists": path.is_file(),
        "directory_present": parent.is_dir(),
        "production_mode": production,
        # Not a prediction, a policy statement: in production mode `_save()` raises rather than
        # writing, so a missing database cannot be papered over with local state.
        "json_fallback_allowed": not production,
        "json_fallback_policy": (
            "prohibited in production mode" if production else "permitted only outside production mode"
        ),
    }


def _database_preflight(
    database_url: str | None,
    *,
    expect_project_ref: str | None = None,
    expect_database: str | None = None,
) -> dict:
    """Assemble the read-only answer to "which database would this app migrate, and is it ready".

    The connection string is resolved exactly the way the application resolves it, so what is reported is
    what the application would use, not what the operator hopes it uses.
    """
    report: dict = {"status": "not_configured", "ddl_executed": False, "backend": "local_json",
                    "local_state": _local_state_report(), "blockers": [], "pending": [], "warnings": [],
                    "next_action": None, "exit_code": 1}
    try:
        resolved_url = configured_database_url(database_url)
    except DatabasePersistenceError as error:
        report["status"] = "blocked"
        report["blockers"].append(str(error))
        report["next_action"] = (
            "Production mode requires Supabase/PostgreSQL: set AI_WEBSITE_AGENT_DATABASE_URL or "
            "DATABASE_URL to a postgresql:// connection string. Nothing was created or modified."
        )
        return report
    if not resolved_url:
        report["blockers"].append(
            "No database URL is configured (AI_WEBSITE_AGENT_DATABASE_URL, DATABASE_URL, or "
            "SUPABASE_DB_URL), so the application would use the single-process local JSON state file."
        )
        report["next_action"] = (
            "For local development that is acceptable. For any shared, deployed, or production service, "
            "set AI_WEBSITE_AGENT_ENV=production, AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres, and "
            "DATABASE_URL - production mode then refuses local JSON instead of falling back to it."
        )
        return report

    backend = RelationalStateBackend(resolved_url, auto_migrate=False)
    report["backend"] = backend.dialect
    # `hard` holds what this command found wrong about the *target* itself. The probe's own blockers
    # include "not migrated yet", which is a step to take rather than a fault to fix, and it has to stay
    # `needs_migration` so an operator is not sent off debugging a healthy database.
    hard: list[str] = []
    probe = backend.preflight()
    report.update(probe)
    identity = probe["identity"]

    if identity["dialect"] == "postgres" and _postgres_driver_available() is None:
        hard.append(
            "No PostgreSQL driver is installed in this environment (`psycopg` or `psycopg2`); the URL "
            "cannot be opened from here. Run `pip install -r requirements.txt` in the environment that "
            "will run the migration."
        )
        report["warnings"].append(
            "Every database answer above is unverified in this environment because no connection was "
            "possible; the schema is reported as unknown rather than absent."
        )
        report["status"] = "blocked"

    if expect_project_ref:
        expected = str(expect_project_ref).strip().lower()
        found = identity.get("supabase_project_ref")
        if found is None:
            hard.append(
                "--expect-project-ref was given but this hostname carries no project reference to "
                "compare. Confirm the connection string against the Supabase dashboard instead of "
                "migrating on assumption."
            )
        elif found != expected:
            hard.append(
                f"This URL points at Supabase project '{found}', not the expected '{expected}'. The "
                "migration was not run."
            )
    if expect_database:
        expected_db = str(expect_database).strip().lower()
        found_db = (identity.get("database") or "").lower()
        if found_db != expected_db:
            hard.append(
                f"This URL opens database '{found_db or '<none>'}', not the expected '{expected_db}'. "
                "The migration was not run."
            )

    if hard:
        report["blockers"] = hard + list(report["blockers"])
        if report["status"] != "unreachable":
            report["status"] = "blocked"
            report["next_action"] = (
                "Fix the identity or driver problem above before running any DDL; `migrate-db` refuses "
                "to apply a schema while it is reported."
            )
    if report["status"] == "needs_migration" and identity["dialect"] == "postgres" and not identity["is_local_host"]:
        report["next_action"] = (
            "Verify the identity above against the Supabase dashboard, then run "
            f"`python3 -m tracking.cli migrate-db --confirm-database {identity['database']}`; the "
            "confirmation flag is required for a remote database and DDL is additive."
        )
    if report["status"] == "needs_migration" and identity["dialect"] == "sqlite":
        report["next_action"] = "Run `python3 -m tracking.cli migrate-db --database-url <this url>` to create the schema."
    report["exit_code"] = 0 if report["status"] == "ready" else 1
    report["supabase_identification"] = (
        "Supabase dashboard -> Connect -> Session pooler shows the project reference; the reference in "
        "this URL is "
        + (f"'{identity['supabase_project_ref']}'." if identity.get("supabase_project_ref") else
           "not present in the hostname, so verify the reference in the dashboard against the connection "
           "string you are about to paste.")
    )
    return report


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

    def _ledger(target: argparse.ArgumentParser, *, lead_required: bool = True) -> None:
        """Engagement subcommands share the same evidence flags; one helper keeps them consistent."""
        if lead_required:
            target.add_argument("lead_id", help="Tracked lead the record attaches to (e.g. lead-0002).")
        target.add_argument("--evidence-note", default=None, help="What the evidence says, in the operator's words. Contact details are redacted on write.")
        target.add_argument("--evidence-url", default=None, help="Absolute http(s) URL of the public source, when there is one.")
        target.add_argument("--occurred-at", default=None, help="When it happened (YYYY-MM-DD or ISO-8601). Defaults to now.")

    eng_parser = subparsers.add_parser("engagement-record", help="Append one engagement event (note, send, response, decision) to a lead's audit trail.")
    _ledger(eng_parser)
    eng_parser.add_argument("--kind", required=True, help="Event kind, e.g. NOTE, RESPONSE_RECEIVED, PROPOSAL_DECISION.")
    eng_parser.add_argument("--summary", required=True, help="Short description of what happened.")
    eng_parser.add_argument("--direction", default="INTERNAL", help="OUTBOUND, INBOUND, or INTERNAL.")
    eng_parser.add_argument("--channel", default=None, help="EMAIL, WHATSAPP, INSTAGRAM, FACEBOOK, LINKEDIN, SMS, PHONE, IN_PERSON, OTHER.")
    eng_parser.add_argument("--project-id", default=None, help="Project the event concerns, when it is not the lead's own.")
    eng_parser.add_argument("--outreach-id", default=None, help="Link an outreach draft record.")
    eng_parser.add_argument("--proposal-id", default=None, help="Link a proposal record.")
    eng_parser.add_argument("--follow-up-id", default=None, help="Link a follow-up record.")
    eng_parser.add_argument("--idempotency-key", default=None, help="Repeat-safe key; the same key records the event once.")

    resp_parser = subparsers.add_parser("response-record", help="Record an inbound reply from the business, with its evidence.")
    _ledger(resp_parser)
    resp_parser.add_argument("--channel", required=True, help="Channel the reply arrived on.")
    resp_parser.add_argument("--summary", required=True, help="What the business said, in the operator's words.")
    resp_parser.add_argument("--quote", default=None, help="Short verbatim quote from the message (contact details redacted).")
    resp_parser.add_argument("--received-at", default=None, help="When the reply arrived (defaults to --occurred-at or now).")
    resp_parser.add_argument("--outreach-id", default=None, help="Outreach draft this answers, if known.")
    resp_parser.add_argument("--idempotency-key", default=None, help="Repeat-safe key for the same inbound message.")

    sent_parser = subparsers.add_parser("outreach-mark-sent", help="Record that a human actually sent an approved draft (a prepared draft is never 'sent').")
    sent_parser.add_argument("outreach_id", help="Outreach record id (e.g. outreach-0001).")
    sent_parser.add_argument("--occurred-at", default=None, help="When it was sent.")
    sent_parser.add_argument("--evidence-note", default=None, help="How the operator knows it went out.")
    sent_parser.add_argument("--evidence-url", default=None, help="Link to the sent message, when one exists.")
    sent_parser.add_argument("--provider-message-id", default=None, help="Provider id as given; recorded verbatim and not verified against any provider.")

    fu_sched = subparsers.add_parser("follow-up-schedule", help="Schedule a follow-up for a lead (refused when that channel is opted out).")
    _ledger(fu_sched)
    fu_sched.add_argument("--due", required=True, help="Due date (YYYY-MM-DD means end of that day).")
    fu_sched.add_argument("--reason", required=True, help="Why this follow-up exists.")
    fu_sched.add_argument("--channel", default=None, help="Channel the follow-up will use, if decided.")
    fu_sched.add_argument("--proposal-id", default=None, help="Link a proposal this follow-up concerns.")
    fu_sched.add_argument("--allow-past-due", action="store_true", help="Permit backdating a schedule deliberately.")

    fu_update = subparsers.add_parser("follow-up-update", help="Complete, cancel, or reschedule a follow-up.")
    fu_update.add_argument("follow_up_id", help="Follow-up id (e.g. follow_up-0001).")
    fu_update.add_argument("--action", required=True, help="COMPLETE, CANCEL, or RESCHEDULE.")
    fu_update.add_argument("--notes", default=None, help="What happened, or why it was cancelled/rescheduled.")
    fu_update.add_argument("--evidence-note", default=None, help="Evidence supporting a completion.")
    fu_update.add_argument("--evidence-url", default=None, help="Public source URL supporting the change.")
    fu_update.add_argument("--due", default=None, help="New due date (required with RESCHEDULE).")
    fu_update.add_argument("--allow-past-due", action="store_true", help="Permit a backdated reschedule.")

    due_parser = subparsers.add_parser("due-follow-ups", help="List scheduled follow-ups that are due or overdue, as of today.")
    due_parser.add_argument("--today", default=None, help="Override the reference date (YYYY-MM-DD) for a report.")

    prop_create = subparsers.add_parser("proposal-create", help="Draft a proposal record for a lead (creating it does not present it).")
    _ledger(prop_create)
    prop_create.add_argument("--summary", required=True, help="What is being offered, in the operator's words.")
    prop_create.add_argument("--project-id", default=None, help="Project this proposal covers (defaults to the lead's project).")
    prop_create.add_argument("--outreach-id", default=None, help="Outreach draft it relates to.")
    prop_create.add_argument("--idempotency-key", default=None, help="Repeat-safe key for the same draft.")
    prop_create.add_argument("--terms-note", default=None, help="Free-text terms as written by the operator.")
    prop_create.add_argument("--currency", default=None, help="ISO currency code, only together with --amount-minor.")
    prop_create.add_argument("--amount-minor", type=int, default=None, help="Amount in minor units, only together with --currency.")

    prop_update = subparsers.add_parser("proposal-update", help="Move a proposal through READY_FOR_PRESENTATION, PRESENTED, ACCEPTED, DECLINED, WITHDRAWN.")
    prop_update.add_argument("proposal_id", help="Proposal id (e.g. proposal-0001).")
    prop_update.add_argument("--status", required=True, help="Target status; transitions are enforced and evidence is required to present or decide.")
    prop_update.add_argument("--evidence-note", default=None, help="How the presentation or decision happened.")
    prop_update.add_argument("--evidence-url", default=None, help="Source URL for it, when one exists.")
    prop_update.add_argument("--occurred-at", default=None, help="When it happened.")
    prop_update.add_argument("--notes", default=None, help="Operator note stored with the decision.")

    out_parser2 = subparsers.add_parser("outcome-record", help="Record the prospect's outcome; WON needs evidence and a contract/invoice/payment reference.")
    _ledger(out_parser2)
    out_parser2.add_argument("--status", required=True, help="INTERESTED, FOLLOW_UP_REQUESTED, NEGOTIATING, WON, LOST, DECLINED, OPTED_OUT, NO_RESPONSE, PAUSED, UNRESOLVED.")
    out_parser2.add_argument("--evidence-kind", default="CUSTOMER_MESSAGE", help="Evidence kind (CUSTOMER_MESSAGE, CONTRACT, INVOICE, PAYMENT, CALL_NOTE, MEETING, ...).")
    out_parser2.add_argument("--reference", default=None, help="Contract/invoice/payment reference (required for WON).")
    out_parser2.add_argument("--notes", default=None, help="Operator note kept with the outcome.")

    opt_parser = subparsers.add_parser("opt-out", help="Record that a business asked not to be contacted, cancelling matching open follow-ups.")
    _ledger(opt_parser, lead_required=True)
    opt_parser.add_argument("--channel", default=None, help="Channel the request covers; omit for all channels.")
    opt_parser.add_argument("--reason", default=None, help="The request as reported by the operator.")

    opt_revoke = subparsers.add_parser("opt-out-revoke", help="Withdraw an opt-out only when the business asked to be contacted again.")
    opt_revoke.add_argument("block_id", help="Opt-out record id (e.g. block-0001).")
    opt_revoke.add_argument("--reason", required=True, help="What the business asked for, in the operator's words.")
    opt_revoke.add_argument("--evidence-note", default=None, help="Evidence of the reversal request.")

    ledger_parser = subparsers.add_parser("ledger", help="Show the engagement, proposal, follow-up, and outcome records for one lead or project.")
    ledger_parser.add_argument("lead_id", nargs="?", default=None, help="Lead id (e.g. lead-0002), or omit with --project-id.")
    ledger_parser.add_argument("--project-id", default=None, help="View by project instead of by lead.")

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
    mig_parser.add_argument(
        "--confirm-database",
        default=None,
        metavar="NAME",
        help="Required to apply DDL to a database that is not on this machine: the database name read "
             "from the URL by `preflight-db`. A stale DATABASE_URL in a shell cannot then migrate an "
             "unintended Supabase project.",
    )

    preflight_parser = subparsers.add_parser(
        "preflight-db",
        help="Read-only check of which database this app would use, whether it is reachable, and whether "
             "the migration is applied. Runs no DDL and writes nothing.",
    )
    preflight_parser.add_argument("--database-url", default=None, help="Database connection URL (defaults to DATABASE_URL env var).")
    preflight_parser.add_argument("--expect-project-ref", default=None,
                                 help="Fail unless the Supabase project reference in the URL host matches this value.")
    preflight_parser.add_argument("--expect-database", default=None,
                                 help="Fail unless the database name in the URL matches this value.")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    try:
        if args.command == "preflight-db":
            report = _database_preflight(args.database_url, expect_project_ref=args.expect_project_ref,
                                         expect_database=args.expect_database)
            out_stream.write(json.dumps(report, indent=2) + "\n")
            return int(report.get("exit_code", 1))

        if args.command == "migrate-db":
            db_url = configured_database_url(args.database_url)
            if not db_url:
                raise ValueError("DATABASE_URL or --database-url is required to run migrate-db.")
            if args.verify_only:
                backend = RelationalStateBackend(db_url, auto_migrate=False)
                verified = backend.verify_schema()
                out_stream.write(json.dumps(verified, indent=2) + "\n")
                return 0
            # Applying DDL is the one irreversible-feeling step in this setup, and `CREATE TABLE IF NOT
            # EXISTS` is quiet about landing in a database someone else owns. So the migration command
            # reads the target first and requires the operator to name it back - exactly the identity a
            # wrong-host mistake would have revealed. Local SQLite/loopback targets keep working as-is.
            probe_backend = RelationalStateBackend(db_url, auto_migrate=False)
            identity = probe_backend.identity()
            if not identity["is_local_host"]:
                discovered = identity["database"] or "<unknown>"
                supplied = str(args.confirm_database or "").strip()
                if not supplied:
                    raise DatabasePersistenceError(
                        f"Refusing to apply DDL to a remote database ({identity['host']}, "
                        f"database '{discovered}'"
                        + (f", Supabase project '{identity['supabase_project_ref']}'"
                           if identity.get("supabase_project_ref") else "")
                        + ") without confirmation. Re-run with "
                        f"`--confirm-database {discovered}` after verifying this is the intended project "
                        "in the Supabase dashboard, or use --verify-only to check without changing anything."
                    )
                if supplied.strip().lower() != discovered.lower():
                    raise DatabasePersistenceError(
                        f"Refusing to apply DDL: --confirm-database '{supplied}' does not match the "
                        f"database named in the configured URL ('{discovered}')."
                    )
            preflight_state = probe_backend.preflight()
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
                        # Recorded with the result so the audit trail says which target was changed and
                        # whether it already held data.
                        "target": {
                            "host": identity["host"],
                            "port": identity["port"],
                            "database": identity["database"],
                            "supabase_project_ref": identity["supabase_project_ref"],
                        },
                        "schema_before": {
                            "migration_applied": preflight_state["migration"]["applied"],
                            "missing_tables": preflight_state["schema"]["missing_tables"],
                            "row_counts": preflight_state["schema"]["row_counts"],
                        },
                        "safe_to_repeat": True,
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

        if args.command in {
            "engagement-record", "response-record", "outreach-mark-sent", "follow-up-schedule",
            "follow-up-update", "due-follow-ups", "proposal-create", "proposal-update", "outcome-record",
            "opt-out", "opt-out-revoke", "ledger",
        }:
            from tracking.engagement import EngagementLedger

            ledger = EngagementLedger(store)
            evidence = [
                payload for payload in [
                    {"kind": "OPERATOR_NOTE", "note": getattr(args, "evidence_note", None)},
                    {"kind": "URL", "source_url": getattr(args, "evidence_url", None)},
                ]
                if payload.get("note") or payload.get("source_url")
            ]
            if args.command == "engagement-record":
                result = ledger.record_event(
                    args.lead_id,
                    kind=args.kind,
                    summary=args.summary,
                    direction=args.direction,
                    channel=args.channel,
                    occurred_at=args.occurred_at,
                    evidence=evidence,
                    project_id=args.project_id if hasattr(args, "project_id") else None,
                    outreach_id=args.outreach_id,
                    proposal_id=args.proposal_id,
                    follow_up_id=args.follow_up_id,
                    idempotency_key=args.idempotency_key,
                )
            elif args.command == "response-record":
                result = ledger.record_response(
                    args.lead_id,
                    channel=args.channel,
                    summary=args.summary,
                    received_at=args.received_at or args.occurred_at,
                    evidence_note=args.evidence_note,
                    evidence_url=args.evidence_url,
                    quote=args.quote,
                    outreach_id=args.outreach_id,
                    idempotency_key=args.idempotency_key,
                )
            elif args.command == "outreach-mark-sent":
                result = ledger.mark_outreach_sent(
                    args.outreach_id,
                    occurred_at=args.occurred_at,
                    evidence_note=args.evidence_note,
                    evidence_url=args.evidence_url,
                    provider_message_id=args.provider_message_id,
                )
            elif args.command == "follow-up-schedule":
                result = ledger.schedule_follow_up(
                    args.lead_id,
                    due_at=args.due,
                    reason=args.reason,
                    channel=args.channel,
                    proposal_id=args.proposal_id,
                    allow_past_due=bool(args.allow_past_due),
                )
            elif args.command == "follow-up-update":
                result = ledger.update_follow_up(
                    args.follow_up_id,
                    action=args.action,
                    notes=args.notes,
                    evidence_note=args.evidence_note,
                    evidence_url=args.evidence_url,
                    due_at=args.due,
                    allow_past_due=bool(args.allow_past_due),
                )
            elif args.command == "due-follow-ups":
                result = ledger.due_follow_ups(today=args.today)
            elif args.command == "proposal-create":
                result = ledger.create_proposal(
                    args.lead_id,
                    summary=args.summary,
                    project_id=args.project_id if hasattr(args, "project_id") else None,
                    outreach_id=args.outreach_id,
                    terms_note=args.terms_note,
                    currency=args.currency,
                    amount_minor=args.amount_minor,
                    idempotency_key=args.idempotency_key,
                )
            elif args.command == "proposal-update":
                result = ledger.update_proposal(
                    args.proposal_id,
                    status=args.status,
                    evidence_note=args.evidence_note,
                    evidence_url=args.evidence_url,
                    occurred_at=args.occurred_at,
                    notes=args.notes,
                )
            elif args.command == "outcome-record":
                result = ledger.record_outcome(
                    args.lead_id,
                    status=args.status,
                    evidence_note=args.evidence_note,
                    evidence_url=args.evidence_url,
                    evidence_kind=args.evidence_kind,
                    reference=args.reference,
                    occurred_at=args.occurred_at,
                    notes=args.notes,
                )
            elif args.command == "opt-out":
                result = ledger.record_opt_out(
                    args.lead_id,
                    channel=args.channel,
                    reason=args.reason,
                    evidence_note=args.evidence_note,
                    evidence_url=args.evidence_url,
                    occurred_at=args.occurred_at,
                )
            elif args.command == "opt-out-revoke":
                result = ledger.revoke_opt_out(args.block_id, reason=args.reason, evidence_note=args.evidence_note)
            else:
                result = ledger.ledger(args.lead_id, project_id=args.project_id)
            out_stream.write(json.dumps(result, indent=2) + "\n")
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
        # A refusal names its code when it has one, so an operator (or an n8n failure branch) can act on the
        # reason rather than parsing prose.
        payload: dict = {"error": str(error)}
        code = getattr(error, "code", None)
        if code:
            payload["error_code"] = code
        err_stream.write(json.dumps(payload) + "\n")
        return 1

    return 1


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
