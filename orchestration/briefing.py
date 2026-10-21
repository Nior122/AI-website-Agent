"""Authoritative Daily Business Briefing engine for Nior operations.

Matches the authoritative schema, strict plain-text rules, and structure
of the production n8n Daily Business Briefing workflow (`loO3tTJlt6aFdhvb`).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import re
from typing import Any

# Africa/Lagos timezone is UTC+1 with no daylight saving time
LAGOS_TZ = timezone(timedelta(hours=1))
DEFAULT_MAX_TELEGRAM_CHUNK = 3900


def get_lagos_now(ref_utc: datetime | None = None) -> datetime:
    """Return the current datetime in Africa/Lagos timezone (UTC+1)."""
    now = ref_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(LAGOS_TZ)


def format_lagos_timestamp(dt: datetime) -> str:
    """Format datetime as 'Wednesday, 21 October 2026, 08:00 (Africa/Lagos)'."""
    return dt.strftime("%A, %d %B %Y, %H:%M (Africa/Lagos)")


def compute_authoritative_briefing_data(
    store: Any = None,
    ledger: Any = None,
    jobs: Any = None,
    *,
    ref_time: datetime | None = None,
) -> dict[str, Any]:
    """Extract and compute authoritative metrics for the daily business briefing."""
    now_lagos = get_lagos_now(ref_time)
    today_date = now_lagos.date()

    leads_map: dict[str, Any] = {}
    projects_map: dict[str, Any] = {}
    jobs_map: dict[str, Any] = {}
    if store is not None:
        state = getattr(store, "state", {})
        if isinstance(state, dict):
            leads_map = state.get("leads") or {}
            projects_map = state.get("projects") or {}
            jobs_map = state.get("jobs") or {}

    # 1. Pipeline & Lead metrics
    active_leads: list[dict[str, Any]] = []
    qualified_leads: list[dict[str, Any]] = []
    pipeline_value: float = 0.0

    for lead_id, lead in leads_map.items():
        if not isinstance(lead, dict):
            continue
        status = str(lead.get("status") or "").lower()
        if status in {"won", "lost", "disqualified", "archived"}:
            continue
        active_leads.append(lead)
        val = lead.get("estimated_value")
        if isinstance(val, (int, float)):
            pipeline_value += float(val)

        if status == "qualified" or lead.get("qualification_status") == "QUALIFIED":
            qualified_leads.append(lead)

    # 2. Tasks / Jobs
    overdue_tasks: list[dict[str, Any]] = []
    tasks_due_today: list[dict[str, Any]] = []
    upcoming_tasks: list[dict[str, Any]] = []

    for job_id, job in jobs_map.items():
        if not isinstance(job, dict):
            continue
        job_status = str(job.get("status") or "").upper()
        if job_status in {"COMPLETED", "CANCELLED", "ARCHIVED"}:
            continue
        
        due_at_raw = job.get("due_at") or job.get("scheduled_at")
        title = str(job.get("title") or job.get("workflow_name") or job.get("action") or f"Job {job_id}")
        
        item = {
            "id": job_id,
            "title": title,
            "status": job_status,
            "due_at": due_at_raw,
            "priority": job.get("priority", "NORMAL"),
        }

        if due_at_raw:
            try:
                due_dt = datetime.fromisoformat(str(due_at_raw).replace("Z", "+00:00")).astimezone(LAGOS_TZ)
                due_date = due_dt.date()
                item["due_formatted"] = due_dt.strftime("%d %b %Y, %H:%M")
                if due_dt < now_lagos:
                    overdue_tasks.append(item)
                elif due_date == today_date:
                    tasks_due_today.append(item)
                elif due_date > today_date and (due_date - today_date).days <= 7:
                    upcoming_tasks.append(item)
            except Exception:
                if job_status == "FAILED":
                    overdue_tasks.append(item)
        elif job_status == "FAILED":
            item["due_formatted"] = "Needs immediate resolution"
            overdue_tasks.append(item)

    # 3. Lead Follow-ups
    past_due_follow_ups: list[dict[str, Any]] = []
    follow_ups_due_today: list[dict[str, Any]] = []
    upcoming_follow_ups: list[dict[str, Any]] = []

    if ledger is not None and hasattr(ledger, "outstanding_work"):
        try:
            outstanding = ledger.outstanding_work(limit=100)
            raw_due = outstanding.get("due_follow_ups") or []
            for row in raw_due:
                f_item = {
                    "id": row.get("follow_up_id") or row.get("id"),
                    "lead_id": row.get("lead_id"),
                    "name": row.get("lead_name") or f"Lead {row.get('lead_id')}",
                    "contact": row.get("contact") or "Not recorded",
                    "due_at": row.get("due_at"),
                }
                past_due_follow_ups.append(f_item)
        except Exception:
            pass

    for lead_id, lead in leads_map.items():
        if not isinstance(lead, dict):
            continue
        next_follow_raw = lead.get("next_follow_up_at")
        if not next_follow_raw:
            continue
        try:
            f_dt = datetime.fromisoformat(str(next_follow_raw).replace("Z", "+00:00")).astimezone(LAGOS_TZ)
            f_date = f_dt.date()
            f_item = {
                "id": lead_id,
                "name": lead.get("business_name") or lead.get("name") or lead_id,
                "contact": lead.get("phone") or lead.get("email") or "Not recorded",
                "next_follow_up_at": f_dt.strftime("%d %b %Y, %H:%M"),
            }
            if f_dt < now_lagos and f_item not in past_due_follow_ups:
                past_due_follow_ups.append(f_item)
            elif f_date == today_date:
                follow_ups_due_today.append(f_item)
            elif f_date > today_date and (f_date - today_date).days <= 7:
                upcoming_follow_ups.append(f_item)
        except Exception:
            continue

    # 4. Duplicate Candidate Check
    duplicate_pairs: list[dict[str, Any]] = []
    lead_list = list(leads_map.values())
    for i in range(len(lead_list)):
        for j in range(i + 1, len(lead_list)):
            l1, l2 = lead_list[i], lead_list[j]
            if not isinstance(l1, dict) or not isinstance(l2, dict):
                continue
            id1, id2 = l1.get("lead_id") or l1.get("id"), l2.get("lead_id") or l2.get("id")
            if not id1 or not id2 or id1 == id2:
                continue

            name1 = re.sub(r"[^a-z0-9]", "", str(l1.get("business_name") or l1.get("name") or "").lower())
            name2 = re.sub(r"[^a-z0-9]", "", str(l2.get("business_name") or l2.get("name") or "").lower())
            email1 = str(l1.get("email") or "").strip().lower()
            email2 = str(l2.get("email") or "").strip().lower()

            match = False
            evidence = ""
            if email1 and email2 and email1 == email2:
                match = True
                evidence = f"Identical contact email ({email1})"
            elif name1 and name2 and len(name1) > 4 and name1 == name2:
                match = True
                evidence = f"Identical normalized business name ({l1.get('business_name') or l1.get('name')})"

            if match:
                duplicate_pairs.append({
                    "lead1_id": id1,
                    "lead2_id": id2,
                    "lead1_name": l1.get("business_name") or l1.get("name") or str(id1),
                    "lead2_name": l2.get("business_name") or l2.get("name") or str(id2),
                    "lead1_email": email1 or "Not recorded",
                    "lead2_email": email2 or "Not recorded",
                    "classification": "REVIEW",
                    "confidence": "HIGH" if email1 and email1 == email2 else "MEDIUM",
                    "evidence": evidence,
                    "next_step": "Recommendation: Compare contact details and conversation history before merging.",
                })

    # 5. Priority Opportunities
    priority_opportunities: list[dict[str, Any]] = []
    # Pick qualified or high-value leads first
    sorted_leads = sorted(
        active_leads,
        key=lambda l: (
            1 if l.get("status") == "qualified" else 0,
            float(l.get("estimated_value") or 0.0),
        ),
        reverse=True,
    )
    for lead in sorted_leads[:3]:
        lid = lead.get("lead_id") or lead.get("id") or "unspecified"
        name = lead.get("business_name") or lead.get("name") or lid
        est = lead.get("estimated_value")
        val_str = f"₦{est:,.0f}" if isinstance(est, (int, float)) and est > 0 else "Not specified"
        opp_desc = lead.get("interest") or lead.get("category") or "High-potential prospect"
        next_act = lead.get("next_action") or "Recommendation: Review verified business data and draft proposal"
        priority_opportunities.append({
            "id": lid,
            "name": name,
            "value": val_str,
            "opportunity": opp_desc,
            "next_step": next_act,
        })

    return {
        "generated_at_local": format_lagos_timestamp(now_lagos),
        "active_lead_count": len(active_leads),
        "active_leads": active_leads,
        "qualified_lead_count": len(qualified_leads),
        "qualified_leads": qualified_leads,
        "pipeline_value": pipeline_value,
        "overdue_tasks": overdue_tasks,
        "tasks_due_today": tasks_due_today,
        "upcoming_tasks": upcoming_tasks,
        "past_due_lead_follow_up_count": len(past_due_follow_ups),
        "past_due_lead_follow_ups": past_due_follow_ups,
        "lead_follow_ups_due_today_count": len(follow_ups_due_today),
        "lead_follow_ups_due_today": follow_ups_due_today,
        "upcoming_lead_follow_up_count": len(upcoming_follow_ups),
        "upcoming_lead_follow_ups": upcoming_follow_ups,
        "duplicate_review_count": len(duplicate_pairs),
        "duplicate_pairs": duplicate_pairs,
        "priority_opportunities": priority_opportunities,
    }


def format_daily_briefing_text(data: dict[str, Any]) -> str:
    """Format the authoritative briefing data into mobile Telegram plain text.

    Strictly adheres to production rules:
    - NO Markdown formatting (*, **, _, #, ```)
    - Africa/Lagos timezone
    - Clear emoji section titles
    - Nigerian naira formatting
    - Recommendation: prefix on every strategic suggestion
    """
    lines: list[str] = [
        "📊 DAILY BUSINESS BRIEFING",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📅 DATE",
        data.get("generated_at_local", "Date unrecorded"),
        "",
        "🔥 PRIORITY OPPORTUNITIES",
    ]

    opps = data.get("priority_opportunities") or []
    if opps:
        for opp in opps:
            lines.append(f"• Lead: {opp['name']} (ID: {opp['id']})")
            lines.append(f"  Value: {opp['value']}")
            lines.append(f"  Opportunity: {opp['opportunity']}")
            step = opp['next_step']
            if not step.startswith("Recommendation:"):
                step = f"Recommendation: {step}"
            lines.append(f"  Next step: {step}")
            lines.append("")
    else:
        lines.append("No active priority opportunities recorded.")
        lines.append("")

    pipeline_val = data.get("pipeline_value", 0.0)
    lines.extend([
        "💰 PIPELINE",
        f"• Total pipeline: ₦{pipeline_val:,.0f}",
        f"• Active leads: {data.get('active_lead_count', 0)}",
        f"• Qualified leads: {data.get('qualified_lead_count', 0)}",
        "",
        "🚨 OVERDUE",
    ])

    overdue = data.get("overdue_tasks") or []
    if overdue:
        lines.append(f"Total overdue items: {len(overdue)}")
        for t in overdue[:5]:
            due = t.get("due_formatted") or t.get("due_at") or "Past due"
            lines.append(f"• Task {t['id']} — {t['title']}")
            lines.append(f"  Due: {due}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("📅 DUE TODAY")
    due_today = data.get("tasks_due_today") or []
    if due_today:
        lines.append(f"Total due today: {len(due_today)}")
        for t in due_today[:5]:
            due = t.get("due_formatted") or t.get("due_at") or "Today"
            priority = t.get("priority", "NORMAL")
            lines.append(f"• Task {t['id']} — {t['title']}")
            lines.append(f"  Due: {due}")
            lines.append(f"  Priority: {priority}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("🗓️ UPCOMING")
    upcoming = data.get("upcoming_tasks") or []
    if upcoming:
        lines.append(f"Total upcoming: {len(upcoming)}")
        for t in upcoming[:5]:
            due = t.get("due_formatted") or t.get("due_at") or "Upcoming"
            lines.append(f"• Task {t['id']} — {t['title']}")
            lines.append(f"  Due: {due}")
    else:
        lines.append("None")
    lines.append("")

    lines.append("⚠️ NEEDS ATTENTION")
    attention_items: list[str] = []

    # Past-due follow-ups
    past_due_fu = data.get("past_due_lead_follow_ups") or []
    if past_due_fu:
        for fu in past_due_fu[:3]:
            attention_items.append(
                f"• Past-due recorded follow-up for Lead {fu['id']} ({fu.get('name')})\n"
                f"  Recorded date: {fu.get('next_follow_up_at') or fu.get('due_at')}\n"
                f"  Why it matters: The database follow-up date has passed and requires review."
            )

    # Duplicate review pairs
    dup_pairs = data.get("duplicate_pairs") or []
    if dup_pairs:
        for p in dup_pairs[:2]:
            attention_items.append(
                f"• Potential duplicate leads: Lead {p['lead1_id']} and Lead {p['lead2_id']}\n"
                f"  Evidence: {p['evidence']}\n"
                f"  Recommendation: Compare contact details and conversation history before considering any consolidation."
            )
    elif data.get("duplicate_review_count") == 0:
        attention_items.append("• Automated duplicate detection found no candidate pairs in this run.")

    if not attention_items:
        lines.append("All queues and lead records are current.")
    else:
        lines.extend(attention_items)
    lines.append("")

    lines.append("🎯 RECOMMENDED ACTIONS")
    rec_count = 1
    if overdue:
        lines.append(f"{rec_count}. Recommendation: Review overdue operational tasks to resolve blockers.")
        lines.append("   Reason: Overdue items may delay service delivery or lead responses.")
        lines.append("")
        rec_count += 1

    if past_due_fu:
        lines.append(f"{rec_count}. Recommendation: Review conversation history for past-due follow-up leads.")
        lines.append("   Reason: Checking whether outreach occurred helps keep the prospect relationship accurate.")
        lines.append("")
        rec_count += 1

    if dup_pairs:
        lines.append(f"{rec_count}. Recommendation: Verify potential duplicate identity records.")
        lines.append("   Reason: Preventing duplicate messaging protects brand reputation.")
        lines.append("")
        rec_count += 1

    if data.get("qualified_lead_count", 0) > 0:
        lines.append(f"{rec_count}. Recommendation: Review build artifacts for qualified leads awaiting presentation.")
        lines.append("   Reason: Moving qualified prospects forward improves conversion velocity.")
        lines.append("")
        rec_count += 1

    lines.append(f"{rec_count}. Recommendation: Run social media queue checks to ensure fresh scheduled posts.")
    lines.append("   Reason: Consistent social presence supports organic inbound discovery.")

    return "\n".join(lines).strip()


def chunk_telegram_text(text: str, max_length: int = DEFAULT_MAX_TELEGRAM_CHUNK) -> list[str]:
    """Split briefing plain text into telegram-safe chunks without splitting mid-section."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()

    while len(remaining) > max_length:
        split_at = remaining.rfind("\n\n", 0, max_length)
        if split_at < 1000:
            split_at = remaining.rfind("\n", 0, max_length)
        if split_at < 500:
            split_at = remaining.rfind(" ", 0, max_length)
        if split_at <= 0:
            split_at = max_length

        chunk = remaining[:split_at].strip()
        chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    if len(chunks) > 1:
        return [
            f"Nior — Daily Briefing (Part {i + 1}/{len(chunks)})\n\n{chunk}"
            for i, chunk in enumerate(chunks)
        ]
    return chunks


class DailyBriefingService:
    """Coordinates daily business briefing generation, formatting, and live dispatch."""

    def __init__(self, store: Any = None, ledger: Any = None, jobs: Any = None) -> None:
        self.store = store
        self.ledger = ledger
        self.jobs = jobs

    def generate_briefing(self, ref_time: datetime | None = None) -> dict[str, Any]:
        """Compute authoritative metrics and format into Telegram plain text."""
        data = compute_authoritative_briefing_data(
            store=self.store,
            ledger=self.ledger,
            jobs=self.jobs,
            ref_time=ref_time,
        )
        text = format_daily_briefing_text(data)
        chunks = chunk_telegram_text(text)
        return {
            "text": text,
            "chunks": chunks,
            "data": data,
            "chunk_count": len(chunks),
        }
