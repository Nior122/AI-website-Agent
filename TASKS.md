# Work Ledger and Agent Handoff

Keep this file concise. It is the shared handoff record, not a replacement for Git history or an issue tracker.

## Current handoff

| Field | Current value |
| --- | --- |
| Current phase | Phase 26 — Pitstop Lagos: Verified Opportunity to Real-Business Website Draft (Corrected) |
| Current task | Phase 26 Correction: Complete elimination of fictional demonstration remnants and generation of verified Pitstop Lagos website draft (build-2026-10-12T044011z) |
| Status | Completed: Diagnosed root causes: hardcoded shared-meal illustration and 'Made for the table' caption in components.hero(); restaurant template routing solely to menu_items and ignoring verified services; silent aliasing of services to menu_items in website_builder; validator lacking fixture leak detection. Corrected pipeline across templates/components.py, templates/renderer.py, templates/styles.css, builder/website_builder.py, and validation/quality_validator.py. Generated clean, isolated artifact at `output/lead-0002/build-2026-10-12T044011z/index.html` passing static QA at 100/100 (14/14 checks passed). Rendered verified offerings (Healthy-Inspired Cafe & Dining, Cycling Community Rides, Wattbike Training Studio, Bicycle Service & Repair) without food fabrication. Included verified email (info@pitstoplagos.com), phone, Instagram, and address. Omitted unverified dishes, prices, hours, reviews, and booking forms. Assigned build-2026-10-12T044011z to project-0002 in TrackingStore at `READY_FOR_REVIEW` with `PENDING` human review. Zero deployments or outreach. 117 unit tests pass. |
| Next task | Human review inspection of the corrected local website draft for Pitstop Lagos (output/lead-0002/build-2026-10-12T044011z/index.html) in TrackingStore. Await explicit operator instructions before any future deployment or outreach consideration. Do not start Phase 27 automatically. |
| Files currently being modified | `TASKS.md`, `CHANGELOG.md`, `templates/components.py`, `templates/renderer.py`, `templates/styles.css`, `builder/website_builder.py`, `validation/quality_validator.py`, `tests/test_phase26_real_draft.py`, `data/leads/phase26-correction-pitstop-lagos-20261012.json` |
| Tests/checks performed | 117 unit tests passed (0 failures, 0 skipped); `python -m compileall` passed clean; `git diff --check` passed clean; static QA passed at 100/100; browser subagent noted internal endpoint failure; historical fictional preview and audit records intact. |
| Deployment status | Fictional preview remains live at `https://ai-website-agent-preview.vercel.app` (lead-0001 / project-0001). Corrected Pitstop Lagos draft is local-only at `output/lead-0002/build-2026-10-12T044011z/index.html`. Real-business deployment remains strictly blocked. |
| Known issues | None. Real website draft is fact-grounded, validated, and isolated without any fictional demo artifacts. |
| Approval required | Human review and explicit operator approval of the corrected local Pitstop Lagos draft before any preview deployment or outreach action. |

## Completed

- [x] Inspected the initial workspace and preserved its `.git` directory.
- [x] Added provider-neutral project documentation and agent guidance.
- [x] Added a non-secret future settings template.
- [x] Added an initial, documented lead-record JSON Schema.
- [x] Defined a phased roadmap and recoverable job vocabulary.
- [x] Added a fixture-only, source-modular Scout Agent with deterministic normalization and conservative deduplication.
- [x] Added explainable initial website-opportunity qualification and recoverable Scout job output.
- [x] Added a fixture-only Research & Qualification Agent with evidence provenance, identity checks, website evaluation, and explicit uncertainty handling.
- [x] Added a reusable static template registry, shared semantic components, two fictional demos, and local rendering tests.
- [x] Added a local Website Builder with grounded deterministic content, validation, and recoverable build results.
- [x] Added a static QA gate with severity findings, PASS/FAIL/NEEDS_REVIEW results, and no automatic repair.
- [x] Added a preview-only Vercel deployment adapter with PASS validation gating, dry run, local deployment records, and mocked CLI tests.
- [x] Added draft-only outreach packages gated on qualification, build, PASS validation, preview deployment, and human project review; records preserve source-backed personalization and revisions.
- [x] Added application-owned orchestration jobs with source-of-truth persistence, idempotency, prerequisite gates, retry/recovery, dry-run, and an interchangeable worker contract.
- [x] Added inactive, self-hosted n8n workflow examples and a fail-closed optional localhost API without credentials or worker activation.
- [x] Added provider-neutral worker registry, routing, leases, execution audit, failover, project locks, human takeover, and mock-worker simulation tests.
- [x] Added fictional full-system lifecycle coverage and local health/readiness diagnostics; verified the pipeline stops at human-approved `READY_TO_SEND` with no delivery capability.
- [x] Added and tested an approved, bounded OpenStreetMap Overpass source adapter; performed one review-only Lagos restaurant run yielding four named real public-data candidates with source evidence.
- [x] Added evidence-led opportunity qualification, content briefs, qualification history, and active-project idempotency; reviewed all four real pilot candidates as `NEEDS_REVIEW` with no additional network requests.
- [x] Performed one approved, review-only targeted public re-verification of Cactus Restaurant; recorded public-source provenance, property classifications, and unresolved ownership/quality without creating a real lead or project.
- [x] Applied the Phase 16 gate to persisted Cactus evidence; a public Facebook title check added no sufficient ownership/quality evidence, so the real preview build was correctly blocked.
- [x] Verified during Phase 17 that no real preview or downstream review record exists; preserved the qualification block and did not treat fictional test artifacts as real deployment evidence.
- [x] Performed one approved, bounded Phase 18 public verification of Cactus; distinguished website discovery from business control, recorded new web-property evidence, and preserved `NOT_YET_QUALIFIED` without creating a real lead/project.
- [x] Added an offline, idempotent backlog selector that delegates to the existing qualification policy and preserves unresolved historical decisions as safety vetoes.
- [x] Performed a bounded Phase 19 review of the four existing candidates and five public-search candidates. No candidate qualified; no real tracking or downstream record was created.
- [x] Performed a one-candidate Phase 20 ownership and public condition review for Calabar Kitchen. Anchored the finding to the Ikeja main branch, retained `LIKELY_OFFICIAL` rather than asserting legal control, and preserved the no-build qualification block.
- [x] Demonstrated the website builder end-to-end on fictional test business Lagos Fresh Kitchen: generated structured content brief, selected restaurant-menu-v1 template, built grounded responsive website, passed 14/14 QA checks (PASS, score 100), verified safe failure of unlinked preview deployment, and recorded PENDING human review state in TrackingStore.
- [x] Verified local website inspection procedure, artifact integrity, and sub-second repeatability; fixed build/validation/deployment ID collision defects; documented Vercel preview deployment prerequisites and fail-closed safety.
- [x] Completed Phase 22B on the fictional Lagos Fresh Kitchen fixture: redesigned the reusable restaurant template, generated a new local artifact, passed static QA, and performed responsive browser checks without deployment or outreach.
- [x] Completed Phase 23 safe local work: generated and inspected a fresh fictional build; audited Vercel CLI/project/authentication preconditions; ran the deployment adapter's fail-closed path; and hardened exact preview-host validation without making an external deployment request.
- [x] Completed Phase 24 deployment: configured dedicated `ai-website-agent-preview` project, resolved skipped symlink test portably, deployed staged public payload (`index.html` only) to Vercel preview without `--prod`, verified live HTTPS URL (`https://ai-website-agent-preview.vercel.app`), and confirmed anonymous accessibility with full responsive content.
- [x] Completed Phase 25 real-business discovery and qualification: audited pipeline bottlenecks; added fallback endpoint support to OverpassRestaurantSource; conducted bounded research on 5 unreviewed independent Lagos restaurants with public evidence; identified Pitstop Lagos with confirmed production defect (broken markup and 70+ hidden casino backlinks); qualified candidate under unmodified `assess_opportunity()` (score 95, P2, high confidence); created `lead-0002` in TrackingStore as `REAL_PUBLIC_DATA` while strictly holding projects/builds/deployments/outreach; preserved historical audits and fictional preview intact; passed 103 unit tests.
- [x] Completed Phase 26 real-business website draft: reverified live website pitstoplagos.com defects; established strict fact ledger omitting unverified dishes, prices, hours, reviews, and booking forms; reused restaurant-menu-v1 template with 'PL' initials, preview draft eyebrow, and uncommissioned evaluation disclosure; generated isolated draft build at `output/lead-0002/build-2026-10-12T041042z/index.html`; passed static QA at 100/100 (14/14 checks passed); hardened validator against casino spam, unparsed shortcodes, and off-screen styles; recorded project-0002 in TrackingStore at `READY_FOR_REVIEW` with `PENDING` human review; zero deployments or outreach; passed 112 unit tests.
- [x] Initialized Git repository on `main` branch, verified `.gitignore` safety boundaries (excluding secrets, credentials, environment files, build artifacts, tracking state, and discovery data), created public GitHub repository `AI-website-Agent` (`https://github.com/Nior122/AI-website-Agent`), and pushed repository code and documentation.

## Open work

- [ ] Select approved discovery/data-source policy before collecting any business data.
- [ ] Approve a future live source adapter before implementing or running it.
- [ ] Approve a further candidate-specific public research scope only if the existing Cactus record remains insufficient for a human decision.
- [ ] Approve the Phase 5 data/customization boundary before any real-business website generation.
- [ ] Approve a future AI content-provider adapter before enabling model-generated content.
- [ ] Approve a browser-testing tool before adding computed-layout, overflow, or live-link checks.
- [ ] Provide/approve an existing Vercel preview project link before any actual preview deployment.
- [ ] Select persistent storage only when a future scale or retention decision requires it.
- [ ] Approve outreach delivery policy, sender identity, channel-specific compliance, and any future integration before adding a delivery capability.
- [ ] Approve a worker-host design and any n8n-to-application networking before registering or running a real worker.
- [ ] Select a multi-process-safe persistent store/lock before scaling claims beyond one local worker process.
- [ ] Review the local ignored pilot result (`data/leads/overpass-lagos-restaurants-review-20261008.json`) before approving further research or expansion.
- [ ] Approve a human decision on the Cactus evidence before creating any real tracked lead/project; do not treat incomplete direct ownership verification as proof of a website opportunity.

## Decision log

| Decision | Rationale | Date |
| --- | --- | --- |
| Use JSON example configuration and JSON Schema first | Portable, inspectable, provider/database neutral, and requires no runtime dependency | 2026-10-02 |
| Keep real lead data out of the initial repository | No collection is authorized; avoids privacy and accidental data retention | 2026-10-02 |
| Use documentation as the initial handoff mechanism | Lightweight and sufficient before jobs, storage, or orchestration exist | 2026-10-02 |
| Begin Phase 2 with a fixture-only Python standard-library pipeline | Enables safe end-to-end testing with no accounts, dependencies, network requests, or real business data | 2026-10-08 |
| Use deterministic matching rather than fuzzy duplicate matching | Keeps merges explainable and avoids incorrectly combining different businesses | 2026-10-08 |
| Keep Phase 3 website evaluation separate from Scout triage | Preserves the Phase 2 contract while making evidence-backed research evaluation authoritative | 2026-10-08 |
| Use dependency-free static HTML/CSS templates first | The repository had no frontend runtime; this keeps template behavior portable, fast, and testable without installing a framework | 2026-10-08 |
| Start with service-led and menu-led templates | They cover two materially different local-business information architectures without creating premature near-duplicates | 2026-10-08 |
| Make deterministic content generation the default Builder path | Local builds remain functional, reviewable, and provider-independent when no AI provider is approved or available | 2026-10-08 |
| Use static validation before browser automation | Existing artifact HTML supports reliable dependency-free checks; browser-based layout/live-link verification needs explicit tool approval | 2026-10-08 |
| Require an existing Vercel project link for preview deployment | Prevents uncontrolled project creation and keeps the preview deployment strategy explicit and reviewable | 2026-10-08 |
| Keep outreach preparation in the existing local tracking state | One append-history state file preserves traceability without prematurely choosing a database or adding a second persistence system | 2026-10-08 |
| Keep orchestration state inside TrackingStore | n8n can be restarted or removed without losing the authoritative job/lead/project history | 2026-10-08 |
| Make the optional API fail closed | A local token from the environment and localhost bind avoid an unauthenticated job trigger; no token is committed | 2026-10-08 |
| Keep Phase 11 worker coordination local and single-process | The existing JSON state store avoids new dependencies but cannot safely arbitrate competing hosts; configuration defaults to one worker process pending an approved storage decision | 2026-10-08 |
| Start live discovery with one bounded Overpass pilot | The user approved Lagos restaurants, five candidates maximum, and review-only output; this creates inspectable provenance without creating projects or contacting businesses | 2026-10-08 |
| Treat single-source listing gaps as unknown | A missing website field in OpenStreetMap does not establish a website opportunity; all real pilot records require additional evidence and human review | 2026-10-08 |
| Retain targeted real-data research as a local audit event | The approved Cactus check had to preserve Phase 13/14 history, distinguish directories/social/likely-official properties, and avoid a premature lead/project or website build | 2026-10-08 |
| Preserve a login-gated direct page as limited evidence | The visible Cactus Facebook title corroborates a likely social association but cannot establish ownership or a website opportunity without profile content; no login or bypass is permitted | 2026-10-10 |
| Do not review fictional artifacts as real business previews | The only local build/deployment files are labeled fictional test output and cannot satisfy any Cactus build, validation, deployment, or human-review gate | 2026-10-10 |
| Website discovery is not ownership proof | Phase 18 found a likely social profile and platform-hosted menu path, but neither establishes control of a professional site; an unverified Lovable-hosted page is excluded from quality assessment | 2026-10-10 |
| Keep backlog selection offline and policy-delegating | Phase 19 needed repeatable selection without creating a second qualification policy; the helper invokes `assess_opportunity()`, excludes fictional data, and lets unresolved historical decisions block promotion | 2026-10-10 |
| Treat an Overpass timeout as a research limitation | The one permitted request returned HTTP 504 and was not retried; a five-candidate public-search fallback remained bounded and does not make missing information affirmative evidence | 2026-10-10 |
| Require exact branch evidence before ownership review | Phase 20 found multiple similarly named/location variants. Only the Ikeja branch had a repeated address, phone, and domain association, so the public website review does not generalize to other locations | 2026-10-11 |
| Do not convert a missing web menu into a defect automatically | Calabar Kitchen's site has no public menu destination, but the existing policy does not establish that all restaurants must publish one. It remains a possible modernization consideration, not a qualification condition | 2026-10-11 |
| Use clearly labeled fictional fixtures for builder proof | Phase 21 demonstrated the builder end-to-end on "Lagos Fresh Kitchen — FICTIONAL TEST BUSINESS" (`TEST_FIXTURE`) to isolate technical builder capability from real-business qualification gates | 2026-10-12 |
| Enforce fail-closed preview deployment without unapproved configuration | Missing `.vercel/project.json` cleanly blocks preview deployment; no credentials or simulated URLs may be fabricated | 2026-10-12 |
| Enforce stage prerequisites before updating human review | `TrackingStore.record_review()` validates required stage before mutating review status, ensuring human review cannot silently approve an undeployed site | 2026-10-12 |
| Scope local preview serving to build directory on localhost | `builder.cli --serve` and `http.server` bind to 127.0.0.1 and restrict serving to the build output directory to prevent public network exposure and require zero dependencies | 2026-10-12 |
| Guarantee unique sub-second build, validation, and deployment IDs | Appends collision counters to `build_id` when timestamp directories exist and derives `validation_id`/`deployment_id` from `build_id`, preventing state overwrite during rapid pipeline reruns | 2026-10-12 |
| Keep fictional contact details non-actionable | `TEST_FIXTURE`/`demo` output must demonstrate a complete journey without presenting fake phone, WhatsApp, map, or social destinations as live services; real records retain their contact validation gate | 2026-10-12 |
| Parse Vercel preview URLs by host, not substring | Only HTTPS hosts ending exactly in `.vercel.app` can be recorded as preview URLs; lookalike or non-HTTPS output must fail closed | 2026-10-12 |
| Cross-platform Vercel CLI binary resolution | Use `shutil.which('vercel')` to find npm `.cmd`/`.ps1` wrappers on Windows without requiring `shell=True` | 2026-10-12 |
| Explicitly target linked project ID during deployment | Supply `--project <projectId>` when deploying a subpath to prevent Vercel CLI from attempting project creation on subdirectory paths | 2026-10-12 |
| Stage an allowlisted public deployment payload | The internal artifact directory has manifests and audit data; copying only `index.html` and safely resolved local assets prevents accidental publication and fails closed on missing, traversing, hidden, symlinked, or secret-bearing files | 2026-10-12 |
| Lock preview deployment to the dedicated project name | A historical church link caused a failed Vite build and must never be reconfigured or redeployed by this system; the adapter requires `ai-website-agent-preview` before it invokes Vercel | 2026-10-12 |

## Update template

When resuming or handing off, update the Current handoff table and add completed work, known issues, checks, and any decision requiring approval. Never remove prior decisions; append corrections when necessary.
