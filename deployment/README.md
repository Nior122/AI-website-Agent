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

`deployment/authorization.py` implements both, and `verify_approval()` returns a specific machine-readable code for every refusal: `APPROVAL_MISSING`, `APPROVAL_UNREADABLE`, `APPROVAL_TAMPERED`, `APPROVAL_REVOKED`, `APPROVAL_INVALID_DECISION`, `APPROVAL_WRONG_ACTION`, `APPROVAL_BUILD_MISMATCH`, `APPROVAL_LEAD_MISMATCH`, `APPROVAL_VALIDATION_MISMATCH`, `VALIDATION_NOT_PASSED`, `APPROVAL_ARTIFACT_STALE`, `BUILD_ID_UNRECORDED`, plus the ledger verdicts `DEPLOYMENT_ALREADY_RECORDED` and `PUBLISH_STATE_UNCERTAIN`. Both files are build-local artifacts under the ignored `output/` tree and contain no credentials.

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

`--build-id <id>` asserts which build you believe you are deploying; a mismatch with the project's assigned build is refused instead of silently deploying the newest artifact. `--resolve-uncertain-publish` is the only override for an interrupted publish, and it merely records a deliberate operator resolution — it never fakes a result.

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

## States and recovery

`PENDING → PREPARING → DEPLOYING → DEPLOYED`; failures end as `FAILED`; review-gated or unauthorized builds end as `NEEDS_REVIEW`.

A publish that stops answering mid-flight is **not** recorded as a failure. If the provider CLI times out or returns no verifiable result, the attempt ends as `PUBLISH_UNCERTAIN`: the preview may already exist, so the next attempt is refused with `PUBLISH_STATE_UNCERTAIN` until an operator inspects the Vercel project and either records the real outcome or re-runs with `--resolve-uncertain-publish`. Recovery therefore never guesses, never double-publishes, and never assumes that an interrupted call did nothing.

`PENDING` (dry run) is a verification record, not a deployment: it never advances a project stage and `TrackingStore.record_deployment` refuses to store anything but `DEPLOYED`. A retried job whose build is already `DEPLOYED` returns `ALREADY_DEPLOYED_REPEAT_SKIPPED` and does not call the provider again.

The artifact-local manifest contains lead/build/validation IDs, provider, URL, project ID when available, Git SHA when available, a non-secret payload file list, timestamps, the `denial_code` of a refusal, and redacted errors. Another agent can inspect this record plus `deployment-approval.json` and `deployment-attempts.json` to retry safely. A protected preview may still be a successful deployment; access protection must be reported separately and never bypassed.

## Git and GitHub

Git SHA is recorded when the local checkout has a commit. This repository currently has no commit and no Git remote, so there is no GitHub source identifier yet. The deployment layer does not commit, push, create repositories, delete branches, or change history.
