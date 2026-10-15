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

from tracking.database import DatabasePersistenceError, is_production_mode, sanitize_db_error
from tracking.store import TrackingStore
from .health import readiness_for, system_health, verify_storage
from .jobs import JobService, JobWorker
from .workers import LocalPipelineWorker

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
ALLOWED_PRODUCTION_HOSTS = {"0.0.0.0"}
ALLOWED_API_JOB_TYPES = {"SCOUT", "RESEARCH", "QUALIFY", "BUILD", "VALIDATE"}


def resolve_bind_config(
    host: str | None = None,
    port: int | None = None,
    *,
    allow_non_loopback: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[str, int, bool]:
    """Resolve and validate HTTP bind host and port with localhost defaults and Render PORT support."""
    source = env if env is not None else os.environ
    resolved_host = str(
        host
        or source.get("AI_WEBSITE_AGENT_BIND_HOST")
        or source.get("AI_WEBSITE_AGENT_HOST")
        or DEFAULT_BIND_HOST
    ).strip()
    if port is not None:
        resolved_port = int(port)
    else:
        raw_port = str(source.get("PORT") or source.get("AI_WEBSITE_AGENT_API_PORT") or DEFAULT_PORT).strip()
        resolved_port = int(raw_port)
    if resolved_port < 0 or resolved_port > 65535:
        raise ValueError(f"Invalid port: {resolved_port}")

    env_non_loopback = str(source.get("AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    effective_allow = bool(allow_non_loopback or env_non_loopback)
    if resolved_host not in ALLOWED_LOOPBACK_HOSTS:
        if not effective_allow or resolved_host not in ALLOWED_PRODUCTION_HOSTS:
            raise ValueError(
                f"Refusing non-loopback bind host '{resolved_host}'; allowed loopback hosts: "
                f"{sorted(ALLOWED_LOOPBACK_HOSTS)}. Set AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK=true "
                "(or pass --allow-non-loopback) with host '0.0.0.0' for Render production deployment."
            )
    return resolved_host, resolved_port, effective_allow


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
            parsed_url = urlparse(self.path)
            path = [seg for seg in parsed_url.path.strip("/").split("/") if seg]
            if path == ["healthz"]:
                # Lightweight Render liveness probe: does not expose operational data or secrets.
                return self._json(200, {"status": "ok"})
            if not self._authorized():
                return self._json(401, {"error": "Unauthorized"})
            query = parse_qs(parsed_url.query)
            with lock:
                try:
                    if getattr(service.store, "db_backend", None) is not None and hasattr(service.store, "refresh"):
                        service.store.refresh()
                    registered_workers = list(
                        (service.store.state.get("workers") or {}).values()
                    )
                    available_registered_workers = [
                        w for w in registered_workers
                        if w.get("enabled")
                        and w.get("status") == "IDLE"
                        and w.get("health_status") == "AVAILABLE"
                    ]
                    if path == ["health"]:
                        return self._json(200, {
                            "api_status": "OK",
                            "worker_registered": active_worker is not None,
                            "worker_name": getattr(active_worker, "name", None),
                            "api_execution_worker_configured": active_worker is not None,
                            "registered_worker_count": len(registered_workers),
                            "available_registered_worker_count": len(available_registered_workers),
                            "health": system_health(service.store),
                        })
                    if path == ["readiness"]:
                        db_backend = getattr(service.store, "db_backend", None)
                        storage = verify_storage(service.store)
                        if storage["status"] != "healthy":
                            # A required database that cannot be reached or verified is never READY.
                            return self._json(503, {
                                "status": "NOT_READY",
                                "reason": storage["error"] or "Storage backend is unavailable.",
                                "storage_backend": storage["backend"],
                                "production_mode": storage["production_mode"],
                            })
                        return self._json(200, {
                            "status": "READY",
                            "storage_backend": db_backend.dialect if db_backend is not None else "json",
                            "production_mode": is_production_mode(),
                            "worker_registered": active_worker is not None,
                            "worker_name": getattr(active_worker, "name", None),
                            "api_execution_worker_configured": active_worker is not None,
                            "registered_worker_count": len(registered_workers),
                            "available_registered_worker_count": len(available_registered_workers),
                            "storage_backend_status": storage["status"],
                        })
                    if len(path) == 3 and path[0] == "jobs" and path[2] == "readiness":
                        return self._json(200, readiness_for(service.store, service, path[1]))
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
                    if len(path) == 3 and path[0] == "projects" and path[2] == "artifact":
                        project_id = path[1]
                        snap = service.store.snapshot(project_id)
                        assigned_build_id = snap["project"].get("assigned_build_id")
                        if not assigned_build_id:
                            return self._json(404, {"error": f"Project {project_id} has no assigned build."})
                        art = service.store.get_or_materialize_build_artifact(
                            assigned_build_id,
                            repository_root=repo_path,
                            verify_hash=True,
                        )
                        durable_rec = service.store.get_build_artifact(assigned_build_id, verify_hash=True) or {}
                        return self._json(200, {
                            "project_id": project_id,
                            "lead_id": snap["lead"]["lead_id"],
                            "build_id": assigned_build_id,
                            "review_status": snap["project"].get("review_status"),
                            "artifact_path": art["artifact_path"],
                            "html_sha256": art["html_sha256"],
                            "html_bytes": art["html_bytes"],
                            "html_content": art["html_content"],
                            "materialized": art["materialized"],
                            "review_package": durable_rec.get("review_package_json"),
                        })
                    if len(path) == 3 and path[0] == "projects" and path[2] == "deployment-authorization":
                        # Read-only view of the server-side publish gate: what a real deployment would be
                        # allowed to do right now, and exactly which condition blocks it if it is not.
                        return self._json(200, service.store.authorize_preview_deployment(
                            path[1], build_id=(query.get("build_id") or [None])[0]
                        ))
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
                except DatabasePersistenceError as db_error:
                    return self._json(503, {"error": "Database persistence error", "detail": str(db_error)})
                except (KeyError, ValueError) as error:
                    return self._json(404, {"error": str(error)})
                except Exception as error:  # Never close the socket without a structured answer.
                    return self._json(500, {"error": "Unhandled request failure", "detail": sanitize_db_error(error)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return self._json(401, {"error": "Unauthorized"})
            path = [seg for seg in urlparse(self.path).path.strip("/").split("/") if seg]
            with lock:
                try:
                    if getattr(service.store, "db_backend", None) is not None and hasattr(service.store, "refresh"):
                        service.store.refresh()
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

                    if len(path) == 3 and path[0] == "projects" and path[2] == "review":
                        # Explicit human review decision recorded through the automation API. The decision
                        # is bound to the currently assigned build by a confirmation string the operator can
                        # only produce after reading it, so a stale or misrouted request cannot approve a
                        # build. Reviewer identity is recorded verbatim and never asserted as verified.
                        project_id = path[1]
                        project = service.store.state["projects"].get(project_id)
                        if not project:
                            raise KeyError(f"Unknown project: {project_id}")
                        decision = str(payload.get("decision") or "").strip().upper()
                        notes = payload.get("notes")
                        confirm = payload.get("confirm")
                        reviewer = payload.get("reviewer") or payload.get("reviewer_id")
                        missing = [name for name, value in (("decision", decision), ("notes", notes), ("confirm", confirm), ("reviewer", reviewer)) if not value]
                        if missing:
                            return self._json(400, {
                                "error": "POST /projects/:id/review requires " + ", ".join(f"'{name}'" for name in missing)
                                         + ". Record the decision with the build id you actually reviewed: "
                                         + "confirm=<project_id>:<build_id>:<DECISION>.",
                            })
                        assigned_build_id = project.get("assigned_build_id")
                        expected_confirmation = f"{project_id}:{assigned_build_id}:{decision}"
                        if str(confirm) != expected_confirmation:
                            return self._json(409, {
                                "error": "Review confirmation does not match the project's currently assigned build.",
                                "expected_confirmation": expected_confirmation,
                                "received_confirmation": str(confirm),
                                "detail": (
                                    "A decision about an older build cannot approve a newer one. Read "
                                    f"GET /projects/{project_id} for the assigned build, then re-send with "
                                    "confirm=" + expected_confirmation + "."
                                ),
                            })
                        reviewer_value: object = str(reviewer)
                        if payload.get("reviewer_type"):
                            reviewer_value = {
                                "reviewer_id": str(reviewer),
                                "reviewer_type": str(payload.get("reviewer_type")),
                                "identity_verified": bool(payload.get("identity_verified", False)),
                            }
                        updated = service.store.record_review(
                            project_id,
                            decision,
                            notes,
                            reviewer=reviewer_value,
                            require_notes=True,
                            build_id=payload.get("build_id"),
                            action=payload.get("action"),
                            identity_source="orchestration-api",
                        )
                        snap_after = service.store.snapshot(project_id)
                        history = service.store.get_review_history(project_id)
                        return self._json(200, {
                            "project_id": project_id,
                            "lead_id": updated["lead_id"],
                            "build_id": assigned_build_id,
                            "validation_id": snap_after["project"].get("latest_validation_id"),
                            "decision": decision,
                            "review": history[-1] if history else None,
                            "review_count": len(history),
                            "current_stage": snap_after["project"]["current_stage"],
                            "review_status": snap_after["project"].get("review_status"),
                            "workflow_state": snap_after["workflow_state"],
                            "deployment_authorization": service.store.authorize_preview_deployment(project_id),
                            "identity_verified": False,
                            "identity_note": (
                                "The automation API records the reviewer string it was given; it cannot verify a human "
                                "identity. An approved decision still requires the publish itself to be executed by an "
                                "operator or worker that re-verifies the approval."
                            ),
                            "deployed": False,
                            "note": "Recording a review decision never publishes anything.",
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
                except DatabasePersistenceError as db_error:
                    return self._json(503, {"error": "Database persistence error", "detail": str(db_error)})
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    return self._json(400, {"error": str(error)})
                except Exception as error:  # A partial write must surface as a 500, not a dropped connection.
                    return self._json(500, {"error": "Unhandled request failure", "detail": sanitize_db_error(error)})

        def log_message(self, _format: str, *_args) -> None:
            """Avoid logging headers or request bodies, which could contain credentials."""

    return Handler


def serve(
    *,
    state_path: str = "data/tracking/state.json",
    host: str | None = None,
    port: int | None = None,
    allow_non_loopback: bool = False,
    output_root: str = "output",
    export_root: str = "preview-export",
    repository_root: str = ".",
    database_url: str | None = None,
) -> None:
    resolved_host, resolved_port, _ = resolve_bind_config(
        host,
        port,
        allow_non_loopback=allow_non_loopback,
    )
    token = os.environ.get("AI_WEBSITE_AGENT_API_TOKEN")
    if not token:
        raise RuntimeError("AI_WEBSITE_AGENT_API_TOKEN is required; server not started.")
    store = TrackingStore(state_path, database_url=database_url)
    service = JobService(store)
    worker = LocalPipelineWorker(repository_root=repository_root, output_root=output_root)
    ThreadingHTTPServer(
        (resolved_host, resolved_port),
        make_handler(
            service,
            token,
            worker=worker,
            output_root=output_root,
            export_root=export_root,
            repository_root=repository_root,
        ),
    ).serve_forever()
