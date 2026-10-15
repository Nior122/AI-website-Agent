# Changelog

All notable project changes are recorded here. Dates use ISO 8601 (`YYYY-MM-DD`).

## [Unreleased]

### Added & Hardened — 2026-10-15 (Phase 39: Monitoring and Operational Controls — Liveness vs Readiness, Per-Attempt Job History, Coded Failures, Operator Commands, & an Alert Hook)

- **Four questions, four answers (`orchestration/health.py`, `orchestration/api.py`)**: liveness, readiness, workflow health, and deployment outcome are now separate surfaces so one can never be mistaken for another. `GET /healthz` stays an unauthenticated liveness probe returning exactly `{"status": "ok"}` and documents that it does not read storage; `GET /readiness` actively verifies the configured backend (`503 NOT_READY` with `error_code: STORAGE_UNAVAILABLE`, `liveness: "ALIVE"`, and a `blocking` list naming the recovery command when it cannot be reached — a live process with an unreachable source of truth is never reported ready); `GET /health` returns the composed report with `source_of_truth.verification: verified|configuration_only`, so a report that did not probe a remote backend says so instead of claiming health. Readiness decisions are split into `blocking` (storage/persistence) and `degradations` (no capable worker, missing local build artifacts with a `recover-workspace` action, an operator backlog, deployment state needing review), and a configured-but-unprobed backend reports `unverified`. Configuration reporting is presence-only (`api_token_configured`, `database_configured`, `operational_log_enabled`) and no report or endpoint carries a token, connection string, auth header, or record content.
- **Per-attempt job history from the ledger the system already keeps (`orchestration/jobs.py`, `orchestration/workers.py`)**: every executed attempt — including the service, CLI, and API paths that previously created no record — now writes an `executions` entry (attempt number, worker, start and completion, duration derived from recorded timestamps, outcome, sanitized error type/message, stable `error_code`, and a `trace` of `lead_id`/`project_id`/`build_id`/`validation_id`/`deployment_id`/`outcome`), reusing the existing collection and `job["execution_id"]` instead of adding a table or a second job system. New `executions_for()` returns the ordered history (insertion order of the ledger, not wall-clock text, so it survives an attempt-counter reset), and a `BLOCKED` job records no attempt at all because nothing started. `retry_eligibility()` answers retryability from the recorded budget (`attempts_remaining`, `requires_reset_attempts`, `error_code`, `next_action`), `retry()` now refuses with an actionable message instead of `Retry limit reached.`, and a retry preserves the cause as `last_error_code` / `last_error_at` / `last_error_message` while recording `retry_count`, `retried_at`, and `retry_of_attempt`.
- **Coded, sanitized, durable failures (`orchestration/jobs.py`, `orchestration/workers.py`, `orchestration/observability.py`)**: `prerequisite_failure()` was refactored into `gate_failure()` returning a stable `(code, message)` pair (`LEAD_MISSING`, `BUILD_MISSING`, `VALIDATION_MISSING`, `VALIDATION_NOT_PASSED`, `STAGE_NOT_REVIEWABLE`, `OUTREACH_NOT_READY`, `RETRY_LIMIT_REACHED`, `HEARTBEAT_EXPIRED`, `LEASE_EXPIRED`, `WORKER_FAULT`, `STORE_CANNOT_AUTHORIZE`, and every Phase 38 approval code) while the prose API stays intact. `_finish()` persists `error_code`, `next_action` (a copy-pasteable command for 40+ codes, including the exact `tracking.cli review … --confirm` invocation that clears an `APPROVAL_MISSING` publish), `requires_operator_action`, and a `failure` summary, and the worker registry applies the same vocabulary plus redaction to `output.errors`, `output.warnings`, `execution.error_message`, and `worker.last_error`. `sanitize_diagnostic()`/`redact()` strip bearer and authorization values, named secrets, `sk-` keys, JWTs, and complete connection-string userinfo before anything is persisted or returned, normalize newlines, and bound length. Transient (`QUEUED` retry), permanent (`FAILED`), blocked (`BLOCKED`), and needs-review (`NEEDS_REVIEW`) remain distinct states; a retryable failure inside its budget is never reported as operator work.
- **One attention source, several surfaces (`orchestration/health.py`, `orchestration/api.py`, `orchestration/cli.py`)**: `operator_attention()` derives the backlog once — stale-running jobs from recorded heartbeats, blocked/failed/exhausted jobs, projects awaiting a decision or sent back by review (a `CHANGES_REQUESTED` project is no longer reported as merely awaiting review), and publish outcomes needing resolution — and `GET /attention`, `GET /health`, `python3 -m orchestration.cli attention [--brief]`, and the alert evaluator all read it, so they cannot disagree. `GET /jobs/:id/history` and `inspect-job --history` expose the trace chain (job → lead → project → build → validation → review → deployment id) without duplicating state; `list-jobs` gained `--status`, `--attention-only`, and a `--brief` projection instead of dumping raw records.
- **Deployment outcome, not deployment guess (`deployment/authorization.py:publish_outcome()`, `deployment/deployer.py:provider_configuration()`, `orchestration/health.py:deployment_report()`)**: each project is classified `VERIFIED_RECORDED`, `RECORDED_UNCONFIRMED`, `UNCERTAIN`, `FAILED`, `SUPERSEDED`, or `NOT_ATTEMPTED` from the recorded deployment plus the append-only publish ledger, with the ledger's latest attempt surfaced (URL reduced to host). `UNCERTAIN` maps to `PUBLISH_STATE_UNCERTAIN` and names the deliberate `--resolve-uncertain-publish` recovery, and is never reported as a failure or a success. `provider_configuration()` reports only what local files prove (CLI on `PATH`, link, isolated target, approval presence) and no monitoring surface invokes the CLI, deploys, or mutates the ledger - `GET /deployment-status` and `deployment-status` always state `provider_contacted: false` and `live_url_checked: false`.
- **Structured logging and a notification hook (`orchestration/observability.py`, `orchestration/alerts.py`, new)**: `log_event()` emits one JSON object per line (timestamp, severity, operation, code, trace identifiers, sanitized detail) to stderr, filtered by `AI_WEBSITE_AGENT_LOG_LEVEL` (default `WARNING`) and appended to `AI_WEBSITE_AGENT_LOG_DIR/operations.jsonl` only when an operator configures it. `check_alerts()` evaluates the critical subset of the same attention data and hands each alert to a sink: `LogAlertSink` by default, and `WebhookAlertSink`, which performs **no** network call of its own and delivers only through a caller-injected `transport`, reporting `SKIPPED_TRANSPORT_NOT_CONFIGURED` otherwise and `DELIVERY_FAILED` on a failed send rather than swallowing it. Alerts are deduplicated in-process, an alert without a configured sink is never counted as notified, and no lead, customer, workflow activation, or paid service is touched.
- **No destructive controls added**: `recover-stale` and `recover-workspace` still only reconcile recorded state (`recover-stale` now exits `1` when it found something to recover, so a scheduled check reports the verdict); retries re-run the full prerequisite and approval gate, so `POST /jobs/:id/retry?run` on an unapproved `DEPLOY` still returns `BLOCKED`/`APPROVAL_MISSING` with zero provider calls, and an ineligible retry is a `409` with `RETRY_NOT_ELIGIBLE` and the recorded explanation. There is no reset, purge, or state-erasing command anywhere in the monitoring surface.
- **Tests and workspace hygiene**: `tests/test_phase39_monitoring_and_operational_controls.py` adds 23 focused tests over the six briefed areas using fictional `TEST_FIXTURE` state, a broken-backend double for storage outages, a recording worker double, and a fake transport for the webhook - including a durability check through a second `TrackingStore`, a check that a report never invents an age when no timestamp was recorded, and an assertion that the read-only surfaces leave the state file and publish ledger byte-identical. `tests/test_full_system.py` now loads the workspace records through a disposable copy (`_workspace_store_copy()`), because the Phase 32 review-package tests previously persisted `updated_at` and package digests onto the real `data/tracking/state.json` whenever the suite ran from the repository root. Suite: 222 → 245 passing, 0 failures, 0 errors. No migration, new table, new dependency, external monitoring service, or real deployment was involved.

### Added & Hardened — 2026-10-15 (Phase 38: Human Approval and Safe Deployment Controls — Per-Build Approval, an Enforced Pre-Publish Gate, & Recoverable Deployment State)

- **Approval is a distinct, human-only fact (`tracking/store.py`)**: `record_review()` remains the sole path to `APPROVED_FOR_PREVIEW` and now also records `identity_source` and `data_classification` on the appended review entry (visible through `get_review_history()`), so a decision identifies the exact reviewed build, its validation, the responsible actor, and the mechanism that recorded it. A generated build, a `PASS` validation, a review package, or an approval *plan* never sets or implies approval; `record_deployment()` still refuses any status other than `DEPLOYED`, so a dry run cannot become a deployment record. Identical repeated decisions stay idempotent, distinct decisions stay append-only, and no history is rewritten to legitimize an invalid transition.
- **Approval capability and publish ledger (`deployment/authorization.py`, new)**: An approved decision materializes `output/<lead>/<build>/deployment-approval.json` (project, lead, build, validation, review id, decision, action `PREVIEW_DEPLOYMENT`, artifact SHA-256, reviewer identity, notes, timestamp, SHA-256 self digest) and every real publish appends to `deployment-attempts.json`. `verify_approval()` returns specific codes — `APPROVAL_MISSING`, `APPROVAL_UNREADABLE`, `APPROVAL_TAMPERED`, `APPROVAL_REVOKED`, `APPROVAL_INVALID_DECISION`, `APPROVAL_WRONG_ACTION`, `APPROVAL_BUILD_MISMATCH`, `APPROVAL_LEAD_MISMATCH`, `APPROVAL_VALIDATION_MISMATCH`, `VALIDATION_NOT_PASSED`, `APPROVAL_ARTIFACT_STALE`, `BUILD_ID_UNRECORDED` — and the ledger adds `DEPLOYMENT_ALREADY_RECORDED` and `PUBLISH_STATE_UNCERTAIN`. The capability is revoked (never edited away) by `REJECTED`, `CHANGES_REQUESTED`, an explicit hold, a superseding build, or by the recording of the deployment it authorized, so one approval authorizes exactly one publish and an approval for an older build or a changed artifact can never authorize a newer one.
- **The gate sits at the operation that publishes (`deployment/deployer.py`)**: `VercelDeploymentProvider.deploy()` verifies the approval and the prior-attempt state whenever `dry_run=False` — for every data class, not only `REAL_PUBLIC_DATA` — refusing with `NEEDS_REVIEW` plus `metadata.denial_code` *before* the payload is staged, so a rejected attempt performs no provider call; structural refusals (manifest mismatch, blocked review status, failed build, non-`PASS` validation, production or non-preview target) keep their existing messages and ordering. `DEPLOYING` is recorded before the CLI is invoked and the terminal state after; a CLI timeout or an unverifiable result now ends as the new `PUBLISH_UNCERTAIN` state instead of a guessed `FAILED`, is refused on the next attempt until an operator inspects the provider, and can only be re-run deliberately with `resolve_uncertain_publish`. A missing provider CLI is now an actionable configuration error instead of an `OSError`, and the publish call is bounded by `DEFAULT_DEPLOY_TIMEOUT_SECONDS`.
- **Server-side authorization shared by every path (`tracking/store.py`, `orchestration/workers.py`, `orchestration/jobs.py`)**: the new `TrackingStore.authorize_preview_deployment(project_id, build_id=...)` answers what a real deployment may do right now from the durable record (project existence and ownership, assigned build, `PASS` validation for that build, reviewable stage, approval binding and supersession, artifact presence, prior publish attempts, and the real-business isolated-target rule) with `authorized` / `code` / `reason` / `skip_publish`. `LocalPipelineWorker` consults it before a non-dry-run publish (refusal -> `NEEDS_REVIEW`, `DEPLOYMENT_UNAUTHORIZED` or `PUBLISH_STATE_UNCERTAIN`, never retryable; a recorded deployment -> `ALREADY_DEPLOYED_REPEAT_SKIPPED`; a refused audit record after a successful publish -> `DEPLOYMENT_RECORD_FAILED` with the live URL), and `JobService.prerequisite_failure("DEPLOY")` blocks unauthorized real publishes at the job layer too, which also covers `POST /jobs/:id/run` and `POST /jobs/:id/retry` where a stored `DEPLOY` job can execute outside the create-time job-type allowlist. Dry runs keep returning `DRY_RUN_VERIFIED_NOT_DEPLOYED` and need no approval because they can never publish.
- **Operator and API surface (`deployment/cli.py`, `tracking/cli.py`, `orchestration/api.py`)**: `deployment.cli --execute` now requires `--project-id` (with `--state-path`, default `data/tracking/state.json`, and optional `--build-id`) and exits `1` with `provider_cli_invoked: false` and no `deployment.json` when the gate refuses; `--resolve-uncertain-publish` records the operator's deliberate resolution. The read-only `tracking.cli deploy-check <project_id>` and `GET /projects/:id/deployment-authorization` report the same verdict with the review command that resolves it, and the new authenticated `POST /projects/:id/review` records approve/reject/request-changes with `decision`, `notes`, `reviewer`, and `confirm=<project_id>:<assigned_build_id>:<DECISION>` (a stale confirmation is refused with `409` before anything is written; the reviewer string is stored verbatim with `identity_verified: false`; the response states `deployed: false`). No endpoint, command, or workflow was activated for production and no new external provider was added.
- **Tests**: `tests/test_phase38_approval_and_deployment_controls.py` adds 27 focused tests over the seven briefed areas — state separation, approval/rejection/change-request recording with idempotent repeats and refused stale decisions, publish refusal without approval at every entry point with zero provider calls, artifact- and build-bound capabilities including tamper and stale-artifact refusal, dry-run semantics, duplicate and interrupted publishes with explicit recovery, cross-path parity, and a relational-backend round trip of the approval and its gate verdict — plus 3 boundary tests in `tests/test_deployment.py` and a documented approval step in the existing Phase 37 no-republish test. Suite: 192 -> 222 passing, 0 failures, 0 errors. No migration, new table, new dependency, or real deployment was involved; all fixtures are fictional.

### Added & Hardened — 2026-10-15 (Phase 37: Data and Workflow Reliability — Idempotent Record Reuse, Concurrent-Write Safety, Atomic State Writes, & Storage-Aware Readiness)

- **Gap-free identifier allocation and content-aware record reuse (`tracking/store.py`, `outreach/service.py`)**: Added `next_numeric_id()` (maximum existing numeric suffix instead of `len(collection) + 1`) for `lead-*`, `project-*`, `review-*`, `outreach-*`, and auto-allocated `qualification-*` identifiers so a deleted or concurrently created record cannot make a still-referenced identifier be reused; `record_qualification()` now refuses an explicit `qualification_id` already assigned to another lead instead of silently returning that lead's record; and `record_build()`, `record_validation()`, `record_deployment()` distinguish an exact idempotent replay (returns the stored record, no duplicate history) from a conflicting reuse of the same ID with materially different content, which is now rejected via `records_conflict()` (shared non-timestamp fields only, so derived `project_id`/`lead_id`/`artifact_sha256` fields never cause false conflicts) instead of overwriting the audited record. A repeated identical review decision (`record_review()`) is likewise an idempotent replay that appends no second audit entry, while any different decision still appends history.
- **All-or-nothing multi-record operations (`tracking/store.py`)**: Added the guarded in-memory transition helper `_set_stage()` and moved `record_build()`, `record_validation()`, `record_deployment()`, and `record_review()` to a single `_save()` at the end of each operation, replacing sequences of two to four intermediate saves that could leave a project committed in a half-applied stage (for example `BUILDING` with no build record) when a later step or write failed. `transition()` keeps its public validate-and-persist behavior. Illegal transitions and rejected builds now name the project, the current stage, and the permitted targets (`Illegal project state transition for project-0001: QUALIFIED -> READY_FOR_OUTREACH. Permitted next stages ...`) instead of one generic message.
- **Atomic, non-destructive local state writes (`tracking/store.py`)**: `_save()` now serializes to a per-process temporary file, `fsync`s it, and `os.replace()`s it into position (a crash can no longer leave a truncated `data/tracking/state.json`), cleans the temporary file up on failure, computes every merged collection before mutating `self.state` so a faulted concurrent merge raises an explicit `Refusing to overwrite tracking state ... Reload the state (TrackingStore.refresh()) and retry` instead of silently clobbering another writer's records, and reports an unreadable pre-existing state file on stderr rather than swallowing the exception.
- **Concurrency and duplicate-safety enforcement inside the relational transaction (`tracking/database.py`)**: `sync_state()` now detects when a record ID was committed by another connection after the snapshot was loaded: an equivalent payload is treated as an idempotent replay (no write, no duplicate), while different content raises `DatabasePersistenceError` naming the collection, record ID, and the owning `lead_id`/`project_id`/`build_id`/`lease_id` with the recovery instruction, so a lost update can no longer look like success. A `project_locks` row is only replaceable when the stored lease has actually expired (an unparseable expiry is treated as live), so a second worker cannot steal another worker's mutating lock. The one-ACTIVE-project-per-lead business rule is now verified inside the same transaction, refusing a duplicate project for a lead whose ACTIVE project another connection just committed. `create_lead()`, `create_project()`, and `record_review()` refresh from the database first when a backend is configured, keeping duplicate detection, ID allocation, and human-review authorization bound to the newest committed build/validation/deployment state.
- **No repeat of external side effects on retried jobs (`orchestration/workers.py`)**: `LocalPipelineWorker` (`DEPLOY`) returns `ALREADY_DEPLOYED_REPEAT_SKIPPED` when the assigned build already has a recorded `DEPLOYED` deployment, so a retried or re-delivered job never publishes a second preview; deployment job outputs now carry explicit outcomes (`PREVIEW_DEPLOYED`, `DRY_RUN_VERIFIED_NOT_DEPLOYED`, `DEPLOYMENT_REQUIRES_HUMAN_REVIEW`, `DEPLOYMENT_FAILED`) so a verified dry run can never be read as a completed deployment. A build whose durable artifact could not be captured now records `artifact_capture_error` on the build record instead of silently dropping the artifact.
- **Storage-aware health, readiness, and fail-closed HTTP errors (`orchestration/health.py`, `orchestration/api.py`)**: Added `storage_report()` (backend, redacted database URL, production requirement, no connectivity) and `verify_storage()`; `system_health()` now derives `application`/`source_of_truth` status from the storage report and reports per-status job counts with `attention` for `FAILED`/`NEEDS_REVIEW`/`BLOCKED` jobs instead of an unconditional `healthy`; `GET /readiness` answers `503 {"status": "NOT_READY"}` when the configured database cannot be verified or production mode has no database, and both handlers now return a structured, credential-redacted `500` instead of dropping the connection on an unexpected fault.
- **Verification (`tests/test_phase37_data_and_workflow_reliability.py`)**: 23 new targeted tests over identifier reuse, replay vs conflict handling for builds/validations/deployments/qualifications/reviews, transition and build-gate error text, artifact-capture warning, atomic and merge-fault-safe local writes, concurrent lead/project/lock writes against the SQLite test database, storage reporting, readiness fail-closed behavior, and DEPLOY re-publish prevention. Full suite: 192 passing unit and integration tests (up from 169); no production database, real business data, deployment, or outreach was used. No new table, migration, dependency, or queue was introduced.


### Added & Hardened — 2026-10-13 (Phase 36: Cloud Deployment Readiness — Verifiable Workspace Recovery, Supabase PostgreSQL Persistence, Durable Artifact Storage, & Render Configuration)

- **Phase A — Protected and recovered workspace state from verifiable sources (`tracking/store.py`, `tracking/cli.py`, `tests/test_phase21_demonstration.py`, `tests/test_phase22_inspection.py`, `tests/test_phase25_discovery.py`, `tests/test_phase26_real_draft.py`)**: Added `TrackingStore.recover_from_verified_exports()` and `python3 -m tracking.cli recover-verified-exports` to reconstruct `project-0001` (from committed `tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json`) and `project-0002` (`Pitstop Lagos`, `build-2026-10-13T012227z`, `validation-2026-10-13T012227z`, `READY_FOR_REVIEW`, `PENDING`) + `data/leads/pitstop_lagos_qualified_lead.json` strictly from committed fixtures and the surviving review package `preview-export/lead-0002-pitstop-lagos/` after verifying its exact SHA-256 hash (`6d58ba2594fb3f92713aef3671646c65c015d2041f93dc4e6bd5282f5753e99b`, `11,242` bytes). Explicitly marked all reconstructed records with `"reconstructed": True` and `"recovery_provenance"`, refused to fabricate the 5 git-ignored historical audit files (`phase19`, `phase20`, `phase25`, `phase26`, `phase26-correction`), and made the full test suite 100% reproducible from a fresh checkout.
- **Phase B — Implemented normalized, transactional PostgreSQL / Supabase persistence (`tracking/database.py`, `migrations/0001_supabase_postgres_schema.sql`, `tracking/store.py`, `orchestration/jobs.py`, `requirements.txt`)**: Added `RelationalStateBackend`, `DatabasePersistenceError`, `redact_database_url()`, and `migrations/0001_supabase_postgres_schema.sql` defining 15 normalized tables (`schema_migrations`, `leads`, `research_evidence`, `qualifications`, `projects`, `builds`, `validations`, `reviews`, `deployments`, `outreach_packages`, `jobs`, `job_executions`, `workers`, `project_locks`, `system_kv`, `build_artifacts`). Eliminated the single-blob overwrite hazard by persisting per-record diffs (`INSERT ... ON CONFLICT DO UPDATE`) inside atomic database transactions and merging concurrent updates across connections without overwriting unrelated records. Enforced fail-closed `DatabasePersistenceError` in production mode (`AI_WEBSITE_AGENT_ENV=production` / `AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres`) with zero silent fallback to temporary local JSON storage while keeping local unit tests runnable without live Supabase credentials.
- **Phase C — Implemented durable generated artifact & review-package persistence (`tracking/store.py`, `orchestration/workers.py`, `orchestration/api.py`)**: Added `record_build_artifact()`, `get_build_artifact()`, and `get_or_materialize_build_artifact()` to `TrackingStore` so rendered HTML (`index.html`), build manifests (`build.json`), review packages (`review-package.json`, `REVIEW_README.md`), and SHA-256 digests are persisted in `build_artifacts` and can be retrieved, SHA-256-verified, and re-materialized after local `output/` or `preview-export/` directories are removed on container restart. Wired `LocalPipelineWorker` (`VALIDATE`, `DEPLOY`), `build_review_package()`, `verify_review_package()`, and `GET /projects/:id/artifact` to durable artifact storage while failing closed on any SHA-256 mismatch.
- **Phase D — Added Render cloud configuration & liveness probe (`orchestration/api.py`, `orchestration/cli.py`, `render.yaml`, `config/system.settings.example.json`, `orchestration/README.md`)**: Added `resolve_bind_config()` reading Render's `PORT` environment variable while retaining `127.0.0.1:8787` as the default for local development; permitted `0.0.0.0` binding only with explicit `--allow-non-loopback` or `AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK=true` alongside mandatory `AI_WEBSITE_AGENT_API_TOKEN`; added unauthenticated `GET /healthz` returning `{"status": "ok"}` without exposing operational data or secrets; preserved Bearer authentication on all operational endpoints; and added `tests/test_cloud_deployment_readiness.py` (167 passing tests total).

### Added & Verified — 2026-10-13 (Phase 35: Connect Existing AI Website Agent to n8n)

- **Connected loopback HTTP API to `LocalPipelineWorker` and `TrackingStore` (`orchestration/api.py`, `orchestration/cli.py`)**: Replaced the static `409 Conflict` stub on `POST /jobs/:id/run` in `orchestration.api` with live execution via `LocalPipelineWorker`, and added authenticated endpoints (`Authorization: Bearer <AI_WEBSITE_AGENT_API_TOKEN>`, loopback `127.0.0.1` only) for `POST /jobs` (with optional `run: true`), `POST /jobs/:id/run`, `POST /jobs/:id/next` (with optional `run: true`), `POST /jobs/:id/retry` (with optional `input` override and `run: true`), `POST /jobs/recover-stale`, `POST /leads/deduplicate`, `GET /projects/:id`, `POST /projects/:id/review-package`, and `GET /projects/:id/review-package`. Added `retry-job` to `orchestration/cli.py` and wired `orchestration.cli serve` to configure a live `LocalPipelineWorker`.
- **Hardened job idempotency, retry overrides, stage outcome reporting, and re-validation transitions (`orchestration/jobs.py`, `orchestration/workers.py`, `tracking/store.py`)**: Updated `JobService.create()` so repeated submissions of the same job remain idempotent even after worker execution has populated `lead_id`, `project_id`, `build_id`, or `validation_id`; updated `JobService.retry()` to accept safe `input_override` updates when retrying `FAILED` or `NEEDS_REVIEW` jobs within `max_attempts`; updated `LocalPipelineWorker.execute()` (`SCOUT`, `RESEARCH`, `QUALIFY`, `BUILD`, `VALIDATE`) to emit explicit `outcome` values (`DISCOVERED_NEW_LEADS`, `DUPLICATE_BUSINESS`, `DUPLICATE_ALREADY_READY_FOR_REVIEW`, `QUALIFIED_FOR_ASSESSMENT`, `INSUFFICIENT_RESEARCH_EVIDENCE`, `UNQUALIFIED_OPPORTUNITY`, `RESEARCH_REQUIRES_ADDITIONAL_WORK`, `QUALIFIED`, `BUILD_COMPLETED`, `BUILD_FAILURE`, `VALIDATION_PASSED`, `VALIDATION_FAILURE`) and completed-job idempotency; and updated `TrackingStore.record_validation()` to permit re-validation of a project from `FAILED` or `READY_FOR_REVIEW`.
- **Added importable n8n core pipeline and recovery workflows (`n8n/workflows/03-core-lead-to-review-package-pipeline.json`, `n8n/workflows/04-recover-and-retry-jobs.json`, `n8n/README.md`, `orchestration/README.md`)**: Preserved `01-create-job.json` and `02-inspect-job.json` untouched; created `03-core-lead-to-review-package-pipeline.json` implementing `TRIGGER -> BUSINESS DISCOVERY -> DEDUPLICATION -> RESEARCH -> QUALIFICATION -> LEAD CREATION OR UPDATE -> PROJECT CREATION OR UPDATE -> WEBSITE BUILD -> AUTOMATED VALIDATION -> REVIEW-PACKAGE GENERATION -> HUMAN REVIEW PENDING` with all 7 required conditional branches; and created `04-recover-and-retry-jobs.json` for recovering stale `RUNNING` jobs (`POST /jobs/recover-stale`), inspecting job state (`GET /jobs/:id`), and retrying eligible jobs (`POST /jobs/:id/retry`).
- **Added local n8n workflow verifier/runner and 11-test integration suite (`orchestration/n8n_runner.py`, `tests/test_n8n_integration.py`)**: Because no `n8n` binary or container is installed in the sandbox environment, added `N8nWorkflowRunner` to validate exported n8n workflow JSON structure and execute the exact n8n node graph against a live loopback `orchestration.api` HTTP server (`127.0.0.1`), covering all 10 required integration scenarios on isolated fictional test fixtures (158 passing tests total).

### Verified & Hardened — 2026-10-13 (Phase 34: Operational Verification of Human Review Workflow, Review-CLI Conventions, Loopback Serving, & LocalPipelineWorker Safeguards)

- **Resolved `tracking.cli review` pre-deployment approval convention and confirmation tokens (`tracking/cli.py`)**: Verified and normalized `tracking.cli review` so both the canonical direct form (`--decision APPROVED_FOR_PREVIEW [--action PREVIEW_DEPLOYMENT]`) and the supported alias form (`--decision APPROVED --action PREVIEW_DEPLOYMENT`) deterministically derive internal decision `APPROVED_FOR_PREVIEW` with `intended_action = "PREVIEW_DEPLOYMENT"`, and accept exact confirmation tokens `<project_id>:<build_id>:APPROVED_FOR_PREVIEW:PREVIEW_DEPLOYMENT`, `<project_id>:<build_id>:APPROVED_FOR_PREVIEW`, and `<project_id>:<build_id>:APPROVED:PREVIEW_DEPLOYMENT`.
- **Enforced pre-deployment human approval on `REAL_PUBLIC_DATA` deployments (`tracking/store.py`, `orchestration/jobs.py`, `outreach/service.py`)**: Updated `TrackingStore.record_deployment()` and `JobService.prerequisite_failure()` (`DEPLOY`) so `REAL_PUBLIC_DATA` builds cannot be deployed (or pass `prepare-preview`) unless the project has recorded `review_status == "APPROVED_FOR_PREVIEW"`, `approved_build_id == assigned_build_id`, and `approved_action == "PREVIEW_DEPLOYMENT"`. Hardened `OutreachService.readiness()` and `OutreachService.create()` to verify `approved_build_id == assigned_build_id` and `approved_action == "OUTREACH_PREPARATION"` and scope existing draft reuse to the current `(build_id, deployment_id)`.
- **Hardened `LocalPipelineWorker` retry/re-run idempotency and failure handling (`orchestration/workers.py`, `tracking/store.py`)**: Updated `SCOUT` and `TrackingStore.ingest_research_record()` so re-running discovery or research reuses existing leads on matching `lead_id`, `EXACT_MATCH`, or same-classification `POSSIBLE_MATCH` without creating duplicate leads; ensured `DEPLOY` outputs propagate `lead_id`, `project_id`, `build_id`, `validation_id`, and `deployment_id`; and ensured missing build artifacts in `VALIDATE` return structured `FAILED` `WorkerResult`s.
- **Added Phase 34 operational verification test (`tests/test_full_system.py`)**: Added `test_phase34_operational_verification_review_cli_11_step_workflow_and_worker_safeguards` exercising the 11-step human review workflow, loopback `127.0.0.1` HTTP serving, stale/modified package rejection, and worker safeguards on isolated temporary fixtures while keeping `project-0002` (`Pitstop Lagos`) untouched at `PENDING` (147 passing unit tests).

### Added & Hardened — 2026-10-13 (Phase 33: Full-System Product Audit, LocalPipelineWorker Execution, 7-State Review Machine, & Operator Workflow CLI)

- **Implemented `LocalPipelineWorker` for end-to-end job execution (`orchestration/workers.py`, `orchestration/cli.py`)**: Added `LocalPipelineWorker` implementing `JobWorker.execute()` across all 7 job types (`SCOUT`, `RESEARCH`, `QUALIFY`, `BUILD`, `VALIDATE`, `DEPLOY`, `PREPARE_OUTREACH`) against the actual repository services (`Scout`, `ResearchAgent`, `assess_opportunity`, `TrackingStore`, `WebsiteBuilder`, `validate_html`, `VercelDeploymentProvider`, `OutreachService`) without manual dictionary glue. Added `create-job` and `run-job` to `orchestration/cli.py`.
- **Completed the 7-state human review state machine (`tracking/store.py`, `tracking/cli.py`)**: Added `project_workflow_state()`, `APPROVED_FOR_PREVIEW`, and `PERMITTED_REVIEW_ACTIONS` (`HOLD`, `REBUILD`, `DISCARD`, `PREVIEW_DEPLOYMENT`, `OUTREACH_PREPARATION`). The review state machine now distinguishes `DRAFT_GENERATED`, `VALIDATION_PASSED_READY_FOR_REVIEW`, `CHANGES_REQUESTED` / `REJECTED`, `APPROVED_FOR_PREVIEW_DEPLOYMENT`, `ISOLATED_PREVIEW_DEPLOYED`, `PREVIEW_UNDER_HUMAN_REVIEW`, and `PREVIEW_APPROVED_READY_FOR_OUTREACH`. Human reviewers can inspect the local draft at `READY_FOR_REVIEW` and either request changes without deploying or approve a specific `build_id` for `PREVIEW_DEPLOYMENT`; recording the subsequent deployment transitions to `ISOLATED_PREVIEW_DEPLOYED` and resets `review_status` to `PENDING` (preserving `pre_deployment_review_status: "APPROVED_FOR_PREVIEW"`) so the live preview must be reviewed before `READY_FOR_OUTREACH`.
- **Connected `opportunities.assess_opportunity()` to `ResearchAgent` records and category normalization (`opportunities/service.py`, `tracking/store.py`)**: Updated `assess_opportunity()` to accept either a Scout candidate or a `ResearchAgent` record (`{"candidate": ..., "research": ...}`), normalize categories via `WebsiteBuilder._category()` so `select_template()` recommends `salon-service-v1` or `restaurant-menu-v1` across all supported categories, and surface `conflicting` facts in `content_brief`. Updated `TrackingStore.ingest_research_record()` so `REAL_PUBLIC_DATA` leads never fall back to unverified `candidate` contact/address/social fields and refresh lead fields when re-ingested.
- **Expanded operator workflow CLI (`tracking/cli.py`)**: Added `ingest-lead`, `build`, `prepare-preview`, and `outreach` subcommands alongside `--action` and `--build-id` flags on `review` so operators can run jobs, inspect results, request corrections, rebuild, and prepare authorized previews without manual JSON edits.
- **Added Phase 33 regression test (`tests/test_full_system.py`)**: Added `test_phase33_local_pipeline_worker_review_state_machine_and_operator_workflow`; full test suite increased from 145 to 146 passing tests.

### Audited & Hardened — 2026-10-13 (Phase 32: Pre-Deployment Readiness Audit, Review-Package Reproducibility & Stale-Export Detection, and Local Review Server Verification)

- **Verified review-package reproducibility and added stale/tampered export detection (`tracking/store.py`, `tracking/cli.py`, `preview-export/lead-0002-pitstop-lagos/`)**: Added `TrackingStore.verify_review_package()` and `python3 -m tracking.cli review-package <project_id> --verify-dir <dir>` to verify that an exported review-package directory (`index.html`, `review-package.json`, `REVIEW_README.md`) matches the project's currently assigned build (`build-2026-10-13T012227z`), latest validation (`validation-2026-10-13T012227z`), and canonical HTML SHA-256 digest (`6d58ba2594fb3f92713aef3671646c65c015d2041f93dc4e6bd5282f5753e99b`), and fails closed on stale build/validation IDs (`STALE_EXPORT_BUILD_ID`, `STALE_EXPORT_VALIDATION_ID`, `STALE_REVIEW_README`), hash mismatches (`PACKAGE_HASH_MISMATCH`, `CANONICAL_HASH_MISMATCH`), fictional demo patterns (`FICTIONAL_DATA_IN_REAL_PACKAGE`), or leaked disputed facts (`DISPUTED_FACT_IN_EXPORTED_HTML`).
- **Enhanced human-review usability without redesigning the website (`tracking/store.py`, `preview-export/lead-0002-pitstop-lagos/`)**: Updated `TrackingStore.build_review_package()` to populate `omitted_disputed_fields`, `claims_lacking_primary_observation` (`SOURCE_URL_ONLY` facts whose raw HTTP/HTML observations are not stored in Git), and `local_preview_instructions` in `review-package.json` and `REVIEW_README.md` while keeping the rendered HTML artifact (`output/lead-0002/build-2026-10-13T012227z/index.html`) unchanged (`11,242` bytes).
- **Hardened local `127.0.0.1` loopback preview server (`builder/cli.py`, `tracking/cli.py`)**: Added `ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost"}` and `validate_serve_target()` to `builder/cli.py`, and wired `python3 -m tracking.cli review-package <project_id> [--export-dir PATH] --serve [--port PORT]` to reuse `builder.cli.serve_build()`. Serving refuses `0.0.0.0` or non-loopback network bindings, validates `review-package.json` `artifact_sha256` against `index.html` before starting, prints explicit `Ctrl+C` stop instructions, and closes the socket in a `finally:` block.
- **Hardened deployment safeguards in `TrackingStore` and `JobService` (`tracking/store.py`, `orchestration/jobs.py`)**: Updated `TrackingStore.record_deployment()` to reject production targets (`metadata.target in {"prod", "production"}` or `*-prod*` / `*-live*` project names) and require a validated isolated preview project name (`validate_preview_project_name(..., real_business=True)`) for all `REAL_PUBLIC_DATA` deployments; updated `JobService.prerequisite_failure()` to validate `project_name` and `isolated_preview_project` with `validate_preview_project_name(..., real_business=True)` before running any `DEPLOY` job.
- **Added Phase 32 regression test (`tests/test_full_system.py`)**: Added `test_phase32_review_package_reproducibility_stale_detection_and_local_server`; full test suite increased from 144 to 145 passing tests.

### Added & Hardened — 2026-10-13 (Phase 31: Evidence Hierarchy, Disputed Fact Withholding, Evidence-Aware Website Generation, & Human-Review Package)

- **Established 5-tier evidence hierarchy and conflict reconciliation (`research/research_agent.py`)**: Added `EVIDENCE_TIERS` (`PRESERVED_PUBLIC_OBSERVATION`, `SOURCE_URL_ONLY`, `GENERATED_CONTENT_COPY`, `TEST_FIXTURE_OR_DOCUMENTATION`, `CONFLICTING_OR_UNVERIFIABLE`), `is_fixture_or_doc_source()`, `classify_evidence_tier()`, and `_reconcile_facts_and_conflicts()`. `ResearchAgent` now rejects test-fixture, synthetic (`example.invalid`), and documentation (`CHANGELOG.md`, `TASKS.md`, `README.md`, `tests/`) sources when researching `REAL_PUBLIC_DATA` candidates, detects conflicting values across candidate and evidence sources, records conflicts in `research.conflictingFacts` and `research.evidenceRecords`, and withholds disputed values from `research.verifiedFacts` without fabricating timestamps or excerpts.
- **Hardened evidence-aware website generation and QA validation (`builder/website_builder.py`, `validation/quality_validator.py`)**: Updated `_verified_template_data()` so `REAL_PUBLIC_DATA` builds reject fixture/documentation `sourceUrls`, never silently resolve conflicting scalar values in `verifiedFacts` / `conflictingFacts` / `evidenceRecords` (purging disputed subfields from nested `location`, `contact`, and `social` containers as well), and preserve `fact_provenance`, `disputed_facts`, and `withheld_fields` on build metadata. Updated `validate_rendered_html()` to fail closed with `DISPUTED_FACT_RENDERED` (`ERROR`) if any disputed/withheld value leaks into rendered HTML or link `href`s.
- **Withheld unresolved Pitstop Lagos street address and telephone conflicts in a new validated build (`data/leads/pitstop_lagos_qualified_lead.json`, `output/lead-0002/build-2026-10-13T012227z/index.html`)**: Traced both street address variants (`92A Younis Bashorun Street` vs `92b Younis Bashorun Street`) and both telephone numbers (`+234 807 749 8324` vs `+234 817 999 9005`) to committed Phase 25/26 test and changelog records; confirmed that no preserved primary-source observation in the workspace establishes which variant is current; explicitly recorded both conflicts under `research.conflictingFacts` and `research.evidenceRecords`; withheld `address` and `phone` from `verifiedFacts` while keeping consistent supported facts (`Pitstop Lagos`, `Lagos`, `Nigeria`, `info@pitstoplagos.com`, `https://www.instagram.com/pitstoplagos/`, description, and 4 offerings); and generated + validated new build `build-2026-10-13T012227z` (`validation-2026-10-13T012227z`, `100/100 PASS`), invalidating `validation-2026-10-12T193556z` while keeping `project-0002` at `READY_FOR_REVIEW` and `review_status: "PENDING"`.
- **Added local human-review package export (`tracking/store.py`, `tracking/cli.py`, `preview-export/lead-0002-pitstop-lagos/`)**: Added `TrackingStore.build_review_package()` and `python3 -m tracking.cli review-package <project_id> [--export-dir PATH]` to export the exact HTML artifact (`index.html`), machine-readable review manifest (`review-package.json`), and operator review brief (`REVIEW_README.md`) summarizing build/validation IDs, SHA-256 digest, supported facts, conflicting/withheld facts, external links, validation limitations, and the uncommissioned disclosure statement.
- **Expanded regression suite (`tests/test_full_system.py`)**: Added `test_phase31_evidence_hierarchy_conflict_withholding_and_review_package`; full test suite increased from 143 to 144 passing tests.

### Audited & Hardened — 2026-10-13 (Phase 30: Pitstop Lagos Human-Review Preparation, Content/Evidence Audit, & Review/Deployment Isolation Verification)

- **Content and evidence audit of Pitstop Lagos draft (`output/lead-0002/build-2026-10-12T193556z/index.html`, `data/leads/pitstop_lagos_qualified_lead.json`)**: Verified that every rendered business fact in `build-2026-10-12T193556z` (`Pitstop Lagos`, `92A Younis Bashorun Street, Victoria Island, Lagos, Nigeria`, `+234 807 749 8324`, `info@pitstoplagos.com`, `https://www.instagram.com/pitstoplagos/`, wellness/cycling cafe description, and 4 offerings cards) matches the committed Phase 26 fact ledger (`tests/test_phase26_real_draft.py` and `CHANGELOG.md`) with zero fictional fixture leakage, zero fabricated prices/dishes/hours, zero external script/font/image dependencies, and valid semantic HTML/CSS structure. Documented for operator review that committed Phase 25 records (`tests/test_phase25_discovery.py`, `CHANGELOG.md`) used plot/phone variants `92b Younis Bashorun Street` and `+234 817 999 9005`, and added explicit per-fact `sourceUrls` provenance to `data/leads/pitstop_lagos_qualified_lead.json`.
- **Enforced preserved `sourceUrls` for real-business builds (`builder/website_builder.py`)**: Updated `_verified_template_data()` so `REAL_PUBLIC_DATA` builds fail closed unless preserved public `sourceUrls` are present on `candidate.sourceUrls`, `research.researchSources`, `lead.source_urls`, or `verifiedFacts[].sourceUrls`.
- **Fixed two-digit card index formatting (`templates/components.py`)**: Changed card index rendering in `card_list()` from `0{index}` to `{index:02d}` so lists with 10+ items render `10`, `11` instead of `010`, `011` while keeping `01`–`04` unchanged on `build-2026-10-12T193556z`.
- **Hardened review and deployment isolation (`deployment/deployer.py`, `tracking/store.py`, `orchestration/jobs.py`)**: Updated `VercelDeploymentProvider._assert_safe_build()` to block manifests with `review_status` in `{"REJECTED", "CHANGES_REQUESTED"}` or mismatched `validation.lead_id`; updated `TrackingStore.record_deployment()` to reject `REAL_PUBLIC_DATA` deployments targeting the shared fictional preview project (`ai-website-agent-preview`) or mismatching `isolated_preview_project`; updated `JobService.prerequisite_failure()` to block `REAL_PUBLIC_DATA` `DEPLOY` jobs unless `approve_real_preview=True` and an isolated preview project target is provided.
- **Added Phase 30 regression test (`tests/test_full_system.py`)**: Added `test_phase30_pitstop_review_and_deployment_isolation_safeguards`; full test suite increased from 142 to 143 passing tests while preserving `project-0002` at `READY_FOR_REVIEW` / `PENDING` / `build-2026-10-12T193556z` with zero regeneration or external deployment.

### Fixed & Hardened — 2026-10-13 (Phase 29: End-to-End Pipeline Integration, Cross-Module Handoffs, & Retry/Recovery Hardening)

- **Fixed candidate empty-dict fact inflation and evidence extraction in `research/research_agent.py`**: Added `_has_meaningful_value()` so candidate nested dicts (`contact`, `social`, `location`) with all-`None` values are recorded in `uncertainties` rather than inflating `verifiedFacts`; added `instagram_url` and `facebook_url` extraction to `_facts_from_evidence()`; preserved top-level `data_classification` on `ResearchJobResult.record`.
- **Added integrated research-to-tracking-to-builder handoffs (`tracking/store.py`, `builder/website_builder.py`)**: Added `TrackingStore.ingest_research_record()` and `TrackingStore.build_payload()` to connect `Scout` / `ResearchAgent` / `assess_opportunity()` outputs to `TrackingStore` and `WebsiteBuilder` without manual dictionary adaptation, preserving `data_classification`, `source_urls`, `research_source_urls`, `website_status`, and `research_record` while failing closed on missing source data.
- **Aligned category aliases and guaranteed global sub-second `build_id` uniqueness (`builder/website_builder.py`)**: Added `"restaurants"`, `"salon"`, `"salons"`, `"cafe"`, `"cafes"`, and `"consulting"` to `WebsiteBuilder._category()` (matching `templates/registry.py`), and added `_allocated_build_ids` plus cross-lead directory checks so multiple builds or failed-build retries within the same second never collide on `build_id`.
- **Hardened retry idempotency and rejected-draft deployment blocking (`tracking/store.py`, `orchestration/jobs.py`, `orchestration/workers.py`)**: Made `record_build()`, `record_validation()`, and `record_deployment()` idempotent when retried with the same ID and status; allowed `record_build()` to supersede builds directly from `READY_FOR_REVIEW` or `VALIDATING`; blocked `record_deployment()` and `DEPLOY` jobs when `review_status` is `REJECTED` or `CHANGES_REQUESTED`; updated `JobService.create_next()` and `WorkerRegistry.report()` to propagate `lead_id`, `project_id`, `build_id`, and `next_job_types`.
- **Expanded end-to-end integration and recovery test suite (`tests/test_full_system.py`)**: Added `test_end_to_end_pipeline_integration_and_isolation` and `test_recovery_and_retry_failure_scenarios` exercising all 12 workflow stages and all 8 failure/retry/recovery scenarios; full test suite increased from 140 to 142 passing tests.

### Added & Hardened — 2026-10-13 (Phase 28: Operator Review CLI, Non-Fabricating Workspace Recovery, & Isolated Preview Target Configuration)

- **Added safe operator review CLI (`tracking/cli.py`, `tracking/store.py`)**: Implemented `status`, `snapshot <project_id>`, `history <project_id>`, `review <project_id> --decision <DECISION> --notes "<NOTES>"`, `configure-preview <project_id> --project-name <SLUG>`, and `check-workspace`. Consequential review decisions (`APPROVED`, `REJECTED`, `CHANGES_REQUESTED`) display the project and assigned build context, require meaningful review notes (`require_notes=True`), and require an explicit confirmation token (`<project_id>:<build_id>:<DECISION>`) via interactive prompt or `--confirm`.
- **Standardized review history schema (`tracking/store.py`)**: Review history entries and `get_review_history()` now expose `project_id`, `lead_id`, `build_id`, `decision`, `notes`, and `timestamp` in chronological order while preserving backward compatibility with `status`, `review_notes`, and `reviewed_at`.
- **Hardened non-fabricating workspace recovery (`tracking/store.py`)**: Removed hardcoded real-business bootstrap records from `TrackingStore` so `check_local_state()` and `recover_local_state()` inspect only actual files on disk, rebuild missing HTML artifacts only when durable qualified source JSON files (`data/leads/*.json` or `tests/fixtures/*.json`) exist, and report `missing_lead_sources` and `unrecoverable_projects` rather than fabricating real-business leads, projects, or audit files.
- **Added isolated preview target configuration (`deployment/cli.py`, `deployment/deployer.py`, `tracking/store.py`)**: Added `--project-name` and `--approve-real-preview` to `deployment/cli.py` and `VercelDeploymentProvider.deploy()`. Real-business (`REAL_PUBLIC_DATA`) preview deployments remain disabled by default (`NEEDS_REVIEW`), cannot use or fall back to the shared fictional demo project (`ai-website-agent-preview`), reject production targets (`*prod*`, `*production*`, `*live*`, or slugs without `preview`), and verify that the linked `.vercel/project.json` matches the expected isolated preview target.
- **Added Phase 28 regression test suite (`tests/test_phase28_operator_cli_and_preview_isolation.py`)**: Added 11 tests covering all CLI commands, review confirmation and isolation, non-fabricating workspace recovery, isolated preview target configuration, shared demo protection, and production target rejection; full test suite increased from 129 to 140 passing tests.

### Fixed & Added — 2026-10-13 (Phase 27: End-to-End Workflow Audit, State Association & Gate Hardening, and Restart Recovery)

- **Prevented failed builds from silently becoming validated or successful (`tracking/store.py`, `deployment/deployer.py`)**: Updated `TrackingStore.record_build()` so builds with `status == "FAILED"` transition the project directly to `FAILED` (never `VALIDATING`), cannot be overwritten with `status == "COMPLETED"` under the same `build_id`, and cannot be validated in `record_validation()`. Updated `VercelDeploymentProvider._assert_safe_build()` to verify `manifest["status"] == "COMPLETED"` and matching `validation.build_id`.
- **Cleared stale downstream references when recording a new build (`tracking/store.py`, `orchestration/jobs.py`, `outreach/service.py`)**: Recording a new build on a project now resets `latest_validation_id`, `latest_deployment_id`, `review_status` (`"PENDING"`), `review_notes`, and `reviewed_at` so previous build approvals/validations/deployments never carry over. Updated `snapshot()`, `JobService`, and `OutreachService.readiness()` to verify that `latestValidation` and `latestDeployment` match the project's current `assigned_build_id`.
- **Enforced exact artifact, lead, and validation binding (`builder/website_builder.py`, `tracking/store.py`)**: `WebsiteBuilder.build()` now stamps `artifact_path` on validation results and `data_classification` on build metadata; `record_validation()` and `record_deployment()` verify matching `lead_id`, `project_id`, `assigned_build_id`, `validation_id`, and `artifact_path` (`output_location`), and require an existing `PASS` validation in `store.state["validations"]` before recording a deployment.
- **Made review decisions explicit, auditable, and recoverable after rejection (`tracking/store.py`)**: `record_review()` now appends a structured audit entry (`review_id`, `project_id`, `lead_id`, `build_id`, `validation_id`, `deployment_id`, `status`, `notes`, `reviewer`, `stage_before`, `stage_after`, `reviewed_at`) to `project["review_history"]` and `state["reviews"]`, requires a reviewable stage (`READY_FOR_REVIEW`, `PREVIEW_DEPLOYED`, or `HUMAN_REVIEW`), and transitions both `CHANGES_REQUESTED` and `REJECTED` drafts (including from `PREVIEW_DEPLOYED`) back to `BUILDING` so rejected drafts can be corrected and reviewed again.
- **Strengthened fictional vs. real-business isolation (`tracking/store.py`, `deployment/deployer.py`, `outreach/service.py`)**: Blocked attaching `TEST_FIXTURE` builds to `REAL_PUBLIC_DATA` projects (and vice versa) in `record_build()`; blocked `REAL_PUBLIC_DATA` builds from deploying to the shared fictional demo project (`ai-website-agent-preview`) in `VercelDeploymentProvider.deploy()` unless `approve_real_preview=True` or a dedicated isolated preview project is configured; added `"WEBSITE_POOR"` and `"WEBSITE_BASIC"` support to `OutreachService._evidence()`.
- **Handled missing local state after cold sandbox restarts safely (`tracking/store.py`, `orchestration/health.py`, `orchestration/cli.py`)**: Added `check_local_state()`, `recover_local_state()`, `ensure_workspace_state()`, and CLI commands `check-workspace` / `recover-workspace` so git-ignored local tracking state (`data/tracking/state.json`), audit records (`data/leads/phase*.json`), and verified build artifacts (`output/lead-0002/build-2026-10-12T193556z/index.html`) are deterministically reconstructed after a sandbox restart without modifying `.gitignore` or auto-approving any project.
- **Added Phase 27 regression suite (`tests/test_phase27_workflow_reliability.py`)**: Added 8 comprehensive workflow reliability tests; full test suite increased from 121 to 129 passing tests.

### Fixed — 2026-10-12 (Phase 26 Recovery: Root-Cause Investigation & Verified Pipeline Fix)

- **Root-cause determination of missing artifacts and unchanged preview**: Verified that commit `4e4c1e22c9fe2e8c50481c120111fb6543120790` included the Python/CSS code edits from the prior Phase 26 correction report, but `.gitignore` excluded `output/`, `data/tracking/`, and `data/leads/*`, so neither `output/lead-0002/build-2026-10-12T041042z/index.html` nor `output/lead-0002/build-2026-10-12T044011z/index.html`, nor `data/tracking/state.json`, nor any `data/leads/*.json` record was present in the GitHub repository (causing 5 of 117 unit tests to fail upon clone). Meanwhile, `README.md` still pointed to Phase 24 (`https://ai-website-agent-preview.vercel.app`), which serves the fictional `Lagos Fresh Kitchen` preview (`build-2026-10-12T024113z`).
- **Eliminated silent candidate fallback in `builder/website_builder.py`**: Hardened `_verified_template_data()` so real-business builds (`not _is_fixture(lead)`) read business facts exclusively from `research.verifiedFacts` and never fall back to unverified `candidate` fields (`openingHours`, `servicesOrProducts`, `description`, `contact`, `social`, `location`, `businessName`); unified `_is_fixture()` detection across `build()` and `_verified_template_data()`.
- **Corrected template component defaults and contact rendering in `templates/components.py` and `templates/styles.css`**: Changed `navigation()` default parameter from `fictional: bool = True` to `fictional: bool = False` so restaurant headers never default to `'LF'`; rendered visible `Telephone: +234 807 749 8324` link alongside CTA buttons and `Email: info@pitstoplagos.com` in `#contact`; fixed CSS contrast where `.button-secondary` in `#contact` had light ivory text on a light ivory background and `.text-link` (`Instagram`) in `.hero-restaurant` had dark green text on a dark green background; corrected `pitstop_lead_payload()` brand mapping so `#de5e28` maps to `accent_color` rather than overwriting `secondary_color` (`#f8f4ea`).
- **Hardened `validation/quality_validator.py` content and fact verification**: Updated `validate_rendered_html()` to check visible page body text (rejecting title-only business name matches), verify `expected_facts` (`address`, `description`, `services`/`offerings`, `instagram_url`/`facebook_url`, and absence of mislabeled `#menu` or unverified `#hours`), verify that rendered `tel:` and `https://wa.me/` links match expected contact digits, treat missing verified email in real builds as `ERROR` (`FAIL`), and expand `FICTIONAL_DEMO_PATTERNS`.
- **Hardened project build isolation in `tracking/store.py`**: Prevented cross-project `build_id` overwrites in `record_build()` and enforced `assigned_build_id` matching in `record_validation()`.
- **Generated fresh verified build and recovery audit**: Produced `output/lead-0002/build-2026-10-12T193556z/index.html` (`build-2026-10-12T193556z`, 100/100 PASS across all 14 checks), updated `TrackingStore` (`project-0002` assigned to `build-2026-10-12T193556z` at `READY_FOR_REVIEW` with `PENDING` human review), preserved `lead-0001`/`project-0001` and historical audit records, recorded `data/leads/phase26-recovery-pitstop-lagos-20261012.json`, and expanded regression suite to 121 passing tests.

### Added — 2026-10-12 (Repository Publishing)

- Initialized Git repository on `main` branch with strict `.gitignore` safety boundaries keeping credentials, local environment files (`.env.local`), Vercel state (`.vercel/`), generated build artifacts (`output/`), local tracking records (`data/tracking/`), and raw discovery data (`data/leads/*`) completely uncommitted.
- Created public GitHub repository `AI-website-Agent` (`https://github.com/Nior122/AI-website-Agent`) under account `Nior122` via GitHub CLI and pushed full codebase history through Phase 26.

### Fixed — 2026-10-12 (Phase 26 Correction: Real-Business Draft Content Purification)

- Complete elimination of fictional demonstration remnants: diagnosed and resolved root causes where the real-business Pitstop Lagos draft inherited the hardcoded fictional shared-meal illustration and 'Made for the table' caption from `components._restaurant_illustration()`. Guarded illustration strictly to fictional demo fixtures (`is_fictional(data) == True`).
- Added responsive single-column layout `.hero-restaurant.no-art` for real-business restaurant concepts without visual illustration dependencies.
- Re-architected template section routing in `templates/renderer.py`: added support for verified offerings/services under section `offerings` with 'What we offer' heading and item indices when menu dishes are unverified, preventing the entire central card section from disappearing.
- Fixed `builder/website_builder.py` data extraction: stopped silent aliasing of services to `menu_items` for real leads, ensuring non-food offerings are never mislabeled as 'Signature dishes'; added extraction of verified contact email into template data.
- Hardened automated validation in `validation/quality_validator.py`: added `FICTIONAL_DEMO_PATTERNS` enforcement blocking fictional business names ('Lagos Fresh Kitchen', 'The Lagos Kitchen', 'Demo Riverside Kitchen'), fictional dishes ('Signature Rice Bowl', 'Lagos Jollof Plate', 'Golden Plantain Bites'), fictional demo prices ('₦0 demo price', '[TEST]'), fictional disclosures ('From the test kitchen', 'Made for the table', 'Fictional dishes for layout testing'), brand mark 'LF', and test fixture domains ('example.invalid') from passing in real builds.
- Added email presence validation ensuring verified business emails appear in rendered customer contact copy.
- Generated new isolated corrected build artifact at `output/lead-0002/build-2026-10-12T044011z/index.html` passing static QA at 100/100 (14/14 checks passed).
- Updated TrackingStore `project-0002` to reference `build-2026-10-12T044011z` at `READY_FOR_REVIEW` with human review held at `PENDING`.
- Preserved historical fictional preview and audit records completely intact.

### Added — 2026-10-12 (Phase 26)

- Fact-grounded local website draft for first qualified real lead Pitstop Lagos (`lead-0002`): reused `restaurant-menu-v1` template with clean brand initials (`PL`), `Lagos · uncommissioned preview draft` eyebrow, and explicit evaluation disclosure (`Uncommissioned preview draft prepared for internal evaluation · Not an authorized or official website of Pitstop Lagos`).
- Strict fact ledger documented in `data/leads/phase26-pitstop-lagos-website-draft-20261012.json`: recorded verified public facts (identity, 92A Younis Bashorun Street address, +234 807 749 8324 phone, info@pitstoplagos.com, Instagram, verified wellness/cafe services) while strictly omitting unverified facts (zero dishes or prices fabricated, no unverified hours or fake booking forms).
- Quality validator hardening in `validation/quality_validator.py`: added detection for casino spam keywords, unparsed template shortcode markup (`[vc_row]`, `[rodich_]`), and hidden off-screen positioning styles (`left/top: -9999px`) in real builds.
- Dedicated draft build artifact generated at `output/lead-0002/build-2026-10-12T041042z/index.html` passing static QA at 100/100 (14/14 checks passed).
- TrackingStore lifecycle progression: created `project-0002`, recorded build and validation, safely transitioning to `READY_FOR_REVIEW` with human review held at `PENDING`.
- Focused Phase 26 regression test suite (`tests/test_phase26_real_draft.py`) verifying lead identity preservation, build qualification gating, fictional/real data isolation, unverified fact omission, spam/shortcode rejection, and tracking transitions.

### Verified — 2026-10-12 (Phase 26)

- Live website reverification: bounded read-only inspection of `https://pitstoplagos.com/` at `2026-10-12T04:04:00Z` re-confirmed unparsed Visual Composer shortcodes and >70 hidden casino spam hyperlinks (`left:-9999px;top:-9999px`) in production DOM.
- Full regression suite: all 112 unit tests passed (0 failures, 0 skipped). `python -m compileall` passed clean. `git diff --check` passed clean.
- Zero fictional content leakage: verified generated real-business draft contains no "LF" brand mark, no "fictional test kitchen" eyebrow, and no fictional disclosure text.
- Historical data intact: `lead-0001` and `project-0001` remain at `READY_FOR_REVIEW` / `PENDING` with live preview at `https://ai-website-agent-preview.vercel.app`. Phase 19, 20, and 25 audit documents remain untouched.

### Not added — 2026-10-12 (Phase 26)

- No external publication or Vercel deployment of the real-business draft (local build only).
- No outreach: zero emails, phone calls, SMS, WhatsApp messages, form submissions, or social media messages.
- No human review approval bypassed; `project-0002` remains `PENDING`.

### Added — 2026-10-12 (Phase 25)

- Bounded discovery and qualification audit: diagnosed historical discovery bottleneck (Overpass static query returning identical 4 nodes lacking website tags); added fallback endpoint retry architecture to `OverpassRestaurantSource` for transient API 504 / timeout resilience.
- Comprehensive Phase 25 discovery and qualification audit document saved at `data/leads/phase25-lagos-restaurants-discovery-and-qualification-20261012.json` evaluating 5 previously unreviewed independent Lagos restaurants with public evidence.
- First verified qualified real lead: Pitstop Lagos (`92b Younis Bashorun Street, Victoria Island, Lagos`) identified with confirmed material production defects (unparsed page builder shortcodes `<p>[vc_row][vc_column]</p>` and over 70 malicious hidden foreign online casino spam backlinks in lines 534–599 and 653–700 of the live HTML payload).
- Lead record `lead-0002` created in `TrackingStore` as `REAL_PUBLIC_DATA` and evaluated under unmodified `assess_opportunity()` policy (score 95, priority P2, high confidence, status `QUALIFIED`).
- Focused Phase 25 unit test suite (`tests/test_phase25_discovery.py`) verifying historical vetoes, branch preservation, duplicate detection, unknown website non-absence, defect validation, project creation gating, fixture separation, audit integrity, and fallback endpoint retry.

### Verified — 2026-10-12 (Phase 25)

- Full regression suite: all 103 unit tests passed (0 failures, 0 skipped). `python -m compileall` passed with zero errors. `git diff --check` passed clean.
- All 5 candidates evaluated with live public evidence without lowering qualification thresholds or inventing defects: Yellow Chilli (`NOT_YET_QUALIFIED`), Ofada Boy (`NOT_YET_QUALIFIED`), Danfo Bistro (`NOT_YET_QUALIFIED`), NOK by Alara (`NOT_YET_QUALIFIED`), and Pitstop Lagos (`QUALIFIED`).
- Prior decisions and audit records from Phase 19 (`data/leads/phase19-lagos-restaurants-backlog-review-20261010.json`) and Phase 20 (`data/leads/phase20-calabar-kitchen-website-verification-20261011.json`) preserved intact.
- Fictional demonstration state preserved: `lead-0001` and `project-0001` remain at `READY_FOR_REVIEW` / `PENDING` with live preview at `https://ai-website-agent-preview.vercel.app`.
- Lead `lead-0002` safely recorded in `TrackingStore` without creating an active website project, build, preview deployment, or outreach draft.

### Not added — 2026-10-12 (Phase 25)

- No real-business website generation, preview deployment, or public publishing.
- No external outreach: zero emails, phone calls, SMS, WhatsApp messages, form submissions, or social media messages.
- No credentials, tokens, or secrets introduced or committed.

### Added — 2026-10-12 (Phase 24)

- Isolated, temporary deployment payload staging for Vercel. Only `index.html` and local assets directly referenced by HTML or local CSS are copied with preserved relative paths; manifests, hidden files, test data, credentials, unrelated files, traversal paths, unsafe symlink targets, missing assets, and secret-like text fail closed.
- Dedicated-target safety gate: `VercelDeploymentProvider` now requires the linked non-secret `projectName` to be `ai-website-agent-preview` and refuses the historic `grace-light-community-church` target before any CLI invocation. Preview execution remains explicitly scoped by project ID and never includes `--prod`.
- Portable symlink escape regression: updated deployment test with mock fallback on `Path.resolve` when OS symlink creation is unavailable on Windows, eliminating skipped tests.
- Deployment tests for staged assets, manifest exclusion, missing/traversal/symlink/secret rejection, church-target rejection, and dedicated-target preview command selection.

### Verified — 2026-10-12 (Phase 24)

- Fresh fictional artifact `output/fictional-lagos-fresh-kitchen-001/build-2026-10-12T024113z/index.html` selected `restaurant-menu-v1` and passed static validation at 100/100 with no findings.
- Operator CLI authentication (`chinecheremjoseph39-4558`) and dedicated project linkage (`.vercel/project.json` identifying `ai-website-agent-preview`, project ID `prj_KXSX2XqjBsbsjSXKKAXXbDrnYJRd`) confirmed.
- Static HTML framework settings verified: Framework Preset `Other`, no build command, root directory `.`.
- Full regression suite: all 91 tests passed (0 failures, 0 skipped). `git diff --check` passed clean.
- Preview deployment executed without `--prod` targeting staged payload (`index.html` only). Succeeded with genuine HTTPS URL: `https://ai-website-agent-preview.vercel.app`.
- Hosted website verified via HTTP GET: returned HTTP 200 OK with anonymous accessibility (no SSO/authentication redirect). Confirmed title (`Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS`), editorial navigation, inline SVG meal illustration, menu cards, and footer fictional disclosure.
- Artifact `deployment.json` updated to `DEPLOYED`. TrackingStore and human review state preserved at `READY_FOR_REVIEW` / `PENDING`.

### Not added — 2026-10-12 (Phase 24)

- No production deployment (`--prod`), DNS/domain modification, real-business deployment, outreach, paid services, commits, or pushes.

### Added — 2026-10-12 (Phase 23)

- Exact Vercel preview-host validation: the adapter now accepts only HTTPS hosts ending in `.vercel.app`, rejecting lookalike and non-HTTPS CLI output.
- Cross-platform binary resolution via `shutil.which("vercel")` in `VercelDeploymentProvider` to support Windows npm command shims (`vercel.cmd`) without `shell=True`.
- Explicit linked-project scoping (`--project <projectId>`) in `VercelDeploymentProvider.deploy()` so subpath deployments strictly target the approved linked project ID rather than triggering unlinked project creation.
- Deployment regressions for unexpected URLs and an unauthenticated Vercel CLI failure, both confirming no fabricated deployment URL is recorded.

### Verified — 2026-10-12 (Phase 23)

- Fresh fictional build `build-2026-10-12T020610z` selected `restaurant-menu-v1`, passed static QA at 100/100, passed structural checks, and rendered locally with working internal links, visible fictional disclosure, and no actionable synthetic contact links.
- Operator CLI authentication (`chinecheremjoseph39-4558`) and project link (`.vercel/project.json` linking `prj_XQCUqsZv41iZJzI07SX8bLy9jPla`, `grace-light-community-church`) confirmed.
- Executed preview deployment adapter without `--prod`. Remote Vercel build failed due to the linked project's Framework Preset (`Vite` / `vite build` exited 127 against zero-dependency static HTML/CSS artifact).
- Deployment adapter failed closed safely: `output/fictional-lagos-fresh-kitchen-001/build-2026-10-12T020610z/deployment.json` records `FAILED` with exact error and `deployment_url: null`. No fabricated URL was recorded.
- TrackingStore state remains at `READY_FOR_REVIEW`; human review remains `PENDING`.

### Not added — 2026-10-12 (Phase 23)

- No production deployment (`--prod`), project recreation, domain/DNS modification, real-business record, outreach, credentials in code/logs, commit, or push.

### Added — 2026-10-12 (Phase 22B)

- Premium reusable restaurant presentation for the fictional `Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS` fixture: bottle-green/ivory/terracotta design tokens, editorial type hierarchy, responsive composition, and an original inline SVG meal illustration with no external asset dependency.
- Safe fixture-contact behavior: `TEST_FIXTURE`/`demo` pages use in-page calls to action, omit synthetic phone/WhatsApp/map/social destinations, and display a persistent fictional-demo disclosure.
- Focused regression coverage for safe synthetic-contact validation; real records continue to require verified contact CTA checks.

### Verified — 2026-10-12 (Phase 22B)

- Fresh fictional artifact `output/fictional-lagos-fresh-kitchen-001/build-2026-10-12T014458z/index.html` passed static validation at 100/100 and browser checks at 1440, 1024, 768, 390, and 360px with no horizontal overflow or console errors.
- No external image, font, script, service, deployment, real-business data, outreach, credential, commit, or push was introduced.

### Added — 2026-10-12 (Phase 22)

- Minimal standard-library `--serve` option in `builder.cli` (`serve_build()`) that binds exclusively to 127.0.0.1 and serves only the specified build artifact directory without external dependencies.
- Sub-second build ID collision protection in `WebsiteBuilder.build()` that inspects target directory existence and appends counter suffixes when multiple builds occur in the same second.
- Derived `validation_id` in `validate_rendered_html()` and `deployment_id` in `VercelDeploymentProvider` anchored directly to `build_id` to guarantee 1-to-1 artifact traceability and prevent tracking state overwrites.
- Phase 22 inspection and repeatability tests (`tests/test_phase22_inspection.py`) verifying local static serving validation, multi-run repeatability, anchor link resolution, landmark structure, and zero real-data leakage.

### Verified — 2026-10-12 (Phase 22)

- Local website artifact inspection verified via zero-server browser opening (`file:///`) and localhost server (`python -m builder.cli --serve ...` and `python -m http.server ...`).
- Build artifact integrity confirmed: generated HTML matches `build.json` manifest output location, all internal navigation anchors (`#top`, `#about`, `#menu`, `#location`, `#hours`, `#contact`) resolve to valid DOM IDs, responsive CSS rules are present, and zero real-business data leaks into fictional output.
- Repeatability verified across multiple consecutive demonstration runs: earlier build directories and tracking records are preserved, `assigned_build_id` advances cleanly, duplicate leads are prevented, and human review remains `PENDING`.
- Vercel preview deployment prerequisites documented; confirmed adapter fails closed safely on unlinked configurations without emitting simulated URLs.
- Full test suite passed (84 tests, 0 failures); Python compilation verified; `git diff --check` passed.

### Added — 2026-10-12 (Phase 21)

- Dedicated fictional test fixture `tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json` for "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS" clearly classified as `TEST_FIXTURE`.
- End-to-end builder demonstration tests (`tests/test_phase21_demonstration.py`) verifying candidate qualification, content brief generation, template selection, deterministic website building, automated QA validation, preview deployment gating, and pending human review record creation.
- Review stage guard in `TrackingStore.record_review()` preventing project state mutation when approval prerequisites are unsatisfied.

### Verified — 2026-10-12

- End-to-end pipeline successfully demonstrated on fictional test business "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS": generated single-page static website with responsive CSS, semantic landmarks, grounded menu/location/contact information; achieved 100/100 validation score (PASS) on all 14 automated QA checks.
- Vercel preview deployment safely failed closed due to absence of approved linked project configuration (`.vercel/project.json`), with exact blocker recorded and zero credentials or fake URLs produced.
- Human review record created in `PENDING` status; website is not automatically approved.
- All 80 unit tests pass; Phase 19 and Phase 20 audit records and qualification decisions remain intact.

### Not added — 2026-10-12

- No real-business lead, project, content brief, build, deployment, outreach, form submission, purchase, or account changes. Calabar Kitchen and Cactus Restaurant remain unqualified.

### Added — 2026-10-11

- A conservative exact-branch comparison helper and focused tests that prevent similarly named, differently located businesses from being silently merged.
- One ignored, review-only Calabar Kitchen ownership/website-condition audit anchored to the independently corroborated Ikeja branch.

### Verified — 2026-10-11

- `calabar-kitchen.com` is `LIKELY_OFFICIAL` with high confidence for the Ikeja main branch, but the accessible public site had no demonstrated material defect satisfying the existing qualification policy. Calabar Kitchen remains `NOT_YET_QUALIFIED`.

### Not added — 2026-10-11

- No Calabar Kitchen lead, project, content brief, build, deployment, contact, outreach, form submission, purchase, account, or production change.

### Added — 2026-10-10

- An offline, non-mutating opportunity-backlog selector that delegates to the existing qualification function, removes duplicate candidate IDs, excludes fictional records, and creates no tracking state.
- A local ignored Phase 19 Lagos restaurant audit covering the four existing candidates and five bounded public-search candidates.

### Verified — 2026-10-10

- `NO_QUALIFIED_OPPORTUNITY_FOUND`: Cactus remains unresolved and none of the five new candidates had both supported ownership and a source-backed website deficiency.

### Not added — 2026-10-10

- No real lead, project, content brief, build, deployment, outreach, form submission, purchase, account, or production change.

### Added — 2026-10-10

- Bounded Phase 18 Cactus public-property evidence, including property classifications, ownership uncertainty, conflicting evidence, and a `WEBSITE_FOUND` result that does not imply a qualified website opportunity.
- Re-verification contract tests covering social/menu-platform ownership preservation and the distinction between unreachable and nonexistent websites.

### Verified — 2026-10-10

- The Cactus record remains `NOT_YET_QUALIFIED`: no independently verified business-controlled professional website or source-backed poor/outdated/basic official-site condition was found.

### Not added — 2026-10-10

- No Cactus lead, project, content brief, build, deployment, contact, or outreach action.

### Verified — 2026-10-10

- Phase 17 confirmed there is no real Cactus lead, project, content brief, build, validation, deployment, preview URL, worker execution, or human-review record. The local output/deployment artifacts are fictional test data only; no preview review can begin.

### Verified — 2026-10-10

- Phase 16 reviewed the persisted Cactus evidence and directly observed the public Facebook page title without login. The remaining profile content was login-gated, so ownership, website quality, and opportunity status remain unverified; the real build gate remains blocked.

### Not added — 2026-10-10

- No real Cactus lead, project, content brief, build, validation, deployment, human-review record, contact, or outreach action.

### Added — 2026-10-08

- A dependency-free targeted public-data re-verification record helper with provenance, property classifications, prior-evidence preservation, and tests.
- One approved, review-only Cactus Restaurant audit event in ignored local lead data; it preserves source URLs and uncertainty without creating a lead, project, build, deployment, or outreach draft.

### Verified — 2026-10-08

- Cactus identity/address/phone are corroborated by several public listings, while the associated Google Business Profile and Facebook properties remain only likely official because direct ownership and quality could not be verified with the approved tool.

### Not added — 2026-10-08

- No Cactus contact, account, order, message, website build, deployment, project, or outreach action.

### Added — 2026-10-08

- Evidence-led website opportunity assessment, content briefs with verified/unknown/suggested separation, qualification history, and idempotent active-project creation.

### Verified — 2026-10-08

- All four Phase 13 real public-data records remain `NEEDS_REVIEW`; no real tracked lead, project, build, deployment, or outreach record was created.

### Added — 2026-10-08

- Bounded, review-only OpenStreetMap Overpass Lagos restaurant discovery adapter with provenance, real-data labeling, timeout, and one-request/five-candidate limits.

### Verified — 2026-10-08

- One approved public-data pilot returned four named candidates; no leads/projects, outreach, deployments, accounts, or messages were created.

### Added — 2026-10-08

- Full fictional local lifecycle integration test and dependency-free system health/readiness reports.

### Verified — 2026-10-08

- A fictional restaurant build can progress through validation, tracking, fictional preview recording, human review, and draft approval to `READY_TO_SEND` without delivery.

### Added — 2026-10-08

- Provider-neutral worker registry, capability-aware router, leases, execution history, failover/recovery, project mutation locks, stale-result protection, human takeover, and deterministic mock-worker tests.

### Not added — 2026-10-08

- No real agent/provider integration, worker host, n8n activation, credential, paid dependency, live deployment, or outreach delivery.

### Added — 2026-10-08

- Application-owned orchestration job ledger with idempotency keys, prerequisite gates, retries, stale-job recovery, dry-run records, and a provider-neutral worker contract.
- Optional fail-closed localhost API plus inactive, credential-free self-hosted n8n workflow examples. No server or workflow is activated by this change.

### Not added — 2026-10-08

- No n8n instance, external queue, database, worker registration, live source, deployment, production deployment, outreach delivery, or credential.

### Added — 2026-10-08

- Evidence-led, human-review-gated outreach preparation with revision history and no sending capability.

### Added — 2026-10-08

- Single-file local lead/project tracking with guarded state transitions, history preservation, deduplication, human review, and project snapshots.

### Added — 2026-10-08

- Preview-only Vercel deployment adapter with artifact-local deployment records, dry-run default, validation gate, Git SHA capture where available, and normalized CLI results.
- Deployment record schema and mocked Vercel CLI tests for success, failure, review gating, missing configuration, and source/build mismatch.

### Not added — 2026-10-08

- No Vercel project link, credential setup, live deployment, production deployment, Git commit, GitHub remote, push, or repository creation.

### Added — 2026-10-08

- Dependency-free Phase 6 static QA validator with severity findings, transparent scoring, PASS/FAIL/NEEDS_REVIEW results, and a local CLI.
- Validation contracts and fixtures covering links, images, CTAs, mobile metadata, accessibility landmarks, SEO basics, placeholders, suspicious claims, and potential secrets.

### Not added — 2026-10-08

- No browser automation, live URL checks, automatic repair, deployment, or external validation service.

### Added — 2026-10-08

- Provider-neutral Website Builder with deterministic fact-grounded content fallback, local build jobs, validation, and local preview artifacts.
- Build-job and generated-content schemas plus fictional qualified-lead build fixtures and tests.

### Not added — 2026-10-08

- No external AI model/provider, real-business build, deployment, hosting, contact flow, payment, outreach, or automation integration.

### Added — 2026-10-08

- Dependency-free reusable template system with deterministic category selection, shared semantic components, data-driven content, brand CSS variables, and local static rendering.
- Fictional salon and restaurant demos, template data schema, and template validation tests.

### Not added — 2026-10-08

- No AI content generation, real-business website generation, image collection, deployment, hosting, tracking, forms, or outreach.

### Added — 2026-10-08

- Fixture-only Research & Qualification Agent with source-backed facts, inferences, uncertainties, identity checks, website classifications, and recoverable job output.
- Research job schema, research configuration defaults, and documented qualification rules.

### Added — 2026-10-08

- Fixture-only, source-modular Scout Agent with a safe CLI and standard-library tests.
- Deterministic normalization, conservative deduplication, source/field provenance, and explainable initial qualification.
- Scout job-result schema and Scout settings defaults.

### Not added — 2026-10-08

- No live discovery source, browser/search integration, scraping, real business data, external service, dependency, deployment, or outreach capability.

### Added — 2026-10-02

- Phase 1 provider-neutral project foundation.
- Multi-agent operating guidance and handoff ledger.
- Future settings template and initial lead JSON Schema.
- Architecture boundaries, recoverable job lifecycle, and 13-phase roadmap.

### Not added

- No dependencies, API clients, database, AI-provider binding, n8n workflow, deployment, business data, or outreach capability.
