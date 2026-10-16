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


def telegram_config_cached() -> dict:
    """The Telegram configuration for this process, secrets excluded. Thin wrapper for readability."""
    from integrations.telegram import telegram_config

    return telegram_config()


def _telegram_api(config: dict):
    """A live Bot API client for the configured token, or None when there is nothing to talk to."""
    import os

    from integrations.telegram import TelegramBotApi

    token = (os.environ.get("AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return None
    return TelegramBotApi(token, api_root=config["api_root"], timeout=10.0)


def _telegram_commands() -> list[str]:
    from .telegram_bot import COMMANDS

    return list(COMMANDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Website Agent orchestration controls")
    parser.add_argument(
        "command",
        choices=[
            "serve", "recover-stale", "list-jobs", "check-workspace", "recover-workspace",
            "create-job", "run-job", "retry-job",
            # Operational controls: what is healthy, what is stuck, and what a human must clear.
            "health", "attention", "inspect-job", "deployment-status", "check-alerts", "preflight",
            "check-n8n",
            # Operator channels and the brake: who may reach the pipeline, and whether it runs at all.
            "hold-automation", "release-automation", "automation-status",
            "telegram-check", "telegram-command", "telegram-serve", "telegram-notify",
            "email-check", "email-poll",
        ],
        help="serve runs the API; health/attention/inspect-job/deployment-status/check-alerts/preflight "
             "report operational state without changing it; recover-stale/recover-workspace restore "
             "recorded state; create-job/run-job/retry-job act on the queue.",
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
    parser.add_argument("--project-id", default=None, help="Optional project_id for create-job; for preflight, include that project's durable deployment gate.")
    parser.add_argument("--build-id", default=None, help="Optional build_id for create-job.")
    parser.add_argument("--input-json", default="{}", help="JSON object string for job input.")
    parser.add_argument("--output-root", default="output", help="Output directory for LocalPipelineWorker BUILD jobs.")
    parser.add_argument("--export-root", default="preview-export", help="Directory review packages may be written under; the API refuses export paths outside it.")
    parser.add_argument("--repository-root", default=".", help="Workspace root used by health reports (defaults to the current directory).")
    parser.add_argument("--workflow", action="append", default=None, metavar="PATH",
                        help="check-n8n: a specific exported workflow file to validate; repeatable. Defaults to every n8n/workflows/*.json.")
    parser.add_argument("--status", default=None, help="list-jobs: only jobs in this status (e.g. FAILED, NEEDS_REVIEW, BLOCKED).")
    parser.add_argument("--attention-only", action="store_true", help="list-jobs: only jobs a human must clear (FAILED, NEEDS_REVIEW, BLOCKED).")
    parser.add_argument("--brief", action="store_true", help="list-jobs/attention: compact operator view instead of full records.")
    parser.add_argument("--history", action="store_true", help="inspect-job: include the recorded execution attempts and retry eligibility.")
    parser.add_argument("--notify", action="store_true", help="check-alerts: emit each critical alert as a structured CRITICAL log event. No external channel is contacted.")
    parser.add_argument("--telegram", action="store_true", help="check-alerts: also deliver each alert to the configured Telegram operator chat. Requires the bot token and allow-list in the environment.")
    parser.add_argument("--actor", default=None, help="hold-automation/release-automation: who is responsible, recorded verbatim on the control record (e.g. 'grace@work' or 'telegram:123').")
    parser.add_argument("--reason", default=None, help="hold-automation: why dispatch is being held, so a later reader knows what they are looking at.")
    parser.add_argument("--note", default=None, help="A short note recorded with an action (never a credential, never a private message).")
    parser.add_argument("--chat-id", type=int, default=None, help="telegram-notify: deliver to this chat instead of the first allow-listed one.")
    parser.add_argument("--limit", type=int, default=None, help="Notification or list size bound.")
    parser.add_argument("--command-text", default=None, help="telegram-command: the exact command line an operator would type, e.g. '/status job-0001'.")
    parser.add_argument("--user-id", type=int, default=None, help="telegram-command: the Telegram user id to authorize against the allow-list. An unlisted id is refused before anything runs.")
    parser.add_argument("--once", action="store_true", help="telegram-serve: one getUpdates cycle instead of a service loop.")
    parser.add_argument("--cycles", type=int, default=None, help="telegram-serve: stop after this many cycles (supervised runs and containers).")
    parser.add_argument("--poll-timeout", type=float, default=20.0, help="telegram-serve: long-poll seconds per getUpdates call (Telegram caps it at 50).")
    parser.add_argument("--dry-run", action="store_true", help="email-poll: fetch, normalize, and match without recording anything.")
    parser.add_argument("--max-messages", type=int, default=None, help="email-poll: how many messages one cycle may read (bounded by the adapter's own ceiling).")
    parser.add_argument("--retry-pending", action="store_true", help="email-poll: re-submit the intake's unmatched and failed events instead of fetching new mail.")
    parser.add_argument("--probe", action="store_true", help="telegram-check/email-check: actually ask the provider (getMe / one messages.list). Off by default so a check never sends or reads anything unexpectedly.")
    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(
            state_path=args.state_path,
            host=args.host,
            port=args.port,
            allow_non_loopback=args.allow_non_loopback,
            output_root=args.output_root,
            export_root=args.export_root,
            repository_root=args.repository_root,
            database_url=args.database_url,
        )
        return 0
    if args.command == "check-n8n":
        # Validation only, deliberately: this reports whether the exported workflows are importable and
        # whether this machine supplies the variables they read. Executing a workflow graph would create
        # jobs, so `create-job`/`run-job` remain the way to drive the pipeline.
        from .n8n_runner import workflow_readiness

        root = Path(args.repository_root)
        if args.workflow:
            paths = [Path(item) for item in args.workflow]
        else:
            directory = root / "n8n" / "workflows"
            paths = sorted(directory.glob("*.json"))
            if not paths:
                print(json.dumps({"status": "blocked", "blockers": [
                    f"No workflow JSON found in {directory}. Import the repository copies into n8n, or "
                    "pass --workflow for an exported file."]}, indent=2))
                return 1
        report = workflow_readiness(paths)
        blockers = [f"{item['file']}: {problem}" for item in report["workflows"] for problem in item["problems"]]
        if report["missing_environment"]:
            blockers.append(
                "n8n reads these variables and this environment does not define them: "
                + ", ".join(report["missing_environment"])
            )
        result = {
            "status": "blocked" if blockers else "ready",
            "workflows_checked": report["checked_count"],
            "importable": report["importable_count"],
            "missing_environment": report["missing_environment"],
            "needs_manual_edit": report["workflows_needing_manual_edit"],
            "requests_sent": report["requests_sent"],
            "blockers": blockers,
            "next_action": (
                "Import the file in the n8n UI (Workflows -> Import from File), set the variables above in "
                "the environment n8n runs with, edit any starter placeholder, and leave the workflow "
                "inactive until an operator decides to activate it."
            ),
        }
        print(json.dumps(result, indent=2))
        return 1 if blockers else 0

    if args.command == "preflight":
        from .health import integration_preflight

        from tracking.database import DatabasePersistenceError

        store, storage_error = None, None
        try:
            store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        except DatabasePersistenceError as error:
            # A production configuration with no reachable database fails closed at construction. The
            # preflight's job is to say so in the report the operator asked for, with the next command to run.
            storage_error = str(error)
        report = integration_preflight(
            store,
            repository_root=args.repository_root,
            host=args.host,
            port=args.port,
            allow_non_loopback=args.allow_non_loopback,
            # Reuses the existing --project-id flag: for preflight it means "include this project's
            # durable deployment-gate answer", which is the same project the other controls act on.
            project_id=args.project_id,
            storage_error=storage_error,
        )
        print(json.dumps(report, indent=2))
        return int(report["exit_code"])
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
        # The alert pipeline stays the decision-maker throughout: `--telegram` only adds a sink, so dedupe,
        # bounding, and sanitizing are the same code the log path runs and cannot be bypassed by asking for a
        # phone message instead. Suppression that used to live only in memory is given a durable record here,
        # because a cron run has no memory of the last one and an operator should not be pinged every five
        # minutes about one stuck job.
        store = TrackingStore(args.state_path, auto_bootstrap_workspace=False, database_url=args.database_url)
        from .telegram_bot import TelegramAlertSink, load_alert_seen, save_alert_seen

        telegram = telegram_config_cached()
        api = _telegram_api(telegram) if args.telegram else None
        if args.telegram and api is None:
            print(json.dumps({"status": "blocked", "code": "TELEGRAM_NOT_CONFIGURED",
                              "next_action": telegram["next_action"]}, indent=2))
            return 1
        sinks = [LogAlertSink()] if args.notify else []
        if api is not None:
            sinks.append(TelegramAlertSink(api, chat_id=args.chat_id, fallback_chats=telegram["allowed_chat_ids"]))
        seen = load_alert_seen(store) if sinks else set()
        before = set(seen)
        report = check_alerts(store, repository_root=Path(args.repository_root), sinks=sinks or None,
                             notify=bool(args.notify), seen=seen)
        report["sinks"] = [sink.name for sink in sinks]
        report["dedupe_window"] = "recorded until the alert's own recorded_at changes, or the window expires"
        if sinks and seen != before:
            save_alert_seen(store, seen)
            report["suppression_persisted"] = len(seen)
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

    # -- operator channels and the automation hold -------------------------------------------------------
    if args.command in {"hold-automation", "release-automation", "automation-status"}:
        from .controls import blocked_by_hold, hold_view, release_hold, set_hold

        from tracking.inbound import InboundEventError  # noqa: F401 - keeps the intake import surface warm for --help

        if args.command == "hold-automation":
            if not args.actor:
                parser.error("hold-automation requires --actor: a brake somebody must be named for.")
            view = set_hold(store, actor=args.actor, reason=args.reason, note=args.note)
        elif args.command == "release-automation":
            if not args.actor:
                parser.error("release-automation requires --actor: record who put the system back in motion.")
            view = release_hold(store, actor=args.actor, note=args.note)
        else:
            view = hold_view(store, changed=False)
        view["blocked_jobs_by_hold"] = blocked_by_hold(store, limit=25)
        print(json.dumps(view, indent=2, default=str))
        return 1 if view.get("unreadable") else 0

    if args.command in {"telegram-check", "telegram-command", "telegram-serve", "telegram-notify"}:
        from .telegram_bot import TelegramOperatorBot

        from integrations.telegram import (TelegramBotApi, telegram_config)

        config = telegram_config()
        if args.command == "telegram-check":
            report = dict(config)
            report["token"] = None  # never this, whatever happens
            if config["token_present"] and args.probe:
                try:
                    api = TelegramBotApi(os.environ.get("AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN", "").strip(),
                                         api_root=config["api_root"], timeout=10.0)
                    identity = api.get_me()
                    report["bot"] = {"username": identity.get("username"), "id": identity.get("id"),
                                   "verified": True}
                except Exception as error:  # noqa: BLE001 - a probe failure is the answer, not a crash.
                    from .telegram_bot import describe_provider_error

                    report.update(describe_provider_error(error))
                    report["bot"] = {"verified": False}
            elif config["token_present"]:
                report["bot"] = {"verified": False, "note": "pass --probe to ask Telegram whether this token works"}
            report["commands_supported"] = sorted(_telegram_commands())
            report["requests_sent"] = bool(args.probe and config["token_present"])
            print(json.dumps(report, indent=2))
            return 0 if config["configured"] else 1
        allow_list = bool(config["allowed_user_ids"] or config["allowed_chat_ids"])
        if args.command == "telegram-command":
            # Rehearsing one command offline needs an allow-list, not a bot token: the rule under test is
            # authorization, and with no transport nothing can leave the machine anyway.
            if not allow_list:
                print(json.dumps({"configured": False, "code": config["code"],
                                  "next_action": config["next_action"], "token_present": config["token_present"]},
                                 indent=2))
                return 1
        elif not config["configured"] and args.command != "telegram-serve":
            print(json.dumps({"configured": False, "code": config["code"], "next_action": config["next_action"],
                              "token_present": config["token_present"]}, indent=2))
            return 1
        api = None
        if args.command != "telegram-check" and config["token_present"]:
            api = TelegramBotApi(os.environ.get("AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN", "").strip(),
                                 api_root=config["api_root"], timeout=max(1.0, float(args.poll_timeout)))
        bot = TelegramOperatorBot(api, store=store, allowed_user_ids=config["allowed_user_ids"],
                                 allowed_chat_ids=config["allowed_chat_ids"])
        if args.command == "telegram-command":
            if not args.command_text or args.user_id is None:
                parser.error("telegram-command requires --command-text and --user-id.")
            answer = bot.dispatch(args.command_text, user_id=args.user_id,
                                 chat_id=args.chat_id, deliver=args.telegram)
            answer["transport"] = ("bot api configured" if api is not None
                                   else "no bot token - the answer was computed and printed, nothing was sent")
            print(json.dumps(answer, indent=2, default=str))
            return 0 if answer.get("code") not in {"AUTHORIZATION_REFUSED", "COMMAND_FAULT"} else 1
        if args.command == "telegram-notify":
            report = bot.notify_once(chat_id=args.chat_id, limit=args.limit or 6)
            print(json.dumps(report, indent=2, default=str))
            return 0 if report.get("code") in {"NOTIFIED", "NOTHING_NEW"} else 1
        outcome = bot.run_forever(cycle_limit=(1 if args.once else args.cycles),
                                 timeout=float(args.poll_timeout))
        print(json.dumps(outcome, indent=2, default=str))
        return 0

    if args.command in {"email-check", "email-poll"}:
        from integrations.gmail import (GmailClient, GmailError, describe_error, gmail_config)

        config = gmail_config()
        if args.command == "email-check":
            report = dict(config)
            report["requests_sent"] = bool(args.probe and config["configured"])
            if config["configured"] and args.probe:
                # One message id, nothing more: the probe answers "does this credential work", not "what is in
                # the mailbox", and no message body is fetched or printed.
                try:
                    client = GmailClient.from_environment()
                    listing = client.list_messages(config["query"], max_results=1)
                    report["probe"] = {"reachable": True, "result_size_estimate": listing.get("result_size_estimate"),
                                      "sample": "one message id was listed; no body was read"}
                    report["client"] = client.status()
                except (GmailError, ValueError) as error:
                    report["probe"] = {"reachable": False, **describe_error(error)}
            elif config["configured"]:
                report["probe"] = {"reachable": None, "note": "pass --probe to ask Gmail whether this credential works"}
            print(json.dumps(report, indent=2, default=str))
            return 0 if config["configured"] else 1
        if not config["configured"]:
            print(json.dumps({"configured": False, "code": config["code"], "missing": config["missing"],
                              "next_action": config["next_action"]}, indent=2))
            return 1
        from .api import build_email_poller

        from .telegram_bot import TelegramOperatorBot

        notifier = None
        telegram = telegram_config_cached()
        if telegram.get("configured"):
            bot = TelegramOperatorBot(_telegram_api(telegram), store=store,
                                     allowed_user_ids=telegram["allowed_user_ids"],
                                     allowed_chat_ids=telegram["allowed_chat_ids"])
            notifier = lambda: bot.notify_once()  # noqa: E731 - an injected one-shot, not a helper in disguise
        try:
            poller = build_email_poller(store, max_messages=args.max_messages)
            poller.notifier = notifier
            if args.retry_pending:
                report = poller.retry_pending(limit=args.limit or 25)
            else:
                report = poller.poll_once(dry_run=args.dry_run)
        except (GmailError, ValueError) as error:
            report = {"recorded": 0, "fetched": 0, **describe_error(error),
                      "next_action": getattr(error, "next_action", None) or "Retry the cycle; nothing was skipped."}
            print(json.dumps(report, indent=2, default=str))
            return 1
        print(json.dumps(report, indent=2, default=str))
        return 1 if report.get("error_code") else 0

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
