# Preview Deployment

The deployment layer consumes a completed local Builder artifact and its Phase 6 validation result. It is provider-neutral at the boundary (`deploy(build, options) → DeploymentResult`) and currently implements only Vercel preview deployment. Generating a website, passing validation, and approving a deployment are three separate facts; only the third one is checked here.

## Safety gate

Deployment requires:

1. A build directory containing `index.html` and `build.json`.
2. A manifest whose `output_location` matches that exact build directory.
3. Validation status `PASS`. `FAIL` is always blocked. `NEEDS_REVIEW` is blocked unless `--approve-needs-review` is explicitly supplied.
4. An existing linked Vercel project at `.vercel/project.json` whose recorded `projectName` is exactly `ai-website-agent-preview`.
5. A clean staged payload. The adapter copies only `index.html` plus local assets directly referenced by HTML or local CSS. It rejects missing assets, traversal, unsafe symlink targets, hidden files, manifest/metadata names, and potential secret-like values. The temporary staged directory—not the build directory—is what Vercel receives.
6. **For a real publish only (`--execute`, `dry_run=False`): an explicit human approval bound to that exact build, validation, artifact, and the `PREVIEW_DEPLOYMENT` action.** A dry run needs none, because a dry run can never publish anything.

A completed build, a `PASS` validation, an approved *plan*, or a UI/n8n condition is never authorization. The check is enforced inside `VercelDeploymentProvider.deploy()` — at the operation that shells out to the provider CLI — so it applies identically to the operator CLI, `LocalPipelineWorker`, `JobService`, and the n8n-triggered HTTP API. A refusal happens before any payload is staged, so a rejected attempt performs **no external call at all**.

## Approval capability and publish ledger

`TrackingStore` owns the durable audit record (`state["reviews"]` plus `project["review_history"]`). Because the deployment adapter is deliberately file-based and store-agnostic, an `APPROVED_FOR_PREVIEW` decision additionally materializes a narrow, build-local capability that the publish step verifies itself:

| File | Purpose |
| --- | --- |
| `output/<lead>/<build>/deployment-approval.json` | The approved decision for exactly one build, validation, artifact SHA-256, and action, with a self digest. Written by `tracking.cli review`/`TrackingStore.record_review`; revoked automatically by `REJECTED`, `CHANGES_REQUESTED`, an explicit hold, a superseding build, and by the recording of the deployment it authorized (one approval, one publish). |
| `output/<lead>/<build>/deployment-attempts.json` | Append-only ledger of publish attempts: `DEPLOYING` before the provider is invoked, then the terminal state. Refusals are recorded too, with `published: false`. |

`deployment/authorization.py` implements both, and `verify_approval()` returns a specific machine-readable code for every refusal: `APPROVAL_MISSING`, `APPROVAL_UNREADABLE`, `APPROVAL_TAMPERED`, `APPROVAL_REVOKED`, `APPROVAL_INVALID_DECISION`, `APPROVAL_WRONG_ACTION`, `APPROVAL_BUILD_MISMATCH`, `APPROVAL_LEAD_MISMATCH`, `APPROVAL_VALIDATION_MISMATCH`, `VALIDATION_NOT_PASSED`, `APPROVAL_ARTIFACT_STALE`, `BUILD_ID_UNRECORDED`, plus the ledger verdicts `DEPLOYMENT_ALREADY_RECORDED`, `PUBLISH_STATE_UNCERTAIN` and `PUBLISH_LEDGER_UNREADABLE`. Both files are build-local artifacts under the ignored `output/` tree and contain no credentials.

Provider *configuration* refusals are a separate family, because no approval can resolve them: a missing
`.vercel/project.json` is `PREVIEW_PROJECT_NOT_LINKED`, an unusable link record is
`PREVIEW_PROJECT_LINK_INVALID`, and an absent `vercel` executable is `PROVIDER_CLI_MISSING`. They surface as
`deployment_status: FAILED` with the code in `metadata.denial_code`, and the same code reaches the job row,
`inspect-job`, and the structured log line, so an operator is never told to "read the worker error" when the
fix is `vercel link`. They are `ValueError`s for existing callers, and they never enter the publish ledger:
nothing is attempted, so nothing is recorded as attempted.

Because `TrackingStore.authorize_preview_deployment(project_id)` answers the same question from the durable record (project ownership, assigned build, PASS validation, stage, approval binding, artifact presence, prior attempts), every entry point can report the identical reason before touching the provider. It reports authorization; it never grants it.

The adapter never invokes `vercel --prod`, never creates a project by default, and never handles a token directly. Vercel authentication, when approved, must already exist in the environment or CLI session; values are never written to source, manifests, or logs.

## Dry run

```powershell
python -m deployment.cli --build output/<lead-id>/<build-id>
```

Dry run is the default. It validates the build, validation result, build/lead association, and Vercel project link, then writes a local `deployment.json` record with `PENDING`. It does not contact Vercel.

## Execute an approved preview

```powershell
python -m tracking.cli deploy-check project-0001
python -m deployment.cli --build output/<lead-id>/<build-id> --execute --project-id project-0001
```

`--execute` now requires `--project-id` (plus `--state-path`, default `data/tracking/state.json`) so the command can consult the durable tracking record before anything is staged. `deploy-check` is read-only: it prints the same gate verdict the publish boundary will reach, with the exact blocking code and the review command that resolves it, and publishes nothing. If the gate refuses, `deployment.cli` exits `1` with `provider_cli_invoked: false` and no `deployment.json` is written.

`--build-id <id>` asserts which build you believe you are deploying; a mismatch with the project's assigned build is refused instead of silently deploying the newest artifact. `--resolve-uncertain-publish` (with `--resolve-note`) is the only override for an interrupted publish: it records a deliberate, attributed operator decision in the build's attempt ledger before the provider is contacted, and it never fakes a result or restates the publish outcome - see *Resolving an uncertain publish* below.

After a successful execution, the recording half stays in the store: the worker calls `TrackingStore.record_deployment`, which consumes the approval (the capability becomes `REVOKED` with the reason `approval consumed by recorded deployment ...`) so the same build cannot be published twice without a new human decision.

This is preview-only. It calls Vercel CLI without `--prod`, captures only an HTTPS host that ends exactly in `.vercel.app`, and records a normalized result in `deployment.json`. Do not run it until an approved existing Vercel preview project is linked and the CLI is authenticated.

### Operator-only setup when blocked

If `.vercel/project.json` is missing, points to another project, or `vercel whoami` cannot confirm an authenticated session, stop before `--execute`. The operator—not the application or an agent—must:

1. Run `vercel login` and complete authentication interactively.
2. Create/configure the dedicated `ai-website-agent-preview` project manually, not through this application. For this static fixture, set Framework Preset to `Other`, enable the Build Command override and leave it empty, and serve the staged root. Do not apply those settings to `grace-light-community-church`.
3. Run `vercel link`, selecting that already approved dedicated preview project. This creates ignored local `.vercel/project.json`; never hand-write its IDs. Confirm its non-secret `projectName` is `ai-website-agent-preview`.
4. Run `vercel whoami` to confirm the intended account without copying credentials into the repository.
5. Regenerate the fictional build, record the human approval (`tracking.cli review <project_id> --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT ...`), and run `python -m deployment.cli --build output/<lead-id>/<build-id> --execute --project-id <project_id>`.

Do not use `--prod`, `vercel domains`, or an account upgrade as part of this workflow.

`python3 -m orchestration.cli preflight --project-id <project_id>` reports the same configuration an
operator would otherwise discover by trial: whether the Vercel CLI is on `PATH` here, whether a project is
linked, whether the linked name is the isolated preview project, and what the durable gate currently
answers for that project - marked configuration-only, because it executes no provider command and makes no
network call.

### Order of operations for one controlled preview

`docs/INTEGRATION-SETUP.md` §5 states the whole procedure; the short form is below, and no step may be
skipped forward. A step that is unverified is not a step that succeeded.

1. Link the intended preview project by hand (`vercel login`, then `vercel link`); confirm the name, never
   hand-write IDs. `vercel whoami --non-interactive` is the proof that the CLI in *that* machine is
   authenticated. The code compares the linked `projectName`; it does not compare the `projectId`, so the
   operator matches that value against the dashboard, then reads `deployment-status`
   (`project_linked`, `linked_project_name`) to confirm the checkout really sees the link.
2. Build and validate the artifact (`tracking.cli build`, then `validate-build`), confirm the disclosure
   text matches the data classification (fictional wording only for `TEST_FIXTURE` data), and confirm the
   file is actually present: `output/` is git-ignored, so a rebuilt workspace can hold a build *record*
   without its artifact. Re-run `build` in that case and have the human review the new build id - the gate
   refuses a decision for a superseded one with `BUILD_MISMATCH`.
3. `tracking.cli deploy-check <project_id> --build-id <build-id>` - the read-only form of the gate verdict.
4. Record the human decision (`tracking.cli review ... --decision APPROVED_FOR_PREVIEW
   --action PREVIEW_DEPLOYMENT --build-id <build-id> --notes ... --confirm ...`).
5. Dry run (`deployment.cli --build <dir> --state-path ... --project-id ... --build-id ...`): verifies
   manifest, approval capability, artifact hash, and build identity; writes no ledger entry.
6. Execute, once, with the same flags plus `--execute`. Evidence of success is a recorded `DEPLOYED`
   status with the **deployment-specific** `https://<name>.vercel.app` URL the provider returned, whose
   `build_id` and `artifact_sha256` match the artifact that was staged, and which answers when fetched -
   nothing else counts. The project's stable alias (`https://<project>.vercel.app`) proves nothing about
   this build: it keeps serving whatever was last published to it, including an older project's artifact,
   and a deployment created by a connected Git repository is not this gate's deployment at all (check
   `gh api repos/<owner>/<repo>/commits/<sha>/check-runs`). This repository has no path that marks a preview
   deployed without the provider's own answer.
7. `deployment-status` / `GET /deployment-status` to read the outcome back, and `attention` for anything
   that needs a human afterwards (an uncertain publish stays uncertain until §"Resolving an uncertain
   publish" is followed).

## States and recovery

`PENDING → PREPARING → DEPLOYING → DEPLOYED`; failures end as `FAILED`; review-gated or unauthorized builds end as `NEEDS_REVIEW`.

A publish that stops answering mid-flight is **not** recorded as a failure. If the provider CLI times out or returns no verifiable result, the attempt ends as `PUBLISH_UNCERTAIN`: the preview may already exist, so the next attempt is refused with `PUBLISH_STATE_UNCERTAIN` until an operator inspects the Vercel project and either records the real outcome or re-runs with `--resolve-uncertain-publish`. Recovery therefore never guesses, never double-publishes, and never assumes that an interrupted call did nothing.

The refusal names the evidence rather than restating a generic warning: the attempt number, deployment id, target project, and the recorded time, plus both ways out. `deploy-check` reports the same condition and points at the resolution command instead of the approval command when the blockage is the publish state.

### Resolving an uncertain publish (explicit, recorded, not standing permission)

```bash
python3 -m deployment.cli --build output/<lead-id>/<build-id> --execute --project-id <project_id> \
  --resolve-uncertain-publish --resolve-note "Provider project list shows no deployment for this artifact hash; retrying once." \
  --resolved-by chioma@example.com
```

- `--resolve-note` is **required** with `--resolve-uncertain-publish`; an anonymous "just retry it" is refused before anything changes, and `--resolve-note` without the flag is refused too (a note that would be silently dropped is a worse outcome than an error). `--resolved-by` defaults to `$AI_WEBSITE_AGENT_OPERATOR_ID`; an attribution string is a claim about who typed it, never verified identity.
- Before the provider is contacted, one entry is appended to `deployment-attempts.json`: `deployment_status: "UNCERTAIN_PUBLISH_RESOLVED"`, the attempt and status it resolves, `resolved_by`, `resolution_note`, `created_at`, and `published: false` / `provider_state_verified: false`. It records a *decision*, not an outcome. If the ledger cannot be written, the publish does not proceed.
- The entry is an annotation, so read paths skip it: `publish_outcome()` still answers `UNCERTAIN` / `PUBLISH_STATE_UNCERTAIN` (with `resolution_count` and `latest_resolution` alongside it), `latest_publish_attempt()` still returns the unresolved provider-facing entry, and a *later* run is refused again unless the flag is given again. A resolution is never standing permission.
- The gate refusal that an uncertain publish produces (`NEEDS_REVIEW` plus a `denial_code` entry in the same ledger) is bookkeeping, not a terminal publish result, so it cannot clear the uncertainty. An earlier build read "the latest line in the ledger" and would let the next run republish freely; both `classify_prior_attempt()` and `publish_outcome()` now decide from the last entry that actually reports what the provider was asked.
- A `deployment-attempts.json` that exists but does not parse is reported as `PUBLISH_LEDGER_UNREADABLE` and refuses the publish: damaged history is never treated as "nothing attempted", and appending refuses rather than replacing the file.
- The worker path is the same boundary: a `DEPLOY` job input of `{"resolve_uncertain_publish": true, "resolve_note": "...", "resolved_by": "..."}` records the annotation first, and the durable `authorize_preview_deployment()` pre-check only steps aside for that explicit flag - it never records or publishes anything itself.

`PENDING` (dry run) is a verification record, not a deployment: it never advances a project stage and `TrackingStore.record_deployment` refuses to store anything but `DEPLOYED`. A retried job whose build is already `DEPLOYED` returns `ALREADY_DEPLOYED_REPEAT_SKIPPED` and does not call the provider again.

The artifact-local manifest contains lead/build/validation IDs, provider, URL, project ID when available, Git SHA when available, a non-secret payload file list, timestamps, the `denial_code` of a refusal, and redacted errors. Another agent can inspect this record plus `deployment-approval.json` and `deployment-attempts.json` to retry safely. A protected preview may still be a successful deployment; access protection must be reported separately and never bypassed.

## Git and GitHub

Git SHA is recorded when the local checkout has a commit. This repository currently has no commit and no Git remote, so there is no GitHub source identifier yet. The deployment layer does not commit, push, create repositories, delete branches, or change history.
