# n8n Workflows & Automation Architecture

This document inventories the automation workflows operating within the Nior ecosystem, mapping live n8n workflows (via Cloudflare tunnel / local instance) and the repository's version-controlled JSON definitions in `n8n/workflows/`.

---

## 1. Verified Live Workflows Inventory

The live n8n instance currently hosts **34 workflows** across website lead operations, social media publishing (Buffer), messaging (Botcake), YouTube automation, business briefing, and Telegram operator control.

### Active Production & Operational Workflows

| Live ID | Workflow Name | Active | Trigger Type | Primary Role & Integrations |
| :--- | :--- | :--- | :--- | :--- |
| `mfFr5P5Asefs62qA` | **Nior Business Operator - Core** | **Yes** | Telegram / Webhook | Core Telegram operator command router & business assistant |
| `zXkoxNTprXSbE1se` | **Nior - Gmail Customer Inbox** | **Yes** | Poll / Webhook | Gmail customer response polling and normalization |
| `JQjLiB7DuQHrgclG` | **Send Approved Outbound Email** | **Yes** | Schedule / Webhook | Outbound email dispatcher (matches repo `07-approved-outbound-send`) |
| `zAd3P31ZrTm9Ax94` | **Nior - Buffer Draft Executor** | **Yes** | Webhook / Sub-wf | Buffer post scheduling and drafting for social media |
| `pdgWlBYPjrdeGlZF` | **Nior - Buffer Social Publisher (TEST)** | **Yes** | Webhook | Buffer publication execution |
| `MSF5ie4N854tN7QD` | **Nior - Botcake Reply Executor** | **Yes** | Webhook | Automated customer reply routing via Botcake |
| `9keV9mvzrimYXshk` | **Nior - Botcake Ingestion (TEST)** | **Yes** | Webhook | Ingests incoming social/chat inquiries into PostgreSQL |
| `loO3tTJlt6aFdhvb` | **Nior - Daily Business Briefing** | **Yes** | Cron (Daily) | Automated morning performance & pipeline brief to Telegram |
| `caX9D6tv5zXNi3ht` | **Business Task Reminders** | **Yes** | Cron / Schedule | Scheduled reminders for pending operational tasks |
| `SW3aQuFdep32j9k5` | **Task Due Reminders** | **Yes** | Cron / Schedule | Due-date alert dispatcher |
| `HuFLLQNiaJFhwUGJ` | **Wait Action Monitor** | **Yes** | Schedule | Monitors state machine timeouts and pause conditions |
| `ElL7kbytoZZzZWtm` | **Wait Resume Scheduler** | **Yes** | Schedule | Resumes paused jobs after wait intervals expire |
| `rgT0jVo0zfpU43Xp` | **send aproval Recovery Monitor** | **Yes** | Cron | Recovery watcher for pending email send approvals |
| `JAkNv5oBC1eU5Ny3` | **Nior - Approval Notification Dispatcher** | **Yes** | Sub-workflow | Prepares structured Telegram alert cards for operator decisions |
| `7nMJEhUggv1P09P4` | **Nior - YouTube Comment Reply (Prepare)** | **Yes** | Sub-workflow | Formats comment reply options for owner review |
| `FkqEN0bdR3fMKu3v` | **Nior - YouTube Comment Reply Executor** | **Yes** | Webhook | Posts approved comment responses to YouTube API |
| `ZLEKBaQuMsKTvIGe` | **Nior - YouTube Metadata Update (Prepare)** | **Yes** | Sub-workflow | Prepares title/description optimization suggestions |
| `20y6IOBTRksLvaGB` | **Nior - YouTube Metadata Update Executor** | **Yes** | Webhook | Applies approved video metadata updates |
| `GN3tsiwcPPkfqlAN` | **Nior - YouTube Metadata Preview** | **Yes** | Sub-workflow | Generates diff previews for metadata edits |
| `6BMSsqKtUprRS6vA` | **Nior - YouTube Tools for Telegram** | **Yes** | Telegram / Tool | Tool router allowing Core Telegram Bot to manage YouTube |

### Repository Exported Workflows (`n8n/workflows/`)

The repository maintains version-controlled, credential-sanitized reference workflows that integrate with the Python orchestration API:

1. **`01-create-job.json`** (`AI Website Agent - Create Dry Run Job`)
   - **Trigger**: Manual
   - **Action**: Calls `POST /jobs` on the orchestration API with `dry_run: true`.
   - **Safety**: Shipped in fixture-only mode; cannot trigger real discovery or builds without operator configuration.

2. **`02-inspect-job.json`** (`AI Website Agent - Inspect Job`)
   - **Trigger**: Manual
   - **Action**: Queries `GET /jobs/{id}` to inspect execution status, error codes, and audit logs.

3. **`03-core-lead-to-review-package-pipeline.json`** (`AI Website Agent - Core Pipeline`)
   - **Nodes**: 19 nodes orchestrating `SCOUT` → `RESEARCH` → `BUILD` → `VALIDATE` → `REVIEW_PACKAGE`.
   - **Safety Gates**: Enforces human approval gate before preview deployment or outreach.

4. **`04-recover-and-retry-jobs.json`** (`AI Website Agent - Recover Stale & Retry Eligible Jobs`)
   - **Nodes**: Calls `POST /jobs/recover-stale` and evaluates retry eligibility against `TrackingStore`.

5. **`05-inbound-response-intake.json`** (`AI Website Agent - Inbound Response Intake`)
   - **Nodes**: Receives webhooks from external messaging/email channels, calls `POST /engagement/inbound`.

6. **`06-email-cycle-and-operator-notification.json`** (`AI Website Agent - Email Cycle & Operator Notification`)
   - **Nodes**: Polls Gmail via `POST /integrations/email/poll` and dispatches alerts via `POST /integrations/telegram/notify`.

7. **`07-approved-outbound-send.json`** (`AI Website Agent - Approved Outbound Send`)
   - **Nodes**: 12 nodes. Dispatches outbound proposals via `POST /integrations/email/send`.
   - **Safety Guard**: Shipped with `dry_run: true` and empty confirmation string. Live execution requires explicit operator confirmation token and `--allow-live` authorization.

---

## 2. Configured Credentials on n8n Instance

The live n8n instance contains **15 pre-configured credential records**:
- **Postgres Account** (`Mx3fVbQC9UXiwKwB`): Supabase/PostgreSQL connection.
- **Gmail Account** (`HMztaR3kJx7E8AEQ`): Google OAuth2 for inbox ingestion and outbound notifications.
- **Telegram Account** (`rAJTUlHH8SzEYuYQ`): Bot token for operator interactions and alert channel.
- **Buffer API** (`lKCnp5gsvWMXiEHL`): Token for social post scheduling (Instagram, Facebook, LinkedIn, X).
- **Botcake API** (`dL88uTKIajsI6J4W`): Token for Facebook Messenger / Instagram DM automation.
- **YouTube Account** (`fIPp71Ie7D5Hms7U`): YouTube Data API v3 integration.
- **Google Sheets API** (`jdyZlQZE6BQaTr3d`): Catalog and spreadsheet ingestion.
- **Google Drive API** (`dIuN6x8WrXCMziLU`): Document and asset storage.
- **LLM API Credentials**:
  - Groq (`BKuWV9yh6di1hFDR`)
  - OpenRouter Primary (`X1N07xyeYuJNQXIq`) & Secondary (`Il6rBViPjLoHfkxE`)
  - NVIDIA Nemotron (`zE1GxbTtA2DgPWo2`)
- **Internal API Tokens**:
  - `AI Website Agent Local Test API` (`gnDuiGUMZQoooPza`)
  - `AI Website Agent Render API` (`Ge2FhrLZze9JT9R3`)

---

## 3. Governance and Security Rules

1. **No Hardcoded Secrets**: Workflows exported to Git must never contain credentials, API tokens, or actual prospect phone numbers/emails.
2. **Fail-Closed Design**: Any workflow attempting external action (email sending, social media posting) must require explicit manual approval or confirmation tokens.
3. **Audit Trail**: Every execution must record its external correlation ID in the persistent ledger.
