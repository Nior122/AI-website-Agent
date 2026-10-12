# Changelog

All notable project changes are recorded here. Dates use ISO 8601 (`YYYY-MM-DD`).

## [Unreleased]

### Added — 2026-10-12 (Repository Publishing)

- Initialized Git repository on `main` branch with strict `.gitignore` safety boundaries keeping credentials, local environment files (`.env.local`), Vercel state (`.vercel/`), generated build artifacts (`output/`), local tracking records (`data/tracking/`), and raw discovery data (`data/leads/*`) completely uncommitted.
- Created public GitHub repository `AI-website-Agent` (`https://github.com/Nior122/AI-website-Agent`) under account `Nior122` via GitHub CLI and pushed full codebase history through Phase 26.

### Added — 2026-10-12 (Phase 26)

- Fact-grounded local website draft for first qualified real lead Pitstop Lagos (`lead-0002`): reused `restaurant-menu-v1` template with clean brand initials (`PL`), `Lagos · uncommissioned preview draft` eyebrow, and explicit evaluation disclosure (`Uncommissioned preview draft prepared for internal evaluation · Not an authorized or official website of Pitstop Lagos`).
- Strict fact ledger documented in `data/leads/phase26-pitstop-lagos-website-draft-20261012.json`: recorded verified public facts (identity, 92A Younis Bashorun Street address, +234 807 749 8324 phone, info@pitstoplagos.com, Instagram, verified wellness/cafe services) while strictly omitting unverified facts (zero dishes or prices fabricated, no unverified hours or fake booking forms).
- Quality validator hardening in `validation/quality_validator.py`: added detection for casino spam keywords, unparsed template shortcode markup (`[vc_row]`, `[rodich_]`), and hidden off-screen positioning styles (`left/top: -9999px`) in real builds.
- Dedicated draft build artifact generated at `output/lead-0002/build-2026-10-12T041042z/index.html` passing static QA at 100/100 (14/14 checks passed).
- TrackingStore lifecycle progression: created `project-0002`, recorded build and validation, safely transitioning to `READY_FOR_REVIEW` with human review held at `PENDING`.
- Focused Phase 26 regression test suite (`tests/test_phase26_real_draft.py`) verifying lead identity preservation, build qualification gating, fictional/real data isolation, unverified fact omission, spam/shortcode rejection, and tracking transitions.

### Verified — 2026-10-12 (Phase 26)

- Live website reverification: bounded read-only inspection of `https://pitstoplagos.com/` at `2026-10-12T04:04:00Z` re-confirmed unparsed Visual Composer shortcodes and >70 hidden casino spam hyperlinks (`left:-9999px;top:-9999px`) in production DOM.
- Full regression suite: all 112 unit tests passed (0 failures, 0 skipped). `python -m compileall` passed clean. `git diff --check` passed clean.
- Zero fictional content leakage: verified generated real-business draft contains no "LF" brand mark, no "fictional test kitchen" eyebrow, and no fictional disclosure text.
- Historical data intact: `lead-0001` and `project-0001` remain at `READY_FOR_REVIEW` / `PENDING` with live preview at `https://ai-website-agent-preview.vercel.app`. Phase 19, 20, and 25 audit documents remain untouched.

### Not added — 2026-10-12 (Phase 26)

- No external publication or Vercel deployment of the real-business draft (local build only).
- No outreach: zero emails, phone calls, SMS, WhatsApp messages, form submissions, or social media messages.
- No human review approval bypassed; `project-0002` remains `PENDING`.

### Added — 2026-10-12 (Phase 25)

- Bounded discovery and qualification audit: diagnosed historical discovery bottleneck (Overpass static query returning identical 4 nodes lacking website tags); added fallback endpoint retry architecture to `OverpassRestaurantSource` for transient API 504 / timeout resilience.
- Comprehensive Phase 25 discovery and qualification audit document saved at `data/leads/phase25-lagos-restaurants-discovery-and-qualification-20261012.json` evaluating 5 previously unreviewed independent Lagos restaurants with public evidence.
- First verified qualified real lead: Pitstop Lagos (`92b Younis Bashorun Street, Victoria Island, Lagos`) identified with confirmed material production defects (unparsed page builder shortcodes `<p>[vc_row][vc_column]</p>` and over 70 malicious hidden foreign online casino spam backlinks in lines 534–599 and 653–700 of the live HTML payload).
- Lead record `lead-0002` created in `TrackingStore` as `REAL_PUBLIC_DATA` and evaluated under unmodified `assess_opportunity()` policy (score 95, priority P2, high confidence, status `QUALIFIED`).
- Focused Phase 25 unit test suite (`tests/test_phase25_discovery.py`) verifying historical vetoes, branch preservation, duplicate detection, unknown website non-absence, defect validation, project creation gating, fixture separation, audit integrity, and fallback endpoint retry.

### Verified — 2026-10-12 (Phase 25)

- Full regression suite: all 103 unit tests passed (0 failures, 0 skipped). `python -m compileall` passed with zero errors. `git diff --check` passed clean.
- All 5 candidates evaluated with live public evidence without lowering qualification thresholds or inventing defects: Yellow Chilli (`NOT_YET_QUALIFIED`), Ofada Boy (`NOT_YET_QUALIFIED`), Danfo Bistro (`NOT_YET_QUALIFIED`), NOK by Alara (`NOT_YET_QUALIFIED`), and Pitstop Lagos (`QUALIFIED`).
- Prior decisions and audit records from Phase 19 (`data/leads/phase19-lagos-restaurants-backlog-review-20261010.json`) and Phase 20 (`data/leads/phase20-calabar-kitchen-website-verification-20261011.json`) preserved intact.
- Fictional demonstration state preserved: `lead-0001` and `project-0001` remain at `READY_FOR_REVIEW` / `PENDING` with live preview at `https://ai-website-agent-preview.vercel.app`.
- Lead `lead-0002` safely recorded in `TrackingStore` without creating an active website project, build, preview deployment, or outreach draft.

### Not added — 2026-10-12 (Phase 25)

- No real-business website generation, preview deployment, or public publishing.
- No external outreach: zero emails, phone calls, SMS, WhatsApp messages, form submissions, or social media messages.
- No credentials, tokens, or secrets introduced or committed.

### Added — 2026-10-12 (Phase 24)

- Isolated, temporary deployment payload staging for Vercel. Only `index.html` and local assets directly referenced by HTML or local CSS are copied with preserved relative paths; manifests, hidden files, test data, credentials, unrelated files, traversal paths, unsafe symlink targets, missing assets, and secret-like text fail closed.
- Dedicated-target safety gate: `VercelDeploymentProvider` now requires the linked non-secret `projectName` to be `ai-website-agent-preview` and refuses the historic `grace-light-community-church` target before any CLI invocation. Preview execution remains explicitly scoped by project ID and never includes `--prod`.
- Portable symlink escape regression: updated deployment test with mock fallback on `Path.resolve` when OS symlink creation is unavailable on Windows, eliminating skipped tests.
- Deployment tests for staged assets, manifest exclusion, missing/traversal/symlink/secret rejection, church-target rejection, and dedicated-target preview command selection.

### Verified — 2026-10-12 (Phase 24)

- Fresh fictional artifact `output/fictional-lagos-fresh-kitchen-001/build-2026-10-12T024113z/index.html` selected `restaurant-menu-v1` and passed static validation at 100/100 with no findings.
- Operator CLI authentication (`chinecheremjoseph39-4558`) and dedicated project linkage (`.vercel/project.json` identifying `ai-website-agent-preview`, project ID `prj_KXSX2XqjBsbsjSXKKAXXbDrnYJRd`) confirmed.
- Static HTML framework settings verified: Framework Preset `Other`, no build command, root directory `.`.
- Full regression suite: all 91 tests passed (0 failures, 0 skipped). `git diff --check` passed clean.
- Preview deployment executed without `--prod` targeting staged payload (`index.html` only). Succeeded with genuine HTTPS URL: `https://ai-website-agent-preview.vercel.app`.
- Hosted website verified via HTTP GET: returned HTTP 200 OK with anonymous accessibility (no SSO/authentication redirect). Confirmed title (`Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS`), editorial navigation, inline SVG meal illustration, menu cards, and footer fictional disclosure.
- Artifact `deployment.json` updated to `DEPLOYED`. TrackingStore and human review state preserved at `READY_FOR_REVIEW` / `PENDING`.

### Not added — 2026-10-12 (Phase 24)

- No production deployment (`--prod`), DNS/domain modification, real-business deployment, outreach, paid services, commits, or pushes.

### Added — 2026-10-12 (Phase 23)

- Exact Vercel preview-host validation: the adapter now accepts only HTTPS hosts ending in `.vercel.app`, rejecting lookalike and non-HTTPS CLI output.
- Cross-platform binary resolution via `shutil.which("vercel")` in `VercelDeploymentProvider` to support Windows npm command shims (`vercel.cmd`) without `shell=True`.
- Explicit linked-project scoping (`--project <projectId>`) in `VercelDeploymentProvider.deploy()` so subpath deployments strictly target the approved linked project ID rather than triggering unlinked project creation.
- Deployment regressions for unexpected URLs and an unauthenticated Vercel CLI failure, both confirming no fabricated deployment URL is recorded.

### Verified — 2026-10-12 (Phase 23)

- Fresh fictional build `build-2026-10-12T020610z` selected `restaurant-menu-v1`, passed static QA at 100/100, passed structural checks, and rendered locally with working internal links, visible fictional disclosure, and no actionable synthetic contact links.
- Operator CLI authentication (`chinecheremjoseph39-4558`) and project link (`.vercel/project.json` linking `prj_XQCUqsZv41iZJzI07SX8bLy9jPla`, `grace-light-community-church`) confirmed.
- Executed preview deployment adapter without `--prod`. Remote Vercel build failed due to the linked project's Framework Preset (`Vite` / `vite build` exited 127 against zero-dependency static HTML/CSS artifact).
- Deployment adapter failed closed safely: `output/fictional-lagos-fresh-kitchen-001/build-2026-10-12T020610z/deployment.json` records `FAILED` with exact error and `deployment_url: null`. No fabricated URL was recorded.
- TrackingStore state remains at `READY_FOR_REVIEW`; human review remains `PENDING`.

### Not added — 2026-10-12 (Phase 23)

- No production deployment (`--prod`), project recreation, domain/DNS modification, real-business record, outreach, credentials in code/logs, commit, or push.

### Added — 2026-10-12 (Phase 22B)

- Premium reusable restaurant presentation for the fictional `Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS` fixture: bottle-green/ivory/terracotta design tokens, editorial type hierarchy, responsive composition, and an original inline SVG meal illustration with no external asset dependency.
- Safe fixture-contact behavior: `TEST_FIXTURE`/`demo` pages use in-page calls to action, omit synthetic phone/WhatsApp/map/social destinations, and display a persistent fictional-demo disclosure.
- Focused regression coverage for safe synthetic-contact validation; real records continue to require verified contact CTA checks.

### Verified — 2026-10-12 (Phase 22B)

- Fresh fictional artifact `output/fictional-lagos-fresh-kitchen-001/build-2026-10-12T014458z/index.html` passed static validation at 100/100 and browser checks at 1440, 1024, 768, 390, and 360px with no horizontal overflow or console errors.
- No external image, font, script, service, deployment, real-business data, outreach, credential, commit, or push was introduced.

### Added — 2026-10-12 (Phase 22)

- Minimal standard-library `--serve` option in `builder.cli` (`serve_build()`) that binds exclusively to 127.0.0.1 and serves only the specified build artifact directory without external dependencies.
- Sub-second build ID collision protection in `WebsiteBuilder.build()` that inspects target directory existence and appends counter suffixes when multiple builds occur in the same second.
- Derived `validation_id` in `validate_rendered_html()` and `deployment_id` in `VercelDeploymentProvider` anchored directly to `build_id` to guarantee 1-to-1 artifact traceability and prevent tracking state overwrites.
- Phase 22 inspection and repeatability tests (`tests/test_phase22_inspection.py`) verifying local static serving validation, multi-run repeatability, anchor link resolution, landmark structure, and zero real-data leakage.

### Verified — 2026-10-12 (Phase 22)

- Local website artifact inspection verified via zero-server browser opening (`file:///`) and localhost server (`python -m builder.cli --serve ...` and `python -m http.server ...`).
- Build artifact integrity confirmed: generated HTML matches `build.json` manifest output location, all internal navigation anchors (`#top`, `#about`, `#menu`, `#location`, `#hours`, `#contact`) resolve to valid DOM IDs, responsive CSS rules are present, and zero real-business data leaks into fictional output.
- Repeatability verified across multiple consecutive demonstration runs: earlier build directories and tracking records are preserved, `assigned_build_id` advances cleanly, duplicate leads are prevented, and human review remains `PENDING`.
- Vercel preview deployment prerequisites documented; confirmed adapter fails closed safely on unlinked configurations without emitting simulated URLs.
- Full test suite passed (84 tests, 0 failures); Python compilation verified; `git diff --check` passed.

### Added — 2026-10-12 (Phase 21)

- Dedicated fictional test fixture `tests/fixtures/fictional_test_business_lagos_fresh_kitchen.json` for "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS" clearly classified as `TEST_FIXTURE`.
- End-to-end builder demonstration tests (`tests/test_phase21_demonstration.py`) verifying candidate qualification, content brief generation, template selection, deterministic website building, automated QA validation, preview deployment gating, and pending human review record creation.
- Review stage guard in `TrackingStore.record_review()` preventing project state mutation when approval prerequisites are unsatisfied.

### Verified — 2026-10-12

- End-to-end pipeline successfully demonstrated on fictional test business "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS": generated single-page static website with responsive CSS, semantic landmarks, grounded menu/location/contact information; achieved 100/100 validation score (PASS) on all 14 automated QA checks.
- Vercel preview deployment safely failed closed due to absence of approved linked project configuration (`.vercel/project.json`), with exact blocker recorded and zero credentials or fake URLs produced.
- Human review record created in `PENDING` status; website is not automatically approved.
- All 80 unit tests pass; Phase 19 and Phase 20 audit records and qualification decisions remain intact.

### Not added — 2026-10-12

- No real-business lead, project, content brief, build, deployment, outreach, form submission, purchase, or account changes. Calabar Kitchen and Cactus Restaurant remain unqualified.

### Added — 2026-10-11

- A conservative exact-branch comparison helper and focused tests that prevent similarly named, differently located businesses from being silently merged.
- One ignored, review-only Calabar Kitchen ownership/website-condition audit anchored to the independently corroborated Ikeja branch.

### Verified — 2026-10-11

- `calabar-kitchen.com` is `LIKELY_OFFICIAL` with high confidence for the Ikeja main branch, but the accessible public site had no demonstrated material defect satisfying the existing qualification policy. Calabar Kitchen remains `NOT_YET_QUALIFIED`.

### Not added — 2026-10-11

- No Calabar Kitchen lead, project, content brief, build, deployment, contact, outreach, form submission, purchase, account, or production change.

### Added — 2026-10-10

- An offline, non-mutating opportunity-backlog selector that delegates to the existing qualification function, removes duplicate candidate IDs, excludes fictional records, and creates no tracking state.
- A local ignored Phase 19 Lagos restaurant audit covering the four existing candidates and five bounded public-search candidates.

### Verified — 2026-10-10

- `NO_QUALIFIED_OPPORTUNITY_FOUND`: Cactus remains unresolved and none of the five new candidates had both supported ownership and a source-backed website deficiency.

### Not added — 2026-10-10

- No real lead, project, content brief, build, deployment, outreach, form submission, purchase, account, or production change.

### Added — 2026-10-10

- Bounded Phase 18 Cactus public-property evidence, including property classifications, ownership uncertainty, conflicting evidence, and a `WEBSITE_FOUND` result that does not imply a qualified website opportunity.
- Re-verification contract tests covering social/menu-platform ownership preservation and the distinction between unreachable and nonexistent websites.

### Verified — 2026-10-10

- The Cactus record remains `NOT_YET_QUALIFIED`: no independently verified business-controlled professional website or source-backed poor/outdated/basic official-site condition was found.

### Not added — 2026-10-10

- No Cactus lead, project, content brief, build, deployment, contact, or outreach action.

### Verified — 2026-10-10

- Phase 17 confirmed there is no real Cactus lead, project, content brief, build, validation, deployment, preview URL, worker execution, or human-review record. The local output/deployment artifacts are fictional test data only; no preview review can begin.

### Verified — 2026-10-10

- Phase 16 reviewed the persisted Cactus evidence and directly observed the public Facebook page title without login. The remaining profile content was login-gated, so ownership, website quality, and opportunity status remain unverified; the real build gate remains blocked.

### Not added — 2026-10-10

- No real Cactus lead, project, content brief, build, validation, deployment, human-review record, contact, or outreach action.

### Added — 2026-10-08

- A dependency-free targeted public-data re-verification record helper with provenance, property classifications, prior-evidence preservation, and tests.
- One approved, review-only Cactus Restaurant audit event in ignored local lead data; it preserves source URLs and uncertainty without creating a lead, project, build, deployment, or outreach draft.

### Verified — 2026-10-08

- Cactus identity/address/phone are corroborated by several public listings, while the associated Google Business Profile and Facebook properties remain only likely official because direct ownership and quality could not be verified with the approved tool.

### Not added — 2026-10-08

- No Cactus contact, account, order, message, website build, deployment, project, or outreach action.

### Added — 2026-10-08

- Evidence-led website opportunity assessment, content briefs with verified/unknown/suggested separation, qualification history, and idempotent active-project creation.

### Verified — 2026-10-08

- All four Phase 13 real public-data records remain `NEEDS_REVIEW`; no real tracked lead, project, build, deployment, or outreach record was created.

### Added — 2026-10-08

- Bounded, review-only OpenStreetMap Overpass Lagos restaurant discovery adapter with provenance, real-data labeling, timeout, and one-request/five-candidate limits.

### Verified — 2026-10-08

- One approved public-data pilot returned four named candidates; no leads/projects, outreach, deployments, accounts, or messages were created.

### Added — 2026-10-08

- Full fictional local lifecycle integration test and dependency-free system health/readiness reports.

### Verified — 2026-10-08

- A fictional restaurant build can progress through validation, tracking, fictional preview recording, human review, and draft approval to `READY_TO_SEND` without delivery.

### Added — 2026-10-08

- Provider-neutral worker registry, capability-aware router, leases, execution history, failover/recovery, project mutation locks, stale-result protection, human takeover, and deterministic mock-worker tests.

### Not added — 2026-10-08

- No real agent/provider integration, worker host, n8n activation, credential, paid dependency, live deployment, or outreach delivery.

### Added — 2026-10-08

- Application-owned orchestration job ledger with idempotency keys, prerequisite gates, retries, stale-job recovery, dry-run records, and a provider-neutral worker contract.
- Optional fail-closed localhost API plus inactive, credential-free self-hosted n8n workflow examples. No server or workflow is activated by this change.

### Not added — 2026-10-08

- No n8n instance, external queue, database, worker registration, live source, deployment, production deployment, outreach delivery, or credential.

### Added — 2026-10-08

- Evidence-led, human-review-gated outreach preparation with revision history and no sending capability.

### Added — 2026-10-08

- Single-file local lead/project tracking with guarded state transitions, history preservation, deduplication, human review, and project snapshots.

### Added — 2026-10-08

- Preview-only Vercel deployment adapter with artifact-local deployment records, dry-run default, validation gate, Git SHA capture where available, and normalized CLI results.
- Deployment record schema and mocked Vercel CLI tests for success, failure, review gating, missing configuration, and source/build mismatch.

### Not added — 2026-10-08

- No Vercel project link, credential setup, live deployment, production deployment, Git commit, GitHub remote, push, or repository creation.

### Added — 2026-10-08

- Dependency-free Phase 6 static QA validator with severity findings, transparent scoring, PASS/FAIL/NEEDS_REVIEW results, and a local CLI.
- Validation contracts and fixtures covering links, images, CTAs, mobile metadata, accessibility landmarks, SEO basics, placeholders, suspicious claims, and potential secrets.

### Not added — 2026-10-08

- No browser automation, live URL checks, automatic repair, deployment, or external validation service.

### Added — 2026-10-08

- Provider-neutral Website Builder with deterministic fact-grounded content fallback, local build jobs, validation, and local preview artifacts.
- Build-job and generated-content schemas plus fictional qualified-lead build fixtures and tests.

### Not added — 2026-10-08

- No external AI model/provider, real-business build, deployment, hosting, contact flow, payment, outreach, or automation integration.

### Added — 2026-10-08

- Dependency-free reusable template system with deterministic category selection, shared semantic components, data-driven content, brand CSS variables, and local static rendering.
- Fictional salon and restaurant demos, template data schema, and template validation tests.

### Not added — 2026-10-08

- No AI content generation, real-business website generation, image collection, deployment, hosting, tracking, forms, or outreach.

### Added — 2026-10-08

- Fixture-only Research & Qualification Agent with source-backed facts, inferences, uncertainties, identity checks, website classifications, and recoverable job output.
- Research job schema, research configuration defaults, and documented qualification rules.

### Added — 2026-10-08

- Fixture-only, source-modular Scout Agent with a safe CLI and standard-library tests.
- Deterministic normalization, conservative deduplication, source/field provenance, and explainable initial qualification.
- Scout job-result schema and Scout settings defaults.

### Not added — 2026-10-08

- No live discovery source, browser/search integration, scraping, real business data, external service, dependency, deployment, or outreach capability.

### Added — 2026-10-02

- Phase 1 provider-neutral project foundation.
- Multi-agent operating guidance and handoff ledger.
- Future settings template and initial lead JSON Schema.
- Architecture boundaries, recoverable job lifecycle, and 13-phase roadmap.

### Not added

- No dependencies, API clients, database, AI-provider binding, n8n workflow, deployment, business data, or outreach capability.
