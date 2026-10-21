# Environment Variables & Configuration Runbook

This document defines all operational environment variables recognized across the Nior system.

> [!CAUTION]
> Never commit actual secrets, API tokens, passwords, or OAuth credentials to Git. Use an untracked `.env` file or host environment variables only.

---

## 1. Core Runtime Configuration

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `AI_WEBSITE_AGENT_ENV` | No | `development` | Operating environment (`development`, `staging`, `production`). In `production`, local fallback state and unprobed remote storage are refused. |
| `AI_WEBSITE_AGENT_STORAGE_BACKEND` | No | `sqlite` | Persistence backend: `sqlite` (local file) or `postgres` (remote Supabase). |
| `DATABASE_URL` | If postgres | None | PostgreSQL connection string (e.g., Supabase transaction pooler). |
| `AI_WEBSITE_AGENT_API_URL` | No | `http://127.0.0.1:8787` | Base URL of the Python orchestration API consulted by n8n. |
| `AI_WEBSITE_AGENT_API_TOKEN` | Yes | None | Shared bearer token for API endpoints (`/jobs`, `/engagement`, `/health`). |

---

## 2. Communications & Operator Channels

### Telegram Operator Console
| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN` | Optional | None | Bot token provided by @BotFather. |
| `AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_USER_IDS` | If bot token set | None | Comma-separated list of authorized numeric Telegram User IDs. |
| `AI_WEBSITE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS` | If bot token set | None | Comma-separated list of authorized Telegram Chat IDs. |

### Gmail Dual-Scope Configuration
The system strictly enforces separate credentials for read-only polling versus outbound sending:

#### Inbound Read-Only (`gmail.readonly`)
| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `AI_WEBSITE_AGENT_GMAIL_CLIENT_ID` | Optional | None | OAuth 2.0 Client ID for inbox reading. |
| `AI_WEBSITE_AGENT_GMAIL_CLIENT_SECRET` | Optional | None | OAuth 2.0 Client Secret. |
| `AI_WEBSITE_AGENT_GMAIL_REFRESH_TOKEN` | Optional | None | Refresh token authorized only for `https://www.googleapis.com/auth/gmail.readonly`. |

#### Outbound Sending (`gmail.send`)
| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_ID` | Optional | None | OAuth 2.0 Client ID for outbound email. |
| `AI_WEBSITE_AGENT_GMAIL_SEND_CLIENT_SECRET` | Optional | None | OAuth 2.0 Client Secret. |
| `AI_WEBSITE_AGENT_GMAIL_SEND_REFRESH_TOKEN` | Optional | None | Refresh token authorized only for `https://www.googleapis.com/auth/gmail.send`. |
| `AI_WEBSITE_AGENT_EMAIL_SEND_ENABLED` | No | `false` | Master kill-switch for outbound email. Must be set to `true` to allow live sending. |
| `AI_WEBSITE_AGENT_EMAIL_UNSUBSCRIBE_URL` | Recommended | None | Base URL included in `List-Unsubscribe` headers. |

---

## 3. Deployment & Cloud Providers

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `AI_WEBSITE_AGENT_OPERATOR_ID` | No | Current User | Identifier recorded in review and approval ledgers (e.g. `operator:name`). |
| `VERCEL_ORG_ID` / `VERCEL_PROJECT_ID` | Managed | None | Written automatically into `.vercel/project.json` by `vercel link`. |

---

## 4. Discovery & AI Enrichment

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `TAVILY_API_KEY` | Optional | None | API key for Tavily search enrichment in `scout/tavily_enrichment.py`. |
| `OPENROUTER_API_KEY` | Optional | None | OpenRouter API key for LLM generation adapters. |
| `GROQ_API_KEY` | Optional | None | Groq API key for high-speed inference. |

---

## 5. Security & Verification Checklist

1. **Preflight Check**:
   ```powershell
   python -m orchestration.cli preflight
   ```
2. **Database Verification**:
   ```powershell
   python -m tracking.cli preflight-db
   ```
3. **Telegram Verification**:
   ```powershell
   python -m orchestration.cli telegram-check
   ```
4. **Email Send Verification**:
   ```powershell
   python -m orchestration.cli email-send-check
   ```

---

## 6. Credential Rotation & Security Incident Audit (Stage 8)

* **Incident**: Exposed bearer token in handover logs.
* **Rotation Action**: Rotated `AI_WEBSITE_AGENT_API_TOKEN` on Render to a freshly generated 43-character token (`secrets.token_urlsafe(32)`).
* **Affected Clients**:
  - Render API Service (`ai-website-agent-api`): Updated and redeployed live.
  - Old token rejection verified: `HTTP 401 Unauthorized`.
  - New token acceptance verified: `HTTP 200 OK`.
  - n8n Client (`AI Website Agent Local Test API`): Pending manual token update in n8n UI.
* **Repository Sanitation**: Git tracked files scanned; zero plaintext bearer tokens found in version control.

---

## 7. Supabase Production Security & Recovery Audit

* **Connection Mode**: Supabase IPv4 Session Pooler (`aws-0-us-east-1.pooler.supabase.com:5432`) with `sslmode=require`.
* **Row Level Security (RLS)**: Enabled across all 11 core tables (`leads`, `research_evidence`, `projects`, `builds`, `validations`, `deployments`, `outreach_drafts`, `operator_reviews`, `jobs`, `worker_leases`, `schema_migrations`). Public anonymous (`anon`) REST access is restricted.
* **Backup & Retention Policy**:
  - **Plan**: Supabase Free Tier.
  - **Automated Backups**: Daily automated snapshots managed by Supabase, retained for 7 days (Point-in-Time-Recovery / PITR requires Supabase Pro plan).
  - **Logical Export Procedure**:
    ```bash
    pg_dump "$DATABASE_URL" --format=custom --file=backup_$(date +%Y%m%d).dump
    ```
  - **Recovery Procedure**:
    ```bash
    pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL" backup.dump
    ```
  - **Recovery Limitations**: Full database restore has not been executed against production data; test restore should be performed in a separate staging project before relying on disaster recovery.
