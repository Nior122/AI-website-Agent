# n8n Workflow Integration

These are importable workflow definitions for self-hosted n8n (`n8n-nodes-base.manualTrigger`, `n8n-nodes-base.set`, `n8n-nodes-base.httpRequest`, and `n8n-nodes-base.if`). They make authenticated HTTP calls to the local Python orchestration API (`orchestration.api`); they do not contain secrets, store business state outside `TrackingStore`, deploy websites, or send outreach messages.

## Starting the Local Application API

Set `AI_WEBSITE_AGENT_API_URL` and `AI_WEBSITE_AGENT_API_TOKEN` in n8n's environment/credential store, and start the Python orchestration API on loopback (`127.0.0.1:8787` by default):

```bash
export AI_WEBSITE_AGENT_API_TOKEN="local-secret-not-committed"
export AI_WEBSITE_AGENT_API_URL="http://127.0.0.1:8787"
python3 -m orchestration.cli serve --host 127.0.0.1 --port 8787
```

The API binds only to `127.0.0.1`/`localhost` (refusing public `0.0.0.0` binds) and fails closed if `AI_WEBSITE_AGENT_API_TOKEN` is unset. Every request must supply `Authorization: Bearer <token>`.

## Exported Workflows (`n8n/workflows/`)

- `01-create-job.json` *(preserved starter workflow)* — creates an idempotent, dry-run `SCOUT` job from a manual trigger (`POST /jobs`).
- `02-inspect-job.json` *(preserved starter workflow)* — reads one job record (`GET /jobs/:id`) so an operator can inspect status, attempts, errors, and next work.
- `03-core-lead-to-review-package-pipeline.json` — executes the complete core pipeline through human review readiness:
  `TRIGGER -> BUSINESS DISCOVERY (SCOUT) -> DEDUPLICATION -> RESEARCH -> QUALIFICATION -> LEAD CREATION OR UPDATE -> PROJECT CREATION OR UPDATE -> WEBSITE BUILD (BUILD) -> AUTOMATED VALIDATION (VALIDATE) -> REVIEW-PACKAGE GENERATION & VERIFICATION -> HUMAN REVIEW PENDING`
  with explicit conditional branches (`n8n-nodes-base.if`) for:
  - Duplicate business (`Outcome - Duplicate Business`)
  - Insufficient research evidence (`Outcome - Insufficient Research Evidence`, `outcome = INSUFFICIENT_RESEARCH_EVIDENCE`)
  - Unqualified opportunity (`Outcome - Unqualified Opportunity`, `outcome = UNQUALIFIED_OPPORTUNITY`)
  - Research requiring additional work (`Outcome - Research Requires Additional Work`, `outcome = RESEARCH_REQUIRES_ADDITIONAL_WORK`)
  - Discovery / Research failure (`Outcome - Discovery Failed`, `Outcome - Research Failed`)
  - Website build failure (`Outcome - Build Failure`)
  - Automated validation failure (`Outcome - Validation Failure`)
  - Successful build ready for human review (`Outcome - Successful Build Ready For Human Review`, `review_status = PENDING`)
- `04-recover-and-retry-jobs.json` — recovers stale `RUNNING` jobs (`POST /jobs/recover-stale`), inspects a target job (`GET /jobs/:id`), and safely retries/runs eligible `FAILED`, `QUEUED`, or `NEEDS_REVIEW` jobs (`POST /jobs/:id/retry`).

## Local Verification When n8n Binary Is Not Installed

When a live self-hosted n8n runtime (`n8n` CLI or Docker container) is not running in the local environment, `orchestration/n8n_runner.py` (`N8nWorkflowRunner`) validates the exported n8n workflow JSON schema and executes the exact node graph against the live loopback `orchestration.api` HTTP server:

```bash
python3 -m unittest discover -s tests -p "test_n8n_integration.py" -v
```
