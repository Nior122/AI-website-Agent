# Lead & Project Tracking

`TrackingStore` is the single local JSON state store (`data/tracking/state.json`, ignored by Git). It preserves leads separately from projects and preserves build, validation, and deployment history by ID. No database or external service is required.

Create a lead, then a project only for `QUALIFIED` leads. Use guarded transitions; record builds, validations, deployments, and human review without overwriting history. `snapshot(project_id)` returns the current lead, project, latest records, and derived next action.

Duplicates use public identifiers: exact website domain, phone, or social URL gives `EXACT_MATCH`; same normalized name plus city/country gives `POSSIBLE_MATCH`; otherwise `NO_MATCH`. Possible matches are retained for review, not merged.

## Prospect engagement and outcome ledger

`tracking/engagement.py` (`EngagementLedger`) records what happens after a message is prepared, in the same
state file and with the same guards as everything else: six collections - `engagements` (an append-only event
log), `proposals`, `follow_ups`, `outcomes` (one current outcome per lead), `contact_blocks` (opt-outs), and
`relationships` (one thin progress row per lead: its stage, the records that prove it, and what is owed next).
The progress row is a projection, not a second history: every `stage_history` entry carries the ids of the records
that moved it, `pipeline` reports `stage_is_current: false` when re-deriving would disagree with what was recorded,
and a hand-asserted stage with no record behind it is refused with `STAGE_NEEDS_CORROBORATION`.

The point of the ledger is that each record states exactly what is known and no more:

| Recorded | What it can never become |
| --- | --- |
| an outreach draft's status moves `AWAITING_REVIEW` -> `READY_TO_SEND` -> `SENT_REPORTED` | `SENT_REPORTED` always carries `provider_verified: false`; nothing here can produce "delivered", and a send needs `--evidence-note`/`--evidence-url` before it is recorded at all |
| a send reported as failed (`--result FAILED`) | it is an *attempt*, not a contact: the outreach record becomes `SEND_FAILED`, the lead is not marked as reached, the relationship stage does not move, and the draft keeps its human approval so a deliberate retry reuses the same record instead of minting a second one |
| `ACCEPTED_BY_PROVIDER` / `VERIFIED_DELIVERED` | refused with `PROVIDER_RECEIPT_UNAVAILABLE` (from `outreach-mark-sent`, from `engagement-record --delivery-status`, from anywhere): this repository has no provider receipt reader, so the strongest send claim it can honestly store is what an operator reported |
| an inbound reply (`RESPONSE_RECEIVED`) with channel, time, and evidence | a paraphrase is never stored as a quote, and a missing reply is never a decision |
| a proposal (`DRAFT` -> `READY_FOR_PRESENTATION` -> `PRESENTED` -> `ACCEPTED`/`DECLINED`/`WITHDRAWN`) | creating one is not presenting one, and presenting one needs evidence of the act |
| an outcome per lead (`INTERESTED`, `FOLLOW_UP_REQUESTED`, `NEGOTIATING`, `WON`, `LOST`, `DECLINED`, `OPTED_OUT`, `NO_RESPONSE`, `PAUSED`, `UNRESOLVED`) | `WON` needs a customer-side evidence kind *and* a contract/invoice/payment `--reference`; no revenue figure is ever invented or stored |
| a follow-up schedule | overdue work is *reported* by `due-follow-ups`; no timer, job, or read path changes an outcome because time passed |
| a follow-up attempt count (`attempts`, `requires_approval: true`, `approval_state`) | a due follow-up is a scheduling fact with an eligibility verdict attached; it never becomes permission, and completing or rescheduling one records who did what when |
| an opt-out | recording one cancels the open follow-ups it covers and blocks new ones *and* further sends on that channel (`CONTACT_BLOCKED`), and it takes a customer statement to record or to revoke |

Refusals are `LedgerError` values carrying a stable code (`EVIDENCE_REQUIRED`, `CONTACT_BLOCKED`, `STAGE_NEEDS_CORROBORATION`, `PROVIDER_RECEIPT_UNAVAILABLE`, `DETAIL_MAY_CONTAIN_SECRET`,
`CONTACT_LIMIT_REACHED` - at most `MAX_SENDS_PER_LEAD_PER_CHANNEL` per lead per channel, counting attempts whether they
succeeded or failed - `OUTREACH_NOT_APPROVED`, `EVIDENCE_KIND_INSUFFICIENT` (an acceptance citing only an
operator's impression), `DEMO_URL_UNRECORDED` / `DEMO_URL_MISMATCH` (a proposal citing a URL no deployment
record holds), `UNKNOWN_APPROVAL` / `APPROVAL_PROJECT_MISMATCH`,
`PROPOSAL_TRANSITION_INVALID`, `OUTCOME_TRANSITION_INVALID`, `WON_EVIDENCE_REQUIRED`, `FOLLOW_UP_CLOSED`, ...)
and the CLI prints `{"error": ..., "error_code": ...}` with exit code 1. Failure and provider details are refused
outright when they look like they carry a credential (`DETAIL_MAY_CONTAIN_SECRET`) rather than quietly scrubbed,
because a silent rewrite of an audit line is its own loss. Every write is idempotent on a
derived or supplied key, so a repeated command returns the stored record with `duplicate: true` instead of
adding a second event. Free-text fields are scanned for email and phone shapes and redacted on write, because
the subject of this record set is the business, not a person's contact string.

```powershell
py -3 -m tracking.cli response-record lead-0002 --channel EMAIL --summary "asked what the demo would cost" --evidence-note "reply: 'how much for the site?'"
py -3 -m tracking.cli proposal-create lead-0002 --summary "one-page site plus menu" --terms-note "phased, quoted on the call"
py -3 -m tracking.cli proposal-update proposal-0001 --status READY_FOR_PRESENTATION
py -3 -m tracking.cli proposal-update proposal-0001 --status PRESENTED --evidence-note "walked through it on a call"
py -3 -m tracking.cli follow-up-schedule lead-0002 --due 2026-11-20 --reason "answer on price" --channel EMAIL
py -3 -m tracking.cli due-follow-ups
py -3 -m tracking.cli outcome-record lead-0002 --status WON --evidence-kind CONTRACT --reference "AGR-2026-14" --evidence-note "signed the engagement letter"
py -3 -m tracking.cli opt-out lead-0002 --channel EMAIL --reason "asked to stop email" --evidence-note "'please stop emailing us'"
py -3 -m tracking.cli ledger lead-0002
py -3 -m tracking.cli outreach-eligibility lead-0002 --channel EMAIL --outreach-id outreach-0001
py -3 -m tracking.cli outreach-mark-sent outreach-0001 --result FAILED --failure-note "no answer by 18:00"
py -3 -m tracking.cli relationship lead-0002
py -3 -m tracking.cli pipeline --stage RESPONDED --limit 25
py -3 -m tracking.cli outstanding
```

`outreach-eligibility` is the one guard every contact path consults - opt-out, the contact budget, the outreach
review decision, and the project's state - re-read at call time, and the answer is explicit that it grants
nothing (`grants_authorization: false`). A future scheduler asks it *and* still needs a human who decided to
send; the ledger has no way to record a send that nobody approved, and no command sends anything.

Each public write is one transaction: the collections it can touch are snapshotted first, every step of the
operation (the event, the outreach annotation, the relationship row, any follow-ups an opt-out cancels) lands in
a *single* durable save, and a refusal part-way through rolls the in-memory edits back so nothing half-applied
survives to be saved by the next command. `pipeline` and `outstanding` are read-only, clamped to 200 rows, and
count only persisted records - a lead the ledger has never been asked about is absent from `pipeline`, which is
not the same statement as "nothing happened" to that business.

The ledger is deliberately not wired to send, deploy, approve, or build anything - `tracking/engagement.py`
imports no network or provider module, and a test asserts that - and over HTTP it is read-only
(`GET /engagement/pipeline`, `/outstanding`, `/eligibility/:lead_id`, `/leads/:lead_id`), so an automated flow
can ask what is due but cannot record its own success: `OUTREACH_SENT` and every customer-side statement come
from an operator who saw them happen. On a database backend the collections persist through the existing `system_kv`
mechanism (one JSON document per collection, the pattern `worker_routing` already uses); promoting them to
real tables with per-row idempotency is the migration that should follow, not a reason to drop records now.

## Database integration commands

`python3 -m tracking.cli preflight-db` answers, without writing anything: which database this workspace
would use (host, port, database name, and the Supabase project reference embedded in the hostname), whether
it can be reached with the credential redacted, whether `migrations/0001_supabase_postgres_schema.sql` is
applied and its recorded checksum matches this checkout, which owned tables are missing, and how many rows
each table holds. `status` distinguishes `ready`, `needs_migration` (a step to take), `blocked` (a fault to
fix, e.g. a checksum conflict or a project-ref that does not match `--expect-project-ref`), `unreachable`,
and `not_configured`. Exit code 0 only for `ready`.

`migrate-db` applies the same schema and is additive and safe to repeat; applying DDL to a database that is
not on this machine requires `--confirm-database <name>`, read back from the URL, so a stale environment
variable cannot migrate the wrong Supabase project. `--verify-only` needs no confirmation because it runs no
DDL. Both print the identity they acted on and what the schema looked like beforehand.

Production mode (`AI_WEBSITE_AGENT_ENV=production` or `AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres`) never
falls back to local JSON: a missing or unreachable database raises `DatabasePersistenceError` at startup and
at the first write, and a `sqlite://` URL is refused outright. Full procedure: `docs/INTEGRATION-SETUP.md`.
