"""Phase 36 cloud deployment readiness tests (Phases A, B, C, and D).

Note on test environments:
- No live Supabase PostgreSQL instance is provisioned or contacted in this sandbox
  (per project constraints: do not create or modify a real Supabase project, and outbound
  access is restricted).
- Relational database persistence, transactions, row-level concurrency, and schema
  migrations are tested in two complementary ways:
  1. Controlled DB-API 2.0 PostgreSQL dialect tests (`_MockPostgresConnection`) that
     verify `migrations/0001_supabase_postgres_schema.sql`, PostgreSQL `%s` placeholders,
     `::jsonb` casts, `ON CONFLICT (...) DO UPDATE` upserts, transaction commits, and
     transaction rollback / fail-closed `DatabasePersistenceError` behavior.
  2. Multi-connection relational SQLite compatibility tests (`sqlite:///...`) that execute
     real SQL transactions across all 14 normalized tables to verify persistence across
     service restarts, concurrent non-overwriting updates, job idempotency, retry/stale
     recovery, and durable artifact re-materialization after local file removal.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from builder.website_builder import WebsiteBuilder
from orchestration.api import make_handler, resolve_bind_config, serve
from orchestration.jobs import JobService, WorkerResult
from orchestration.workers import LocalPipelineWorker, WorkerRegistry
from tracking.cli import run_cli
from tracking.database import (
    DatabasePersistenceError,
    MIGRATION_0001_PATH,
    RelationalStateBackend,
    configured_database_url,
    migration_0001_checksum,
    redact_database_url,
    sanitize_db_error,
)
from tracking.store import TrackingStore


class _PassWorker:
    name = "cloud-test-worker"

    def execute(self, job, context):
        return WorkerResult("COMPLETED", output={"executed": True, "job_id": job["job_id"]})


class _MockPostgresCursor:
    """Translates PostgreSQL `%s` / `::jsonb` syntax into an underlying SQLite cursor while recording PostgreSQL SQL."""

    def __init__(self, sqlite_cur: sqlite3.Cursor, executed_sql: list[tuple[str, tuple]], fail_on_write: bool = False) -> None:
        self._cur = sqlite_cur
        self._executed_sql = executed_sql
        self._fail_on_write = fail_on_write

    def execute(self, sql: str, params: tuple = ()) -> "_MockPostgresCursor":
        self._executed_sql.append((sql, tuple(params)))
        stripped = sql.strip()
        if self._fail_on_write and stripped.upper().startswith(("INSERT INTO LEADS", "INSERT INTO JOBS", "INSERT INTO PROJECTS")):
            raise RuntimeError("Simulated Supabase PostgreSQL connection drop during write")
        if "CREATE TABLE IF NOT EXISTS schema_migrations" in sql and "TIMESTAMPTZ" in sql:
            for stmt in RelationalStateBackend._sqlite_schema_statements():
                self._cur.execute(stmt)
            self._cur.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?) ON CONFLICT(version) DO NOTHING",
                ("0001_supabase_postgres_schema", "Initial normalized PostgreSQL schema", "2026-10-13T00:00:00Z"),
            )
            return self
        translated = sql.replace("::jsonb", "").replace("%s", "?")
        norm_params = tuple(1 if isinstance(p, bool) and p else (0 if isinstance(p, bool) else p) for p in params)
        self._cur.execute(translated, norm_params)
        return self

    def fetchall(self) -> list:
        return self._cur.fetchall()


class _MockPostgresConnection:
    """Controlled DB-API 2.0 connection verifying PostgreSQL dialect queries, commits, and rollbacks."""

    def __init__(self, sqlite_path: Path, executed_sql: list[tuple[str, tuple]], *, fail_on_write: bool = False) -> None:
        self._conn = sqlite3.connect(str(sqlite_path), timeout=30.0)
        self._executed_sql = executed_sql
        self._fail_on_write = fail_on_write
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _MockPostgresCursor:
        return _MockPostgresCursor(self._conn.cursor(), self._executed_sql, fail_on_write=self._fail_on_write)

    def commit(self) -> None:
        self.commits += 1
        self._conn.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self._conn.rollback()

    def close(self) -> None:
        self.closed = True
        self._conn.close()


class CloudDeploymentReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_file = self.root / "tracking.sqlite"
        self.sqlite_url = f"sqlite:///{self.db_file}"
        self.fictional_fixture = json.loads(
            Path("tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json").read_text(encoding="utf-8")
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 1. Database adapter behavior (PostgreSQL dialect + SQLite engine)
    # ------------------------------------------------------------------
    def test_01_postgres_dialect_migration_parameterization_and_rollback(self) -> None:
        self.assertTrue(MIGRATION_0001_PATH.is_file())
        migration_sql = MIGRATION_0001_PATH.read_text(encoding="utf-8")
        for required_table in (
            "schema_migrations",
            "leads",
            "research_evidence",
            "qualifications",
            "projects",
            "builds",
            "validations",
            "reviews",
            "deployments",
            "outreach_packages",
            "jobs",
            "job_executions",
            "workers",
            "project_locks",
            "system_kv",
            "build_artifacts",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {required_table}", migration_sql)

        # Verify password redaction never exposes secret credentials
        raw_pg_url = "postgresql://postgres:SuperSecretPass123!@db.example.supabase.co:5432/postgres?sslmode=require"
        redacted = redact_database_url(raw_pg_url)
        self.assertNotIn("SuperSecretPass123!", redacted)
        self.assertIn("postgres:***@db.example.supabase.co:5432", redacted)

        # Test PostgreSQL dialect (`%s`, `::jsonb`, `ON CONFLICT`) via controlled DB-API connection factory
        executed_sql: list[tuple[str, tuple]] = []
        pg_sqlite_backing = self.root / "pg_dialect_backing.sqlite"
        connections: list[_MockPostgresConnection] = []

        def pg_factory() -> _MockPostgresConnection:
            conn = _MockPostgresConnection(pg_sqlite_backing, executed_sql)
            connections.append(conn)
            return conn

        pg_backend = RelationalStateBackend(raw_pg_url, connection_factory=pg_factory)
        self.assertEqual(pg_backend.dialect, "postgres")
        store = TrackingStore(self.root / "unused.json", db_backend=pg_backend)
        lead = store.create_lead({
            "business_name": "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS",
            "city": "Lagos",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
            "source_urls": ["https://directory.example.invalid/lagos-fresh-kitchen"],
        })
        proj = store.create_project(lead["lead_id"])
        self.assertEqual(proj["project_id"], "project-0001")
        self.assertTrue(any("::jsonb" in sql and "%s" in sql and "ON CONFLICT (lead_id)" in sql for sql, _ in executed_sql))
        self.assertTrue(any("ON CONFLICT (project_id)" in sql for sql, _ in executed_sql))
        self.assertGreaterEqual(sum(c.commits for c in connections), 2)

        # Verify transaction rollback and fail-closed DatabasePersistenceError when DB write fails
        failing_conns: list[_MockPostgresConnection] = []

        def failing_pg_factory() -> _MockPostgresConnection:
            conn = _MockPostgresConnection(pg_sqlite_backing, executed_sql, fail_on_write=True)
            failing_conns.append(conn)
            return conn

        failing_backend = RelationalStateBackend(raw_pg_url, connection_factory=failing_pg_factory, auto_migrate=False)
        failing_backend._initialized = True
        failing_store = TrackingStore(self.root / "unused_fallback.json", db_backend=failing_backend)
        with self.assertRaises(DatabasePersistenceError) as ctx:
            failing_store.create_lead({
                "business_name": "Should Rollback",
                "city": "Lagos",
                "country": "Nigeria",
                "qualification_status": "QUALIFIED",
                "data_classification": "TEST_FIXTURE",
            })
        self.assertNotIn("SuperSecretPass123!", str(ctx.exception))
        self.assertGreaterEqual(sum(c.rollbacks for c in failing_conns), 1)
        # Confirm it did NOT fall back to writing the local JSON file
        self.assertFalse((self.root / "unused_fallback.json").exists())

    # ------------------------------------------------------------------
    # 2. Persistence across simulated service restarts
    # ------------------------------------------------------------------
    def test_02_persistence_across_simulated_service_restarts(self) -> None:
        store_1 = TrackingStore(self.root / "state.json", database_url=self.sqlite_url)
        lead = store_1.ingest_research_record(self.fictional_fixture)
        qual = store_1.record_qualification({
            "qualification_id": "qual-restart-001",
            "lead_id": lead["lead_id"],
            "status": "QUALIFIED",
            "website_status": "NO_WEBSITE",
            "score": 90,
            "priority": "P1",
        })
        proj = store_1.create_project(lead["lead_id"])
        builder = WebsiteBuilder(output_root=self.root / "output")
        build_res = builder.build(store_1.build_payload(lead["lead_id"]))
        store_1.record_build(proj["project_id"], build_res.to_dict())
        store_1.record_validation(proj["project_id"], build_res.validation)

        jobs_1 = JobService(store_1)
        jobs_1.create("BUILD", job_id="job-restart-1", lead_id=lead["lead_id"], project_id=proj["project_id"])

        reg_1 = WorkerRegistry(store_1)
        reg_1.register("worker-alpha", name="Alpha Worker", worker_type="LOCAL_AGENT", capabilities=["BUILD"])
        claimed_job = reg_1.claim("job-restart-1", "worker-alpha")
        lease_id = claimed_job["lease"]["lease_id"]

        # Simulate full service restart: discard store_1 and open store_2 against the same database
        del store_1
        store_2 = TrackingStore(self.root / "state-after-restart.json", database_url=self.sqlite_url)
        snap = store_2.snapshot(proj["project_id"])
        self.assertEqual(snap["lead"]["lead_id"], lead["lead_id"])
        self.assertEqual(snap["project"]["project_id"], proj["project_id"])
        self.assertEqual(snap["project"]["assigned_build_id"], build_res.build_id)
        self.assertEqual(snap["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap["project"]["review_status"], "PENDING")
        self.assertEqual(store_2.state["qualifications"]["qual-restart-001"]["score"], qual["score"])
        self.assertTrue(len(store_2.state["research_evidence"]) > 0)
        self.assertIn("job-restart-1", store_2.state["jobs"])
        self.assertIn("worker-alpha", store_2.state["workers"])
        self.assertEqual(store_2.state["project_locks"][proj["project_id"]]["lease_id"], lease_id)
        self.assertTrue(len(store_2.state["executions"]) > 0)
        self.assertIn(build_res.build_id, store_2.state["build_artifacts"])

    # ------------------------------------------------------------------
    # 3. Duplicate job prevention
    # ------------------------------------------------------------------
    def test_03_duplicate_job_prevention_across_instances_and_restarts(self) -> None:
        store_a = TrackingStore(self.root / "a.json", database_url=self.sqlite_url)
        store_b = TrackingStore(self.root / "b.json", database_url=self.sqlite_url)
        jobs_a = JobService(store_a)
        jobs_b = JobService(store_b)

        j1 = jobs_a.create("SCOUT", job_id="scout-idemp-001", input={"city": "Lagos"})
        # Second instance submits identical job_id -> returns existing record without duplicating
        j2 = jobs_b.create("SCOUT", job_id="scout-idemp-001", input={"city": "Lagos"})
        self.assertEqual(j1["job_id"], j2["job_id"])
        self.assertEqual(len(store_b.state["jobs"]), 1)

        # Second instance submits different job_id with the same explicit idempotency_key -> returns existing job
        j3 = jobs_b.create("SCOUT", job_id="scout-idemp-alias", idempotency_key="scout-idemp-001", input={"city": "Lagos"})
        self.assertEqual(j3["job_id"], "scout-idemp-001")
        self.assertEqual(len(store_b.state["jobs"]), 1)

        # Conflicting job_type with same job_id or idempotency_key is rejected
        with self.assertRaisesRegex(ValueError, "Idempotency key is already associated with a different job"):
            jobs_b.create("RESEARCH", job_id="scout-idemp-001")

    # ------------------------------------------------------------------
    # 4. Concurrent updates and idempotency (no lost updates on unrelated records)
    # ------------------------------------------------------------------
    def test_04_concurrent_updates_do_not_overwrite_unrelated_records(self) -> None:
        seed_store = TrackingStore(self.root / "seed.json", database_url=self.sqlite_url)
        lead = seed_store.ingest_research_record(self.fictional_fixture)
        proj = seed_store.create_project(lead["lead_id"])

        # Open two concurrent store instances that both load the initial state before either writes
        store_worker = TrackingStore(self.root / "w1.json", database_url=self.sqlite_url)
        store_operator = TrackingStore(self.root / "w2.json", database_url=self.sqlite_url)

        # Worker instance creates and runs a job
        jobs_w = JobService(store_worker)
        jobs_w.create("SCOUT", job_id="concurrent-job-1")
        jobs_w.run("concurrent-job-1", _PassWorker())

        # Operator instance (whose in-memory snapshot predates `concurrent-job-1`) configures preview target
        # and creates a second lead + job
        store_operator.configure_preview_target(proj["project_id"], "lagos-fresh-isolated-preview")
        lead_2 = store_operator.create_lead({
            "business_name": "Second Fictional Bistro",
            "city": "Abuja",
            "country": "Nigeria",
            "phone": "+234 800 111 2222",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        JobService(store_operator).create("SCOUT", job_id="concurrent-job-2")

        # Verify in a fresh store that NEITHER transaction overwrote the other's records
        verifier = TrackingStore(self.root / "verify.json", database_url=self.sqlite_url)
        self.assertIn("concurrent-job-1", verifier.state["jobs"])
        self.assertEqual(verifier.state["jobs"]["concurrent-job-1"]["status"], "COMPLETED")
        self.assertIn("concurrent-job-2", verifier.state["jobs"])
        self.assertIn(lead_2["lead_id"], verifier.state["leads"])
        self.assertEqual(
            verifier.snapshot(proj["project_id"])["project"]["isolated_preview_project"],
            "lagos-fresh-isolated-preview",
        )

        # Also test multi-threaded concurrent job creation against the database
        def _create_worker_job(idx: int) -> str:
            t_store = TrackingStore(self.root / f"thread-{idx}.json", database_url=self.sqlite_url)
            t_jobs = JobService(t_store)
            created = t_jobs.create("SCOUT", job_id=f"parallel-job-{idx:02d}")
            return created["job_id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            created_ids = list(pool.map(_create_worker_job, range(8)))
        self.assertEqual(len(created_ids), 8)
        verifier.refresh()
        for jid in created_ids:
            self.assertIn(jid, verifier.state["jobs"])

    # ------------------------------------------------------------------
    # 5. Retry and stale-job recovery persisted in database
    # ------------------------------------------------------------------
    def test_05_retry_and_stale_job_recovery_with_database_persistence(self) -> None:
        store = TrackingStore(self.root / "retry.json", database_url=self.sqlite_url)
        jobs = JobService(store, max_attempts=3)
        job = jobs.create("SCOUT", job_id="stale-job-001")
        stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1800)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        store.state["jobs"]["stale-job-001"].update({
            "status": "RUNNING",
            "attempts": 1,
            "started_at": stale_ts,
            "heartbeat_at": stale_ts,
        })
        store._save()

        # Recover stale job from a second store instance
        store_2 = TrackingStore(self.root / "retry2.json", database_url=self.sqlite_url)
        jobs_2 = JobService(store_2, max_attempts=3)
        recovered = jobs_2.recover_stale(older_than_seconds=900)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "NEEDS_REVIEW")

        # Retry recovered job and run to completion
        retried = jobs_2.retry("stale-job-001", input={"recovered": True}, worker=_PassWorker())
        self.assertEqual(retried["status"], "COMPLETED")
        self.assertEqual(retried["attempts"], 2)
        self.assertTrue(retried["input"]["recovered"])

    # ------------------------------------------------------------------
    # 6 & 7. Artifact retrieval after local file removal + SHA-256 verification
    # ------------------------------------------------------------------
    def test_06_and_07_artifact_retrieval_after_local_removal_and_sha256_verification(self) -> None:
        store = TrackingStore(self.root / "art.json", database_url=self.sqlite_url)
        lead = store.ingest_research_record(self.fictional_fixture)
        proj = store.create_project(lead["lead_id"])
        out_root = self.root / "output"
        exp_dir = self.root / "preview-export" / "fictional-pkg"

        builder = WebsiteBuilder(output_root=out_root)
        build_res = builder.build(store.build_payload(lead["lead_id"]))
        store.record_build(proj["project_id"], build_res.to_dict())
        store.record_validation(proj["project_id"], build_res.validation)
        pkg = store.build_review_package(proj["project_id"], export_dir=exp_dir, repository_root=self.root)
        expected_sha = pkg["artifact_sha256"]

        # Simulate container restart: delete the entire local `output/` and `preview-export/` directories!
        shutil.rmtree(out_root)
        shutil.rmtree(exp_dir)
        self.assertFalse(Path(build_res.output_location).exists())
        self.assertFalse(exp_dir.exists())

        # Open a brand-new TrackingStore connected to the database
        restarted_store = TrackingStore(self.root / "art-restarted.json", database_url=self.sqlite_url)
        # Snapshot still reports artifact_exists=True because durable artifact is in DB
        self.assertTrue(restarted_store.snapshot(proj["project_id"])["artifact_exists"])

        # Re-materialize build artifact and verify SHA-256 matches
        mat = restarted_store.get_or_materialize_build_artifact(
            build_res.build_id,
            repository_root=self.root,
            verify_hash=True,
        )
        self.assertTrue(mat["materialized"])
        self.assertEqual(mat["html_sha256"], expected_sha)
        self.assertTrue(Path(build_res.output_location).is_file())
        self.assertEqual(
            hashlib.sha256(Path(build_res.output_location).read_bytes()).hexdigest(),
            expected_sha,
        )

        # Delete local `output/` again and verify `LocalPipelineWorker` VALIDATE automatically restores from DB
        shutil.rmtree(out_root)
        worker = LocalPipelineWorker(repository_root=self.root, output_root=out_root)
        jobs = JobService(restarted_store)
        val_job = jobs.create("VALIDATE", job_id="val-after-wipe", project_id=proj["project_id"], build_id=build_res.build_id)
        val_done = jobs.run(val_job["job_id"], worker)
        self.assertEqual(val_done["status"], "COMPLETED")
        self.assertEqual(val_done["output"]["validation_status"], "PASS")

        # Verify review package verification also succeeds after local export_dir was wiped
        ver = restarted_store.verify_review_package(proj["project_id"], exp_dir, repository_root=self.root)
        self.assertEqual(ver["status"], "VALID")
        self.assertTrue(ver["hash_matches"])
        self.assertEqual(ver["canonical_sha256"], expected_sha)

        # Tamper with on-disk HTML and confirm SHA-256 verification fails closed
        Path(build_res.output_location).write_text("<html>Tampered</html>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            restarted_store.get_or_materialize_build_artifact(
                build_res.build_id,
                repository_root=self.root,
                verify_hash=True,
            )

    # ------------------------------------------------------------------
    # 8. Review and approval-state preservation (including Pitstop Lagos)
    # ------------------------------------------------------------------
    def test_08_review_and_approval_state_preservation_across_restarts_and_workspace_protection(self) -> None:
        store = TrackingStore(self.root / "rev.json", database_url=self.sqlite_url)
        lead = store.ingest_research_record(self.fictional_fixture)
        proj = store.create_project(lead["lead_id"])
        builder = WebsiteBuilder(output_root=self.root / "output")
        build_res = builder.build(store.build_payload(lead["lead_id"]))
        store.record_build(proj["project_id"], build_res.to_dict())
        store.record_validation(proj["project_id"], build_res.validation)

        store.record_review(
            proj["project_id"],
            "APPROVED_FOR_PREVIEW",
            notes="Approved fictional draft for isolated preview.",
            reviewer="qa-reviewer",
            require_notes=True,
            build_id=build_res.build_id,
            action="PREVIEW_DEPLOYMENT",
        )

        # Restart store and verify approval state and review history survived
        restarted = TrackingStore(self.root / "rev-restarted.json", database_url=self.sqlite_url)
        snap = restarted.snapshot(proj["project_id"])
        self.assertEqual(snap["project"]["review_status"], "APPROVED_FOR_PREVIEW")
        self.assertEqual(snap["project"]["approved_build_id"], build_res.build_id)
        self.assertEqual(snap["project"]["approved_action"], "PREVIEW_DEPLOYMENT")
        self.assertEqual(snap["workflow_state"], "APPROVED_FOR_PREVIEW_DEPLOYMENT")
        history = restarted.get_review_history(proj["project_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["decision"], "APPROVED_FOR_PREVIEW")

        # Verify live workspace project-0001 and project-0002 (Pitstop Lagos) remain untouched at PENDING
        ws = TrackingStore()
        snap_1 = ws.snapshot("project-0001")
        snap_2 = ws.snapshot("project-0002")
        self.assertEqual(snap_1["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap_1["project"]["review_status"], "PENDING")
        self.assertEqual(snap_2["lead"]["business_name"], "Pitstop Lagos")
        self.assertEqual(snap_2["lead"]["data_classification"], "REAL_PUBLIC_DATA")
        self.assertEqual(snap_2["project"]["assigned_build_id"], "build-2026-10-13T012227z")
        self.assertEqual(snap_2["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snap_2["project"]["review_status"], "PENDING")
        self.assertIsNone(snap_2["latestDeployment"])

    # ------------------------------------------------------------------
    # 9. Local test operation without Supabase credentials & fail-closed production mode
    # ------------------------------------------------------------------
    def test_09_local_operation_without_supabase_credentials_and_fail_closed_production(self) -> None:
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in {
                "DATABASE_URL",
                "AI_WEBSITE_AGENT_DATABASE_URL",
                "SUPABASE_DB_URL",
                "AI_WEBSITE_AGENT_ENV",
                "AI_WEBSITE_AGENT_STORAGE_BACKEND",
                "RENDER",
            }
        }
        with patch.dict(os.environ, clean_env, clear=True):
            self.assertIsNone(configured_database_url())
            local_store = TrackingStore(self.root / "local_only.json")
            self.assertIsNone(local_store.db_backend)
            lead = local_store.create_lead({
                "business_name": "Local Only Test Cafe",
                "city": "Lagos",
                "country": "Nigeria",
                "qualification_status": "QUALIFIED",
                "data_classification": "TEST_FIXTURE",
            })
            self.assertEqual(lead["lead_id"], "lead-0001")
            self.assertTrue((self.root / "local_only.json").is_file())

        # In production mode without DATABASE_URL, TrackingStore refuses to fall back to local JSON
        prod_env = {**clean_env, "AI_WEBSITE_AGENT_ENV": "production"}
        with patch.dict(os.environ, prod_env, clear=True):
            with self.assertRaisesRegex(DatabasePersistenceError, "Refusing to fall back to temporary local JSON storage"):
                TrackingStore(self.root / "prod_should_not_exist.json")
            self.assertFalse((self.root / "prod_should_not_exist.json").exists())
            # Production mode also refuses SQLite fallback / URLs
            with self.assertRaisesRegex(DatabasePersistenceError, "Refusing SQLite database URL"):
                TrackingStore(self.root / "prod_sqlite_blocked.json", database_url=self.sqlite_url)

        # CLI migrate-db works cleanly with --database-url
        rc = run_cli(["migrate-db", "--database-url", self.sqlite_url])
        self.assertEqual(rc, 0)

    # ------------------------------------------------------------------
    # 10. Production configuration, PORT binding, /healthz, and Bearer auth
    # ------------------------------------------------------------------
    def test_10_production_config_port_binding_healthz_and_bearer_auth(self) -> None:
        # Default local binding is 127.0.0.1:8787
        host, port, allow_ext = resolve_bind_config(env={})
        self.assertEqual((host, port, allow_ext), ("127.0.0.1", 8787, False))

        # Render PORT env var is respected
        host, port, allow_ext = resolve_bind_config(env={"PORT": "10000"})
        self.assertEqual((host, port, allow_ext), ("127.0.0.1", 10000, False))

        # Non-loopback host (0.0.0.0) is rejected unless explicitly enabled
        with self.assertRaisesRegex(ValueError, "Refusing non-loopback bind host"):
            resolve_bind_config(host="0.0.0.0", env={})

        # Explicit production opt-in allows 0.0.0.0
        host, port, allow_ext = resolve_bind_config(
            host="0.0.0.0",
            env={"PORT": "10000", "AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK": "true"},
        )
        self.assertEqual((host, port, allow_ext), ("0.0.0.0", 10000, True))

        # Missing AI_WEBSITE_AGENT_API_TOKEN refuses to start server even with allow_non_loopback
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "AI_WEBSITE_AGENT_API_TOKEN is required"):
                serve(host="0.0.0.0", port=0, allow_non_loopback=True)

        # Start a live local HTTP server backed by the relational DB and test /healthz, /health, and /projects/:id/artifact
        store = TrackingStore(self.root / "api.json", database_url=self.sqlite_url)
        lead = store.ingest_research_record(self.fictional_fixture)
        proj = store.create_project(lead["lead_id"])
        out_root = self.root / "api-output"
        build_res = WebsiteBuilder(output_root=out_root).build(store.build_payload(lead["lead_id"]))
        store.record_build(proj["project_id"], build_res.to_dict())
        store.record_validation(proj["project_id"], build_res.validation)
        store.build_review_package(proj["project_id"], export_dir=self.root / "api-export", repository_root=self.root)

        # Delete local output directory to prove GET /projects/:id/artifact serves from durable DB storage
        shutil.rmtree(out_root)

        token = "cloud-test-secret-token-999"
        service = JobService(store)
        worker = LocalPipelineWorker(repository_root=self.root, output_root=out_root)
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                service,
                token,
                worker=worker,
                output_root=out_root,
                export_root=self.root / "api-export",
                repository_root=self.root,
            ),
        )
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            # 1. Unauthenticated GET /healthz succeeds with minimal payload and zero sensitive info
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                hz = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(hz, {"status": "ok"})

            # 2. Unauthenticated GET /health is rejected with 401 Unauthorized
            with self.assertRaises(urllib.error.HTTPError) as err_ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5)
            self.assertEqual(err_ctx.exception.code, 401)

            # 3. Authenticated GET /readiness reports READY and storage_backend
            req_ready = urllib.request.Request(
                f"http://127.0.0.1:{port}/readiness",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req_ready, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                ready_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(ready_payload["status"], "READY")
                self.assertEqual(ready_payload["storage_backend"], "sqlite")
                self.assertTrue(ready_payload["worker_registered"])

            # 4. Authenticated GET /projects/project-0001/artifact retrieves and re-materializes HTML + SHA-256
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/projects/{proj['project_id']}/artifact",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                art_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(art_payload["project_id"], proj["project_id"])
                self.assertEqual(art_payload["build_id"], build_res.build_id)
                self.assertTrue(art_payload["materialized"])
                self.assertIn("Lagos Fresh Kitchen", art_payload["html_content"])
                self.assertEqual(
                    hashlib.sha256(art_payload["html_content"].encode("utf-8")).hexdigest(),
                    art_payload["html_sha256"],
                )
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

    # ------------------------------------------------------------------
    # 11. Migration checksum verification, same-record concurrent merge,
    #     worker claim race protection, and error credential sanitization
    # ------------------------------------------------------------------
    def test_11_migration_checksum_same_record_merge_and_worker_claim_race(self) -> None:
        # A. Verify migration checksum is stored and tampered checksum fails closed
        backend = RelationalStateBackend(self.sqlite_url)
        conn = sqlite3.connect(str(self.db_file))
        try:
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = '0001_supabase_postgres_schema'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], migration_0001_checksum())
            # Tamper with stored checksum to simulate migration drift
            conn.execute(
                "UPDATE schema_migrations SET checksum = '0000000000000000000000000000000000000000000000000000000000000000' "
                "WHERE version = '0001_supabase_postgres_schema'"
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(DatabasePersistenceError, "Migration checksum mismatch"):
            RelationalStateBackend(self.sqlite_url)

        # Restore valid checksum
        conn = sqlite3.connect(str(self.db_file))
        try:
            conn.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = '0001_supabase_postgres_schema'",
                (migration_0001_checksum(),),
            )
            conn.commit()
        finally:
            conn.close()

        # B. Verify concurrent updates to the SAME project record merge fields, append-only lists, and increment lock_version
        seed = TrackingStore(self.root / "s.json", database_url=self.sqlite_url)
        lead = seed.ingest_research_record(self.fictional_fixture)
        proj = seed.create_project(lead["lead_id"])
        self.assertEqual(seed.state["projects"][proj["project_id"]]["lock_version"], 1)

        inst_a = TrackingStore(self.root / "a_same.json", database_url=self.sqlite_url)
        inst_b = TrackingStore(self.root / "b_same.json", database_url=self.sqlite_url)

        # Instance A updates isolated_preview_project and appends to review_history
        inst_a.state["projects"][proj["project_id"]]["isolated_preview_project"] = "lagos-fresh-preview-a"
        inst_a.state["projects"][proj["project_id"]]["review_history"].append({"review_id": "rev-a", "notes": "From A"})
        inst_a._save()

        # Instance B (holding stale baseline) updates review_notes and appends a different item to review_history
        inst_b.state["projects"][proj["project_id"]]["review_notes"] = "Concurrent note from B"
        inst_b.state["projects"][proj["project_id"]]["review_history"].append({"review_id": "rev-b", "notes": "From B"})
        inst_b._save()

        merged_proj = inst_b.state["projects"][proj["project_id"]]
        self.assertEqual(merged_proj["isolated_preview_project"], "lagos-fresh-preview-a")
        self.assertEqual(merged_proj["review_notes"], "Concurrent note from B")
        self.assertEqual(len(merged_proj["review_history"]), 2)
        self.assertEqual(merged_proj["lock_version"], 3)

        # C. Verify WorkerRegistry across instances prevents duplicate job claims and persists worker_type / attempt
        jobs_a = JobService(inst_a)
        jobs_a.create("BUILD", job_id="race-build-job", lead_id=lead["lead_id"], project_id=proj["project_id"])
        reg_a = WorkerRegistry(inst_a)
        reg_b = WorkerRegistry(inst_b)
        reg_a.register("worker-1", name="Worker One", worker_type="LOCAL_AGENT", capabilities=["BUILD"])
        reg_b.register("worker-2", name="Worker Two", worker_type="REMOTE_AGENT", capabilities=["BUILD"])

        reg_a.claim("race-build-job", "worker-1")
        # Worker 2 on Instance B attempts to claim the same job -> rejected because refresh sees RUNNING
        with self.assertRaisesRegex(ValueError, "Job is not claimable"):
            reg_b.claim("race-build-job", "worker-2")

        # Verify SQL columns in workers and job_executions match domain fields
        conn = sqlite3.connect(str(self.db_file))
        try:
            w_row = conn.execute("SELECT worker_type FROM workers WHERE worker_id = 'worker-1'").fetchone()
            self.assertEqual(w_row[0], "LOCAL_AGENT")
            e_row = conn.execute("SELECT attempt FROM job_executions WHERE job_id = 'race-build-job'").fetchone()
            self.assertEqual(e_row[0], 1)
        finally:
            conn.close()

        # D. Verify sanitize_db_error redacts raw and URL-encoded credentials from driver errors
        raw_url = "postgresql://postgres:My%40Secret#99@db.example.supabase.co:5432/postgres?password=InlineQueryPassword123"
        raw_err = f"FATAL: password authentication failed for My@Secret#99 on {raw_url}"
        sanitized = sanitize_db_error(raw_err, raw_url)
        self.assertNotIn("My@Secret#99", sanitized)
        self.assertNotIn("My%40Secret#99", sanitized)
        self.assertNotIn("InlineQueryPassword123", sanitized)

    def test_12_controlled_migration_local_asset_restoration_and_gitignore(self) -> None:
        # 1. Verify render.yaml does NOT run database migrations automatically inside buildCommand
        repo_root = Path(__file__).resolve().parents[1]
        render_yaml = (repo_root / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("buildCommand: pip install -r requirements.txt\n", render_yaml)
        self.assertNotIn("buildCommand: pip install -r requirements.txt && python3 -m tracking.cli migrate-db", render_yaml)

        # 2. Verify migrate-db and migrate-db --verify-only CLI behavior
        buf = StringIO()
        rc = run_cli(["migrate-db", "--database-url", self.sqlite_url], stdout=buf)
        self.assertEqual(rc, 0)
        mig_payload = json.loads(buf.getvalue())
        self.assertEqual(mig_payload["status"], "MIGRATED")
        self.assertEqual(mig_payload["checksum"], migration_0001_checksum())

        verify_buf = StringIO()
        rc_verify = run_cli(
            ["migrate-db", "--database-url", self.sqlite_url, "--verify-only"],
            stdout=verify_buf,
        )
        self.assertEqual(rc_verify, 0)
        verify_payload = json.loads(verify_buf.getvalue())
        self.assertEqual(verify_payload["status"], "VERIFIED")
        self.assertEqual(verify_payload["checksum"], migration_0001_checksum())

        # 3. Verify future local assets inside a build directory are persisted, SHA-256 verified, and restored after restart
        output_root = self.root / "output"
        store1 = TrackingStore(self.root / "assets1.json", database_url=self.sqlite_url)
        lead = store1.ingest_research_record(self.fictional_fixture)
        proj = store1.create_project(lead["lead_id"])
        build_dir = output_root / lead["lead_id"] / "build-local-assets-1"
        (build_dir / "assets").mkdir(parents=True, exist_ok=True)
        html_path = build_dir / "index.html"
        html_path.write_text("<!doctype html><html><body><img src='assets/logo.svg'></body></html>", encoding="utf-8")
        (build_dir / "build.json").write_text(json.dumps({"build_id": "build-local-assets-1"}), encoding="utf-8")
        svg_text = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'><circle cx='5' cy='5' r='4'/></svg>"
        (build_dir / "assets" / "logo.svg").write_text(svg_text, encoding="utf-8")
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        (build_dir / "assets" / "badge.png").write_bytes(png_bytes)

        store1.record_build(
            proj["project_id"],
            {
                "build_id": "build-local-assets-1",
                "lead_id": lead["lead_id"],
                "template_id": "food-warm-v1",
                "status": "SUCCESS",
                "output_location": str(html_path),
            },
        )
        # Wipe the entire local output directory to simulate a Render container restart
        shutil.rmtree(output_root)
        self.assertFalse(html_path.exists())

        store2 = TrackingStore(self.root / "assets2.json", database_url=self.sqlite_url)
        restored = store2.get_or_materialize_build_artifact("build-local-assets-1", repository_root=self.root)
        self.assertTrue(restored["materialized"])
        self.assertCountEqual(restored["restored_assets"], ["assets/badge.png", "assets/logo.svg"])
        self.assertEqual((build_dir / "assets" / "logo.svg").read_text(encoding="utf-8"), svg_text)
        self.assertEqual((build_dir / "assets" / "badge.png").read_bytes(), png_bytes)

        # Corrupt an on-disk restored local asset -> SHA-256 verification fails closed
        (build_dir / "assets" / "logo.svg").write_text("<svg>tampered</svg>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "On-disk local asset SHA-256 mismatch"):
            store2.get_or_materialize_build_artifact("build-local-assets-1", repository_root=self.root)

        # Reject path traversal in local_assets
        with self.assertRaisesRegex(ValueError, "Unsafe relative asset path"):
            store2.record_build_artifact(
                "build-local-assets-1",
                project_id=proj["project_id"],
                lead_id=lead["lead_id"],
                artifact_path=str(html_path),
                html_content="<!doctype html><html></html>",
                local_assets={"../escape.txt": {"encoding": "utf-8", "content": "x", "sha256": hashlib.sha256(b"x").hexdigest()}},
            )

        # 4. Verify verify_schema() rejects NULL checksum (e.g. if raw SQL was run without migrate-db) until migrate-db stamps it
        conn = sqlite3.connect(str(self.db_file))
        try:
            conn.execute("UPDATE schema_migrations SET checksum = NULL WHERE version = '0001_supabase_postgres_schema'")
            conn.commit()
        finally:
            conn.close()
        err_buf = StringIO()
        rc_missing_sum = run_cli(
            ["migrate-db", "--database-url", self.sqlite_url, "--verify-only"],
            stdout=StringIO(),
            stderr=err_buf,
        )
        self.assertEqual(rc_missing_sum, 1)
        self.assertIn("Migration checksum", err_buf.getvalue())
        # Running migrate-db stamps the checksum and --verify-only passes again
        self.assertEqual(run_cli(["migrate-db", "--database-url", self.sqlite_url], stdout=StringIO()), 0)
        self.assertEqual(run_cli(["migrate-db", "--database-url", self.sqlite_url, "--verify-only"], stdout=StringIO()), 0)

        # 5. Verify Supabase Supavisor transaction pooler compatibility (prepare_threshold=None & ?pgbouncer=true stripping)
        captured_connect: dict[str, object] = {}

        class _FakePsycopg:
            @staticmethod
            def connect(url: str, **kwargs: object) -> object:
                captured_connect["url"] = url
                captured_connect["kwargs"] = kwargs

                class _DummyConn:
                    autocommit = True

                    def close(self) -> None:
                        pass

                return _DummyConn()

        pooler_url = "postgresql://postgres.ref:SecretPass@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?sslmode=require&pgbouncer=true"
        pg_backend = RelationalStateBackend(pooler_url, auto_migrate=False)
        with patch("builtins.__import__", side_effect=lambda name, *a, **k: _FakePsycopg if name == "psycopg" else __import__(name, *a, **k)):
            conn_obj = pg_backend._connect()
            self.assertFalse(getattr(conn_obj, "autocommit", True))
        self.assertNotIn("pgbouncer=", str(captured_connect.get("url")))
        self.assertIn("sslmode=require", str(captured_connect.get("url")))
        self.assertIsNone(captured_connect["kwargs"].get("prepare_threshold"))


if __name__ == "__main__":
    unittest.main()
