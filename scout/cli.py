"""Command-line entry point for offline-first, approved Scout runs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .scout_agent import (
    FixtureSource,
    OverpassRestaurantSource,
    Scout,
    Target,
)
from .tavily_enrichment import TavilySearchEnricher


#: Maximum attempts for the approved Overpass discovery source.
MAX_REQUESTS_CEILING = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run Scout against an explicit source. Fixture mode is offline; "
            "live discovery and enrichment require explicit approval."
        ),
    )

    parser.add_argument(
        "--source",
        choices=["fixture", "overpass"],
        default="fixture",
        help="Discovery source. Fixture is the default; Overpass is opt-in.",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="JSON fixture array; required with --source fixture.",
    )
    parser.add_argument(
        "--approve-live-lookups",
        action="store_true",
        help="Required before Scout can query the live Overpass source.",
    )
    parser.add_argument(
        "--enrich-tavily",
        action="store_true",
        help=(
            "Enable optional Tavily search enrichment. Requires the Overpass "
            "source and both live-discovery and enrichment approvals."
        ),
    )
    parser.add_argument(
        "--approve-enrichment",
        action="store_true",
        help="Explicitly approve sending candidate search queries to Tavily.",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Override the Overpass endpoint if required.",
    )
    parser.add_argument(
        "--fallback-endpoint",
        action="append",
        default=[],
        help="Additional Overpass endpoint to try if the first fails.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1,
        help=f"Overpass endpoint attempts permitted (1-{MAX_REQUESTS_CEILING}).",
    )
    parser.add_argument("--country", default="Nigeria")
    parser.add_argument("--city", default="Lagos")
    parser.add_argument("--niche", default="Restaurants")
    parser.add_argument("--lead-limit", type=int, default=3)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help=(
            "Required safety flag. Scout only returns a review result; it does "
            "not create tracked leads, projects, builds, deployments, or messages."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional local JSON output path; otherwise output goes to stdout.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Approval without enabling enrichment is a configuration error.
    if args.approve_enrichment and not args.enrich_tavily:
        parser.error(
            "--approve-enrichment requires --enrich-tavily. No enrichment request was made."
        )

    # Construct no external enrichment client unless all explicit requirements pass.
    enricher = None

    if args.enrich_tavily:
        if args.source != "overpass":
            parser.error(
                "Tavily enrichment is currently limited to the approved live "
                "Overpass pilot. Fixture data remains offline. No request was made."
            )

        if not args.approve_live_lookups:
            parser.error(
                "--enrich-tavily also requires --approve-live-lookups. "
                "No discovery or enrichment request was made."
            )

        if not args.approve_enrichment:
            parser.error(
                "--enrich-tavily requires --approve-enrichment. "
                "No enrichment request was made."
            )

        api_key = os.environ.get("TAVILY_API_KEY", "").strip()

        if not api_key:
            parser.error(
                "TAVILY_API_KEY is not set. Configure the environment variable "
                "before enabling enrichment. No Tavily request was made."
            )

        enricher = TavilySearchEnricher(
            api_key=api_key,
            timeout_seconds=20,
            max_results=5,
        )

    # Configure the discovery source.
    if args.source == "fixture":
        if not args.source_file:
            parser.error("--source-file is required with --source fixture.")

        sources = [FixtureSource(args.source_file)]

    else:
        if not args.approve_live_lookups:
            parser.error(
                "--source overpass reaches a public API and requires "
                "--approve-live-lookups. No request was made."
            )

        if not 1 <= args.max_requests <= MAX_REQUESTS_CEILING:
            parser.error(
                f"--max-requests must be between 1 and {MAX_REQUESTS_CEILING}."
            )

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

        if enricher is None:
            print(
                "Live discovery: one bounded, read-only Overpass lookup for "
                "the approved Lagos restaurant pilot. Business sites and "
                "contact links are not opened; no one is contacted.",
                file=sys.stderr,
            )
        else:
            print(
                "Live discovery and approved Tavily enrichment are enabled. "
                "Up to three candidate queries will be sent to Tavily using "
                "business names, location, and niche. Search findings are "
                "unverified evidence for review. No outreach, tracked lead "
                "creation, demo generation, or deployment will occur.",
                file=sys.stderr,
            )

    # Enrichment defaults to None and cannot run in fixture mode.
    result = Scout(
        Target(
            args.country,
            args.city,
            args.niche,
            args.lead_limit,
            args.dry_run,
        ),
        sources,
        enricher=enricher,
    ).run().to_dict()

    content = json.dumps(result, indent=2) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")

    return 0 if result["status"] in {"completed", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())