"""Preview-only deployment command. Dry run is the default."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .deployer import VercelDeploymentProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or deploy one validated local build to Vercel preview only.")
    parser.add_argument("--build", required=True, type=Path, help="Directory containing index.html and build.json.")
    parser.add_argument("--project-name", default=None, help="Explicitly expected isolated Vercel preview project name.")
    parser.add_argument("--execute", action="store_true", help="Execute Vercel preview deploy; omit for safe dry run.")
    parser.add_argument("--approve-needs-review", action="store_true", help="Explicitly permit a NEEDS_REVIEW validation result.")
    parser.add_argument("--approve-real-preview", action="store_true", help="Explicitly authorize deploying a REAL_PUBLIC_DATA build to its isolated preview project.")
    args = parser.parse_args(argv)
    provider = VercelDeploymentProvider(Path.cwd())
    result = provider.deploy(
        args.build,
        dry_run=not args.execute,
        approve_needs_review=args.approve_needs_review,
        approve_real_preview=args.approve_real_preview,
        project_name=args.project_name,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.deployment_status in {"PENDING", "DEPLOYED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
