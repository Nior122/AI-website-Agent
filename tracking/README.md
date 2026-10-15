# Lead & Project Tracking

`TrackingStore` is the single local JSON state store (`data/tracking/state.json`, ignored by Git). It preserves leads separately from projects and preserves build, validation, and deployment history by ID. No database or external service is required.

Create a lead, then a project only for `QUALIFIED` leads. Use guarded transitions; record builds, validations, deployments, and human review without overwriting history. `snapshot(project_id)` returns the current lead, project, latest records, and derived next action.

Duplicates use public identifiers: exact website domain, phone, or social URL gives `EXACT_MATCH`; same normalized name plus city/country gives `POSSIBLE_MATCH`; otherwise `NO_MATCH`. Possible matches are retained for review, not merged.

## Database integration commands

`python3 -m tracking.cli preflight-db` answers, without writing anything: which database this workspace
would use (host, port, database name, and the Supabase project reference embedded in the hostname), whether
it can be reached with the credential redacted, whether `migrations/0001_supabase_postgres_schema.sql` is
applied and its recorded checksum matches this checkout, which owned tables are missing, and how many rows
each table holds. `status` distinguishes `ready`, `needs_migration` (a step to take), `blocked` (a fault to
fix, e.g. a checksum conflict or a project-ref that does not match `--expect-project-ref`), `unreachable`,
and `not_configured`. Exit code 0 only for `ready`.

`migrate-db` applies the same schema and is additive and safe to repeat; applying DDL to a database that is
not on this machine requires `--confirm-database <name>`, read back from the URL, so a stale environment
variable cannot migrate the wrong Supabase project. `--verify-only` needs no confirmation because it runs no
DDL. Both print the identity they acted on and what the schema looked like beforehand.

Production mode (`AI_WEBSITE_AGENT_ENV=production` or `AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres`) never
falls back to local JSON: a missing or unreachable database raises `DatabasePersistenceError` at startup and
at the first write, and a `sqlite://` URL is refused outright. Full procedure: `docs/INTEGRATION-SETUP.md`.
