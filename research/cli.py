"""Command line for the Research Agent: fixture evidence by default, one prospect's own public pages on request.

The default is unchanged and offline - ``--candidate-file`` plus ``--evidence-file``. ``--source live`` (or
``both``) adds ``PublicWebsiteResearchSource``, which reads only the website URL already recorded for that
candidate and nothing else. It is gated by the deployment, not by this command line: both
``AI_WEBSITE_AGENT_ALLOW_LIVE_RESEARCH=1`` and ``research.allowLiveResearch: true`` in the local settings file
have to be true, and a run that lacks either refuses before building a request.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .live_source import DEFAULT_SETTINGS_PATH, LiveResearchPolicy, PublicWebsiteResearchSource
from .research_agent import FixtureResearchSource, ResearchAgent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research one Scout candidate from explicit sources. Fixture by default; live pages require a permitted deployment.",
    )
    parser.add_argument("--candidate-file", required=True, type=Path, help="JSON object containing one Scout candidate.")
    parser.add_argument("--evidence-file", type=Path, help="Local evidence fixture; required unless a live source is used.")
    parser.add_argument("--source", choices=["fixture", "live", "both"], default="fixture",
                        help="Which sources to run. 'live' fetches the candidate's own recorded website URL only.")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH,
                        help=f"Local settings file carrying the research bounds (default: {DEFAULT_SETTINGS_PATH}).")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Narrow the live page budget for this run (1-3). It can only lower the configured limit.")
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="Required safety flag: research returns evidence; it never qualifies, builds, reviews, deploys, or sends.")
    parser.add_argument("--output", type=Path, help="Optional ignored local JSON result path; defaults to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    candidate = json.loads(args.candidate_file.read_text(encoding="utf-8"))

    sources: list[object] = []
    policy: LiveResearchPolicy | None = None
    live_source: PublicWebsiteResearchSource | None = None
    if args.source in {"fixture", "both"}:
        if not args.evidence_file:
            parser.error("--evidence-file is required with --source fixture (the default) or both.")
        sources.append(FixtureResearchSource(args.evidence_file))
    if args.source in {"live", "both"}:
        policy = LiveResearchPolicy.load(args.settings)
        if args.max_pages is not None:
            if not 1 <= args.max_pages <= 3:
                parser.error("--max-pages must be between 1 and 3.")
            policy = LiveResearchPolicy(**{**policy.__dict__, "max_pages": min(policy.max_pages, args.max_pages)})
        if not policy.enabled:
            # The command line cannot grant consent to itself: without the environment permit and the local
            # settings flag there is no policy, therefore no source, therefore no request.
            parser.error(policy.refusal_reason() or "live research is not permitted for this deployment.")
        live_source = PublicWebsiteResearchSource(policy)
        sources.append(live_source)

    result = ResearchAgent(sources).run(candidate)
    record = result.to_dict()
    content = json.dumps(record, indent=2) + "\n"
    if live_source is not None:
        summary = live_source.summary()
        print(f"live research: {summary['requests_made']} of {summary['max_requests']} permitted requests used; "
              f"permitted targets were the URLs already on the record; attempted: "
              f"{', '.join(sorted({str(entry['status']) for entry in summary['attempts']}) or ['none'])}",
              file=sys.stderr)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    # "partial"/"failed" is reported as such: an operator must be able to tell a quiet record from a broken one.
    return 0 if record["status"] in {"completed", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
