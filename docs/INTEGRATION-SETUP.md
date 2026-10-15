# Integration Setup: from local operation to a controlled preview

This is the path from "the code works on my machine" to "a preview exists and an operator decided it
should". Four states get confused constantly, so every step below names which one it produces:

| State | What it means | How you know you have it |
| --- | --- | --- |
| **Code readiness** | The repository's own logic runs, with no accounts or services | `python3 -m unittest discover -s tests` (344 tests) |
| **Configuration readiness** | The variables, files, and workflow JSON describe things that exist | `python3 -m orchestration.cli preflight` |
| **Connectivity** | A database was actually opened and its schema was actually read | `python3 -m tracking.cli preflight-db` → `status: ready` |
| **A real preview** | The provider accepted an artifact and returned a URL | a `DEPLOYED` record with an `https://<name>.vercel.app` URL, produced only by `deployment.cli --execute` |

Nothing in this guide is a substitute for the last row: no command in this repository reports a preview as
deployed without provider evidence, and `preflight` deliberately prints `not_tested` / `not_performed`
rather than an optimistic guess.

Secrets are never written into a report, and never belong in a file here. Use your shell or the dashboard
for values; this document uses `<placeholders>`.

---

## 1. Environment variables

Only these are read. A variable that is absent is not an error unless the "Required when" column says so.

| Variable | Purpose | Required when | Secret? |
| --- | --- | --- | --- |
| `AI_WEBSITE_AGENT_ENV` | `production` (or `RENDER=1`) turns on fail-closed storage and bind rules | deployed/production | no |
| `AI_WEBSITE_AGENT_STORAGE_BACKEND` | `postgres`/`supabase` selects the database backend | deployed | no |
| `DATABASE_URL`, or `AI_WEBSITE_AGENT_DATABASE_URL`, or `SUPABASE_DB_URL` | `postgresql://...` connection string (first non-empty wins, in that order) | production mode: **yes** — the app refuses to run rather than fall back to JSON | **yes** |
| `AI_WEBSITE_AGENT_API_TOKEN` | Bearer token for every API route except `/healthz` | always for `serve`; ≥32 chars and not a placeholder for any non-loopback bind | **yes** |
| `AI_WEBSITE_AGENT_API_URL` | Base URL the n8n workflows call (e.g. `http://127.0.0.1:8787`) | when running the n8n workflows | no |
| `AI_WEBSITE_AGENT_BIND_HOST` / `--host` | Bind address; defaults to `127.0.0.1` | optional | no |
| `AI_WEBSITE_AGENT_ALLOW_NON_LOOPBACK` | Explicit opt-in for `0.0.0.0` binds (Render sets it) | non-loopback binds | no |
| `PORT` | Render's assigned port; used when `--port`/`AI_WEBSITE_AGENT_API_PORT` are absent | on Render | no |
| `AI_WEBSITE_AGENT_DB_CONNECT_TIMEOUT` | Seconds before a database connection attempt is abandoned (default 10) | optional | no |
| `AI_WEBSITE_AGENT_OPERATOR_ID` | Default attribution for operator actions recorded by the CLI | optional | no |
| `AI_WEBSITE_AGENT_LOG_DIR` / `_LOG_FILE` / `_LOG_LEVEL` | Structured operational log location and level | optional | no |

Local shell, POSIX:

```bash
export AI_WEBSITE_AGENT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DATABASE_URL="postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres"
```

Windows PowerShell (no history of the value is kept if you set it in the current session only):

```powershell
$env:AI_WEBSITE_AGENT_API_TOKEN = -join ((48..57)+(97..122) | Get-Random -Count 40 | ForEach-Object {[char]$_})
$env:DATABASE_URL = "postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres"
# later, in the same session:
Remove-Item Env:AI_WEBSITE_AGENT_API_TOKEN, Env:DATABASE_URL
```

---

## 2. Local API startup

**Automatic.** Start the server and answer its four questions separately:

```bash
python3 -m orchestration.cli serve --host 127.0.0.1 --port 8787
# PowerShell equivalent:
py -3 -m orchestration.cli serve --host 127.0.0.1 --port 8787
```

| Question | Endpoint | What it will not claim |
| --- | --- | --- |
| Is the process alive? | `GET /healthz` | anything about the database — it is the only route that needs no token, so a load balancer can poll it |
| Is storage usable? | `GET /readiness` | healthiness without a probe: an unprobed backend is reported as configuration-only |
| What is stuck, and who must act? | `GET /attention` | guesses about jobs with no recorded timestamp |
| What did the provider actually record? | `GET /deployment-status` | a live URL check; it reads this repository's records |

Authentication is one shared bearer token (documented design, no user management). `serve` refuses to start
without it, and refuses a short or placeholder token for any bind reachable beyond the machine.

```bash
curl -H "Authorization: Bearer $AI_WEBSITE_AGENT_API_TOKEN" http://127.0.0.1:8787/attention
```

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8787/attention" -Headers @{ Authorization = "Bearer $env:AI_WEBSITE_AGENT_API_TOKEN" }
```

---

## 3. Supabase / PostgreSQL: identify, preflight, then migrate

**Manual step, in the Supabase dashboard:** open the project you intend to use and note its **reference ID**
(the 20-character string in the project URL and in the connection string, e.g. `db.ipn4c08glpwfc
...supabase.co`). Do this before you paste anything into a shell. If the connection string you are about to
use does not contain that reference, you are holding a different project.

**Automatic.** Ask what this workspace would migrate, before migrating it:

```bash
python3 -m tracking.cli preflight-db --expect-project-ref <project-ref>
```

It opens one connection, runs `SELECT`s, and writes nothing. `status` is one of:

- `ready` — reachable, migration applied, checksum matches the file in this checkout.
- `needs_migration` — reachable, schema absent. Apply it (next step). Not a fault.
- `blocked` — reachable but wrong: an identity mismatch, a checksum that differs from this checkout, or a
  migration recorded without a checksum. Reconcile by hand; re-running will not fix a conflict.
- `unreachable` / `not_configured` — no connection or no URL. Nothing was created.

The report also carries `row_counts`, so a database that already holds someone's operational records is
visible before DDL lands in it, and `supabase_identification`, which explains how to confirm the project
when the hostname does not name it (a transaction-pooler host such as
`aws-0-<region>.pooler.supabase.com` carries no reference, so `--expect-project-ref` cannot help and the
report says so).

**Automatic, with a deliberate confirmation.** `migrate-db` is additive (`CREATE TABLE IF NOT EXISTS`,
`ADD COLUMN IF NOT EXISTS`, a keyed `schema_migrations` row) and safe to repeat. Because it can also land
in the wrong project by way of a stale `DATABASE_URL`, applying DDL to a database that is not on this
machine requires naming that database back:

```bash
python3 -m tracking.cli migrate-db --confirm-database postgres
python3 -m tracking.cli migrate-db --verify-only          # read-only check, no confirmation needed
```

`migrate-db` prints the identity it applied to, plus what the schema looked like beforehand. The
`schema_migrations` row is recorded with a SHA-256 checksum of the migration file; that file is
checksum-pinned, so **never edit it** — add a new numbered migration instead.

**Fail-closed, by design.** In production mode with no database URL the application refuses to start rather
than writing local JSON, and `TrackingStore._save()` raises instead of silently creating a state file. A
SQLite URL is refused outright in production mode. Verified by the suite
(`tests/test_cloud_deployment_readiness.py`, `tests/test_phase42_integration_setup_and_preflight.py`).

---

## 4. n8n: import, configure, keep inactive

**Automatic.** Check the exports without sending a request:

```bash
python3 -m orchestration.cli check-n8n
python3 -m orchestration.cli preflight          # includes the same section with more context
```

`check-n8n` reports importability, the environment variables each workflow reads, which of those are unset
in this environment, and which files still contain `REPLACE_WITH_*` starter placeholders. Unset
`AI_WEBSITE_AGENT_API_URL` matters more than it looks: it renders to an **empty string**, so the request
goes to `/jobs` instead of your API and reads like an outage.

| File | What it does | Notes |
| --- | --- | --- |
| `n8n/workflows/01-create-job.json` | `POST /jobs` with a dry-run `SCOUT` job | starter; safe as-is |
| `n8n/workflows/02-inspect-job.json` | `GET /jobs/:id` | **manual edit**: replace `REPLACE_WITH_JOB_ID` in the n8n UI |
| `n8n/workflows/03-core-lead-to-review-package-pipeline.json` | discovery → research → qualification → build → validation → review package | ends at *human review pending*; there is no deploy node |
| `n8n/workflows/04-recover-and-retry-jobs.json` | `POST /jobs/recover-stale`, `GET /jobs/:id`, `POST /jobs/:id/retry` | retries only eligible jobs; a retried `DEPLOY` still passes the same durable gate |

**Manual steps, in the n8n UI** (Workflows → Import from File, one file at a time):

1. Set the credentials/environment n8n passes to the workflow: `AI_WEBSITE_AGENT_API_URL` (loopback, e.g.
   `http://127.0.0.1:8787`) and `AI_WEBSITE_AGENT_API_TOKEN`. The workflows contain no secrets by design —
   the header is `Bearer {{ $env.AI_WEBSITE_AGENT_API_TOKEN }}`.
2. Edit `02-inspect-job.json`'s node URL to a real job id.
3. Leave every workflow **inactive**. The repository copies ship with `"active": false` and validation
   refuses an export that arrives activated.
4. Point n8n only at this machine. The local verifier (`orchestration/n8n_runner.py`) refuses any node
   address outside `127.0.0.1`/`localhost` **before it builds the request**, because each node carries the
   bearer token; a hand-edited export cannot walk the credential to another host.
5. To drive the pipeline, trigger the workflow or use the CLI/worker. `DEPLOY` is not creatable over HTTP
   (`ALLOWED_API_JOB_TYPES`), so no workflow can schedule a publish.

No messaging, outreach, or customer-contact node exists in these four files, and outreach sending is
disabled in this repository's configuration.

---

## 5. Vercel: link the preview project, then a controlled publish

**Manual step, once, with the Vercel CLI** — in a *dedicated preview project*, never the production one:

```bash
vercel login
vercel link --project ai-website-agent-preview     # writes .vercel/project.json
```

`deployment/deployer.py` compares the linked `projectName` with the expected preview project and refuses a
mismatch, so a link to the production project is a refusal, not a publish. `requirements.txt` and the Vercel
CLI are only needed in the environment that will publish — not in the sandbox that runs the tests.

**Automatic.** Readiness and the artifact to publish:

```bash
python3 -m tracking.cli deploy-check project-0002 --build-id <build-id>   # gate answer + next command
python3 -m tracking.cli prepare-preview project-0002                       # dry-run readiness, no provider
python3 -m deployment.cli --build output/lead-0002/<build-id> --state-path data/tracking/state.json \
    --project-id project-0002 --build-id <build-id>                       # DRY RUN: no provider call
```

The dry run verifies the manifest, the build-local approval capability, `artifact_sha256`, and that the
approved build identifier matches the assigned and intended build (`APPROVAL_BUILD_MISMATCH` otherwise), and
it does not touch `deployment-attempts.json`.

**The disclosure is part of the artifact.** A `TEST_FIXTURE` build renders
`Fictional demonstration website · Created for AI Website Agent testing.`; a real-business build renders
`Uncommissioned preview draft prepared for internal evaluation · Not an authorized or official website of
<name>.` instead — never the fictional wording. Covered by `tests/test_phase22_inspection.py` and
`tests/test_phase26_real_draft.py`.

**Manual step, explicit, only after a human approval is recorded.** Publishing is preview-only in this
repository: `vercel deploy <dir> --yes --project <projectId>` with no `--prod`, and the recorded URL must
be an `https://<name>.vercel.app` host.

```bash
python3 -m deployment.cli --build output/lead-0002/<build-id> --state-path data/tracking/state.json \
    --project-id project-0002 --build-id <build-id> --execute
```

If the process is interrupted mid-publish, the attempt ledger keeps `DEPLOYING` unresolved and every later
run refuses until a human decides what happened:

```bash
python3 -m deployment.cli --build <dir> --state-path data/tracking/state.json --project-id <pid> \
    --build-id <build-id> --execute --resolve-uncertain-publish \
    --resolve-note "Checked the Vercel dashboard at 14:05Z: no deployment exists for this build." \
    --resolved-by chioma
```

The note is required and is written to `deployment-attempts.json` **before** the provider is contacted. It
records that an operator decided, never that the publish succeeded.

---

## 6. Render

`render.yaml` is checked against the code it starts by `tests/test_phase41_integration_and_recovery.py`
(every flag exists, `--state-path` is passed, `/healthz` is reachable without a token and is checked before
authentication, the declared environment variables are read by code, `buildCommand` contains no DDL, secrets
are `sync: false`, and `autoDeploy` is `false`). Those are the *only* claims that document makes:

- **Not performed by Render:** no database migration runs during a build. Apply §3 once, deliberately.
- **Required dashboard settings:** `DATABASE_URL` and `AI_WEBSITE_AGENT_API_TOKEN` are `sync: false`, so
  you type them into the Render service's Environment tab. A token shorter than 32 characters will stop the
  service from starting, by design.
- **Production storage:** `AI_WEBSITE_AGENT_ENV=production` and `AI_WEBSITE_AGENT_STORAGE_BACKEND=postgres`
  are set in the blueprint. If `DATABASE_URL` is missing, `serve` fails at startup with a message naming the
  missing variable; it does not fall back to a local JSON file.
- **Filesystem:** Render's container filesystem is ephemeral and `data/tracking/` is not in Git. The service
  must therefore run against PostgreSQL; build artifacts and review packages that live on disk are not a
  durable store there.

`autoDeploy: false` means nothing deploys until you press the button. This repository has never been
deployed to Render from within this workspace, and no claim is made that it is running.

---

## 7. Verification and recovery commands

| Situation | Command | Reads / writes |
| --- | --- | --- |
| Whole-workspace verdict | `python3 -m orchestration.cli preflight [--project-id <pid>]` | reads only; exit 1 when blocked |
| Database target and state | `python3 -m tracking.cli preflight-db` | one connection, `SELECT`s only |
| Schema check | `python3 -m tracking.cli migrate-db --verify-only` | read-only |
| n8n configuration | `python3 -m orchestration.cli check-n8n` | parses files; sends nothing |
| What a human must clear | `python3 -m orchestration.cli attention --brief` | reads; exit 1 when non-empty |
| Worker died mid-job | `python3 -m orchestration.cli recover-stale` | marks stranded jobs `NEEDS_REVIEW` (`HEARTBEAT_UNDATED`, `HEARTBEAT_EXPIRED`) |
| Retry what is eligible | `python3 -m orchestration.cli retry-job --job-id <id>` | one attempt, recorded |
| Provider outcome and history | `python3 -m orchestration.cli deployment-status` | reads records and the per-build ledger |
| Correlation for one job | `python3 -m orchestration.cli inspect-job --job-id <id> --history` | reads executions and retry eligibility |
| Workspace/artifact integrity | `python3 -m tracking.cli check-workspace` | verifies artifact hashes |
| Rebuild demo state after a reset | `python3 -m tracking.cli recover-verified-exports` | reconstructs project-0001/0002 from the SHA-256-verified review package, stamped `reconstructed: true` |

The last row exists because `data/tracking/state.json` is deliberately **not** in Git. On a fresh clone the
first store built on the default path materialises that demonstration state from the tracked review package;
it never overwrites an existing state file, and every reconstructed record says so.

---

## 8. What remains manual

A live integration is only real once someone with dashboard access does these. Nothing here claims they
have been done:

1. Create/select the Supabase project, set `DATABASE_URL`, run §3's preflight and migration.
2. Install `requirements.txt` in the environment that will migrate or publish (`psycopg` for PostgreSQL,
   the Vercel CLI for publishing).
3. In n8n: import, set the two variables, edit `02-inspect-job.json`, keep workflows inactive.
4. `vercel link` to the dedicated preview project.
5. Record the human review decision for the project you intend to publish
   (`python3 -m tracking.cli review <pid> --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT
   --build-id <build-id> --notes "..." --confirm <token>`).
6. Deploy to Render only when you want the API public, and expect startup to fail until the secrets are set.

Outreach remains draft-only, and sending is disabled by configuration; this guide does not enable it.
