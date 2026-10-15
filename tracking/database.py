"""Normalized, transactional PostgreSQL / relational persistence adapter for TrackingStore.

Why a normalized relational model instead of a single JSONB document:
- A single shared JSONB blob suffers from lost-update race conditions whenever two
  concurrent requests or workers load the state, modify different collections or rows
  (e.g., Worker A updates `jobs["job-1"]` while Operator B records a review on
  `projects["project-2"]`), and write back the full blob.
- This adapter persists every domain collection (`leads`, `research_evidence`,
  `qualifications`, `projects`, `builds`, `validations`, `reviews`, `deployments`,
  `outreach_packages`, `jobs`, `job_executions`, `workers`, `project_locks`,
  `system_kv`, and `build_artifacts`) in its own relational table with primary-key
  upserts (`INSERT ... ON CONFLICT DO UPDATE`), `UNIQUE (idempotency_key)` on jobs,
  optimistic `lock_version` tracking on projects, and atomic per-operation transactions.
- Only records added, modified, or explicitly deleted since the store's baseline
  snapshot are written during a transaction, and concurrent updates committed by
  other connections are merged back into memory inside the same transaction.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
from urllib.parse import unquote, urlparse, urlunparse

MIGRATION_0001_PATH = Path(__file__).resolve().parent.parent / "migrations" / "0001_supabase_postgres_schema.sql"
MIGRATION_VERSION = "0001_supabase_postgres_schema"
DEFAULT_PG_CONNECT_TIMEOUT_SECONDS = 10


def migration_0001_checksum() -> str:
    """Return the deterministic SHA-256 hex digest of migrations/0001_supabase_postgres_schema.sql."""
    normalized = MIGRATION_0001_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

TRACKED_COLLECTIONS = (
    "leads",
    "research_evidence",
    "qualifications",
    "projects",
    "builds",
    "validations",
    "reviews",
    "deployments",
    "outreach",
    "jobs",
    "executions",
    "workers",
    "project_locks",
    "build_artifacts",
)


class DatabasePersistenceError(RuntimeError):
    """Raised when persistent database storage is required or configured and an operation fails."""


# Fields that legitimately differ between two equivalent writes of the same record (retry,
# replay, or a second worker touching the same row), used only to decide whether a concurrent
# insert of an already-existing record ID is an idempotent replay or a real conflict.
VOLATILE_RECORD_FIELDS = frozenset({
    "created_at",
    "updated_at",
    "reviewed_at",
    "completed_at",
    "started_at",
    "heartbeat_at",
    "last_heartbeat",
    "timestamp",
})


def _comparable_record(rec: Any) -> Any:
    if not isinstance(rec, dict):
        return rec
    return {k: v for k, v in rec.items() if k not in VOLATILE_RECORD_FIELDS}


def _conflict_owner_label(rec: Any) -> str:
    """Describe which lead/project/build already owns a conflicting row so the error is actionable."""
    if not isinstance(rec, dict):
        return "unknown owner"
    parts = [
        f"{field}={rec[field]}"
        for field in ("lead_id", "project_id", "build_id", "job_id", "worker_id", "lease_id", "validation_id", "deployment_id")
        if rec.get(field)
    ]
    return ", ".join(parts) if parts else "unknown owner"


def _lease_is_live(rec: Any, *, now_value: datetime | None = None) -> bool:
    expires_at = (rec or {}).get("expires_at") if isinstance(rec, dict) else None
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        # An unparseable expiry is treated as live: never assume another worker's lock has lapsed.
        return True
    return parsed > (now_value or datetime.now(timezone.utc))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redact_database_url(url: str | None) -> str:
    """Return a safe representation of a database URL with password/credentials redacted."""
    if not url:
        return "<unset>"
    raw = str(url).strip()
    redacted = raw
    try:
        parsed = urlparse(raw)
        if parsed.password:
            user = parsed.username or ""
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{user}:***@{host}{port}" if user else f"***@{host}{port}"
            redacted = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass
    redacted = re.sub(r"(://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", redacted)
    redacted = re.sub(r"((?:password|passwd|pwd)\s*=\s*)[^\s&'\"]+", r"\1***", redacted, flags=re.IGNORECASE)
    return redacted


def sanitize_db_error(exc: BaseException | str, raw_url: str | None = None) -> str:
    """Redact any raw database URL or password from an exception message before logging or raising."""
    msg = str(exc)
    if raw_url:
        raw_str = str(raw_url).strip()
        if raw_str:
            msg = msg.replace(raw_str, redact_database_url(raw_str))
        pw_candidates: set[str] = set()
        try:
            parsed = urlparse(raw_str)
            if parsed.password:
                pw_candidates.add(parsed.password)
                pw_candidates.add(unquote(parsed.password))
        except Exception:
            pass
        m = re.search(r"://[^:/\s]+:([^@\s]+)@", raw_str)
        if m:
            pw_candidates.add(m.group(1))
            pw_candidates.add(unquote(m.group(1)))
        for secret_variant in sorted(pw_candidates, key=len, reverse=True):
            if secret_variant:
                msg = msg.replace(secret_variant, "***")
    msg = re.sub(r"(://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", msg)
    msg = re.sub(r"((?:password|passwd|pwd)\s*=\s*)[^\s&'\"]+", r"\1***", msg, flags=re.IGNORECASE)
    return msg


def supabase_project_ref(host: str | None) -> str | None:
    """The Supabase project reference carried by a hostname, when the hostname carries one.

    Direct and session-pool hostnames name the project (`db.<ref>.supabase.co`); the transaction
    pooler hostnames Supabase publishes (`aws-0-<region>.pooler.supabase.com`) do not. A `None` result
    therefore means "this URL does not identify a project", never "there is no project" - the preflight
    says so instead of guessing, because applying a schema to the wrong project is not undoable by hand.
    """
    if not host:
        return None
    labels = str(host).lower().strip().split(".")
    if "supabase" not in labels:
        return None
    index = labels.index("supabase")
    if index == 0:
        return None
    candidate = labels[index - 1]
    return candidate if re.fullmatch(r"[a-z0-9]{14,32}", candidate) else None


def is_production_mode(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    app_env = str(source.get("AI_WEBSITE_AGENT_ENV") or source.get("RENDER") or "").strip().lower()
    backend = str(source.get("AI_WEBSITE_AGENT_STORAGE_BACKEND") or "").strip().lower()
    return app_env in {"production", "prod", "true", "1"} or backend in {"postgres", "postgresql", "supabase"}


def configured_database_url(
    explicit_url: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> str | None:
    source = env if env is not None else os.environ
    prod = is_production_mode(source)
    candidate: str | None = None
    if explicit_url is not None and str(explicit_url).strip():
        candidate = str(explicit_url).strip()
    else:
        for key in ("AI_WEBSITE_AGENT_DATABASE_URL", "DATABASE_URL", "SUPABASE_DB_URL"):
            val = str(source.get(key) or "").strip()
            if val:
                candidate = val
                break
    if candidate:
        scheme = urlparse(candidate).scheme.lower()
        if prod and scheme == "sqlite":
            raise DatabasePersistenceError(
                f"Refusing SQLite database URL ({redact_database_url(candidate)}) in production / PostgreSQL mode; "
                "Supabase PostgreSQL ('postgresql://...') is required."
            )
        return candidate
    if prod:
        raise DatabasePersistenceError(
            "Production / PostgreSQL storage mode is enabled, but no DATABASE_URL "
            "(or AI_WEBSITE_AGENT_DATABASE_URL) is configured. Refusing to fall back to temporary local JSON storage."
        )
    return None


def extract_research_evidence_entries(lead_id: str, lead_record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract normalized research_evidence rows from a lead record without mutating the lead."""
    entries: dict[str, dict[str, Any]] = {}
    created_at = str(lead_record.get("updated_at") or lead_record.get("created_at") or _now_iso())
    research_rec = lead_record.get("research_record")
    research_block = research_rec.get("research") if isinstance(research_rec, dict) else None
    evidence_records = research_block.get("evidenceRecords") if isinstance(research_block, dict) else None
    if isinstance(evidence_records, dict):
        for field_name, ev in evidence_records.items():
            if not isinstance(ev, dict):
                continue
            ev_id = f"{lead_id}:{field_name}"
            src_urls = ev.get("sourceUrls") or ev.get("source_urls") or lead_record.get("source_urls") or []
            entries[ev_id] = {
                "evidence_id": ev_id,
                "lead_id": lead_id,
                "field_name": str(field_name),
                "evidence_tier": str(ev.get("evidenceTier") or ev.get("tier") or "SOURCE_URL_ONLY"),
                "verification_status": str(ev.get("status") or ev.get("verificationStatus") or "RECORDED"),
                "source_urls": list(src_urls) if isinstance(src_urls, list) else [],
                "value": ev.get("value"),
                "candidates": ev.get("candidates"),
                "created_at": created_at,
                "updated_at": created_at,
            }
    if not entries and lead_record.get("source_urls"):
        ev_id = f"{lead_id}:source_urls"
        entries[ev_id] = {
            "evidence_id": ev_id,
            "lead_id": lead_id,
            "field_name": "source_urls",
            "evidence_tier": "SOURCE_URL_ONLY",
            "verification_status": str(lead_record.get("qualification_status") or "RECORDED"),
            "source_urls": list(lead_record.get("source_urls") or []),
            "value": list(lead_record.get("source_urls") or []),
            "created_at": created_at,
            "updated_at": created_at,
        }
    return entries


class RelationalStateBackend:
    """Transactional relational persistence backend for TrackingStore.

    Supports:
    - PostgreSQL / Supabase PostgreSQL (`postgresql://...` or `postgres://...`) using
      DB-API 2.0 (`psycopg` v3, `psycopg2`, or an injected `connection_factory`).
    - SQLite relational compatibility mode (`sqlite:///path/to/db.sqlite`) for local
      multi-connection transactional testing when a live PostgreSQL server is unavailable.
    """

    def __init__(
        self,
        database_url: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
        auto_migrate: bool = True,
        connect_timeout: int | None = None,
    ) -> None:
        raw_url = str(database_url or "").strip()
        if not raw_url:
            raise DatabasePersistenceError("database_url must be a non-empty database connection string.")
        self.database_url = raw_url
        self.redacted_url = redact_database_url(raw_url)
        self._connection_factory = connection_factory
        raw_timeout = connect_timeout if connect_timeout is not None else int(
            os.environ.get("AI_WEBSITE_AGENT_DB_CONNECT_TIMEOUT") or DEFAULT_PG_CONNECT_TIMEOUT_SECONDS
        )
        self.connect_timeout = max(1, int(raw_timeout))
        scheme = urlparse(raw_url).scheme.lower()
        if scheme in {"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"}:
            self.dialect = "postgres"
        elif scheme == "sqlite":
            if is_production_mode():
                raise DatabasePersistenceError(
                    f"Refusing SQLite database URL ({self.redacted_url}) in production / PostgreSQL mode; "
                    "Supabase PostgreSQL ('postgresql://...') is required."
                )
            self.dialect = "sqlite"
        else:
            raise DatabasePersistenceError(
                f"Unsupported database URL scheme '{scheme}' in {self.redacted_url}. "
                "Expected 'postgresql://' (Supabase/PostgreSQL) or 'sqlite:///' (local test DB)."
            )
        self._loaded_snapshot: dict[str, Any] = self._empty_snapshot()
        self._initialized = False
        if auto_migrate:
            self.ensure_schema()

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        snap: dict[str, Any] = {col: {} for col in TRACKED_COLLECTIONS}
        snap["worker_routing"] = {}
        return snap

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            try:
                return self._connection_factory()
            except Exception as exc:
                safe_msg = sanitize_db_error(exc, self.database_url)
                raise DatabasePersistenceError(
                    f"Failed to open database connection via custom factory for {self.redacted_url}: {safe_msg}"
                ) from exc

        if self.dialect == "sqlite":
            parsed = urlparse(self.database_url)
            if self.database_url in {"sqlite:///:memory:", "sqlite://"}:
                db_path = ":memory:"
            else:
                raw_path = parsed.path or ""
                if raw_path.startswith("//"):
                    db_path = raw_path[1:]
                elif raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
                    db_path = raw_path[1:]
                else:
                    db_path = raw_path
                if db_path and db_path != ":memory:":
                    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            try:
                conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
                conn.execute("PRAGMA busy_timeout = 30000")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA foreign_keys = OFF")
                return conn
            except Exception as exc:
                safe_msg = sanitize_db_error(exc, self.database_url)
                raise DatabasePersistenceError(
                    f"Failed to connect to SQLite database {self.redacted_url}: {safe_msg}"
                ) from exc

        # PostgreSQL driver resolution (`psycopg` v3 or `psycopg2`)
        clean_pg_url = re.sub(r"([?&])pgbouncer=(?:true|false|1|0)(&|$)", lambda m: m.group(1) if m.group(2) else "", self.database_url, flags=re.IGNORECASE)
        clean_pg_url = re.sub(r"[?&]$", "", clean_pg_url)
        last_import_error: Exception | None = None
        for module_name in ("psycopg", "psycopg2"):
            try:
                driver = __import__(module_name)
                connect_kwargs: dict[str, Any] = {}
                if "connect_timeout=" not in clean_pg_url:
                    connect_kwargs["connect_timeout"] = self.connect_timeout
                if module_name == "psycopg":
                    # Disable server-side prepared statements so Supabase Supavisor
                    # transaction pooling (port 6543) and session/direct connections (port 5432) both work safely.
                    connect_kwargs["prepare_threshold"] = None
                conn = driver.connect(clean_pg_url, **connect_kwargs)
                if hasattr(conn, "autocommit"):
                    conn.autocommit = False
                return conn
            except ImportError as imp_err:
                last_import_error = imp_err
                continue
            except Exception as exc:
                safe_msg = sanitize_db_error(exc, self.database_url)
                raise DatabasePersistenceError(
                    f"Failed to connect to PostgreSQL database {self.redacted_url}: {safe_msg}"
                ) from exc

        raise DatabasePersistenceError(
            f"PostgreSQL driver ('psycopg' or 'psycopg2') is required to connect to {self.redacted_url}, "
            f"but neither package is installed ({last_import_error}). "
            "Install dependencies from requirements.txt before running in PostgreSQL mode."
        )

    def _placeholder(self) -> str:
        return "%s" if self.dialect == "postgres" else "?"

    def _json_param(self, value: Any) -> str:
        return json.dumps(value, sort_keys=True)

    def _json_cast(self) -> str:
        return "::jsonb" if self.dialect == "postgres" else ""

    def _exec(self, cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        result = cursor.execute(sql, params)
        return result if result is not None and hasattr(result, "fetchall") else cursor

    #: Tables this application owns. Reported on, never assumed to exist.
    OWNED_TABLES = (
        "schema_migrations", "leads", "research_evidence", "qualifications", "projects", "builds",
        "validations", "reviews", "deployments", "outreach_packages", "jobs", "job_executions",
        "workers", "project_locks", "system_kv", "build_artifacts",
    )
    #: Counted in a preflight so a database that already holds operational records is visible before
    #: anything is applied to it. A fresh migration and a shared production database look identical
    #: to `CREATE TABLE IF NOT EXISTS`, which is precisely why the counts are reported.
    COUNTED_TABLES = ("leads", "projects", "jobs", "reviews", "deployments", "outreach_packages")

    def identity(self) -> dict[str, Any]:
        """Where this connection string points, with every secret removed.

        ``redacted_url`` cannot tell two Supabase projects apart, so the identity names the host, port,
        database, and the project reference embedded in the hostname. Those are the values an operator
        compares against the Supabase dashboard before running the migration command.
        """
        parsed = urlparse(self.database_url)
        host = (parsed.hostname or "").lower() or None
        raw_path = parsed.path or ""
        database = raw_path[1:] if raw_path.startswith("//") else raw_path.lstrip("/")
        identity: dict[str, Any] = {
            "dialect": self.dialect,
            "scheme": parsed.scheme.lower(),
            "host": host,
            "port": parsed.port,
            "database": database or None,
            "url": self.redacted_url,
            "supabase_project_ref": supabase_project_ref(host),
            "is_local_host": host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"},
            "connect_timeout_seconds": self.connect_timeout,
        }
        if self.dialect == "sqlite":
            identity["host"] = None
            identity["port"] = None
            identity["is_local_host"] = True
        return identity

    def _probe(self, cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> tuple[list[Any], str | None]:
        """Run one read-only probe; return ``(rows, fault)`` instead of raising.

        Each question a preflight asks is independent: a connection that forbids ``information_schema``
        or a database with no tables yet should produce one `unknown` field, not a crash that tempts the
        operator to skip the preflight and run the migration blind.
        """
        try:
            return list(self._exec(cursor, sql, params).fetchall() or []), None
        except Exception as error:  # any DB-API fault is data for the report, never a traceback
            return [], sanitize_db_error(error, self.database_url)

    def preflight(self) -> dict[str, Any]:
        """Identify the target database and report its migration state without running any DDL.

        Read-only by construction: one connection, SELECTs only, then close. No table is created, no row
        is written, and no local JSON fallback is possible from here. A fault is reported in
        ``blockers`` with the credential already redacted, so the answer to "can the app reach the
        database you intend it to" is a document an operator can act on.
        """
        report: dict[str, Any] = {
            "status": "ready",
            "checked_at": _now_iso(),
            "ddl_executed": False,
            "wrote_anything": False,
            "identity": self.identity(),
            "reachable": False,
            "server_version": None,
            "schema": {"present_tables": [], "missing_tables": list(self.OWNED_TABLES), "row_counts": {},
                       "populated": None},
            "migration": {
                "version": MIGRATION_VERSION,
                "file": MIGRATION_0001_PATH.name,
                "expected_checksum": migration_0001_checksum(),
                "recorded_checksum": None,
                "applied": False,
                "checksum_matches": False,
                # `ensure_schema` is `CREATE TABLE IF NOT EXISTS` plus a keyed checksum row, and it
                # re-verifies rather than rewrites: repeating it is safe, so say so explicitly.
                "safe_to_repeat": True,
            },
            "blockers": [],
            "pending": [],
            "warnings": [],
            "next_action": None,
        }
        try:
            connection = self._connect()
        except Exception as error:  # DatabasePersistenceError wraps driver faults, and so should this
            # "Cannot reach it" and "reached it, something is wrong" send the operator to different
            # places - a firewall or a wrong hostname versus a schema that was never applied.
            report["status"] = "unreachable"
            detail = sanitize_db_error(error, self.database_url)
            # `_connect` already names the target it failed on. Prefixing it again buries the part an
            # operator acts on ("Connection refused - is the server running?") under a repeated URL.
            report["blockers"].append(
                detail if self.redacted_url in detail else f"Could not connect to {self.redacted_url}: {detail}"
            )
            report["next_action"] = (
                "Start PostgreSQL/Supabase and re-check the connection string, then run "
                "`python3 -m tracking.cli preflight-db` again."
            )
            return report

        try:
            cursor = connection.cursor()
            version_sql = "SELECT version()" if self.dialect == "postgres" else "SELECT sqlite_version()"
            rows, fault = self._probe(cursor, version_sql)
            if rows:
                report["server_version"] = str(rows[0][0])
            elif fault:
                report["warnings"].append(f"Server version could not be read: {fault}")
            report["reachable"] = True

            table_sql = (
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
                if self.dialect == "postgres"
                else "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            rows, fault = self._probe(cursor, table_sql)
            if fault:
                report["warnings"].append(
                    f"Table listing could not be read: {fault}. Existence is reported as unknown, not absent."
                )
                report["schema"]["present_tables"] = []
                report["schema"]["missing_tables"] = []
            else:
                found = {str(row[0]) for row in rows}
                report["schema"]["present_tables"] = sorted(found & set(self.OWNED_TABLES))
                report["schema"]["missing_tables"] = sorted(set(self.OWNED_TABLES) - found)

            report["migration"]["needs_migration"] = False
            if "schema_migrations" in {str(row[0]) for row in rows} or not report["schema"]["missing_tables"]:
                rows, fault = self._probe(
                    cursor,
                    f"SELECT checksum FROM schema_migrations WHERE version = {self._placeholder()}",
                    (MIGRATION_VERSION,),
                )
                if fault:
                    report["blockers"].append(f"Migration history could not be read: {fault}")
                elif rows:
                    recorded = rows[0][0]
                    report["migration"]["applied"] = True
                    report["migration"]["recorded_checksum"] = str(recorded) if recorded else None
                    if not recorded:
                        report["blockers"].append(
                            f"Migration {MIGRATION_VERSION} is recorded without a checksum on "
                            f"{self.redacted_url}; run `python3 -m tracking.cli migrate-db` to stamp it."
                        )
                    elif str(recorded) != report["migration"]["expected_checksum"]:
                        report["migration"]["safe_to_repeat"] = False
                        report["blockers"].append(
                            f"Migration checksum mismatch for {MIGRATION_VERSION} on {self.redacted_url}: "
                            f"recorded {recorded}, expected {report['migration']['expected_checksum']}. "
                            "The applied schema is not the one this checkout ships; reconcile by hand "
                            "rather than re-running the migration."
                        )
                    else:
                        report["migration"]["checksum_matches"] = True
                else:
                    report["migration"]["needs_migration"] = True
                    report["pending"].append(
                        f"Migration {MIGRATION_VERSION} has not been applied on {self.redacted_url}."
                    )
                    report["next_action"] = (
                        "Run `python3 -m tracking.cli migrate-db` to apply migration "
                        f"{MIGRATION_VERSION}; it is additive and safe to repeat."
                    )
            else:
                report["migration"]["needs_migration"] = True
                report["pending"].append(
                    "The schema is absent (no `schema_migrations` table): migration has not been applied here."
                )
                report["next_action"] = (
                    "Confirm the identity above matches the intended Supabase project, then run "
                    "`python3 -m tracking.cli migrate-db` to apply the schema."
                )

            for table in self.COUNTED_TABLES:
                if table not in report["schema"]["present_tables"]:
                    continue
                rows, fault = self._probe(cursor, f"SELECT count(*) FROM {table}")
                if fault:
                    report["warnings"].append(f"Row count for {table} could not be read: {fault}")
                elif rows:
                    report["schema"]["row_counts"][table] = int(rows[0][0])
            counts = report["schema"]["row_counts"]
            if counts:
                report["schema"]["populated"] = any(value > 0 for value in counts.values())
                if report["schema"]["populated"]:
                    report["warnings"].append(
                        "This database already holds records ("
                        + ", ".join(f"{name}={count}" for name, count in sorted(counts.items()) if count)
                        + "). Migration is additive, but a shared or live database deserves a deliberate "
                        "confirmation."
                    )
        finally:
            try:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass

        unresolved = report["identity"]["supabase_project_ref"] is None and self.dialect == "postgres" and not report["identity"]["is_local_host"]
        if unresolved:
            report["warnings"].append(
                "This hostname does not carry a Supabase project reference (a transaction-pooler host "
                "such as aws-0-<region>.pooler.supabase.com does not), so `--expect-project-ref` cannot "
                "confirm the project from the URL alone: verify the connection string against the "
                "Supabase dashboard before migrating."
            )
        # "Blocked" and "not migrated yet" are different operator problems: one is a fault to fix, the
        # other is a step to take. Collapsing them would send an operator off to debug a healthy database.
        if not report["reachable"]:
            report["status"] = "unreachable"
        elif report["blockers"]:
            report["status"] = "blocked"
        elif report["migration"]["applied"] and report["migration"]["checksum_matches"]:
            report["status"] = "ready"
            if not report["next_action"]:
                report["next_action"] = (
                    "Schema verified; no DDL was executed by this command. Start the API with "
                    "`python3 -m orchestration.cli serve` and check `python3 -m orchestration.cli preflight`."
                )
        else:
            report["status"] = "needs_migration"
        report["blockers"] = report["blockers"] + report["pending"]
        return report

    def verify_schema(self) -> dict[str, Any]:
        """Verify that schema_migrations exists and matches the expected migration checksum without running DDL."""
        expected_checksum = migration_0001_checksum()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._placeholder()
            res = self._exec(
                cur,
                f"SELECT version, checksum FROM schema_migrations WHERE version = {ph}",
                (MIGRATION_VERSION,),
            )
            rows = res.fetchall()
            if not rows:
                raise DatabasePersistenceError(
                    f"Database schema migration {MIGRATION_VERSION} has not been applied on {self.redacted_url}. "
                    "Run 'python3 -m tracking.cli migrate-db' first."
                )
            recorded_checksum = rows[0][1]
            if not recorded_checksum:
                raise DatabasePersistenceError(
                    f"Migration checksum for {MIGRATION_VERSION} is missing on {self.redacted_url}. "
                    "Run 'python3 -m tracking.cli migrate-db' to record and verify the migration checksum."
                )
            if str(recorded_checksum) != expected_checksum:
                raise DatabasePersistenceError(
                    f"Migration checksum mismatch for {MIGRATION_VERSION} on {self.redacted_url}: "
                    f"recorded {recorded_checksum}, expected {expected_checksum}."
                )
            self._initialized = True
            return {
                "status": "VERIFIED",
                "migration_version": MIGRATION_VERSION,
                "checksum": expected_checksum,
                "dialect": self.dialect,
                "database": self.redacted_url,
            }
        except DatabasePersistenceError:
            self._safe_rollback(conn)
            raise
        except Exception as exc:
            self._safe_rollback(conn)
            safe_msg = sanitize_db_error(exc, self.database_url)
            raise DatabasePersistenceError(
                f"Database schema verification failed on {self.redacted_url}: {safe_msg}"
            ) from exc
        finally:
            self._safe_close(conn)

    def ensure_schema(self) -> None:
        if self._initialized:
            return
        expected_checksum = migration_0001_checksum()
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._placeholder()
            # Fast path on already-migrated databases: check schema_migrations before running DDL
            already_applied = False
            try:
                res_check = self._exec(
                    cur,
                    f"SELECT checksum FROM schema_migrations WHERE version = {ph}",
                    (MIGRATION_VERSION,),
                )
                check_rows = res_check.fetchall()
                if check_rows:
                    rec_sum = check_rows[0][0]
                    if rec_sum and str(rec_sum) != expected_checksum:
                        raise DatabasePersistenceError(
                            f"Migration checksum mismatch for {MIGRATION_VERSION} on {self.redacted_url}: "
                            f"recorded {rec_sum}, expected {expected_checksum}."
                        )
                    if rec_sum == expected_checksum:
                        already_applied = True
            except DatabasePersistenceError:
                raise
            except Exception:
                self._safe_rollback(conn)

            if not already_applied:
                if self.dialect == "sqlite":
                    self._exec(cur, "BEGIN IMMEDIATE")
                    for stmt in self._sqlite_schema_statements():
                        self._exec(cur, stmt)
                else:
                    migration_sql = MIGRATION_0001_PATH.read_text(encoding="utf-8")
                    self._exec(cur, migration_sql)

                res = self._exec(
                    cur,
                    f"SELECT checksum FROM schema_migrations WHERE version = {ph}",
                    (MIGRATION_VERSION,),
                )
                existing_rows = res.fetchall()
                if existing_rows:
                    recorded_checksum = existing_rows[0][0]
                    if recorded_checksum and str(recorded_checksum) != expected_checksum:
                        raise DatabasePersistenceError(
                            f"Migration checksum mismatch for {MIGRATION_VERSION} on {self.redacted_url}: "
                            f"recorded {recorded_checksum}, expected {expected_checksum}."
                        )
                    if not recorded_checksum:
                        self._exec(
                            cur,
                            f"UPDATE schema_migrations SET checksum = {ph} WHERE version = {ph}",
                            (expected_checksum, MIGRATION_VERSION),
                        )
                else:
                    self._exec(
                        cur,
                        f"INSERT INTO schema_migrations (version, description, checksum, applied_at) "
                        f"VALUES ({ph}, {ph}, {ph}, {ph}) "
                        f"ON CONFLICT (version) DO UPDATE SET checksum = COALESCE(schema_migrations.checksum, EXCLUDED.checksum)",
                        (MIGRATION_VERSION, "Initial normalized schema", expected_checksum, _now_iso()),
                    )

                if self.dialect == "sqlite":
                    self._exec(cur, "COMMIT")
                elif hasattr(conn, "commit"):
                    conn.commit()
            elif hasattr(conn, "commit") and self.dialect != "sqlite":
                conn.commit()
            self._initialized = True
        except DatabasePersistenceError:
            self._safe_rollback(conn)
            raise
        except Exception as exc:
            self._safe_rollback(conn)
            safe_msg = sanitize_db_error(exc, self.database_url)
            raise DatabasePersistenceError(
                f"Failed to initialize database schema on {self.redacted_url}: {safe_msg}"
            ) from exc
        finally:
            self._safe_close(conn)

    @staticmethod
    def _sqlite_schema_statements() -> list[str]:
        return [
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                checksum TEXT,
                applied_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS leads (
                lead_id TEXT PRIMARY KEY,
                business_name TEXT NOT NULL,
                data_classification TEXT NOT NULL,
                qualification_status TEXT NOT NULL,
                website_status TEXT,
                city TEXT,
                country TEXT,
                phone TEXT,
                email TEXT,
                existing_website TEXT,
                instagram_url TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS research_evidence (
                evidence_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                evidence_tier TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                source_urls TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS qualifications (
                qualification_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                status TEXT NOT NULL,
                website_status TEXT,
                score INTEGER,
                priority TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                project_status TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                review_status TEXT NOT NULL,
                assigned_build_id TEXT,
                latest_validation_id TEXT,
                latest_deployment_id TEXT,
                approved_build_id TEXT,
                approved_action TEXT,
                isolated_preview_project TEXT,
                lock_version INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS builds (
                build_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                template_id TEXT,
                status TEXT NOT NULL,
                output_location TEXT,
                artifact_sha256 TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS validations (
                validation_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                build_id TEXT NOT NULL,
                lead_id TEXT,
                status TEXT NOT NULL,
                score INTEGER,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                build_id TEXT,
                decision TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                intended_action TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS deployments (
                deployment_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                build_id TEXT NOT NULL,
                status TEXT NOT NULL,
                deployment_url TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS outreach_packages (
                outreach_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                lead_id TEXT NOT NULL,
                build_id TEXT NOT NULL,
                deployment_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE NOT NULL,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                lead_id TEXT,
                project_id TEXT,
                build_id TEXT,
                validation_id TEXT,
                deployment_id TEXT,
                parent_job_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                dry_run INTEGER NOT NULL DEFAULT 1,
                heartbeat_at TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS job_executions (
                execution_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                worker_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                worker_type TEXT NOT NULL,
                status TEXT NOT NULL,
                health_status TEXT NOT NULL,
                current_job_id TEXT,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS project_locks (
                project_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                lease_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS system_kv (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS build_artifacts (
                build_id TEXT PRIMARY KEY,
                project_id TEXT,
                lead_id TEXT NOT NULL,
                template_id TEXT,
                artifact_path TEXT NOT NULL,
                html_content TEXT NOT NULL,
                html_bytes INTEGER NOT NULL,
                html_sha256 TEXT NOT NULL,
                build_manifest TEXT NOT NULL,
                review_package_json TEXT,
                review_readme_md TEXT,
                review_package_sha256 TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
        ]

    @staticmethod
    def _safe_rollback(conn: Any) -> None:
        try:
            if hasattr(conn, "rollback"):
                conn.rollback()
        except Exception:
            pass

    @staticmethod
    def _safe_close(conn: Any) -> None:
        try:
            if hasattr(conn, "close"):
                conn.close()
        except Exception:
            pass

    @staticmethod
    def _decode_json(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return copy.deepcopy(raw)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str) and raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        return {}

    def load_state(self) -> dict[str, Any]:
        """Load the full tracking state from normalized relational tables."""
        self.ensure_schema()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.dialect == "sqlite":
                self._exec(cur, "BEGIN")
            state = self._read_all_tables(cur)
            if self.dialect == "sqlite":
                self._exec(cur, "COMMIT")
            elif hasattr(conn, "commit"):
                conn.commit()
            self._loaded_snapshot = copy.deepcopy(state)
            return copy.deepcopy(state)
        except DatabasePersistenceError:
            self._safe_rollback(conn)
            raise
        except Exception as exc:
            self._safe_rollback(conn)
            safe_msg = sanitize_db_error(exc, self.database_url)
            raise DatabasePersistenceError(
                f"Failed to load state from database {self.redacted_url}: {safe_msg}"
            ) from exc
        finally:
            self._safe_close(conn)

    def _read_all_tables(self, cur: Any) -> dict[str, Any]:
        state = self._empty_snapshot()
        table_map = {
            "leads": ("leads", "lead_id"),
            "research_evidence": ("research_evidence", "evidence_id"),
            "qualifications": ("qualifications", "qualification_id"),
            "projects": ("projects", "project_id"),
            "builds": ("builds", "build_id"),
            "validations": ("validations", "validation_id"),
            "reviews": ("reviews", "review_id"),
            "deployments": ("deployments", "deployment_id"),
            "outreach": ("outreach_packages", "outreach_id"),
            "jobs": ("jobs", "job_id"),
            "executions": ("job_executions", "execution_id"),
            "workers": ("workers", "worker_id"),
            "project_locks": ("project_locks", "project_id"),
        }
        for collection, (table_name, pk_col) in table_map.items():
            if collection == "projects":
                res = self._exec(cur, "SELECT project_id, payload, lock_version FROM projects ORDER BY project_id")
                for row in res.fetchall():
                    rec_id = str(row[0])
                    decoded = self._decode_json(row[1])
                    if row[2] is not None:
                        decoded["lock_version"] = int(row[2])
                    state[collection][rec_id] = decoded
            else:
                res = self._exec(cur, f"SELECT {pk_col}, payload FROM {table_name} ORDER BY {pk_col}")
                for row in res.fetchall():
                    rec_id = str(row[0])
                    state[collection][rec_id] = self._decode_json(row[1])

        res_art = self._exec(
            cur,
            "SELECT build_id, project_id, lead_id, template_id, artifact_path, "
            "html_content, html_bytes, html_sha256, build_manifest, "
            "review_package_json, review_readme_md, review_package_sha256, payload, created_at, updated_at "
            "FROM build_artifacts ORDER BY build_id",
        )
        for row in res_art.fetchall():
            b_id = str(row[0])
            payload = self._decode_json(row[12])
            manifest = self._decode_json(row[8])
            rev_pkg = self._decode_json(row[9]) if row[9] is not None else None
            payload.update({
                "build_id": b_id,
                "project_id": row[1],
                "lead_id": row[2],
                "template_id": row[3],
                "artifact_path": row[4],
                "html_content": row[5],
                "html_bytes": int(row[6]),
                "html_sha256": row[7],
                "build_manifest": manifest,
                "review_package_json": rev_pkg,
                "review_readme_md": row[10],
                "review_package_sha256": row[11],
                "created_at": str(row[13]) if row[13] is not None else _now_iso(),
                "updated_at": str(row[14]) if row[14] is not None else _now_iso(),
            })
            state["build_artifacts"][b_id] = payload

        res_kv = self._exec(cur, "SELECT key, payload FROM system_kv WHERE key = 'worker_routing'")
        kv_rows = res_kv.fetchall()
        if kv_rows:
            state["worker_routing"] = self._decode_json(kv_rows[0][1])

        return state

    def sync_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Atomically persist only modified/added/deleted records and merge concurrent DB rows."""
        self.ensure_schema()

        # Populate normalized `research_evidence` rows from leads before diffing
        evidence_bucket = state.setdefault("research_evidence", {})
        for lead_id, lead_rec in (state.get("leads") or {}).items():
            if isinstance(lead_rec, dict):
                extracted = extract_research_evidence_entries(str(lead_id), lead_rec)
                for ev_id, ev_row in extracted.items():
                    evidence_bucket[ev_id] = ev_row

        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.dialect == "sqlite":
                self._exec(cur, "BEGIN IMMEDIATE")

            # Read current DB state inside the transaction so we can safely merge concurrent changes
            db_current = self._read_all_tables(cur)

            # Pass 1: Delete records removed by this store instance in REVERSE dependency order
            # so child records (validations, deployments, builds, projects) are removed before parents.
            for collection in reversed(TRACKED_COLLECTIONS):
                baseline_col = self._loaded_snapshot.get(collection) or {}
                current_col = state.get(collection) or {}
                db_col = db_current.get(collection) or {}
                for rec_id in list(baseline_col.keys()):
                    if rec_id not in current_col:
                        self._delete_record(cur, collection, rec_id)
                        db_col.pop(rec_id, None)

            # Pass 2: Upsert records added or modified by this store instance in FORWARD dependency order
            for collection in TRACKED_COLLECTIONS:
                baseline_col = self._loaded_snapshot.get(collection) or {}
                current_col = state.get(collection) or {}
                db_col = db_current.get(collection) or {}

                for rec_id, rec in current_col.items():
                    if rec_id not in baseline_col or baseline_col[rec_id] != rec:
                        db_rec = db_col.get(rec_id)
                        if db_rec is not None and rec_id not in baseline_col:
                            # Another connection committed this record ID after our snapshot was
                            # loaded. Never overwrite it silently.
                            if _comparable_record(db_rec) == _comparable_record(rec):
                                # Idempotent replay (same content, retry after a partial failure).
                                continue
                            if collection == "project_locks" and not _lease_is_live(db_rec):
                                pass  # The stored lease has lapsed; taking it over is safe.
                            elif collection == "project_locks":
                                raise DatabasePersistenceError(
                                    f"Project lock conflict for {rec_id}: worker {(db_rec.get('worker_id'))} "
                                    f"holds a live lease until {db_rec.get('expires_at')} "
                                    f"(job {db_rec.get('job_id')}). Refusing to overwrite another worker's lock."
                                )
                            else:
                                raise DatabasePersistenceError(
                                    f"Concurrent-insert conflict on {collection}.{rec_id}: this connection tried to "
                                    f"insert a new record, but the row already exists with different content "
                                    f"({_conflict_owner_label(db_rec)}). Load the latest state "
                                    "(TrackingStore.refresh()) and retry instead of overwriting the other writer."
                                )
                        if collection == "projects" and rec_id not in baseline_col and rec.get("project_status") == "ACTIVE":
                            # Business rule: one ACTIVE project per lead. A concurrent connection
                            # that already committed one must be reused, never duplicated.
                            duplicate_project = next(
                                (
                                    other
                                    for other_id, other in db_col.items()
                                    if other_id != rec_id
                                    and other.get("lead_id") == rec.get("lead_id")
                                    and other.get("project_status") == "ACTIVE"
                                ),
                                None,
                            )
                            if duplicate_project is not None:
                                raise DatabasePersistenceError(
                                    f"Lead {rec.get('lead_id')} already has an ACTIVE project "
                                    f"({duplicate_project.get('project_id')}) committed by another connection; "
                                    f"refusing to create duplicate project {rec_id}. Reload the latest state "
                                    "(TrackingStore.refresh()) and reuse the existing project."
                                )
                        if collection == "jobs":
                            eff_key = str(rec.get("idempotency_key") or f"idemp-{rec_id}")
                            if rec_id not in baseline_col:
                                existing_by_key = next(
                                    (
                                        j
                                        for jid, j in db_col.items()
                                        if jid != rec_id and str(j.get("idempotency_key") or f"idemp-{jid}") == eff_key
                                    ),
                                    None,
                                )
                                if existing_by_key is not None:
                                    if (
                                        existing_by_key.get("job_type") != rec.get("job_type")
                                        or (rec.get("lead_id") is not None and existing_by_key.get("lead_id") is not None and existing_by_key.get("lead_id") != rec.get("lead_id"))
                                        or (rec.get("project_id") is not None and existing_by_key.get("project_id") is not None and existing_by_key.get("project_id") != rec.get("project_id"))
                                        or (rec.get("build_id") is not None and existing_by_key.get("build_id") is not None and existing_by_key.get("build_id") != rec.get("build_id"))
                                    ):
                                        raise ValueError("Idempotency key is already associated with a different job.")
                                    continue
                            base_job = baseline_col.get(rec_id)
                            db_job = db_col.get(rec_id)
                            if (
                                isinstance(base_job, dict)
                                and isinstance(db_job, dict)
                                and base_job.get("status") in {"PENDING", "QUEUED", "BLOCKED"}
                                and db_job.get("status") == "RUNNING"
                                and rec.get("status") == "RUNNING"
                            ):
                                db_lease = (db_job.get("lease") or {}).get("lease_id")
                                local_lease = (rec.get("lease") or {}).get("lease_id")
                                if db_lease and local_lease and db_lease != local_lease:
                                    raise ValueError(
                                        f"Job {rec_id} is not claimable: concurrently claimed by worker "
                                        f"{(db_job.get('lease') or {}).get('worker_id')}."
                                    )
                        merged_rec = self._merge_concurrent_record(
                            collection,
                            rec_id,
                            baseline_col.get(rec_id),
                            db_col.get(rec_id),
                            rec,
                        )
                        if collection == "projects":
                            prev_lock_ver = int(
                                (db_col.get(rec_id) or baseline_col.get(rec_id) or {}).get("lock_version") or 0
                            )
                            merged_rec["lock_version"] = prev_lock_ver + 1
                        self._upsert_record(cur, collection, rec_id, merged_rec)

            # Persist `worker_routing` system KV if modified
            baseline_routing = self._loaded_snapshot.get("worker_routing") or {}
            current_routing = state.get("worker_routing") or {}
            if current_routing and current_routing != baseline_routing:
                ph = self._placeholder()
                cast = self._json_cast()
                now_val = _now_iso()
                self._exec(
                    cur,
                    f"INSERT INTO system_kv (key, payload, updated_at) VALUES ({ph}, {ph}{cast}, {ph}) "
                    f"ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
                    ("worker_routing", self._json_param(current_routing), now_val),
                )

            # Re-read the committed state inside the transaction so concurrent rows are visible
            fresh_state = self._read_all_tables(cur)
            if self.dialect == "sqlite":
                self._exec(cur, "COMMIT")
            elif hasattr(conn, "commit"):
                conn.commit()

            self._loaded_snapshot = copy.deepcopy(fresh_state)
            for col_key, col_val in fresh_state.items():
                if col_key == "worker_routing":
                    if isinstance(state.get("worker_routing"), dict) and isinstance(col_val, dict):
                        state["worker_routing"].clear()
                        state["worker_routing"].update(copy.deepcopy(col_val))
                    else:
                        state["worker_routing"] = copy.deepcopy(col_val)
                    continue
                target_col = state.setdefault(col_key, {})
                if isinstance(target_col, dict) and isinstance(col_val, dict):
                    for dead_id in list(target_col.keys()):
                        if dead_id not in col_val:
                            target_col.pop(dead_id, None)
                    for rec_id, fresh_rec in col_val.items():
                        if rec_id in target_col and isinstance(target_col[rec_id], dict) and isinstance(fresh_rec, dict):
                            target_col[rec_id].clear()
                            target_col[rec_id].update(copy.deepcopy(fresh_rec))
                        else:
                            target_col[rec_id] = copy.deepcopy(fresh_rec)
                else:
                    state[col_key] = copy.deepcopy(col_val)
            return state
        except ValueError:
            self._safe_rollback(conn)
            raise
        except DatabasePersistenceError:
            self._safe_rollback(conn)
            raise
        except Exception as exc:
            self._safe_rollback(conn)
            safe_msg = sanitize_db_error(exc, self.database_url)
            raise DatabasePersistenceError(
                f"Database transaction failed on {self.redacted_url} (rolled back safely): {safe_msg}"
            ) from exc
        finally:
            self._safe_close(conn)

    @staticmethod
    def _merge_concurrent_record(
        collection: str,
        rec_id: str,
        baseline_rec: dict[str, Any] | None,
        db_rec: dict[str, Any] | None,
        local_rec: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge field-level changes when the same row was touched concurrently."""
        if not isinstance(db_rec, dict) or not isinstance(baseline_rec, dict):
            return copy.deepcopy(local_rec)
        if db_rec == baseline_rec:
            return copy.deepcopy(local_rec)
        # Another transaction updated this row since our baseline load; preserve DB fields
        # that our local operation did not change, and overlay fields we changed.
        merged = copy.deepcopy(db_rec)
        for k, v in local_rec.items():
            base_val = baseline_rec.get(k)
            if base_val != v:
                db_val = db_rec.get(k)
                if isinstance(v, list) and isinstance(db_val, list) and isinstance(base_val, list) and db_val != base_val:
                    # Merge append-only lists (e.g. review_history) without dropping concurrent entries
                    combined = copy.deepcopy(db_val)
                    for item in v:
                        if item not in combined:
                            combined.append(copy.deepcopy(item))
                    merged[k] = combined
                else:
                    merged[k] = copy.deepcopy(v)
        return merged

    def _delete_record(self, cur: Any, collection: str, rec_id: str) -> None:
        table_map = {
            "leads": ("leads", "lead_id"),
            "research_evidence": ("research_evidence", "evidence_id"),
            "qualifications": ("qualifications", "qualification_id"),
            "projects": ("projects", "project_id"),
            "builds": ("builds", "build_id"),
            "validations": ("validations", "validation_id"),
            "reviews": ("reviews", "review_id"),
            "deployments": ("deployments", "deployment_id"),
            "outreach": ("outreach_packages", "outreach_id"),
            "jobs": ("jobs", "job_id"),
            "executions": ("job_executions", "execution_id"),
            "workers": ("workers", "worker_id"),
            "project_locks": ("project_locks", "project_id"),
            "build_artifacts": ("build_artifacts", "build_id"),
        }
        table_name, pk_col = table_map[collection]
        ph = self._placeholder()
        self._exec(cur, f"DELETE FROM {table_name} WHERE {pk_col} = {ph}", (str(rec_id),))

    def _upsert_record(self, cur: Any, collection: str, rec_id: str, rec: dict[str, Any]) -> None:
        ph = self._placeholder()
        cast = self._json_cast()
        now_val = _now_iso()
        payload_json = self._json_param(rec)

        if collection == "leads":
            self._exec(
                cur,
                f"""INSERT INTO leads (
                    lead_id, business_name, data_classification, qualification_status,
                    website_status, city, country, phone, email, existing_website,
                    instagram_url, payload, created_at, updated_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph}, {ph})
                ON CONFLICT (lead_id) DO UPDATE SET
                    business_name = EXCLUDED.business_name,
                    data_classification = EXCLUDED.data_classification,
                    qualification_status = EXCLUDED.qualification_status,
                    website_status = EXCLUDED.website_status,
                    city = EXCLUDED.city,
                    country = EXCLUDED.country,
                    phone = EXCLUDED.phone,
                    email = EXCLUDED.email,
                    existing_website = EXCLUDED.existing_website,
                    instagram_url = EXCLUDED.instagram_url,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    str(rec.get("business_name") or ""),
                    str(rec.get("data_classification") or "REAL_PUBLIC_DATA"),
                    str(rec.get("qualification_status") or "DISCOVERED"),
                    rec.get("website_status"),
                    rec.get("city"),
                    rec.get("country"),
                    rec.get("phone"),
                    rec.get("email"),
                    rec.get("existing_website"),
                    rec.get("instagram_url"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return

        if collection == "research_evidence":
            self._exec(
                cur,
                f"""INSERT INTO research_evidence (
                    evidence_id, lead_id, field_name, evidence_tier, verification_status,
                    source_urls, payload, created_at, updated_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph}{cast}, {ph}, {ph})
                ON CONFLICT (evidence_id) DO UPDATE SET
                    evidence_tier = EXCLUDED.evidence_tier,
                    verification_status = EXCLUDED.verification_status,
                    source_urls = EXCLUDED.source_urls,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    str(rec.get("lead_id") or ""),
                    str(rec.get("field_name") or ""),
                    str(rec.get("evidence_tier") or "SOURCE_URL_ONLY"),
                    str(rec.get("verification_status") or "RECORDED"),
                    self._json_param(rec.get("source_urls") or []),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return

        if collection == "qualifications":
            score_val = rec.get("score")
            self._exec(
                cur,
                f"""INSERT INTO qualifications (
                    qualification_id, lead_id, status, website_status, score, priority, payload, created_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph})
                ON CONFLICT (qualification_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    website_status = EXCLUDED.website_status,
                    score = EXCLUDED.score,
                    priority = EXCLUDED.priority,
                    payload = EXCLUDED.payload""",
                (
                    str(rec_id),
                    str(rec.get("lead_id") or ""),
                    str(rec.get("status") or "DISQUALIFIED"),
                    rec.get("website_status"),
                    int(score_val) if score_val is not None else None,
                    rec.get("priority"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                ),
            )
            return

        if collection == "projects":
            lock_ver = int(rec.get("lock_version") or 1)
            rec["lock_version"] = lock_ver
            payload_json = self._json_param(rec)
            self._exec(
                cur,
                f"""INSERT INTO projects (
                    project_id, lead_id, project_status, current_stage, review_status,
                    assigned_build_id, latest_validation_id, latest_deployment_id,
                    approved_build_id, approved_action, isolated_preview_project,
                    lock_version, payload, created_at, updated_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph}, {ph})
                ON CONFLICT (project_id) DO UPDATE SET
                    project_status = EXCLUDED.project_status,
                    current_stage = EXCLUDED.current_stage,
                    review_status = EXCLUDED.review_status,
                    assigned_build_id = EXCLUDED.assigned_build_id,
                    latest_validation_id = EXCLUDED.latest_validation_id,
                    latest_deployment_id = EXCLUDED.latest_deployment_id,
                    approved_build_id = EXCLUDED.approved_build_id,
                    approved_action = EXCLUDED.approved_action,
                    isolated_preview_project = EXCLUDED.isolated_preview_project,
                    lock_version = EXCLUDED.lock_version,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    str(rec.get("lead_id") or ""),
                    str(rec.get("project_status") or "ACTIVE"),
                    str(rec.get("current_stage") or "QUALIFIED"),
                    str(rec.get("review_status") or "PENDING"),
                    rec.get("assigned_build_id"),
                    rec.get("latest_validation_id"),
                    rec.get("latest_deployment_id"),
                    rec.get("approved_build_id"),
                    rec.get("approved_action"),
                    rec.get("isolated_preview_project"),
                    lock_ver,
                    payload_json,
                    str(rec.get("created_at") or now_val),
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return

        if collection == "builds":
            self._exec(
                cur,
                f"""INSERT INTO builds (
                    build_id, project_id, lead_id, template_id, status, output_location,
                    artifact_sha256, payload, created_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph})
                ON CONFLICT (build_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    output_location = EXCLUDED.output_location,
                    artifact_sha256 = EXCLUDED.artifact_sha256,
                    payload = EXCLUDED.payload""",
                (
                    str(rec_id),
                    str(rec.get("project_id") or ""),
                    str(rec.get("lead_id") or ""),
                    rec.get("template_id"),
                    str(rec.get("status") or "COMPLETED"),
                    rec.get("output_location"),
                    rec.get("artifact_sha256"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                ),
            )
            return

        if collection == "validations":
            score_val = rec.get("score")
            self._exec(
                cur,
                f"""INSERT INTO validations (
                    validation_id, project_id, build_id, lead_id, status, score, payload, created_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph})
                ON CONFLICT (validation_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    score = EXCLUDED.score,
                    payload = EXCLUDED.payload""",
                (
                    str(rec_id),
                    str(rec.get("project_id") or ""),
                    str(rec.get("build_id") or ""),
                    rec.get("lead_id"),
                    str(rec.get("status") or "FAIL"),
                    int(score_val) if score_val is not None else None,
                    payload_json,
                    str(rec.get("created_at") or now_val),
                ),
            )
            return

        if collection == "reviews":
            self._exec(
                cur,
                f"""INSERT INTO reviews (
                    review_id, project_id, build_id, decision, reviewer, intended_action, payload, created_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph})
                ON CONFLICT (review_id) DO UPDATE SET
                    decision = EXCLUDED.decision,
                    intended_action = EXCLUDED.intended_action,
                    payload = EXCLUDED.payload""",
                (
                    str(rec_id),
                    str(rec.get("project_id") or ""),
                    rec.get("build_id"),
                    str(rec.get("decision") or rec.get("status") or "PENDING"),
                    str(rec.get("reviewer") or "human-reviewer"),
                    rec.get("intended_action"),
                    payload_json,
                    str(rec.get("reviewed_at") or rec.get("timestamp") or now_val),
                ),
            )
            return

        if collection == "deployments":
            self._exec(
                cur,
                f"""INSERT INTO deployments (
                    deployment_id, project_id, build_id, status, deployment_url, payload, created_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph})
                ON CONFLICT (deployment_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    deployment_url = EXCLUDED.deployment_url,
                    payload = EXCLUDED.payload""",
                (
                    str(rec_id),
                    str(rec.get("project_id") or ""),
                    str(rec.get("build_id") or ""),
                    str(rec.get("status") or rec.get("deployment_status") or "PENDING"),
                    rec.get("deployment_url"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                ),
            )
            return

        if collection == "outreach":
            self._exec(
                cur,
                f"""INSERT INTO outreach_packages (
                    outreach_id, project_id, lead_id, build_id, deployment_id, status, payload, created_at, updated_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph}, {ph})
                ON CONFLICT (outreach_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    str(rec.get("project_id") or ""),
                    str(rec.get("lead_id") or ""),
                    str(rec.get("build_id") or ""),
                    str(rec.get("deployment_id") or ""),
                    str(rec.get("status") or "READY_FOR_REVIEW"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return

        if collection == "jobs":
            dry_run_val = bool(rec.get("dry_run", True))
            if self.dialect == "sqlite":
                dry_run_db: Any = 1 if dry_run_val else 0
            else:
                dry_run_db = dry_run_val
            self._exec(
                cur,
                f"""INSERT INTO jobs (
                    job_id, idempotency_key, job_type, status, lead_id, project_id,
                    build_id, validation_id, deployment_id, parent_job_id,
                    attempts, max_attempts, dry_run, heartbeat_at, payload, created_at, updated_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph}, {ph})
                ON CONFLICT (job_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    lead_id = EXCLUDED.lead_id,
                    project_id = EXCLUDED.project_id,
                    build_id = EXCLUDED.build_id,
                    validation_id = EXCLUDED.validation_id,
                    deployment_id = EXCLUDED.deployment_id,
                    attempts = EXCLUDED.attempts,
                    max_attempts = EXCLUDED.max_attempts,
                    heartbeat_at = EXCLUDED.heartbeat_at,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    str(rec.get("idempotency_key") or f"idemp-{rec_id}"),
                    str(rec.get("job_type") or "SCOUT"),
                    str(rec.get("status") or "PENDING"),
                    rec.get("lead_id"),
                    rec.get("project_id"),
                    rec.get("build_id"),
                    rec.get("validation_id"),
                    rec.get("deployment_id"),
                    rec.get("parent_job_id"),
                    int(rec.get("attempts") or 0),
                    int(rec.get("max_attempts") or 3),
                    dry_run_db,
                    rec.get("heartbeat_at"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return

        if collection == "executions":
            self._exec(
                cur,
                f"""INSERT INTO job_executions (
                    execution_id, job_id, worker_id, attempt, status, payload, started_at, completed_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph}, {ph})
                ON CONFLICT (execution_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    completed_at = EXCLUDED.completed_at""",
                (
                    str(rec_id),
                    str(rec.get("job_id") or ""),
                    rec.get("worker_id"),
                    int(rec.get("attempt_number") or rec.get("attempt") or 1),
                    str(rec.get("status") or "RUNNING"),
                    payload_json,
                    str(rec.get("started_at") or now_val),
                    rec.get("completed_at"),
                ),
            )
            return

        if collection == "workers":
            self._exec(
                cur,
                f"""INSERT INTO workers (
                    worker_id, worker_type, status, health_status, current_job_id, payload, updated_at
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast}, {ph})
                ON CONFLICT (worker_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    health_status = EXCLUDED.health_status,
                    current_job_id = EXCLUDED.current_job_id,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    str(rec.get("type") or rec.get("worker_type") or "OTHER"),
                    str(rec.get("status") or "IDLE"),
                    str(rec.get("health_status") or "HEALTHY"),
                    rec.get("current_job_id"),
                    payload_json,
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return

        if collection == "project_locks":
            self._exec(
                cur,
                f"""INSERT INTO project_locks (
                    project_id, job_id, worker_id, lease_id, expires_at, payload
                ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}{cast})
                ON CONFLICT (project_id) DO UPDATE SET
                    job_id = EXCLUDED.job_id,
                    worker_id = EXCLUDED.worker_id,
                    lease_id = EXCLUDED.lease_id,
                    expires_at = EXCLUDED.expires_at,
                    payload = EXCLUDED.payload""",
                (
                    str(rec_id),
                    str(rec.get("job_id") or ""),
                    str(rec.get("worker_id") or ""),
                    str(rec.get("lease_id") or ""),
                    str(rec.get("expires_at") or now_val),
                    payload_json,
                ),
            )
            return

        if collection == "build_artifacts":
            rev_pkg = rec.get("review_package_json")
            rev_pkg_param = self._json_param(rev_pkg) if rev_pkg is not None else None
            rev_pkg_cast = cast if rev_pkg is not None else ""
            self._exec(
                cur,
                f"""INSERT INTO build_artifacts (
                    build_id, project_id, lead_id, template_id, artifact_path,
                    html_content, html_bytes, html_sha256, build_manifest,
                    review_package_json, review_readme_md, review_package_sha256,
                    payload, created_at, updated_at
                ) VALUES (
                    {ph}, {ph}, {ph}, {ph}, {ph},
                    {ph}, {ph}, {ph}, {ph}{cast},
                    {ph}{rev_pkg_cast}, {ph}, {ph},
                    {ph}{cast}, {ph}, {ph}
                )
                ON CONFLICT (build_id) DO UPDATE SET
                    project_id = EXCLUDED.project_id,
                    artifact_path = EXCLUDED.artifact_path,
                    html_content = EXCLUDED.html_content,
                    html_bytes = EXCLUDED.html_bytes,
                    html_sha256 = EXCLUDED.html_sha256,
                    build_manifest = EXCLUDED.build_manifest,
                    review_package_json = COALESCE(EXCLUDED.review_package_json, build_artifacts.review_package_json),
                    review_readme_md = COALESCE(EXCLUDED.review_readme_md, build_artifacts.review_readme_md),
                    review_package_sha256 = COALESCE(EXCLUDED.review_package_sha256, build_artifacts.review_package_sha256),
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at""",
                (
                    str(rec_id),
                    rec.get("project_id"),
                    str(rec.get("lead_id") or ""),
                    rec.get("template_id"),
                    str(rec.get("artifact_path") or ""),
                    str(rec.get("html_content") or ""),
                    int(rec.get("html_bytes") or 0),
                    str(rec.get("html_sha256") or ""),
                    self._json_param(rec.get("build_manifest") or {}),
                    rev_pkg_param,
                    rec.get("review_readme_md"),
                    rec.get("review_package_sha256"),
                    payload_json,
                    str(rec.get("created_at") or now_val),
                    str(rec.get("updated_at") or now_val),
                ),
            )
            return
