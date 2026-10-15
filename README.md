# AI Website Agent

AI Website Agent is a future, provider-neutral system for finding and qualifying businesses, preparing legitimate website previews from public information, and preparing transparent outreach. It is intentionally not an autonomous outreach tool today.

## Current state

**Phase:** 42 — Real Integration Setup and Controlled Preview Readiness (read-only database preflight, a confirmed migration step, one readiness command, n8n workflow configuration checks, and a documented operator path to a preview)
**Status:** Fictional demonstration (`lead-0001` / `project-0001`) remains live at `https://ai-website-agent-preview.vercel.app` (`READY_FOR_REVIEW`, `PENDING` approval). An anonymous inspection of that URL on 2026-10-15 returned the `build-2026-10-12T024113z` page (section titles and fictional labels identical to this repository's own artifact), so the preview project exists and is reachable - while it is *not* evidence about any newer build, and `project-0003` has nothing deployed. The repository holds no `.vercel/project.json` link, so `deployment-status` correctly reports `project_linked: false` here. First qualified real business Pitstop Lagos (`lead-0002` / `project-0002`) has its updated, conflict-free local website draft at `output/lead-0002/build-2026-10-13T012227z/index.html` (and verified exported review package at `preview-export/lead-0002-pitstop-lagos/`) validated at 100/100 PASS (`validation-2026-10-13T012227z`, `READY_FOR_REVIEW`, `workflow_state: "VALIDATION_PASSED_READY_FOR_REVIEW"`, `PENDING` human review; not deployed), with the unresolved street address (`92A` vs `92b`) and telephone (`+234 807 749 8324` vs `+234 817 999 9005`) conflicts explicitly recorded and withheld until primary-source verification.
**Implemented:** Phase 1–36 foundations, safe preview deployment, bounded public-data qualification, 5-tier evidence hierarchy and automatic conflict withholding, strict verified-fact template mapping, real-business draft validation, `LocalPipelineWorker` end-to-end job execution (`SCOUT` → `PREPARE_OUTREACH`), 7-state human review state machine (`APPROVED_FOR_PREVIEW` for `PREVIEW_DEPLOYMENT` vs `APPROVED` for `OUTREACH_PREPARATION`), operator workflow CLI (`ingest-lead`, `build`, `review`, `review-package`, `configure-preview`, `prepare-preview`, `outreach`, `check-workspace`), integrated cross-module handoffs, and Phase 37 reliability hardening (gap-free identifier allocation, content-aware idempotent record reuse, single-transaction multi-record writes, atomic local state writes, concurrent-insert and project-lock conflict refusal, no repeat publish on retried DEPLOY jobs, and storage-verified API readiness), plus Phases 38-43 (approval and deployment controls, monitoring and operational controls, security hardening, integration readiness and reliable recovery, real integration setup, and the first-real-preview milestone) across 356 passing unit tests.
**Not implemented:** automated lead/project creation from real data, n8n activation, real external worker/provider integration, production deployment, external AI providers, outreach delivery, or automated messaging.

**Integration status, stated as four separate things** (see `docs/INTEGRATION-SETUP.md`, and `python3 -m orchestration.cli preflight` for the live verdict): *code readiness* is verified - the whole suite runs locally with the standard library only; *configuration readiness* is verified for the files and flags this repository owns (`render.yaml` against the CLI it starts, n8n exports against the variables they read); *connectivity* is **not** verified - no Supabase/PostgreSQL instance, Render service, n8n runtime, or Vercel project was reachable from here, so `preflight` reports `connectivity: not_tested` rather than assuming the configuration is correct; and *a real preview deployment* has **not** been performed in this workspace - `assurance.preview_deployment` is `not_performed`, and the steps that would change that all require an operator with dashboard access. The pre-publish checklist in `docs/INTEGRATION-SETUP.md` §8 was run against this workspace's real records: a validated fictional build exists (`project-0003`, validation `PASS`), and the deployment is blocked by four unmet conditions (no human approval for that build, no Vercel CLI, no `.vercel` link, no operator authorisation) - reported as the blocker, with the exact handoff commands, rather than approximated by a dry run or a hand-written link file.

Phase 20 verified Calabar Kitchen's Ikeja main branch against public independent listings and its accessible website. The domain is `LIKELY_OFFICIAL` with high confidence for that branch, but no material, source-backed website defect was established. Calabar Kitchen remains `NOT_YET_QUALIFIED`; no lead, project, build, deployment, contact, or outreach record was created.

Phase 22B redesigned the reusable `restaurant-menu-v1` experience using the explicitly fictional Lagos Fresh Kitchen fixture only. The renderer now produces an editorial green/ivory/terracotta restaurant concept with an original inline SVG meal illustration, responsive menu composition, visible fictional disclosure, and non-actionable fixture contacts. A fresh local build passed static QA (100/100) and browser checks at 1440, 1024, 768, 390, and 360px.

Phase 24 produced `build-2026-10-12T024113z` from the same fictional fixture. It passed static QA (100/100). Deployment payload packaging staged only `index.html` (zero manifests, secrets, or internal tracking files). The deployment adapter verified linkage to `ai-website-agent-preview` (`prj_KXSX2XqjBsbsjSXKKAXXbDrnYJRd`) and deployed without `--prod` to `https://ai-website-agent-preview.vercel.app`. Anonymous public inspection confirmed HTTP 200 OK and full rendering of the responsive demonstration site. TrackingStore review remains pending operator approval.

Phase 25 identified Pitstop Lagos (`lead-0002`) as the first qualified real business (`QUALIFIED`, score 95, P2) based on verified production defects on `https://pitstoplagos.com/` (unparsed Visual Composer shortcodes and hidden off-screen casino spam backlinks). Phase 26 and its recovery hardened the `WebsiteBuilder`, `restaurant-menu-v1` template components, CSS contrast, and `quality_validator` so real-business builds strictly use `research.verifiedFacts` without silent fallback to unverified candidate fields or fictional fixture remnants, generating `output/lead-0002/build-2026-10-12T193556z/index.html` (100/100 PASS) while keeping `project-0002` local-only at `READY_FOR_REVIEW` (`PENDING` human review).

## Repository map

```text
.
├── AGENTS.md                         # Instructions for any coding agent
├── ARCHITECTURE.md                   # Boundaries, future design, and roadmap
├── CHANGELOG.md                      # Human-readable change record
├── README.md                         # Project entry point
├── TASKS.md                          # Small, recoverable handoff/status ledger
├── render.yaml                       # Render blueprint: API service, no auto-deploy, no DDL at build
├── requirements.txt                  # The one external dependency: a PostgreSQL driver for the DB backend
├── config/
│   └── system.settings.example.json  # Non-secret future settings template
├── docs/
│   └── INTEGRATION-SETUP.md          # Environment, migration, n8n, Vercel, Render, verification, pre-publish checklist
├── migrations/
│   └── 0001_supabase_postgres_schema.sql  # Checksum-pinned relational schema; never edited in place
├── data/
│   └── leads/.gitkeep                # Ignored local lead/review data; no tracked leads
├── jobs/
│   └── .gitkeep                      # Reserved for future recoverable job records
├── schemas/
│   ├── lead.schema.json              # Initial persistent-lead contract
│   └── scout-job.schema.json         # Recoverable Scout job-result contract
├── scout/                            # Default-offline Scout Agent; opt-in bounded live pilot
├── research/                         # Default-offline Research Agent; opt-in bounded live page research
├── builder/                          # Local qualified-lead → validated preview builder
├── validation/                       # Local static website QA and structured findings
├── deployment/                       # Preview-only deployment adapter and local records
├── tracking/                         # Local lead/project state and history service
├── outreach/                         # Draft-only outreach preparation service
├── orchestration/                    # Local job ledger, worker contract, optional fail-closed API
├── n8n/                              # Inactive self-hosted n8n workflow examples; no secrets
├── templates/                        # Reusable static templates, registry, demos, and renderer
├── tests/                            # Fictional fixtures and standard-library tests
└── templates/
    └── .gitkeep                      # Reserved for reusable website templates
```

## Run and test

The Scout, Research, Template, Builder, Validation, Deployment, Tracking, and Orchestration systems use Python's standard library only; no package installation is required to run them or the tests. `requirements.txt` exists for one thing only: the PostgreSQL driver needed by the database backend. Preview deployment defaults to dry run; see [deployment/README.md](deployment/README.md). The n8n examples are inactive and do not start or replace an n8n installation.

Three commands describe the workspace without changing it, in PowerShell and in bash alike (`py -3` on
Windows, `python3` elsewhere):

```powershell
py -3 -m orchestration.cli preflight          # liveness, storage, workflow, n8n, provider - and what is only configured
py -3 -m tracking.cli preflight-db            # which database, reachable?, migrated? writes nothing
py -3 -m orchestration.cli check-n8n          # exported workflows: importable, required variables, starter placeholders
py -3 -m unittest discover -s tests           # the whole local suite
```

```powershell
git status --short --branch
git diff --check
git diff
```

Do not push, deploy, send outreach, create accounts, or install services unless explicitly authorized.

## Vercel preview prerequisite

An operator must manually authenticate with Vercel and link this directory to the dedicated, already approved `ai-website-agent-preview` project before an executed preview can be attempted (`docs/INTEGRATION-SETUP.md` §5 lists the order, including the exact Windows PowerShell sequence, and `preflight` reports `project_linked` and whether the linked name is the isolated preview project). Confirmed by the operator on 2026-10-15 for that project: domain `ai-website-agent-preview.vercel.app`, production branch `main`, preview deployments for all unassigned Git branches, and Ignored Build Step set to "Don't build anything" - recorded as operator statements, because this sandbox has no Vercel CLI and no route to `vercel.com`, and none of it is verified from here. The project ID is deliberately not assumed anywhere: it is read from `Settings → General` and compared with the `.vercel/project.json` that `vercel link` writes, since the deployer pins the linked project *name* and not its *id*. This repository must not create that project or invent `.vercel/project.json`; it explicitly refuses any other linked name, including `grace-light-community-church`. Configure the dedicated project as static HTML (Framework Preset `Other`, Build Command override enabled and empty, output served from the staged root) without changing the church project.

After that manual setup, an executed preview additionally requires an **explicit human approval for that exact build**: generated artifacts and `PASS` validations are never authorization on their own. Record the decision, check what the server-side gate will answer, and only then execute:

```powershell
python3 -m tracking.cli review project-0001 --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT --notes "Reviewed the fictional draft" --confirm project-0001:<build-id>:APPROVED_FOR_PREVIEW
python3 -m tracking.cli deploy-check project-0001
python -m deployment.cli --build output/<lead-id>/<build-id> --execute --project-id project-0001
```

`--execute` refuses without a recorded approval (`APPROVAL_MISSING`) and exits `1` before staging anything, so no external call happens. `deploy-check` is read-only and reports the same verdict the publish boundary will reach. A successful publish consumes the approval, so a retried job returns `ALREADY_DEPLOYED_REPEAT_SKIPPED` instead of publishing twice; an interrupted publish is recorded as `PUBLISH_UNCERTAIN` and must be resolved by an operator.

The adapter calls Vercel without `--prod`, explicitly targets the dedicated linked project ID, deploys a temporary sanitized payload rather than the internal build directory, accepts only an HTTPS `*.vercel.app` host, and records a real result in the artifact-local `deployment.json`. It never creates a fake URL.

### Establishing and verifying an isolated preview target for a real-business draft

Real-business (`REAL_PUBLIC_DATA`) builds are **disabled by default** (`NEEDS_REVIEW`) and can **never** use or fall back to the shared fictional demonstration project (`ai-website-agent-preview`) or any production target (`*prod*`, `*production*`, `*live*`, or a slug without `preview`). When an operator later decides to publish an isolated preview for a real-business draft:

1. Create and link a dedicated Vercel preview project whose slug includes `preview` (for example, `pitstop-lagos-preview`) so `.vercel/project.json` points to that isolated project.
2. Optionally record the expected isolated target on the tracked project:
   ```bash
   python3 -m tracking.cli configure-preview project-0002 --project-name pitstop-lagos-preview
   ```
3. Verify the dry-run deployment gate and the authorization gate before executing:
   ```bash
   python3 -m deployment.cli --build output/lead-0002/build-2026-10-13T012227z --project-name pitstop-lagos-preview --approve-real-preview
   python3 -m tracking.cli deploy-check project-0002
   ```
   Real-business builds follow the same approval rule as fictional ones, plus their isolation rules: without `APPROVED_FOR_PREVIEW` for the assigned build the gate answers `APPROVAL_MISSING`, and without a configured isolated target it answers `REAL_ISOLATED_TARGET_REQUIRED`. Nothing is published in either case.
4. Export, verify, or locally serve (`127.0.0.1` only) the human-review package, inspect project status/history, or record an explicit human review decision with operator confirmation (`<project_id>:<build_id>:<DECISION>[:<ACTION>]`):
   ```bash
   python3 -m tracking.cli review-package project-0002 --export-dir preview-export/lead-0002-pitstop-lagos
   python3 -m tracking.cli review-package project-0002 --verify-dir preview-export/lead-0002-pitstop-lagos
   python3 -m tracking.cli review-package project-0002 --export-dir preview-export/lead-0002-pitstop-lagos --serve --port 8080
   python3 -m tracking.cli status
   python3 -m tracking.cli snapshot project-0002
   python3 -m tracking.cli history project-0002
   python3 -m tracking.cli review project-0002 --decision CHANGES_REQUESTED --build-id build-2026-10-13T012227z --notes "Explain requested changes"
   python3 -m tracking.cli review project-0002 --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT --build-id build-2026-10-13T012227z --notes "Approved local draft for isolated preview deployment"
   python3 -m tracking.cli check-workspace
   ```

`orchestration.system_health(store, registry)` reports local subsystem state without contacting services. For operations, `python3 -m orchestration.cli health|attention|inspect-job|deployment-status|check-alerts` - and the matching authenticated `GET /health`, `GET /attention`, `GET /jobs/:id/history`, `GET /deployment-status` - report liveness separately from readiness, per-attempt job history, why a stage failed (stable code plus a sanitized message and the safe next action), which projects need a person, and whether a deployment outcome is recorded, uncertain, superseded, or never attempted. `attention --brief` exits `1` when work needs a human, so it is safe to schedule. `recover-stale` closes age-expired `RUNNING` jobs the same way the report ages them - including a worker that died before its first heartbeat - and a publish left unresolved is cleared only by an explicit, recorded operator decision (`deployment.cli --resolve-uncertain-publish --resolve-note ...`), never by the passage of time or by a retry. `readiness_for(store, jobs, job_id, registry)` checks whether a specific job has valid prerequisites and an available capable worker. Both are safe for local diagnostics and do not expose secrets.

### Running the n8n HTTP orchestration bridge

To connect a self-hosted n8n instance to the Python application over loopback (`127.0.0.1:8787`):

```bash
export AI_WEBSITE_AGENT_API_TOKEN="local-secret-not-committed"
export AI_WEBSITE_AGENT_API_URL="http://127.0.0.1:8787"
python3 -m orchestration.cli serve --host 127.0.0.1 --port 8787
```

Import the workflows under `n8n/workflows/` (`01-create-job.json`, `02-inspect-job.json`, `03-core-lead-to-review-package-pipeline.json`, `04-recover-and-retry-jobs.json`) into n8n, or verify them locally against the loopback HTTP API via `python3 -m unittest discover -s tests -p "test_n8n_integration.py" -v`. See `n8n/README.md` and `orchestration/README.md` for endpoint and workflow details.

## Safe change workflow

1. Inspect the repository and read `AGENTS.md`, this file, `ARCHITECTURE.md`, and `TASKS.md`.
2. Make one small, scoped change.
3. Run relevant checks/tests.
4. Review `git diff --check` and `git diff`.
5. Update `TASKS.md`, `CHANGELOG.md`, and related documentation when needed.
6. Commit only after review. Do not push unless authorized.

## Next step

The Phase 14 review classified all four Phase 13 real public-data candidates as `NEEDS_REVIEW`. Phase 15–18 checks corroborated Cactus identity/contact details and identified likely social/business-profile and menu-platform properties. Ownership and quality remain unverified, and an unverified Lovable-hosted page is explicitly excluded as an official-site signal. Phase 19 reviewed five bounded public-search candidates without finding an evidence-backed opportunity. Phase 20 resolved a likely-official association for Calabar Kitchen's Ikeja branch but found no qualifying material defect. No real leads or projects were created. The ignored audits include `data/leads/phase19-lagos-restaurants-backlog-review-20261010.json` and `data/leads/phase20-calabar-kitchen-website-verification-20261011.json`.

See `ARCHITECTURE.md` for the full roadmap and `TASKS.md` for the active handoff record.
