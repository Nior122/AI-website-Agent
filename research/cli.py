"""Fixture-only Research Agent command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .research_agent import FixtureResearchSource, ResearchAgent


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixture-only research for one Scout candidate.")
    parser.add_argument("--candidate-file", required=True, type=Path, help="JSON object containing one Scout candidate.")
    parser.add_argument("--evidence-file", required=True, type=Path, help="Fictional local evidence fixture; no live source is implemented.")
    parser.add_argument("--dry-run", action="store_true", required=True, help="Required safety flag for Phase 3.")
    parser.add_argument("--output", type=Path, help="Optional ignored local JSON result path; defaults to stdout.")
    args = parser.parse_args()
    candidate = json.loads(args.candidate_file.read_text(encoding="utf-8"))
    result = ResearchAgent([FixtureResearchSource(args.evidence_file)]).run(candidate).to_dict()
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
