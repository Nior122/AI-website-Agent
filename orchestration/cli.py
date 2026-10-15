"""Explicit local commands for operating the pipeline: inspect, run, recover, and report on it.

Every command is read-only unless it says otherwise, prints one JSON document on stdout (so it can
be piped), and uses the exit code to carry the verdict: ``0`` healthy or completed, ``1`` attention
required, ``2`` usage or configuration error.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from .alerts import LogAlertSink, check_alerts
from .api import serve
from .health import operator_attention, provider_section, system_health, verify_storage
from .jobs import ATTENTION_STATUSES, JobService
from .workers import LocalPipelineWorker
from tracking.store import TrackingStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Website Agent orchestration controls")
    parser.add_argument(
        "command",
        choices=[
            "serve", "recover-stale", "list-jobs", "check-workspace", "recover-workspace",
            "create-job", "run-job", "retry-job",
            # Operational controls: what is healthy, what is stuck, and what a human must clear.
            "health", "attention", "inspect-job", "deployment-status", "check-alerts",
        ],
        help="serve runs the API; health/attention/inspect-job/deployment-status/check-alerts report "
             "operational state without changing it; recover-stale/recover-workspace restore recorded "
             "state; create-job/run-job/retry-job act on the queue.",
    )
    parser.add_argument("--state-path", default="data/tracking/state.json")
    parser.add_argument("--host", default=None, help="Bind host (defaults to AI_WEBSITE_AGENT_BIND_HOST or 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help="Bind port (defaults to Render PORT, AI_WEBSITE_AGENT_API_PORT, or 8787).")
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow binding to 0.0.0.0 for cloud/Render deployment (requires AI_WEBSITE_AGENT_API_TOKEN).",
    )
    parser.add_argument("--database-url", default=None, help="Optional PostgreSQL/SQLite database URL override.")
    parser.add_argument("--older-than-seconds", type=int, default=900)
    parser.add_argument("--job-type", default=None, help="Job type for create-job (SCOUT, RESEARCH, QUALIFY, BUILD, VALIDATE, DEPLOY, PREPARE_OUTREACH).")
    parser.add_argument("--job-id", default=None, help="Idempotent job_id for create-job, run-job, or retry-job.")
    parser.add_argument("--lead-id", default=None, help="Optional lead_id for create-job.")
    parser.add_argument("--project-id", default=None, help="Optional project_id for create-job.")
    parser.add_argument("--build-id", default=None, help="Optional build_id for create-job.")
    parser.add_argument("--input-json", default="{}", help="JSON object string for job input.")
    parser.add_argument("--output-root", default="output", help="Output directory for LocalPipelineWorker BUILD jobs.")
    parser.add_argument("--repository-root", default=".", help="Workspace root used by health reports (defaults to the current directory).")
    parser.add_argument("--status", default=None, help="list-jobs: only jobs in this status (e.g. FAILED, NEEDS_REVIEW, BLOCKED).")
    parser.add_argument("--attention-only", action="store_true", help="list-jobs: only jobs a human must clear (FAILED, NEEDS_REVIEW, BLOCKED).")
    parser.add_argument("--brief", action="store_true", help="list-jobs/attention: compact operator view instead of full records.")
    parser.add_argument("--history", action="store_true", help="inspect-job: include the recorded execution attempts and retry eligibility.")
    parser.add_argument("--notify", action="store_true", help="check-alerts: emit each critical alert as a structured CRITICAL log event. No external channel is contacted.")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(
            state_path=args.state_path,
            host=args.host,
            port=args.port,
            allow_non_loopback=args.allow_non_loopback,
            output_root=args.output_root,
            database_url=args.database_url,
        )
        return 0
    if args.command == "health":
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        # The operator command verifies storage actively, so "healthy" here means the source of truth
        # was reached - unlike a liveness probe, which can only prove the process is running.
        report = system_health(store, repository_root=Path(args.repository_root), storage=verify_storage(store))
        print(json.dumps(report, indent=2, default=str))
        return 1 if report["readiness"]["status"] == "NOT_READY" else 0
    if args.command in ("attention", "deployment-status"):
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        if args.command == "deployment-status":
            report = provider_section(store, Path(args.repository_root))
        else:
            report = operator_attention(store, repository_root=Path(args.repository_root))
            if args.brief:
                report = {
                    "checked_at": report["checked_at"],
                    "requires_attention": report["requires_attention"],
                    "counts": report["counts"],
                    "items": [
                        {key: item.get(key) for key in
                         ("kind", "code", "status", "job_id", "project_id", "message", "age_seconds", "next_action")}
                        for item in report["items"]
                    ],
                }
        print(json.dumps(report, indent=2, default=str))
        # Exit 1 only for work a human must clear. A missing provider link is a configuration
        # observation (deployments are intentionally not executed from this workspace), so it must
        # not page a monitor on every run.
        if args.command == "deployment-status":
            return 1 if report.get("attention_count") else 0
        return 1 if report.get("requires_attention") else 0
    if args.command == "check-alerts":
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        report = check_alerts(store, repository_root=Path(args.repository_root),
                              sinks=[LogAlertSink()] if args.notify else None, notify=args.notify)
        print(json.dumps(report, indent=2, default=str))
        return 1 if report["critical_count"] else 0
    if args.command == "inspect-job":
        if not args.job_id:
            parser.error("inspect-job requires --job-id.")
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        service = JobService(store)
        job = service.get(args.job_id.strip())
        report = {"job": job, "related": {
            "lead_id": job.get("lead_id"),
            "project": store.state["projects"].get(job.get("project_id")) if job.get("project_id") else None,
            "build": store.state["builds"].get(job.get("build_id")) if job.get("build_id") else None,
            "parent_job_id": job.get("parent_job_id"),
        }}
        if args.history:
            report["executions"] = service.executions_for(job["job_id"])
            report["retry_eligibility"] = service.retry_eligibility(job["job_id"])
        print(json.dumps(report, indent=2, default=str))
        return 0 if job.get("status") == "COMPLETED" else 1
    if args.command == "check-workspace":
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        print(json.dumps(store.check_local_state(), indent=2))
        return 0
    if args.command == "recover-workspace":
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        print(json.dumps(store.recover_local_state(), indent=2))
        return 0
    store = TrackingStore(args.state_path, database_url=args.database_url)
    service = JobService(store)
    if args.command == "create-job":
        if not args.job_type or not args.job_id:
            parser.error("create-job requires --job-type and --job-id.")
        job = service.create(
            args.job_type.strip().upper(),
            job_id=args.job_id.strip(),
            lead_id=args.lead_id,
            project_id=args.project_id,
            build_id=args.build_id,
            input=json.loads(args.input_json),
            dry_run=True,
        )
        print(json.dumps(job, indent=2))
        return 0
    if args.command == "run-job":
        if not args.job_id:
            parser.error("run-job requires --job-id.")
        worker = LocalPipelineWorker(output_root=args.output_root)
        result = service.run(args.job_id.strip(), worker)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "COMPLETED" else 1
    if args.command == "retry-job":
        if not args.job_id:
            parser.error("retry-job requires --job-id.")
        worker = LocalPipelineWorker(output_root=args.output_root)
        retry_input = json.loads(args.input_json) if args.input_json and args.input_json != "{}" else None
        result = service.retry(args.job_id.strip(), input=retry_input, worker=worker)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "COMPLETED" else 1
    if args.command == "recover-stale":
        data = service.recover_stale(older_than_seconds=args.older_than_seconds)
        print(json.dumps(data, indent=2))
        return 1 if data else 0
    jobs = list(service.store.state["jobs"].values())
    if args.status:
        wanted = args.status.strip().upper()
        jobs = [job for job in jobs if job.get("status") == wanted]
    if args.attention_only:
        jobs = [job for job in jobs if str(job.get("status")) in ATTENTION_STATUSES
                or job.get("requires_operator_action")]
    if args.brief:
        from .observability import recorded_age_seconds

        jobs = [{
            "job_id": job.get("job_id"), "job_type": job.get("job_type"), "status": job.get("status"),
            "lead_id": job.get("lead_id"), "project_id": job.get("project_id"), "build_id": job.get("build_id"),
            "error_code": job.get("error_code") or job.get("last_error_code"),
            "attempts": job.get("attempts"), "max_attempts": job.get("max_attempts"),
            "retry_exhausted": bool(job.get("max_attempts")) and int(job.get("attempts") or 0) >= int(job.get("max_attempts") or 0),
            "retry_count": int(job.get("retry_count") or 0),
            "requires_operator_action": bool(job.get("requires_operator_action")),
            "age_seconds": recorded_age_seconds(job.get("completed_at") or job.get("updated_at")),
            "next_action": job.get("next_action"),
            "error": job.get("error"),
        } for job in jobs]
    print(json.dumps({"count": len(jobs), "jobs": jobs}, indent=2, default=str) if (args.status or args.attention_only or args.brief)
          else json.dumps(jobs, indent=2, default=str))
    return 1 if (args.attention_only and jobs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
