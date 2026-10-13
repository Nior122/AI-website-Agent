-- Migration: 0001_supabase_postgres_schema.sql
-- Purpose: Normalized, transactional PostgreSQL schema for AI Website Agent
-- Compatible with: Supabase PostgreSQL (PostgreSQL 15+)
-- How to apply:
--   Option 1 (psql):
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_supabase_postgres_schema.sql
--   Option 2 (Python CLI):
--     python3 -m tracking.cli migrate-db
--   Option 3 (Supabase SQL Editor):
--     Paste the contents of this file into the Supabase SQL Editor and run.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    checksum TEXT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE schema_migrations
    ADD COLUMN IF NOT EXISTS checksum TEXT;

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    business_name TEXT NOT NULL,
    data_classification TEXT NOT NULL,
    qualification_status TEXT NOT NULL,
    website_status TEXT,
    city TEXT,
    country TEXT,
    phone TEXT,
    email TEXT,
    existing_website TEXT,
    instagram_url TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_leads_classification_qualification
    ON leads (data_classification, qualification_status);
CREATE INDEX IF NOT EXISTS idx_leads_business_name
    ON leads (business_name);

CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    evidence_tier TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    source_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_research_evidence_lead_id
    ON research_evidence (lead_id);

CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    website_status TEXT,
    score INTEGER,
    priority TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qualifications_lead_id
    ON qualifications (lead_id);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE RESTRICT,
    project_status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    review_status TEXT NOT NULL,
    assigned_build_id TEXT,
    latest_validation_id TEXT,
    latest_deployment_id TEXT,
    approved_build_id TEXT,
    approved_action TEXT,
    isolated_preview_project TEXT,
    lock_version INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_lead_id
    ON projects (lead_id);
CREATE INDEX IF NOT EXISTS idx_projects_stage_review
    ON projects (current_stage, review_status);

CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE RESTRICT,
    template_id TEXT,
    status TEXT NOT NULL,
    output_location TEXT,
    artifact_sha256 TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_builds_project_id
    ON builds (project_id);
CREATE INDEX IF NOT EXISTS idx_builds_lead_id
    ON builds (lead_id);

CREATE TABLE IF NOT EXISTS validations (
    validation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL REFERENCES builds(build_id) ON DELETE RESTRICT,
    lead_id TEXT,
    status TEXT NOT NULL,
    score INTEGER,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validations_project_build
    ON validations (project_id, build_id);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    build_id TEXT,
    decision TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    intended_action TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_project_id
    ON reviews (project_id);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL REFERENCES builds(build_id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    deployment_url TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deployments_project_id
    ON deployments (project_id);

CREATE TABLE IF NOT EXISTS outreach_packages (
    outreach_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    lead_id TEXT NOT NULL REFERENCES leads(lead_id) ON DELETE RESTRICT,
    build_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outreach_project_id
    ON outreach_packages (project_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    lead_id TEXT,
    project_id TEXT,
    build_id TEXT,
    validation_id TEXT,
    deployment_id TEXT,
    parent_job_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    heartbeat_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_type
    ON jobs (status, job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_project_id
    ON jobs (project_id);

CREATE TABLE IF NOT EXISTS job_executions (
    execution_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    worker_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_job_executions_job_id
    ON job_executions (job_id);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    worker_type TEXT NOT NULL,
    status TEXT NOT NULL,
    health_status TEXT NOT NULL,
    current_job_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_locks (
    project_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS system_kv (
    key TEXT PRIMARY KEY,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_artifacts (
    build_id TEXT PRIMARY KEY,
    project_id TEXT,
    lead_id TEXT NOT NULL,
    template_id TEXT,
    artifact_path TEXT NOT NULL,
    html_content TEXT NOT NULL,
    html_bytes INTEGER NOT NULL,
    html_sha256 TEXT NOT NULL,
    build_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_package_json JSONB,
    review_readme_md TEXT,
    review_package_sha256 TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_build_artifacts_project_id
    ON build_artifacts (project_id);
CREATE INDEX IF NOT EXISTS idx_build_artifacts_lead_id
    ON build_artifacts (lead_id);

INSERT INTO schema_migrations (version, description)
VALUES ('0001_supabase_postgres_schema', 'Initial normalized PostgreSQL schema for tracking state and durable build artifacts')
ON CONFLICT (version) DO NOTHING;

COMMIT;
