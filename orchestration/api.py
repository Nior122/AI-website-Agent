"""Optional local HTTP adapter for a separately managed coordinator such as n8n.

No server starts on import. Deploy behind the user's chosen private network or
reverse proxy; this small adapter intentionally has no public-internet defaults.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse

from tracking.store import TrackingStore
from .health import system_health
from .jobs import JobService, JobWorker
from .workers import LocalPipelineWorker

ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
ALLOWED_API_JOB_TYPES = {"SCOUT", "RESEARCH", "QUALIFY", "BUILD", "VALIDATE"}


def _enrich_job(job: dict) -> dict:
    status = str(job.get("status") or "")
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 3)
    recoverable = status in {"FAILED", "NEEDS_REVIEW"} and attempts < max_attempts
    if status == "COMPLETED":
        category = "SUCCESSFUL"
    elif status in {"PENDING", "QUEUED", "RUNNING"}:
        category = "PENDING"
    elif recoverable:
        category = "RECOVERABLE"
    else:
        category = "FAILED"
    return {
        **job,
        "recoverable": recoverable,
        "execution_category": category,
    }


def make_handler(
    service: JobService,
    token: str,
    *,
    worker: JobWorker | bool | None = None,
    output_root: str | Path = "output",
    export_root: str | Path = "preview-export",
    repository_root: str | Path = ".",
):
    repo_path = Path(repository_root)
    out_path = Path(output_root)
    exp_path = Path(export_root)
    active_worker: JobWorker | None
    if worker is False:
        active_worker = None
    elif worker is None:
        active_worker = LocalPipelineWorker(repository_root=repo_path, output_root=out_path)
    else:
        active_worker = worker
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            return bool(token) and self.headers.get("Authorization") == f"Bearer {token}"

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if not length:
                return {}
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("Request body must be a JSON object.")
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._json(401, {"error": "Unauthorized"})
            parsed_url = urlparse(self.path)
            path = [seg for seg in parsed_url.path.strip("/").split("/") if seg]
            query = parse_qs(parsed_url.query)
            with lock:
                try:
                    if path == ["health"]:
                        return self._json(200, {
                            "api_status": "OK",
                            "worker_registered": active_worker is not None,
                            "worker_name": getattr(active_worker, "name", None),
                            "health": system_health(service.store),
                        })
                    if path == ["jobs"]:
                        jobs = [_enrich_job(j) for j in service.store.state["jobs"].values()]
                        status_filter = (query.get("status") or [None])[0]
                        type_filter = (query.get("job_type") or [None])[0]
                        cat_filter = (query.get("category") or [None])[0]
                        if status_filter:
                            jobs = [j for j in jobs if j.get("status") == status_filter.upper()]
                        if type_filter:
                            jobs = [j for j in jobs if j.get("job_type") == type_filter.upper()]
                        if cat_filter:
                            jobs = [j for j in jobs if j.get("execution_category") == cat_filter.upper()]
                        return self._json(200, {"jobs": jobs, "count": len(jobs)})
                    if len(path) == 2 and path[0] == "jobs":
                        return self._json(200, _enrich_job(service.get(path[1])))
                    if path == ["leads"]:
                        return self._json(200, {"leads": list(service.store.state["leads"].values())})
                    if len(path) == 2 and path[0] == "leads":
                        return self._json(200, service.store.state["leads"][path[1]])
                    if path == ["projects"]:
                        return self._json(200, service.store.status_summary())
                    if len(path) == 2 and path[0] == "projects":
                        return self._json(200, service.store.snapshot(path[1]))
                    if len(path) == 3 and path[0] == "projects" and path[2] == "snapshot":
                        return self._json(200, service.store.snapshot(path[1]))
                    if len(path) == 3 and path[0] == "projects" and path[2] == "review-package":
                        project_id = path[1]
                        snap = service.store.snapshot(project_id)
                        lead_id = snap["lead"]["lead_id"]
                        req_export = (query.get("export_dir") or [None])[0]
                        target_export = Path(req_export) if req_export else (exp_path / f"{lead_id}-{project_id}")
                        verification = service.store.verify_review_package(
                            project_id,
                            target_export,
                            repository_root=repo_path,
                        )
                        code = 200 if verification.get("status") == "VALID" else 409
                        return self._json(code, {
                            "project_id": project_id,
                            "lead_id": lead_id,
                            "build_id": snap["project"].get("assigned_build_id"),
                            "validation_id": (snap["latestValidation"] or {}).get("validation_id"),
                            "review_status": snap["project"].get("review_status"),
                            "verification": verification,
                        })
                    return self._json(404, {"error": "Not found"})
                except (KeyError, ValueError) as error:
                    return self._json(404, {"error": str(error)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._json(401, {"error": "Unauthorized"})
            path = [seg for seg in urlparse(self.path).path.strip("/").split("/") if seg]
            with lock:
                try:
                    payload = self._body()
                    if path == ["jobs"]:
                        if "job_type" not in payload or "job_id" not in payload:
                            raise ValueError("POST /jobs requires 'job_type' and 'job_id'.")
                        run_immediately = bool(payload.pop("run", False))
                        job_type = str(payload.pop("job_type")).strip().upper()
                        if job_type not in ALLOWED_API_JOB_TYPES:
                            raise ValueError(
                                f"Job type '{job_type}' is not permitted via the n8n automation API; "
                                f"allowed stages: {sorted(ALLOWED_API_JOB_TYPES)}. Human operator review is required before deployment or outreach."
                            )
                        job = service.create(job_type, **payload)
                        if run_immediately:
                            if active_worker is None:
                                return self._json(409, {"error": "No worker is registered by the HTTP adapter. Run an approved worker process."})
                            job = service.run(job["job_id"], active_worker)
                        return self._json(201, _enrich_job(job))

                    if path == ["jobs", "recover-stale"]:
                        older_than = int(payload.get("older_than_seconds", 900))
                        recovered = [_enrich_job(j) for j in service.recover_stale(older_than_seconds=older_than)]
                        return self._json(200, {
                            "recovered_count": len(recovered),
                            "recovered_jobs": recovered,
                        })

                    if len(path) == 3 and path[0] == "jobs" and path[2] == "run":
                        if active_worker is None:
                            return self._json(409, {"error": "No worker is registered by the HTTP adapter. Run an approved worker process."})
                        job = service.run(path[1], active_worker)
                        return self._json(200, _enrich_job(job))

                    if len(path) == 3 and path[0] == "jobs" and path[2] == "next":
                        if "next_job_id" not in payload:
                            raise ValueError("POST /jobs/:id/next requires 'next_job_id'.")
                        run_immediately = bool(payload.pop("run", False))
                        next_job = service.create_next(path[1], **payload)
                        if next_job.get("job_type") not in ALLOWED_API_JOB_TYPES:
                            raise ValueError(
                                f"Next job type '{next_job.get('job_type')}' is not permitted via the n8n automation API."
                            )
                        if run_immediately:
                            if active_worker is None:
                                return self._json(409, {"error": "No worker is registered by the HTTP adapter. Run an approved worker process."})
                            next_job = service.run(next_job["job_id"], active_worker)
                        return self._json(201, _enrich_job(next_job))

                    if len(path) == 3 and path[0] == "jobs" and path[2] == "retry":
                        run_immediately = bool(payload.get("run", True))
                        retry_input = payload.get("input")
                        reset_attempts = bool(payload.get("reset_attempts", False))
                        if run_immediately and active_worker is None:
                            return self._json(409, {"error": "No worker is registered by the HTTP adapter. Run an approved worker process."})
                        job = service.retry(
                            path[1],
                            input=retry_input,
                            reset_attempts=reset_attempts,
                            worker=active_worker if run_immediately else None,
                        )
                        return self._json(200, _enrich_job(job))

                    if path == ["leads", "deduplicate"]:
                        dup = service.store.find_duplicate(payload)
                        existing_lead = service.store.state["leads"].get(dup["lead_id"]) if dup.get("lead_id") else None
                        existing_proj = (
                            next(
                                (
                                    p
                                    for p in service.store.state["projects"].values()
                                    if p["lead_id"] == dup["lead_id"] and p.get("project_status") == "ACTIVE"
                                ),
                                None,
                            )
                            if dup.get("lead_id")
                            else None
                        )
                        return self._json(200, {
                            "match": dup["match"],
                            "lead_id": dup["lead_id"],
                            "is_duplicate": dup["match"] == "EXACT_MATCH",
                            "existing_lead": existing_lead,
                            "existing_project_id": existing_proj["project_id"] if existing_proj else None,
                        })

                    if len(path) == 3 and path[0] == "projects" and path[2] == "review-package":
                        project_id = path[1]
                        snap = service.store.snapshot(project_id)
                        lead_id = snap["lead"]["lead_id"]
                        req_export = payload.get("export_dir")
                        target_export = Path(req_export) if req_export else (exp_path / f"{lead_id}-{project_id}")
                        pkg = service.store.build_review_package(
                            project_id,
                            export_dir=target_export,
                            repository_root=repo_path,
                        )
                        verification = service.store.verify_review_package(
                            project_id,
                            target_export,
                            repository_root=repo_path,
                        )
                        snap_after = service.store.snapshot(project_id)
                        return self._json(200, {
                            "project_id": project_id,
                            "lead_id": lead_id,
                            "build_id": snap_after["project"].get("assigned_build_id"),
                            "validation_id": (snap_after["latestValidation"] or {}).get("validation_id"),
                            "current_stage": snap_after["project"].get("current_stage"),
                            "workflow_state": snap_after["workflow_state"],
                            "review_status": snap_after["project"].get("review_status"),
                            "human_review_pending": (
                                snap_after["project"].get("current_stage") == "READY_FOR_REVIEW"
                                and snap_after["project"].get("review_status") == "PENDING"
                            ),
                            "review_package": pkg,
                            "verification": verification,
                        })

                    return self._json(404, {"error": "Not found"})
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    return self._json(400, {"error": str(error)})

        def log_message(self, _format: str, *_args) -> None:
            """Avoid logging headers or request bodies, which could contain credentials."""

    return Handler


def serve(
    *,
    state_path: str = "data/tracking/state.json",
    host: str = "127.0.0.1",
    port: int = 8787,
    output_root: str = "output",
    export_root: str = "preview-export",
    repository_root: str = ".",
) -> None:
    if host not in ALLOWED_LOOPBACK_HOSTS:
        raise ValueError(f"Refusing non-loopback bind host '{host}'; allowed hosts: {sorted(ALLOWED_LOOPBACK_HOSTS)}.")
    token = os.environ.get("AI_WEBSITE_AGENT_API_TOKEN")
    if not token:
        raise RuntimeError("AI_WEBSITE_AGENT_API_TOKEN is required; server not started.")
    service = JobService(TrackingStore(state_path))
    worker = LocalPipelineWorker(repository_root=repository_root, output_root=output_root)
    ThreadingHTTPServer(
        (host, port),
        make_handler(
            service,
            token,
            worker=worker,
            output_root=output_root,
            export_root=export_root,
            repository_root=repository_root,
        ),
    ).serve_forever()
