# Nior Operations Runbook & Daily Operator Guide

This runbook guides operators on managing daily tasks, reviewing prospects, monitoring queue health, executing deployments, and handling incidents.

---

## 1. Quick Operator Health Check

To inspect the system health and active attention items:

```powershell
# 1. Check system-wide health and blockers
python -m orchestration.cli health

# 2. View operator attention items (pending reviews, failed workers, uncertain publishes)
python -m orchestration.cli attention --brief

# 3. Inspect deployment status across all projects
python -m orchestration.cli deployment-status
```

---

## 2. Scout Discovery & Lead Pipeline Workflow

### Step 1: Run Scout Discovery
Run a bounded discovery against OpenStreetMap Overpass (e.g., Lagos restaurants):
```powershell
python -m scout.cli --source osm --city Lagos --category restaurant --limit 10
```
*Note: Candidates are ingested with status `NEEDS_REVIEW` by default.*

### Step 2: Human Lead Qualification
Review discovered candidates:
```powershell
# List pending reviews
python -m tracking.cli leads --status NEEDS_REVIEW

# Qualify a verified business
python -m tracking.cli qualify-lead <lead_id> --decision QUALIFIED --notes "Verified active physical location in Ikeja"
```

### Step 3: Trigger Website Build
```powershell
python -m builder.cli build --lead-id <lead_id> --template restaurant-menu-v1
```

### Step 4: Validate Static Quality
```powershell
python -m validation.cli validate --build-id <build_id>
```

### Step 5: Review Package Export & Approval
Export the review package for local inspection before preview deployment:
```powershell
python -m tracking.cli review-package <project_id> --export-dir preview-export/<project_id>

# If approved by operator:
python -m tracking.cli review <project_id> --decision APPROVED_FOR_PREVIEW --action PREVIEW_DEPLOYMENT --notes "Reviewed menu copy and branding layout" --confirm <project_id>:<build_id>:APPROVED_FOR_PREVIEW
```

---

## 3. Preview Deployment Workflow

Deployments are strictly restricted to isolated preview environments (`--project-name` must match isolated target):

```powershell
# 1. Preflight deploy authorization
python -m tracking.cli deploy-check <project_id>

# 2. Execute dry-run first
python -m deployment.cli --build output/<lead_id>/<build_id> --project-name ai-website-agent-preview

# 3. Execute live preview deployment (requires explicit operator flags)
python -m deployment.cli --build output/<lead_id>/<build_id> --execute --project-id <project_id> --project-name ai-website-agent-preview
```

---

## 4. Outbound Email & Communication Console

Outreach proposals are draft-only until explicitly sent through the dual-gate:

```powershell
# 1. Inspect outbound outbox
python -m orchestration.cli email-outbox

# 2. Preview proposal text
python -m orchestration.cli email-preview --outreach-id <outreach_id>

# 3. Dry-run send
python -m orchestration.cli email-send --outreach-id <outreach_id> --dry-run

# 4. Live send (requires confirmation flag)
python -m orchestration.cli email-send --outreach-id <outreach_id> --confirm SEND --allow-live
```

---

## 5. Incident Response & Recovery Procedures

### Incident A: Worker Crashed or Heartbeat Expired
When a worker crashes while processing a job, the job remains in `RUNNING` status with an expired heartbeat.
```powershell
# Recover stale jobs older than 15 minutes
python -m orchestration.cli recover-stale --older-than-seconds 900
```

### Incident B: Uncertain Publish State
If a Vercel deployment network timeout occurred, the attempt ledger records `PUBLISH_UNCERTAIN`:
```powershell
# Check ledger details
python -m orchestration.cli deployment-status

# Resolve the uncertain state after checking the Vercel web console
python -m deployment.cli --build output/<lead_id>/<build_id> --execute --project-id <project_id> --project-name ai-website-agent-preview --resolve-uncertain-publish --resolve-note "Checked Vercel dashboard: deployment failed due to network timeout, safe to retry." --resolved-by "operator:name"
```

### Incident C: Emergency Dispatch Hold
To pause all automated scheduling and n8n job dispatches immediately:
```powershell
# Place system on automation hold
python -m orchestration.cli hold-automation --actor "operator:name" --reason "Investigating email bounce rate"

# Resume automated dispatches
python -m orchestration.cli release-automation --actor "operator:name"
```
