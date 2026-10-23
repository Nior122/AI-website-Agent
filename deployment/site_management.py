"""Approval-gated inventory and lifecycle management for Agent-owned Vercel sites.

Deletion requires a project ID from a recorded successful deployment, a live API read that confirms the
ID/name pair, an allow-listed operator, and a fingerprint-bound second confirmation. Provider uncertainty is
recorded and is never retried automatically.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from integrations.http import ProviderError, provider_request

API_ROOT = "https://api.vercel.com"
PROTECTED_PROJECT_NAMES = frozenset({
    "ai-website-agent-preview",
    "digitaljoe",
    "portfolio",
    "josephportifolio",
    "joseph-portifolio",
    "josephportfolio",
    "joseph-portfolio",
})


class SiteManagementError(ValueError):
    """An operator-readable refusal with a stable code."""

    def __init__(self, message: str, *, code: str = "SITE_OPERATION_REFUSED") -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _site_id(project_id: str) -> str:
    """Stable short ID derived from an immutable Vercel project ID, never a mutable name."""
    return "site-" + hashlib.sha256(str(project_id).encode("utf-8")).hexdigest()[:10]


def _protected_ids() -> set[str]:
    return {item.strip() for item in (os.environ.get("AI_WEBSITE_AGENT_VERCEL_PROTECTED_PROJECT_IDS") or "").split(",")
            if item.strip()}


def _protected_names() -> set[str]:
    extra = {item.strip().casefold() for item in
             (os.environ.get("AI_WEBSITE_AGENT_VERCEL_PROTECTED_PROJECT_NAMES") or "").split(",")
             if item.strip()}
    return set(PROTECTED_PROJECT_NAMES) | extra


def _is_protected(project_id: str, project_name: str) -> bool:
    return project_id in _protected_ids() or project_name.strip().casefold() in _protected_names()


class VercelProjectsClient:
    """Small authenticated Vercel project client using the repository's safe HTTPS transport."""

    def __init__(self, *, token: str | None = None, team_id: str | None = None, request=provider_request) -> None:
        self.token = str(token or os.environ.get("AI_WEBSITE_AGENT_VERCEL_TOKEN")
                         or os.environ.get("VERCEL_TOKEN") or "").strip()
        self.team_id = str(team_id or os.environ.get("AI_WEBSITE_AGENT_VERCEL_TEAM_ID")
                           or os.environ.get("VERCEL_TEAM_ID") or "").strip()
        self.request = request
        if not self.token:
            raise SiteManagementError(
                "Vercel project management is not configured: set AI_WEBSITE_AGENT_VERCEL_TOKEN.",
                code="VERCEL_TOKEN_MISSING",
            )
        if not self.team_id:
            raise SiteManagementError(
                "Vercel project management requires AI_WEBSITE_AGENT_VERCEL_TEAM_ID to prevent operating on "
                "the wrong account.",
                code="VERCEL_TEAM_ID_MISSING",
            )

    def _call(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{API_ROOT}{path}"
        joiner = "&" if "?" in url else "?"
        url += joiner + urlencode({"teamId": self.team_id})
        request_args: dict[str, Any] = {
            "method": method,
            "headers": {"Authorization": f"Bearer {self.token}"},
            "timeout": 15.0,
            "max_bytes": 1_000_000,
        }
        if json_body is not None:
            request_args["json_body"] = json_body
        answer = self.request(url, **request_args)
        if not isinstance(answer, dict):
            raise SiteManagementError("Vercel returned an invalid API response.", code="VERCEL_RESPONSE_INVALID")
        return answer

    def get_project(self, project_id: str) -> dict[str, Any]:
        ident = str(project_id or "").strip()
        if not ident or not re.fullmatch(r"prj_[A-Za-z0-9]+", ident):
            raise SiteManagementError("A valid recorded Vercel project ID is required.", code="VERCEL_PROJECT_ID_INVALID")
        answer = self._call("GET", f"/v9/projects/{quote(ident, safe='')}")
        payload = answer.get("json")
        if isinstance(payload, dict) and isinstance(payload.get("project"), dict):
            payload = payload["project"]
        if not isinstance(payload, dict):
            raise SiteManagementError("Vercel did not return the requested project record.",
                                      code="VERCEL_PROJECT_RESPONSE_INVALID")
        actual_id = str(payload.get("id") or "")
        actual_name = str(payload.get("name") or "")
        if actual_id != ident or not actual_name:
            raise SiteManagementError(
                "Vercel project identity did not match the recorded project ID; refusing to operate on it.",
                code="VERCEL_PROJECT_IDENTITY_MISMATCH",
            )
        return {"id": actual_id, "name": actual_name, "updated_at": payload.get("updatedAt"),
                "created_at": payload.get("createdAt")}

    @staticmethod
    def _normalise_project(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict) and isinstance(payload.get("project"), dict):
            payload = payload["project"]
        if not isinstance(payload, dict):
            raise SiteManagementError("Vercel did not return a project record.",
                                      code="VERCEL_PROJECT_RESPONSE_INVALID")
        project_id = str(payload.get("id") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not re.fullmatch(r"prj_[A-Za-z0-9]+", project_id) or not name:
            raise SiteManagementError("Vercel returned an invalid project ID/name pair.",
                                      code="VERCEL_PROJECT_RESPONSE_INVALID")
        return {"id": project_id, "name": name, "updated_at": payload.get("updatedAt"),
                "created_at": payload.get("createdAt")}

    def get_project_by_name(self, project_name: str) -> dict[str, Any] | None:
        """Look up a project name without ever treating an API outage as name availability."""
        name = str(project_name or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
            raise SiteManagementError("A valid lowercase Vercel project name is required.",
                                      code="VERCEL_PROJECT_NAME_INVALID")
        try:
            answer = self._call("GET", f"/v9/projects/{quote(name, safe='')}")
        except ProviderError as error:
            if error.status == 404:
                return None
            raise
        project = self._normalise_project(answer.get("json"))
        if project["name"] != name:
            raise SiteManagementError("Vercel project lookup returned a different name; refusing to adopt it.",
                                      code="VERCEL_PROJECT_IDENTITY_MISMATCH")
        return project

    def create_project(self, project_name: str) -> dict[str, Any]:
        """Create a standalone, unlinked static-site project; this method never attaches domains."""
        name = str(project_name or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", name):
            raise SiteManagementError("A valid lowercase Vercel project name is required.",
                                      code="VERCEL_PROJECT_NAME_INVALID")
        # Keep this payload minimal: no Git repository, environment variables, domains, or
        # framework-specific build settings are attached during provisioning.
        answer = self._call("POST", "/v11/projects", json_body={"name": name})
        project = self._normalise_project(answer.get("json"))
        if project["name"] != name:
            raise SiteManagementError("Vercel created a project with a different name; refusing deployment.",
                                      code="VERCEL_PROJECT_IDENTITY_MISMATCH")
        return project

    def delete_project(self, project_id: str, *, expected_name: str) -> dict[str, Any]:
        """Delete only the exact project reviewed by the operator. Re-read identity immediately before DELETE."""
        current = self.get_project(project_id)
        if current["name"] != expected_name:
            raise SiteManagementError(
                "The Vercel project name changed since approval. Nothing was deleted; request a fresh review.",
                code="VERCEL_PROJECT_CHANGED",
            )
        if _is_protected(current["id"], current["name"]):
            raise SiteManagementError("This Vercel project is protected and cannot be deleted by site commands.",
                                      code="VERCEL_PROJECT_PROTECTED")
        answer = self._call("DELETE", f"/v9/projects/{quote(current['id'], safe='')}")
        status = int(answer.get("status") or 0)
        if status and status not in {200, 202, 204}:
            raise SiteManagementError(f"Vercel did not confirm deletion (HTTP {status}).",
                                      code="VERCEL_DELETE_NOT_CONFIRMED")
        return {"deleted": True, "project_id": current["id"], "project_name": current["name"],
                "http_status": status or 200}


class ManagedSiteService:
    """Read managed-site inventory from TrackingStore and confirm destructive actions explicitly."""

    def __init__(self, store, *, client: VercelProjectsClient | None = None) -> None:
        self.store = store
        self.client = client
        self.store.state.setdefault("managed_sites", {})

    def _client(self) -> VercelProjectsClient:
        if self.client is None:
            self.client = VercelProjectsClient()
        return self.client

    def _refresh(self) -> None:
        if getattr(self.store, "db_backend", None) is not None and hasattr(self.store, "refresh"):
            self.store.refresh()
        self.store.state.setdefault("managed_sites", {})

    def _latest_recorded_deployment(self, vercel_project_id: str) -> dict[str, Any] | None:
        """Return the newest successful deployment for a Vercel project from the durable ledger."""
        candidates: list[dict[str, Any]] = []
        for deployment in (self.store.state.get("deployments") or {}).values():
            if not isinstance(deployment, dict) or deployment.get("deployment_status") != "DEPLOYED":
                continue
            url = str(deployment.get("deployment_url") or "")
            if not url.startswith("https://"):
                continue
            metadata = deployment.get("metadata") if isinstance(deployment.get("metadata"), dict) else {}
            recorded_id = str(deployment.get("vercel_project_id") or metadata.get("vercel_project_id") or "").strip()
            if recorded_id != vercel_project_id:
                continue
            candidates.append({
                "deployment_id": str(deployment.get("deployment_id") or ""),
                "deployment_url": url,
                "last_deployed_at": str(deployment.get("completed_at") or deployment.get("created_at") or ""),
            })
        candidates.sort(key=lambda row: (row["last_deployed_at"], row["deployment_id"]), reverse=True)
        return candidates[0] if candidates else None

    def list_sites(self, *, include_deleted: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        """List projects represented by successful deployment records plus durable deletion tombstones."""
        self._refresh()
        state = self.store.state
        projects = state.get("projects") or {}
        leads = state.get("leads") or {}
        registry = state.get("managed_sites") or {}
        grouped: dict[str, dict[str, Any]] = {}

        for deployment in (state.get("deployments") or {}).values():
            if not isinstance(deployment, dict) or deployment.get("deployment_status") != "DEPLOYED":
                continue
            url = str(deployment.get("deployment_url") or "")
            if not url.startswith("https://"):
                continue
            tracking_project_id = str(deployment.get("project_id") or "")
            project = projects.get(tracking_project_id) or {}
            lead_id = str(deployment.get("lead_id") or project.get("lead_id") or "")
            lead = leads.get(lead_id) or {}
            metadata = deployment.get("metadata") if isinstance(deployment.get("metadata"), dict) else {}
            vercel_id = str(deployment.get("vercel_project_id") or metadata.get("vercel_project_id") or "").strip()
            key = vercel_id or f"unmanaged:{tracking_project_id}"
            updated = str(deployment.get("completed_at") or deployment.get("created_at") or "")
            row = grouped.get(key)
            if row is None:
                row = {
                    "site_id": _site_id(vercel_id or key),
                    "vercel_project_id": vercel_id or None,
                    "vercel_project_name": str(metadata.get("project_name") or deployment.get("vercel_project_name")
                                                or project.get("isolated_preview_project") or "").strip() or None,
                    "business_name": str(lead.get("business_name") or deployment.get("business_name")
                                         or "Unknown business"),
                    "lead_id": lead_id or None,
                    "tracking_project_ids": [],
                    "deployment_id": str(deployment.get("deployment_id") or ""),
                    "deployment_url": url,
                    "deployment_status": "DEPLOYED",
                    "deployment_history_count": 0,
                    "last_deployed_at": updated or None,
                    "site_status": "DEPLOYED" if vercel_id else "DEPLOYED_UNMANAGED",
                    "managed": bool(vercel_id),
                }
                grouped[key] = row
            row["deployment_history_count"] += 1
            if tracking_project_id and tracking_project_id not in row["tracking_project_ids"]:
                row["tracking_project_ids"].append(tracking_project_id)
            if updated >= str(row.get("last_deployed_at") or ""):
                row.update({
                    "business_name": str(lead.get("business_name") or row["business_name"]),
                    "lead_id": lead_id or row.get("lead_id"),
                    "deployment_id": str(deployment.get("deployment_id") or ""),
                    "deployment_url": url,
                    "last_deployed_at": updated or None,
                    "vercel_project_name": str(metadata.get("project_name") or deployment.get("vercel_project_name")
                                                or project.get("isolated_preview_project")
                                                or row.get("vercel_project_name") or "").strip() or None,
                })

        merged: dict[str, dict[str, Any]] = {}
        for base in grouped.values():
            site_id = base["site_id"]
            persisted = registry.get(site_id) if isinstance(registry.get(site_id), dict) else {}
            # Live deployment facts must win over the last lifecycle snapshot. Preserve only the
            # durable deletion/tombstone state from the registry so a new deployment cannot be hidden
            # behind an old deletion request.
            row = {**copy.deepcopy(persisted), **base}
            row["site_id"] = site_id
            for lifecycle_field in ("site_status", "pending_deletion", "deletion_history", "deleted_at", "updated_at"):
                if lifecycle_field in persisted:
                    row[lifecycle_field] = copy.deepcopy(persisted[lifecycle_field])
            if len(row.get("tracking_project_ids") or []) > 1:
                row["shared_tracking_projects"] = True
            merged[site_id] = row
        for site_id, persisted in registry.items():
            if site_id not in merged and isinstance(persisted, dict):
                merged[site_id] = copy.deepcopy(persisted)

        rows = list(merged.values())
        if not include_deleted:
            rows = [row for row in rows if row.get("site_status") not in {"DELETED"}]
        rows.sort(key=lambda row: (str(row.get("last_deployed_at") or row.get("updated_at") or ""), row["site_id"]),
                  reverse=True)
        return rows[:max(1, min(int(limit or 100), 100))]

    def get_site(self, site_id: str, *, include_deleted: bool = True) -> dict[str, Any]:
        ident = str(site_id or "").strip()
        if not ident:
            raise SiteManagementError("Name a site ID, for example site-a1b2c3d4e5.", code="SITE_ID_REQUIRED")
        for row in self.list_sites(include_deleted=include_deleted, limit=100):
            if row.get("site_id") == ident:
                return row
        raise SiteManagementError(f"No managed website is recorded as {ident}. Run /sites to see site IDs.",
                                  code="SITE_NOT_FOUND")

    def request_delete(self, site_id: str, *, actor: str) -> dict[str, Any]:
        """Create a fingerprint-bound deletion request; this operation does not delete anything."""
        site = self.get_site(site_id, include_deleted=True)
        if site.get("site_status") == "DELETED":
            raise SiteManagementError("This site is already recorded as deleted.", code="SITE_ALREADY_DELETED")
        project_id = str(site.get("vercel_project_id") or "")
        if not project_id:
            raise SiteManagementError(
                "This deployment has no recorded Vercel project ID, so it cannot be safely deleted automatically.",
                code="VERCEL_PROJECT_ID_MISSING",
            )
        if site.get("shared_tracking_projects"):
            raise SiteManagementError(
                "This Vercel project is referenced by more than one tracking project. Separate the sites first; "
                "deleting it here could remove another business's website.",
                code="VERCEL_PROJECT_SHARED",
            )
        client = self._client()
        current = client.get_project(project_id)
        if _is_protected(current["id"], current["name"]):
            raise SiteManagementError(
                f"Vercel project '{current['name']}' is protected. No deletion request was created.",
                code="VERCEL_PROJECT_PROTECTED",
            )
        latest_deployment = self._latest_recorded_deployment(project_id)
        if latest_deployment is None:
            raise SiteManagementError(
                "No current successful deployment record matches this Vercel project. Reconcile the inventory before requesting deletion.",
                code="SITE_DEPLOYMENT_RECORD_MISSING",
            )
        # Use current ledger evidence, not fields copied from a potentially older deletion snapshot.
        site.update(latest_deployment)
        recorded_name = str(site.get("vercel_project_name") or "").strip()
        if recorded_name and recorded_name != current["name"]:
            raise SiteManagementError(
                "The live Vercel project name differs from the deployment record. Refresh/reconcile inventory "
                "before requesting deletion.",
                code="VERCEL_PROJECT_CHANGED",
            )
        registry = self.store.state.setdefault("managed_sites", {})
        existing = registry.get(site_id)
        if isinstance(existing, dict):
            pending = existing.get("pending_deletion")
            if isinstance(pending, dict) and pending.get("status") in {"AWAITING_CONFIRMATION", "DELETING", "UNCERTAIN"}:
                return {"site": copy.deepcopy(existing), "request": copy.deepcopy(pending),
                        "deletion_performed": False, "reused_request": True}
        request_id = "site-delete-" + uuid.uuid4().hex[:10]
        requested_at = _now()
        material = {
            "site_id": site_id, "vercel_project_id": current["id"], "vercel_project_name": current["name"],
            "deployment_id": site.get("deployment_id"), "deployment_url": site.get("deployment_url"),
            "last_deployed_at": site.get("last_deployed_at"), "request_id": request_id,
            "requested_by": str(actor or "unknown"), "requested_at": requested_at,
        }
        fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        record = {
            **copy.deepcopy(site),
            "site_id": site_id,
            "vercel_project_id": current["id"],
            "vercel_project_name": current["name"],
            "site_status": "DELETE_AWAITING_CONFIRMATION",
            "updated_at": requested_at,
            "pending_deletion": {
                **material, "fingerprint": fingerprint, "status": "AWAITING_CONFIRMATION",
            },
            "deletion_history": copy.deepcopy((existing or {}).get("deletion_history") or []),
        }
        registry[site_id] = record
        self.store._save()
        return {"site": copy.deepcopy(record), "request": copy.deepcopy(record["pending_deletion"]),
                "deletion_performed": False, "reused_request": False}

    def confirm_delete(self, site_id: str, fingerprint: str, *, actor: str) -> dict[str, Any]:
        """Execute one deletion only after revalidating the current ID/name and the one-time fingerprint."""
        self._refresh()
        registry = self.store.state.setdefault("managed_sites", {})
        record = registry.get(str(site_id or "").strip())
        if not isinstance(record, dict):
            raise SiteManagementError("No pending deletion request exists for that site.", code="SITE_DELETE_REQUEST_NOT_FOUND")
        pending = record.get("pending_deletion")
        if not isinstance(pending, dict) or pending.get("status") != "AWAITING_CONFIRMATION":
            raise SiteManagementError(
                "There is no unused deletion confirmation for this site. Request a new review if the site is still present.",
                code="SITE_DELETE_CONFIRMATION_NOT_PENDING",
            )
        requested_at = str(pending.get("requested_at") or "")
        try:
            created = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            age_seconds = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            age_seconds = 10 * 60 + 1
        if age_seconds < 0 or age_seconds > 10 * 60:
            pending["status"] = "EXPIRED"
            record["site_status"] = "DELETE_CONFIRMATION_EXPIRED"
            record["updated_at"] = _now()
            self.store._save()
            raise SiteManagementError("This deletion confirmation expired after 10 minutes. Re-read the exact project and request a fresh review.", code="SITE_DELETE_CONFIRMATION_EXPIRED")
        supplied = str(fingerprint or "").strip()
        expected = str(pending.get("fingerprint") or "")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise SiteManagementError(
                "The confirmation fingerprint does not match the displayed deletion request. Nothing was deleted.",
                code="SITE_DELETE_CONFIRMATION_STALE",
            )
        project_id = str(pending.get("vercel_project_id") or "")
        expected_name = str(pending.get("vercel_project_name") or "")
        latest_deployment = self._latest_recorded_deployment(project_id)
        deployment_changed = (
            latest_deployment is None
            or str(latest_deployment.get("deployment_id") or "") != str(pending.get("deployment_id") or "")
            or str(latest_deployment.get("deployment_url") or "") != str(pending.get("deployment_url") or "")
            or str(latest_deployment.get("last_deployed_at") or "") != str(pending.get("last_deployed_at") or "")
        )
        if deployment_changed:
            pending["status"] = "STALE"
            pending["stale_at"] = _now()
            pending["error_code"] = "SITE_DELETE_REQUEST_STALE"
            record["site_status"] = "DELETE_REVIEW_STALE"
            record["updated_at"] = _now()
            self.store._save()
            raise SiteManagementError(
                "The website deployment changed after deletion was reviewed. Nothing was deleted; inspect the current site and request a fresh confirmation.",
                code="SITE_DELETE_REQUEST_STALE",
            )
        client = self._client()
        current = client.get_project(project_id)
        if current["name"] != expected_name or _is_protected(current["id"], current["name"]):
            raise SiteManagementError(
                "The Vercel project identity changed or is protected. Nothing was deleted; request a fresh review.",
                code="VERCEL_PROJECT_CHANGED",
            )
        now = _now()
        pending["status"] = "DELETING"
        pending["confirmed_by"] = str(actor or "unknown")
        pending["confirmed_at"] = now
        record["site_status"] = "DELETING"
        record["updated_at"] = now
        self.store._save()
        try:
            result = client.delete_project(project_id, expected_name=expected_name)
        except ProviderError as error:
            uncertain = bool(error.retryable or error.status is None or error.status >= 500)
            pending["status"] = "UNCERTAIN" if uncertain else "FAILED"
            pending["error_code"] = error.code
            pending["detail"] = str(error)[:300]
            record["site_status"] = "DELETE_UNCERTAIN" if uncertain else "DELETE_FAILED"
            record["updated_at"] = _now()
            record.setdefault("deletion_history", []).append(copy.deepcopy(pending))
            self.store._save()
            raise SiteManagementError(
                ("Vercel did not provide a definitive deletion result. The request is now uncertain; inspect the "
                 "Vercel dashboard before any retry." if uncertain else
                 f"Vercel refused the deletion ({error.code}). The site was not reported as deleted."),
                code="SITE_DELETE_UNCERTAIN" if uncertain else error.code,
            ) from None
        except SiteManagementError as error:
            pending["status"] = "FAILED"
            pending["error_code"] = error.code
            pending["detail"] = str(error)[:300]
            record["site_status"] = "DELETE_FAILED"
            record["updated_at"] = _now()
            record.setdefault("deletion_history", []).append(copy.deepcopy(pending))
            self.store._save()
            raise
        finished = _now()
        pending["status"] = "DELETED"
        pending["completed_at"] = finished
        pending["provider_result"] = {k: v for k, v in result.items()
                                      if k in {"deleted", "project_id", "project_name", "http_status"}}
        record["site_status"] = "DELETED"
        record["deleted_at"] = finished
        record["updated_at"] = finished
        record.setdefault("deletion_history", []).append(copy.deepcopy(pending))
        self.store._save()
        return {
            "site_id": record["site_id"], "business_name": record.get("business_name"),
            "vercel_project_id": project_id, "vercel_project_name": expected_name,
            "deployment_url": record.get("deployment_url"), "site_status": "DELETED",
            "deleted_at": finished, "provider_result": result,
        }
