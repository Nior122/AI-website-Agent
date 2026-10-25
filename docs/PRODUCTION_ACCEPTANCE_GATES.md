# Production Acceptance Gates — 2026-10-25

This checklist distinguishes local CI from live operational evidence. A passing unit suite is required, but it does not prove that Render, Vercel, Gmail, or n8n is ready to serve real customers.

## Current evidence

- **Code integration:** per-business site provisioning, site inventory/deletion approvals, Telegram publish consent, opt-in AI email drafting, storage changes, and tests have been ported onto the current `main` baseline.
- **Automated tests:** GitHub Actions run [40161572301](https://github.com/Nior122/AI-website-Agent/actions/runs/40161572301) completed successfully on Ubuntu and Windows. Each platform ran 821 tests.
- **Render API health:** a read-only fetch of `https://ai-website-agent-api.onrender.com/healthz` returned HTTP 503 on 2026-10-25. This is a failed probe, not proof of its root cause. The Render service logs and current deployment state still need inspection.
- **Vercel Git status is not live-preview evidence:** deployment `dpl_9r2Hp9my8f9SaHZ4PBrvRpwEBPYs` for this branch was `CANCELED` with Vercel's `ignored-build-step` error link. The connected shared project `ai-website-agent-preview` also has Vercel Authentication enabled and is not a customer-ready public preview. No existing project was changed.
- **Commercial account policy:** the connected Vercel team currently reports the Hobby plan. Vercel describes Hobby as for personal, non-commercial use; a commercial client-site service needs an appropriate plan or a different approved hosting arrangement before launch.
- **Outbound communications:** the live n8n go-live notes say the Gmail `gmail.send` credential is absent and Workflow 05 must remain inactive. A separate Stage 10 doc reports all five workflows inactive, so the live n8n state must be inspected and these documents reconciled before activation. No email has been sent by this integration work.

## Release gates

- [x] Current-main integration preserved; old PR #2 superseded by PR #3.
- [x] Ubuntu and Windows automated suite green (821 tests each).
- [ ] Inspect the current Render deploy, logs, and authenticated readiness probe; restore `/healthz` to HTTP 200 and `/readiness` to `READY` before release.
- [ ] With explicit operator authorization, run a smoke test using a newly created disposable Vercel project: confirm provider-returned project ID/name, confirmed preview-access settings, successful deployment ID and `READY` state, then make a public HTTPS request to the generated preview URL. Do not use or overwrite the shared project or a customer site.
- [ ] Confirm commercial hosting plan/policy before using the service for paying client websites.
- [ ] Inspect n8n live workflow activation and credentials. Keep outbound sending inactive until the separate Gmail send scope/credential has been authorized and tested with a provider-safe test recipient plus explicit send approval.
- [ ] Reconcile the unresolved `outreach-0005` send attempt against Gmail/provider records before retrying anything; do not infer a send from a timestamp alone.

## Safety boundaries used during this integration

No production deployment was triggered. No Vercel project was created, changed, or deleted. No custom domain was purchased or attached. No n8n workflow was activated. No outbound email was sent. The PR remains a draft until the live acceptance gates above are evidenced.
