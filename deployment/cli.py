"""Preview-only deployment command. Dry run is the default and is never a deployment.

An executed (``--execute``) publish is refused unless the tracking store says the exact build is
authorized: an explicit human ``APPROVED_FOR_PREVIEW`` decision for action ``PREVIEW_DEPLOYMENT``
bound to the assigned, PASS-validated build. The publish boundary in ``deployer.py`` re-verifies
that approval itself, so this CLI check produces an earlier, clearer refusal - removing it (or
bypassing the CLI entirely) cannot make an unapproved build deployable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .deployer import VercelDeploymentProvider

DEFAULT_STATE_PATH = Path("data/tracking/state.json")


def _authorization(store_path: Path | None, project_id: str | None, build_id: str | None) -> dict:
    """Consult the durable tracking record; a missing store or project is itself a refusal."""
    if not project_id:
        return {
            "authorized": False,
            "code": "PROJECT_ID_REQUIRED",
            "reason": (
                "--execute requires --project-id so the publish can be checked against the recorded human "
                "approval, the assigned build, and any prior deployment of that build."
            ),
        }
    if store_path is None or not Path(store_path).is_file():
        module = Path(__file__).resolve().parent.parent / "tracking" / "store.py"
        raise SystemExit(
            f"--execute requires the tracking state file so the approval can be verified (looked for {store_path}). "
            "Pass --state-path <state.json> or use the default data/tracking/state.json "
            f"(TrackingStore lives in {module})."
        )
    from tracking.store import TrackingStore

    store = TrackingStore(Path(store_path))
    gate = store.authorize_preview_deployment(project_id, build_id=build_id)
    gate.pop("approval", None)
    return gate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or deploy one validated local build to Vercel preview only.")
    parser.add_argument("--build", required=True, type=Path, help="Directory containing index.html and build.json.")
    parser.add_argument("--project-name", default=None, help="Explicitly expected isolated Vercel preview project name.")
    parser.add_argument("--execute", action="store_true", help="Execute Vercel preview deploy; omit for safe dry run.")
    parser.add_argument("--approve-needs-review", action="store_true", help="Explicitly permit a NEEDS_REVIEW validation result.")
    parser.add_argument("--approve-real-preview", action="store_true", help="Explicitly authorize deploying a REAL_PUBLIC_DATA build to its isolated preview project.")
    parser.add_argument("--project-id", default=None, help="Tracking project id; required with --execute to verify the human approval.")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH, help="TrackingStore state file used for the authorization check.")
    parser.add_argument("--build-id", default=None, help="Assert the intended build id; a mismatch with the assigned build is refused.")
    parser.add_argument("--resolve-uncertain-publish", action="store_true", help="Record an operator resolution of an interrupted publish so a retry may proceed.")
    args = parser.parse_args(argv)

    authorization: dict | None = None
    if args.execute:
        try:
            authorization = _authorization(args.state_path, args.project_id, str(args.build_id) if args.build_id else None)
        except (ValueError, OSError) as error:
            print(json.dumps({"authorized": False, "code": "AUTHORIZATION_UNVERIFIABLE", "reason": str(error)}, indent=2))
            return 1
        if not authorization.get("authorized") or authorization.get("skip_publish"):
            payload = dict(authorization)
            payload["deployment_status"] = "SKIPPED_ALREADY_DEPLOYED" if authorization.get("skip_publish") else "NEEDS_REVIEW"
            payload["dry_run"] = False
            payload["provider_cli_invoked"] = False
            print(json.dumps(payload, indent=2))
            return 0 if authorization.get("skip_publish") else 1

    provider = VercelDeploymentProvider(Path.cwd())
    result = provider.deploy(
        args.build,
        dry_run=not args.execute,
        approve_needs_review=args.approve_needs_review,
        approve_real_preview=args.approve_real_preview,
        project_name=args.project_name,
        resolve_uncertain_publish=args.resolve_uncertain_publish,
    )
    payload = result.to_dict()
    if authorization is not None:
        payload["authorization"] = {key: value for key, value in authorization.items() if key != "reason"}
    print(json.dumps(payload, indent=2))
    return 0 if result.deployment_status in {"PENDING", "DEPLOYED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
