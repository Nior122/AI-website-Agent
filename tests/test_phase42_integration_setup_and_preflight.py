"""Phase 42 tests: integration setup and controlled preview readiness.

These tests cover the operator-facing setup surface added to turn "the code works locally" into "this
workspace is configured, and here is exactly what still has to happen outside the repository":

1. **`tracking.cli preflight-db`** - a read-only answer to "which database would this app migrate, can
   it be reached, and is the schema applied?". A wrong-host mistake against a managed database is the
   one integration step an operator cannot undo by reading a log, so the preflight names the target
   (host, port, database, Supabase project reference) and reports only what it actually observed.
2. **`migrate-db` confirmation at the remote boundary** - DDL against a non-local database now requires
   the operator to name the discovered database back, and a repeated migration is demonstrated to be
   additive rather than assumed.
3. **`orchestration.cli preflight`** - liveness, storage readiness, workflow execution readiness, n8n
   automation, and deployment-provider configuration reported as four distinct assurance levels, with no
   socket bound, no provider command run, and no secret printed.
4. **`workflow_readiness`** - which environment variables the exported n8n workflows read, which of them
   are unset (an unset `$env.AI_WEBSITE_AGENT_API_URL` renders to an empty URL and reads like an API
   outage), and which files still hold starter placeholders that must be edited in the n8n UI.
5. **Test isolation** - the suite's dependence on untracked runtime state is removed, so a fresh clone
   behaves the same as a machine that has already run the pipeline.

No database server, Render service, n8n runtime, Vercel project, or network endpoint is contacted.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tracking import cli as tracking_cli
from tracking.database import (DatabasePersistenceError, RelationalStateBackend,
                               migration_0001_checksum, supabase_project_ref)

PASSWORD_CANARY = "Supa!secret-pw-9f3a"
SUPABASE_REF = "ipn4c08glpwfccrpzhtq"
SUPABASE_URL = f"postgresql://postgres:{PASSWORD_CANARY}@db.{SUPABASE_REF}.supabase.co:5432/postgres"


def run_module(module: str, *argv: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    """Run one repository CLI as a subprocess so argparse and exit codes are the real ones."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    for key in ("AI_WEBSITE_AGENT_ENV", "AI_WEBSITE_AGENT_STORAGE_BACKEND", "DATABASE_URL",
                "AI_WEBSITE_AGENT_DATABASE_URL", "SUPABASE_DB_URL", "AI_WEBSITE_AGENT_API_TOKEN",
                "AI_WEBSITE_AGENT_API_URL", "RENDER", "PORT"):
        environment.pop(key, None)
    environment.update(env or {})
    process = subprocess.run([sys.executable, "-m", module, *argv], cwd=cwd or REPO_ROOT, env=environment,
                             capture_output=True, text=True, timeout=120)
    return process


def payload_of(process) -> dict:
    return json.loads(process.stdout)


# =============================================================================================
# 1. Database preflight
# =============================================================================================
class DatabasePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "preflight.sqlite"
        self.url = f"sqlite:///{self.db_path}"

    def test_an_unconfigured_workspace_reports_its_own_state_instead_of_failing_open(self) -> None:
        process = run_module("tracking.cli", "preflight-db", cwd=self.root)
        self.assertEqual(process.returncode, 1, process.stderr[-400:])
        report = payload_of(process)
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual(report["backend"], "local_json")
        self.assertFalse(report["ddl_executed"])
        self.assertIn("No database URL is configured", report["blockers"][0])
        # The distinction that matters here: local JSON is fine for development and forbidden in
        # production, and the report says which of the two applies without being asked.
        self.assertTrue(report["local_state"]["json_fallback_allowed"])
        self.assertIn("production", report["next_action"])

    def test_production_mode_without_a_database_url_is_blocked_not_silently_local(self) -> None:
        process = run_module("tracking.cli", "preflight-db", cwd=self.root,
                             env={"AI_WEBSITE_AGENT_ENV": "production"})
        self.assertEqual(process.returncode, 1, process.stderr[-400:])
        report = payload_of(process)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("Refusing to fall back to temporary local JSON storage", report["blockers"][0])
        self.assertFalse(report["local_state"]["json_fallback_allowed"])
        self.assertTrue(report["local_state"]["production_mode"])
        self.assertFalse((self.root / "data" / "tracking" / "state.json").exists(),
                         "a preflight must not materialise the workspace it is only meant to inspect")

    def test_an_unmigrated_database_is_needs_migration_and_nothing_is_created(self) -> None:
        self.assertFalse(self.db_path.exists())
        report = payload_of(run_module("tracking.cli", "preflight-db", "--database-url", self.url))
        self.assertEqual(report["status"], "needs_migration")
        self.assertTrue(report["reachable"])
        self.assertFalse(report["ddl_executed"])
        self.assertFalse(report["migration"]["applied"])
        self.assertTrue(report["migration"]["safe_to_repeat"])
        self.assertEqual(len(report["schema"]["missing_tables"]), len(RelationalStateBackend.OWNED_TABLES))
        self.assertIn("migrate-db", report["next_action"])
        backend = RelationalStateBackend(self.url, auto_migrate=False)
        connection = backend._connect()
        try:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [], "a preflight that reports the schema as absent must not create it")

    def test_a_migrated_database_reports_ready_with_observed_counts(self) -> None:
        self.assertEqual(run_module("tracking.cli", "migrate-db", "--database-url", self.url).returncode, 0)
        report = payload_of(run_module("tracking.cli", "preflight-db", "--database-url", self.url))
        self.assertEqual(report["status"], "ready", json.dumps(report["blockers"]))
        self.assertTrue(report["migration"]["checksum_matches"])
        self.assertEqual(report["migration"]["expected_checksum"], migration_0001_checksum())
        self.assertEqual(report["schema"]["row_counts"]["leads"], 0)
        self.assertIs(report["schema"]["populated"], False)
        self.assertIn("no DDL was executed by this command", report["next_action"])

    def test_connection_faults_are_reported_with_the_credential_redacted(self) -> None:
        def explode():
            raise ConnectionError(f"connection failed for user postgres password={PASSWORD_CANARY} "
                                  f"host db.{SUPABASE_REF}.supabase.co port 5432")

        with mock.patch("tracking.database.RelationalStateBackend._connect", side_effect=explode):
            report = RelationalStateBackend(SUPABASE_URL, auto_migrate=False).preflight()
        self.assertEqual(report["status"], "unreachable")
        self.assertFalse(report["reachable"])
        text = json.dumps(report)
        self.assertNotIn(PASSWORD_CANARY, text, "a preflight answer must not echo the credential back")
        self.assertIn(f"db.{SUPABASE_REF}.supabase.co", report["blockers"][0])
        self.assertIn("preflight-db", report["next_action"])

    def test_a_probe_that_cannot_be_answered_degrades_to_unknown_instead_of_crashing(self) -> None:
        """A restricted role (no information_schema, no table privileges) still gets a usable answer."""
        backend = RelationalStateBackend(self.url, auto_migrate=False)

        class Denied:
            def cursor(self):
                return self

            def execute(self, sql, params=()):
                raise RuntimeError(f"permission denied for {str(sql)[:24]} password={PASSWORD_CANARY}")

            def fetchall(self):
                raise RuntimeError("permission denied")

            def rollback(self):
                pass

            def close(self):
                pass

        with mock.patch.object(RelationalStateBackend, "_connect", return_value=Denied()):
            report = backend.preflight()
        self.assertTrue(report["reachable"])
        self.assertIsNone(report["server_version"])
        text = json.dumps(report)
        self.assertNotIn(PASSWORD_CANARY, text)
        self.assertTrue(any("could not be read" in item for item in report["warnings"] + report["blockers"]),
                        "an unreadable probe is reported as unknown, never as 'schema absent, go ahead'")

    def test_a_database_that_already_holds_records_is_flagged(self) -> None:
        self.assertEqual(run_module("tracking.cli", "migrate-db", "--database-url", self.url).returncode, 0)
        backend = RelationalStateBackend(self.url, auto_migrate=True)
        backend.sync_state({"leads": {"lead-0001": {"lead_id": "lead-0001",
                                                    "business_name": "Fictional Sole Stitch Salon"}}})
        report = payload_of(run_module("tracking.cli", "preflight-db", "--database-url", self.url))
        self.assertEqual(report["schema"]["row_counts"]["leads"], 1)
        self.assertIs(report["schema"]["populated"], True)
        self.assertTrue(any("already holds records" in item for item in report["warnings"]),
                        "a shared or live database deserves an explicit warning before DDL is applied")

    def test_a_checksum_mismatch_is_a_blocking_conflict_not_a_reapply_invitation(self) -> None:
        self.assertEqual(run_module("tracking.cli", "migrate-db", "--database-url", self.url).returncode, 0)
        connection = RelationalStateBackend(self.url, auto_migrate=False)._connect()
        try:
            connection.execute("UPDATE schema_migrations SET checksum = 'ffffffff' WHERE version = '0001_supabase_postgres_schema'")
        finally:
            connection.close()
        report = payload_of(run_module("tracking.cli", "preflight-db", "--database-url", self.url))
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["migration"]["safe_to_repeat"])
        self.assertIn("reconcile by hand", report["blockers"][0])


class TargetIdentificationTests(unittest.TestCase):
    def test_the_project_reference_is_read_from_the_hostnames_that_carry_one(self) -> None:
        self.assertEqual(supabase_project_ref(f"db.{SUPABASE_REF}.supabase.co"), SUPABASE_REF)
        self.assertEqual(supabase_project_ref(f"{SUPABASE_REF}.supabase.co"), SUPABASE_REF)
        # The transaction pooler host Supabase publishes genuinely does not name the project, and
        # saying so is more useful than inventing a match.
        self.assertIsNone(supabase_project_ref("aws-0-us-east-1.pooler.supabase.com"))
        self.assertIsNone(supabase_project_ref("localhost"))
        self.assertIsNone(supabase_project_ref(None))

    def test_identity_names_the_target_without_naming_the_credential(self) -> None:
        identity = RelationalStateBackend(SUPABASE_URL, auto_migrate=False).identity()
        self.assertEqual(identity["host"], f"db.{SUPABASE_REF}.supabase.co")
        self.assertEqual(identity["port"], 5432)
        self.assertEqual(identity["database"], "postgres")
        self.assertEqual(identity["supabase_project_ref"], SUPABASE_REF)
        self.assertFalse(identity["is_local_host"])
        self.assertNotIn(PASSWORD_CANARY, json.dumps(identity))
        self.assertIn("***", identity["url"])

    def test_the_connection_string_supabase_publishes_is_parsed_as_is(self) -> None:
        """Supabase hands out `?sslmode=require` URLs, and the pooler variant on port 6543.

        The identity check is what the confirmation guard and `--expect-project-ref` compare against, so a
        query string or a non-default port must not silently change the database name or lose the project.
        """
        direct = RelationalStateBackend(
            f"postgresql://postgres:{PASSWORD_CANARY}@db.{SUPABASE_REF}.supabase.co:5432/postgres?sslmode=require",
            auto_migrate=False).identity()
        self.assertEqual(direct["database"], "postgres", "the query string is not part of the database name")
        self.assertEqual(direct["supabase_project_ref"], SUPABASE_REF)
        self.assertEqual(direct["port"], 5432)
        pooler = RelationalStateBackend(
            f"postgresql://postgres:{PASSWORD_CANARY}@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
            "?sslmode=require&pgbouncer=true", auto_migrate=False).identity()
        self.assertEqual(pooler["database"], "postgres")
        self.assertIsNone(pooler["supabase_project_ref"],
                        "a pooler hostname genuinely does not name the project; the preflight says so")
        self.assertNotIn(PASSWORD_CANARY, json.dumps(direct) + json.dumps(pooler))

    def test_a_connection_failure_names_the_target_once_and_keeps_the_drivers_own_reason(self) -> None:
        """psycopg's message already contains the redacted URL; repeating it hides the actionable sentence."""
        backend = RelationalStateBackend(SUPABASE_URL, auto_migrate=False)
        with mock.patch.object(RelationalStateBackend, "_connect",
                               side_effect=DatabasePersistenceError(
                                   f"Failed to connect to PostgreSQL database {backend.redacted_url}: "
                                   "connection failed: Connection refused. Is the server running on that host?")):
            report = backend.preflight()
        self.assertEqual(len(report["blockers"]), 1)
        self.assertIn("Is the server running", report["blockers"][0])
        self.assertEqual(report["blockers"][0].count(backend.redacted_url), 1)

    def test_the_guards_compare_against_what_the_url_actually_says(self) -> None:
        stub = mock.Mock()
        stub.identity.return_value = None  # unused; the backend object below supplies everything
        probe = {
            "status": "needs_migration", "reachable": True, "ddl_executed": False,
            "identity": {"dialect": "postgres", "host": f"db.{SUPABASE_REF}.supabase.co", "port": 5432,
                         "database": "postgres", "supabase_project_ref": SUPABASE_REF,
                         "is_local_host": False, "url": "postgresql://postgres:***@x/y"},
            "server_version": "PostgreSQL 15.4", "schema": {"present_tables": [], "missing_tables": ["leads"],
                                                            "row_counts": {}, "populated": None},
            "migration": {"version": "0001_supabase_postgres_schema", "file": "migrations.sql",
                          "expected_checksum": "abc", "recorded_checksum": None, "applied": False,
                          "checksum_matches": False, "safe_to_repeat": True, "needs_migration": True},
            "blockers": [], "pending": ["migration not applied"], "warnings": [], "next_action": None,
        }

        class FakeBackend:
            dialect = "postgres"

            def __init__(self, url, **kwargs):
                self.database_url = url

            def preflight(self):
                return json.loads(json.dumps(probe))

        with mock.patch.object(tracking_cli, "RelationalStateBackend", FakeBackend), \
                mock.patch.object(tracking_cli, "_postgres_driver_available", return_value="psycopg"):
            wrong = tracking_cli._database_preflight(SUPABASE_URL, expect_project_ref="ot7zdifferentrefxx")
            right = tracking_cli._database_preflight(SUPABASE_URL, expect_project_ref=SUPABASE_REF,
                                                      expect_database="postgres")
            wrong_database = tracking_cli._database_preflight(SUPABASE_URL, expect_database="other_db")
        self.assertEqual(wrong["status"], "blocked")
        self.assertIn("not the expected 'ot7zdifferentrefxx'", wrong["blockers"][-1])
        self.assertEqual(right["status"], "needs_migration")
        self.assertIn("--confirm-database postgres", right["next_action"])
        self.assertEqual(wrong_database["status"], "blocked")
        for report in (wrong, right, wrong_database):
            self.assertNotIn(PASSWORD_CANARY, json.dumps(report))


# =============================================================================================
# 2. The migration command's remote-target guard
# =============================================================================================
class MigrationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url = f"sqlite:///{Path(self.temp.name) / 'guard.sqlite'}"

    def test_ddl_against_a_remote_database_requires_naming_the_target_back(self) -> None:
        # A refusal is reported on stderr with exit 1, and stdout stays empty: a half-written success
        # document next to an error is how an operator misreads a blocked migration as a completed one.
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(tracking_cli.run_cli(["migrate-db", "--database-url", SUPABASE_URL],
                                              stdout=out, stderr=err), 1)
        self.assertEqual(out.getvalue(), "")
        message = json.loads(err.getvalue())["error"]
        self.assertIn("without confirmation", message)
        self.assertIn(f"Supabase project '{SUPABASE_REF}'", message,
                      "the refusal must identify the project the URL points at, not just complain")
        self.assertIn("--confirm-database postgres", message, "and name the flag that clears it")
        self.assertNotIn(PASSWORD_CANARY, message)

    def test_a_confirmation_that_does_not_match_the_url_is_refused(self) -> None:
        err = io.StringIO()
        self.assertEqual(tracking_cli.run_cli(["migrate-db", "--database-url", SUPABASE_URL,
                                               "--confirm-database", "analytics"], stdout=io.StringIO(),
                                              stderr=err), 1)
        self.assertIn("does not match the database named in the configured URL",
                      json.loads(err.getvalue())["error"])

    def test_verify_only_needs_no_confirmation_because_it_writes_nothing(self) -> None:
        buffer = io.StringIO()
        self.assertEqual(tracking_cli.run_cli(["migrate-db", "--database-url", self.url], stdout=buffer), 0)
        self.assertEqual(json.loads(buffer.getvalue())["status"], "MIGRATED")
        buffer = io.StringIO()
        self.assertEqual(
            tracking_cli.run_cli(["migrate-db", "--database-url", self.url, "--verify-only"], stdout=buffer), 0)
        self.assertEqual(json.loads(buffer.getvalue())["checksum"], migration_0001_checksum())

    def test_repeating_the_migration_is_additive_and_says_what_it_found_first(self) -> None:
        first = io.StringIO()
        self.assertEqual(tracking_cli.run_cli(["migrate-db", "--database-url", self.url], stdout=first), 0)
        first_payload = json.loads(first.getvalue())
        self.assertFalse(first_payload["schema_before"]["migration_applied"])
        self.assertTrue(first_payload["schema_before"]["missing_tables"])

        second = io.StringIO()
        self.assertEqual(tracking_cli.run_cli(["migrate-db", "--database-url", self.url], stdout=second), 0)
        second_payload = json.loads(second.getvalue())
        self.assertTrue(second_payload["schema_before"]["migration_applied"],
                        "the second run reports that the schema was already there")
        self.assertEqual(second_payload["checksum"], first_payload["checksum"])
        self.assertTrue(second_payload["safe_to_repeat"])
        verify = io.StringIO()
        self.assertEqual(tracking_cli.run_cli(["migrate-db", "--database-url", self.url, "--verify-only"],
                                              stdout=verify), 0)


# =============================================================================================
# 3. One command for the four readiness questions
# =============================================================================================
class IntegrationPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "n8n" / "workflows").mkdir(parents=True)
        # Every export the repository carries, staged into the scratch workspace: a fifth workflow that
        # joins n8n/workflows/ is checked here too instead of drifting out of the assertion below.
        for path in sorted((REPO_ROOT / "n8n" / "workflows").glob("*.json")):
            shutil.copyfile(path, self.root / "n8n" / "workflows" / path.name)
        self.state_path = self.root / "state.json"

    def cli(self, *argv: str, env: dict[str, str] | None = None):
        return run_module("orchestration.cli", "--state-path", str(self.state_path), *argv,
                          env={"AI_WEBSITE_AGENT_API_TOKEN": "local-fixture-token-0123456789abcdef"} | (env or {}),
                          cwd=self.root)

    def test_the_sections_answer_different_questions_and_are_noted_as_such(self) -> None:
        report = payload_of(self.cli("preflight"))
        sections = report["sections"]
        self.assertEqual(
            sorted(sections),
            ["api_liveness", "automation_n8n", "deployment_provider_configuration", "operator_channels",
             "storage_readiness", "workflow_execution_readiness"])
        self.assertFalse(sections["api_liveness"]["verified"], "no socket is bound, so liveness is not claimed")
        self.assertIn("no socket was opened", sections["api_liveness"]["evidence"])
        self.assertFalse(sections["storage_readiness"]["verified"], "no database was contacted here")
        self.assertTrue(sections["workflow_execution_readiness"]["verified"], "the queue was actually read")
        self.assertFalse(sections["deployment_provider_configuration"]["verified"])
        self.assertEqual(report["assurance"]["connectivity"], "not_tested")
        self.assertEqual(report["assurance"]["preview_deployment"].split(" ")[0], "not_performed")

    def test_no_network_no_provider_no_mutation_and_no_token_in_the_output(self) -> None:
        from orchestration.health import integration_preflight

        token = "local-fixture-token-0123456789abcdef"
        process = self.cli("preflight", env={"AI_WEBSITE_AGENT_API_TOKEN": token})
        self.assertNotIn(token, process.stdout, "the shared API credential never belongs in a report")
        self.assertNotIn(token, process.stderr)
        report = payload_of(process)
        actions = report["actions_not_performed"]
        self.assertEqual(actions["network_calls"], 0)
        self.assertEqual(actions["provider_commands_executed"], 0)
        self.assertFalse(actions["ddl_executed"])
        self.assertFalse(actions["socket_bound"])
        self.assertFalse(actions["state_mutated"])
        self.assertFalse(self.state_path.exists(), "preflight must not create the state file it reads")

        # In-process, with the two ways this command could reach outside the workspace both wired shut.
        store = tracking_cli.TrackingStore(self.state_path)
        with mock.patch("urllib.request.urlopen") as urlopen, \
                mock.patch("deployment.deployer.subprocess.run") as provider_run, \
                mock.patch("socket.socket") as socket_:
            integration_preflight(store, repository_root=self.root)
        urlopen.assert_not_called()
        provider_run.assert_not_called()
        socket_.assert_not_called()

    def test_a_valid_token_clears_the_liveness_blocker_without_starting_a_server(self) -> None:
        report = payload_of(self.cli("preflight"))
        liveness = report["sections"]["api_liveness"]
        self.assertTrue(liveness["ok"], liveness)
        self.assertEqual(liveness["bind_host"], "127.0.0.1")
        self.assertEqual(liveness["liveness_route"], "/healthz")
        self.assertFalse(liveness["reachable_beyond_this_machine"])

    def test_a_non_loopback_bind_with_a_weak_token_is_refused_before_anything_listens(self) -> None:
        process = self.cli("preflight", "--allow-non-loopback",
                           env={"AI_WEBSITE_AGENT_API_TOKEN": "short", "RENDER": "1"})
        report = payload_of(process)
        self.assertEqual(report["status"], "blocked")
        liveness = report["sections"]["api_liveness"]
        self.assertFalse(liveness["ok"])
        self.assertEqual(liveness["token_strength"], "too_short_for_public_bind")
        self.assertTrue(liveness["would_be_refused_by_serve"],
                        "the preflight reproduces serve's refusal instead of discovering it later")
        self.assertTrue(any("shorter than 32 characters" in item for item in report["blockers"]),
                        json.dumps(report["blockers"]))
        # The value that was rejected is never echoed back into the report.
        self.assertNotIn('"short"', json.dumps(report))
        self.assertEqual(process.returncode, 1)

    def test_the_production_storage_expectation_reaches_the_preflight(self) -> None:
        process = self.cli("preflight", env={"AI_WEBSITE_AGENT_ENV": "production"})
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("Traceback", process.stderr, "a refused startup is an answer, not a crash")
        report = payload_of(process)
        self.assertEqual(report["status"], "blocked")
        # `serve` would have died here on the missing database URL; the report says which rule fired and
        # that connectivity failed, rather than presenting the local configuration as healthy.
        self.assertIn("Refusing to fall back to temporary local JSON storage", report["blockers"][0])
        self.assertEqual(report["assurance"]["connectivity"], "failed")
        self.assertEqual(report["sections"]["storage_readiness"]["status"], "unavailable")
        self.assertTrue(any("preflight-db" in item for item in report["next_actions"]))

    def test_the_preview_gate_answer_is_included_verbatim_for_one_project(self) -> None:
        store = tracking_cli.TrackingStore(self.state_path)
        lead = store.create_lead({"business_name": "Fictional Sole Stitch Salon", "city": "Lagos",
                                 "country": "Nigeria", "qualification_status": "QUALIFIED",
                                 "data_classification": "TEST_FIXTURE"})
        project = store.create_project(lead["lead_id"])
        report = payload_of(self.cli("preflight", "--project-id", project["project_id"]))
        preview = report["sections"]["preview_readiness"]
        # No build is recorded yet, so the gate says exactly that: the preflight reports the live gate's
        # own code instead of a generic "not ready".
        self.assertEqual(preview["code"], "BUILD_NOT_ASSIGNED")
        self.assertIs(preview["authorized"], False)
        self.assertIn("Build and validate a website first", preview["reason"])
        self.assertTrue(any("BUILD_NOT_ASSIGNED" in step and "Build and validate" in step
                            for step in report["manual_steps"]), json.dumps(report["manual_steps"]))
        # Reading the gate must not change it.
        self.assertEqual(tracking_cli.TrackingStore(self.state_path).snapshot(project["project_id"])
                         ["project"]["review_status"], "PENDING")

    def test_missing_n8n_configuration_is_a_named_blocker_not_a_crash(self) -> None:
        report = payload_of(self.cli("preflight"))
        automation = report["sections"]["automation_n8n"]
        # One more exported workflow per phase that adds a scheduled surface; the count is the assertion, so a
        # file that appears in n8n/workflows/ without being reviewed here is a failing test, not an oversight.
        self.assertEqual(automation["checked_count"], 7)
        self.assertTrue(any("AI_WEBSITE_AGENT_API_URL" in item for item in report["blockers"]),
                        json.dumps(report["blockers"]))
        self.assertFalse(automation["requests_sent"])
        self.assertEqual(automation["api_url_configured"], False)

    def test_setting_the_api_url_moves_the_workflow_blocker_out_of_the_way(self) -> None:
        before = payload_of(self.cli("preflight"))
        after = payload_of(self.cli("preflight", env={"AI_WEBSITE_AGENT_API_URL": "http://127.0.0.1:8787"}))
        self.assertTrue(any("AI_WEBSITE_AGENT_API_URL" in item for item in before["blockers"]))
        self.assertFalse(any("AI_WEBSITE_AGENT_API_URL" in item for item in after["blockers"]))
        self.assertEqual(after["sections"]["automation_n8n"]["api_url_configured"], True)


# =============================================================================================
# 4. n8n workflow configuration readiness
# =============================================================================================
class N8nWorkflowReadinessTests(unittest.TestCase):
    def test_the_seven_exported_workflows_are_importable_and_inactive(self) -> None:
        from orchestration.n8n_runner import workflow_readiness

        paths = sorted((REPO_ROOT / "n8n" / "workflows").glob("*.json"))
        # The count is the count: an eighth exported workflow must be listed here, which is the point - an
        # export that silently joined the directory would otherwise be validated by nothing.
        self.assertEqual([path.name for path in paths], [
            "01-create-job.json", "02-inspect-job.json", "03-core-lead-to-review-package-pipeline.json",
            "04-recover-and-retry-jobs.json", "05-inbound-response-intake.json",
            "06-email-cycle-and-operator-notification.json", "07-approved-outbound-send.json"])
        report = workflow_readiness(paths, env={"AI_WEBSITE_AGENT_API_URL": "http://127.0.0.1:8787",
                                                "AI_WEBSITE_AGENT_API_TOKEN": "t"})
        self.assertEqual(report["importable_count"], 7)
        self.assertEqual(report["missing_environment"], [])
        self.assertIs(report["requests_sent"], False)
        for entry in report["workflows"]:
            self.assertIs(entry["active_in_export"], False,
                          "an exported workflow that arrives already activated would start running on import")
            self.assertGreater(entry["node_count"], 0)

    def test_the_send_workflow_is_inert_as_exported(self) -> None:
        """The one exported workflow that *could* contact a prospect must arrive unable to.

        Three properties, each checked in the file itself rather than in a runner: it is inactive, its send node
        carries no credential of its own, and its parameters make the first run a dry run with no confirmation - so
        importing it cannot mail anybody, and a real send needs an operator to edit in both the draft id and the
        word SEND.
        """
        import json

        payload = json.loads((REPO_ROOT / "n8n" / "workflows" / "07-approved-outbound-send.json").read_text())
        self.assertIs(payload["active"], False)
        # Node types are the real capability surface: this workflow can call one HTTP endpoint of our own API and
        # nothing else, so it holds no Gmail or Telegram credential and cannot message a business directly. Checking
        # the types (and the environment names) is meaningful in a way that grepping prose is not.
        self.assertEqual({node["type"] for node in payload["nodes"]},
                         {"n8n-nodes-base.manualTrigger", "n8n-nodes-base.set", "n8n-nodes-base.httpRequest",
                          "n8n-nodes-base.if"})
        for node in payload["nodes"]:
            if node["type"] == "n8n-nodes-base.httpRequest":
                self.assertNotIn("credentials", node, "an exported workflow must not carry a credential reference")
                self.assertTrue(str(node["parameters"]["url"]).startswith("={{ $env.AI_WEBSITE_AGENT_API_URL }}"),
                                "every call goes to this application's own API, never to a provider directly")
        text = json.dumps(payload)
        self.assertNotIn("$env.AI_WEBSITE_AGENT_GMAIL", text)
        self.assertNotIn("refresh_token", text)
        self.assertNotIn("client_secret", text)
        for forbidden_key in ("password", "api_key", "private_key", "token"):
            self.assertNotIn(f'"{forbidden_key}"', text)
        send_node = next(node for node in payload["nodes"] if node["name"] == "Send Approved Draft")
        self.assertEqual(send_node["type"], "n8n-nodes-base.httpRequest")
        self.assertIn("/integrations/email/send", send_node["parameters"]["url"])
        # The exported body has no recipient, subject, or message: those come from the approved record.
        body = send_node["parameters"]["jsonBody"]
        for forbidden in ("recipient", "to", "subject", "draft_message", "body", "channel"):
            self.assertNotIn(f'"{forbidden}":', body)
        parameters = next(node for node in payload["nodes"] if node["name"] == "Send Parameters")
        values = parameters["parameters"]["values"]
        self.assertIs([item for item in values["boolean"] if item["name"] == "dry_run"][0]["value"], True,
                      "an exported send workflow must default to a dry run")
        self.assertEqual([item for item in values["string"] if item["name"] == "confirmation"][0]["value"], "",
                         "an exported send workflow must not carry the confirmation word")

    def test_an_unset_environment_variable_is_reported_before_it_becomes_a_mystery_404(self) -> None:
        from orchestration.n8n_runner import workflow_readiness

        report = workflow_readiness([REPO_ROOT / "n8n" / "workflows" / "03-core-lead-to-review-package-pipeline.json"],
                                    env={})
        entry = report["workflows"][0]
        self.assertIn("AI_WEBSITE_AGENT_API_URL", entry["required_environment"])
        self.assertEqual(entry["missing_environment"], sorted(["AI_WEBSITE_AGENT_API_URL",
                                                                "AI_WEBSITE_AGENT_API_TOKEN"]))

    def test_starter_workflows_are_flagged_as_needing_a_manual_edit_not_secretly_rewritten(self) -> None:
        from orchestration.n8n_runner import workflow_readiness

        report = workflow_readiness([REPO_ROOT / "n8n" / "workflows" / "02-inspect-job.json"],
                                    env={"AI_WEBSITE_AGENT_API_URL": "http://127.0.0.1:8787",
                                         "AI_WEBSITE_AGENT_API_TOKEN": "t"})
        placeholders = report["workflows"][0]["unresolved_placeholders"]
        self.assertEqual([item["value"] for item in placeholders], ["REPLACE_WITH_JOB_ID"])
        self.assertIn("url", placeholders[0]["field"])
        self.assertEqual(report["workflows_needing_manual_edit"],
                         [str((REPO_ROOT / "n8n" / "workflows" / "02-inspect-job.json").as_posix())])

    def test_a_missing_workflow_file_explains_where_it_comes_from(self) -> None:
        from orchestration.n8n_runner import workflow_readiness

        report = workflow_readiness([REPO_ROOT / "n8n" / "workflows" / "99-nope.json"], env={})
        self.assertIs(report["workflows"][0]["present"], False)
        self.assertIn("n8n/workflows/", report["workflows"][0]["problems"][0])

    def test_retries_and_recovery_cannot_reach_the_deployment_boundary_over_http(self) -> None:
        """Workflow 04 retries jobs; a retried DEPLOY must still clear the same gate. The API refuses to
        create one at all, so the workflow cannot bypass human review by re-queueing."""
        from orchestration.api import ALLOWED_API_JOB_TYPES

        self.assertNotIn("DEPLOY", ALLOWED_API_JOB_TYPES)
        report = payload_of(run_module("orchestration.cli", "--state-path", str(Path(tempfile.mkdtemp()) / "s.json"),
                                       "preflight"))
        self.assertFalse(report["sections"]["workflow_execution_readiness"]["deployment_jobs_reachable_over_http"])


# =============================================================================================
# 5. A fresh clone behaves like any other workspace
# =============================================================================================
class FreshCloneBehaviourTests(unittest.TestCase):
    def test_the_suite_reads_workspace_records_without_depending_on_module_order(self) -> None:
        """`tests.test_full_system` used to read the untracked state file directly, so running it alone on
        a fresh checkout raised FileNotFoundError while the full discovery run passed - a suite that only
        works in one order tells you nothing about a new machine."""
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        (scratch / "data" / "tracking").mkdir(parents=True)
        source = (REPO_ROOT / "tests" / "test_full_system.py").read_text(encoding="utf-8")
        self.assertIn("if workspace_state.is_file():", source)
        self.assertIn("recover_from_verified_exports", source)
        self.assertIn("shutil.copyfile(REPO_ROOT / relative, target)", source)

        process = subprocess.run([sys.executable, "-m", "unittest", "tests.test_full_system"], cwd=REPO_ROOT,
                                 capture_output=True, text=True, timeout=900,
                                 env={"PYTHONPATH": str(REPO_ROOT), "PATH": os.environ["PATH"]})
        self.assertEqual(process.returncode, 0, process.stderr[-800:])

    def test_the_recovery_path_used_by_the_tests_produces_the_same_project_records(self) -> None:
        isolated = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, isolated, True)
        for relative in (Path("preview-export/lead-0002-pitstop-lagos/review-package.json"),
                         Path("preview-export/lead-0002-pitstop-lagos/index.html"),
                         Path("preview-export/lead-0002-pitstop-lagos/REVIEW_README.md"),
                         Path("tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json")):
            target = isolated / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, target)
        store = tracking_cli.TrackingStore(isolated / "state.json")
        store.recover_from_verified_exports(repository_root=isolated)
        snapshot = store.snapshot("project-0002")
        self.assertEqual(snapshot["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertEqual(snapshot["project"]["review_status"], "PENDING")
        self.assertIsNone(snapshot["project"].get("approved_build_id"))
        self.assertEqual(snapshot["lead"]["data_classification"], "REAL_PUBLIC_DATA")
        self.assertTrue(snapshot["artifact_exists"])
        for key in ("latestBuild", "latestValidation"):
            self.assertIs(snapshot[key]["reconstructed"], True,
                          "recovered records are stamped as reconstructed, never passed off as fresh history")
            self.assertIn("SHA-256 verified", snapshot[key]["recovery_provenance"])
        report = store.check_local_state(repository_root=isolated)
        self.assertEqual(report["status"], "HEALTHY", json.dumps(report)[:400])

    def test_a_deployed_workspace_without_a_local_state_directory_still_starts_on_the_database(self) -> None:
        """Render clones the repository, and `data/tracking/` is not in Git. With PostgreSQL configured the
        service must neither need nor create that directory - otherwise the failure looks like storage
        corruption at the first write."""
        isolated = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, isolated, True)
        backend = RelationalStateBackend(f"sqlite:///{isolated / 'state.sqlite'}", auto_migrate=True)
        store = tracking_cli.TrackingStore(isolated / "missing-dir" / "state.json", db_backend=backend)
        store.create_lead({"business_name": "Fictional Second Kitchen", "city": "Lagos", "country": "Nigeria",
                           "qualification_status": "QUALIFIED", "data_classification": "TEST_FIXTURE"})
        self.assertFalse((isolated / "missing-dir").exists(),
                         "the JSON path must stay untouched when a database backend owns the state")
        self.assertEqual(list(backend.load_state()["leads"]), ["lead-0001"])


if __name__ == "__main__":
    unittest.main()
