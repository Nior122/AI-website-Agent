# Orchestration

`JobService` is the application-owned job ledger. It persists jobs under `TrackingStore.state["jobs"]`; n8n is an orchestration caller and never owns lead, project, build, validation, deployment, or outreach state.

## Core Principles

- **Idempotent Job Submission**: Create every job with a caller-provided `job_id` or `idempotency_key`. Repeating the same request returns the existing job record even after worker execution has populated `lead_id`, `project_id`, `build_id`, or `validation_id`; a conflicting payload with the same ID is rejected (`409 Conflict`).
- **Completed-Job Protection**: Re-running an already `COMPLETED` job returns the completed job without repeating work or creating duplicate leads, projects, or builds.
- **Controlled Retries & Stale Recovery**: Default retry limit is three attempts. `retry(job_id)` re-queues a `FAILED` or `NEEDS_REVIEW` job when `attempts < max_attempts` and allows safe input overrides (e.g., corrected fixture/evidence paths). `recover_stale()` moves stale `RUNNING` jobs to `NEEDS_REVIEW` without corrupting project state.
- **Strict Scope & Safety**: The HTTP API only permits local pipeline stages (`SCOUT`, `RESEARCH`, `QUALIFY`, `BUILD`, `VALIDATE`) for job creation and `next` chaining. A `DEPLOY` job can never be created or chained through the API, and outreach remains gated by explicit human operator review via `python3 -m tracking.cli review` (or `POST /projects/:id/review`, which records a decision and never deploys). `GET /projects/:id/deployment-authorization` exposes the server-side gate verdict. Even when a `DEPLOY` job is executed through `POST /jobs/:id/run` or `POST /jobs/:id/retry`, `JobService.prerequisite_failure()` blocks an unapproved real publish (`BLOCKED`, `DEPLOY requires explicit human approval (APPROVAL_MISSING)`) and the provider re-verifies the approval at the actual publish call.

## Loopback HTTP API (`orchestration.api`)

Start the server on loopback (`127.0.0.1:8787`):

```bash
export AI_WEBSITE_AGENT_API_TOKEN="local-secret-not-committed"
python3 -m orchestration.cli serve --host 127.0.0.1 --port 8787
```

Supported authenticated endpoints (`Authorization: Bearer <token>`):

- `GET /health` — returns health status, job status counts, project counts, and worker availability.
- `GET /jobs` — lists jobs (optional `?status=` and `?job_type=` query filters).
- `GET /jobs/:id` — retrieves a single job record including `status`, `attempts`, `output`, `error`, and `next_job_types`.
- `GET /jobs/:id/history` — the job record plus every recorded execution attempt for it (`attempt_number`, worker, start/completion, recorded duration, outcome, sanitized error type/message, `error_code`, and a `trace` of lead/project/build/validation/deployment ids) together with `retry_eligibility`: remaining attempt budget, whether `reset_attempts` is required, and the next action.
- `POST /jobs` — submits an idempotent job (`run: true` optional to execute immediately via `LocalPipelineWorker`).
- `POST /jobs/:id/run` — executes a `QUEUED` job via `LocalPipelineWorker` (returns existing record if already `COMPLETED`).
- `POST /jobs/:id/next` — creates (and optionally runs with `run: true`) the next stage jobs (`next_job_types`) from a `COMPLETED` parent job.
- `POST /jobs/:id/retry` — re-queues a `FAILED` or `NEEDS_REVIEW` job within `max_attempts` (and optionally executes immediately with `run: true`).
- `POST /jobs/recover-stale` — moves stale `RUNNING` jobs older than `older_than_seconds` to `NEEDS_REVIEW`. Age is measured from the newest timestamp the claim actually recorded (`heartbeat_at`, else `started_at`, else `updated_at`), so a worker that died before writing its first heartbeat is recoverable too and is coded `HEARTBEAT_UNDATED` rather than `HEARTBEAT_EXPIRED`. A `RUNNING` job with no timestamp at all is left in place - there is no honest threshold to apply - and is surfaced by `GET /attention` as `undated_running` instead of silently never resolving.
- `POST /leads/deduplicate` — checks candidate business identity against `TrackingStore.find_duplicate()`.
- `GET /projects/:id` — returns `TrackingStore.snapshot(project_id)`.
- `POST /projects/:id/review` — records one explicit human review decision (`decision`, `notes`, `reviewer`, and `confirm=<project_id>:<assigned_build_id>:<DECISION>`). `decision` must be one of `APPROVED_FOR_PREVIEW`, `APPROVED`, `REJECTED`, `CHANGES_REQUESTED`; `notes`, `reviewer`, and `confirm` must be strings within the recorded bounds (4000 and 120 characters). A stale or misrouted confirmation is refused with `409 REVIEW_CONFIRMATION_MISMATCH` before anything is written, the reviewer string is recorded verbatim with `identity_verified: false`, and the response carries the resulting `deployment_authorization`. Supplying `identity_verified: true` is refused with `400 IDENTITY_ASSERTION_REFUSED` (see *HTTP trust boundary* below). Recording a decision never publishes anything.
- `GET /projects/:id/deployment-authorization` — read-only view of the server-side publish gate for the assigned build (or `?build_id=` to assert one): `authorized`, a machine-readable `code` (`APPROVAL_MISSING`, `APPROVED_FOR_DEPLOYMENT`, `ALREADY_DEPLOYED` with `skip_publish: true`, `REVIEW_REJECTED`, `REVIEW_CHANGES_REQUESTED`, `VALIDATION_NOT_PASSED`, `PUBLISH_STATE_UNCERTAIN`, `REAL_ISOLATED_TARGET_REQUIRED`, …) and an actionable `reason`. It never deploys and never mutates state.
- `GET /attention` — the operator backlog from `operator_attention()`: stale-running jobs, blocked/failed/exhausted jobs, projects awaiting a human decision or sent back by review, and deployment outcomes that need manual resolution - each with a stable code, the sanitized recorded message, and the safe next action.
- `GET /deployment-status` — recorded publish state per project (`VERIFIED_RECORDED`, `UNCERTAIN`, `FAILED`, `RECORDED_UNCONFIRMED`, `SUPERSEDED`, `NOT_ATTEMPTED`) plus deployment *configuration* (CLI on `PATH`, project link, isolated target). It reads the ledger and configuration only and always reports `provider_contacted: false` / `live_url_checked: false`, so a configuration check cannot be mistaken for verification of a live site; `?project_id=` narrows the attention list.
- `POST /jobs/:id/retry` refuses an ineligible retry with `409`, `error_code: RETRY_NOT_ELIGIBLE`, and the recorded eligibility explanation rather than a generic error.

- `GET /projects/:id/artifact` — retrieves (and re-materializes from durable storage if local temporary files were removed) the SHA-256-verified `index.html`, `html_sha256`, `html_bytes`, and `review_package` for a project's assigned build.
- `POST /projects/:id/review-package` — exports and verifies a human-review package (`index.html`, `review-package.json`, `REVIEW_README.md`) for a `READY_FOR_REVIEW` project, leaving `review_status` at `"PENDING"`. `export_dir` is optional and must name a directory *inside* the handler's configured export root; anything else is `400 EXPORT_PATH_NOT_ALLOWED`.
- `GET /projects/:id/review-package?export_dir=...` — verifies an exported review package against `TrackingStore`, under the same export-root confinement (verification reads files by path, so an unrestricted name would let a request inspect another project's evidence).

### SCOUT job inputs

A `SCOUT` job discovers and qualifies only - it never builds, deploys, or contacts anyone, and a real-data candidate is written as a `DISCOVERED` lead with public contact fields withheld from the lead book.

| Input | Effect |
| --- | --- |
| absent or `source: "fixture"` | the offline default; `source_file` is then required (`SCOUT_INPUT_INVALID` if missing) |
| `source: "openstreetmap_overpass"` | the one approved live pilot, capped at five candidates in Lagos for the restaurant niche; any other name is `SCOUT_SOURCE_UNKNOWN` |
| `approve_live_lookups: true` | required with a live source, otherwise `LIVE_DISCOVERY_NOT_APPROVED` and no request is made |
| `AI_WEBSITE_AGENT_ALLOW_LIVE_DISCOVERY=1` (environment of the worker, not a job input) | the deployment's own permission for outbound discovery, otherwise `LIVE_DISCOVERY_NOT_ENABLED`; a job created over HTTP or n8n must not be the only thing that consented |
| `country` / `city` / `niche` / `lead_limit` | target for qualification; anything outside the approved pilot scope is refused by the adapter itself |
| `endpoint`, `fallback_endpoints`, `timeout_seconds`, `max_requests` (1-5) | the polite request budget for one run |
| `create_leads: false` | inspect candidates without touching `TrackingStore` (defaults to `true`) |

Refusals are terminal for the attempt (`retryable: false`) and carry a runnable remedy through `NEXT_ACTIONS`, because "wait for a retry" is not an answer to a missing approval.

Unauthenticated liveness probe (for Render load balancer health checks):

- `GET /healthz` — returns `200 OK` with `{"status": "ok"}` and never exposes operational state, paths, or credentials. It is intentionally *liveness only*: it does not read the database, so it keeps answering while `GET /readiness` correctly reports `NOT_READY` because the source of truth is unreachable. `200 /healthz` is therefore never a statement that work can proceed.

### HTTP trust boundary (Phase 40)

The API is a single-trust-zone automation surface: whoever holds the shared token may read and advance
workflow state, and nothing else. Everything below keeps that one credential meaningful without
pretending it is something it is not.

- **Credential check.** `_authorized()` compares the exact `Bearer <token>` form with
  `hmac.compare_digest`, requires exactly one `Authorization` header, and fails closed when no token is
  configured (`serve()` refuses to start without one). Missing, wrong-scheme, malformed, wrong, and
  duplicated credentials all answer the identical `401 {"error": "Unauthorized"}` plus
  `WWW-Authenticate: Bearer realm="ai-website-agent"`, so a probe learns nothing about which part
  failed. Only `GET /healthz` is public, and it returns exactly `{"status": "ok"}`.
- **No credential material in answers or logs.** The refusal body is fixed text; the `Authorization`
  value is never echoed, and the failure is logged as `{"operation":"http.auth","code":"AUTH_MISSING|
  AUTH_FAILED","detail":"GET /jobs"}` with nothing else. `log_message` stays disabled so request headers
  never reach the access log.
- **Bind policy.** A token shorter than 32 characters or equal to a documented placeholder is refused for
  any bind reachable outside the machine (`0.0.0.0` or production mode): the shared-token design is only
  safe while the token is unguessable. Local loopback development is unaffected.
- **Request limits.** Bodies are capped at `MAX_REQUEST_BYTES` (1 MiB) and the cap is applied before the
  socket is read (`413 REQUEST_BODY_TOO_LARGE`). `Transfer-Encoding: chunked` is refused rather than
  silently buffered, a non-numeric `Content-Length`, a truncated body, non-UTF-8 bytes, and a JSON value
  that is not an object are `400 INVALID_REQUEST`.
- **Identifier validation.** Every `/jobs/:id`, `/leads/:id`, and `/projects/:id` segment, plus body
  `job_id`/`lead_id`/`project_id`/`build_id`/`parent_job_id`/`idempotency_key`/`reviewer_id`, is checked
  by `tracking.store.validate_record_id` before it reaches state, SQL parameters, or a derived path.
  A malformed identifier is `400 INVALID_REQUEST`; an unknown but well-formed one is `404
  RECORD_NOT_FOUND`.
- **Review bounds and identity.** `notes` (4000) and `reviewer` (120) are bounded strings; an object or
  array is refused instead of being stringified into persisted evidence. `identity_verified` cannot be
  supplied at all: the API authenticates an automation token, not a person, and deployment
  authorization is bound to that flag, so the request is refused and the recorded decision always says
  `identity_verified: false` with `identity_source: "orchestration-api"`. `TrackingStore` re-checks this
  with `clamp_reviewer_identity_claim()` — a claim arriving from any source outside
  `VERIFIED_IDENTITY_SOURCES` (currently empty, because no verifying mechanism exists here) is stored as
  unverified and annotated `identity_claim_refused`, so neither the durable review nor the build-local
  approval capability can carry a self-granted human assurance.
- **Error text.** `404`/`400`/`503`/`500` bodies carry a stable `error_code` plus
  `sanitize_diagnostic()`-bounded, `redact_local_paths()`-filtered text: absolute POSIX, Windows drive,
  UNC, and `file:` paths become `[LOCAL-PATH-REDACTED]` while URLs and repository-relative paths stay
  readable, and unexpected failures are additionally logged as one structured `UNHANDLED_FAULT` event
  rather than a stack trace. Unsupported verbs answer `405` JSON with `Allow: GET, POST` instead of the
  standard library's HTML error page.
- **Deliberate asymmetry.** Review-package exports over HTTP are confined to the handler's
  `export_root` (`preview-export` by default), while `tracking.cli review-package --export-dir` still
  honours any path the operator names: a local operator already has that filesystem authority, and a
  network caller does not. That boundary is the trust decision, not an inconsistency to reconcile.
- **Known limitation (documented, not hidden).** One shared bearer token cannot distinguish reviewers,
  rotate per client, or authenticate a human. Grants are therefore not per-user: any holder may record a
  review *decision*, but no holder may represent it as identity-verified, and publishing still requires
  the build-local approval capability that only a review recorded through a verifying channel produces.
  Adding per-reviewer identity is a deliberate non-goal here (it would need an authentication provider
  and a schema change) and is listed in `TASKS.md`.

## Cloud Deployment Configuration (Render + Supabase PostgreSQL)

### 1. Applying the Versioned PostgreSQL Migration

The normalized relational schema (`leads`, `research_evidence`, `qualifications`, `projects`, `builds`, `validations`, `reviews`, `deployments`, `outreach_packages`, `jobs`, `job_executions`, `workers`, `project_locks`, `system_kv`, and `build_artifacts`) is defined in `migrations/0001_supabase_postgres_schema.sql`.

Apply and verify the migration explicitly before starting or updating the production service (do **not** run database migrations automatically inside `render.yaml`'s `buildCommand` on every container build). `preflight-db` runs first: it opens one connection, issues `SELECT`s, writes nothing, and names the target - the database name, host, port, and the Supabase project reference in the hostname - because "the migration ran" and "the migration ran on the project you meant" are different claims. It also reports `row_counts`, so a database that already holds operational records is visible before DDL lands in it, and `schema_migrations` checksum conflicts come back as `blocked` (reconcile by hand) rather than `needs_migration` (run the command).:

```bash
# Option A: Controlled Python CLI migration runner & checksum verification
export DATABASE_URL="postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres?sslmode=require"
python3 -m tracking.cli preflight-db --expect-project-ref <project-ref>   # read-only: identity, reachability, migration state
python3 -m tracking.cli migrate-db --confirm-database postgres            # DDL on a remote DB requires naming it back
python3 -m tracking.cli migrate-db --verify-only                          # no DDL, no confirmation needed

# Option B: psql CLI + checksum verification
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_supabase_postgres_schema.sql
python3 -m tracking.cli migrate-db --verify-only
```

### 1b. One Readiness Command

`python3 -m orchestration.cli preflight [--project-id <pid>]` answers the four questions an operator
otherwise assembles from four places, without binding a socket, contacting a provider, or running DDL:

- `api_liveness` - the bind `serve` would use, whether a token exists, and whether it is long enough for
  that bind. It reproduces `serve`'s refusal so a weak credential is found here rather than at startup, and
  it never copies the value into the report.
- `storage_readiness` - `verify_storage()`'s answer, marked `verified: true` only when a database was
  actually probed, plus the explicit statement that local JSON is prohibited in production mode.
- `workflow_execution_readiness` - the recorded job/worker/project state, the combined human-actionable
  backlog with its codes, and the job types reachable over HTTP (which never includes `DEPLOY`).
- `automation_n8n` - each exported workflow's importability, the variables it reads, which of those are
  unset, and which files still hold `REPLACE_WITH_*` starter placeholders (`requests_sent: false`).
- `deployment_provider_configuration` - CLI availability, linked project, isolation of the target, and the
  recorded outcomes, all marked configuration-only because nothing was executed.

`assurance` names the four states explicitly (`code_readiness`, `configuration_readiness`, `connectivity`,
`preview_deployment`), and `actions_not_performed` records that no network call, provider command, DDL, or
state mutation happened. With `--project-id` it also quotes the durable deployment gate's own answer
(`authorized`, `code`, `reason`) for that project; reading the gate does not change it. A store that cannot
even be constructed (production mode, no `DATABASE_URL`) is reported as a blocker in the same document
instead of a traceback. `python3 -m orchestration.cli check-n8n` is the same automation section on its own,
for when only the workflow files need checking. Full procedure: `docs/INTEGRATION-SETUP.md`.

### 2. Environment Variables

| Variable | Local Default | Production (Render) | Purpose |
|---|---|---|---|
| `AI_WEBSITE_AGENT_API_TOKEN` | *(required)* | *(required secret)* | Bearer token required on all API endpoints except `/healthz`. |
| `DATABASE_URL` | *(unset — uses local JSON)* | `postgresql://...` *(required secret)* | Supabase PostgreSQL connection string (`sslmode=require`). |
| `AI_WEBSITE_AGENT_ENV` | `development` | `production` | Enables strict production safeguards (forbids fallback to local JSON or SQLite if DB fails). |
| `AI_WEBSITE_AGENT_STORAGE_BACKEND` | `json` | `postgres` | Forces relational PostgreSQL persistence. |
| `PORT` | `8787` | *(injected by Render)* | TCP port for `orchestration.api`. |
| `AI_WEBSITE_AGENT_BIND_HOST` | `127.0.0.1` | `0.0.0.0` | Network interface to bind (`AI_WEBSITE_AGENT_HOST` is also accepted). Non-loopback requires explicit opt-in below. |
| `AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK` | `false` | `true` | Explicit opt-in permitting `--host 0.0.0.0` in Render container environment. |

### 3. Production Build & Start Commands (Render)

- **Build Command**: `pip install -r requirements.txt` (does not execute database DDL during image builds)
- **Start Command**:
```bash
python3 -m orchestration.cli serve --host 0.0.0.0 --allow-non-loopback
```

What the blueprint declares is checked against the code it starts, so the two cannot drift apart silently: every flag in `startCommand` exists in `orchestration/cli.py`, `healthCheckPath: /healthz` is the one route answered before any credential check, every `envVars` key is read somewhere in `orchestration/`, `tracking/`, or `deployment/`, secrets stay `sync: false` (never committed as values), `buildCommand` installs `requirements.txt` (which carries the PostgreSQL driver) and runs no DDL, and `autoDeploy` stays `false`. See `tests/test_phase41_integration_and_recovery.py` (`DeploymentTopologyTests`). Those are the assertions this environment can honestly make: no Render account, service, or live Supabase/PostgreSQL instance was contacted, so the blueprint has not been executed here. Starting it remains a deliberate operator action, including applying `python3 -m tracking.cli preflight-db` and then `migrate-db --confirm-database <database>` once, and supplying `DATABASE_URL` plus a long `AI_WEBSITE_AGENT_API_TOKEN` in the Render dashboard. The Render filesystem is ephemeral and `data/tracking/` is not in Git, so PostgreSQL is what makes a deployed service able to start at all; with the backend configured the store neither needs nor creates that directory (asserted in `tests/test_phase42_integration_setup_and_preflight.py`).

In production mode (`AI_WEBSITE_AGENT_ENV=production` or `AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres`), `TrackingStore` never falls back to temporary local JSON storage or SQLite if `DATABASE_URL` is missing or unreachable; it fails closed and reports `DatabasePersistenceError`. Durable `build_artifacts` rows persist and re-materialize `index.html`, `build.json`, `review-package.json`, `REVIEW_README.md`, and any relative local asset files in the build directory (verified by SHA-256 and guarded against path traversal) across container restarts.

### 4. n8n Hosting & Persistence Compatibility (Low-Budget Constraint)

- **Role of n8n**: n8n executes the external orchestration workflows (`n8n/workflows/01-create-job.json`, `02-inspect-job.json`, `03-core-lead-to-review-package-pipeline.json`, `04-recover-and-retry-jobs.json`) against the HTTP API (`AI_WEBSITE_AGENT_API_URL` + `AI_WEBSITE_AGENT_API_TOKEN`). All authoritative domain state (`leads`, `projects`, `builds`, `build_artifacts`, `validations`, `deployments`, `reviews`, `jobs`) is stored by the Python service in Supabase PostgreSQL.
- **Why ephemeral n8n storage is unsafe**: Running n8n on a free/ephemeral container without persistent backing loses n8n's internal database (imported workflows, credentials, `N8N_ENCRYPTION_KEY`, and execution history) whenever the container restarts or sleeps.
- **Compatible low-budget arrangements**:
  1. **Self-hosted n8n on a persistent local machine / home server / low-cost VPS volume** calling the Render HTTP API over HTTPS.
  2. **Containerized n8n backed by PostgreSQL (`DB_TYPE=postgresdb`)** using a dedicated schema (e.g., `DB_POSTGRESDB_SCHEMA=n8n`) in the same Supabase PostgreSQL instance plus a fixed `N8N_ENCRYPTION_KEY` secret so n8n workflows and credentials survive container restarts.
  3. **Operator-triggered CLI runner (`python3 -m orchestration.cli run-workflow ...`)** for zero-cost deterministic execution without running an always-on n8n instance.

## Operational Monitoring and Controls

- `python3 -m orchestration.cli health` - the full report: liveness, application, source of truth (with `verification: verified|configuration_only`), workflow execution, workers, deployment outcomes and provider configuration, and configuration presence. Readiness separates `blocking` (storage or persistence cannot be trusted -> `NOT_READY`, exit `1`) from `degradations` (no capable worker, missing local build artifacts, an operator backlog - still serving).
- `python3 -m orchestration.cli attention [--brief]` - jobs and projects a human must clear, with ages derived only from recorded timestamps. Exit `1` when anything needs attention, so it is safe to schedule from cron.
- `python3 -m orchestration.cli inspect-job --job-id <id> [--history]` - one job, its related lead/project/build records, and with `--history` every execution attempt plus retry eligibility. Read-only; exit `1` unless the job completed.
- `python3 -m orchestration.cli list-jobs [--status FAILED] [--attention-only] [--brief]` - filterable listing; `--brief` projects the operator fields (status, code, attempts, recorded retries, age, next action) instead of dumping raw state.
- `python3 -m orchestration.cli deployment-status` - recorded publish outcome per project plus provider configuration. Exit `1` only when a project outcome needs resolution, never merely because no provider link exists.
- `python3 -m orchestration.cli check-alerts [--notify]` - the critical subset of the same attention data (uncertain publish, expired or stale leases, exhausted retries, a store that cannot authorize). `--notify` emits one structured `CRITICAL` log event per alert through `LogAlertSink`. Exit `1` when any critical condition exists. No external channel is contacted and no lead or customer is ever messaged: `WebhookAlertSink` is the documented integration point and delivers only through a `transport` callable the operator injects, so no credential lives in this repository.

There is intentionally no reset, purge, or state-erasing command. `recover-stale` and `recover-workspace` reconcile recorded state; a retry re-evaluates every prerequisite; and no monitoring, alert, or read endpoint can grant an approval or bypass the deployment authorization gate - they read state only.

Each executed attempt (including the service, CLI, and API paths, not only leased worker claims) is written to the pre-existing `executions` ledger with attempt number, worker, start/completion, recorded duration, outcome, sanitized error type and message, and a stable `error_code`; a blocked job records no attempt because nothing started. Failures carry both a machine-readable code and a sanitized message plus `next_action`, and the previous code survives a retry as `last_error_code`. A `DEPLOY` job that is blocked by the checkout rather than by the review gate carries its own code
(`PREVIEW_PROJECT_NOT_LINKED`, `PREVIEW_PROJECT_LINK_INVALID`, `PROVIDER_CLI_MISSING`) instead of the generic
`WORKER_FAILED`, and `NEXT_ACTIONS` gives each one a runnable remedy - a configuration blocker and a human
decision have to be told apart, or the operator is sent to approve something that is already approved.

Structured events are emitted with `orchestration.observability.log_event()`: one JSON object per line to stderr (filtered by `AI_WEBSITE_AGENT_LOG_LEVEL`, default `WARNING`) and appended to `AI_WEBSITE_AGENT_LOG_DIR/operations.jsonl` when that directory is configured. Every line carries `timestamp`, `severity`, `operation`, a stable `code`, the identifiers needed to trace it (`job_id`, `lead_id`, `project_id`, `build_id`, `validation_id`, `deployment_id`, `execution_id`, `worker_id`, `attempt`), and a sanitized `detail`. `redact()` removes bearer/authorization values, named secrets, `sk-` keys, JWTs, and complete connection-string userinfo before anything is persisted or returned, and messages are length-bounded. Duplicate alert suppression is in-process by design: a long-lived caller should pass its own `seen` set, and no second copy of workflow state is kept for monitoring.
