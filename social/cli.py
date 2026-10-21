"""Command-line interface for Social Media Engine operations.

Usage:
    python -m social.cli generate --topic "Speed optimization" --channel twitter
    python -m social.cli list [--channel linkedin] [--status DRAFT]
    python -m social.cli show <draft_id>
    python -m social.cli approve <draft_id> [--note "Looks great"]
    python -m social.cli reject <draft_id> [--reason "Too long"]
    python -m social.cli export <draft_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .brand import validate_brand_voice
from .models import ContentArchetype, VERIFIED_CHANNELS, normalize_channel
from .service import SocialDraftService, SocialServiceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="social.cli",
        description="Nior Social Media Content Engine & Buffer Draft Manager",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # generate
    gen_p = subparsers.add_parser("generate", help="Generate a new social media draft post")
    gen_p.add_argument("--topic", required=True, help="Topic or subject for the post")
    gen_p.add_argument(
        "--channel",
        default="twitter",
        choices=["twitter", "x", "linkedin", "instagram"],
        help="Target social channel (default: twitter)",
    )
    gen_p.add_argument(
        "--archetype",
        default="tech_insight",
        choices=[a.value for a in ContentArchetype],
        help="Content structure archetype (default: tech_insight)",
    )
    gen_p.add_argument("--media-url", default=None, help="Public image/media URL (required for Instagram)")
    gen_p.add_argument("--context", default=None, help="Additional business or technical context")
    gen_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # list
    list_p = subparsers.add_parser("list", help="List social media drafts")
    list_p.add_argument("--channel", default=None, help="Filter by channel")
    list_p.add_argument("--status", default=None, help="Filter by status (DRAFT, APPROVED, REJECTED)")
    list_p.add_argument("--limit", type=int, default=20, help="Maximum number of drafts to display")
    list_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # show
    show_p = subparsers.add_parser("show", help="Show full content and metadata for one draft")
    show_p.add_argument("draft_id", help="Draft ID (e.g., soc-tw-a1b2c3d4)")
    show_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # approve
    app_p = subparsers.add_parser("approve", help="Approve draft for Buffer scheduling")
    app_p.add_argument("draft_id", help="Draft ID to approve")
    app_p.add_argument("--note", default=None, help="Optional approval note")
    app_p.add_argument("--force", action="store_true", help="Bypass brand voice gate")
    app_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # reject
    rej_p = subparsers.add_parser("reject", help="Reject draft")
    rej_p.add_argument("draft_id", help="Draft ID to reject")
    rej_p.add_argument("--reason", default=None, help="Reason for rejection")
    rej_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # export
    exp_p = subparsers.add_parser("export", help="Export action_approvals row payload for n8n Buffer Draft Executor")
    exp_p.add_argument("draft_id", help="Draft ID to export")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = SocialDraftService()

    try:
        if args.command == "generate":
            draft = service.create_draft(
                topic=args.topic,
                channel=args.channel,
                archetype=args.archetype,
                media_url=args.media_url,
                extra_context=args.context,
            )
            if args.json:
                print(json.dumps(draft.to_dict(), indent=2))
            else:
                print(f"Generated draft: {draft.id} [{draft.channel.upper()}]")
                print(f"Brand Voice Score: {draft.brand_voice_score:.2f} | Status: {draft.status}")
                print(f"--- Content ({len(draft.caption)} chars) ---")
                print(draft.caption)
                if draft.media_prompt:
                    print(f"--- Media Prompt ---\n{draft.media_prompt}")

        elif args.command == "list":
            drafts = service.list_drafts(channel=args.channel, status=args.status, limit=args.limit)
            if args.json:
                print(json.dumps([d.to_dict() for d in drafts], indent=2))
            else:
                if not drafts:
                    print("No drafts found matching criteria.")
                else:
                    print(f"{'ID':<18} {'CHANNEL':<10} {'STATUS':<10} {'SCORE':<6} {'TOPIC'}")
                    print("-" * 75)
                    for d in drafts:
                        print(f"{d.id:<18} {d.channel:<10} {d.status:<10} {d.brand_voice_score:<6.2f} {d.topic[:30]}")

        elif args.command == "show":
            draft = service.get_draft(args.draft_id)
            if not draft:
                print(f"Error: Draft '{args.draft_id}' not found.", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(draft.to_dict(), indent=2))
            else:
                print(f"Draft: {draft.id}")
                print(f"Channel: {draft.channel} | Status: {draft.status} | Score: {draft.brand_voice_score}")
                print(f"Topic: {draft.topic} | Archetype: {draft.archetype}")
                if draft.media_url:
                    print(f"Media URL: {draft.media_url}")
                print(f"\n--- Caption ({len(draft.caption)} chars) ---")
                print(draft.caption)

        elif args.command == "approve":
            draft = service.approve_draft(args.draft_id, operator_note=args.note, force=args.force)
            if args.json:
                print(json.dumps(draft.to_dict(), indent=2))
            else:
                print(f"Approved draft {draft.id} for Buffer execution.")
                if draft.operator_note:
                    print(f"Note: {draft.operator_note}")

        elif args.command == "reject":
            draft = service.reject_draft(args.draft_id, reason=args.reason)
            if args.json:
                print(json.dumps(draft.to_dict(), indent=2))
            else:
                print(f"Rejected draft {draft.id}.")
                if draft.operator_note:
                    print(f"Reason: {draft.operator_note}")

        elif args.command == "export":
            payload = service.export_buffer_payload(args.draft_id)
            print(json.dumps(payload, indent=2))

        return 0

    except SocialServiceError as exc:
        print(f"Error ({exc.code}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
