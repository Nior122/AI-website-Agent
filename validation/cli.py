"""Validate one local HTML artifact without deploying or contacting anyone."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .quality_validator import validate_rendered_html


def main() -> int:
    parser = argparse.ArgumentParser(description="Run static QA on a local generated HTML artifact.")
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--business-name", required=True)
    parser.add_argument("--real-build", action="store_true")
    parser.add_argument("--build-id")
    parser.add_argument("--lead-id")
    args = parser.parse_args()
    html = args.html.read_text(encoding="utf-8")
    result = validate_rendered_html(html, args.business_name, real_build=args.real_build, build_id=args.build_id, lead_id=args.lead_id)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
