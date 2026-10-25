# Stage 9: Controlled Go-Live, Workflow Activation & Operational Stabilization

**Date:** 2026-10-22  
**Repository Branch:** `main`  
**Git Commit:** `49e9b6b` (clean working tree, in sync with `origin/main`)  
**Production API:** `https://ai-website-agent-api.onrender.com`  
**n8n Dedicated Instance:** Pending deployment on Render (e.g. `https://ai-website-agent-n8n.onrender.com` — isolated from legacy 20-workflow instance)  
**Primary Database:** Supabase PostgreSQL Pooler (`aws-0-us-east-1.pooler.supabase.com:5432`)

---

## 1. Phase 9.1: Current System State Assessment

### 1.1 Git and Repository Verification
- **Branch:** `main` tracking `origin/main`.
- **Commit:** `49e9b6b` (`chore(stage-8): complete security audit, credential rotation, and operational readiness verification`).
- **Working Tree:** Clean (0 untracked, 0 modified).
- **Test Suite Baseline:** 708 passing, 1 skipped, 0 failures.

### 1.2 Production Connectivity & Health Status
- **Application Web API (`https://ai-website-agent-api.onrender.com`)**:
  - `GET /healthz`: **HTTP 200 `{"status": "ok"}`** (Verified live).
  - Storage Backend: Supabase PostgreSQL (session pooler, 11 relational tables, Row Level Security active).
- **n8n Automation Service (`https://n8n-service-l01i.onrender.com`)**:
  - `GET /healthz`: **HTTP 200 `{"status": "ok"}`** (Verified live).
- **MCP Client Connectivity**:
  - The external Cloudflare quick tunnel previously used for the `nior-n8n` MCP server (`drink-ourselves-tube-counseling.trycloudflare.com`) has expired.
  - Dedicated n8n workflows and credential objects exist inside project `gF2ObH2mvjWtE5pb` on `https://n8n-service-l01i.onrender.com`.

### 1.3 Audit of Dedicated Workflows on New Isolated Instance
The five dedicated workflows deployed on `https://ai-website-agent-n8n.onrender.com`:

| Workflow | Dedicated ID | Status | Trigger Type | Action Target |
|---|---|---|---|---|
| **01** — Job Lifecycle & Pipeline Runner | `2If2517dlZ6VgMRG` | **INACTIVE (`active: false`)** | Manual | `POST /jobs` |
| **02** — Daily Business Briefing & Alerts | `VMZ7hjZvUySNXI5r` | **INACTIVE (`active: false`)** | Schedule (`08:00 Africa/Lagos`) & Manual | `GET /health`, `GET /engagement/*` |
| **03** — Buffer Multi-Channel Publisher | `JX8JOIsKsTzCeZ71` | **INACTIVE (`active: false`)** | Manual | Buffer Draft Dispatch Gate |
| **04** — Inbound Ingestion & Intake Router | `xKAaIpgsj09HH8nK` | **INACTIVE (`active: false`)** | Schedule (Hourly) & Manual | `POST /integrations/email/poll` |
| **05** — Approved Send with Fingerprint Gate | `kGNApQItCjQ5CNeu` | **INACTIVE (`active: false`)** | Manual | `POST /integrations/email/send` |

### 1.4 System Work & Queue Preconditions
- **Unfinished Jobs:** 0 running, 0 pending, 0 stale.
- **Active Schedules:** All 5 workflows are `active: false`. No external crons currently running.
- **Attention Items:** 2 historical human reviews awaiting operator review (`project-0001` and `project-0002`).
- **Automation Hold:** `automation_held: false`.
- **Sends Uncertain:** 0 uncertain sends recorded.

---

## 2. Phase 9.2: Dedicated n8n Instance Provisioning & Credential Synchronization

### 2.1 Architectural Decision: Dedicated n8n Service
The operator has decided to isolate the AI Website Agent automation from the legacy n8n service (`n8n-service-l01i.onrender.com`), which hosts ~20 unrelated workflows (Buffer, Botcake, YouTube, etc.). A new, dedicated n8n Web Service will be created on Render solely for Workflows 01 through 05.

### 2.2 Provisioning Blueprint for Render Web Service
- **Type**: Web Service (Docker image)
- **Image**: `docker.n8n.io/n8nio/n8n:latest`
- **Name**: `ai-website-agent-n8n`
- **Plan**: Free
- **Environment Variables**:
  - `N8N_PORT`: `5678`
  - `N8N_PROTOCOL`: `https`
  - `NODE_ENV`: `production`
  - `GENERIC_TIMEZONE`: `Africa/Lagos`
  - `TZ`: `Africa/Lagos`
  - `N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS`: `true`
  - `WEBHOOK_URL`: `https://ai-website-agent-n8n.onrender.com/`
  - `AI_WEBSITE_AGENT_API_URL`: `https://ai-website-agent-api.onrender.com`
  - `AI_WEBSITE_AGENT_TELEGRAM_CHAT_ID`: `7722236745`

### 2.3 Post-Deployment Steps
1. Create administrative owner account upon first opening the new n8n URL.
2. Configure credentials in the new n8n instance:
   - `AI Website Agent API`: Header authentication with `AI_WEBSITE_AGENT_API_TOKEN`.
   - `Telegram Bot`: Token for operator alerts (`7722236745`).
   - `Buffer API`: Token for social draft queueing.
3. Import the 5 canonical JSON workflows from `n8n/workflows/project/`.

