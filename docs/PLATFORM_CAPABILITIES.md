# Platform Capabilities & Social Media Matrix

This document provides a realistic evaluation of external platform APIs, pricing, authentication requirements, rate limits, and viability for a low-cost, Nigeria-focused automated business operating system.

---

## 1. Platform Evaluation Matrix

| Platform | Channel / API | Cost / Tier | Auth Model | Free-Tier Feasibility | Recommended Adapter / Route |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Telegram** | Telegram Bot API | **100% Free** | Bot Token | **High** (Production Ready) | Direct via `integrations/telegram.py` & live Core Bot |
| **Email (Gmail)** | Google Workspace / Gmail API | Free (standard quotas) | OAuth 2.0 (Dual Scopes) | **High** (Production Ready) | `integrations/gmail.py` (read) & `integrations/gmail_send.py` (send) |
| **Buffer** | Buffer Publish API | Free Tier (3 channels, 10 queued posts) | API Key / Token | **High** (Production Ready) | n8n live workflows `zAd3P31ZrTm9Ax94` & `pdgWlBYPjrdeGlZF` |
| **Botcake** | Botcake Messenger/IG API | Free / Low-tier | Access Token | **High** (Production Ready) | n8n live workflows `MSF5ie4N854tN7QD` & `9keV9mvzrimYXshk` |
| **YouTube** | YouTube Data API v3 | Free (10,000 units/day) | OAuth 2.0 | **High** (Verified) | n8n live workflows `7nMJEhUggv1P09P4` & `FkqEN0bdR3fMKu3v` |
| **Google Sheets**| Google Sheets API v4 | Free (standard quotas) | OAuth 2.0 | **High** (Verified) | n8n live credential `jdyZlQZE6BQaTr3d` (Catalog reader) |
| **OpenStreetMap**| Overpass API | **100% Free** | None (public endpoint) | **High** (Production Ready) | Direct via `scout/scout_agent.py` |
| **Tavily** | Search / Enrichment API | Free tier (1,000 searches/mo) | API Key | **High** (Active in Scout) | `scout/tavily_enrichment.py` |
| **Meta (FB & IG)**| Meta Graph API | Free (requires verified Business) | App Secret + Page Token | **Medium** (via Buffer/Botcake) | Route via Buffer / Botcake to avoid Meta Business verification hurdles |
| **WhatsApp** | WhatsApp Cloud API | 1,000 free service convs/mo | Meta System Token | **Medium** (via Botcake / Cloud) | Route via Botcake or dedicated webhook intake |
| **LinkedIn** | LinkedIn UGC / Share API | Free | OAuth 2.0 (Member/Org) | **Medium** (via Buffer) | Route via Buffer multi-channel scheduler |
| **X (Twitter)** | X API v2 | $100/mo (Basic tier) | OAuth 1.0a / 2.0 | **Low** (Direct API costly) | Route via Buffer free tier (Buffer handles X posting) |

---

## 2. Key Architectural Insights for Nigeria Operations

### A. The Buffer Aggregation Strategy
- Directly integrating Meta Graph API, LinkedIn Community Management API, and X API requires individual developer account approvals, corporate verifications, and (for X) expensive monthly subscription fees ($100/mo).
- By routing scheduled social posts through **Buffer** (using the live n8n workflows `Nior - Buffer Draft Executor` and `Nior - Buffer Social Publisher`), Nior achieves multi-platform publishing (Instagram, Facebook Page, LinkedIn, X) on a single free plan with zero direct platform fees.

### B. The Botcake Messaging Strategy
- Managing direct customer engagement across Instagram DMs and Facebook Messenger can be complex due to Meta Webhook verifications and 24-hour messaging window rules.
- **Botcake** handles the incoming fanpage/IG webhook, normalizes the conversation, and forwards actionable inquiries to n8n (`Nior - Botcake Ingestion`), where Nior's PostgreSQL store records the interaction and alerts the operator via Telegram.

### C. Local Telegram Control Hub
- For an operator managing business in Nigeria with variable desktop connectivity, Telegram serves as the mobile primary console.
- Telegram Bot API requires zero hosting costs, delivers instantaneous push notifications, supports inline interactive approval keyboards, and operates reliably over low-bandwidth mobile connections.

---

## 3. Quota Limits & Safety Guidelines

1. **Overpass OSM API**: Maximum 1 query per 5 seconds; Lagos bounding box queries limited to 50 results per run.
2. **Gmail API**: Daily sending limit of 500 emails/day for standard Gmail accounts, 2,000 for Google Workspace. Outbound engine enforces maximum batch size of 5 per dispatch window.
3. **YouTube Data API**: 10,000 units/day quota budget. Comment reply costs 50 units (~200 comments/day max); metadata updates cost 50 units.
4. **Buffer API**: Free tier limit of 10 pending scheduled posts per profile. Executor workflow checks pending queue depth before scheduling.
