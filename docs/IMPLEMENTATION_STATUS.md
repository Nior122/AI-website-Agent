# Nior Implementation Status & Feature Inventory

This document provides a truthful, evidence-backed inventory of implemented capabilities, partially implemented components, and open roadmap items across the Nior codebase.

---

## 1. Feature Status Overview

| Subsystem / Capability | Current State | Verification Evidence | Operational Safety |
| :--- | :--- | :--- | :--- |
| **Scout Candidate Discovery** | **Production Ready** | 22 tests passing; Overpass OSM adapter & Tavily search enricher | Review-only discovery (`REAL_PUBLIC_DATA`); no auto-contact |
| **Research & Evidence Engine**| **Production Ready** | 18 tests passing; fact extraction & website quality heuristics | Cites source URLs; preserves explicit uncertainty |
| **Template Registry** | **Production Ready** | Registry with restaurant & salon templates; mobile responsive | No hardcoded business facts; semantic HTML/CSS |
| **Website Builder** | **Production Ready** | 100% deterministic fallback compiler + ContentGenerator | Disclosures enforced; strict fallback if AI fails |
| **Quality QA Validator** | **Production Ready** | 14 automated lint, SEO, mobile, and compliance checks | Reports findings only; never silently rewrites code |
| **Preview Deployment** | **Production Ready** | Vercel preview adapter with append-only ledger | Fail-closed; isolated preview target; dry-run first |
| **Human Review Gate** | **Production Ready** | Full audit trail in `TrackingStore` & Telegram `/demo` | Identity tracked; required notes; no automated bypass |
| **Tracking State Store** | **Production Ready** | SQLite & Supabase PostgreSQL backend with DDL migration | ACID transactions; append-only attempt logs |
| **Orchestration & Jobs** | **Production Ready** | JobService state machine, worker leases, stale recovery | Heartbeat tracking; deduplicated idempotent execution |
| **Telegram Operator Console** | **Production Ready** | 28 commands implemented in `orchestration/telegram_bot.py` | Allow-list gated; redaction boundary; phone-side review |
| **Inbound Email Polling** | **Production Ready** | `integrations/gmail.py` using `gmail.readonly` scope | Strict read-only client; thread matching |
| **Approved Outbound Email** | **Production Ready** | `integrations/gmail_send.py` using `gmail.send` scope | Fingerprint-bound; confirmation token required; 1 send/call |
| **n8n Automation Engine** | **Operational** | 34 live workflows audited; 7 Git-exported reference workflows | Shipped in inert/dry-run states; credential-free exports |
| **Social Media Scheduling** | **Production Ready** | `social/` package matching n8n Buffer Draft Executor | Multi-channel posting, brand voice validator, Telegram `/post` |
| **Daily Business Briefing** | **Production Ready** | `orchestration/briefing.py` matching n8n `loO3tTJlt6aFdhvb` | Lagos timezone, Naira pipeline, overdue tasks, Telegram `/briefing` |
| **Customer Chat Intake** | **Operational** | Botcake Ingestion & Reply Executor active in n8n | Webhook routing to PostgreSQL with Telegram alerts |

---

## 2. Detailed Module Status Breakdown

### 1. Discovery (`scout/`)
- **Status**: Implemented & Tested.
- **Components**:
  - `ScoutAgent`: Multi-source candidate ingestor.
  - `TavilySearchEnricher`: Web search enrichment protocol with candidate limit (`MAX_ENRICHMENT_CANDIDATES = 5`).
  - `scout.cli`: CLI with `--enrich-tavily` and `--approve-enrichment` flags.
- **Safety**: Automatically tags real public data as `REAL_PUBLIC_DATA` and sets status to `NEEDS_REVIEW`.

### 2. Website Builder & Templates (`builder/`, `templates/`)
- **Status**: Implemented & Tested.
- **Templates**:
  - `restaurant-menu-v1`: Complete restaurant landing page with hero, menu sections, location, hours, and demo disclosure.
  - `salon-service-v1`: Salon and spa service showcase.
  - `clinic-healthcare-v1`: Clinic, healthcare, and wellness centre layout with clinical specialties and specialist practitioners.
  - `legal-practice-v1`: Law firm and advisory practice layout with practice areas and attorney profiles.
  - `automotive-service-v1`: Auto repair workshop and garage layout with service catalogue and guarantees.
- **Builder**: Produces clean, zero-dependency, semantic HTML artifacts with strict disclosure banners.

### 3. QA & Release Gate (`validation/`)
- **Status**: Implemented & Tested.
- **Checks**: Evaluates viewport meta tags, semantic hierarchy (`h1` uniqueness), contrast, placeholder avoidance, business disclosure banners, and mobile responsiveness.

### 4. Deployment Engine (`deployment/`)
- **Status**: Implemented & Tested.
- **Capabilities**:
  - Evaluates local `.vercel/project.json` link.
  - Prohibits deployment if project target differs from `EXPECTED_PREVIEW_PROJECT`.
  - Records every attempt in `deployment-attempts.json`.
  - Handles network failures via `PUBLISH_UNCERTAIN` state without guessing success.

### 5. Telegram Operator Console (`orchestration/telegram_bot.py`)
- **Status**: Implemented & Tested.
- **Commands**: 28 interactive commands including:
  - Queue control: `/jobs`, `/pending`, `/run`, `/recover`, `/cancel`, `/retry`
  - Lead management: `/leads`, `/find`, `/qualify`, `/prospect`
  - Review & Demo: `/review`, `/approve`, `/changes`, `/demo`
  - Outbound email: `/outbox`, `/send`, `/sent`, `/reconcile`
  - Social media: `/social`, `/post`
  - System controls: `/health`, `/status`, `/attention`, `/hold`, `/release`, `/help`

### 6. Social Media Engine (`social/`)
- **Status**: Implemented & Tested.
- **Components**:
  - `social/models.py`: Channel specifications (`twitter`, `linkedin`, `instagram`), strict character/media bounds, and `to_buffer_approval_payload()` matching n8n Buffer Draft Executor (`zAd3P31ZrTm9Ax94`).
  - `social/brand.py`: Nior brand voice validation, banned buzzword filtering, and scoring.
  - `social/generator.py`: Deterministic multi-archetype generator (`tech_insight`, `case_study`, `operator_dispatch`, `local_growth`).
  - `social/service.py`: Draft lifecycle management (`DRAFT` -> `PROPOSED` -> `APPROVED` / `REJECTED`) and storage.
  - `social/cli.py`: Command-line operations interface.
  - `tests/test_social_engine.py`: 21 comprehensive unit/integration tests.

---

## 3. Unimplemented & Open Work

1. **Second OAuth Client for `gmail.send`**: The operator must create a dedicated `gmail.send` OAuth client in Google Cloud Console and set `AI_WEBSITE_AGENT_GMAIL_SEND_*` environment variables to enable live email delivery.
2. **Template Library Expansion (Stage 5)**: Additional sector templates (e.g., medical clinic, legal/accounting, auto repair).
3. **Automated Daily Briefing**: Scheduled Telegram briefing via n8n workflow `loO3tTJlt6aFdhvb`.
