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

## Engagement ledger reads (and why there are no writes)

A scheduled workflow may call `GET /engagement/outstanding`, `GET /engagement/pipeline?stage=`, or
`GET /engagement/eligibility/:lead_id?outreach_id=` to decide *whether to notify a human*, and `python3 -m tracking.cli
outreach-eligibility` from a local shell task. No workflow example writes to the ledger, and none can: routes that
would record `OUTREACH_SENT`, a presented proposal, or a `WON` outcome do not exist over HTTP, because a graph
able to mark its own outreach successful would replace the human approval step this system exists to keep.
Opt-outs and the per-channel contact budget are enforced in the application rather than in the graph, so editing
a workflow cannot widen them.

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

When a live self-hosted n8n runtime (`n8n` CLI or Docker container) is not running in the local environment, `orchestration/n8n_runner.py` (`N8nWorkflowRunner`) validates the exported n8n workflow JSON schema and executes the exact node graph against the live loopback `orchestration.api` HTTP server. Two rules govern that verifier:

- **Loopback only.** Every HTTP node carries the API bearer token in a header, so a node addressed to a host outside this machine raises `N8nWorkflowTargetRefused` *before the request is built* - a copied or hand-edited export cannot walk the credential to another host. `allow_remote=True` is the explicit opt-out for an operator who means it.
- **Faults are data.** A refused connection, a timeout, a non-2xx answer, and a body that is not a JSON object each become a structured output on the failing node (`error`, `error_kind`, `error_code`, `http_status`, `status: "FAILED"`) instead of a traceback, so the workflow's own IF branches reach their documented outcome node. The result also carries `execution_status` (`COMPLETED`/`FAILED`) and a collected `errors` list, and the validation step reports a missing or malformed workflow file as an actionable message. None of this retries, schedules, or activates anything.

### Checking the workflow configuration before importing

```bash
python3 -m orchestration.cli check-n8n                      # all four exports
python3 -m orchestration.cli check-n8n --workflow path/to.json
python3 -m orchestration.cli preflight                      # the same section, with more context
```

`check-n8n` validates each file and reports the environment variables it reads, which of those this
environment does not define, and which files still contain `REPLACE_WITH_*` starter placeholders. It sends
no requests and executes no node. Two of its answers matter more than they look:

- **An unset `AI_WEBSITE_AGENT_API_URL` is not a missing value, it is a wrong request.** The expression
  renders to an empty string, so `POST {{ $env.AI_WEBSITE_AGENT_API_URL }}/jobs` becomes `POST /jobs` and
  the failure reads like an API outage or a 404 on the wrong host. Set it to the loopback URL of the API
  you mean (`http://127.0.0.1:8787`).
- **`02-inspect-job.json` needs a manual edit in the n8n UI.** Its URL carries `REPLACE_WITH_JOB_ID`. The
  repository copies are kept runnable as demonstrations - `tests/test_n8n_integration.py` runs 01 and 02
  unmodified - so the placeholder is reported, never silently rewritten.

| Variable | Where it is set | Purpose |
| --- | --- | --- |
| `AI_WEBSITE_AGENT_API_URL` | the environment n8n runs with | base URL every node calls; loopback |
| `AI_WEBSITE_AGENT_API_TOKEN` | the environment n8n runs with | bearer token in each node's header; never stored in a workflow file |

Both are read through `$env.` rather than an n8n credential record, so no secret is ever written into an
exported JSON file; `validate_n8n_workflow_json()` refuses a workflow that embeds a literal token.

```bash
python3 -m unittest discover -s tests -p "test_n8n_integration.py" -v
```
