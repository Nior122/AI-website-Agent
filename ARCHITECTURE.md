# Architecture

## Purpose and boundary

The future system helps identify businesses with an active public presence that either lack a professional website or may have an outdated/poor one. It may research lawful public information, generate a clearly reviewable website preview using reusable templates, and prepare honest outreach.

This repository provides offline Scout, Research, Template, Builder, Validation, preview-deployment, tracking, outreach-preparation, and job-orchestration foundations. The preview adapter is verified against dedicated static project `ai-website-agent-preview`, with safe payload staging and live HTTPS preview verification. It does not run autonomous live research, scrape, generate real-business sites, deploy to production, contact anyone, or run active automation. One specifically approved, bounded public-data re-verification event is retained locally as an audit record; it did not create a lead or project.

## Core principle: agent-independent application

The application must not depend on Codex, Claude Code, n8n, Vercel, GitHub, or a single model provider. Core contracts, job state, templates, configuration, tests, scripts, and documentation belong in this repository. Agents are interchangeable operators of those artifacts.

Provider-specific adapters should be isolated from core business rules. A future AI configuration selects an approved provider/model profile; it must not embed credentials or make one provider mandatory.

## Future conceptual topology

```text
                         Orchestrator (future: n8n)
                                      |
                    +-----------------+-----------------+
                    |                 |                 |
                  Scout             Research            Build
                    \                 |                 /
                     +----------- AI Agent Layer --------+
                     | Codex | Claude Code | Other Agent |
                     +-----------------------------------+
                                      |
                 Project tools: GitHub | Browser | Vercel
                                      |
                            Lead state / persistent data
```

n8n may schedule, queue, retry, and route application-owned jobs. It must not directly write website code, own the only business state, bypass validation/human approval, or send messages. Specialized workers perform research, generation, modification, and validation through repository-controlled contracts and scripts. The application remains usable without n8n.

## Initial contracts

- `config/system.settings.example.json` is a checked-in, non-secret settings template. A future local settings file must be ignored and must not contain committed secrets.
- `schemas/lead.schema.json` is the initial portable lead record contract. It is deliberately a file contract, not a database selection.
- `schemas/scout-job.schema.json` describes the state and counters for a recoverable Scout run. `scout/` produces candidate leads first; a later storage phase maps reviewed candidates into persisted lead records.
- `scout/` contains the provider- and source-neutral Scout pipeline. Only `FixtureSource` is implemented; it loads explicitly fictional local data and cannot make network requests.
- `research/` contains evidence-led research, identity verification, website evaluation, and qualification. `FixtureResearchSource` is the only research source implementation and cannot make network requests. `reverification.py` is an offline validator for evidence supplied by an explicitly approved manual/public research event; it is not a scraper or browser.
- `templates/` contains a registry, shared semantic components, static CSS, a local renderer, and fictional demo data. It is provider-independent: business data, content, branding, and template structure remain separate.
- `builder/` maps qualified research records to template data, generates grounded structured content through a provider-neutral interface, validates output, and writes local build artifacts. Its default generator uses no AI provider.
- `validation/` is the independent local QA gate. It returns PASS, FAIL, or NEEDS_REVIEW with severity-tagged findings, but never changes an artifact or deploys it.
- `deployment/` consumes a validated artifact and creates an artifact-local preview deployment record. Executing a publish (not a dry run) additionally requires the build-local approval capability that `TrackingStore` materializes from an explicit `APPROVED_FOR_PREVIEW` decision for action `PREVIEW_DEPLOYMENT`, bound to that build, its validation, and the artifact's SHA-256. Its Vercel adapter accepts `ai-website-agent-preview` only for `TEST_FIXTURE` builds, requires an explicit `--approve-real-preview` flag plus a dedicated non-shared `*preview*` Vercel project for `REAL_PUBLIC_DATA` builds, stages an allowlisted public payload (`index.html` plus safely resolved referenced local assets), never uses `--prod`, defaults to dry run, and normalizes CLI output before returning it. Build/deployment manifests, hidden files, credentials, unrelated artifacts, unsafe links, and unresolved local assets fail closed and are never staged.
- `tracking/` is the source of truth for leads, projects, historical domain records, and the local `jobs` ledger. `TrackingStore` writes either the single local JSON state file (atomic temp-file + rename) or the normalized relational backend in `tracking/database.py` (SQLite compatibility mode for tests, PostgreSQL/Supabase in production), and never silently falls back from one to the other.
- `orchestration/` defines the job lifecycle, idempotency, retry/recovery, prerequisite gates, and provider-neutral `JobWorker` contract. It contains an optional fail-closed localhost HTTP adapter; no HTTP server starts automatically.
- `n8n/` contains inactive, credential-free self-hosted workflow examples. n8n calls the application job API only after the operator deliberately enables the API and configures a private network/secret store.
- `TASKS.md` is the Phase 1 handoff ledger. Future job records should be machine-readable and tied to the same job status vocabulary.

### Recoverable future job lifecycle

```text
lead_discovered → research_completed → qualified → website_build_started
    → website_build_completed → preview_deployed → outreach_prepared → completed
```

Failures must be explicit (`failed`) with an error and retry context; they must not require restarting an entire batch. A future implementation may add `queued`, `in_progress`, `needs_review`, `skipped`, and `cancelled` states.

## Orchestration boundary

`JobService` persists a minimal job record under `TrackingStore`: identity/reference fields, status, priority, attempts, small input/output summaries, error, timestamps, heartbeat, and dry-run flag. It never copies complete lead/project records into a job. A caller supplies `job_id` as the idempotency key; a repeat request for the same identity returns the original job, while a conflicting reuse is rejected.

Jobs are `PENDING`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `NEEDS_REVIEW`, or `BLOCKED`. The service checks prerequisites before running: BUILD requires a qualified lead; VALIDATE requires a project-associated build; DEPLOY requires PASS validation; PREPARE_OUTREACH requires a deployed, human-approved ready project. Missing requirements result in an explicit `BLOCKED` job—not an attempted external action.

Workers receive `{job, context}` and return `{status, output, artifacts, error, retryable}`. They are responsible for calling existing application services and updating domain state; `JobService` only records orchestration state. A retryable failure is re-queued until the configured maximum (default 3); permanent errors fail. `heartbeat()` and `recover_stale()` make interrupted RUNNING work visible and move stale work to `NEEDS_REVIEW`, never silently repeating it. Default concurrency is one; scheduling belongs to n8n, not another internal cron system.

Successful jobs expose a suggested next type, but successors are explicitly created. This avoids n8n blindly advancing through subjective gates. The intended path is SCOUT → RESEARCH → QUALIFY → BUILD → VALIDATE → DEPLOY → human review → PREPARE_OUTREACH. There is intentionally no SEND job.

The optional local API requires `AI_WEBSITE_AGENT_API_TOKEN` in the environment and a matching Bearer header, binds to localhost by default, and logs neither request headers nor bodies. It has read/create job endpoints only; its run endpoint deliberately refuses work until a future approved worker host is registered. Store n8n credentials in n8n's local secret/credential facility, never in exports or Git.

## Multi-agent worker layer

`WorkerRegistry` persists provider-neutral worker records in the same local state file: worker type, capabilities, priority, cost label, enabled/status/health state, heartbeat, current job, and non-secret configuration. It supports local, remote, API, human, and future worker types without coupling business logic to one agent.

The router selects an enabled, fresh-heartbeat, IDLE, AVAILABLE worker with the job capability. Optional configuration supplies per-capability preference order; priority then provides a stable fallback. Claiming creates a worker/job lease and an execution history record. A result is accepted only from the matching `worker_id` and `lease_id`, preventing stale or duplicate completion from overwriting newer state.

BUILD, DEPLOY, and PREPARE_OUTREACH take a project-level mutating lock. Validation/deployment job claims also reject a build ID that is no longer the project's assigned build. Expired leases release the lock, mark the worker unavailable, and re-queue within the job's retry limit; otherwise they need review. Transient worker failures can fail over to another capable worker. Deterministic and security failures do not retry. Human workers can take over a queued/review job deliberately.

The current JSON state store supports this only in the configured single-process local mode (default concurrency: one). It is deliberately not represented as a distributed lock service. Before multiple hosts/processes claim jobs, select and approve a persistent store with atomic compare-and-set/lease operations; until then, run only one worker process.

## Observability, operator controls, and alerting

Four surfaces answer operational questions, and they deliberately do not overlap: **liveness** is a process answering (`GET /healthz`, which proves nothing else); **readiness** requires an actively verified source of truth (`GET /readiness` via `verify_storage()`, `python3 -m orchestration.cli health`); **operational state** is what the records say (`GET /health`, `GET /attention`, `python3 -m orchestration.cli attention`, `inspect-job`, `list-jobs --attention-only --brief`); and **deployment outcome** is what publish evidence supports (`GET /deployment-status`, `python3 -m orchestration.cli deployment-status`, `deployment/authorization.py:publish_outcome()`), which classifies each project as `VERIFIED_RECORDED`, `UNCERTAIN`, `FAILED`, `RECORDED_UNCONFIRMED`, `SUPERSEDED`, or `NOT_ATTEMPTED` and never claims a live URL was checked.

`operator_attention()` is the single source for 'a human must act', derived from job codes (`error_code`, `next_action`, `requires_operator_action`), project stage/review/validation records, stale-running heartbeats, and the per-build publish ledger. A retryable `FAILED` job stays the system's business; an exhausted, blocked, or review-pending one surfaces as attention. Readiness separates `blocking` (storage or persistence - `NOT_READY`) from `degradations` (no capable worker, missing local artifacts, an operator backlog - still serving).

Each execution attempt is written to the pre-existing `executions` ledger, including on the service/CLI/API paths: attempt number, worker, start and completion, recorded duration, outcome, sanitized error type and message, stable error code, and a `trace` of the lead, project, build, validation, and deployment identifiers. Attempt history is therefore never reconstructed from wall-clock guesses, and a blocked job records no attempt at all.

`orchestration/observability.py` provides `redact()` / `sanitize_diagnostic()` used before any error is persisted on a job, execution, or worker record, or returned by an endpoint: bearer values, authorization headers, named secret assignments, `sk-` keys, JWTs, and complete connection-string userinfo are removed. `log_event()` emits one JSON object per line to stderr (filtered by `AI_WEBSITE_AGENT_LOG_LEVEL`, also appended to `AI_WEBSITE_AGENT_LOG_DIR/operations.jsonl` when configured) carrying timestamp, severity, operation, stable code, identifiers, and sanitized detail; nothing in this repository writes secrets to logs, and no logs are produced unless a severity threshold or log directory is configured.

`orchestration/alerts.py` derives critical alerts from the same attention data (uncertain publish, expired or stale leases, exhausted retries, unauthorized stores) and hands each to a pluggable sink: `LogAlertSink` by default, and `WebhookAlertSink`, which performs no network call of its own and delivers only through a caller-injected `transport` (documented limitation: no external messaging service is wired or configured here). Duplicate suppression is in-process. No alert or monitoring surface may grant an approval or bypass the Phase 38 publish gate; they read state only.

## Health, readiness, and integration evidence

`orchestration.health.system_health()` reports the application, source-of-truth (including the configured storage backend), job, worker, n8n, deployment, and configuration conditions without reaching external services. `storage_report()` is side-effect-free, while `verify_storage()` is the explicit connectivity check used by the API's `GET /readiness`, which answers `503 NOT_READY` when a required database cannot be reached or verified instead of reporting readiness from local memory alone. `readiness_for()` is job-specific: it checks prerequisites and an available capable worker, so a healthy local application is not incorrectly presented as ready for unavailable deployment work.

`tests/test_full_system.py` is the complete local integration proof. It uses only the clearly marked fictional `Lagos Fresh Kitchen (TEST / FICTIONAL)` record, creates a local website artifact, passes it through validation/tracking, records a fictional preview URL, requires human review, and stops at `READY_TO_SEND`. It makes no Vercel request and has no sender. Live n8n, Vercel, and worker-host verification remain outside the test because they require separately approved configuration.

## Controlled real-data discovery pilot

`scout.OverpassRestaurantSource` is a single, source-isolated public-data adapter. Its approved scope is Lagos, Nigeria restaurants only; it rejects other targets, permits one request per instance, limits output to five named candidates, applies a timeout, and retains an OpenStreetMap element URL as provenance. It neither follows business links nor infers website quality. The missing OSM website tag is represented as `none_known`, never as a factual claim that the business has no website.

Real candidates are marked `REAL_PUBLIC_DATA` and exported locally to ignored `data/leads/` review files. They do not automatically enter `TrackingStore`, create a project, trigger Research, create an outreach package, or contact a business. A human must review and explicitly authorize any next stage. Test fixtures remain separately marked `TEST_FIXTURE`.

## Website opportunity handoff

`opportunities.assess_opportunity()` is the single Scout-to-builder qualification handoff. It produces a score, reasons, evidence, priority, normalized website status, and a content brief split into `verified`, `unknown`, and `suggested` information. Existing website evidence maps to `WEBSITE_POOR`, `WEBSITE_OUTDATED`, `WEBSITE_BASIC`, or `WEBSITE_MODERN`; missing website data from a listing maps only to `WEBSITE_UNKNOWN`.

A project may be created only for a `QUALIFIED` tracked lead. The qualification policy requires score ≥60, sufficient independent evidence, and a source-backed website opportunity; uncertain identity/website ownership becomes `NEEDS_REVIEW`. `TrackingStore.record_qualification()` preserves decision history and `create_project()` returns the existing active project for the same lead, preventing duplicate projects; the relational backend re-checks that rule inside its transaction so a concurrent connection cannot commit a second ACTIVE project for the same lead. Restaurant briefs recommend the existing `restaurant-menu-v1` template, but suggestions never become verified business claims.

`MockWorker` is the deterministic test adapter. Real adapters must implement the existing worker contract, honor dry run, record normalized results and artifacts, never run arbitrary untrusted commands, and must be approved before registration. n8n observes/queues jobs only; it never chooses a provider, owns a lease, or bypasses the router.

## Persistence and workflow reliability invariants

`TrackingStore` (both backends) and `RelationalStateBackend.sync_state()` enforce the following invariants; they are covered by `tests/test_phase37_data_and_workflow_reliability.py`.

- Identifiers are allocated from the maximum existing numeric suffix (`next_numeric_id`), never `len(collection) + 1`, so a removed or concurrently created record cannot cause an ID to be reused while history still references it.
- Every multi-record operation (`record_build`, `record_validation`, `record_deployment`, `record_review`) validates its guarded stage transitions in memory (`_set_stage`) and performs one `_save()`, so a rejected transition or failed write cannot leave a half-applied project state.
- Record reuse is content-aware: an exactly repeated build/validation/deployment/review is an idempotent replay that returns the stored record, while the same ID carrying materially different content (`records_conflict`, ignoring derived and timestamp-only fields) is refused so the earlier result stays auditable.
- For the relational backend, an insert whose record ID was committed by another connection since the snapshot was loaded is either an idempotent replay or a `DatabasePersistenceError` naming the owning lead/project/build; a row is never silently overwritten. A live `project_locks` lease held by another worker is likewise refused, and only an expired lease may be taken over.
- `create_lead`, `create_project`, and `JobService.create` refresh from the database first when a backend is configured, so duplicate detection and ID allocation see the newest committed rows. The local JSON backend merges the on-disk state with the in-memory diff before writing; if that merge faults, the write is refused rather than clobbering another writer's records.
- `LocalPipelineWorker` (`DEPLOY`) never repeats an external publish: when the assigned build already has a recorded `DEPLOYED` result it returns `ALREADY_DEPLOYED_REPEAT_SKIPPED`, and dry runs are reported as `DRY_RUN_VERIFIED_NOT_DEPLOYED` so a verified plan cannot look like a completed deployment.

The local JSON state file remains a single-process design (see the multi-worker note above); cross-process safety is provided by the relational backend, not by file locking. No schema migration was required for these invariants: existing primary keys, `UNIQUE (idempotency_key)` on jobs, foreign keys, and `projects.lock_version` remain the database-level protections.

## Approval and deployment boundary

Generated, validated, awaiting-review, approved-for-one-action, deployed, and rejected are six distinct facts. Only a human can create the fifth, and only a provider result can create the sixth. `tests/test_phase38_approval_and_deployment_controls.py` covers these rules.

- `record_review` is the only way to reach `APPROVED_FOR_PREVIEW`. It requires a completed assigned build, a `PASS` validation for that build, and stage `READY_FOR_REVIEW`; it records project, lead, build, validation, decision, intended action, notes, reviewer, timestamp, and the identity source in an append-only history. An identical repeat is an idempotent replay; a materially different repeat becomes a new entry; `REJECTED`, `CHANGES_REQUESTED`, and an explicit hold revoke the deployment authorization instead of rewriting history.
- Recording a decision never deploys. `record_deployment` still refuses any status other than `DEPLOYED`, so a dry run can never be stored as a deployment, and `deploy-check`/`GET /projects/:id/deployment-authorization` are read-only.
- The authorization an approval grants is narrow and single-use: it is materialized as `output/<lead>/<build>/deployment-approval.json` (decision, action, build/validation/project/lead IDs, artifact SHA-256, reviewer, self digest) and revoked by a superseding build, a negative decision, or the recording of the deployment it authorized.
- The gate lives at the operation that initiates the publish — `VercelDeploymentProvider.deploy()` with `dry_run=False` — which every server-side path reaches: the operator CLI, `LocalPipelineWorker`, `JobService.prerequisite_failure()` (also covering `POST /jobs/:id/run` and `/retry`), and an n8n-triggered job. UI and n8n conditions are convenience layers, never the boundary. A refusal returns before the payload is staged, so an unauthorized attempt makes no external call.
- `TrackingStore.authorize_preview_deployment(project_id)` answers the same question from the durable record — project existence, ownership, assigned build, `PASS` validation, stage, approval binding and supersession, artifact presence, prior attempts, and the real-business isolated-target requirement — so every entry point reports the same machine-readable code. It requires the approval for **all** data classes, not only `REAL_PUBLIC_DATA`.
- Publishing is recorded, never assumed: `deployment-attempts.json` appends `DEPLOYING` before the provider is invoked and the terminal state after. A repeat of an already-published artifact is refused (`DEPLOYMENT_ALREADY_RECORDED`), a retried job whose deployment is recorded returns `ALREADY_DEPLOYED_REPEAT_SKIPPED`, and an interrupted or unverifiable publish ends as `PUBLISH_UNCERTAIN` — refused on the next attempt until an operator inspects the provider and deliberately re-runs with `--resolve-uncertain-publish`.

## HTTP trust boundary

The loopback API has exactly one credential: the shared `AI_WEBSITE_AGENT_API_TOKEN`. It is compared in
constant time and grants access to workflow state, not to a human identity. The boundary is therefore
drawn where authority differs, not where convenience does: a request may name identifiers only in a
validated form, may send a bounded body read only after the size check, may export a review package only
inside the operator-configured export root, and may never assert that its reviewer was verified -
`TrackingStore` clamps such a claim again at the point the approval capability is materialized, so a
self-granted assurance cannot reach the publish gate. Refusals are machine-readable (`error_code`) and
sanitized: internal filesystem paths, credentials, and stack traces stay out of responses and logs.
Read `orchestration/README.md` (*HTTP trust boundary*) for the endpoint-level rules and for the
limitations this design accepts.

## Directory responsibilities

| Path | Responsibility |
| --- | --- |
| `config/` | Versioned, non-secret defaults and example configuration. |
| `data/leads/` | Reserved for local/exported lead data; no real data is committed by default. |
| `jobs/` | Reserved for resumable job manifests/log metadata. |
| `schemas/` | Provider- and database-neutral data contracts. |
| `scout/` | Offline candidate discovery interface, normalization, deduplication, qualification, and CLI. |
| `research/` | Offline research, evidence provenance, identity checks, website evaluation, qualification, and CLI. |
| `templates/` | Static reusable templates, registry, shared components, brand variables, demo data, and renderer. |
| `builder/` | Qualified-lead build jobs, content-generator interface, local artifact creation, and validation. |
| `validation/` | Static QA, severity findings, result contracts, and local validation CLI. |
| `deployment/` | Preview-only Vercel adapter, deployment records, dry-run CLI, approval capability and publish ledger (`authorization.py`), and safety gates. |
| `tracking/` | Single local JSON source of truth for lead, project, build, validation, deployment, and review history, plus the `authorize_preview_deployment` gate. |
| `outreach/` | Draft-only, evidence-led outreach packages, revision history, and human draft review; no sender or transport. |
| `orchestration/` | Application-owned job ledger, gating, retry/recovery, worker interface, and optional private API. |
| `n8n/` | Inactive self-hosted n8n JSON examples and operating notes; no credentials. |
| `tests/` | Fixture-only verification; fixtures are not real business data. |
| `templates/` | Reserved for reusable, generic website templates only. |
| Root documentation | Project knowledge, safety constraints, roadmap, and handoff state. |

## Phase roadmap

| Phase | Scope | Not part of this phase |
| --- | --- | --- |
| 1 | Foundation, contracts, documentation, multi-agent safety | Application behavior |
| 2 | Scout Agent and dry-run discovery design | Unapproved live scraping |
| 3 | Public-information research and qualification | Fabricated data, live research without approved sources |
| 4 | Reusable website templates | Real-business generation or deployment |
| 5 | Provider-neutral generation/customization and local website builds | Autonomous publishing or deployment |
| 6 | Website validation and quality assurance | Deployment or automatic repair |
| 7 | Git source tracking and preview-deployment adapter | Project creation, automatic push, or production deployment |
| 8 | Persistent lead/project state tracking | Vendor lock-in |
| 9 | Transparent outreach preparation | Sending, account integration, or unapproved contact |
| 10 | n8n-compatible job orchestration, recovery, and worker contract | n8n writing site code, owning state, or sending messages |
| 11 | Provider-neutral worker registry, routing, leases, failover, execution history, and local mock worker | Real provider integration, autonomous messaging, or unsafe parallel project writes |
| 12 | Multi-agent/provider adapters and handoff reliability | Single-provider dependency |
| 13 | Batch processing, observability, reliability | Unbounded autonomous operation |
| 14 | Real-data candidate qualification and content-brief handoff | Automatic real lead/project creation or a website build |
| 15 | One bounded public re-verification event | Treating directory links as official ownership, building, deployment, or outreach |
| 19 | Review-only backlog resolution and bounded candidate selection | Lowering qualification gates, real website generation, deployment, or outreach |
| 20 | Branch-specific public website ownership and quality verification | Assuming a directory domain link proves ownership or treating design preference as a defect |

## Decisions requiring future approval

- Persistent storage choice and data-retention policy.
- Approved discovery sources, rate limits, and legal/compliance boundaries.
- Approved AI providers/models and credential handling.
- Hosting/deployment account and preview visibility policy.
- Outreach channels, sender identity, consent/compliance policy, and sending authorization.
- Whether and how n8n is introduced.
## Controlled targeted public research

Phase 15 added a narrowly scoped audit pattern for one approved candidate. It stores the prior source URLs, every additional public source, a property classification (`DIRECTORY`, `SOCIAL`, `LIKELY_OFFICIAL`, etc.), and explicit ownership/quality uncertainty. It does not promote a candidate, build a site, or create a project. The Cactus event demonstrates the pattern: public listings consistently associate a Google Business Profile URL and Facebook page with the restaurant, but direct ownership and web quality were not verified, so it remains `NOT_YET_QUALIFIED`.

Phase 16 re-applied the existing qualification gate to that persisted record. A read-only direct check confirmed the public Facebook page title but encountered a login gate for further profile content; it did not change ownership confidence, website status, or qualification. A real build requires a `QUALIFIED` record and is therefore blocked.

Phase 17 confirms that this block also prevents downstream preview review: without a tracked real lead/project, qualified content brief, build, PASS validation, and preview deployment, no review record can be created or approved. Fictional test artifacts are never evidence of a real deployment.

Phase 18 distinguishes discovery of a relevant web property from proof of business control. A social profile, business profile, directory, or platform-hosted menu can establish `WEBSITE_FOUND` without establishing a professional official website or an opportunity. An unverified hosted page with unsupported claims is classified `UNKNOWN` and is not used to judge the business's website quality. This preserves the existing qualification gate.

Phase 19 adds `opportunities.backlog.review_backlog()`, an offline, non-mutating selection helper. It invokes `assess_opportunity()` rather than creating a second qualification policy, ignores fictional records, deduplicates stable candidate IDs, and treats a historical unresolved decision as a veto rather than a promotion. The bounded Lagos review record is local and ignored by Git; it records `NO_QUALIFIED_OPPORTUNITY_FOUND` without creating tracking state.

Phase 20 adds a conservative branch-location comparison helper used by public-evidence reviews. It returns only `MATCHED`, `CONFLICT`, or `UNKNOWN`; it does not fuzzy-match business names or determine qualification. A Phase 20 website review must anchor ownership evidence to a specific recorded branch, retain `LIKELY_OFFICIAL` where legal control is not proven, and send the evidence through `assess_opportunity()` unchanged. A functioning public site without a source-backed material defect remains unqualified.

Phase 22B is a template-level visual-design follow-up, not a real-business build. The restaurant template owns the editorial layout, inline original SVG illustration, responsive CSS tokens, and fictional disclosure; structured fixture data still owns business facts. `TEST_FIXTURE` data suppresses actionable phone, WhatsApp, map, and social destinations, while non-fictional data retains the existing safe CTA behavior. The validator's `synthetic_contact` switch is narrowly for this clearly labeled fixture mode; real records still require expected verified contact CTAs.

Phase 23 retains the deployment boundary: the artifact-local `deployment.json` is the only record written for an attempted deployment until a successful `DEPLOYED` result exists. `TrackingStore.record_deployment()` intentionally rejects failed/pending results, so a project cannot be advanced to `PREVIEW_DEPLOYED` by a local dry run or configuration failure. Phase 24 adds a temporary, allowlisted public-payload stage before any Vercel command. The adapter requires a matching PASS build manifest and `.vercel/project.json` whose non-secret `projectName` is exactly `ai-website-agent-preview`; it refuses the historic `grace-light-community-church` link. Its preview URL parser accepts only `https` hosts ending exactly in `.vercel.app`. CLI authentication and project linking are explicit operator actions, never source-controlled configuration.

Phase 25 and Phase 26 establish the real-business draft boundary: a real candidate (`REAL_PUBLIC_DATA`) qualifies only through `assess_opportunity()` with corroborated public identity and a source-backed material website defect. `WebsiteBuilder` maps real-business template data strictly from `research.verifiedFacts` with zero fallback to unverified `candidate` fields or fictional defaults. When a restaurant's food menu is unverified, `restaurant-menu-v1` omits `#menu` and renders verified services under `#offerings` (`Offerings`) with an explicit uncommissioned preview draft disclosure. `validate_rendered_html()` verifies visible body content and expected verified facts (address, description, offerings, phone/WhatsApp digit match, email, social links) while blocking fictional fixture remnants, unparsed shortcodes, and off-screen spam. Real-business drafts remain local-only at `READY_FOR_REVIEW` (`PENDING` human review) and are never deployed without explicit operator approval.

Phase 27 hardens end-to-end workflow reliability across `TrackingStore`, `WebsiteBuilder`, `VercelDeploymentProvider`, `JobService`, `OutreachService`, and `system_health()`:
- Failed builds (`status == "FAILED"`) transition a project directly to `FAILED`, cannot be validated or promoted to `COMPLETED`, and can be superseded only by a new `build_id` (`FAILED -> BUILDING -> VALIDATING`).
- Recording a new build resets stale `latest_validation_id`, `latest_deployment_id`, and `review_status` on the project so prior build approvals/validations/deployments never carry over.
- Validation and deployment records verify exact `lead_id`, `project_id`, `assigned_build_id`, `validation_id`, and `artifact_path` (`output_location`) associations.
- Review decisions (`PENDING`, `APPROVED`, `REJECTED`, `CHANGES_REQUESTED`) append an auditable entry to `project["review_history"]` and `state["reviews"]`; both `REJECTED` and `CHANGES_REQUESTED` transition the project back to `BUILDING` so a draft can be corrected and reviewed again.
- `TEST_FIXTURE` and `REAL_PUBLIC_DATA` builds cannot cross-contaminate projects in `TrackingStore`, and `VercelDeploymentProvider` blocks `REAL_PUBLIC_DATA` builds from overwriting the shared `ai-website-agent-preview` project unless `approve_real_preview=True` (or a dedicated isolated preview project) is explicitly authorized.
- Because `.gitignore` excludes `output/`, `data/tracking/`, and `data/leads/*`, `TrackingStore` provides `check_local_state()` and `recover_local_state()` to inspect local state and rebuild missing HTML artifacts only when durable qualified lead source JSON files exist on disk, reporting missing evidence instead of fabricating real-business records.

Phase 28 adds the operator-facing CLI (`tracking/cli.py`) and isolated preview target configuration:
- `python3 -m tracking.cli` supports `status`, `snapshot <project_id>`, `history <project_id>`, `review <project_id> --decision <DECISION> --notes "<NOTES>"`, `configure-preview <project_id> --project-name <SLUG>`, and `check-workspace`.
- Recording a consequential review decision (`APPROVED`, `REJECTED`, `CHANGES_REQUESTED`) displays the target project and assigned build, requires meaningful review notes, and requires confirmation bound to `<project_id>:<build_id>:<DECISION>` (via interactive prompt or `--confirm`).
- `deployment/cli.py` and `VercelDeploymentProvider.deploy()` support `--project-name <SLUG>` and `--approve-real-preview`, reject production targets (`*prod*`, `*production*`, `*live*`, or slugs without `preview`), keep real-business preview deployments disabled by default, and refuse to let `REAL_PUBLIC_DATA` builds use or fall back to `ai-website-agent-preview`.

Phase 29 completes cross-module integration and retry/recovery hardening across the 12-stage pipeline:
- `ResearchAgent` filters out empty nested dictionaries (`contact`, `social`) when computing `verifiedFacts` vs. `uncertainties`, extracts `instagram_url` and `facebook_url` from verified evidence, and preserves top-level `data_classification`.
- `TrackingStore.ingest_research_record()` and `TrackingStore.build_payload()` provide an idempotent, non-fabricating handoff from `Scout` / `ResearchAgent` / `assess_opportunity()` into `TrackingStore` and `WebsiteBuilder.build()`.
- `WebsiteBuilder` supports plural/alias categories (`Restaurants`, `Salons`, `Cafes`, `Consulting`), builds directly from tracked lead payloads (`research_record`), and guarantees globally unique `build_id`s across leads and failed attempts in the same second.
- `TrackingStore.record_build()`, `record_validation()`, and `record_deployment()` are idempotent on exact retry, allow new builds to supersede `READY_FOR_REVIEW` / `VALIDATING` builds cleanly, and block deployment of `REJECTED` or `CHANGES_REQUESTED` drafts in both `TrackingStore` and `JobService`.
- `JobService.create_next()` and `WorkerRegistry.report()` propagate `lead_id`, `project_id`, `build_id`, and `next_job_types` across chained jobs.

Phase 37 (program Phase 2 — Data and Workflow Reliability) tightened persistence and workflow consistency without adding tables, migrations, queues, or a second workflow engine: gap-free numeric ID allocation (`next_numeric_id`), content-aware record reuse that refuses conflicting `build_id`/`validation_id`/`deployment_id`/`qualification_id` reuse, one single-transaction `_save()` per multi-record operation (`_set_stage`), atomic local state-file replacement, explicit refusal to clobber a concurrent DB row or another worker's live project lock, enforcement of one ACTIVE project per lead inside the relational transaction, an idempotent repeated review decision, a retried `DEPLOY` job that never re-publishes an already deployed build, and storage-aware health/readiness plus a structured `500` for unexpected API faults.
