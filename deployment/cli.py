"""Preview-only deployment command. Dry run is the default."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .deployer import VercelDeploymentProvider


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or deploy one validated local build to Vercel preview only.")
    parser.add_argument("--build", required=True, type=Path, help="Directory containing index.html and build.json.")
    parser.add_argument("--execute", action="store_true", help="Execute Vercel preview deploy; omit for safe dry run.")
    parser.add_argument("--approve-needs-review", action="store_true", help="Explicitly permit a NEEDS_REVIEW validation result.")
    args = parser.parse_args()
    provider = VercelDeploymentProvider(Path.cwd())
    result = provider.deploy(args.build, dry_run=not args.execute, approve_needs_review=args.approve_needs_review)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.deployment_status in {"PENDING", "DEPLOYED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
