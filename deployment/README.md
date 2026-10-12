# Preview Deployment

The deployment layer consumes a completed local Builder artifact and its Phase 6 validation result. It is provider-neutral at the boundary (`deploy(build, options) → DeploymentResult`) and currently implements only Vercel preview deployment.

## Safety gate

Deployment requires:

1. A build directory containing `index.html` and `build.json`.
2. A manifest whose `output_location` matches that exact build directory.
3. Validation status `PASS`. `FAIL` is always blocked. `NEEDS_REVIEW` is blocked unless `--approve-needs-review` is explicitly supplied.
4. An existing linked Vercel project at `.vercel/project.json` whose recorded `projectName` is exactly `ai-website-agent-preview`.
5. A clean staged payload. The adapter copies only `index.html` plus local assets directly referenced by HTML or local CSS. It rejects missing assets, traversal, unsafe symlink targets, hidden files, manifest/metadata names, and potential secret-like values. The temporary staged directory—not the build directory—is what Vercel receives.

The adapter never invokes `vercel --prod`, never creates a project by default, and never handles a token directly. Vercel authentication, when approved, must already exist in the environment or CLI session; values are never written to source, manifests, or logs.

## Dry run

```powershell
python -m deployment.cli --build output/<lead-id>/<build-id>
```

Dry run is the default. It validates the build, validation result, build/lead association, and Vercel project link, then writes a local `deployment.json` record with `PENDING`. It does not contact Vercel.

## Execute an approved preview

```powershell
python -m deployment.cli --build output/<lead-id>/<build-id> --execute
```

This is preview-only. It calls Vercel CLI without `--prod`, captures only an HTTPS host that ends exactly in `.vercel.app`, and records a normalized result in `deployment.json`. Do not run it until an approved existing Vercel preview project is linked and the CLI is authenticated.

### Operator-only setup when blocked

If `.vercel/project.json` is missing, points to another project, or `vercel whoami` cannot confirm an authenticated session, stop before `--execute`. The operator—not the application or an agent—must:

1. Run `vercel login` and complete authentication interactively.
2. Create/configure the dedicated `ai-website-agent-preview` project manually, not through this application. For this static fixture, set Framework Preset to `Other`, enable the Build Command override and leave it empty, and serve the staged root. Do not apply those settings to `grace-light-community-church`.
3. Run `vercel link`, selecting that already approved dedicated preview project. This creates ignored local `.vercel/project.json`; never hand-write its IDs. Confirm its non-secret `projectName` is `ai-website-agent-preview`.
4. Run `vercel whoami` to confirm the intended account without copying credentials into the repository.
5. Regenerate the fictional build and run `python -m deployment.cli --build output/<lead-id>/<build-id> --execute`.

Do not use `--prod`, `vercel domains`, or an account upgrade as part of this workflow.

## States and recovery

`PENDING → PREPARING → DEPLOYING → DEPLOYED`; failures end as `FAILED`; review-gated builds end as `NEEDS_REVIEW`. The artifact-local manifest contains lead/build/validation IDs, provider, URL, project ID when available, Git SHA when available, a non-secret payload file list, timestamps, and redacted errors. Another agent can inspect this record to retry safely. A protected preview may still be a successful deployment; access protection must be reported separately and never bypassed.

## Git and GitHub

Git SHA is recorded when the local checkout has a commit. This repository currently has no commit and no Git remote, so there is no GitHub source identifier yet. The deployment layer does not commit, push, create repositories, delete branches, or change history.
