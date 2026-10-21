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

from tracking import inbound
from tracking.database import DatabasePersistenceError, is_production_mode
from tracking.engagement import EngagementLedger, LedgerError
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

#: Header an external intake adapter signs its delivery with: ``sha256=<hex of HMAC-SHA256(body bytes)>``.
#: The operator token authenticates *who may write*; this proves *which bytes* the provider sent, so a relay
#: that is trusted to deliver is not also trusted to edit. It is required whenever a signing secret is set.
#: A server with no secret still records events (the token gate is what protects it), and each record says
#: ``signature.verified: false`` with the reason in prose rather than pretending verification happened.
INBOUND_SIGNATURE_HEADER = inbound.INBOUND_SIGNATURE_HEADER
INBOUND_SIGNING_SECRET_ENV = "AI_WEBSITE_AGENT_INBOUND_SIGNING_SECRET"


def inbound_signing_secret(env: dict[str, str] | None = None) -> str:
    """The HMAC key for inbound deliveries, or ``""`` when this instance verifies none."""
    return (os.environ if env is None else env).get(INBOUND_SIGNING_SECRET_ENV, "").strip()


TELEGRAM_TOKEN_ENV = "AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN"

#: Body fields an integration caller may name. A mailbox query or fetch format is not one of them: they decide
#: *which messages this process downloads*, so they are operator configuration, not request parameters, and a
#: caller that could set either could pull one chosen message instead of the bounded summary.
INTEGRATION_POLL_FIELDS = {"dry_run", "max_messages", "retry_pending"}
INTEGRATION_NOTIFY_FIELDS = {"limit", "chat_id"}
INTEGRATION_MAX_MESSAGES = 50
#: What a caller may say to the send route: *which* approved draft, and whether it is only a preview. Nothing
#: else - no recipient, no subject, no body, no channel. Those live on the record a human reviewed, and a route
#: that let a workflow supply them would let a workflow decide what a prospect reads.
INTEGRATION_SEND_FIELDS = {"outreach_id", "dry_run", "expected_fingerprint", "confirmation", "actor", "note"}
INTEGRATION_RECONCILE_FIELDS = {"outreach_id", "limit"}
INTEGRATION_CHECK_FIELDS: set[str] = set()
#: Refusals that mean "the records say no", as opposed to "your request was malformed". A workflow that sees one
#: of these must not retry: an attempt that is *uncertain* is answered with the same 409 as a blocked one, and
#: `retry_allowed: false`, precisely so a scheduler cannot turn a timeout into two emails to the same person.
OUTBOUND_STATE_CODES = {"NOT_APPROVED", "APPROVAL_STALE", "APPROVAL_UNBOUND", "CONTACT_BLOCKED",
                        "ADDRESS_SUPPRESSED", "SEND_UNCERTAIN", "SENDING_DISABLED", "ALREADY_SENT",
                        "DRAFT_UNSAFE", "RECIPIENT_UNPARSEABLE", "PREVIEW_UNAVAILABLE", "DEPLOYMENT_STALE",
                        "LEAD_NO_LONGER_QUALIFIED", "LIVE_EFFECTS_DISABLED", "SEND_CLIENT_UNAVAILABLE",
                        "MESSAGE_NOT_COMPOSABLE", "OUTREACH_ALREADY_SENT", "OUTREACH_NOT_APPROVED",
                        "CONTACT_LIMIT_REACHED", "SEND_UNRESOLVED", "PROJECT_CLOSED", "OUTREACH_LEAD_MISMATCH",
                        "PROVIDER_RECEIPT_UNAVAILABLE", "EVIDENCE_REQUIRED",
                        # A hold is a state, not a malformed request: the same 409 the mailbox-cycle route gives.
                        "AUTOMATION_HELD"}
OUTBOUND_NOT_FOUND_CODES = {"OUTREACH_NOT_FOUND", "OUTREACH_ID_REQUIRED"}


def _send_report(answer: dict) -> dict:
    """What the send route tells a caller: state, ids, and codes - never a contact address or a body.

    The recipient is masked here rather than inside the executor because this is the only place it is
    *unnecessary*: the operator already knows which draft they named, and a log or a workflow execution record
    should not hold a prospect's address.
    """
    from .telegram_limits import mask_address

    keep = ("dry_run", "send_state", "code", "outreach_id", "lead_id", "engagement_id", "provider_message_id",
            "result", "verified", "provider_verified", "verify_detail", "content_fingerprint", "delivery_status",
            "outreach_status", "contact_established", "counts_as_contact", "delivery_note", "reason", "unchanged",
            "next_action", "capability_note")
    report = {key: answer[key] for key in keep if key in answer}
    for key in ("would_contact",):
        if answer.get(key):
            report[key + "_masked"] = mask_address(str(answer[key]))
    report.setdefault("send_state", "UNKNOWN")
    report["recipient_omitted"] = True
    if report.get("code") == "SEND_UNCERTAIN":
        report["retry_allowed"] = False
        report["attempt_may_have_landed"] = True
    return report


def _mask_or_none(value) -> str | None:
    from .telegram_limits import mask_address

    return mask_address(str(value)) if value else None


def integration_report(store, *, environment: dict[str, str] | None = None) -> dict:
    """What the operator channels are, without contacting anything. See ``orchestration.health.channels_section``."""
    from .health import channels_section

    return channels_section(store, environment=environment)


def live_outreach_allowed(env: dict[str, str] | None = None) -> bool:
    """Whether this machine is allowed to put an outbound email in a stranger's inbox.

    An environment switch rather than a request field, so no caller - not a workflow, not an operator's script,
    not this API - can escalate itself into being able to send. The Telegram surface reads the same flag.
    """
    import os

    source = os.environ if env is None else env
    return (source.get("AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH") or "").strip().lower() in {"1", "true", "yes", "on"}


def build_outbound_sender(store, *, allow_live: bool | None = None):
    """The application's one send path, wired to the same store, ledger, and outreach service as every other.

    Nothing is contacted while building it: the Gmail send credential is only opened when a real send is asked
    for, which is what lets ``dry_run`` and the outbox read stay free of any provider.
    """
    from .outbound import ApprovedOutboundSender

    return ApprovedOutboundSender(store, allow_live=(live_outreach_allowed() if allow_live is None
                                                     else bool(allow_live)))


def build_email_poller(store, *, max_messages: int | None = None):
    """A poller for this request, or a coded refusal naming what is missing.

    Constructing the client is where a credential would be read, so this is also where "no mailbox is connected"
    becomes an answer instead of a stack trace.
    """
    from integrations.gmail import GmailClient, GmailError, gmail_config

    from .email_inbound import EmailInboundPoller

    config = gmail_config(os.environ)
    if not config["configured"]:
        raise BadRequest("This instance has no mailbox configured: missing "
                         + ", ".join(config["missing"]) + ".", code="GMAIL_NOT_CONFIGURED",
                         next_action=config["next_action"])
    try:
        client = GmailClient.from_environment()
    except GmailError as error:
        raise BadRequest(str(error), code=error.error_code,
                         next_action=getattr(error, "next_action", None)) from None
    limit = INTEGRATION_MAX_MESSAGES if max_messages is None else max(1, min(int(max_messages),
                                                                             INTEGRATION_MAX_MESSAGES))
    return EmailInboundPoller(client, store=store, query=config["query"], max_messages=limit,
                              fetch_format=config["fetch_format"])


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

    def __init__(self, message: str, *, code: str = "INVALID_REQUEST",
                 next_action: str | None = None) -> None:
        super().__init__(message)
        self.error_code = code
        self.next_action = next_action


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

        def _raw_body(self) -> bytes:
            """Return the request body verbatim, refusing more than ``MAX_REQUEST_BYTES`` before reading it all.

            Separate from :meth:`_body` because the inbound route must HMAC the *exact bytes* the provider
            sent: re-serialising the parsed JSON would produce a different digest that could not be verified.
            """
            raw_length = self.headers.get("Content-Length")
            if (self.headers.get("Transfer-Encoding") or "").strip().lower().startswith("chunked"):
                raise BadRequest("Chunked request bodies are not supported; send a JSON object with Content-Length.")
            if raw_length is None or not str(raw_length).strip():
                return b""
            cleaned = str(raw_length).strip()
            if not cleaned.isdigit():
                raise BadRequest("Content-Length must be a non-negative integer number of bytes.")
            length = int(cleaned)
            if length > MAX_REQUEST_BYTES:
                raise RequestBodyTooLarge(
                    f"Request body is {length} bytes; the limit is {MAX_REQUEST_BYTES} bytes."
                )
            if not length:
                return b""
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise BadRequest(f"Request body was truncated ({len(raw)} of {length} bytes received).")
            return raw

        def _body(self) -> dict:
            """Parse a JSON object body out of :meth:`_raw_body`."""
            raw = self._raw_body()
            if not raw:
                return {}
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
                    if path == ["integrations", "status"]:
                        # Configuration and recorded cursors only; a GET never contacts a provider.
                        return self._json(200, integration_report(service.store))
                    if path == ["integrations", "outbox"]:
                        # A reading of the ledger, never a queue this process drains: GET cannot send anything,
                        # and every row says why it is or is not sendable right now.
                        from .outbound import OutboundRefusal

                        try:
                            report = build_outbound_sender(service.store).outbox(
                                limit=(query.get("limit") or ["10"])[0])
                        except OutboundRefusal as error:
                            return self._json(400, {"error": str(error), "error_code": error.code})
                        report["outbox"] = [{key: value for key, value in row.items() if key != "recipient"}
                                           | {"recipient_masked": _mask_or_none(row.get("recipient"))}
                                           for row in report["outbox"]]
                        return self._json(200, report)
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
                    if path[:1] == ["engagement"]:
                        # Read-only engagement surface. There is deliberately no POST/PUT for the ledger:
                        # `OUTREACH_SENT`, `PRESENTED`, and `WON` are claims about what a person did, and an
                        # endpoint that let a workflow write them would let that workflow approve itself. The
                        # one write the engagement namespace accepts is POST engagement/inbound, which records
                        # an inbound message as evidence and refuses every state-asserting field outright.
                        ledger = EngagementLedger(service.store, source="api-readonly")
                        sub = path[1:]
                        first = sub[0] if sub else ""
                        if first == "pipeline":
                            return self._json(200, ledger.pipeline(
                                stage=(query.get("stage") or [None])[0],
                                limit=(query.get("limit") or [50])[0],
                                offset=(query.get("offset") or [0])[0],
                                today=(query.get("today") or [None])[0],
                            ))
                        if first == "outstanding":
                            return self._json(200, ledger.outstanding_work(
                                today=(query.get("today") or [None])[0],
                                limit=(query.get("limit") or [50])[0],
                            ))
                        if first == "eligibility" and len(sub) == 2:
                            return self._json(200, ledger.outreach_eligibility(
                                sub[1],
                                channel=(query.get("channel") or [None])[0],
                                outreach_id=(query.get("outreach_id") or [None])[0],
                                for_action=(query.get("for_action") or ["SEND"])[0],
                            ))
                        if first == "integrations":
                            raise BadRequest("Integrations are reported at GET /integrations/status, and the "
                                             "approved-but-unsent list at GET /integrations/outbox; the ledger "
                                             "namespace has no integration routes.",
                                             code="LEDGER_ROUTE_NOT_FOUND")
                        if first == "inbound":
                            # The intake's own backlog: events this side could not route, or is not sure
                            # about. Read-only, bounded, and it never exposes a message body.
                            from tracking.inbound import InboundIntake

                            try:
                                limit = int((query.get("limit") or ["25"])[0])
                            except (TypeError, ValueError):
                                limit = 25
                            return self._json(200, InboundIntake(ledger).pending(limit=limit))
                        if first in {"leads", "relationship"} and len(sub) == 2:
                            self._record_id(sub[1], kind="lead_id")
                            if first == "leads":
                                return self._json(200, ledger.ledger(sub[1]))
                            return self._json(200, ledger.relationship(sub[1]))
                        return self._json(404, {
                            "error": "Unknown engagement route. The ledger is read-only over HTTP "
                                     "(engagement/pipeline, engagement/outstanding, engagement/eligibility/:lead_id, "
                                     "engagement/leads/:lead_id, engagement/relationship/:lead_id, "
                                     "engagement/inbound); the only permitted write is POST "
                                     "engagement/inbound, which records what a business said and can assert "
                                     "nothing else; other writes come from the operator's CLI.",
                            "error_code": "LEDGER_ROUTE_NOT_FOUND",
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
                except LedgerError as error:
                    # A ledger refusal is about the request (a bad limit, an unknown stage), so it is a client
                    # error with its own code - never a 500 and never a silently widened query.
                    return self._json(400, {"error": self._client_detail(error), "error_code": error.code})
                except (KeyError, ValueError) as error:
                    # A GET failure is a lookup failure on this surface (unknown job/project); malformed
                    # input is answered above as 400.
                    return self._json(404, {"error": self._client_detail(error), "error_code": "RECORD_NOT_FOUND"})
                except Exception as error:  # Never close the socket without a structured answer.
                    log_event(severity="ERROR", operation="http.get", code="UNHANDLED_FAULT",
                              message=f"{self.command} {urlparse(self.path).path}: {type(error).__name__}: {error}")
                    return self._json(500, {"error": "Unhandled request failure", "error_code": "INTERNAL_ERROR",
                                            "detail": self._detail(error)})

        def _handle_inbound_event(self) -> None:
            """Accept one authenticated inbound provider event and record it (auth and routing done upstream).

            This route never replies to the business, never approves anything, and never decides an outcome.
            :class:`tracking.inbound.InboundIntake` links what arrived to the record it can prove, triages it
            with the rules the operator uses, and parks everything else for review. A payload that breaks the
            intake contract is refused with a code and stores nothing. The raw body is read here rather than
            through :meth:`_body` because a signature has to be checked against the exact bytes sent.
            """
            from tracking.inbound import (InboundEventError, InboundIntake, signature_mismatch,
                                          verify_inbound_signature)

            try:
                raw = self._raw_body()
            except RequestBodyTooLarge as error:
                # The same code the rest of this API answers an oversized body with, so a caller only ever
                # learns one vocabulary for "too large" (`EVENT_BODY_TOO_LARGE` below is a different limit:
                # the message text the intake is willing to keep, not the bytes it is willing to read).
                return self._json(413, {"error": str(error), "error_code": "REQUEST_BODY_TOO_LARGE",
                                        "max_bytes": MAX_REQUEST_BYTES,
                                        "next_action": "Send the message the provider delivered, not the whole "
                                                       "mailbox; this limit is deliberate."})
            except BadRequest as error:
                return self._json(400, {"error": self._client_detail(error), "error_code": error.error_code,
                                        "next_action": "Send the exact bytes with a valid Content-Length header."})

            secret = inbound_signing_secret()
            supplied = (self.headers.get(INBOUND_SIGNATURE_HEADER) or "").strip()
            if supplied and not secret:
                return self._json(401, {"error": "A signature was sent but no signing secret is configured here, "
                                                 "so it cannot be verified.",
                                        "error_code": "INBOUND_SIGNATURE_UNVERIFIABLE",
                                        "next_action": f"Set {INBOUND_SIGNING_SECRET_ENV} to the secret the adapter "
                                                       "uses, or stop signing."})
            if secret and signature_mismatch(supplied):
                return self._json(401, {"error": signature_mismatch(supplied),
                                        "error_code": "INBOUND_SIGNATURE_MALFORMED",
                                        "next_action": f"Send {INBOUND_SIGNATURE_HEADER}: sha256=<hex digest>."})
            if secret and not verify_inbound_signature(secret, supplied, raw):
                log_event(severity="WARNING", operation="http.post", code="INBOUND_SIGNATURE_FAILED",
                          message="inbound delivery did not match the configured signing secret")
                return self._json(401, {"error": "The inbound signature does not match this request body.",
                                        "error_code": "INBOUND_SIGNATURE_INVALID",
                                        "next_action": "Sign the exact bytes with the shared secret."})
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BadRequest("Request body must be valid JSON.") from error
            if not isinstance(payload, dict):
                raise BadRequest("Request body must be a JSON object.")
            if secret:
                # Provenance only: a holder of the shared key signed these exact bytes.
                payload["signature_verified"] = True
            intake = InboundIntake(EngagementLedger(service.store, source="api-inbound"))
            try:
                result = intake.process(payload, source="api-inbound")
            except InboundEventError as error:
                # A contract violation is the adapter's to fix; nothing was stored, so a resend is safe.
                log_event(severity="WARNING", operation="http.post", code=error.code or "EVENT_INVALID",
                          message="inbound event refused at intake")
                return self._json(400, {"error": str(error), "error_code": error.code,
                                        "next_action": "Fix the payload and resend it; a refused event created no record."})
            log_event(severity="INFO", operation="http.post", code="INBOUND_" + str(result.get("disposition")),
                      message=f"inbound event disposition={result.get('disposition')}",
                      lead_id=result.get("lead_id"), engagement_id=result.get("engagement_id"),
                      event_ref=result.get("event_id") or result.get("external_message_id"))
            return self._json(200, result)

        def _integration_body(self, permitted: set[str]) -> dict:
            """Parse a small allow-listed body for an integration route, refusing anything else by name."""
            payload = self._body()
            unknown = sorted(set(payload) - permitted)
            if unknown:
                raise BadRequest(
                    "These fields are not accepted here: " + ", ".join(unknown) + ". A mailbox query or fetch "
                    "format decides which messages this process downloads, so they stay operator configuration.",
                    code="INTEGRATION_PARAMETER_NOT_PERMITTED",
                )
            return payload

        def _handle_email_poll(self) -> None:
            """Run one mailbox cycle through the application's own intake - scheduled by n8n or cron, never implied.

            The answer is counts, ids, and codes: it carries no message text, because a workflow execution log is
            a poor place for a business's correspondence and the intake already stores what needs storing.
            """
            from .controls import held

            payload = self._integration_body(INTEGRATION_POLL_FIELDS)
            if held(service.store):
                # A hold on automated processing means exactly that: no provider is contacted and no cycle runs.
                return self._json(409, {
                    "error": "Automated dispatch is on hold, so no mailbox cycle was started.",
                    "error_code": "AUTOMATION_HELD",
                    "next_action": "Release the hold (release-automation, or /resume in Telegram), then re-run.",
                })
            try:
                poller = build_email_poller(service.store, max_messages=payload.get("max_messages"))
            except BadRequest as error:
                return self._json(400, {"error": self._client_detail(error), "error_code": error.error_code,
                                        **({"next_action": error.next_action} if error.next_action else {})})
            try:
                if payload.get("retry_pending"):
                    report = poller.retry_pending(limit=INTEGRATION_MAX_MESSAGES)
                else:
                    report = poller.poll_once(dry_run=bool(payload.get("dry_run")), notify=True)
            except DatabasePersistenceError as error:
                return self._json(503, {"error": "The cycle could not be persisted.",
                                        "error_code": "STORAGE_UNAVAILABLE", "detail": self._detail(error),
                                        "next_action": "Re-run the cycle: the provider's own message id keeps a "
                                                       "retry from double-counting, and no cursor advanced."})
            for row in report.get("results") or []:
                if isinstance(row, dict):
                    row.pop("excerpt", None)
            report["next_action"] = report.get("next_action") or "Nothing needs an operator from this cycle."
            log_event(severity="INFO" if not report.get("error_code") else "WARNING",
                      operation="integration.email_poll", code=str(report.get("error_code") or "CYCLE_COMPLETE"),
                      message=f"mailbox cycle: fetched={report.get('fetched', 0)} "
                              f"recorded={report.get('recorded', 0)} unmatched={report.get('unmatched', 0)}")
            return self._json(200, report)

        def _handle_email_send(self) -> None:
            """Send one *already approved* draft, or preview it. The approval is not in this body.

            The caller names an ``outreach_id`` and optionally confirms a content fingerprint; everything about
            the message itself - recipient, subject, wording, channel - is read from the record a human reviewed.
            The executor then re-reads the lead, the opt-out list, the contact budget, the hold, and the approval
            digest before it contacts Gmail, and it writes the attempt down first, so this route cannot be used
            to send un-reviewed text and a retry of it cannot mail the same prospect twice.
            """
            from .controls import held
            from .outbound import OutboundRefusal

            payload = self._integration_body(INTEGRATION_SEND_FIELDS)
            dry_run = bool(payload.get("dry_run"))
            # No dry_run exception here, even though a dry run would contact nobody: this route's answer while a
            # hold is on is "held", for every kind of send call, so an operator cannot read a difference between
            # the two as a difference in the hold's strength. Reads stay open - see GET /integrations/outbox.
            if held(service.store):
                return self._json(409, {"error": "Automated dispatch is on hold, so no send was attempted.",
                                        "error_code": "AUTOMATION_HELD", "retry_allowed": False,
                                        # Both flags are the point of this answer: a held send is not a failed
                                        # send, and nothing was handed to a provider that would have to be
                                        # reconciled before any retry is considered.
                                        "attempt_may_have_landed": False,
                                        "next_action": "release the hold, then decide again; nothing was sent and "
                                                       "no attempt was recorded"})
            sender = build_outbound_sender(service.store)
            try:
                answer = sender.send(str(payload.get("outreach_id") or "").strip(),
                                     actor_id=str(payload.get("actor") or "api-caller")[:120],
                                     identity_source="integration_api_bearer_token",
                                     dry_run=dry_run,
                                     expected_fingerprint=payload.get("expected_fingerprint"),
                                     confirmation=payload.get("confirmation"),
                                     note=payload.get("note"))
            except OutboundRefusal as error:
                status = 400 if error.code in OUTBOUND_NOT_FOUND_CODES else (
                    409 if error.code in OUTBOUND_STATE_CODES else 400)
                return self._json(status, {"error": str(error), "error_code": error.code,
                                           "retry_allowed": False,
                                           "attempt_may_have_landed": error.code == "SEND_UNCERTAIN",
                                           **({"next_action": error.next_action} if error.next_action else {})})
            except Exception as error:  # noqa: BLE001 - a fault is typed, never a stack trace to the client
                log_event(severity="ERROR", operation="http.post", code="OUTBOUND_SEND_FAULT",
                          message="the send executor faulted", detail=self._detail(error))
                return self._json(500, {"error": "The send could not be carried out.", "error_code": "SEND_FAULT",
                                        "retry_allowed": False,
                                        "next_action": "check `email-send-status` before anything is retried: an "
                                                       "answer that never arrived is not an answer"})
            body = _send_report(answer)
            log_event(severity="INFO" if body["send_state"] == "SENT" else "WARNING",
                      operation="http.post", code=f"SEND_{str(body['send_state']).upper()}",
                      message=f"outreach {payload.get('outreach_id')} send attempt: {body['send_state']}",
                      outreach_id=payload.get("outreach_id"), lead_id=body.get("lead_id"))
            return self._json(200, body)

        def _handle_email_reconcile(self) -> None:
            """Ask the account what happened to an unanswered send. Read-only, and never a resend."""
            from .outbound import OutboundRefusal

            payload = self._integration_body(INTEGRATION_RECONCILE_FIELDS)
            sender = build_outbound_sender(service.store)
            try:
                report = sender.reconcile(str(payload.get("outreach_id") or "").strip() or None,
                                          limit=payload.get("limit") or 25)
            except OutboundRefusal as error:
                return self._json(400 if error.code in OUTBOUND_NOT_FOUND_CODES else 409,
                                  {"error": str(error), "error_code": error.code,
                                   **({"next_action": error.next_action} if error.next_action else {})})
            except Exception as error:  # noqa: BLE001
                return self._json(500, {"error": "The account could not be consulted.",
                                        "error_code": "RECONCILE_FAULT", "detail": self._detail(error)})
            return self._json(200, report)

        def _handle_email_send_check(self) -> None:
            """Exchange the send credential's token and send nothing. The honest limit of a connectivity check.

            A success here means "Google accepted this machine's send-only refresh token". It says nothing about
            whether any message was delivered, and it is not what makes sending allowed - the approval records and
            the live-effect flag are.
            """
            sender = build_outbound_sender(service.store)
            try:
                answer = sender.verify()
            except Exception as error:  # noqa: BLE001 - a credential fault is a configuration answer
                return self._json(503, {"error": self._detail(error) or "the send credential could not be used",
                                        "error_code": str(getattr(error, "code", "SEND_CHECK_FAILED")),
                                        "sent": False,
                                        "next_action": getattr(error, "next_action", None) or
                                                       "re-authorize the send-only scope on this machine"})
            return self._json(200, {**answer, "sent": False,
                                    "capability_note": "a token exchange only: no message was composed, sent, or "
                                                        "read"})

        def _handle_telegram_notify(self) -> None:
            """Push what needs a person to the operator's chat. Read-only apart from the send itself."""
            from integrations.telegram import ProviderError, TelegramBotApi, telegram_config

            from .telegram_bot import TelegramOperatorBot

            payload = self._integration_body(INTEGRATION_NOTIFY_FIELDS)
            config = telegram_config(os.environ)
            if not config["configured"]:
                return self._json(409, {"error": "No Telegram bot is configured on this instance.",
                                        "error_code": config["code"], "next_action": config["next_action"],
                                        "token_present": config["token_present"]})
            api = TelegramBotApi((os.environ.get(TELEGRAM_TOKEN_ENV) or "").strip(),
                                 api_root=config["api_root"], timeout=10.0)
            bot = TelegramOperatorBot(api, store=service.store,
                                      allowed_user_ids=config["allowed_user_ids"],
                                      allowed_chat_ids=config["allowed_chat_ids"])
            try:
                limit = int(payload.get("limit") or 6)
                chat_id = int(payload["chat_id"]) if payload.get("chat_id") is not None else None
            except (TypeError, ValueError) as error:
                raise BadRequest("'limit' and 'chat_id' must be numbers.", code="INVALID_REQUEST") from error
            try:
                report = bot.notify_once(chat_id=chat_id, limit=limit)
            except ProviderError as error:
                return self._json(502, {"error": str(error), "error_code": error.code,
                                        "retryable": bool(error.retryable)})
            return self._json(200, report)

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            path = [seg for seg in urlparse(self.path).path.strip("/").split("/") if seg]
            with lock:
                try:
                    if getattr(service.store, "db_backend", None) is not None and hasattr(service.store, "refresh"):
                        service.store.refresh()
                    path = self._guard_ids(path)
                    if path == ["engagement", "inbound"]:
                        # The one write route in the engagement namespace, and it can only record what a
                        # business actually said. It reads its own body: see _handle_inbound_event.
                        return self._handle_inbound_event()
                    if path == ["integrations", "email", "poll"]:
                        return self._handle_email_poll()
                    if path == ["integrations", "email", "send"]:
                        return self._handle_email_send()
                    if path == ["integrations", "email", "reconcile"]:
                        return self._handle_email_reconcile()
                    if path == ["integrations", "email", "send-check"]:
                        return self._handle_email_send_check()
                    if path == ["integrations", "telegram", "notify"]:
                        return self._handle_telegram_notify()
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
