# Orchestration

`JobService` is the application-owned job ledger. It persists jobs under `TrackingStore.state["jobs"]`; n8n is optional and never owns lead, project, build, validation, deployment, or outreach state.

Create every job with a caller-provided `job_id`. Repeating the same request returns the same record; a different job with the same ID is rejected. Jobs support `PENDING`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `NEEDS_REVIEW`, and `BLOCKED` states. A job records references, not copies of domain data.

`JobWorker.execute(job, context)` is provider-neutral. Workers must use the existing Scout, Research, Builder, Validation, Deployment, Tracking, and Outreach services to make domain changes, then return `WorkerResult`. `JobService` does not make external calls itself. This keeps a human-operated CLI, a future Codex/Claude worker, and n8n equally viable.

Default retry limit is three attempts. Only a worker result marked `retryable=True` is re-queued. `recover_stale()` moves jobs with an expired heartbeat to `NEEDS_REVIEW`; it never silently repeats work. `dry_run` is recorded in every job, but workers must honor it before any external action.

The optional standard-library API in `orchestration.api` is fail-closed: it requires `AI_WEBSITE_AGENT_API_TOKEN` in the local environment and a matching `Authorization: Bearer` header. It is not started automatically.
