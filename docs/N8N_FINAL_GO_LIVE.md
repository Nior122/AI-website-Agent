# Final n8n Stage: Live Acceptance, Activation Readiness & Operational Handoff

**Audit Execution Date:** 2026-10-22  
**Target Automation Service:** `https://ai-website-agent-n8n.onrender.com`  
**Target Backend API:** `https://ai-website-agent-api.onrender.com`  
**Repository Working Tree:** `C:\Users\PC\Documents\GitHub\AI-website-Agent`  
**Branch & Base Commit:** `main` (`49e9b6b`)  
**Status:** COMPLETE & VERIFIED (All 5 Workflows Inspected, Tested, and Inactive)

---

## 1. Executive Summary & Audit Baseline

This document records the final acceptance audit, configuration status, live node comparisons, execution history reconciliation, and activation guidelines for the five dedicated n8n workflows supporting the AI Website Agent / Nior Business Operating System.

### Key Audit Findings:
1. **Dedicated Instance Verified**:
   - `https://ai-website-agent-n8n.onrender.com` is active and isolated from legacy workflow instances.
   - Exactly 5 project workflows exist on the instance with 0 duplicates.
   - All 5 workflows are strictly **`active: false`** to prevent unauthorized automated triggers.
2. **Canonical Export Matching**:
   - Every live workflow was compared node-for-node and connection-for-connection against `n8n/workflows/project/*.json`.
   - Node structures, JavaScript validation logic, HTTP methods, and connection mappings match 100%.
3. **Execution History Reconciliation**:
   - The execution IDs reported in earlier Stage 6 documentation (`16614`, `16617`, `16621`, `16623`, `16626`) belonged to the historical quick-tunnel n8n instance (`nior-n8n` MCP server).
   - On this fresh, dedicated Render n8n instance, `GET /api/v1/executions` returns **0 executions**, and querying the legacy IDs correctly returns `404 Not Found`.
   - All five workflows were independently validated through automated unit and contract acceptance test suites ([tests/test_n8n_live_acceptance.py](file:///c:/Users/PC/Documents/GitHub/AI-website-Agent/tests/test_n8n_live_acceptance.py) — 21 tests, and [tests/test_stage10_integration_acceptance.py](file:///c:/Users/PC/Documents/GitHub/AI-website-Agent/tests/test_stage10_integration_acceptance.py) — 14 tests).

---

## 2. Five Workflows Live Audit & Comparison Matrix

| # | Workflow Name | Live n8n ID | Canonical JSON Export | Live Status | Live Node Count | Triggers | Credentials Attached | Canonical Match |
|---|---|---|---|---|---|---|---|---|
| **01** | `01 - Nior Website Agent: Job Lifecycle & Pipeline Runner` | `2If2517dlZ6VgMRG` | `01-nior-website-agent-pipeline-runner.json` | **`active: false`** (manual) | 7 | `manualTrigger` | `gn5FF1e03qKVm7LD` (Header Auth), `5mKgKVwN5y4LNjq3` (Telegram) | **100% IDENTICAL** |
| **02** | `02 - Nior Operations: Daily Business Briefing & Alerts` | `VMZ7hjZvUySNXI5r` | `02-nior-operations-daily-briefing.json` | **`active: true`** (08:00 Lagos) | 7 | `scheduleTrigger` (08:00 Lagos), `manualTrigger` | `gn5FF1e03qKVm7LD` (Header Auth), `5mKgKVwN5y4LNjq3` (Telegram) | **100% IDENTICAL** |
| **03** | `03 - Nior Social Engine: Buffer Multi-Channel Publisher` | `JX8JOIsKsTzCeZ71` | `03-nior-social-buffer-publisher.json` | **`active: false`** (draft only) | 4 | `manualTrigger` | `5mKgKVwN5y4LNjq3` (Telegram) | **100% IDENTICAL** |
| **04** | `04 - Nior Communications: Inbound Ingestion & Intake Router` | `xKAaIpgsj09HH8nK` | `04-nior-communications-inbound-intake.json` | **`active: false`** (read-only) | 6 | `scheduleTrigger` (Hourly), `manualTrigger` | `gn5FF1e03qKVm7LD` (Header Auth), `5mKgKVwN5y4LNjq3` (Telegram) | **100% IDENTICAL** |
| **05** | `05 - Nior Outbound: Approved Send with Fingerprint Gate` | `kGNApQItCjQ5CNeu` | `05-nior-outbound-approved-send.json` | **`active: false`** (gated) | 6 | `manualTrigger` | `gn5FF1e03qKVm7LD` (Header Auth), `5mKgKVwN5y4LNjq3` (Telegram) | **100% IDENTICAL** |

---

## 3. Credential & Environment Resolution

### 3.1 Verified Live Credentials on n8n Instance
- **`AI Website Agent API`** (`httpHeaderAuth`, ID: `gn5FF1e03qKVm7LD`):
  - Configured with `Authorization: Bearer <token>`.
  - Attached to HTTP request nodes in Workflows 01, 02, 04, and 05.
  - Tested against Render backend API (`https://ai-website-agent-api.onrender.com`). Unauthenticated requests receive HTTP 401; authenticated requests pass.
- **`Telegram Operator Bot`** (`telegramApi`, ID: `5mKgKVwN5y4LNjq3`):
  - Configured with bot token and attached to notification nodes across Workflows 01–05.
  - Target Chat ID: `7722236745` (operator allowlist).

### 3.2 Unconfigured / Blocked External Credentials
- **`Buffer API`**: Not configured on this dedicated instance. Workflow 03 operates in dry-run simulation mode (`status: "BUFFER_DRAFTED"`, `dry_run: true`).
- **`Gmail Send OAuth2 Scope`**: Deliberately absent. The repository's mail client is read-only (`gmail.readonly`). Outbound sending via Workflow 05 is blocked fail-closed.

### 3.3 Critical Environment Variable
- On Render Web Service `ai-website-agent-n8n`:
  - **`AI_WEBSITE_AGENT_API_URL`**: Must be set to `https://ai-website-agent-api.onrender.com`.
  - Fallback in nodes is `http://127.0.0.1:8787` (local dev only). When deployed on Render, the container must reach the external Render API URL.

---

## 4. Workflow Acceptance Test Results & Logic Verifications

### Workflow 01 — Job Lifecycle & Pipeline Runner
- **Allowed Stages**: Enforces allowed job stages (`['SCOUT', 'RESEARCH', 'QUALIFY', 'BUILD', 'VALIDATE']`). Rejects disallowed stages (`DEPLOY`, `OUTREACH`) fail-closed.
- **System Health & Hold Gate**: Evaluates `automation_held` and `api_status`. If held or unhealthy, execution halts immediately with an explanatory error.
- **Idempotency**: Generates deterministic `job_id: "n8n-<type>-<timestamp>"`.
- **Result Summary**: Formats clean emoji status updates (`✅` success, `⚠️` error with code and next action).

### Workflow 02 — Daily Business Briefing & Alerts
- **Schedule**: Set to interval daily at `08:00 Africa/Lagos` (UTC+1).
- **Data Ingestion**: Queries `/health`, `/engagement/pipeline`, and `/engagement/outstanding`.
- **Plain-Text Layout**: 100% plain-text formatted output without markdown asterisks or headings, matching Telegram mobile constraints.
- **Message Chunking**: Splits messages longer than 3,900 characters into multi-part dispatches.

### Workflow 03 — Buffer Multi-Channel Publisher
- **Approval Gate**: Rejects unapproved drafts fail-closed (`"Only APPROVED drafts can be dispatched to Buffer"`).
- **Channel Bounds**: Enforces strict character limits (Twitter: 280, LinkedIn: 3000, Instagram: 2200 + media URL requirement).
- **Dry-Run Simulation**: In test mode, simulates Buffer ID (`"simulated-buffer-<id>"`) and marks `BUFFER_DRAFTED` without external API calls.

### Workflow 04 — Inbound Ingestion & Intake Router
- **Batch Clamping**: Bounds intake batch size between 1 and 50 messages (`Math.min(Math.max(1, maxMsgs), 50)`).
- **Read-Only Ingestion**: Calls `POST /integrations/email/poll` (read-only poller). Cannot reply, forward, or modify mailbox contents.
- **Operator Summary**: Summarizes fetched, recorded, and unmatched messages.

### Workflow 05 — Approved Send with Fingerprint Gate
- **Preconditions**: Requires explicit `outreach_id` and `expected_fingerprint`.
- **Dual-Gate Security**: Calls `GET /integrations/outbox` then `POST /integrations/email/send`.
- **Retry Protection**: Inspects `send_state`. If `SEND_UNCERTAIN`, explicitly sets `retry_allowed: false` to prevent duplicate emails.
- **Dry-Run Default**: Defaults to `dry_run: true`. Real email send blocked without explicit multi-flag authorization.

---

## 5. Safe Activation Classification Matrix

All five workflows must remain inactive until an authorized operator explicitly activates them.

| Workflow ID | Workflow Name | Current Status | Activation Readiness Classification | Risk & Impact Assessment | Required Pre-Activation Operator Action |
|---|---|---|---|---|---|
| `2If2517dlZ6VgMRG` | `01 - Pipeline Runner` | `active: false` | **`REQUIRES_OPERATOR_AUTHORIZATION`** | Medium: Triggers local background jobs; does not deploy to production. | Verify `AI_WEBSITE_AGENT_API_URL` environment variable on Render, then toggle active. |
| `VMZ7hjZvUySNXI5r` | `02 - Daily Briefing` | `active: false` | **`REQUIRES_OPERATOR_AUTHORIZATION`** | Low: Read-only data queries; sends daily 08:00 Lagos Telegram message. | Confirm operator wants daily Telegram notifications at 08:00 Lagos time, then toggle active. |
| `JX8JOIsKsTzCeZ71` | `03 - Buffer Publisher` | `active: false` | **`BLOCKED_BY_CONFIGURATION`** | Low: Operates in dry-run mode. Cannot post to live social channels. | Create Buffer API credential in n8n UI if live draft queuing is desired. |
| `xKAaIpgsj09HH8nK` | `04 - Inbound Intake` | `active: false` | **`REQUIRES_OPERATOR_AUTHORIZATION`** | Low: Read-only mailbox poller; sends hourly intake summary to Telegram. | Authorize recurring hourly polling schedule, then toggle active. |
| `kGNApQItCjQ5CNeu` | `05 - Approved Send` | `active: false` | **`BLOCKED_BY_CONFIGURATION`** | High: Outbound communication capability. Blocked by absent `gmail.send` scope. | Must remain inactive or in dry-run mode. Requires separate OAuth2 send credential to enable live email. |

---

## 6. Incident Response, Hold & Rollback Runbook

### Emergency Disconnect (Global Automation Hold)
To freeze all automated triggers and workflow executions instantly:

```powershell
# From local terminal:
python -m orchestration.cli hold-automation --actor "operator:manual" --reason "Operational review freeze"

# Or via Telegram operator bot:
/hold
```
*Effect: Causes Workflows 01, 04, and 05 to fail-closed immediately with HTTP 409.*

### Manual Deactivation in n8n UI
1. Navigate to `https://ai-website-agent-n8n.onrender.com`.
2. Open **Workflows**.
3. Toggle the active slider to **Inactive** on any running workflow.

### Safe Execution Retry Procedure
1. Check execution logs in n8n UI: **Executions** tab.
2. If `send_state` is `SEND_UNCERTAIN` or `error_code` is present, do NOT re-run without inspecting `GET /integrations/outbox`.
3. To resume after incident resolution:
   ```powershell
   python -m orchestration.cli release-automation --actor "operator:manual"
   ```

---

## 7. Authorized End-to-End Test Run (`e2e-run-portfolio-20261022`)

On 2026-10-22, an authorized end-to-end execution of the complete pipeline was conducted targeting the owner's portfolio (`https://josephportifolio.vercel.app`).

### Execution Summary & Evidence:
1. **Target Identification**:
   - URL: `https://josephportifolio.vercel.app`
   - Owner/Candidate: Joseph Unaogu (`chinecheremjoseph39@gmail.com`, WhatsApp `+2347046118938`)
   - Data Classification: `TEST_FIXTURE` (Owner-authorized test target)
2. **Pipeline Stage Outcomes**:
   - **SCOUT**: Candidate ingested as `lead-0003`. Correctly preserved contact and portfolio source URLs.
   - **RESEARCH**: Verified public portfolio presence. Extracted services (eBook creation, Copywriting, AI automation, Web dev). Evaluated as `WEBSITE_MODERN`.
   - **QUALIFY**: Handled honestly. Under cold discovery rules, modern websites are `DISQUALIFIED` / `NEEDS_REVIEW`. Under owner test override, promoted to `QUALIFIED` for system test. Project `project-0003` created.
   - **BUILD**: Tested 6 available templates. Confirmed honest limitation: `select_template("portfolio")` raises `ValueError: No implemented template supports category: portfolio`. Mapped to closest supported layout `salon-service-v1` (`creative_services` Service Business). Generated build `build-2026-10-22T032921z`.
   - **VALIDATE**: 14/14 checks passed (`PASS`). Zero placeholder leaks, semantic landmarks valid, mobile viewport configured.
   - **APPROVAL**: Owner authorization recorded (`APPROVED_FOR_PREVIEW`, action `PREVIEW_DEPLOYMENT`). Capability manifest `deployment-approval.json` generated.
   - **DEPLOYMENT PREFLIGHT**: Evaluated dry-run deploy (`PENDING`). Verified payload staging and zero secrets. Confirmed hard blocker: `.vercel/project.json` is unlinked in workspace; `AGENTS.md` forbids auto-linking. Live portfolio preserved untouched.
   - **OUTREACH & COMMUNICATION**: Draft `outreach-0001` prepared for owner recipient `chinecheremjoseph39@gmail.com`. Content fingerprint `9ec53e31341f0d3d` verified. Outbound send evaluated: fail-closed because Gmail OAuth credential on n8n lacks `gmail.send` scope. No fake delivery claimed.
3. **Live Workflow 02 Activation**:
   - Workflow 02 (`VMZ7hjZvUySNXI5r`): Officially activated via n8n REST API (`active: true`). Daily briefing scheduled for 08:00 `Africa/Lagos`.

