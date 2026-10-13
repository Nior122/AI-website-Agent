# Orchestration

`JobService` is the application-owned job ledger. It persists jobs under `TrackingStore.state["jobs"]`; n8n is an orchestration caller and never owns lead, project, build, validation, deployment, or outreach state.

## Core Principles

- **Idempotent Job Submission**: Create every job with a caller-provided `job_id` or `idempotency_key`. Repeating the same request returns the existing job record even after worker execution has populated `lead_id`, `project_id`, `build_id`, or `validation_id`; a conflicting payload with the same ID is rejected (`409 Conflict`).
- **Completed-Job Protection**: Re-running an already `COMPLETED` job returns the completed job without repeating work or creating duplicate leads, projects, or builds.
- **Controlled Retries & Stale Recovery**: Default retry limit is three attempts. `retry(job_id)` re-queues a `FAILED` or `NEEDS_REVIEW` job when `attempts < max_attempts` and allows safe input overrides (e.g., corrected fixture/evidence paths). `recover_stale()` moves stale `RUNNING` jobs to `NEEDS_REVIEW` without corrupting project state.
- **Strict Scope & Safety**: The HTTP API only permits local pipeline stages (`SCOUT`, `RESEARCH`, `QUALIFY`, `BUILD`, `VALIDATE`) by default. Preview deployment (`DEPLOY_PREVIEW`) and outreach preparation (`PREPARE_OUTREACH`) remain gated by explicit human operator review via `python3 -m tracking.cli review`.

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
- `POST /jobs` — submits an idempotent job (`run: true` optional to execute immediately via `LocalPipelineWorker`).
- `POST /jobs/:id/run` — executes a `QUEUED` job via `LocalPipelineWorker` (returns existing record if already `COMPLETED`).
- `POST /jobs/:id/next` — creates (and optionally runs with `run: true`) the next stage jobs (`next_job_types`) from a `COMPLETED` parent job.
- `POST /jobs/:id/retry` — re-queues a `FAILED` or `NEEDS_REVIEW` job within `max_attempts` (and optionally executes immediately with `run: true`).
- `POST /jobs/recover-stale` — moves stale `RUNNING` jobs older than `older_than_seconds` to `NEEDS_REVIEW`.
- `POST /leads/deduplicate` — checks candidate business identity against `TrackingStore.find_duplicate()`.
- `GET /projects/:id` — returns `TrackingStore.snapshot(project_id)`.
- `GET /projects/:id/artifact` — retrieves (and re-materializes from durable storage if local temporary files were removed) the SHA-256-verified `index.html`, `html_sha256`, `html_bytes`, and `review_package` for a project's assigned build.
- `POST /projects/:id/review-package` — exports and verifies a human-review package (`index.html`, `review-package.json`, `REVIEW_README.md`) for a `READY_FOR_REVIEW` project, leaving `review_status` at `"PENDING"`.
- `GET /projects/:id/review-package?export_dir=...` — verifies an exported review package against `TrackingStore`.

Unauthenticated liveness probe (for Render load balancer health checks):

- `GET /healthz` — returns `200 OK` with `{"status": "ok"}` and never exposes operational state, paths, or credentials.

## Cloud Deployment Configuration (Render + Supabase PostgreSQL)

### 1. Applying the Versioned PostgreSQL Migration

The normalized relational schema (`leads`, `research_evidence`, `qualifications`, `projects`, `builds`, `validations`, `reviews`, `deployments`, `outreach_packages`, `jobs`, `job_executions`, `workers`, `project_locks`, `system_kv`, and `build_artifacts`) is defined in `migrations/0001_supabase_postgres_schema.sql`.

Apply and verify the migration explicitly before starting or updating the production service (do **not** run database migrations automatically inside `render.yaml`'s `buildCommand` on every container build):

```bash
# Option A: Controlled Python CLI migration runner & checksum verification
export DATABASE_URL="postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres?sslmode=require"
python3 -m tracking.cli migrate-db
python3 -m tracking.cli migrate-db --verify-only

# Option B: psql CLI + checksum verification
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_supabase_postgres_schema.sql
python3 -m tracking.cli migrate-db --verify-only
```

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

In production mode (`AI_WEBSITE_AGENT_ENV=production` or `AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres`), `TrackingStore` never falls back to temporary local JSON storage or SQLite if `DATABASE_URL` is missing or unreachable; it fails closed and reports `DatabasePersistenceError`. Durable `build_artifacts` rows persist and re-materialize `index.html`, `build.json`, `review-package.json`, `REVIEW_README.md`, and any relative local asset files in the build directory (verified by SHA-256 and guarded against path traversal) across container restarts.

### 4. n8n Hosting & Persistence Compatibility (Low-Budget Constraint)

- **Role of n8n**: n8n executes the external orchestration workflows (`n8n/workflows/01_lead_to_review_Readiness_workflow.json` and `02_recovery_and_audit_workflow.json`) against the HTTP API (`AI_WEBSITE_AGENT_API_URL` + `AI_WEBSITE_AGENT_API_TOKEN`). All authoritative domain state (`leads`, `projects`, `builds`, `build_artifacts`, `validations`, `deployments`, `reviews`, `jobs`) is stored by the Python service in Supabase PostgreSQL.
- **Why ephemeral n8n storage is unsafe**: Running n8n on a free/ephemeral container without persistent backing loses n8n's internal database (imported workflows, credentials, `N8N_ENCRYPTION_KEY`, and execution history) whenever the container restarts or sleeps.
- **Compatible low-budget arrangements**:
  1. **Self-hosted n8n on a persistent local machine / home server / low-cost VPS volume** calling the Render HTTP API over HTTPS.
  2. **Containerized n8n backed by PostgreSQL (`DB_TYPE=postgresdb`)** using a dedicated schema (e.g., `DB_POSTGRESDB_SCHEMA=n8n`) in the same Supabase PostgreSQL instance plus a fixed `N8N_ENCRYPTION_KEY` secret so n8n workflows and credentials survive container restarts.
  3. **Operator-triggered CLI runner (`python3 -m orchestration.cli run-workflow ...`)** for zero-cost deterministic execution without running an always-on n8n instance.
