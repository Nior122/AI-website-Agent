"""Render a demo/approved structured data file locally; no deployment occurs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .renderer import render_site


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a website template to a local HTML file.")
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--template-id")
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_site(data, args.template_id), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
