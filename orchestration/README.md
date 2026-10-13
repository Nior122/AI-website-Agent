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
- `POST /projects/:id/review-package` — exports and verifies a human-review package (`index.html`, `review-package.json`, `REVIEW_README.md`) for a `READY_FOR_REVIEW` project, leaving `review_status` at `"PENDING"`.
- `GET /projects/:id/review-package?export_dir=...` — verifies an exported review package against `TrackingStore`.
