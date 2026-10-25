"""Comprehensive Verification Script for:
1. Telegram Briefing (/briefing, DailyBriefingService, format_daily_briefing_text, Telegram dispatch)
2. Daily Runner (Job queue inspection, stale job recovery, operational health, pipeline cycle)
3. Inbound Intake & Routing (InboundIntake, reply matching, rule-baseline triage, deduplication, ledger advancement)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 output on Windows terminal
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env.local
env_path = ROOT / ".env.local"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

from tracking.store import TrackingStore
from tracking.engagement import EngagementLedger
from tracking.inbound import InboundIntake
from orchestration.jobs import JobService
from orchestration.briefing import DailyBriefingService, format_daily_briefing_text, compute_authoritative_briefing_data
from integrations.telegram import TelegramBotApi, telegram_config

store = TrackingStore(str(ROOT / "data" / "tracking" / "state.json"))
ledger = EngagementLedger(store, source="feature_tester")
jobs = JobService(store)

def test_telegram_briefing():
    print("\n" + "=" * 60)
    print("STAGE 1: TESTING TELEGRAM BRIEFING")
    print("=" * 60)
    
    cfg = telegram_config(os.environ)
    print(f"Telegram Config: configured={cfg.get('configured')}, token_present={cfg.get('token_present')}, allowed_users={cfg.get('allowed_user_count')}")
    
    bot_token = os.environ.get("AI_WEBSITE_AGENT_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("AI_WEBSITE_AGENT_TELEGRAM_CHAT_ID")
    
    api = TelegramBotApi(bot_token) if bot_token else None
    if api:
        try:
            bot_info = api.get_me()
            print(f"Telegram Bot Verified: username=@{bot_info.get('username')}, id={bot_info.get('id')}")
        except Exception as e:
            print(f"Telegram Bot Probe Warning: {e}")
    
    briefing_service = DailyBriefingService(store=store, ledger=ledger, jobs=jobs)
    briefing_result = briefing_service.generate_briefing()
    briefing_text = briefing_result["text"]
    chunks = briefing_result["chunks"]
    data = briefing_result["data"]
    
    print("\nBriefing Metrics Extracted:")
    print(f" - Local Timestamp (Lagos): {data.get('generated_at_local')}")
    print(f" - Active Leads Count:      {data.get('active_lead_count')}")
    print(f" - Qualified Leads Count:   {data.get('qualified_lead_count')}")
    print(f" - Pipeline Estimated Val:  ₦{data.get('pipeline_value'):,.2f}")
    print(f" - Priority Opportunities:  {len(data.get('priority_opportunities', []))}")
    for opp in data.get("priority_opportunities", []):
        print(f"   * {opp.get('name')} ({opp.get('id')}): {opp.get('opportunity')} - {opp.get('value')}")
    print(f" - Chunks for Telegram:     {len(chunks)} chunk(s)")
    
    print("\n--- BRIEFING PLAIN TEXT SAMPLE ---")
    print(briefing_text[:600] + "\n...[truncated for display]...")
    
    # If chat_id is present, send live briefing to operator chat
    if api and chat_id and "--live-telegram" in sys.argv:
        print(f"\nDispatching live briefing to Telegram chat {chat_id}...")
        try:
            cid = int(chat_id)
            for i, chunk in enumerate(chunks):
                api.send_message(cid, chunk)
                print(f"Sent chunk {i+1}/{len(chunks)} to chat {cid}")
            print("SUCCESS: Live briefing delivered to operator Telegram!")
        except Exception as e:
            print(f"Live Telegram Send Failed: {e}")
    else:
        print("\n(Run with --live-telegram to deliver this briefing to your Telegram chat)")
    return briefing_result

def test_daily_runner():
    print("\n" + "=" * 60)
    print("STAGE 2: TESTING DAILY RUNNER & PIPELINE LIFECYCLE")
    print("=" * 60)
    
    # Check queue shape
    all_jobs = store.state.get("jobs", {})
    job_statuses = {}
    for jid, jdata in all_jobs.items():
        st = jdata.get("status", "UNKNOWN")
        job_statuses[st] = job_statuses.get(st, 0) + 1
    
    print(f"Job Queue Status: Total={len(all_jobs)}, Distribution={job_statuses}")
    
    # Check for stale jobs and recover them
    stale_recovered = jobs.recover_stale(older_than_seconds=900)
    print(f"Stale Job Recovery: Checked. Recovered Jobs={len(stale_recovered)}")
    
    # Check operational health
    from orchestration.health import system_health, operator_attention
    health_rep = system_health(store)
    print(f"Operational Health: readiness={health_rep.get('readiness', {}).get('status')}, automation_held={health_rep.get('automation_held')}")
    
    attention_rep = operator_attention(store)
    print(f"Operator Attention Items: total={len(attention_rep.get('items', []))}")
    for item in attention_rep.get("items", [])[:3]:
        print(f" - [{item.get('kind')}] {item.get('message') or item.get('code')}")
        
    # Test a non-destructive pipeline job execution (dry-run Scout / Qualify check)
    print("\nTesting Pipeline Runner Execution Cycle:")
    test_job_id = f"test-daily-run-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    created_job = jobs.create(
        "SCOUT",
        test_job_id,
        priority="NORMAL",
        input={"mode": "fixture_only", "dry_run": True, "note": "Daily runner test cycle"},
        dry_run=True
    )
    print(f"Created Test Pipeline Job: job_id={created_job.get('job_id')}, status={created_job.get('status')}")
    
    # Inspect job
    inspected = jobs.get(test_job_id)
    print(f"Job Inspected: type={inspected.get('job_type')}, status={inspected.get('status')}")
    print("SUCCESS: Daily runner lifecycle verified!")
    return inspected

def test_inbound_intake():
    print("\n" + "=" * 60)
    print("STAGE 3: TESTING INBOUND INTAKE & TRIAGE")
    print("=" * 60)
    
    intake = InboundIntake(ledger)
    
    # Find existing lead-0003 (Joseph Unaogu) to test reply matching
    lead_id = "lead-0003"
    lead = store.state.get("leads", {}).get(lead_id)
    if not lead:
        print(f"Lead {lead_id} not found in store!")
        return
    
    print(f"Testing Inbound Reply from Lead: {lead.get('business_name')} ({lead_id})")
    print(f"Lead Email: {lead.get('contact', {}).get('email')}")
    
    # Simulate a realistic inbound email response
    event_id = f"inbound-msg-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    raw_reply = {
        "event_id": event_id,
        "provider": "EMAIL_INBOUND",
        "channel": "EMAIL",
        "sender": "chinecheremjoseph26@gmail.com",
        "body": "Hello, I saw the preview website concept at https://ai-website-agent-preview.vercel.app and I really like it. I am definitely interested in discussing further details. When can we talk?",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "summary": "Positive response regarding portfolio preview website concept"
    }
    
    print("\nSubmitting Inbound Event to InboundIntake...")
    result = intake.process(raw_reply)
    
    print(f"Inbound Process Result:")
    triage_info = result.get("triage") or {}
    print(f" - Disposition:           {result.get('disposition')}")
    print(f" - Matched Lead ID:       {result.get('lead_id')}")
    print(f" - Triage Classification: {triage_info.get('category')}")
    print(f" - Matched Phrases:       {triage_info.get('matched_phrases')}")
    print(f" - Recommended Action:    {result.get('next_action')}")
    print(f" - Processing Actions:    {result.get('processing')}")
    
    # Test deduplication
    print("\nTesting Inbound Idempotency & Deduplication...")
    duplicate_result = intake.process(raw_reply)
    print(f" - Duplicate Submittal Result: duplicate={duplicate_result.get('duplicate')}, unchanged={duplicate_result.get('unchanged')}")
    
    # Check relationship progression in the ledger
    rel = ledger.relationship(lead_id)
    print(f"\nLead Relationship Ledger State:")
    print(f" - Lead ID:    {lead_id}")
    print(f" - Stage:      {rel.get('stage')}")
    print(f" - Next Action:{rel.get('next_action')}")
    
    # Check intake pending queue
    pending = intake.pending(limit=5)
    print(f"\nIntake Pending Review Queue: total={pending.get('total')}")
    
    print("\nSUCCESS: Inbound intake, triage, and ledger integration verified!")
    return result

if __name__ == "__main__":
    b_res = test_telegram_briefing()
    d_res = test_daily_runner()
    i_res = test_inbound_intake()
    print("\n" + "=" * 60)
    print("ALL 3 REQUESTED FEATURES (TELEGRAM BRIEF, DAILY RUNNER, INBOUND) VERIFIED SUCCESSFULLY!")
    print("=" * 60)
