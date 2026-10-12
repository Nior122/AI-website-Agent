# AI Website Agent

AI Website Agent is a future, provider-neutral system for finding and qualifying businesses, preparing legitimate website previews from public information, and preparing transparent outreach. It is intentionally not an autonomous outreach tool today.

## Current state

**Phase:** 24 — Isolated Vercel preview, safe artifact packaging, and deployment completion
**Status:** Dedicated project `ai-website-agent-preview` linked and verified; build `build-2026-10-12T024113z` validated at 100/100 PASS; staged payload deployed without `--prod` to `https://ai-website-agent-preview.vercel.app`; hosted page verified anonymously accessible.
**Implemented:** Phase 1–24 foundations, safe preview deployment, and bounded public-data re-verification record format.
**Not implemented:** automated lead/project creation from real data, n8n activation, real worker/provider integration, production deployment, external AI providers, outreach delivery, or automated messaging.

Phase 20 verified Calabar Kitchen's Ikeja main branch against public independent listings and its accessible website. The domain is `LIKELY_OFFICIAL` with high confidence for that branch, but no material, source-backed website defect was established. Calabar Kitchen remains `NOT_YET_QUALIFIED`; no lead, project, build, deployment, contact, or outreach record was created.

Phase 22B redesigned the reusable `restaurant-menu-v1` experience using the explicitly fictional Lagos Fresh Kitchen fixture only. The renderer now produces an editorial green/ivory/terracotta restaurant concept with an original inline SVG meal illustration, responsive menu composition, visible fictional disclosure, and non-actionable fixture contacts. A fresh local build passed static QA (100/100) and browser checks at 1440, 1024, 768, 390, and 360px.

Phase 24 produced `build-2026-10-12T024113z` from the same fictional fixture. It passed static QA (100/100). Deployment payload packaging staged only `index.html` (zero manifests, secrets, or internal tracking files). The deployment adapter verified linkage to `ai-website-agent-preview` (`prj_KXSX2XqjBsbsjSXKKAXXbDrnYJRd`) and deployed without `--prod` to `https://ai-website-agent-preview.vercel.app`. Anonymous public inspection confirmed HTTP 200 OK and full rendering of the responsive demonstration site. TrackingStore review remains pending operator approval.

## Repository map

```text
.
├── AGENTS.md                         # Instructions for any coding agent
├── ARCHITECTURE.md                   # Boundaries, future design, and roadmap
├── CHANGELOG.md                      # Human-readable change record
├── README.md                         # Project entry point
├── TASKS.md                          # Small, recoverable handoff/status ledger
├── config/
│   └── system.settings.example.json  # Non-secret future settings template
├── data/
│   └── leads/.gitkeep                # Ignored local lead/review data; no tracked leads
├── jobs/
│   └── .gitkeep                      # Reserved for future recoverable job records
├── schemas/
│   ├── lead.schema.json              # Initial persistent-lead contract
│   └── scout-job.schema.json         # Recoverable Scout job-result contract
├── scout/                            # Offline-first Scout Agent and CLI
├── research/                         # Offline-first Research & Qualification Agent
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

The Scout, Research, Template, Builder, Validation, Deployment, Tracking, and Orchestration systems use Python's standard library only; no package installation is required. Preview deployment defaults to dry run; see [deployment/README.md](C:\Users\PC\Documents\ChatGPT\AI website Agent\deployment\README.md). n8n examples are inactive and do not start or replace an n8n installation.

```powershell
git status --short --branch
git diff --check
git diff
```

Do not push, deploy, send outreach, create accounts, or install services unless explicitly authorized.

## Vercel preview prerequisite

An operator must manually authenticate with Vercel and link this directory to the dedicated, already approved `ai-website-agent-preview` project before an executed preview can be attempted. This repository must not create that project or invent `.vercel/project.json`; it explicitly refuses any other linked name, including `grace-light-community-church`. Configure the dedicated project as static HTML (Framework Preset `Other`, Build Command override enabled and empty, output served from the staged root) without changing the church project. After that manual setup, regenerate a fictional build and run:

```powershell
python -m deployment.cli --build output/<lead-id>/<build-id> --execute
```

The adapter calls Vercel without `--prod`, explicitly targets the dedicated linked project ID, deploys a temporary sanitized payload rather than the internal build directory, accepts only an HTTPS `*.vercel.app` host, and records a real result in the artifact-local `deployment.json`. It never creates a fake URL.

`orchestration.system_health(store, registry)` reports local subsystem state without contacting services. `readiness_for(store, jobs, job_id, registry)` checks whether a specific job has valid prerequisites and an available capable worker. Both are safe for local diagnostics and do not expose secrets.

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
