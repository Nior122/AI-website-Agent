"""Test ApprovedOutboundSender with outreach-0005 directly using .env.local."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

env_path = ROOT / ".env.local"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# Ensure allow live is set
os.environ["AI_WEBSITE_AGENT_ALLOW_LIVE_OUTREACH"] = "true"

from tracking.store import TrackingStore
from orchestration.outbound import ApprovedOutboundSender, OutboundRefusal

store = TrackingStore(str(ROOT / "data" / "tracking" / "state.json"))
sender = ApprovedOutboundSender(store, allow_live=True)

outreach_id = "outreach-0005"
record = store.state.get("outreach", {}).get(outreach_id)
if not record:
    print(f"Record {outreach_id} not found!")
    sys.exit(1)

print("Record found:", outreach_id)
print("Channel:", record.get("channel"))
print("Recipient:", record.get("recipient"))
print("Status:", record.get("status"))

if "--reconcile-failed" in sys.argv:
    from tracking.engagement import EngagementLedger
    ledger = EngagementLedger(store, source="operator_cli")
    res = ledger.mark_outreach_sent(
        outreach_id,
        result="FAILED",
        provider="gmail",
        failure_note="Network getaddrinfo glitch during token exchange before send request",
        idempotency_key="reconcile:outreach-0005:failed:network-dns-hiccup"
    )
    print("Reconciled as FAILED:")
    print("send_state:", res.get("send_state"))
    print("status:", res.get("status"))
    record = store.state.get("outreach", {}).get(outreach_id)

annotation = sender.annotate(record)
print("\nAnnotation:")
print("Eligible:", annotation.get("eligible"))
print("Blockers:", annotation.get("blockers"))
print("\nChecks:")
for c in annotation.get("checks", []):
    print(f"  {c['check']:<20} {c['status']:<8} {c['detail']}")

if "--dry-run" in sys.argv:
    print("\nExecuting dry run via sender.send()...")
    res = sender.send(
        outreach_id,
        actor_id="portfolio_owner_explicit_authorization",
        identity_source="operator_cli",
        dry_run=True,
        expected_fingerprint=record.get("content_fingerprint"),
        confirmation="SEND",
        note="Owner-authorized test message dry-run"
    )
    print("Dry run result:")
    print(res)

if "--live" in sys.argv:
    print("\nExecuting LIVE SEND via sender.send()...")
    try:
        res = sender.send(
            outreach_id,
            actor_id="portfolio_owner_explicit_authorization",
            identity_source="operator_cli",
            dry_run=False,
            expected_fingerprint=record.get("content_fingerprint"),
            confirmation="SEND",
            note="Owner-authorized live send to chinecheremjoseph26@gmail.com"
        )
        print("Live send result:")
        print(res)
    except OutboundRefusal as e:
        print(f"OutboundRefusal: {e.code} - {e}")
    except Exception as e:
        print(f"Exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
