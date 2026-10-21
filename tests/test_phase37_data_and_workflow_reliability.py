"""Phase 37 data and workflow reliability tests.

These tests target only the reliability changes made in this phase:

1. Record identity allocation that cannot reuse an ID still referenced by history.
2. Lost-update protection: a concurrent insert of the same record ID is either an
   idempotent replay or a loud `DatabasePersistenceError`, never a silent overwrite.
3. Exact-replay idempotency for review decisions, and refusal to reuse a
   build/validation/deployment ID for materially different content.
4. Actionable state-transition and build-gate errors.
5. Atomic, non-destructive local state writes and clear refusal when a concurrent
   on-disk state cannot be merged.
6. `system_health()` / readiness reporting that reflects real storage state, and a
   retried DEPLOY job that never publishes the same build twice.

Everything here runs against temporary directories, the configured SQLite test
compatibility database, or fictional fixtures. No real business data, network
request, deployment, or outreach is performed.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import zlib
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from orchestration.api import make_handler
from orchestration.health import storage_report, system_health, verify_storage
from orchestration.jobs import JobService
from orchestration.workers import LocalPipelineWorker
from tracking.database import DatabasePersistenceError
from tracking.store import TrackingStore, next_numeric_id, records_conflict


class _FakeDeploymentResult:
    def __init__(self, status: str) -> None:
        self.deployment_status = status
        self.deployment_id = "deployment-fake-0001"
        self.deployment_url = None
        self.error = None

    def to_dict(self) -> dict:
        return {
            "deployment_id": self.deployment_id,
            "deployment_status": self.deployment_status,
            "deployment_url": self.deployment_url,
            "error": self.error,
            "metadata": {},
        }


class _CountingDeploymentProvider:
    def __init__(self, status: str = "PENDING") -> None:
        self.calls = 0
        self.status = status

    def deploy(self, build_dir, **kwargs):  # noqa: ANN001, ANN003 - stub signature
        self.calls += 1
        return _FakeDeploymentResult(self.status)


class _StubBrokenBackend:
    """Relational backend stand-in whose schema verification fails on purpose."""

    dialect = "postgres"
    redacted_url = "db.test.example:5432/postgres"

    def load_state(self) -> dict:
        return {}

    def sync_state(self, state):  # noqa: ANN001
        return state

    def verify_schema(self) -> None:
        raise DatabasePersistenceError(
            f"Failed to connect to PostgreSQL database {self.redacted_url}: simulated connection refusal"
        )


class ReliabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sqlite_url = f"sqlite:///{self.root / 'reliability.sqlite'}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # Shared fixture helpers (fictional data only)
    # ------------------------------------------------------------------
    def _lead(self, store: TrackingStore, name: str = "Fictional Review Bistro") -> dict:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        digits = f"{abs(zlib.crc32(name.encode())) % 100000000:08d}"
        return store.create_lead({
            "business_name": name,
            "business_type": "restaurant",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": f"+234 800 {digits[:4]} {digits[4:]}",
            "website_status": "WEBSITE_OUTDATED",
            "existing_website": f"https://{slug}.example.invalid",
            "source_urls": [f"https://fixture.example.invalid/{slug}"],
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })

    def _build_and_validate(self, store: TrackingStore, project_id: str, lead_id: str) -> str:
        out_dir = self.root / "output" / lead_id / "build-fixture-0001"
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / "index.html"
        html_path.write_text(
            "<html><body><h1>Fictional Review Bistro — FICTIONAL TEST BUSINESS</h1></body></html>",
            encoding="utf-8",
        )
        store.record_build(project_id, {
            "build_id": "build-fixture-0001",
            "lead_id": lead_id,
            "status": "COMPLETED",
            "template_id": "restaurant-menu-v1",
            "output_location": str(html_path),
        })
        store.record_validation(project_id, {
            "validation_id": "validation-fixture-0001",
            "build_id": "build-fixture-0001",
            "status": "PASS",
            "score": 100,
        })
        return "build-fixture-0001"

    def _record_deployment(self, store: TrackingStore, project_id: str, lead_id: str) -> None:
        store.record_deployment(project_id, {
            "deployment_id": "deployment-fixture-0001",
            "lead_id": lead_id,
            "build_id": "build-fixture-0001",
            "validation_id": "validation-fixture-0001",
            "validation_status": "PASS",
            "deployment_status": "DEPLOYED",
            "deployment_url": "https://fictional-preview.example.invalid",
            "metadata": {"project_name": "fictional-isolated-preview"},
        })


class IdentifierAndDuplicateSafetyTests(ReliabilityTestCase):
    def test_ids_are_never_reused_while_history_still_references_them(self) -> None:
        store = TrackingStore(self.root / "ids.json")
        first = self._lead(store, "Fictional One")
        second = self._lead(store, "Fictional Two")
        third = self._lead(store, "Fictional Three")
        self.assertEqual([first["lead_id"], second["lead_id"], third["lead_id"]], ["lead-0001", "lead-0002", "lead-0003"])

        # A removal (for example an out-of-scope record deleted during review) must not make the
        # next create_lead reuse `lead-0002`, which historical rows may still reference.
        del store.state["leads"][second["lead_id"]]
        store._save()
        fourth = self._lead(store, "Fictional Four")
        self.assertEqual(fourth["lead_id"], "lead-0004")
        self.assertEqual(next_numeric_id(store.state["leads"], "lead"), "lead-0005")
        project = store.create_project(first["lead_id"])
        self.assertEqual(project["project_id"], "project-0001")

    def test_records_conflict_ignores_derived_and_volatile_fields(self) -> None:
        stored = {
            "build_id": "build-x",
            "status": "COMPLETED",
            "output_location": "output/lead-0001/build-x/index.html",
            "artifact_sha256": "abc123",
            "created_at": "2026-10-15T00:00:00Z",
            "updated_at": "2026-10-15T00:00:01Z",
        }
        replay = {
            "build_id": "build-x",
            "status": "COMPLETED",
            "output_location": "output/lead-0001/build-x/index.html",
            "created_at": "2026-10-14T23:59:59Z",
        }
        self.assertFalse(records_conflict(stored, replay))
        different_artifact = dict(replay, output_location="output/lead-0001/build-other/index.html")
        self.assertTrue(records_conflict(stored, different_artifact))

    def test_review_id_allocation_uses_numeric_maximum(self) -> None:
        self.assertEqual(next_numeric_id({}, "review"), "review-0001")
        self.assertEqual(next_numeric_id({"review-0007": {}}, "review"), "review-0008")
        self.assertEqual(next_numeric_id({"review-0002": {}, "manual": {}}, "review"), "review-0003")


class RecordReuseAndReplayTests(ReliabilityTestCase):
    def test_exact_replay_returns_the_existing_record(self) -> None:
        store = TrackingStore(self.root / "replay.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        self._build_and_validate(store, project["project_id"], lead["lead_id"])
        out_path = store.state["builds"]["build-fixture-0001"]["output_location"]

        replayed = store.record_build(project["project_id"], {
            "build_id": "build-fixture-0001",
            "lead_id": lead["lead_id"],
            "status": "COMPLETED",
            "template_id": "restaurant-menu-v1",
            "output_location": out_path,
        })
        self.assertEqual(replayed["build_id"], "build-fixture-0001")
        self.assertEqual(store.state["projects"][project["project_id"]]["current_stage"], "READY_FOR_REVIEW")

        replayed_validation = store.record_validation(project["project_id"], {
            "validation_id": "validation-fixture-0001",
            "build_id": "build-fixture-0001",
            "status": "PASS",
            "score": 100,
        })
        self.assertEqual(replayed_validation["validation_id"], "validation-fixture-0001")

    def test_same_id_with_different_content_is_refused_not_overwritten(self) -> None:
        store = TrackingStore(self.root / "reuse.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        self._build_and_validate(store, project["project_id"], lead["lead_id"])

        other_dir = self.root / "output" / "build-fixture-other"
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / "index.html").write_text("<html><body>different</body></html>", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "already recorded with different content"):
            store.record_build(project["project_id"], {
                "build_id": "build-fixture-0001",
                "lead_id": lead["lead_id"],
                "status": "COMPLETED",
                "template_id": "salon-service-v1",
                "output_location": str(other_dir / "index.html"),
            })
        with self.assertRaisesRegex(ValueError, "already recorded with different content"):
            store.record_validation(project["project_id"], {
                "validation_id": "validation-fixture-0001",
                "build_id": "build-fixture-0001",
                "status": "FAIL",
                "score": 12,
            })
        # The refused writes left the durable records untouched.
        self.assertEqual(store.state["builds"]["build-fixture-0001"]["template_id"], "restaurant-menu-v1")
        self.assertEqual(store.state["validations"]["validation-fixture-0001"]["status"], "PASS")

    def test_deployment_id_cannot_be_replaced_by_a_different_result(self) -> None:
        store = TrackingStore(self.root / "deploy.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        self._build_and_validate(store, project["project_id"], lead["lead_id"])
        self._record_deployment(store, project["project_id"], lead["lead_id"])
        self.assertEqual(store.state["projects"][project["project_id"]]["current_stage"], "PREVIEW_DEPLOYED")

        with self.assertRaisesRegex(ValueError, "already recorded with different content"):
            store.record_deployment(project["project_id"], {
                "deployment_id": "deployment-fixture-0001",
                "lead_id": lead["lead_id"],
                "build_id": "build-fixture-0001",
                "validation_status": "PASS",
                "deployment_status": "DEPLOYED",
                "deployment_url": "https://other-preview.example.invalid",
                "metadata": {"project_name": "fictional-isolated-preview"},
            })
        self.assertEqual(
            store.state["deployments"]["deployment-fixture-0001"]["deployment_url"],
            "https://fictional-preview.example.invalid",
        )

    def test_repeated_identical_review_decision_is_idempotent(self) -> None:
        store = TrackingStore(self.root / "review.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        self._build_and_validate(store, project["project_id"], lead["lead_id"])

        first = store.record_review(
            project["project_id"], "APPROVED_FOR_PREVIEW", "Approved fictional preview build.",
            action="PREVIEW_DEPLOYMENT",
        )
        self.assertEqual(first["review_status"], "APPROVED_FOR_PREVIEW")
        replay = store.record_review(
            project["project_id"], "APPROVED_FOR_PREVIEW", "Approved fictional preview build.",
            action="PREVIEW_DEPLOYMENT",
        )
        self.assertEqual(len(store.state["reviews"]), 1)
        self.assertEqual(len(replay["review_history"]), 1)
        self.assertEqual(replay["approved_build_id"], "build-fixture-0001")

        # A genuinely different decision still appends a second audit entry.
        store.record_review(
            project["project_id"], "CHANGES_REQUESTED", "Header copy needs the verified address.",
            action="REBUILD",
        )
        self.assertEqual(len(store.state["reviews"]), 2)
        self.assertEqual(store.state["projects"][project["project_id"]]["current_stage"], "BUILDING")


class TransitionAndGateMessageTests(ReliabilityTestCase):
    def test_illegal_transition_names_project_stage_and_permitted_targets(self) -> None:
        store = TrackingStore(self.root / "transitions.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        with self.assertRaises(ValueError) as ctx:
            store.transition(project["project_id"], "READY_FOR_OUTREACH")
        message = str(ctx.exception)
        self.assertIn(f"{project['project_id']}: QUALIFIED -> READY_FOR_OUTREACH", message)
        self.assertIn("Permitted next stages from QUALIFIED: ARCHIVED, BUILDING, PAUSED", message)

        with self.assertRaisesRegex(ValueError, "Unknown project"):
            store.transition("project-9999", "BUILDING")

    def test_build_gate_reports_the_current_stage(self) -> None:
        store = TrackingStore(self.root / "gate.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        store.transition(project["project_id"], "PAUSED")
        with self.assertRaisesRegex(ValueError, "is in stage PAUSED and cannot accept a new build"):
            store.record_build(project["project_id"], {
                "build_id": "build-while-paused",
                "lead_id": lead["lead_id"],
                "status": "COMPLETED",
            })
        with self.assertRaisesRegex(ValueError, "Unknown project"):
            store.record_build("project-9999", {"build_id": "build-orphan", "lead_id": lead["lead_id"]})

    def test_unreadable_artifact_is_recorded_on_the_build_instead_of_silently_dropped(self) -> None:
        store = TrackingStore(self.root / "artifact.json")
        lead = self._lead(store)
        project = store.create_project(lead["lead_id"])
        missing = self.root / "output" / "lead-missing" / "build-missing-0001" / "index.html"
        build = store.record_build(project["project_id"], {
            "build_id": "build-missing-0001",
            "lead_id": lead["lead_id"],
            "status": "COMPLETED",
            "output_location": str(missing),
        })
        self.assertIn("artifact_capture_error", build)
        self.assertNotIn("build-missing-0001", store.state.get("build_artifacts") or {})
        # Review packaging therefore fails with an explicit reason rather than an empty package.
        with self.assertRaisesRegex(ValueError, "does not exist on disk"):
            store.build_review_package(project["project_id"], repository_root=self.root)
        # A later good build clears the stale warning.
        self._build_and_validate(store, project["project_id"], lead["lead_id"])
        self.assertNotIn("artifact_capture_error", store.state["builds"]["build-fixture-0001"])

    def test_qualification_id_cannot_be_reused_across_leads(self) -> None:
        store = TrackingStore(self.root / "qualification.json")
        first = self._lead(store, "Fictional One")
        second = self._lead(store, "Fictional Two")
        stored = store.record_qualification({
            "qualification_id": "qualification-fixed-001",
            "lead_id": first["lead_id"],
            "status": "QUALIFIED",
            "score": 80,
        })
        self.assertEqual(store.state["qualifications"]["qualification-fixed-001"]["lead_id"], first["lead_id"])
        # Replaying the same qualification for the same lead is idempotent.
        replay = store.record_qualification({
            "qualification_id": "qualification-fixed-001",
            "lead_id": first["lead_id"],
            "status": "QUALIFIED",
            "score": 80,
        })
        self.assertEqual(replay["created_at"], stored["created_at"])
        with self.assertRaisesRegex(ValueError, "already assigned to another lead"):
            store.record_qualification({
                "qualification_id": "qualification-fixed-001",
                "lead_id": second["lead_id"],
                "status": "QUALIFIED",
                "score": 81,
            })


class LocalStateWriteSafetyTests(ReliabilityTestCase):
    def test_state_write_is_atomic_and_leaves_no_temporary_files(self) -> None:
        state_path = self.root / "atomic.json"
        store = TrackingStore(state_path)
        self._lead(store, "Fictional Durable")
        original = state_path.read_text(encoding="utf-8")

        def _failing_replace(src, dst):  # noqa: ANN001 - simulate an interrupted rename
            raise OSError("simulated disk failure during rename")

        with patch("tracking.store.os.replace", side_effect=_failing_replace):
            with self.assertRaises(OSError):
                store.create_lead({
                    "business_name": "Fictional Interrupted",
                    "city": "Abuja",
                    "country": "Nigeria",
                    "qualification_status": "QUALIFIED",
                    "data_classification": "TEST_FIXTURE",
                })
        # The previously committed state is intact and still parseable.
        self.assertEqual(state_path.read_text(encoding="utf-8"), original)
        self.assertEqual(json.loads(original)["leads"]["lead-0001"]["business_name"], "Fictional Durable")
        # No partially written temporary files are left behind.
        self.assertEqual([p.name for p in self.root.glob("*.tmp")], [])

        # A successful write replaces the file and still leaves no temporary residue.
        store._save()
        reloaded = TrackingStore(state_path)
        self.assertIn("lead-0002", reloaded.state["leads"])
        self.assertEqual([p.name for p in self.root.glob("*.tmp")], [])

    def test_failed_concurrent_merge_is_refused_instead_of_clobbered(self) -> None:
        state_path = self.root / "merge.json"
        store = TrackingStore(state_path)
        lead = self._lead(store, "Fictional Keep Me")

        # Another writer appends a lead to the same state file after our snapshot was loaded.
        other = TrackingStore(state_path)
        other_lead = self._lead(other, "Fictional Other Writer")
        concurrent_content = state_path.read_text(encoding="utf-8")
        self.assertIn(other_lead["lead_id"], json.loads(concurrent_content)["leads"])

        # Deleting a lead forces a real merge; if that merge faults, nothing may be written.
        del store.state["leads"][lead["lead_id"]]
        with patch("tracking.store.copy.deepcopy", side_effect=RuntimeError("simulated merge fault")):
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite tracking state"):
                store._save()
        self.assertEqual(state_path.read_text(encoding="utf-8"), concurrent_content)
        self.assertEqual([p.name for p in self.root.glob("*.tmp")], [])

        # The other writer's record survives, and the operation can be retried normally.
        store._loaded_snapshot = json.loads(concurrent_content)
        store.state["leads"][other_lead["lead_id"]] = other_lead
        store._save()
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn(other_lead["lead_id"], final["leads"])
        self.assertNotIn(lead["lead_id"], final["leads"])

    def test_unreadable_state_file_is_reported_but_does_not_block_recovery(self) -> None:
        state_path = self.root / "corrupt.json"
        store = TrackingStore(state_path)
        self._lead(store, "Fictional Recover")
        state_path.write_text('{"leads": {"lead-0001"', encoding="utf-8")  # truncated JSON
        with _capture_stderr() as captured:
            store.create_lead({
                "business_name": "Fictional Recovery Write",
                "city": "Lagos",
                "country": "Nigeria",
                "qualification_status": "QUALIFIED",
                "data_classification": "TEST_FIXTURE",
            })
        self.assertIn("could not be read", captured.text)
        recovered = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {k: v["business_name"] for k, v in recovered["leads"].items()},
            {"lead-0001": "Fictional Recover", "lead-0002": "Fictional Recovery Write"},
        )


class _capture_stderr:
    """Minimal context manager capturing stderr text without extra dependencies."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def __enter__(self) -> "_capture_stderr":
        import io
        import sys

        self._buffer = io.StringIO()
        self._saved = sys.stderr
        sys.stderr = self._buffer
        return self

    @property
    def text(self) -> str:
        return self._buffer.getvalue()

    def __exit__(self, *exc_info) -> None:  # noqa: ANN002, ANN003
        import sys

        sys.stderr = self._saved


class DatabaseConcurrencyTests(ReliabilityTestCase):
    def test_concurrent_lead_creation_keeps_both_records(self) -> None:
        store_a = TrackingStore(self.root / "conc-a.json", database_url=self.sqlite_url)
        store_b = TrackingStore(self.root / "conc-b.json", database_url=self.sqlite_url)
        lead_b = store_b.create_lead({
            "business_name": "Fictional Beta Bistro",
            "city": "Lagos",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        # `store_a` has not seen `lead_b` yet; allocation must still skip the taken identifier.
        lead_a = store_a.create_lead({
            "business_name": "Fictional Alpha Grill",
            "city": "Abuja",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        self.assertNotEqual(lead_a["lead_id"], lead_b["lead_id"])

        verifier = TrackingStore(self.root / "conc-verify.json", database_url=self.sqlite_url)
        self.assertIn(lead_a["lead_id"], verifier.state["leads"])
        self.assertIn(lead_b["lead_id"], verifier.state["leads"])

    def test_conflicting_concurrent_insert_raises_instead_of_overwriting(self) -> None:
        stale = TrackingStore(self.root / "stale.json", database_url=self.sqlite_url)
        fresh = TrackingStore(self.root / "fresh.json", database_url=self.sqlite_url)
        committed = fresh.create_lead({
            "business_name": "Fictional Committed Lead",
            "city": "Lagos",
            "country": "Nigeria",
            "phone": "+234 800 222 3333",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        self.assertNotIn(committed["lead_id"], stale.state["leads"])

        # `stale` independently allocated the same identifier for a different business.
        stale.state["leads"][committed["lead_id"]] = {
            "lead_id": committed["lead_id"],
            "business_name": "Fictional Conflicting Lead",
            "city": "Ibadan",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        }
        with self.assertRaisesRegex(DatabasePersistenceError, "Concurrent-insert conflict on leads"):
            stale._save()

        # The committed row survived, and the operator can recover by reloading and retrying.
        verifier = TrackingStore(self.root / "stale-verify.json", database_url=self.sqlite_url)
        self.assertEqual(verifier.state["leads"][committed["lead_id"]]["business_name"], "Fictional Committed Lead")
        stale.refresh()
        recovered_lead = stale.create_lead({
            "business_name": "Fictional Conflicting Lead",
            "city": "Ibadan",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "TEST_FIXTURE",
        })
        self.assertEqual(recovered_lead["lead_id"], "lead-0002")
        verifier.refresh()
        self.assertEqual(len(verifier.state["leads"]), 2)

    def test_equivalent_concurrent_insert_is_treated_as_idempotent_replay(self) -> None:
        store = TrackingStore(self.root / "replay-db.json", database_url=self.sqlite_url)
        lead = self._lead(store, "Fictional Replay Lead")

        other = TrackingStore(self.root / "replay-db-other.json", database_url=self.sqlite_url)
        # Pretend `other` loaded its snapshot before the lead existed (a stale reader).
        self.assertEqual(other.state["leads"][lead["lead_id"]]["updated_at"], lead["updated_at"])
        other.state["leads"][lead["lead_id"]] = dict(lead, updated_at="2020-01-01T00:00:00Z")
        other.db_backend._loaded_snapshot = {
            **other.db_backend._loaded_snapshot,
            "leads": {},
        }
        other._save()  # identical content apart from a volatile timestamp -> replay, no conflict

        verifier = TrackingStore(self.root / "replay-db-verify.json", database_url=self.sqlite_url)
        self.assertEqual(len(verifier.state["leads"]), 1)
        self.assertEqual(verifier.state["leads"][lead["lead_id"]]["business_name"], "Fictional Replay Lead")
        # The committed row was left untouched rather than rewritten by the replay.
        self.assertEqual(verifier.state["leads"][lead["lead_id"]]["updated_at"], lead["updated_at"])

    def test_duplicate_active_project_for_one_lead_is_blocked_in_the_database(self) -> None:
        store_a = TrackingStore(self.root / "dup-a.json", database_url=self.sqlite_url)
        lead = self._lead(store_a, "Fictional Single Project Bistro")
        # A second connection loaded its state before the project existed.
        store_b = TrackingStore(self.root / "dup-b.json", database_url=self.sqlite_url)
        project = store_a.create_project(lead["lead_id"])
        self.assertNotIn(project["project_id"], store_b.state["projects"])

        # The normal path reuses the ACTIVE project instead of duplicating it.
        reused = store_b.create_project(lead["lead_id"])
        self.assertEqual(reused["project_id"], project["project_id"])

        # A forced second ACTIVE project for the same lead is refused inside the transaction.
        store_b.state["projects"]["project-0002"] = {
            "project_id": "project-0002",
            "lead_id": lead["lead_id"],
            "current_stage": "QUALIFIED",
            "project_status": "ACTIVE",
            "review_status": "PENDING",
            "data_classification": "TEST_FIXTURE",
            "review_history": [],
        }
        with self.assertRaisesRegex(DatabasePersistenceError, "already has an ACTIVE project"):
            store_b._save()

        verifier = TrackingStore(self.root / "dup-verify.json", database_url=self.sqlite_url)
        self.assertEqual(list(verifier.state["projects"].keys()), [project["project_id"]])

    def test_live_project_lock_is_never_stolen_by_a_second_worker(self) -> None:
        store = TrackingStore(self.root / "locks.json", database_url=self.sqlite_url)
        lead = self._lead(store, "Fictional Locked Bistro")
        project = store.create_project(lead["lead_id"])
        jobs = JobService(store)
        jobs.create("BUILD", job_id="lock-job-1", lead_id=lead["lead_id"], project_id=project["project_id"])

        # Second connection claims the job and takes the project lock with a live lease.
        holder = TrackingStore(self.root / "locks-holder.json", database_url=self.sqlite_url)
        from orchestration.workers import WorkerRegistry

        holder_jobs = JobService(holder)
        holder_jobs.create("BUILD", job_id="lock-job-1", lead_id=lead["lead_id"], project_id=project["project_id"])
        registry = WorkerRegistry(holder, lease_seconds=600)
        registry.register("worker-holder", name="Holder", worker_type="LOCAL_AGENT", capabilities=["BUILD"])
        registry.claim("lock-job-1", "worker-holder")

        # A stale intruder force-writes a conflicting lock for another job.
        intruder = TrackingStore(self.root / "locks-intruder.json", database_url=self.sqlite_url)
        intruder.state["project_locks"][project["project_id"]] = {
            "project_id": project["project_id"],
            "job_id": "lock-job-intruder",
            "worker_id": "worker-intruder",
            "lease_id": "lease-intruder",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        # Pretend the intruder loaded its snapshot before the holder claimed the lease.
        intruder.db_backend._loaded_snapshot = {**intruder.db_backend._loaded_snapshot, "project_locks": {}}
        with self.assertRaisesRegex(DatabasePersistenceError, "live lease"):
            intruder._save()

        verifier = TrackingStore(self.root / "locks-verify.json", database_url=self.sqlite_url)
        self.assertEqual(verifier.state["project_locks"][project["project_id"]]["job_id"], "lock-job-1")


class HealthAndRetrySafetyTests(ReliabilityTestCase):
    def test_storage_report_reflects_the_configured_backend(self) -> None:
        json_store = TrackingStore(self.root / "health.json")
        report = storage_report(json_store)
        self.assertEqual(report["backend"], "json")
        self.assertEqual(report["status"], "healthy")
        health = system_health(json_store)
        self.assertEqual(health["application"]["status"], "healthy")
        self.assertEqual(health["source_of_truth"]["storage"]["backend"], "json")
        self.assertEqual(health["job_system"]["jobs"], 0)

        with patch.dict(os.environ, {"AI_WEBSITE_AGENT_ENV": "production"}, clear=False):
            prod_report = storage_report(json_store)
        self.assertEqual(prod_report["status"], "unavailable")
        self.assertIn("PostgreSQL", prod_report["error"])

    def test_readiness_reports_not_ready_when_the_database_cannot_be_verified(self) -> None:
        store = TrackingStore(self.root / "readiness.json")
        store.state["jobs"] = {}
        store.db_backend = _StubBrokenBackend()
        result = verify_storage(store)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("simulated connection refusal", result["error"])
        # A report never carries connection credentials.
        self.assertNotIn("@", json.dumps(result))

        service = JobService(store)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, "test-token-123", worker=False))
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/readiness",
                headers={"Authorization": "Bearer test-token-123"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
            payload = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertEqual(payload["status"], "NOT_READY")
            self.assertEqual(payload["storage_backend"], "postgres")
            self.assertNotIn("@", json.dumps(payload))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_unexpected_store_failure_returns_500_instead_of_dropping_the_connection(self) -> None:
        store = TrackingStore(self.root / "boom.json")
        service = JobService(store)
        boom = RuntimeError("simulated unexpected storage fault")
        with patch.object(TrackingStore, "status_summary", side_effect=boom):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, "test-token-456", worker=False))
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/projects",
                    headers={"Authorization": "Bearer test-token-456"},
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(ctx.exception.code, 500)
                payload = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertIn("simulated unexpected storage fault", payload["detail"])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_retry_of_a_completed_deployment_does_not_publish_again(self) -> None:
        store = TrackingStore(self.root / "deploy-retry.json")
        lead = self._lead(store, "Fictional Preview Bistro")
        project = store.create_project(lead["lead_id"])
        self._build_and_validate(store, project["project_id"], lead["lead_id"])
        self._record_deployment(store, project["project_id"], lead["lead_id"])

        provider = _CountingDeploymentProvider()
        worker = LocalPipelineWorker(repository_root=self.root, output_root=self.root / "output", deployment_provider=provider)
        job = {
            "job_id": "deploy-retry-job",
            "job_type": "DEPLOY",
            "lead_id": lead["lead_id"],
            "project_id": project["project_id"],
            "build_id": "build-fixture-0001",
            "input": {},
            "dry_run": False,
        }
        result = worker.execute(job, {"tracking_store": store, "dry_run": False})
        self.assertEqual(provider.calls, 0, "a retried DEPLOY job must not re-publish an already deployed build")
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output["outcome"], "ALREADY_DEPLOYED_REPEAT_SKIPPED")
        self.assertEqual(result.output["deployment_id"], "deployment-fixture-0001")

        # A superseding build (which clears the stale deployment pointer) is deployed normally.
        store.record_review(project["project_id"], "CHANGES_REQUESTED", "Fictional copy correction requested.", action="REBUILD")
        second_dir = self.root / "output" / "lead-0001" / "build-fixture-0002"
        second_dir.mkdir(parents=True, exist_ok=True)
        (second_dir / "index.html").write_text("<html><body>Fictional v2</body></html>", encoding="utf-8")
        store.record_build(project["project_id"], {
            "build_id": "build-fixture-0002",
            "lead_id": lead["lead_id"],
            "status": "COMPLETED",
            "template_id": "restaurant-menu-v1",
            "output_location": str(second_dir / "index.html"),
        })
        store.record_validation(project["project_id"], {
            "validation_id": "validation-fixture-0002",
            "build_id": "build-fixture-0002",
            "status": "PASS",
            "score": 100,
        })
        # Publishing (job dry_run False) additionally requires an explicit human approval bound to this
        # exact build; the approval is what materializes the capability the deployment gate verifies.
        store.record_review(project["project_id"], "APPROVED_FOR_PREVIEW", "Fictional v2 approved for preview.", action="PREVIEW_DEPLOYMENT")
        job["build_id"] = "build-fixture-0002"
        second_result = worker.execute(job, {"tracking_store": store, "dry_run": False})
        self.assertEqual(provider.calls, 1)
        self.assertEqual(second_result.output["outcome"], "DRY_RUN_VERIFIED_NOT_DEPLOYED")
        self.assertEqual(second_result.status, "COMPLETED")
        # A dry run never advances the project past human review.
        snapshot = store.snapshot(project["project_id"])
        self.assertEqual(snapshot["project"]["current_stage"], "READY_FOR_REVIEW")
        self.assertIsNone(snapshot["project"]["latest_deployment_id"])
        self.assertEqual(store.state["deployments"]["deployment-fixture-0001"]["build_id"], "build-fixture-0001")


if __name__ == "__main__":
    unittest.main()
