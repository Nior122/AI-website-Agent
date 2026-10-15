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

**Linking is an authenticated, human action — and only three things can prove the identity.** A project
*name* and a public URL are not a link. Verify in this order, and accept nothing you cannot point at:

1. **The dashboard** (`Settings → General`): the project's slug and its `prj_…` id. The name must be
   `ai-website-agent-preview`, which is what `deployment/deployer.py` compares against
   (`EXPECTED_PREVIEW_PROJECT`), and the slug rules additionally refuse anything that looks like a
   production or live target. Nothing in this repository creates the project.
2. **`vercel link --project ai-website-agent-preview`**, run *in the checkout that will publish*, by someone
   who has authenticated that CLI (`vercel login`, or an environment the operator controls). It is the link
   command that writes `.vercel/project.json`; **that file must never be hand-written**, because its
   `projectId` is what decides where the bytes go. `git check-ignore .vercel` confirms it stays out of Git.
3. **The repo's own read-back**: `python3 -m orchestration.cli deployment-status` reports
   `configuration.project_linked`, `linked_project_name`, and `cli_available`, so after step 2 the answer is
   `ai-website-agent-preview` rather than `null`. The code pins the linked **name**; it does not compare the
   linked **id** against a recorded expectation, so the `prj_…` value from step 1 is the operator's to match
   against the file from step 2. After an executed publish, `deployment.json` and
   `deployment-attempts.json` carry the `vercel_project_id` that was actually used.

**A public project URL is not evidence about a build.** `https://ai-website-agent-preview.vercel.app` is the
project's stable alias: it answers even when the project has never received the artifact you are holding,
when an older artifact is what is live, or when a Git-triggered deploy published something else entirely. The
evidence that *this* build was published is (a) a recorded `DEPLOYED` whose `build_id` and
`artifact_sha256` match the artifact you staged, (b) the matching `DEPLOYING` → `DEPLOYED` pair in the
build's `deployment-attempts.json`, and (c) a fetch of the **deployment-specific** URL the provider returned
(a `*.vercel.app` host, checked for the disclosure and the expected content), not the alias alone.
`deployment-status` deliberately reports `live_url_checked: false`: this repository never upgrades its own
record into a claim about the live web.

**A connected Git repository publishes without asking this gate.** Vercel's Git integration deploys on push —
preview deployments for pushed branches, and the production deployment for the project's production branch —
and it never consults the review, the validation result, or the authorization capability. Two consequences
have to be decided deliberately, in the project's settings, not assumed:

- **A Git-triggered deploy cannot publish a validated artifact.** Builds live in `output/`, which is
  git-ignored (as are `.vercel/` and `data/tracking/`), so the repository contains no `index.html` to
  deploy. Pointing the project's Root Directory / Build Output at the repo root would publish source and
  committed fixtures instead of a reviewed page — a different thing than the artifact the human approved.
- **Keep the automatic path off unless you want it.** Vercel's `Settings → Git → Ignored Build Step` (or
  disabling Preview Deployments, or disconnecting the repository) is what guarantees that pushing a branch
  cannot publish. Changing that setting is an operator decision, and this repository will not change it for
  you. To see whether a push already triggered something, without dashboard access:
  `gh api repos/<owner>/<repo>/commits/<sha>/check-runs` and `…/commits/<sha>/status` — a Vercel Git deploy
  posts a `Vercel` check-run whose title is the deployment URL; an empty list means no deployment is
  attributed to that commit.

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

## 8. Before the first real preview: six checks, then the handoff

A provider call is allowed only when all six of these are true **at the same time**. Each has a command that
answers it from records rather than from hope, and `python3 -m orchestration.cli preflight --project-id
<project_id>` collects five of them into one document (the sixth is the operator's own decision):

| # | Must be true | Command that proves it | What a "yes" looks like |
| --- | --- | --- | --- |
| 1 | The build exists and validation passed for **that** build | `python3 -m tracking.cli snapshot <project_id>` | `latestBuild.status COMPLETED`, `latestValidation.status PASS`, `artifact_exists true` |
| 2 | A human approval is recorded for **that exact build** | `python3 -m tracking.cli deploy-check <project_id> --build-id <build-id>` | `authorized true`, `code APPROVED_FOR_DEPLOYMENT`, and the `build_id`/`validation_id` in the answer are the ones you are publishing |
| 3 | The authorization capability is intact and not revoked | same `deploy-check` (it verifies the build-local capability and artifact digest) | no `APPROVAL_REVOKED`, `APPROVAL_TAMPERED`, or `APPROVAL_ARTIFACT_STALE` |
| 4 | The provider is configured **and authenticated** in the machine that will publish | `python3 -m orchestration.cli deployment-status`, then `vercel whoami --non-interactive` in the publishing machine | `configuration.cli_available true`, `project_linked true`, `linked_project_name` equal to the expected preview project, and `whoami` naming the account that owns it |
| 5 | The target is the intended preview project | `python3 -m orchestration.cli preflight --project-id <project_id>` | `deployment_provider_configuration.expected_preview_project` equals the linked name; no `--prod` path exists in this repository |
| 6 | The operator has explicitly authorised a real preview deployment | - | a human decision, recorded nowhere else; the commands refuse without it |

Reading these answers changes nothing: `deploy-check` and `preflight` never publish, never consume an
approval, and never write to the publish ledger.

**Configuration refusals name themselves.** When the checkout cannot reach the provider at all, the failure
is recorded with its own code instead of a generic worker fault, because no human approval can fix them:

| Code | Meaning | Remedy in `next_action` |
| --- | --- | --- |
| `PREVIEW_PROJECT_NOT_LINKED` | no `.vercel/project.json` in this checkout | `vercel link --project <preview-project>`, then re-run `deploy-check` |
| `PREVIEW_PROJECT_LINK_INVALID` | the link file is unparseable, has no `projectId`, or names another project | re-link with the CLI instead of editing the record |
| `PROVIDER_CLI_MISSING` | the `vercel` executable is not on `PATH` | install and authenticate the CLI in the environment that publishes |

They appear identically in `deployment.json` (`metadata.denial_code`), in the job row, in `inspect-job`, and
in the structured log line, so the operator-facing surfaces cannot disagree.

**The artifact has to be present, not merely recorded.** `output/` is git-ignored, so a rebuilt or freshly
cloned workspace can hold the *record* of a build while missing its file. `snapshot <project_id>` compares
them (`latestBuild.artifact_sha256` against the bytes on disk, `artifact_exists: true`); when the artifact is
gone, re-run `build` and let the review name the **new** build id — an approval for the superseded id is
refused with `BUILD_MISMATCH`, which is the gate protecting you rather than an obstacle to route around.
Rebuilding the same verified data from the same template produces an identical digest, so the human review
still covers the same page.

**When this machine is not the one that publishes.** A sandbox, CI runner, or fresh clone normally answers
`cli_available: false` / `project_linked: false`, and provider credentials must never be pasted into a shared
session. The prepared handoff is:

```bash
# 1. On the publishing machine: create the review package and read it before deciding anything.
python3 -m tracking.cli review-package <project_id> --repo-root . \
    --export-dir preview-export/<lead_id>-<slug>
# 2. Still there: the human decision, bound to one build, with the operator's own handle.
python3 -m tracking.cli review <project_id> --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT \
    --notes "what was actually checked" --reviewer <handle> \
    --confirm <project_id>:<build-id>:APPROVED_FOR_PREVIEW
# 3. Link, verify, publish once, preview-only.
vercel link --project ai-website-agent-preview
python3 -m tracking.cli deploy-check <project_id> --build-id <build-id>
python3 -m deployment.cli --build output/<lead_id>/<build-id> --state-path data/tracking/state.json \
    --project-id <project_id> --build-id <build-id> --execute
```

Evidence that a preview exists is a recorded `DEPLOYED` with an `https://<name>.vercel.app` URL, plus the
matching `DEPLOYING`/`DEPLOYED` pair in `deployment-attempts.json`. Anything else - a timeout, an empty
provider response - stays `PUBLISH_UNCERTAIN` and is resolved by §7, never by retrying blind. A dry run is
never evidence of a deployment, and no command in this repository reports one as such.

## 9. What remains manual

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

A workspace that has run §8 and cannot reach the provider should stop there with
`PREVIEW_PROJECT_NOT_LINKED` or `PROVIDER_CLI_MISSING` recorded. That is the expected end state for a
sandbox, a CI runner, or a fresh clone - not a condition to route around by creating `.vercel/project.json`
by hand, storing a token in the repository, or reporting a dry run as a deployment.

Outreach remains draft-only, and sending is disabled by configuration; this guide does not enable it.
