# Managed business sites and AI outreach drafts

This phase adds per-business Vercel project provisioning for approved preview deployments, inventory and explicit deletion approval, and an opt-in AI email copywriter. Project creation is separate from deployment: the agent records a durable creation intent, verifies the provider's returned project ID/name, binds that exact project to one tracked business, and only then attempts to publish. It never repurposes the shared preview project, attaches or purchases custom domains, deploys to production, or deletes a project as part of provisioning.

## Telegram site commands

- `/sites` lists recorded successful deployments, stable site IDs, current URLs, Vercel project names, and recorded deployment-history counts.
- `/sites --all` includes deletion tombstones.
- `/site <site-id>` shows one record.
- `/deletesite <site-id>` requests a deletion review only. It does not delete.
- `/confirmdelete <site-id> <fingerprint>` is the second explicit action. The fingerprint expires after ten minutes.

Deletion is refused if the Vercel project ID is missing, project identity/name changed, the project is shared by more than one tracking project, or it is protected. A never-deployed project can be cleaned up only when its durable provisioning history proves a verified one-to-one binding and the deployment ledger confirms zero deployments. That approval becomes stale if the first deployment arrives before confirmation. The service re-reads the live Vercel project before deletion and persists a DELETING state before the external request. An ambiguous provider response becomes DELETE_UNCERTAIN and is **not** retried automatically. Check the Vercel dashboard and reconcile manually before creating a new request.

A Vercel project deletion can remove all deployments and affect attached domains; it is not equivalent to deleting one deployment. Custom-domain registration/purchase is not performed by this feature. Site inventory does not fabricate project IDs from URLs, so older deployments without a recorded Vercel project ID remain listed as unmanaged and cannot be deleted automatically.

## Vercel configuration

Configure these on the API/Telegram runtime only, never in Git:

- `AI_WEBSITE_AGENT_VERCEL_TOKEN`: a least-privilege token with the Vercel project read/delete scopes required for the management operations.
- `AI_WEBSITE_AGENT_VERCEL_TEAM_ID`: the exact Vercel team/account ID; required to scope all project requests.
- `AI_WEBSITE_AGENT_VERCEL_PROTECTED_PROJECT_IDS`: comma-separated immutable project IDs to protect.
- `AI_WEBSITE_AGENT_VERCEL_PROTECTED_PROJECT_NAMES`: optional comma-separated additional project names to protect.

The built-in protected names include the shared `ai-website-agent-preview` project and portfolio names. Verify these against your current Vercel account before enabling the commands. Never grant project deletion to a token used by unrelated automation if a narrower token is available.

## AI email drafting

AI copywriting is disabled by default. Enable it only after the provider environment is configured:

- `AI_WEBSITE_AGENT_EMAIL_COPYWRITER_ENABLED=1`
- `AI_WEBSITE_AGENT_EMAIL_COPYWRITER_PROVIDERS=openrouter,nvidia` (ordered fallback list; allowed values only)
- `AI_WEBSITE_AGENT_OPENROUTER_API_KEY`
- `AI_WEBSITE_AGENT_OPENROUTER_EMAIL_MODEL=openrouter/free`
- Optional `AI_WEBSITE_AGENT_NVIDIA_API_KEY`, `AI_WEBSITE_AGENT_NVIDIA_EMAIL_MODEL`, and `AI_WEBSITE_AGENT_NVIDIA_CHAT_COMPLETIONS_URL`

Use a NVIDIA production endpoint only when the chosen model/endpoint's terms permit the intended production use; the NVIDIA route refuses production use unless `AI_WEBSITE_AGENT_NVIDIA_PRODUCTION_LICENSE_CONFIRMED=1` is explicitly set by an operator who verified that authorization. Do not set that flag merely to bypass the gate.

OpenRouter's free router is a best-effort route, not a quota or uptime guarantee. The writer only permits `openrouter/free` or a model ID ending in `:free`; it does not silently switch to a paid model. If all configured providers fail, no AI draft is saved. Existing deterministic copy remains the default until the explicit enable flag is set.

The model returns only a proposed subject/body and metadata. Existing review and content-fingerprint approval remain the authority. Editing recipient, subject, body, preview URL, or other fingerprint-covered content invalidates approval. Sending is never triggered by draft generation.

## Email preview and send gates

The Telegram `/review <outreach-id>` and `/send <outreach-id>` preview now show the full recipient, subject, exact preview URL, body, and content fingerprint to the allow-listed operator. The general `/review` list and ordinary notifications remain minimized/contact-masked. A preview is not approval; the existing approval, opt-out, hold, budget, and send confirmation checks still apply.

Do not retry the unresolved historical outreach attempt just because a message appears around the same time in Gmail Sent. Reconcile against exact recipient, subject/body, and provider message ID first. No email was sent by this change.

## Verification

The new tests are offline and use fake Vercel/provider clients. The repository's Python test workflow is defined in `.github/workflows/python-tests.yml`. Review the resulting GitHub Actions run and all required checks before merging. A successful Vercel preview status is not a substitute for Python tests or human review.

## Per-business Vercel project provisioning

The approved DEPLOY worker can provision one standalone Vercel project per REAL_PUBLIC_DATA tracking project. It only runs after the current assigned build passes the normal human-approval gate and the job includes the separate explicit approve_real_preview flag. A dry run never creates a Vercel resource. Project creation uses the Vercel Projects API and stores the returned project ID/name on the tracked project before attempting deployment; the CLI is then pointed at that verified ID via VERCEL_ORG_ID and VERCEL_PROJECT_ID. The shared fictional preview project and portfolio projects are never used for real-business output. New prospect-facing projects explicitly clear Vercel Authentication (ssoProtection: null) so the generated vercel.app preview URL can be opened without membership in the operator Vercel team. This is intentional because these are public website previews: never put private customer data, credentials, or non-public information in the site payload.

The management token therefore needs project read, create, and (only if you use Telegram deletion) project delete permissions in the same team scope. Keep AI_WEBSITE_AGENT_VERCEL_TOKEN and AI_WEBSITE_AGENT_VERCEL_TEAM_ID set on the worker/API runtime. The deploy worker provides the token to the child Vercel CLI through its process environment, not command-line arguments. Do not print process environments or tokens in diagnostic output.

Project names are deterministically derived from the business and tracked project ID, include a preview marker, and fit Vercel's project-name length bound. If a name is already taken, the allocator checks a bounded sequence of suffixed alternatives; it never adopts a project simply because its name matches. Each tracked project may bind to only one Vercel project ID, and the same Vercel project ID cannot be bound to two tracked projects. A saved manual project name without a verified project ID stops automatic provisioning until an operator reconciles it.

Provisioning persists a creation intent before making the API request. If the API response is lost, the agent checks the exact intended name and verifies that provider creation time is consistent with the durable intent before recovering the mapping. If it cannot prove that the project was created by this intent, it stops and requests manual reconciliation instead of creating another project. A definite name collision may advance to another deterministic suffix. An uncertain create or failed binding never proceeds to deployment.

Project provisioning does not import a Git repository, assign a custom domain, buy a domain, deploy to production, or delete any project. It creates a standalone project and deploys a preview artifact only after verifying the exact saved project ID/name pair. Deployment failures after project creation leave the project mapped for safe inspection/retry rather than silently creating a replacement.
