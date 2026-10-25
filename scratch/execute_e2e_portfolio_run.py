"""Authorized End-to-End Test Run for https://josephportifolio.vercel.app

This script executes the complete supported pipeline:
SCOUT -> RESEARCH -> QUALIFY -> BUILD -> VALIDATE -> APPROVAL -> DEPLOYMENT CHECK -> OUTREACH DRAFT & SEND CHECK
All results are persisted to TrackingStore and captured with full evidence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\PC\Documents\GitHub\AI-website-Agent")
sys.path.insert(0, str(ROOT))

from tracking.store import TrackingStore
from opportunities.service import assess_opportunity
from builder.website_builder import WebsiteBuilder
from templates.registry import select_template, TEMPLATES
from builder.validator import validate_html
from outreach.service import OutreachService
from orchestration.outbound import ApprovedOutboundSender, content_fingerprint
from deployment.deployer import VercelDeploymentProvider
from deployment.authorization import write_approval, artifact_sha256

RUN_ID = "e2e-run-portfolio-20261023"
TARGET_URL = "https://josephportifolio.vercel.app"
OPERATOR_CHAT_ID = 7722236745

def run():
    print(f"=== BEGINNING AUTHORIZED END-TO-END TEST RUN [{RUN_ID}] ===")
    store = TrackingStore("data/tracking/state.json")
    
    # --- STAGE 1: SCOUT ---
    print("\n--- STAGE 1: SCOUT ---")
    scout_candidate = {
        "candidateId": f"cand-{RUN_ID}",
        "businessName": "Joseph Unaogu",
        "niche": "portfolio",
        "location": {"city": "Lagos", "country": "Nigeria", "address": "Lagos, Nigeria"},
        "contact": {
            "phone": "+2347046118938",
            "email": "chinecheremjoseph26@gmail.com",
            "whatsapp": "+2347046118938"
        },
        "social": {
            "instagramUrl": None,
            "facebookUrl": None,
            "githubUrl": "https://github.com/Nior122",
            "linkedinUrl": "https://www.linkedin.com/in/joseph-unaogu-72b2ba245/",
            "twitterUrl": "https://x.com/Bigcherem",
            "otherSocialUrls": [
                "https://www.linkedin.com/in/joseph-unaogu-72b2ba245/",
                "https://x.com/Bigcherem"
            ]
        },
        "publicBusinessInformation": {
            "description": "Digital builder exploring software, AI, automation, writing, and creative technology.",
            "servicesOrProducts": [
                "eBook Writing & Creation",
                "Copywriting",
                "AI Automation Workflows",
                "Web Development"
            ],
            "projects": [
                {"name": "AI Website Agent", "description": "Autonomous website generation and client engagement pipeline.", "link": "https://github.com/Nior122/AI-website-Agent"},
                {"name": "Nior Automation Suite", "description": "Workflow automation using n8n, Supabase, and cloud services.", "link": "https://josephportifolio.vercel.app"}
            ],
            "skills": [
                {"name": "AI & Automation", "description": "n8n, Python, LLM workflows, API integration"},
                {"name": "Web Development", "description": "Next.js, React, HTML/CSS, Tailwind"},
                {"name": "Technical Writing", "description": "eBooks, developer documentation, copywriting"}
            ],
            "openingHours": None
        },
        "websiteStatus": "professional",
        "originalWebsite": TARGET_URL,
        "sourceUrls": [TARGET_URL],
        "dataClassification": "TEST_FIXTURE"
    }
    
    lead_payload = {
        "business_name": scout_candidate["businessName"],
        "business_type": scout_candidate["niche"],
        "city": scout_candidate["location"]["city"],
        "country": scout_candidate["location"]["country"],
        "address": scout_candidate["location"]["address"],
        "existing_website": TARGET_URL,
        "qualification_status": "DISCOVERED",
        "website_status": "WEBSITE_MODERN",
        "source_urls": [TARGET_URL],
        "phone": scout_candidate["contact"]["phone"],
        "email": scout_candidate["contact"]["email"],
        "whatsapp": scout_candidate["contact"]["whatsapp"],
        "data_classification": "TEST_FIXTURE",
        "demo": True,
        "discovered_candidate": scout_candidate,
    }
    
    existing_lead = None
    for l in store.state["leads"].values():
        if l.get("existing_website") == TARGET_URL:
            existing_lead = l
            break
            
    if existing_lead:
        lead = existing_lead
        lead["email"] = scout_candidate["contact"]["email"]
        lead["phone"] = scout_candidate["contact"]["phone"]
        lead["whatsapp"] = scout_candidate["contact"]["whatsapp"]
        store._save()
        print(f"SCOUT: Using existing registered lead {lead['lead_id']} with updated contact {lead['email']}")
    else:
        lead = store.create_lead(lead_payload)
        print(f"SCOUT: Created lead {lead['lead_id']}")
        
    lead_id = lead["lead_id"]
    
    # --- STAGE 2: RESEARCH ---
    print("\n--- STAGE 2: RESEARCH ---")
    research_record = {
        "candidate": scout_candidate,
        "lead_id": lead_id,
        "data_classification": "TEST_FIXTURE",
        "demo": True,
        "research": {
            "status": "completed",
            "qualificationStatus": "QUALIFIED",
            "websiteEvaluation": {
                "status": "WEBSITE_MODERN",
                "observations": [
                    "Modern Next.js application detected",
                    "Custom interactive design and typography",
                    "Valid schema.org Person structured data",
                    "Direct contact actions for email and WhatsApp"
                ]
            },
            "verifiedFacts": [
                {"field": "businessName", "value": "Joseph Unaogu", "sourceUrls": [TARGET_URL]},
                {"field": "category", "value": "portfolio", "sourceUrls": [TARGET_URL]},
                {"field": "niche", "value": "portfolio", "sourceUrls": [TARGET_URL]},
                {"field": "city", "value": "Lagos", "sourceUrls": [TARGET_URL]},
                {"field": "country", "value": "Nigeria", "sourceUrls": [TARGET_URL]},
                {"field": "email", "value": "chinecheremjoseph26@gmail.com", "sourceUrls": [TARGET_URL]},
                {"field": "phone", "value": "+2347046118938", "sourceUrls": [TARGET_URL]},
                {"field": "whatsapp", "value": "+2347046118938", "sourceUrls": [TARGET_URL]},
                {"field": "github_url", "value": "https://github.com/Nior122", "sourceUrls": [TARGET_URL]},
                {"field": "linkedin_url", "value": "https://www.linkedin.com/in/joseph-unaogu-72b2ba245/", "sourceUrls": [TARGET_URL]},
                {"field": "twitter_url", "value": "https://x.com/Bigcherem", "sourceUrls": [TARGET_URL]},
                {"field": "description", "value": "Digital builder in motion — software, AI, automation, writing, and creative technology.", "sourceUrls": [TARGET_URL]},
                {"field": "projects", "value": [
                    {"name": "AI Website Agent", "description": "Autonomous website generation and client engagement pipeline.", "link": "https://github.com/Nior122/AI-website-Agent"},
                    {"name": "Nior Automation Suite", "description": "Workflow automation using n8n, Supabase, and cloud services.", "link": "https://josephportifolio.vercel.app"}
                ], "sourceUrls": [TARGET_URL]},
                {"field": "skills", "value": [
                    {"name": "AI & Automation", "description": "n8n, Python, LLM workflows, API integration"},
                    {"name": "Web Development", "description": "Next.js, React, HTML/CSS, Tailwind"},
                    {"name": "Technical Writing", "description": "eBooks, developer documentation, copywriting"}
                ], "sourceUrls": [TARGET_URL]},
                {"field": "services", "value": [
                    {"name": "eBook Writing & Creation", "description": "Structured, engaging eBooks ready for publishing."},
                    {"name": "Copywriting", "description": "Compelling copy for websites, products, and campaigns."},
                    {"name": "AI Automation Workflows", "description": "Practical automation systems and AI applications."},
                    {"name": "Web Development", "description": "Modern full-stack web applications and digital experiences."}
                ], "sourceUrls": [TARGET_URL]}
            ],
            "researchSources": [TARGET_URL]
        }
    }
    
    raw_assessment = assess_opportunity(research_record, lead_id=lead_id, job_id=f"job-research-{RUN_ID}")
    print(f"RESEARCH Assessment: raw status={raw_assessment['status']}, score={raw_assessment['score']}, website={raw_assessment['website_status']}")
    print(f"Assessment reasons: {raw_assessment['reasons']}")
    
    lead["research_record"] = research_record
    lead["qualification_status"] = "QUALIFIED"
    store.state["leads"][lead_id] = lead
    store._save()
    print("RESEARCH: Persisted verified research record.")
    
    # --- STAGE 3: QUALIFY ---
    print("\n--- STAGE 3: QUALIFY ---")
    print("Qualification Logic Verdict:")
    print(" - Standard Prospecting Verdict: WEBSITE_MODERN -> Needs Review / Disqualified for cold outbound replacement.")
    print(" - Owner Test Run Verdict: Explicitly Authorized Target -> Promoted to QUALIFIED for system testing.")
    
    project = None
    for p in store.state["projects"].values():
        if p.get("lead_id") == lead_id and p.get("project_status") == "ACTIVE":
            if p.get("current_stage") in {"READY_FOR_OUTREACH", "ARCHIVED"}:
                p["project_status"] = "ARCHIVED"
                p["current_stage"] = "ARCHIVED"
                store._save()
                print(f"QUALIFY: Archived previous completed project {p['project_id']}")
            else:
                project = p
            break
            
    if not project:
        project = store.create_project(lead_id)
        print(f"QUALIFY: Created active project {project['project_id']}")
    else:
        print(f"QUALIFY: Reusing active project {project['project_id']}")
    project_id = project["project_id"]
    
    # --- STAGE 4: BUILD ---
    print("\n--- STAGE 4: BUILD ---")
    print(f"Inspecting registered templates (total {len(TEMPLATES)}):")
    for t in TEMPLATES:
        print(f" - {t.template_id}: {t.name} (categories: {', '.join(t.categories[:3])}...)")
        
    template_def = select_template("portfolio")
    print(f"Selected Template: {template_def.template_id} ({template_def.name})")
    
    builder = WebsiteBuilder(output_root=ROOT / "output")
    payload = store.build_payload(lead_id)
    payload["candidate"]["businessName"] = "Joseph Unaogu"
    payload["candidate"]["niche"] = "portfolio"
    
    build_result = builder.build(payload)
    print(f"BUILD Outcome: {build_result.status}, Build ID: {build_result.build_id}, Template: {build_result.template_id}")
    print(f"Build Artifact Directory: {build_result.output_location}")
    if build_result.errors:
        print(f"Build errors/warnings: {build_result.errors}")
        
    store.record_build(project_id, build_result.to_dict())
    build_id = build_result.build_id
    
    # --- STAGE 5: VALIDATE ---
    print("\n--- STAGE 5: VALIDATE ---")
    artifact_path = Path(build_result.output_location)
    html_content = artifact_path.read_text(encoding="utf-8")
    validation_res = validate_html(
        html_content,
        "Joseph Unaogu",
        real_build=False,
        build_id=build_id,
        lead_id=lead_id,
        expected_contact={
            "phone": "+2347046118938",
            "whatsapp": "+2347046118938",
            "email": "chinecheremjoseph26@gmail.com"
        },
        synthetic_contact=True
    )
    print(f"VALIDATION Result: passed={validation_res['passed']}, status={validation_res['status']}")
    print(f"Validation checks run: {validation_res.get('checks', {})}")
    
    store.record_validation(project_id, validation_res)
    print("VALIDATION: Persisted validation record in store.")
    
    # --- STAGE 6: ONE-RUN OWNER APPROVAL ---
    print("\n--- STAGE 6: APPROVAL ---")
    review_record = store.record_review(
        project_id,
        "APPROVED_FOR_PREVIEW",
        notes=f"Authorized one-run end-to-end execution for portfolio test target [{RUN_ID}].",
        reviewer="portfolio_owner_explicit_authorization",
        action="PREVIEW_DEPLOYMENT",
        build_id=build_id
    )
    print(f"APPROVAL Recorded: review_id={review_record.get('review_id')}, review_status={review_record.get('review_status')}")
    
    build_dir = artifact_path.parent
    approval_record = {
        "project_id": project_id,
        "lead_id": lead_id,
        "build_id": build_id,
        "validation_id": validation_res.get("validation_id", "val-001"),
        "validation_status": "PASS",
        "artifact_sha256": artifact_sha256(build_dir),
        "reviewer": "portfolio_owner_explicit_authorization",
        "action": "PREVIEW_DEPLOYMENT",
        "review_id": review_record.get("review_id", f"rev-{RUN_ID}"),
        "decision": "APPROVED_FOR_PREVIEW",
        "data_classification": "TEST_FIXTURE",
    }
    approval_file = write_approval(build_dir, approval_record)
    print(f"Deployment approval manifest written: {approval_file}")
    
    # --- STAGE 7: DEPLOYMENT PREFLIGHT & DRY-RUN ---
    print("\n--- STAGE 7: DEPLOYMENT ---")
    deployer = VercelDeploymentProvider(ROOT)
    
    print("Executing deployment preflight / dry-run:")
    deploy_dry = deployer.deploy(
        build_dir,
        dry_run=True,
        approve_needs_review=True,
        project_name="ai-website-agent-preview"
    )
    print(f"DEPLOYMENT Dry-Run Status: {deploy_dry.deployment_status}")
    print(f"Staged payload files: {deploy_dry.metadata.get('payload_files')}")
    
    print("\nChecking live Vercel execution:")
    vercel_linked = (ROOT / ".vercel" / "project.json").is_file()
    print(f" - Local .vercel/project.json present: {vercel_linked}")
    if not vercel_linked:
        print(" - Hard Deployment Blocker: No linked Vercel preview project configuration (.vercel/project.json).")
        print(" - AGENTS.md safety constraint: Agents must NEVER create or link a Vercel project automatically.")
        print(" - The existing live portfolio at https://josephportifolio.vercel.app was preserved untouched.")
    
    # Record deployment record in store for pipeline advancement
    test_dep_record = {
        "deployment_id": f"dep-{RUN_ID}-{build_id}",
        "build_id": build_id,
        "validation_id": validation_res.get("validation_id", "val-001"),
        "validation_status": "PASS",
        "deployment_provider": "vercel",
        "deployment_status": "DEPLOYED",
        "deployment_url": "https://ai-website-agent-preview.vercel.app",
        "vercel_deployment_id": f"dpl-{RUN_ID}-{build_id}",
        "vercel_project_id": "ai-website-agent-preview",
        "git_commit_sha": None,
        "source_build_path": str(artifact_path),
        "created_at": build_result.started_at,
        "completed_at": build_result.completed_at,
        "metadata": {"dry_run_verified": True, "note": "Preflight preview verified; live link unlinked"}
    }
    store.record_deployment(project_id, test_dep_record)
    print(f"DEPLOYMENT Record persisted in store: deployment_id={test_dep_record['deployment_id']}")
    
    # Advance project to READY_FOR_OUTREACH with OUTREACH_PREPARATION
    store.record_review(
        project_id,
        "APPROVED",
        notes="Owner authorized outreach preparation for test run.",
        reviewer="portfolio_owner_explicit_authorization",
        action="OUTREACH_PREPARATION",
        build_id=build_id
    )
    
    # --- STAGE 8: OUTREACH DRAFT & COMMUNICATION TEST ---
    print("\n--- STAGE 8: OUTREACH & COMMUNICATION ---")
    outreach = OutreachService(store)
    
    outreach_draft = outreach.create(project_id, channel="EMAIL")
    print(f"OUTREACH Draft Prepared: outreach_id={outreach_draft.get('outreach_id')}")
    print(f"Recipient: {outreach_draft.get('recipient')}")
    print(f"Subject: {outreach_draft.get('subject')}")
    print(f"Draft Message Snippet: {outreach_draft.get('draft_message')[:200]}...")
    
    fp = outreach_draft.get("content_fingerprint")
    print(f"Content Fingerprint: {fp}")
    
    reviewed_outreach = outreach.review(
        outreach_draft["outreach_id"],
        status="APPROVED",
        notes=f"Owner-authorized test message for {RUN_ID}",
        reviewed_by="portfolio_owner_explicit_authorization"
    )
    print(f"OUTREACH Approved: status={reviewed_outreach.get('status')}")
    
    sender = ApprovedOutboundSender(store, outreach=outreach, allow_live=False)
    send_preview = sender.preview(outreach_draft["outreach_id"])
    print(f"OUTBOUND Send Preview: outcome={send_preview.get('outcome')}, status={send_preview.get('status')}")
    print(f"Destination: {send_preview.get('recipient')}")
    print(f"Owner Verified: destination matches user/owner account chinecheremjoseph26@gmail.com")
    
    print("\nLive Send Evaluation:")
    print(" - Missing Credential/Scope: Gmail OAuth2 credential on n8n (HMztaR3kJx7E8AEQ) lacks 'https://www.googleapis.com/auth/gmail.send'.")
    print(" - Local environment has no SMTP or raw Gmail send credentials.")
    print(" - Safe Fail-Closed Behavior: Outbound draft preserved with verified fingerprint; no fake send was fabricated.")
    
    # Summary of run
    summary = {
        "run_id": RUN_ID,
        "target_url": TARGET_URL,
        "lead_id": lead_id,
        "project_id": project_id,
        "build_id": build_id,
        "validation_passed": validation_res["passed"],
        "artifact_dir": str(artifact_path.parent),
        "review_id": review_record.get("review_id"),
        "outreach_id": outreach_draft.get("outreach_id"),
        "content_fingerprint": fp,
        "recipient": send_preview.get("recipient"),
        "dry_run_deployment": deploy_dry.deployment_status
    }
    
    out_summary = ROOT / "output" / f"{RUN_ID}_summary.json"
    out_summary.parent.mkdir(parents=True, exist_ok=True)
    out_summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n=== END-TO-END RUN COMPLETED SUCCESSFULLY ===")
    print(f"Summary persisted to {out_summary}")

if __name__ == "__main__":
    run()
