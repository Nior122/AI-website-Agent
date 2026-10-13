"""Explicit local commands for inspecting, creating, running, and recovering orchestration state."""
from __future__ import annotations
import argparse
import json
from .api import serve
from .jobs import JobService
from .workers import LocalPipelineWorker
from tracking.store import TrackingStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Website Agent orchestration controls")
    parser.add_argument(
        "command",
        choices=["serve", "recover-stale", "list-jobs", "check-workspace", "recover-workspace", "create-job", "run-job", "retry-job"],
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
    data = service.recover_stale(older_than_seconds=args.older_than_seconds) if args.command == "recover-stale" else list(service.store.state["jobs"].values())
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
