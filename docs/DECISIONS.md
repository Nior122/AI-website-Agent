# Architectural Decision Records (ADRs)

This document records the foundational technical and architectural decisions made in the Nior codebase.

---

## ADR 01: Standard-Library First Architecture & Zero Heavy Frameworks

- **Context**: The application runs on local operator machines in Nigeria and free-tier cloud containers (Render, Supabase). Bloated framework dependencies increase startup latency, memory footprint, dependency drift, and breakage risk.
- **Decision**: All core modules (`builder`, `research`, `orchestration`, `deployment`, `tracking`) use the Python standard library wherever possible (`http.server`, `urllib.request`, `sqlite3`, `json`, `dataclasses`, `argparse`). `psycopg` is the single external database driver, and even it is abstracted behind a DB-API double for testing.
- **Alternatives Considered**: FastAPI, Celery, SQLAlchemy, LangChain.
- **Consequences**: Fast test execution (679 tests in 106s), zero package bloat, minimal attack surface, easy maintenance on resource-constrained environments.

---

## ADR 02: Append-Only Immutable State Ledgers

- **Context**: In automated web generation and outreach, mutating state in-place leads to lost audit records (e.g., did an email really send, did a preview fail, who authorized a review?).
- **Decision**: All critical domain records are append-only:
  - `publish_outcome` and `deployment-attempts.json` append every attempt, timeout, and operator resolution without modifying prior attempts.
  - Reviews append to history with reviewer identity and timestamps.
  - `TrackingStore` maintains durable execution histories.
- **Alternatives Considered**: In-place status overwrite columns (`status = 'DEPLOYED'`).
- **Consequences**: Impossible to disguise a failed attempt or bypass an operator gate by manually editing a state row. Full auditability.

---

## ADR 03: Separate Minimum-Scope Credentials for Gmail

- **Context**: Polling an inbox for customer replies requires `gmail.readonly`. Sending outbound emails requires `gmail.send`. If a single token has both scopes, any code bug or compromise in the ingestion loop could trigger unauthorized emails.
- **Decision**: The system splits Gmail operations into two completely distinct modules:
  - `integrations/gmail.py`: Has only read methods; its client explicitly refuses send capabilities.
  - `integrations/gmail_send.py`: Contains only `send()` and `verify_authorization()`, bound to a dedicated OAuth client.
- **Alternatives Considered**: A single `GmailClient` with `send_message()` and `get_messages()`.
- **Consequences**: Strict least-privilege security. Ingestion workers physically cannot transmit messages.

---

## ADR 04: Content Fingerprinting on Outbound Outreach

- **Context**: A draft could be approved by an operator, and subsequently an AI model or bug could alter the body or subject before the actual network send occurs.
- **Decision**: At approval time, `outreach/service.py` computes an SHA-256 fingerprint over normalized content keys (`recipient`, `subject`, `pitch_text`, `preview_url`). At send time, `ApprovedOutboundSender` recomputes the fingerprint against the current package and aborts if any discrepancy is detected (`FINGERPRINT_MISMATCH`).
- **Alternatives Considered**: Storing a boolean `is_approved: true`.
- **Consequences**: Guarantees that what was reviewed is identically what is sent.

---

## ADR 05: Buffer and Botcake as Social Automation Abstraction Layer

- **Context**: Direct integration with Meta Graph API, LinkedIn API, and X API requires heavy ongoing compliance, business verification, and costly API subscriptions ($100/mo for X).
- **Decision**: Nior delegates social scheduling to Buffer's free tier and chat DM intake to Botcake via n8n webhook nodes.
- **Alternatives Considered**: Direct REST integration with X v2, Facebook Graph, Instagram Graph, and LinkedIn UGC.
- **Consequences**: Zero recurring API costs, simplified token management, bypasses Meta Business verification barriers, leverages proven mobile-friendly posting queues.
