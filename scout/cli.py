"""Command-line entry point for a safe fixture-only Scout Agent run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scout_agent import FixtureSource, Scout, Target


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline-first Scout Agent with an explicit fixture source.")
    parser.add_argument("--source-file", required=True, type=Path, help="JSON fixture; no live network source is implemented.")
    parser.add_argument("--country", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--niche", required=True)
    parser.add_argument("--lead-limit", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", required=True, help="Required safety flag for Phase 2.")
    parser.add_argument("--output", type=Path, help="Optional local JSON output path; defaults to stdout only.")
    args = parser.parse_args()
    result = Scout(Target(args.country, args.city, args.niche, args.lead_limit, args.dry_run), [FixtureSource(args.source_file)]).run().to_dict()
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
