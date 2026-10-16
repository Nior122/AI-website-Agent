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
- `06-email-cycle-and-operator-notification.json` — one scheduled cycle for the two operator channels:
  `SCHEDULE/MANUAL TRIGGER -> cycle parameters (Set) -> POST /integrations/email/poll -> IF refused -> outcome`,
  `-> IF new mail arrived -> POST /integrations/telegram/notify -> IF nothing notified -> outcome`. It contains no
  Gmail credential, no bot token, and no messaging node: the mailbox and the bot are configured in the
  *application's* environment, and the workflow only asks for a cycle and reports what came back. That split is the
  whole design - n8n supplies the schedule, the retry, and the "did it run" visibility, while authorization,
  which messages to download, how a message is matched to a lead, what may be approved, and whether an operator
  has already been told about a condition all stay in this repository. Three outcomes are named: a cycle that needs
  an operator (a refused hold, a mailbox error, a contract refusal), nothing new, and the operator being notified.
  A held automation answers `409` before Google is contacted, and the notify step is deduplicated app-side, so
  running this graph every five minutes cannot re-page anybody about an unchanged record.
- `07-approved-outbound-send.json` — one operator-confirmed send, expressed as a request to the application:
  `MANUAL TRIGGER -> send parameters (Set: outreach_id, dry_run=true, confirmation="") -> POST /integrations/email/send
  -> IF refused -> outcome`, `-> IF provider answered -> outcome (accepted | reported-only)`,
  `-> POST /integrations/telegram/notify -> IF nothing notified -> outcome`. There is no Gmail credential in this
  file and no Gmail node in this graph: the send happens inside the application, against its own send-only
  interface, and the workflow can only ask for the draft an operator already approved. The exported defaults are
  the safe ones — `dry_run` on and the confirmation word empty — so a fresh import answers
  `CONFIRMATION_REQUIRED` or `DRY_RUN` and records nothing. A run cannot grant itself approval: eligibility,
  the opt-out list, the contact budget, the content digest, and the automation hold are all re-checked by the
  application per request, and `attempt_may_have_landed: false` on every refusal tells a reader that nothing
  reached a provider. An unanswered provider response arrives as `SEND_UNCERTAIN` with `retry_allowed: false`,
  and the outcome node tells the operator to reconcile rather than resend — Gmail has no idempotency key, so a
  retry could mail the same prospect twice.

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
### Activating `06` - scheduling a cycle without owning a mailbox

`06` is exported inactive like the others, and it should stay inactive until the two checks pass on the machine
that will run the application:

```bash
python3 -m orchestration.cli telegram-check            # configuration only; add --probe to ask Telegram
python3 -m orchestration.cli email-check               # configuration only; add --probe for one bounded read
python3 -m orchestration.cli telegram-command --command-text "/status" --user-id 1001
```

1. Give the application its own secrets, not the workflow: `AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN`,
   `AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS` (preferred - it authorizes a person, not a chat), and for Gmail
   `AI_WEBSITE_AGENT_GMAIL_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`, all with the **`gmail.readonly`** scope.
   The exported file references `AI_WEBSITE_AGENT_API_URL` and `AI_WEBSITE_AGENT_API_TOKEN` only; nothing in it can
   carry a mailbox credential, which is also why the adapter's own `query` is refused as a request body field.
2. In n8n, replace `Manual Trigger` with a Schedule node at whatever cadence the mailbox deserves (a cycle costs
   one `messages.list` call, or one `history` call once a cursor exists), keep the default `max_messages` of 25 or
   lower it, and leave `dry_run` on for the first days: a dry cycle reports what it *would* record and writes
   nothing, which is how you confirm the cursor, the matching, and the notification path before any of it lands in
   the ledger.
3. Read what it recorded with `python3 -m tracking.cli inbound-pending` and
   `python3 -m orchestration.cli email-poll` output in the execution log; a `409 AUTOMATION_HELD` answer is the
   brake working, not a fault. Turning on `telegram-serve` (or leaving notifications to this workflow) is an
   either/or choice: both call `notify_once()`, and the durable suppression window is what keeps them from
   duplicating each other.
4. Activating the workflow starts *scheduling* only. It cannot send a reply, cannot authorize a live discovery
   against a provider it was not given, and cannot approve anything - the approval buttons live in the bot's own
   callback path, where the allow-list is checked per update.

### Activating `07` — a send that stays the operator's decision

`07` is exported inactive, and unlike the others it should stay that way until the sending machine has said yes
to it three separate times:

```bash
python3 -m orchestration.cli email-send-check    # configuration + one token exchange, nothing sent
python3 -m orchestration.cli email-outbox        # what a human has approved and nobody has sent
python3 -m orchestration.cli email-send --outreach-id <id> --dry-run
```

1. `email-send-check` is the only command here that touches Google, and it sends nothing: it exchanges the
   refresh token minted for `gmail.send` and reports `verified`. Until that answer exists, sending state is
   `configured` at best, and `configured` is a statement about files, not about a working channel.
2. The workflow's `Set` node holds the only three parameters a send takes — which draft, whether to dry-run, and
   the confirmation word. Put a real `outreach_id` in it and leave `dry_run` on until an operator has read the
   preview that id points at. `tests/test_phase42_integration_setup_and_preflight.py` fails if the exported
   defaults ever drift to `dry_run: false` or a pre-filled confirmation, because the file as exported must be
   inert.
3. A schedule on `07` is a *reminder* mechanism, not a sending policy: if it fires for a draft whose content,
   preview, or opt-out state moved, the application refuses and the run ends at `Outcome - Nothing Sent`. That is
   the expected behaviour of a correctly imported workflow, not a failure to fix in the graph.
4. Activating it cannot make the system able to prove delivery. Gmail's send returns a message id and nothing
   else, so the strongest recorded result is `ACCEPTED_BY_PROVIDER` (id read back from the account through the
   read-only interface and matched to the approved draft). `VERIFIED_DELIVERED` stays unreachable from every
   surface, including this one.

## Local Verification When n8n Binary Is Not Installed

When a live self-hosted n8n runtime (`n8n` CLI or Docker container) is not running in the local environment, `orchestration/n8n_runner.py` (`N8nWorkflowRunner`) validates the exported n8n workflow JSON schema and executes the exact node graph against the live loopback `orchestration.api` HTTP server. Two rules govern that verifier:

- **Loopback only.** Every HTTP node carries the API bearer token in a header, so a node addressed to a host outside this machine raises `N8nWorkflowTargetRefused` *before the request is built* - a copied or hand-edited export cannot walk the credential to another host. `allow_remote=True` is the explicit opt-out for an operator who means it.
- **Faults are data.** A refused connection, a timeout, a non-2xx answer, and a body that is not a JSON object each become a structured output on the failing node (`error`, `error_kind`, `error_code`, `http_status`, `status: "FAILED"`) instead of a traceback, so the workflow's own IF branches reach their documented outcome node. The result also carries `execution_status` (`COMPLETED`/`FAILED`) and a collected `errors` list, and the validation step reports a missing or malformed workflow file as an actionable message. None of this retries, schedules, or activates anything.

### Checking the workflow configuration before importing

```bash
python3 -m orchestration.cli check-n8n                      # all seven exports
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
