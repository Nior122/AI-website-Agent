"""Render a demo/approved structured data file locally; no deployment occurs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .multipage import render_site_pages
from .renderer import render_site


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a website template to a local HTML file.")
    parser.add_argument("--data", required=True, type=Path)
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--output", type=Path, help="Write the legacy single-document HTML preview.")
    output_group.add_argument("--site-output", type=Path, help="Write a complete five-page website bundle to this directory.")
    parser.add_argument("--template-id")
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    if args.site_output:
        args.site_output.mkdir(parents=True, exist_ok=True)
        for filename, html in render_site_pages(data, args.template_id).items():
            (args.site_output / filename).write_text(html, encoding="utf-8")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(render_site(data, args.template_id), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
