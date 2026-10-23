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
import os
from pathlib import Path

from .deployer import VercelDeploymentProvider

DEFAULT_STATE_PATH = Path("data/tracking/state.json")


def _authorization(store_path: Path | None, project_id: str | None, build_id: str | None,
                   *, resolve_uncertain_publish: bool = False) -> dict:
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
    gate = store.authorize_preview_deployment(project_id, build_id=build_id,
                                             resolve_uncertain_publish=resolve_uncertain_publish)
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
    parser.add_argument("--resolve-uncertain-publish", action="store_true",
                        help="Proceed past an unresolved publish attempt. Requires --resolve-note and records "
                             "the decision (who, when, what was checked) in the build's "
                             "deployment-attempts.json before the provider is contacted; it never restates "
                             "the publish outcome and publishes nothing by itself.")
    parser.add_argument("--resolve-note", default=None,
                        help="What you checked and why retrying is safe; stored with the resolution record. "
                             "Required with --resolve-uncertain-publish so the decision is not anonymous.")
    parser.add_argument("--resolved-by", default=None,
                        help="Operator identifier attributed to the resolution (default: "
                             "$AI_WEBSITE_AGENT_OPERATOR_ID). Never treated as verified identity.")
    args = parser.parse_args(argv)
    if args.resolve_note and not args.resolve_uncertain_publish:
        parser.error("--resolve-note only applies with --resolve-uncertain-publish; a note with no recorded "
                     "resolution would be dropped, so nothing was done.")
    if args.resolve_uncertain_publish and not (args.resolve_note or "").strip():
        parser.error("--resolve-uncertain-publish requires --resolve-note: proceeding past an unresolved "
                     "publish is only auditable if the reason is recorded with it, and the attempt ledger "
                     "is where the next operator looks. Nothing was published or recorded.")

    authorization: dict | None = None
    if args.execute:
        try:
            authorization = _authorization(args.state_path, args.project_id, str(args.build_id) if args.build_id else None,
                                          resolve_uncertain_publish=args.resolve_uncertain_publish)
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

    vercel_project_id = None
    if args.execute:
        # The CLI is a separate deployment entry point from the worker. Apply the same per-business
        # provisioning rule here so a local .vercel link cannot bypass project-ID isolation.
        from tracking.store import TrackingStore
        store = TrackingStore(Path(args.state_path))
        tracked = (store.state.get("projects") or {}).get(args.project_id) or {}
        lead = (store.state.get("leads") or {}).get(tracked.get("lead_id")) or {}
        is_real_business = (
            (lead.get("data_classification") or tracked.get("data_classification")) == "REAL_PUBLIC_DATA"
        )
        if is_real_business:
            current_name = str(tracked.get("isolated_preview_project") or "").strip()
            if args.project_name and args.project_name != current_name:
                print(json.dumps({
                    "authorized": False, "code": "REAL_TARGET_OVERRIDE_REFUSED",
                    "reason": "The operator CLI cannot override a real-business project's Vercel name. "
                              "Use the verified binding stored on the tracking project.",
                    "deployment_status": "NEEDS_REVIEW", "provider_cli_invoked": False,
                }, indent=2))
                return 1
            if not args.approve_real_preview:
                print(json.dumps({
                    "authorized": False, "code": "REAL_PREVIEW_APPROVAL_FLAG_REQUIRED",
                    "reason": "Provisioning a per-business Vercel project requires --approve-real-preview "
                              "in addition to the current tracking approval. No Vercel project was created.",
                    "deployment_status": "NEEDS_REVIEW", "provider_cli_invoked": False,
                }, indent=2))
                return 1
            try:
                from deployment.provisioning import VercelProjectProvisioner, VercelProjectProvisioningError
                binding = VercelProjectProvisioner(store).ensure_project(args.project_id)
            except VercelProjectProvisioningError as error:
                print(json.dumps({
                    "authorized": False, "code": error.code, "reason": str(error),
                    "deployment_status": "NEEDS_REVIEW", "provider_cli_invoked": False,
                }, indent=2))
                return 1
            vercel_project_id = binding["id"]
            args.project_name = binding["name"]

    provider = VercelDeploymentProvider(Path.cwd())
    result = provider.deploy(
        args.build,
        dry_run=not args.execute,
        approve_needs_review=args.approve_needs_review,
        approve_real_preview=args.approve_real_preview,
        project_name=args.project_name,
        vercel_project_id=vercel_project_id,
        resolve_uncertain_publish=args.resolve_uncertain_publish,
        resolution_note=args.resolve_note,
        resolved_by=args.resolved_by or os.environ.get("AI_WEBSITE_AGENT_OPERATOR_ID"),
    )
    payload = result.to_dict()
    if authorization is not None:
        payload["authorization"] = {key: value for key, value in authorization.items() if key != "reason"}
    print(json.dumps(payload, indent=2))
    return 0 if result.deployment_status in {"PENDING", "DEPLOYED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
