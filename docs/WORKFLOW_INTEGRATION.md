# Dedicated n8n Workflow Integration Guide

This document records the five dedicated n8n workflows built, deployed, and verified for the AI Website Agent / Nior Business Operating System via the connected `nior-n8n` MCP server.

---

## 1. Deployed Workflows Summary

All five workflows are deployed in personal project `gF2ObH2mvjWtE5pb` and kept **INACTIVE (`active: false`)** until explicit operator activation.

| # | Workflow Name | Dedicated n8n ID | Trigger Type | Primary Endpoint / Action | Credentials Attached | Activation Readiness Classification |
|---|---|---|---|---|---|---|
| **01** | `01 - Nior Website Agent: Job Lifecycle & Pipeline Runner` | `2If2517dlZ6VgMRG` | Manual Trigger | `POST /jobs` (with `run: true`) | `gn5FF1e03qKVm7LD` (Header Auth), `5mKgKVwN5y4LNjq3` (Telegram) | **`REQUIRES_OPERATOR_AUTHORIZATION`** |
| **02** | `02 - Nior Operations: Daily Business Briefing & Alerts` | `VMZ7hjZvUySNXI5r` | Schedule (08:00 Africa/Lagos) & Manual | `GET /health`, `GET /engagement/pipeline`, `GET /engagement/outstanding` | `gn5FF1e03qKVm7LD`, `5mKgKVwN5y4LNjq3` | **`REQUIRES_OPERATOR_AUTHORIZATION`** |
| **03** | `03 - Nior Social Engine: Buffer Multi-Channel Publisher` | `JX8JOIsKsTzCeZ71` | Manual Trigger | Buffer Dispatch Gate (`saveToDraft: true`, dry-run simulated) | `5mKgKVwN5y4LNjq3` (Telegram) | **`BLOCKED_BY_CONFIGURATION`** (Missing live Buffer credential) |
| **04** | `04 - Nior Communications: Inbound Ingestion & Intake Router` | `xKAaIpgsj09HH8nK` | Schedule (1 hr) & Manual | `POST /integrations/email/poll` (read-only intake) | `gn5FF1e03qKVm7LD`, `5mKgKVwN5y4LNjq3` | **`REQUIRES_OPERATOR_AUTHORIZATION`** |
| **05** | `05 - Nior Outbound: Approved Send with Fingerprint Gate` | `kGNApQItCjQ5CNeu` | Manual Trigger | `GET /integrations/outbox`, `POST /integrations/email/send` | `gn5FF1e03qKVm7LD`, `5mKgKVwN5y4LNjq3` | **`BLOCKED_BY_CONFIGURATION`** (Missing `gmail.send` scope) |

---

## 2. Detailed Workflow Architecture & Node Specifications

### Workflow 01: Job Lifecycle & Pipeline Runner (`YaH7bhvuxxnnH7yt`)
- **Trigger**: `Manual Trigger` (parameters: `job_type`, `dry_run`, `priority`, `input`, optional `job_id`).
- **Validation Node (`Validate Input & Build Request`)**: Enforces allowed job stages (`SCOUT`, `RESEARCH`, `QUALIFY`, `BUILD`, `VALIDATE`). Rejects unauthorized stages (`DEPLOY`, `OUTREACH`). Generates deterministic idempotent `job_id` (`n8n-<type>-<execution_id>`).
- **Health Gate (`Check System Health` & `Evaluate Health & Hold`)**: Calls `GET /health`. Strictly refuses execution if `automation_held: true` or `api_status != "OK"`.
- **Execution Node (`Create & Run Job`)**: Calls `POST /jobs` with `run: true`.
- **Result Inspection Node (`Inspect Execution Result`)**: Evaluates `status`, `recoverable`, `error_code`, `next_action`.
- **Notification Node (`Notify Operator`)**: Sends structured status update to Telegram.
- **Export File**: `n8n/workflows/01-nior-website-agent-pipeline-runner.json` (7 nodes, secret-free).

### Workflow 02: Daily Business Briefing & Alerts (`eRAS3vC1wq19zaYu`)
- **Trigger**: `Schedule Trigger` set to daily interval at `08:00` in `Africa/Lagos` timezone (UTC+1), plus `Manual Trigger`.
- **Data Ingestion Nodes**:
  - `GET /health`: Health metrics and attention backlog.
  - `GET /engagement/pipeline`: Active lead count, qualified lead count, total pipeline value.
  - `GET /engagement/outstanding`: Overdue tasks and past-due follow-ups.
- **Briefing Composer (`Compose Briefing Text`)**: Matches `orchestration/briefing.py` rules:
  - Nigerian Naira (`₦`) currency symbol.
  - Africa/Lagos timestamp header.
  - Clean plain-text formatting (no raw Markdown asterisks/underscores).
  - Mobile chunking at <= 3900 characters per message.
- **Notification Node (`Send Briefing to Telegram`)**: Dispatches message chunks to operator Telegram chat.
- **Export File**: `n8n/workflows/02-nior-operations-daily-briefing.json` (7 nodes, secret-free).

### Workflow 03: Buffer Multi-Channel Publisher (`hgLCDChiBiJhooUE`)
- **Trigger**: `Manual Trigger` accepting approved social draft payloads.
- **Gatekeeper Node (`Validate Approval & Fingerprint`)**:
  - Verifies target channel is in `VERIFIED_CHANNELS` (`twitter`, `linkedin`, `instagram`).
  - Verifies status is `approved` or `execution_authorized === "true"`.
  - Verifies character limits (280 for Twitter/X, 3000 for LinkedIn, 2200 for Instagram) and media presence.
  - Rejects unapproved drafts fail-closed.
- **Dispatcher Gate (`Buffer Dispatcher Gate`)**:
  - In `dry_run=true` (default during tests): Simulates draft creation without public network calls.
  - In live mode: Enforces `saveToDraft: true` so posts land in Buffer's approval queue, preventing accidental live publishing.
- **Notification Node (`Notify Social Status`)**: Confirms draft placement to operator Telegram.
- **Export File**: `n8n/workflows/03-nior-social-buffer-publisher.json` (4 nodes, secret-free).

### Workflow 04: Inbound Ingestion & Intake Router (`gWXFx91CdFmvH45R`)
- **Trigger**: `Schedule Trigger` (hourly) and `Manual Trigger`.
- **Cycle Configuration (`Configure Intake Cycle`)**: Bounds batch limit (`max_messages: 10`, max 50) and sets `dry_run: true` default.
- **Poller Node (`Poll Inbound Mailbox`)**: Calls `POST /integrations/email/poll` on the local API. Reuses read-only `integrations/gmail.py` and `tracking/inbound.py`. Contains no send capability.
- **Intake Processor (`Process Intake Results`)**: Summarizes `fetched`, `recorded`, `unmatched`, and flags attention items.
- **Notification Node (`Notify Operator of Intake`)**: Dispatches report to operator.
- **Export File**: `n8n/workflows/04-nior-communications-inbound-intake.json` (6 nodes, secret-free).

### Workflow 05: Approved Send with Fingerprint Gate (`UbKxQXbz5Q7PSxQs`)
- **Trigger**: `Manual Trigger` with required `outreach_id`, `confirmation`, `expected_fingerprint`.
- **Precondition Validator (`Validate Preconditions & Fail-Closed Gate`)**:
  - Enforces `dry_run=true` by default.
  - Requires explicit `outreach_id`.
  - Rejects missing inputs fail-closed.
- **Outbox Readiness Check (`Check Outbox Readiness`)**: Calls `GET /integrations/outbox`.
- **Send Execution (`Execute Approved Send`)**: Calls `POST /integrations/email/send`. Enforces server-side content fingerprint check, opt-out check, and contact budget gate.
- **Result Inspector (`Inspect Send Result & Safety Ledger`)**: Checks `send_state` and blocks retries if `SEND_UNCERTAIN`.
- **Notification Node (`Notify Operator of Send`)**: Reports send outcome to operator.
- **Export File**: `n8n/workflows/05-nior-outbound-approved-send.json` (6 nodes, secret-free).

---

## 3. Environment Variables & Network Configuration

The workflows use parameter expressions referencing environment variables or falling back to local defaults:

| Variable | Description | Default Fallback |
|---|---|---|
| `AI_WEBSITE_AGENT_API_URL` | Base HTTP URL of the local orchestration API | `http://127.0.0.1:8787` |
| `AI_WEBSITE_AGENT_TELEGRAM_CHAT_ID` | Operator chat ID for operational notifications | `7722236745` |

### Reachability Note
When n8n runs in a container or external tunnel, `127.0.0.1` refers to the n8n container, not the host machine. To reach the local application API:
- Set `AI_WEBSITE_AGENT_API_URL` in n8n environment to host bridge (e.g. `http://host.docker.internal:8787` or tunnel endpoint).
- Or use credential `AI Website Agent Render API` (`Ge2FhrLZze9JT9R3`) if deploying to Render.

---

## 4. Referenced Credentials

All credentials exist in personal project `gF2ObH2mvjWtE5pb`:
1. `AI Website Agent Local Test API` (`gnDuiGUMZQoooPza`): Header authentication (`Authorization: Bearer <token>`).
2. `Telegram account` (`rAJTUlHH8SzEYuYQ`): Telegram Bot API credential.
3. `Nior - Buffer API - TEST` (`lKCnp5gsvWMXiEHL`): Buffer API header token.
4. `Gmail account` (`HMztaR3kJx7E8AEQ`): Read-only OAuth2 Gmail credential.

No secret tokens or private keys are stored in the workflow definitions or exported JSON files.

---

## 5. Outstanding Operator Tasks Before Production Activation

1. **Verify Network Reachability**: If triggering from cloud/tunnel n8n, confirm `AI_WEBSITE_AGENT_API_URL` resolves to the running API server.
2. **Review Telegram Bot Token**: Ensure the Telegram bot is active and allowed user IDs include `7722236745`.
3. **Keep Workflows Inactive**: Workflows must remain `active: false` until an operator explicitly toggles them active in the n8n UI.
