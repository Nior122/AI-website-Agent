"""Optional local HTTP adapter for a separately managed coordinator such as n8n.

No server starts on import. Deploy behind the user's chosen private network or
reverse proxy; this small adapter intentionally has no public-internet defaults.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse

from tracking.database import DatabasePersistenceError, is_production_mode
from tracking.store import (
    MAX_REVIEWER_CHARS,
    MAX_REVIEW_NOTES_CHARS,
    TrackingStore,
    validate_bounded_text,
    validate_export_directory,
    validate_record_id,
)
from .health import (
    STALE_RUNNING_SECONDS,
    deployment_report,
    operator_attention,
    provider_section,
    readiness_for,
    system_health,
    verify_storage,
)
from .jobs import JobService, JobWorker
from .observability import log_event, redact_local_paths, sanitize_diagnostic
from .workers import LocalPipelineWorker

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
ALLOWED_PRODUCTION_HOSTS = {"0.0.0.0"}
ALLOWED_API_JOB_TYPES = {"SCOUT", "RESEARCH", "QUALIFY", "BUILD", "VALIDATE"}

#: Largest request body the HTTP adapter accepts (JSON), so a caller cannot exhaust memory or bloat
#: the durable audit record with an oversized payload.
MAX_REQUEST_BYTES = 1_048_576

#: Review decisions an automation caller may submit. Anything else is a bad request, not a 500.
API_REVIEW_DECISIONS = {"APPROVED_FOR_PREVIEW", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}

#: A shared token short enough to be brute-forced, or one of the documented placeholders, is refused
#: for any bind that is reachable outside the machine.
MIN_NON_LOOPBACK_TOKEN_CHARS = 32
WEAK_TOKEN_PLACEHOLDERS = {
    "changeme", "change-me", "secret", "token", "test", "test-token", "local", "dev", "development",
    "password", "ai-website-agent", "local-secret-not-committed",
}


class BadRequest(ValueError):
    """Malformed or disallowed client input; answered with 400 instead of a misleading 404/500.

    ``code`` is the stable machine-readable reason an automation caller can branch on.
    """

    def __init__(self, message: str, *, code: str = "INVALID_REQUEST") -> None:
        super().__init__(message)
        self.error_code = code


class RequestBodyTooLarge(ValueError):
    """A body over ``MAX_REQUEST_BYTES``; answered with 413 and never read in full."""


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
        def _json(self, code: int, payload: dict, *, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            """Single shared API credential, compared in constant time.

            A missing header, a malformed scheme, a repeated header, and a wrong value are all simply
            unauthorized, and nothing the caller submitted is echoed back or logged. `token` empty is
            a hard refusal (`serve()` requires one), so no unauthenticated default exists.
            """
            supplied = self.headers.get("Authorization")
            headers = self.headers.get_all("Authorization") or []
            if not token or supplied is None or len(headers) != 1:
                return False
            return hmac.compare_digest(supplied.encode("utf-8"), f"Bearer {token}".encode("utf-8"))

        def _require_auth(self) -> bool:
            """Answer 401 (with no credential material) unless the request is authenticated."""
            if self._authorized():
                return True
            log_event(
                severity="WARNING",
                operation="http.auth",
                code="AUTH_FAILED" if (self.headers.get("Authorization") or "") else "AUTH_MISSING",
                message=f"{self.command} {urlparse(self.path).path}",
            )
            self._json(401, {"error": "Unauthorized"}, headers={"WWW-Authenticate": 'Bearer realm="ai-website-agent"'})
            return False

        def _body(self) -> dict:
            raw_length = self.headers.get("Content-Length")
            if (self.headers.get("Transfer-Encoding") or "").strip().lower().startswith("chunked"):
                raise BadRequest("Chunked request bodies are not supported; send a JSON object with Content-Length.")
            if raw_length is None or not str(raw_length).strip():
                return {}
            cleaned = str(raw_length).strip()
            if not cleaned.isdigit():
                raise BadRequest("Content-Length must be a non-negative integer number of bytes.")
            length = int(cleaned)
            if length > MAX_REQUEST_BYTES:
                raise RequestBodyTooLarge(
                    f"Request body is {length} bytes; the limit is {MAX_REQUEST_BYTES} bytes."
                )
            if not length:
                return {}
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise BadRequest(f"Request body was truncated ({len(raw)} of {length} bytes received).")
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise BadRequest("Request body must be UTF-8 encoded JSON.") from error
            except json.JSONDecodeError as error:
                raise BadRequest(f"Request body must be valid JSON: {error.msg} at line {error.lineno}.") from error
            if not isinstance(parsed, dict):
                raise BadRequest("Request body must be a JSON object.")
            return parsed

        def _record_id(self, value: object, *, kind: str) -> str:
            """Validate an externally supplied identifier before it touches state, SQL, or a path."""
            try:
                return validate_record_id(value, kind=kind)
            except ValueError as error:
                raise BadRequest(str(error)) from error

        def _export_directory(self, requested: object, *, lead_id: str, project_id: str) -> Path:
            """Confine a caller-named export directory to the operator-configured export root.

            A review-package export creates directories and writes files, so a remote caller may only
            name a location inside `export_root` (`preview-export` by default). The default target stays
            `<export_root>/<lead_id>-<project_id>`. Local operators keep full freedom through the CLIs,
            which run with the operator's own authority.
            """
            if requested is None or str(requested).strip() == "":
                return exp_path / f"{lead_id}-{project_id}"
            try:
                return validate_export_directory(requested, base_root=exp_path, allowed_roots=[exp_path])
            except ValueError as error:
                # The store's message names the resolved root; keep the absolute path out of the answer.
                raise BadRequest(
                    f"Export directory must stay inside the configured export root ({export_root!r}); "
                    f"use a plain directory name (the default is '{lead_id}-{project_id}').",
                    code="EXPORT_PATH_NOT_ALLOWED",
                ) from error

        def _guard_ids(self, path: list[str]) -> list[str]:
            """Reject unsafe path identifiers once, before any route can use them.

            Every `/jobs/:id`, `/leads/:id`, and `/projects/:id` route reaches state records, the durable
            ledger, or a directory named after the project, so the shape is enforced at the boundary: a
            malformed identifier is answered 400 instead of being looked up, persisted, or interpolated
            into a path. Collection pseudo-segments are left alone.
            """
            if len(path) >= 2 and path[0] in ("jobs", "leads", "projects") and path[1] not in (
                "recover-stale", "deduplicate",
            ):
                path[1] = self._record_id(path[1], kind=f"{path[0][:-1]}_id")
            return path

        def _detail(self, error: BaseException) -> str:
            """Sanitized, bounded, path-free text for an unexpected 500 answer."""
            return redact_local_paths(sanitize_diagnostic(error) or type(error).__name__)

        def _client_detail(self, error: BaseException) -> str:
            """Message-only text for expected client/storage refusals (no exception class noise)."""
            return redact_local_paths(sanitize_diagnostic(str(error)) or "Rejected.")

        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlparse(self.path)
            path = [seg for seg in parsed_url.path.strip("/").split("/") if seg]
            if path == ["healthz"]:
                # Lightweight Render liveness probe: does not expose operational data or secrets.
                # Deliberately liveness-only and always unauthenticated: it proves the process is
                # executing and answering, never that storage, workers or the workflow are healthy.
                # Readiness (which verifies the database) is the authenticated GET /readiness, and
                # operational state is GET /health, GET /attention and GET /deployment-status.
                return self._json(200, {"status": "ok"})
            if not self._require_auth():
                return
            query = parse_qs(parsed_url.query)
            try:
                path = self._guard_ids(path)
            except ValueError as error:
                return self._json(400, {"error": str(error), "error_code": getattr(error, "error_code", "INVALID_REQUEST")})
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
                        health = system_health(service.store, repository_root=repo_path, storage=verify_storage(service.store))
                        return self._json(200, {
                            "api_status": "OK",
                            "worker_registered": active_worker is not None,
                            "worker_name": getattr(active_worker, "name", None),
                            "api_execution_worker_configured": active_worker is not None,
                            "registered_worker_count": len(registered_workers),
                            "available_registered_worker_count": len(available_registered_workers),
                            "stale_running_threshold_seconds": STALE_RUNNING_SECONDS,
                            "attention": health["operator_attention"]["counts"],
                            "readiness": health["readiness"]["status"],
                            "attention_required": health["operator_attention"]["requires_attention"],
                            "health": health,
                        })
                    if path == ["readiness"]:
                        db_backend = getattr(service.store, "db_backend", None)
                        storage = verify_storage(service.store)
                        if storage["status"] != "healthy":
                            # A required database that cannot be reached or verified is never READY.
                            # The process stays alive (GET /healthz still answers) - readiness is about
                            # whether work can be trusted, and it cannot.
                            return self._json(503, {
                                "status": "NOT_READY",
                                "liveness": "ALIVE",
                                "reason": storage["error"] or "Storage backend is unavailable.",
                                "error_code": "STORAGE_UNAVAILABLE",
                                "storage_backend": storage["backend"],
                                "production_mode": storage["production_mode"],
                                "blocking": [{
                                    "code": "STORAGE_UNAVAILABLE",
                                    "message": storage["error"] or "Storage backend is unavailable.",
                                    "next_action": (
                                        "Verify the database connection configuration and schema, then re-check "
                                        "with: python3 -m orchestration.cli check-workspace"
                                    ),
                                }],
                                "degradations": [],
                            })
                        state = system_health(service.store, repository_root=repo_path, verify_build_evidence=False, storage=storage)
                        return self._json(200, {
                            "liveness": "ALIVE",
                            "blocking": state["readiness"]["blocking"],
                            "degradations": state["readiness"]["degradations"],
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
                    if len(path) == 3 and path[0] == "jobs" and path[2] == "history":
                        # Full attempt history for one job, from the execution ledger the system already
                        # writes: what was attempted, when, by which worker, and how it ended.
                        job = service.get(path[1])
                        return self._json(200, {
                            "job": _enrich_job(job),
                            "executions": service.executions_for(path[1]),
                            "retry_eligibility": service.retry_eligibility(path[1]),
                        })
                    if path == ["attention"]:
                        return self._json(200, operator_attention(service.store, repository_root=repo_path))
                    if path == ["deployment-status"]:
                        project_id = (query.get("project_id") or [None])[0]
                        report = provider_section(service.store, repo_path)
                        if project_id:
                            scoped = deployment_report(service.store, repository_root=repo_path)
                            report["projects"] = [
                                item for item in (scoped.get("attention", []) + [])
                                if item.get("project_id") == project_id
                            ] or [{
                                "project_id": project_id,
                                "note": "No deployment attention recorded for this project.",
                            }]
                        return self._json(200, report)
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
                        # A read-only check may not name a directory outside the operator's export root:
                        # verification hashes files on disk, so an arbitrary path would let a caller probe
                        # or steer evidence for another project.
                        target_export = self._export_directory(
                            (query.get("export_dir") or [None])[0], lead_id=lead_id, project_id=project_id
                        )
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
                    return self._json(404, {"error": "Not found", "error_code": "ROUTE_NOT_FOUND"})
                except RequestBodyTooLarge as error:
                    return self._json(413, {"error": str(error), "error_code": "REQUEST_BODY_TOO_LARGE"})
                except BadRequest as error:
                    return self._json(400, {"error": self._client_detail(error),
                                            "error_code": getattr(error, "error_code", "INVALID_REQUEST")})
                except DatabasePersistenceError as db_error:
                    # The storage layer already redacts connection secrets; paths are removed here so an
                    # HTTP answer cannot map the host filesystem.
                    return self._json(503, {"error": "Database persistence error", "error_code": "STORAGE_UNAVAILABLE",
                                            "detail": self._client_detail(db_error)})
                except (KeyError, ValueError) as error:
                    # A GET failure is a lookup failure on this surface (unknown job/project); malformed
                    # input is answered above as 400.
                    return self._json(404, {"error": self._client_detail(error), "error_code": "RECORD_NOT_FOUND"})
                except Exception as error:  # Never close the socket without a structured answer.
                    log_event(severity="ERROR", operation="http.get", code="UNHANDLED_FAULT",
                              message=f"{self.command} {urlparse(self.path).path}: {type(error).__name__}: {error}")
                    return self._json(500, {"error": "Unhandled request failure", "error_code": "INTERNAL_ERROR",
                                            "detail": self._detail(error)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            path = [seg for seg in urlparse(self.path).path.strip("/").split("/") if seg]
            with lock:
                try:
                    if getattr(service.store, "db_backend", None) is not None and hasattr(service.store, "refresh"):
                        service.store.refresh()
                    path = self._guard_ids(path)
                    payload = self._body()
                    if path == ["jobs"]:
                        if "job_type" not in payload or "job_id" not in payload:
                            raise BadRequest("POST /jobs requires 'job_type' and 'job_id'.")
                        # Identifiers are validated before they reach the ledger, SQL parameters, or the
                        # export path derived from a project's lead.
                        for name in ("job_id", "lead_id", "project_id", "build_id", "parent_job_id", "idempotency_key"):
                            if payload.get(name) is not None:
                                payload[name] = self._record_id(payload[name], kind=name)
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
                        try:
                            job = service.retry(
                                path[1],
                                input=retry_input,
                                reset_attempts=reset_attempts,
                                worker=active_worker if run_immediately else None,
                            )
                        except ValueError as error:
                            # A refused retry is a decision the operator can act on, not a missing
                            # resource: report the recorded reason and what remains possible.
                            eligibility = service.retry_eligibility(path[1])
                            return self._json(409, {
                                "error": str(error),
                                "error_code": eligibility.get("error_code") or "RETRY_NOT_ELIGIBLE",
                                "job_id": path[1],
                                "retry_eligibility": eligibility,
                                "last_error_code": eligibility.get("last_error_code"),
                                "next_action": eligibility.get("next_action")
                                or "Correct the recorded cause, then retry explicitly.",
                            })
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
                        if decision not in API_REVIEW_DECISIONS:
                            raise BadRequest(
                                "'decision' must be one of: " + ", ".join(sorted(API_REVIEW_DECISIONS)) + "."
                            )
                        notes = payload.get("notes")
                        confirm = payload.get("confirm")
                        reviewer = payload.get("reviewer") or payload.get("reviewer_id")
                        # Type-checked before coercion: a structured value must not be stringified into
                        # persisted review evidence (an object repr would otherwise read like a note).
                        for name, value in (("notes", notes), ("confirm", confirm), ("reviewer", reviewer)):
                            if value is not None and not isinstance(value, str):
                                raise BadRequest(f"'{name}' must be a string.")
                        missing = [name for name, value in (("decision", decision), ("notes", notes), ("confirm", confirm), ("reviewer", reviewer)) if not value]
                        if missing:
                            return self._json(400, {
                                "error": "POST /projects/:id/review requires " + ", ".join(f"'{name}'" for name in missing)
                                         + ". Record the decision with the build id you actually reviewed: "
                                         + "confirm=<project_id>:<build_id>:<DECISION>.",
                                "error_code": "REVIEW_FIELDS_REQUIRED",
                            })
                        # Notes and reviewer names are persisted operator-visible evidence; bound their size.
                        notes = validate_bounded_text(notes, field="notes", limit=MAX_REVIEW_NOTES_CHARS)
                        reviewer = validate_bounded_text(reviewer, field="reviewer", limit=MAX_REVIEWER_CHARS)
                        # An API caller cannot assert that its own reviewer identity was verified: this
                        # surface authenticates the automation token, not a person, and a claimed identity
                        # is exactly what deployment authorization is bound to.
                        if payload.get("identity_verified"):
                            return self._json(400, {
                                "error": (
                                    "'identity_verified' cannot be supplied by an API caller. Record the decision "
                                    "through the review channel that authenticates a human reviewer; the field is "
                                    "always False here so the approval gate stays honest."
                                ),
                                "error_code": "IDENTITY_ASSERTION_REFUSED",
                            })
                        assigned_build_id = project.get("assigned_build_id")
                        expected_confirmation = f"{project_id}:{assigned_build_id}:{decision}"
                        if str(confirm) != expected_confirmation:
                            return self._json(409, {
                                "error": "Review confirmation does not match the project's currently assigned build.",
                                "error_code": "REVIEW_CONFIRMATION_MISMATCH",
                                "expected_confirmation": expected_confirmation,
                                # Bounded reflection of the caller's own value, for diagnostics only.
                                "received_confirmation": str(confirm)[:200],
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
                                "reviewer_type": validate_bounded_text(
                                    payload.get("reviewer_type"), field="reviewer_type", limit=40
                                ).strip().upper(),
                                # Forced, never inferred from the request.
                                "identity_verified": False,
                            }
                        build_id = payload.get("build_id")
                        if build_id is not None:
                            build_id = self._record_id(build_id, kind="build_id")
                        action = payload.get("action")
                        if action is not None:
                            action = validate_bounded_text(action, field="action", limit=40).strip().upper()
                        updated = service.store.record_review(
                            project_id,
                            decision,
                            notes,
                            reviewer=reviewer_value,
                            require_notes=True,
                            build_id=build_id,
                            action=action,
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
                            "identity_source": "orchestration-api",
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
                        # Confined like the GET form: a request can choose a name inside `export_root`,
                        # never an arbitrary filesystem location to create directories and files in.
                        target_export = self._export_directory(
                            payload.get("export_dir"), lead_id=lead_id, project_id=project_id
                        )
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

                    return self._json(404, {"error": "Not found", "error_code": "ROUTE_NOT_FOUND"})
                except RequestBodyTooLarge as error:
                    return self._json(413, {"error": str(error), "error_code": "REQUEST_BODY_TOO_LARGE"})
                except BadRequest as error:
                    return self._json(400, {"error": self._client_detail(error),
                                            "error_code": getattr(error, "error_code", "INVALID_REQUEST")})
                except DatabasePersistenceError as db_error:
                    return self._json(503, {"error": "Database persistence error", "error_code": "STORAGE_UNAVAILABLE",
                                            "detail": self._client_detail(db_error)})
                except KeyError as error:
                    # An unknown record is 404; invalid input is 400, never both.
                    return self._json(404, {"error": self._client_detail(error), "error_code": "RECORD_NOT_FOUND"})
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    return self._json(400, {"error": self._client_detail(error), "error_code": "INVALID_REQUEST"})
                except Exception as error:  # A partial write must surface as a 500, not a dropped connection.
                    log_event(severity="ERROR", operation="http.post", code="UNHANDLED_FAULT",
                              message=f"{self.command} {urlparse(self.path).path}: {type(error).__name__}: {error}")
                    return self._json(500, {"error": "Unhandled request failure", "error_code": "INTERNAL_ERROR",
                                            "detail": self._detail(error)})

        def do_PUT(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_HEAD(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            """Answer 405 as JSON. This API is intentionally read/mutate-only over GET and POST.

            The state ledger has no delete or partial-update verb on purpose: records stay auditable.
            The answer is produced only after authentication, so an unauthenticated probe cannot map
            which verbs or routes this API supports.
            """
            if not self._require_auth():
                return
            self._json(405, {"error": f"{self.command} is not supported; use GET or POST.",
                             "error_code": "METHOD_NOT_ALLOWED"},
                       headers={"Allow": "GET, POST"})

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
    resolved_host, resolved_port, outside_loopback = resolve_bind_config(
        host,
        port,
        allow_non_loopback=allow_non_loopback,
    )
    token = os.environ.get("AI_WEBSITE_AGENT_API_TOKEN")
    if not token:
        raise RuntimeError("AI_WEBSITE_AGENT_API_TOKEN is required; server not started.")
    # A single shared bearer token is the documented design (no user management). It is only adequate
    # when it cannot be guessed: refuse a short or placeholder credential for any socket that is
    # reachable beyond this machine or in production, while local development keeps working unchanged.
    # The token value itself is never included in the message.
    if outside_loopback or is_production_mode():
        normalized = token.strip()
        if len(normalized) < MIN_NON_LOOPBACK_TOKEN_CHARS or normalized.lower() in WEAK_TOKEN_PLACEHOLDERS:
            raise RuntimeError(
                "AI_WEBSITE_AGENT_API_TOKEN is too weak for a non-loopback or production bind: use at "
                f"least {MIN_NON_LOOPBACK_TOKEN_CHARS} characters and not a placeholder value. Local "
                "loopback development may keep a shorter token."
            )
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
