# Agent Operating Guide

This repository, not any chat or particular AI model, is the source of truth. Codex, Claude Code, and future agents may all work here.

## Before changing anything

1. Inspect the working tree with `git status --short --branch` and inspect relevant files.
2. Read `README.md`, `ARCHITECTURE.md`, and `TASKS.md`; read the schema/configuration before changing related contracts.
3. Preserve existing functionality and unrelated user changes.
4. Identify the smallest safe change. Do not rewrite working areas merely for style.

## Engineering rules

- Make small, understandable, reviewable changes.
- Avoid dependencies, frameworks, APIs, databases, SaaS products, and infrastructure unless they are necessary for the approved task. Document why a new dependency is needed, its free alternative, and whether it can be avoided.
- For templates, keep business data, content, branding, template metadata, and layout components separate. Add a template through `templates/registry.py`, include fictional demo data and tests, and never embed real-business facts in reusable components.
- For website builds, accept only `QUALIFIED` research records; preserve verified facts, content, brand, and assets as separate inputs. Do not fabricate copy, testimonials, claims, or assets. Keep AI adapters behind `builder.content_generator.ContentGenerator`, retain the deterministic fallback, and test both successful artifacts and failure recovery.
- Treat `validation/` as the release gate for future deployment. Never promote a `FAIL` artifact; a `NEEDS_REVIEW` artifact requires explicit approval. Validators report findings and repair suggestions only—do not let them rewrite generated sites automatically.
- For preview deployment, use `deployment/` and its dry run first. Require a matching build manifest, Phase 6 PASS, and an already linked approved Vercel project. Never use `--prod`, create/link a Vercel project automatically, commit/push automatically, or place credentials in commands, files, manifests, or reports.
- Use `tracking.TrackingStore` as the local state source of truth. Preserve historical builds, validations, and deployments; use its guarded transitions and never edit state JSON manually to bypass a review or validation gate.
- Outreach is draft-only. Use `outreach.OutreachService` only after its eligibility gate; preserve revisions and human review. Never add sending, login, message transport, or contact-form behavior without explicit future approval.
- Treat `orchestration.JobService` and `TrackingStore` as the source of truth for jobs; n8n may coordinate only. Require caller-provided idempotency IDs, honor prerequisite gates and dry-run, and never put a secret in a workflow export. Do not start the optional API or n8n workflows without explicit authorization.
- Before changing a project, inspect its jobs, worker leases, execution history, and project lock. Never claim work owned by another healthy worker, overwrite its artifacts, force-push, or bypass a stale-result/version check. Use `WorkerRegistry` capabilities and normalized `WorkerResult` records; real worker adapters require explicit approval and must not contain credentials.
- Real discovery data is review-only unless the user separately authorizes later stages. Keep it out of fixtures and Git, label it `REAL_PUBLIC_DATA`, preserve the source URL, enforce the approved source/target/request/candidate limits, and never infer that a missing directory field proves a business fact. Do not create projects, build sites, or contact a discovered business automatically.
- Use `opportunities.assess_opportunity` for the Scout-to-builder handoff. Preserve verified, unknown, and suggested facts separately. A directory listing without an official website field is `WEBSITE_UNKNOWN`, never `NO_WEBSITE`; it requires human review before a real lead, project, or build is created.
- Never delete working code, reset history, overwrite user work, or alter Git history without explicit approval.
- Test changes proportionately before declaring completion. At minimum review `git diff --check` and the relevant tests/validation.
- Explain significant changes and update `README.md`, `ARCHITECTURE.md`, `TASKS.md`, and `CHANGELOG.md` when applicable.
- Use Git as a safety layer: inspect → modify → test → review diff → commit. Never push, deploy, or create production resources without explicit authorization.
- Do not expose, request, commit, log, or hardcode credentials, API keys, cookies, passwords, or session tokens. Use ignored local environment files only when an approved implementation needs them.

## Business-data and outreach safety

- Never fabricate business facts, contacts, images, affiliations, qualifications, or public information. Record source URLs and uncertainty.
- Keep directly supported facts, reasonable inferences, and unresolved uncertainties separate. A website evaluation must identify its supporting observations; an unverified website must remain `UNKNOWN` or be sent for review.
- Treat both no-professional-website and outdated/poor-website businesses as possible future categories; do not assume a website exists.
- Do not scrape, contact, message, email, call, submit forms, or send WhatsApp/Instagram/Facebook outreach without explicit authorization.
- Future outreach must identify the sender accurately, be personalized from legitimate public information, and must not impersonate customers or submit fake inquiries.
- Never make purchases, subscriptions, paid accounts, or paid-service changes without explicit authorization.
- Never modify production infrastructure recklessly; use dry runs and reviewable plans where possible.

## Handoff protocol

Before ending work, leave the project recoverable for another agent:

1. Update the "Current handoff" table in `TASKS.md` with phase, task, next task, files touched, checks performed, known issues, and deployment status.
2. Update `CHANGELOG.md` for user-visible or structural changes.
3. State assumptions, decisions, and anything requiring approval in the final report.
4. Do not claim an external action occurred unless it is recorded and verifiable.

If work is interrupted, a future agent should start by reading `TASKS.md`, checking `git status`, and reviewing the latest diff before resuming.
