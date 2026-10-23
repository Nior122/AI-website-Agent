"""Durable per-business Vercel project provisioning for approved preview deployments.

Project creation is not a deployment and never assigns a custom domain. A creation intent is persisted
before the provider is called, and ambiguous API outcomes are reconciled rather than blindly retried.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any

from integrations.http import ProviderError
from deployment.site_management import (
    PROTECTED_PROJECT_NAMES,
    SiteManagementError,
    VercelProjectsClient,
    _is_protected,
)
from tracking.store import validate_preview_project_name


class VercelProjectProvisioningError(ValueError):
    """Provisioning failure with a stable, operator-actionable code."""

    def __init__(self, message: str, *, code: str = "VERCEL_PROJECT_PROVISIONING_FAILED") -> None:
        super().__init__(message)
        self.code = code


def project_name_base(business_name: str, tracking_project_id: str, lead_id: str) -> str:
    """Build a stable, unique-looking preview project name under Vercel's 63-character slug bound."""
    words = re.findall(r"[a-z0-9]+", str(business_name or "").casefold())
    slug = "-".join(words).strip("-") or "business-site"
    identity = hashlib.sha256(f"{tracking_project_id}:{lead_id}".encode("utf-8")).hexdigest()[:10]
    # The hash is part of the name on every attempt, not an ephemeral run ID. Rebuilds therefore map
    # back to the same business project while different tracked projects cannot normally collide.
    base = f"{slug[:34].rstrip('-')}-preview-{identity}"
    if base.casefold() in PROTECTED_PROJECT_NAMES or "preview" not in base:
        base = f"business-site-preview-{identity}"
    return validate_preview_project_name(base[:63], real_business=True)


def _candidate_name(base: str, ordinal: int) -> str:
    if ordinal <= 1:
        return validate_preview_project_name(base, real_business=True)
    suffix = f"-{ordinal}"
    return validate_preview_project_name(f"{base[:63-len(suffix)].rstrip('-')}{suffix}", real_business=True)


def _epoch_ms(value: object) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
        # Vercel createdAt fields are unix milliseconds; accept epoch seconds for test adapters.
        return number * 1000 if number < 100_000_000_000 else number
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_name_conflict(error: ProviderError) -> bool:
    if error.status not in {400, 409}:
        return False
    text = str(error).casefold()
    return any(phrase in text for phrase in (
        "already exists", "already taken", "already in use", "name is taken",
        "name already exists", "project with this name",
    ))


class VercelProjectProvisioner:
    """Create or verify one managed Vercel project for one tracked REAL_PUBLIC_DATA business."""

    def __init__(self, store, *, client: VercelProjectsClient | None = None, max_name_attempts: int = 20) -> None:
        self.store = store
        self.client = client
        self.max_name_attempts = max(1, min(int(max_name_attempts), 50))

    def _api(self):
        if self.client is None:
            try:
                self.client = VercelProjectsClient()
            except SiteManagementError as error:
                raise VercelProjectProvisioningError(str(error), code=error.code) from None
        return self.client

    def _refresh(self) -> None:
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()

    def _project(self, tracking_project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        project = (self.store.state.get("projects") or {}).get(tracking_project_id)
        if not isinstance(project, dict):
            raise VercelProjectProvisioningError(
                f"Unknown tracking project {tracking_project_id!r}.", code="TRACKING_PROJECT_NOT_FOUND",
            )
        lead = (self.store.state.get("leads") or {}).get(project.get("lead_id")) or {}
        if (lead.get("data_classification") or project.get("data_classification")) != "REAL_PUBLIC_DATA":
            raise VercelProjectProvisioningError(
                "Automatic Vercel project provisioning is restricted to REAL_PUBLIC_DATA businesses.",
                code="VERCEL_PROJECT_CLASSIFICATION_REFUSED",
            )
        return project, lead

    def _verify(self, project_id: str, expected_name: str) -> dict[str, Any]:
        try:
            live = self._api().get_project(project_id)
        except (ProviderError, SiteManagementError) as error:
            raise VercelProjectProvisioningError(
                f"Cannot verify the saved Vercel project binding; nothing was deployed: {error}",
                code="VERCEL_PROJECT_BINDING_UNVERIFIABLE",
            ) from None
        if live["id"] != project_id or live["name"] != expected_name:
            raise VercelProjectProvisioningError(
                "The saved Vercel project ID/name no longer matches the live project. Nothing was deployed; "
                "reconcile the mapping explicitly.",
                code="VERCEL_PROJECT_BINDING_MISMATCH",
            )
        if _is_protected(live["id"], live["name"]) or live["name"].casefold() in PROTECTED_PROJECT_NAMES:
            raise VercelProjectProvisioningError(
                "This Vercel project is protected and cannot be used as a per-business deployment target.",
                code="VERCEL_PROJECT_PROTECTED",
            )
        try:
            validate_preview_project_name(live["name"], real_business=True)
        except ValueError as error:
            raise VercelProjectProvisioningError(str(error), code="VERCEL_PROJECT_TARGET_INVALID") from None
        return live

    def _historical_binding(self, tracking_project_id: str, project: dict[str, Any]) -> dict[str, Any] | None:
        """Migrate an existing site's binding only from its own successful deployment ledger."""
        records = []
        for deployment in (self.store.state.get("deployments") or {}).values():
            if not isinstance(deployment, dict) or deployment.get("project_id") != tracking_project_id:
                continue
            if deployment.get("deployment_status") != "DEPLOYED":
                continue
            vercel_id = str(deployment.get("vercel_project_id") or "").strip()
            metadata = deployment.get("metadata") if isinstance(deployment.get("metadata"), dict) else {}
            name = str(metadata.get("project_name") or "").strip()
            if not vercel_id or not name:
                continue
            configured_name = str(project.get("isolated_preview_project") or "").strip()
            if configured_name and configured_name != name:
                continue
            try:
                validate_preview_project_name(name, real_business=True)
            except ValueError:
                continue
            records.append((str(deployment.get("completed_at") or deployment.get("created_at") or ""), vercel_id, name))
        records.sort(reverse=True)
        if not records:
            return None
        _, vercel_id, name = records[0]
        live = self._verify(vercel_id, name)
        try:
            self.store.bind_vercel_project(
                tracking_project_id,
                vercel_project_id=live["id"],
                project_name=live["name"],
                provider_created_at=live.get("created_at"),
                binding_source="historical-ledger",
            )
        except ValueError as error:
            raise VercelProjectProvisioningError(str(error), code="VERCEL_PROJECT_BINDING_CONFLICT") from None
        return live

    def _reconcile_pending(self, tracking_project_id: str, project: dict[str, Any]) -> dict[str, Any]:
        name = str(project.get("isolated_preview_project") or "").strip()
        status = str(project.get("vercel_project_status") or "")
        intent_at = project.get("vercel_project_intent_at")
        if not name or not intent_at or status not in {"CREATE_INTENT", "CREATE_UNCERTAIN", "CREATE_FAILED"}:
            raise VercelProjectProvisioningError(
                "The tracked business has a Vercel project name but no verified project ID. "
                "Refusing to guess which existing project to adopt.",
                code="VERCEL_PROJECT_BINDING_REQUIRED",
            )
        try:
            remote = self._api().get_project_by_name(name)
        except (ProviderError, SiteManagementError) as error:
            raise VercelProjectProvisioningError(
                f"Cannot reconcile the pending project creation: {error}. No new project was created.",
                code="VERCEL_PROJECT_RECONCILIATION_FAILED",
            ) from None
        if remote is not None:
            intended = _epoch_ms(intent_at)
            created = _epoch_ms(remote.get("created_at"))
            if intended is not None and created is not None and created >= intended:
                live = self._verify(remote["id"], name)
                try:
                    self.store.bind_vercel_project(
                        tracking_project_id, vercel_project_id=live["id"], project_name=live["name"],
                        provider_created_at=live.get("created_at"), binding_source="recovered-create-intent",
                    )
                except ValueError as error:
                    raise VercelProjectProvisioningError(str(error), code="VERCEL_PROJECT_BINDING_CONFLICT") from None
                return live
            if status == "CREATE_UNCERTAIN":
                raise VercelProjectProvisioningError(
                    "A project with the requested name exists, but its creation time does not prove it came from "
                    "this agent's recorded intent. Nothing was adopted or deployed; review the exact project ID.",
                    code="VERCEL_PROJECT_OWNERSHIP_UNCERTAIN",
                )
            # A project that pre-dates a known, still-unsubmitted intent is a collision, not a project to adopt.
            self.store.mark_vercel_project_name_conflict(tracking_project_id, project_name=name)
            return self._create_with_next_name(tracking_project_id, project, base_name=str(project.get("vercel_project_base_name") or name), first_ordinal=2)
        if status in {"CREATE_INTENT", "CREATE_UNCERTAIN"}:
            if status == "CREATE_INTENT":
                self.store.mark_vercel_project_create_uncertain(
                    tracking_project_id, error_code="VERCEL_PROJECT_CREATE_OUTCOME_UNKNOWN",
                )
            raise VercelProjectProvisioningError(
                "The previous Vercel project-creation request has no confirmed response and the named project "
                "is not visible yet. No duplicate project was created; inspect Vercel before retrying.",
                code="VERCEL_PROJECT_CREATE_UNCERTAIN",
            )
        # CREATE_FAILED is written only after a definitive non-retryable client error (for example 401/403).
        # A new user-triggered deploy may retry once configuration is corrected.
        return self._create_named_project(tracking_project_id, project, name=name,
                                          base_name=str(project.get("vercel_project_base_name") or name))

    @staticmethod
    def _next_ordinal(base_name: str, current_name: str) -> int:
        if current_name == base_name:
            return 2
        match = re.fullmatch(re.escape(base_name) + r"-(\d+)", current_name)
        if match:
            return int(match.group(1)) + 1
        # The bound comes from project_name_base; if a custom legacy base has no suffix shape,
        # start at two rather than reusing the name that was just rejected.
        return 2

    def _create_with_next_name(
        self, tracking_project_id: str, project: dict[str, Any], *, base_name: str, first_ordinal: int,
    ) -> dict[str, Any]:
        for ordinal in range(first_ordinal, self.max_name_attempts + 1):
            name = _candidate_name(base_name, ordinal)
            try:
                existing = self._api().get_project_by_name(name)
            except (ProviderError, SiteManagementError) as error:
                raise VercelProjectProvisioningError(
                    f"Cannot verify whether the generated project name is available: {error}.",
                    code="VERCEL_PROJECT_LOOKUP_FAILED",
                ) from None
            if existing is not None:
                continue
            return self._create_named_project(tracking_project_id, project, name=name, base_name=base_name)
        raise VercelProjectProvisioningError(
            "Could not find an unused project name after bounded collision checks; nothing was deployed.",
            code="VERCEL_PROJECT_NAME_EXHAUSTED",
        )

    def _create_named_project(
        self, tracking_project_id: str, project: dict[str, Any], *, name: str, base_name: str,
    ) -> dict[str, Any]:
        try:
            self.store.begin_vercel_project_provisioning(tracking_project_id, name, base_name=base_name)
        except ValueError as error:
            raise VercelProjectProvisioningError(str(error), code="VERCEL_PROJECT_INTENT_REFUSED") from None
        # Resolve credentials/configuration before marking a provider call as in-flight: a missing token
        # is a definitive local blocker, not an ambiguous remote creation outcome.
        api = self._api()
        try:
            created = api.create_project(name)
        except ProviderError as error:
            if _is_name_conflict(error):
                self.store.mark_vercel_project_name_conflict(tracking_project_id, project_name=name)
                return self._create_with_next_name(
                    tracking_project_id, project, base_name=base_name,
                    first_ordinal=self._next_ordinal(base_name, name),
                )
            if error.status is not None and 400 <= error.status < 500 and error.status not in {408, 429}:
                target = (self.store.state.get("projects") or {}).get(tracking_project_id) or {}
                target["vercel_project_status"] = "CREATE_FAILED"
                target["vercel_project_error_code"] = str(error.code or "VERCEL_PROJECT_CREATE_FAILED")
                target["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                self.store._save()
                raise VercelProjectProvisioningError(
                    f"Vercel rejected project creation: {error}. No deployment was attempted.",
                    code="VERCEL_PROJECT_CREATE_FAILED",
                ) from None
            try:
                self.store.mark_vercel_project_create_uncertain(
                    tracking_project_id, error_code=str(error.code or "VERCEL_PROJECT_CREATE_UNCERTAIN"),
                )
            except ValueError:
                pass
            raise VercelProjectProvisioningError(
                f"Vercel may have created the project but did not return a confirmed result: {error}. "
                "No deployment was attempted and automatic creation will not be retried.",
                code="VERCEL_PROJECT_CREATE_UNCERTAIN",
            ) from None
        except (SiteManagementError, ValueError) as error:
            # If a malformed success response hides a created project, retain the intent for safe reconciliation.
            try:
                self.store.mark_vercel_project_create_uncertain(
                    tracking_project_id, error_code="VERCEL_PROJECT_CREATE_RESPONSE_INVALID",
                )
            except ValueError:
                pass
            raise VercelProjectProvisioningError(
                f"Vercel's project-creation result could not be verified: {error}. No deployment was attempted.",
                code="VERCEL_PROJECT_CREATE_RESPONSE_INVALID",
            ) from None
        if created.get("name") != name:
            try:
                self.store.mark_vercel_project_create_uncertain(
                    tracking_project_id, error_code="VERCEL_PROJECT_IDENTITY_MISMATCH",
                )
            except ValueError:
                pass
            raise VercelProjectProvisioningError(
                "Vercel returned a project with a different name. No deployment was attempted.",
                code="VERCEL_PROJECT_IDENTITY_MISMATCH",
            )
        live = self._verify(created["id"], name)
        try:
            self.store.bind_vercel_project(
                tracking_project_id, vercel_project_id=live["id"], project_name=live["name"],
                provider_created_at=live.get("created_at"), binding_source="api-created",
            )
        except ValueError as error:
            raise VercelProjectProvisioningError(
                f"Vercel created project {name!r}, but the durable tracking binding failed: {error}. "
                "No deployment was attempted; reconcile this project manually before retrying.",
                code="VERCEL_PROJECT_BINDING_PERSIST_FAILED",
            ) from None
        return live

    def ensure_project(self, tracking_project_id: str) -> dict[str, Any]:
        """Return the exact business project ID/name, creating it only when no target has been configured."""
        self._refresh()
        project, lead = self._project(tracking_project_id)
        project_id = str(project.get("vercel_project_id") or "").strip()
        project_name = str(project.get("isolated_preview_project") or "").strip()
        if project_id:
            if not project_name:
                raise VercelProjectProvisioningError(
                    "A Vercel project ID is saved without its expected name. Reconcile the binding before deployment.",
                    code="VERCEL_PROJECT_BINDING_INCOMPLETE",
                )
            return self._verify(project_id, project_name)

        # Migrate only from a successful deployment belonging to this same tracking project, and verify it live.
        historical = self._historical_binding(tracking_project_id, project)
        if historical is not None:
            return historical

        status = str(project.get("vercel_project_status") or "")
        if status in {"CREATE_INTENT", "CREATE_UNCERTAIN", "CREATE_FAILED"}:
            return self._reconcile_pending(tracking_project_id, project)
        if project_name:
            raise VercelProjectProvisioningError(
                f"Tracking project already names '{project_name}' but has no verified Vercel ID. "
                "It will not be overwritten or silently adopted; record a verified project binding first.",
                code="VERCEL_PROJECT_BINDING_REQUIRED",
            )
        base = project_name_base(str(lead.get("business_name") or "business"), tracking_project_id, str(project.get("lead_id") or ""))
        return self._create_with_next_name(tracking_project_id, project, base_name=base, first_ordinal=1)
