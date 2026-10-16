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

One write is open to a workflow, and it is the narrow one: `POST /engagement/inbound` records what a business
replied (`n8n/workflows/05-inbound-response-intake.json`). It cannot record that anything was sent, approved,
presented, delivered, or won - those fields are refused by the application, not by the graph - so a workflow that
receives a reply still cannot mark its own outreach successful or close an opportunity.

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
- `05-inbound-response-intake.json` — hands one provider event to the authenticated intake route and routes on the answer:
  `WEBHOOK/MANUAL TRIGGER -> field allow-list (Set) -> POST /engagement/inbound -> IF recorded -> IF operator action needed -> outcome`.
  It is the smallest graph that does the job, and it deliberately contains no webhook, email, chat, or messaging node:
  receiving a delivery and *replying* to it are different capabilities, and only the first is exported here. The Set
  node lists the intake's fields, so an `outcome` or `approved` key cannot ride through a relay unnoticed - the
  application refuses them anyway. Three outcomes are named in the graph: response recorded, operator review
  required (unmatched / failed / uncertain opt-out), and not recorded (a contract refusal). Nothing in it sends.
- `04-recover-and-retry-jobs.json` — recovers stale `RUNNING` jobs (`POST /jobs/recover-stale`), inspects a target job (`GET /jobs/:id`), and safely retries/runs eligible `FAILED`, `QUEUED`, or `NEEDS_REVIEW` jobs (`POST /jobs/:id/retry`).

### Activating `05` - what an operator actually has to do

The file being importable is not a running workflow, and no workflow in this directory is active: each export
carries `"active": false`, and `check-n8n` refuses one that does not. To put inbound capture to work:

1. Choose the real receiving channel and check **its** API before writing any relay: does it hand you a stable
   message id, can it sign or be verified, and does it report an unsubscribe as the customer's own action? If it
   cannot, use `GENERIC_WEBHOOK` / `FORM_INBOUND` semantics and accept that identity is derived from content
   rather than proven. This repository has verified none of them, and connects none.
2. Set `AI_WEBSITE_AGENT_API_URL` and `AI_WEBSITE_AGENT_API_TOKEN` in the environment n8n reads (the export
   references nothing else; `check-n8n` lists what a file needs and what is missing here).
3. In the n8n UI, import the JSON, replace `Manual Trigger` with a Webhook node (its request body becomes
   `$json.body`, which is what the Set node reads), and keep the node's failure path enabled so a `400`/`401`
   from the application fails the execution visibly instead of being swallowed.
4. Optional, recommended when the relay is not this application's own process: set
   `AI_WEBSITE_AGENT_INBOUND_SIGNING_SECRET` on the *application*, and have the relay put
   `HMAC-SHA256(secret, raw body)` into `X-Inbound-Signature` (in this export the value comes from
   `$json.signature_header`, so a Code node or n8n's Crypto node computes it). If a secret is configured and the
   header is missing or does not match, the application answers `401` and records nothing.
5. Quote-bearing bodies: the exported `jsonBody` interpolates fields into a JSON string. That is fine for ids and
   plain text; in n8n's expression language prefer `={{ JSON.stringify($json) }}` for the message field so a
   customer typing a `"` cannot produce malformed JSON (the application would refuse it as `BODY_INVALID`, never
   mis-parse it).
6. Activate the workflow only when you want it running, and confirm the first delivery with
   `python3 -m tracking.cli inbound-pending` - the intake's backlog is the check that events are arriving, being
   linked, and being left alone.

Re-delivery is safe: the intake dedupes on the provider's event id, so pointing a webhook at this route twice,
or replaying an execution, cannot double-count a reply. A re-run after a storage fault is likewise how a `FAILED`
row gets resolved.

## Local Verification When n8n Binary Is Not Installed

When a live self-hosted n8n runtime (`n8n` CLI or Docker container) is not running in the local environment, `orchestration/n8n_runner.py` (`N8nWorkflowRunner`) validates the exported n8n workflow JSON schema and executes the exact node graph against the live loopback `orchestration.api` HTTP server. Two rules govern that verifier:

- **Loopback only.** Every HTTP node carries the API bearer token in a header, so a node addressed to a host outside this machine raises `N8nWorkflowTargetRefused` *before the request is built* - a copied or hand-edited export cannot walk the credential to another host. `allow_remote=True` is the explicit opt-out for an operator who means it.
- **Faults are data.** A refused connection, a timeout, a non-2xx answer, and a body that is not a JSON object each become a structured output on the failing node (`error`, `error_kind`, `error_code`, `http_status`, `status: "FAILED"`) instead of a traceback, so the workflow's own IF branches reach their documented outcome node. The result also carries `execution_status` (`COMPLETED`/`FAILED`) and a collected `errors` list, and the validation step reports a missing or malformed workflow file as an actionable message. None of this retries, schedules, or activates anything.

### Checking the workflow configuration before importing

```bash
python3 -m orchestration.cli check-n8n                      # all five exports
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
