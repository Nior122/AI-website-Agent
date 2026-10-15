"""Command-line entry point for a bounded Scout run: fixture by default, live lookup only on request.

The default stays offline - ``--source-file`` with a JSON fixture - because discovery is the step that
touches other people's businesses, and a test or an automated call must never reach a public API by
accident. ``--source overpass`` is the one approved live pilot (``scout.scout_agent.OverpassRestaurantSource``:
Lagos restaurants only, documented ``key="value"`` tag filters, one bounded request per endpoint attempt,
at most five candidates, provenance kept, nothing visited, nothing contacted). It additionally requires
``--approve-live-lookups``, so a run that sends a request is one an operator chose.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .scout_agent import (
    FixtureSource,
    OverpassRestaurantSource,
    Scout,
    Target,
)

#: A live run is a review instrument, not a crawler, so the request budget stays small even when asked.
MAX_REQUESTS_CEILING = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Scout Agent against an explicit source. Fixture by default; live lookup requires approval.",
    )
    parser.add_argument("--source", choices=["fixture", "overpass"], default="fixture",
                        help="Discovery source. 'overpass' is the only live source and is opt-in.")
    parser.add_argument("--source-file", type=Path,
                        help="JSON fixture array; required for --source fixture (the default).")
    parser.add_argument("--approve-live-lookups", action="store_true",
                        help="Required with --source overpass. Without it no request is made and the run refuses.")
    parser.add_argument("--endpoint", default=None,
                        help="Override the Overpass endpoint (rarely needed; the default is the public instance).")
    parser.add_argument("--fallback-endpoint", action="append", default=[],
                        help="Additional Overpass endpoint to try if the first fails. Repeatable.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-requests", type=int, default=1,
                        help=f"Endpoint attempts permitted for this run (1-{MAX_REQUESTS_CEILING}).")
    parser.add_argument("--country", default="Nigeria")
    parser.add_argument("--city", default="Lagos")
    parser.add_argument("--niche", default="Restaurants")
    parser.add_argument("--lead-limit", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="Required safety flag: a Scout run records candidates, it never creates leads, "
                             "projects, builds, deployments, or messages.")
    parser.add_argument("--output", type=Path, help="Optional local JSON output path; defaults to stdout only.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.source == "fixture":
        if not args.source_file:
            parser.error("--source-file is required with --source fixture.")
        sources = [FixtureSource(args.source_file)]
    else:
        if not args.approve_live_lookups:
            # Refuse before constructing the source: the object is what owns the request budget, and an
            # unapproved run must not even be able to describe itself as one that could have gone out.
            parser.error(
                "--source overpass reaches a public API and requires --approve-live-lookups. "
                "No request was made."
            )
        if not 1 <= args.max_requests <= MAX_REQUESTS_CEILING:
            parser.error(f"--max-requests must be between 1 and {MAX_REQUESTS_CEILING}.")
        if not 1 <= args.timeout_seconds <= 120:
            parser.error("--timeout-seconds must be between 1 and 120.")
        source = OverpassRestaurantSource(
            timeout_seconds=args.timeout_seconds,
            max_requests=args.max_requests,
        )
        if args.endpoint:
            source.endpoint = args.endpoint
        if args.fallback_endpoint:
            source.fallback_endpoints = list(args.fallback_endpoint)
        sources = [source]
        print(
            "live discovery: one bounded read-only Overpass request for the approved Lagos restaurant pilot; "
            "no business site, social profile, or contact link is visited and no one is contacted.",
            file=sys.stderr,
        )

    result = Scout(Target(args.country, args.city, args.niche, args.lead_limit, args.dry_run), sources).run().to_dict()
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    # A run that produced nothing, or whose only source failed, is not a success: discovery is the first
    # link of the chain, and an operator has to be able to tell "no opportunities right now" from "broken".
    return 0 if result["status"] in {"completed", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
