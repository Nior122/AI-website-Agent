# Managed business sites and AI outreach drafts

This phase adds inventory and explicit deletion approval for Vercel projects already represented by successful deployment records. It also adds an opt-in AI email copywriter. It does **not** yet create a Vercel project or deploy a new business site automatically; that needs a separate project-provisioning/deployment change so it cannot overwrite the shared preview project.

## Telegram site commands

- `/sites` lists recorded successful deployments, stable site IDs, current URLs, Vercel project names, and recorded deployment-history counts.
- `/sites --all` includes deletion tombstones.
- `/site <site-id>` shows one record.
- `/deletesite <site-id>` requests a deletion review only. It does not delete.
- `/confirmdelete <site-id> <fingerprint>` is the second explicit action. The fingerprint expires after ten minutes.

Deletion is refused if the Vercel project ID is missing, project identity/name changed, the project is shared by more than one tracking project, or it is protected. The service re-reads the live Vercel project before deletion and persists a DELETING state before the external request. An ambiguous provider response becomes DELETE_UNCERTAIN and is **not** retried automatically. Check the Vercel dashboard and reconcile manually before creating a new request.

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
