"""Safe, preview-only Vercel deployment adapter for validated local artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Callable
import json
import os
import re
import shutil
import subprocess
from urllib.parse import unquote, urlparse, urlsplit

from .authorization import (
    APPROVAL_FILENAME,
    DEPLOYMENT_ACTION,
    UNRESOLVED_ATTEMPT_STATES,
    append_attempt,
    append_resolution_attempt,
    artifact_sha256,
    classify_prior_attempt,
    read_attempts,
    verify_approval,
)

DEPLOYMENT_STATES = {"PENDING", "PREPARING", "DEPLOYING", "DEPLOYED", "FAILED", "NEEDS_REVIEW", "PUBLISH_UNCERTAIN"}
DEFAULT_DEPLOY_TIMEOUT_SECONDS = 300
EXPECTED_PREVIEW_PROJECT = "ai-website-agent-preview"
_FORBIDDEN_PAYLOAD_NAMES = {"build.json", "deployment.json", "deployment-approval.json", "deployment-attempts.json", "vercel.json", ".env"}
_SECRET_PATTERNS = (r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}", r"sk-[A-Za-z0-9]{12,}")
_TEXT_PAYLOAD_SUFFIXES = {".css", ".html", ".js", ".json", ".svg", ".txt"}
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


class ProviderConfigurationError(ValueError):
    """The workspace cannot reach the provider at all: no link, a broken link record, no CLI.

    Kept separate from an authorization refusal on purpose. A missing link is not something a human
    approval can resolve, so it is recorded with its own code and remedy instead of collapsing into the
    generic worker failure, and it stays a ``ValueError`` for callers that only catch that.
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class DeploymentResult:
    deployment_id: str; lead_id: str; business_name: str | None; build_id: str; validation_id: str | None
    validation_status: str; deployment_provider: str; deployment_status: str; deployment_url: str | None
    vercel_deployment_id: str | None; vercel_project_id: str | None; git_commit_sha: str | None
    source_build_path: str; created_at: str; completed_at: str | None = None; error: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StagedPayload:
    """A deliberately small, public-only copy of a validated website artifact."""
    directory: Path
    files: tuple[str, ...]


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.references: list[str] = []; self._in_style = False; self._style_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() in {"img", "script", "source", "video", "audio", "track", "embed", "object"}:
            self.references.append(values.get("src", ""))
        if tag.casefold() == "link" and values.get("href"):
            self.references.append(values["href"])
        if values.get("srcset"):
            self.references.extend(item.strip().split()[0] for item in values["srcset"].split(",") if item.strip())
        if values.get("style"):
            self.references.extend(_css_references(values["style"]))
        if tag.casefold() == "style": self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style": self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style: self._style_parts.append(data)

    def close(self) -> None:
        super().close(); self.references.extend(_css_references("".join(self._style_parts)))


class VercelDeploymentProvider:
    """Deploys only a staged public payload to the dedicated Vercel preview project."""
    name = "vercel"

    def __init__(self, repository_root: Path, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, *, expected_project_name: str = EXPECTED_PREVIEW_PROJECT, deploy_timeout_seconds: int = DEFAULT_DEPLOY_TIMEOUT_SECONDS) -> None:
        self.repository_root, self.runner, self.expected_project_name = repository_root, runner, expected_project_name
        self.deploy_timeout_seconds = max(1, int(deploy_timeout_seconds))

    def deploy(
        self,
        build_path: Path,
        *,
        dry_run: bool = True,
        approve_needs_review: bool = False,
        approve_real_preview: bool = False,
        project_name: str | None = None,
        vercel_project_id: str | None = None,
        resolve_uncertain_publish: bool = False,
        resolution_note: str | None = None,
        resolved_by: str | None = None,
    ) -> DeploymentResult:
        created_at, build_path = _now(), build_path.resolve()
        manifest = _load_build_manifest(build_path); build_id = str(manifest.get("build_id") or "unknown-build")
        suffix = build_id.removeprefix("build-") if manifest.get("build_id") else created_at.replace(":", "").replace("Z", "z")
        result = DeploymentResult(f"deployment-{suffix}", str(manifest.get("lead_id") or "unknown-lead"), _business_name(build_path), build_id,
            (manifest.get("validation") or {}).get("validation_id"), (manifest.get("validation") or {}).get("status", "MISSING"), self.name,
            "PREPARING", None, None, _vercel_project_id(self.repository_root), _git_sha(self.repository_root), str(build_path), created_at)
        approval: dict | None = None
        try:
            _assert_safe_build(build_path, manifest, approve_needs_review)
            data_class = manifest.get("data_classification") or (manifest.get("metadata") or {}).get("data_classification")
            is_real = data_class == "REAL_PUBLIC_DATA"
            configured_isolated = manifest.get("isolated_preview_project") or (manifest.get("metadata") or {}).get("isolated_preview_project")
            if project_name is not None:
                target_project_name = project_name.strip()
            elif configured_isolated:
                target_project_name = str(configured_isolated).strip()
            elif is_real and self.expected_project_name == EXPECTED_PREVIEW_PROJECT:
                target_project_name = ""
            else:
                target_project_name = self.expected_project_name
            if is_real and not approve_real_preview:
                raise NeedsReview("Real-business preview deployment is disabled by default and requires explicit operator authorization (--approve-real-preview).")
            if is_real and (not target_project_name or target_project_name == EXPECTED_PREVIEW_PROJECT):
                raise ValueError(
                    f"Refusing deployment: real-business drafts cannot use or fall back to the shared fictional preview project '{EXPECTED_PREVIEW_PROJECT}'; specify an isolated preview project via --project-name."
                )
            _assert_valid_preview_target(target_project_name, configured_isolated=configured_isolated)
            if not dry_run:
                # The operation that publishes authorizes itself: an explicit human approval bound to
                # this exact build, validation, and artifact must exist, and no prior attempt may be
                # unresolved. A refusal below stages no payload and never calls the provider CLI, so it
                # holds for the operator CLI, a worker, a retried job, and an n8n-triggered call alike.
                approved, code, reason, approval = verify_approval(build_path, manifest=manifest)
                if not approved:
                    return self._refuse(build_path, result, code, reason, approval=approval,
                        status="NEEDS_REVIEW" if code.startswith("APPROVAL_") else "FAILED")
                if is_real and not vercel_project_id:
                    raise NeedsReview(
                        "A real-business publish requires a verified Vercel project ID persisted on its tracking project. "
                        "Use the approved per-business provisioning path; a project name or local .vercel link is not sufficient."
                    )
                project = self._verified_project_target(target_project_name, vercel_project_id)
                proceed, code, reason, prior = classify_prior_attempt(
                    build_path,
                    artifact_sha=artifact_sha256(build_path),
                    project_id=project["projectId"],
                    resolve_uncertain_publish=resolve_uncertain_publish,
                )
                if not proceed:
                    # An already-published artifact is a redundant request (FAILED); an unresolved
                    # attempt needs an operator to inspect the provider first (NEEDS_REVIEW).
                    # An unreadable ledger and an unresolved publish both need a human before anything is
                    # sent; a recorded duplicate publish is an ordinary failure of a redundant request.
                    return self._refuse(build_path, result, code, reason, prior=prior,
                        status="NEEDS_REVIEW" if code in {"PUBLISH_STATE_UNCERTAIN", "PUBLISH_LEDGER_UNREADABLE"}
                        else "FAILED")
                if resolve_uncertain_publish and prior is not None \
                        and str(prior.get("deployment_status") or "") in UNRESOLVED_ATTEMPT_STATES:
                    # `--resolve-uncertain-publish` promises an auditable retry, so the promise is kept
                    # before the provider is contacted: who decided, what they were deciding about, and the
                    # reason they gave go into the same append-only ledger. The annotation never restates
                    # the publish outcome - `published` stays false and the uncertainty remains visible to
                    # every read path until the provider state is actually confirmed.
                    resolution = append_resolution_attempt(
                        build_path, resolves=prior, resolved_by=resolved_by, note=resolution_note,
                    )
                    result.metadata["publish_resolution"] = resolution
            else:
                project = self._verified_project_target(target_project_name, vercel_project_id)
            result.vercel_project_id = project["projectId"]
            result.metadata.update({"target": "preview", "dry_run": dry_run, "project_name": project["projectName"]})
            if vercel_project_id:
                result.metadata["vercel_team_id"] = str(project.get("orgId") or "")
            with TemporaryDirectory(prefix="ai-website-agent-preview-") as temporary:
                payload = stage_public_payload(build_path, Path(temporary))
                result.metadata["payload_files"] = list(payload.files)
                if dry_run:
                    # A dry run is a verification, never a deployment: no ledger entry, no provider call.
                    result.deployment_status, result.completed_at = "PENDING", _now(); _write_manifest(build_path, result); return result
                if self.runner is subprocess.run:
                    # Only the default real executor needs a PATH check; an injected runner (a test
                    # double or an operator-supplied executor) is the caller's responsibility.
                    _assert_vercel_cli_available()
                result.deployment_status = "DEPLOYING"
                # The in-flight attempt is recorded before the provider is called: if this process is
                # interrupted, the ledger keeps an unresolved state so a later run cannot publish on
                # the assumption that nothing happened.
                _append_attempt(build_path, result, "DEPLOYING", approval_review_id=(approval or {}).get("review_id"))
                cmd = [_vercel_command(), "deploy", str(payload.directory), "--yes", "--project", project["projectId"]]
                run_kwargs = {
                    "cwd": str(self.repository_root), "capture_output": True, "text": True,
                    "check": False, "timeout": self.deploy_timeout_seconds,
                }
                if vercel_project_id:
                    # API-created projects are unlinked intentionally. Vercel CLI supports headless
                    # targeting through ORG_ID/PROJECT_ID, avoiding a workspace link or interactive prompt.
                    # Credentials are passed only in the child environment, never as command-line arguments.
                    client = self._vercel_client()
                    run_env = os.environ.copy()
                    run_env["VERCEL_TOKEN"] = client.token
                    run_env["VERCEL_ORG_ID"] = client.team_id
                    run_env["VERCEL_PROJECT_ID"] = project["projectId"]
                    run_kwargs["env"] = run_env
                try:
                    completed = self.runner(cmd, **run_kwargs)
                except subprocess.TimeoutExpired as error:
                    result.deployment_status = "PUBLISH_UNCERTAIN"
                    result.error = (
                        f"The {self.name} CLI did not return within {self.deploy_timeout_seconds}s, so the publish may have been "
                        f"accepted by the provider ({_safe_error(str(error)) or 'no output'}). Verify the preview project before retrying."
                    )
                    result.completed_at = _now(); _write_manifest(build_path, result)
                    _append_attempt(build_path, result, "PUBLISH_UNCERTAIN")
                    return result
                if completed is None:
                    # The call was made and produced nothing verifiable, so the provider's state is
                    # unknown; that must never be recorded as an ordinary failure.
                    result.deployment_status = "PUBLISH_UNCERTAIN"
                    result.error = (
                        f"The {self.name} CLI invocation produced no verifiable result; the publish may have been accepted "
                        "by the provider. Verify the preview project before retrying."
                    )
                    result.completed_at = _now(); _write_manifest(build_path, result)
                    _append_attempt(build_path, result, "PUBLISH_UNCERTAIN")
                    return result
                output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
                if completed.returncode != 0: raise RuntimeError(_safe_error(output) or f"Vercel CLI exited with code {completed.returncode}.")
                url = _preview_url(output)
                if not url: raise RuntimeError("Vercel CLI completed but did not return an HTTPS preview URL.")
                result.deployment_url, result.vercel_deployment_id, result.deployment_status = url, _deployment_id_from_url(url), "DEPLOYED"
        except NeedsReview as error:
            result.deployment_status, result.error = "NEEDS_REVIEW", str(error)
        except ProviderConfigurationError as error:
            # The workspace, not the approval, is what is missing here. The code travels in the manifest so
            # a job records "link the preview project" rather than an undifferentiated worker failure.
            result.deployment_status, result.error = "FAILED", str(error)
            result.metadata["denial_code"] = error.error_code
        except Exception as error:
            result.deployment_status, result.error = "FAILED", str(error)
        result.completed_at = _now(); _write_manifest(build_path, result)
        if not dry_run:
            _append_attempt(build_path, result, result.deployment_status, validation_id=result.validation_id, deployment_url=result.deployment_url)
        return result

    @staticmethod
    def _vercel_client():
        from deployment.site_management import VercelProjectsClient
        return VercelProjectsClient()

    def _verified_project_target(self, target_project_name: str, vercel_project_id: str | None) -> dict:
        """Verify the exact API-managed project or retain the legacy explicit linked-project gate."""
        if not vercel_project_id:
            return _assert_project_linked(self.repository_root, target_project_name)
        from deployment.site_management import PROTECTED_PROJECT_NAMES, _is_protected

        client = self._vercel_client()
        try:
            live = client.get_project(vercel_project_id)
        except Exception as error:
            raise ProviderConfigurationError(
                f"Could not verify the dedicated Vercel project ID; deployment refused: {error}",
                error_code="VERCEL_PROJECT_BINDING_UNVERIFIABLE",
            ) from None
        if live["id"] != vercel_project_id or live["name"] != target_project_name:
            raise ProviderConfigurationError(
                "The API-managed Vercel project ID/name did not match the approved target; deployment refused.",
                error_code="VERCEL_PROJECT_BINDING_MISMATCH",
            )
        if _is_protected(live["id"], live["name"]) or live["name"].casefold() in PROTECTED_PROJECT_NAMES:
            raise ProviderConfigurationError(
                "The API-managed Vercel project is protected and cannot receive a business deployment.",
                error_code="VERCEL_PROJECT_PROTECTED",
            )
        _assert_valid_preview_target(live["name"])
        return {"projectId": live["id"], "projectName": live["name"], "orgId": client.team_id}

    @staticmethod
    def _refuse(build_path: Path, result: DeploymentResult, code: str, reason: str, *, status: str, approval: dict | None = None, prior: dict | None = None) -> DeploymentResult:
        """Record a gate refusal in the manifest and the attempt ledger, without publishing anything."""
        result.deployment_status, result.error, result.completed_at = status, f"{code}: {reason}", _now()
        result.metadata.update({
            "denial_code": code,
            "required_action": DEPLOYMENT_ACTION,
            "dry_run": False,
            "target": "preview",
            "approval_review_id": (approval or {}).get("review_id"),
            "prior_attempt": prior,
        })
        _write_manifest(build_path, result)
        try:
            _append_attempt(build_path, result, status, denial_code=code, published=False)
        except ValueError as error:
            # A corrupt or unreadable ledger is reported on the refusal instead of being replaced, and the
            # refusal still stands: nothing was published and the operator now sees both facts.
            result.metadata["ledger_write_error"] = str(error)
            _write_manifest(build_path, result)
        return result

class NeedsReview(Exception): pass


def stage_public_payload(build_path: Path, staging_directory: Path) -> StagedPayload:
    """Copy only index.html and directly referenced safe local assets into a clean directory."""
    build_path, staging_directory = build_path.resolve(), staging_directory.resolve()
    if staging_directory.exists() and any(staging_directory.iterdir()): raise ValueError("Deployment staging directory must be empty.")
    staging_directory.mkdir(parents=True, exist_ok=True)
    index_path = build_path / "index.html"; html = index_path.read_text(encoding="utf-8")
    parser = _AssetParser(); parser.feed(html); parser.close()
    files: dict[Path, Path] = {Path("index.html"): index_path}; pending = [(reference, build_path) for reference in parser.references]
    while pending:
        reference, origin = pending.pop()
        local = _safe_local_asset(build_path, reference, origin)
        if local is None: continue
        relative, source = local
        if relative in files: continue
        files[relative] = source
        if source.suffix.casefold() == ".css": pending.extend((nested, source.parent) for nested in _css_references(source.read_text(encoding="utf-8")))
    copied: list[str] = []
    for relative, source in sorted(files.items(), key=lambda item: item[0].as_posix()):
        destination = staging_directory / relative; destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, destination)
        _assert_no_payload_secret(destination); copied.append(relative.as_posix())
    _assert_payload_only_contains(staging_directory, set(copied))
    return StagedPayload(staging_directory, tuple(copied))


def _safe_local_asset(build_path: Path, reference: str, origin: Path | None = None) -> tuple[Path, Path] | None:
    reference = reference.strip()
    if not reference or reference.startswith("#") or reference.startswith("data:"): return None
    parsed = urlsplit(reference)
    if parsed.scheme.casefold() in {"http", "https", "mailto", "tel"} or parsed.netloc: return None
    if parsed.scheme: raise ValueError(f"Unsupported asset URL scheme in deployment payload: {parsed.scheme}.")
    value = unquote(parsed.path)
    if not value: return None
    if Path(value).is_absolute() or PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValueError(f"Absolute asset path is not allowed in deployment payload: {reference}.")
    relative = Path(value)
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts): raise ValueError(f"Unsafe asset path is not allowed in deployment payload: {reference}.")
    if relative.name.casefold() in _FORBIDDEN_PAYLOAD_NAMES: raise ValueError(f"Internal metadata is not allowed in deployment payload: {reference}.")
    source = ((origin or build_path) / relative).resolve()
    try: source.relative_to(build_path)
    except ValueError as error: raise ValueError(f"Asset path escapes the build directory: {reference}.") from error
    if not source.is_file(): raise ValueError(f"Required local asset is missing: {reference}.")
    return source.relative_to(build_path), source


def _css_references(css: str) -> list[str]: return [match.group(2).strip() for match in _CSS_URL.finditer(css) if match.group(2).strip()]

def _assert_no_payload_secret(path: Path) -> None:
    if path.suffix.casefold() not in _TEXT_PAYLOAD_SUFFIXES: return
    text = path.read_text(encoding="utf-8", errors="replace")
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _SECRET_PATTERNS): raise ValueError(f"Potential secret-like value detected in staged payload file: {path.name}.")

def _assert_payload_only_contains(staging_directory: Path, expected: set[str]) -> None:
    actual = {path.relative_to(staging_directory).as_posix() for path in staging_directory.rglob("*") if path.is_file()}
    if actual != expected or any(any(segment.startswith(".") for segment in Path(part).parts) or Path(part).name.casefold() in _FORBIDDEN_PAYLOAD_NAMES for part in actual): raise ValueError("Deployment staging integrity check failed.")

def _load_build_manifest(build_path: Path) -> dict:
    if not build_path.is_dir() or not (build_path / "index.html").is_file(): raise ValueError("Build directory or required index.html is missing.")
    manifest_path = build_path / "build.json"
    if not manifest_path.is_file(): raise ValueError("Build manifest build.json is missing.")
    return json.loads(manifest_path.read_text(encoding="utf-8"))

def _assert_safe_build(build_path: Path, manifest: dict, approve_needs_review: bool) -> None:
    if Path(manifest.get("output_location") or "").resolve() != (build_path / "index.html").resolve(): raise ValueError("Build manifest output location does not match the requested deployment directory.")
    if (manifest.get("metadata") or {}).get("target") in {"prod", "production"}:
        raise ValueError("Refusing deployment: production target is forbidden; only preview deployments are supported.")
    review_status = manifest.get("review_status") or (manifest.get("metadata") or {}).get("review_status")
    if review_status in {"REJECTED", "CHANGES_REQUESTED"}:
        raise ValueError(f"Deployment is blocked because build review_status is {review_status}.")
    build_status = manifest.get("status")
    if build_status is not None and build_status != "COMPLETED": raise ValueError(f"Deployment is blocked because build status is {build_status}, not COMPLETED.")
    validation = manifest.get("validation") or {}
    if validation.get("build_id") and manifest.get("build_id") and validation.get("build_id") != manifest.get("build_id"):
        raise ValueError("Build manifest validation build_id does not match build_id.")
    if validation.get("lead_id") and manifest.get("lead_id") and validation.get("lead_id") != manifest.get("lead_id"):
        raise ValueError("Build manifest validation lead_id does not match lead_id.")
    status = validation.get("status")
    if status == "PASS": return
    if status == "NEEDS_REVIEW" and approve_needs_review: return
    if status == "NEEDS_REVIEW": raise NeedsReview("Validation requires explicit review approval before preview deployment.")
    raise ValueError(f"Deployment is blocked because validation status is {status or 'missing'}, not PASS.")

def _assert_valid_preview_target(target_project_name: str, *, configured_isolated: str | None = None) -> None:
    cleaned = str(target_project_name or "").strip()
    if not cleaned or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", cleaned):
        raise ValueError("Refusing deployment: preview project name must be a valid lowercase slug.")
    if cleaned == "grace-light-community-church":
        raise ValueError(f"Refusing deployment: linked Vercel project is not the dedicated preview project '{cleaned}'.")
    if re.search(r"(?:^|-)prod(?:uction)?(?:$|-)|(?:^|-)live(?:$|-)", cleaned):
        raise ValueError(f"Refusing deployment: '{cleaned}' is a production target; only preview projects are permitted.")
    if "preview" not in cleaned:
        raise ValueError(f"Refusing deployment: target project '{cleaned}' must be a dedicated preview project (name must include 'preview').")
    if configured_isolated and cleaned != str(configured_isolated).strip():
        raise ValueError(
            f"Refusing deployment: requested project '{cleaned}' does not match configured isolated preview project '{configured_isolated}'."
        )

def _assert_project_linked(root: Path, expected_project_name: str) -> dict:
    project = root / ".vercel" / "project.json"
    if not project.is_file(): raise ProviderConfigurationError(
        "No linked Vercel project found (.vercel/project.json). Link the approved dedicated preview project before deploying.",
        error_code="PREVIEW_PROJECT_NOT_LINKED")
    try: record = json.loads(project.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error: raise ProviderConfigurationError(
        "Linked Vercel project configuration is not valid JSON.", error_code="PREVIEW_PROJECT_LINK_INVALID") from error
    if not isinstance(record.get("projectId"), str) or not record["projectId"].strip(): raise ProviderConfigurationError(
        "Linked Vercel project configuration has no projectId.", error_code="PREVIEW_PROJECT_LINK_INVALID")
    if record.get("projectName") != expected_project_name: raise ProviderConfigurationError(
        f"Refusing deployment: linked Vercel project is not the dedicated preview project '{expected_project_name}'.",
        error_code="PREVIEW_PROJECT_LINK_INVALID")
    _assert_valid_preview_target(record["projectName"])
    return record

def _vercel_project_id(root: Path) -> str | None:
    project = root / ".vercel" / "project.json"
    try: return json.loads(project.read_text(encoding="utf-8")).get("projectId") if project.is_file() else None
    except json.JSONDecodeError: return None

def _vercel_command() -> str:
    candidates = ["vercel.cmd", "vercel.exe", "vercel"] if os.name == "nt" else ["vercel"]
    return next((path for candidate in candidates if (path := shutil.which(candidate))), candidates[-1])


def _append_attempt(build_dir: Path, result: DeploymentResult, state: str, **extra) -> list[dict]:
    """Append one entry to the build-local publish ledger; history is never rewritten."""
    record = {
        "attempt": len(read_attempts(build_dir)) + 1,
        "deployment_id": result.deployment_id,
        "deployment_status": state,
        "build_id": result.build_id,
        "lead_id": result.lead_id,
        "validation_id": result.validation_id,
        "deployment_url": result.deployment_url,
        "vercel_project_id": result.vercel_project_id,
        "git_commit_sha": result.git_commit_sha,
        "artifact_sha256": artifact_sha256(build_dir),
        "error": result.error,
        "created_at": result.created_at,
        "completed_at": result.completed_at,
    }
    record.update(extra)
    return append_attempt(build_dir, {key: value for key, value in record.items() if value is not None})


def _assert_vercel_cli_available() -> None:
    """Fail with an actionable configuration error instead of an opaque OS error."""
    command = _vercel_command()
    if shutil.which(command) is None:
        raise ProviderConfigurationError(
            f"Deployment configuration is incomplete: the '{command}' CLI is not available on PATH. "
            "Install/authenticate the approved Vercel CLI before executing a preview deployment.",
            error_code="PROVIDER_CLI_MISSING",
        )


def _git_sha(root: Path) -> str | None:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None

def _business_name(build_path: Path) -> str | None:
    title = re.search(r"<title>([^<]+)</title>", (build_path / "index.html").read_text(encoding="utf-8"), re.IGNORECASE)
    return title.group(1) if title else None

def _preview_url(output: str) -> str | None:
    matches = re.findall(r"https://[^\s'\"]+", output); return next((url.rstrip(".,)") for url in reversed(matches) if urlparse_https(url)), None)

def urlparse_https(url: str) -> bool:
    parsed = urlparse(url); host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and host.endswith(".vercel.app") and host != "vercel.app"

def _deployment_id_from_url(url: str) -> str | None: return url.removeprefix("https://").split(".")[0] or None
def provider_configuration(repository_root: Path) -> dict:
    """Report whether the publish provider is *configured*, without invoking it.

    Health reporting must distinguish "the operator has a CLI, a link and a target" from "the
    provider accepted a publish". Only the first is observable from local files, so that is all this
    function inspects: the ledger and the CLI are consulted by a real deployment run instead.
    """
    root = Path(repository_root)
    command = _vercel_command()
    link: dict = {}
    link_error = None
    link_path = root / ".vercel" / "project.json"
    if link_path.is_file():
        try:
            loaded = json.loads(link_path.read_text(encoding="utf-8"))
            link = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError) as error:
            link_error = f"{type(error).__name__}: Vercel project link could not be read."
    target = str(link.get("project_name") or link.get("name") or "")
    return {
        "provider": "vercel",
        "cli_command": command if shutil.which(command) is not None else None,
        "cli_available": shutil.which(command) is not None,
        "project_linked": bool(link) and link_error is None,
        "link_error": link_error,
        "linked_project_name": target or None,
        "expected_preview_project": EXPECTED_PREVIEW_PROJECT,
        "target_is_isolated": bool(target) and target != EXPECTED_PREVIEW_PROJECT,
        "approval_capability_present": any(root.glob(f"output/*/*/{APPROVAL_FILENAME}")),
        "note": "Configuration only: no provider command was executed and no network call was made.",
    }


def _safe_error(output: str) -> str: return re.sub(r"(?i)(token|secret|password)\s*[:=]\s*\S+", r"\1=[REDACTED]", output).strip()[-1000:]
def _write_manifest(build_path: Path, result: DeploymentResult) -> None: (build_path / "deployment.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
def _now() -> str: return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
