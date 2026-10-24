from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import unittest

from deployment.provisioning import (
    VercelProjectProvisioner,
    VercelProjectProvisioningError,
    project_name_base,
)
from deployment.site_management import SiteManagementError, VercelProjectsClient
from integrations.http import ProviderError
from tracking.store import TrackingStore

class FakeVercelClient:
    def __init__(self):
        self.projects_by_id = {}
        self.projects_by_name = {}
        self.create_calls = []
        self.get_calls = []
        self.reject_create_names = set()
        self.create_error = None
        self.create_then_timeout = False
        self.next_id = 1

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def get_project_by_name(self, name):
        project = self.projects_by_name.get(name)
        return dict(project) if project else None

    def get_project(self, project_id):
        self.get_calls.append(project_id)
        project = self.projects_by_id.get(project_id)
        if not project:
            raise ProviderError("project missing", code="PROVIDER_NOT_FOUND", status=404)
        return dict(project)

    def create_project(self, name):
        self.create_calls.append(name)
        if name in self.reject_create_names:
            raise ProviderError("Project name is already taken", code="PROVIDER_REQUEST_REFUSED", status=409)
        project = {
            "id": f"prj_Fixture{self.next_id}",
            "name": name,
            "created_at": self._now(),
        }
        self.next_id += 1
        if self.create_error:
            raise self.create_error
        if not self.create_then_timeout:
            self.projects_by_id[project["id"]] = project
            self.projects_by_name[name] = project
        if self.create_then_timeout:
            self.projects_by_id[project["id"]] = project
            self.projects_by_name[name] = project
            raise ProviderError("network timed out after creating project",
                                code="PROVIDER_UNRESPONSIVE", retryable=True)
        return dict(project)


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = TrackingStore(Path(self.tempdir.name) / "state.json")
        self.lead = self.store.create_lead({
            "business_name": "Aisha's Coastal Kitchen",
            "city": "Lagos",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
            "demo": False,
        })
        self.project = self.store.create_project(self.lead["lead_id"])
        self.client = FakeVercelClient()
        self.provisioner = VercelProjectProvisioner(self.store, client=self.client)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_project_name_is_stable_bounded_and_distinguishes_tracking_projects(self):
        first = project_name_base("Aisha's Coastal Kitchen", "project-12", "lead-8")
        again = project_name_base("Aisha's Coastal Kitchen", "project-12", "lead-8")
        other = project_name_base("Aisha's Coastal Kitchen", "project-13", "lead-8")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertLessEqual(len(first), 63)
        self.assertRegex(first, r"^[a-z0-9][a-z0-9-]+$")
        self.assertIn("preview", first)

    def test_existing_unbound_create_intent_cannot_issue_a_second_create(self):
        name = project_name_base(
            self.lead["business_name"], self.project["project_id"], self.project["lead_id"],
        )
        self.store.begin_vercel_project_provisioning(
            self.project["project_id"], name, base_name=name,
        )
        intent = self.store.state["projects"][self.project["project_id"]]["vercel_project_intent_id"]
        with self.assertRaisesRegex(ValueError, "intent already exists"):
            self.store.begin_vercel_project_provisioning(
                self.project["project_id"], name, base_name=name,
            )
        saved = self.store.state["projects"][self.project["project_id"]]
        self.assertEqual(saved["vercel_project_status"], "CREATE_INTENT")
        self.assertEqual(saved["vercel_project_intent_id"], intent)
        self.assertEqual(self.client.create_calls, [])

    def test_same_vercel_project_id_cannot_be_bound_to_two_tracking_projects(self):
        first = self.provisioner.ensure_project(self.project["project_id"])
        other_lead = self.store.create_lead({
            "business_name": "Second Independent Business",
            "city": "Abuja",
            "country": "Nigeria",
            "qualification_status": "QUALIFIED",
            "data_classification": "REAL_PUBLIC_DATA",
            "demo": False,
        })
        other_project = self.store.create_project(other_lead["lead_id"])
        with self.assertRaisesRegex(ValueError, "already bound to tracked project"):
            self.store.bind_vercel_project(
                other_project["project_id"],
                vercel_project_id=first["id"],
                project_name=first["name"],
                binding_source="historical-ledger",
            )

    def test_missing_local_provider_configuration_does_not_leave_create_intent(self):
        from unittest.mock import patch

        with patch.object(
            self.provisioner, "_api",
            side_effect=VercelProjectProvisioningError(
                "Vercel project management is not configured.",
                code="VERCEL_TOKEN_MISSING",
            ),
        ):
            with self.assertRaises(VercelProjectProvisioningError) as caught:
                self.provisioner.ensure_project(self.project["project_id"])

        self.assertEqual(caught.exception.code, "VERCEL_TOKEN_MISSING")
        saved = self.store.state["projects"][self.project["project_id"]]
        self.assertFalse(saved.get("vercel_project_intent_at"))
        self.assertFalse(saved.get("vercel_project_id"))
        self.assertEqual(self.client.create_calls, [])

    def test_creates_and_binds_once_then_reuses_exact_project_id(self):
        first = self.provisioner.ensure_project(self.project["project_id"])
        second = self.provisioner.ensure_project(self.project["project_id"])
        saved = self.store.state["projects"][self.project["project_id"]]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(saved["vercel_project_id"], first["id"])
        self.assertEqual(saved["isolated_preview_project"], first["name"])
        self.assertEqual(saved["vercel_project_status"], "READY")
        self.assertEqual(len(self.client.create_calls), 1)

    def test_manual_name_without_verified_project_id_is_never_silently_adopted(self):
        self.store.configure_preview_target(self.project["project_id"], "manual-business-preview")
        with self.assertRaises(VercelProjectProvisioningError) as caught:
            self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(caught.exception.code, "VERCEL_PROJECT_BINDING_REQUIRED")
        self.assertEqual(self.client.create_calls, [])
        self.assertIsNone(self.store.state["projects"][self.project["project_id"]].get("vercel_project_id"))

    def test_ambiguous_create_outcome_is_not_retried_automatically(self):
        self.client.create_error = ProviderError("connection lost", code="PROVIDER_UNRESPONSIVE", retryable=True)
        with self.assertRaises(VercelProjectProvisioningError) as first:
            self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(first.exception.code, "VERCEL_PROJECT_CREATE_UNCERTAIN")
        state = self.store.state["projects"][self.project["project_id"]]
        self.assertEqual(state["vercel_project_status"], "CREATE_UNCERTAIN")
        self.client.create_error = None
        with self.assertRaises(VercelProjectProvisioningError) as second:
            self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(second.exception.code, "VERCEL_PROJECT_CREATE_UNCERTAIN")
        self.assertEqual(len(self.client.create_calls), 1)

    def test_response_lost_after_create_is_reconciled_without_duplicate_project(self):
        self.client.create_then_timeout = True
        with self.assertRaises(VercelProjectProvisioningError):
            self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(len(self.client.create_calls), 1)
        # Provider timestamps have millisecond precision. Make this fake outcome provably later than
        # the durable intent rather than depending on two clock reads landing in different milliseconds.
        name = self.client.create_calls[-1]
        project = self.client.projects_by_name[name]
        intent_at = self.store.state["projects"][self.project["project_id"]]["vercel_project_intent_at"]
        created_after_intent = datetime.fromisoformat(intent_at.replace("Z", "+00:00")) + timedelta(milliseconds=1)
        project["created_at"] = created_after_intent.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.client.projects_by_id[project["id"]]["created_at"] = project["created_at"]
        self.client.create_then_timeout = False
        result = self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(len(self.client.create_calls), 1)
        self.assertEqual(
            self.store.state["projects"][self.project["project_id"]]["vercel_project_id"],
            result["id"],
        )

    def test_missing_provider_timestamp_does_not_adopt_ambiguous_project(self):
        self.client.create_then_timeout = True
        with self.assertRaises(VercelProjectProvisioningError):
            self.provisioner.ensure_project(self.project["project_id"])
        name = self.client.create_calls[-1]
        project = self.client.projects_by_name[name]
        project["created_at"] = None
        self.client.projects_by_id[project["id"]]["created_at"] = None
        self.client.create_then_timeout = False

        with self.assertRaises(VercelProjectProvisioningError) as caught:
            self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(caught.exception.code, "VERCEL_PROJECT_OWNERSHIP_UNCERTAIN")
        self.assertEqual(len(self.client.create_calls), 1)
        self.assertFalse(self.store.state["projects"][self.project["project_id"]].get("vercel_project_id"))

    def test_name_collision_uses_a_new_suffix_not_the_same_failed_name(self):
        base = project_name_base(
            self.lead["business_name"], self.project["project_id"], self.project["lead_id"],
        )
        self.client.projects_by_name[base] = {
            "id": "prj_OldCollision", "name": base,
            "created_at": "2025-01-01T00:00:00Z",
        }
        self.client.projects_by_id["prj_OldCollision"] = dict(self.client.projects_by_name[base])
        second_name = f"{base}-2"
        self.client.reject_create_names.add(second_name)
        result = self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(result["name"], f"{base}-3")
        self.assertEqual(self.client.create_calls, [second_name, f"{base}-3"])
        self.assertEqual(
            self.store.state["projects"][self.project["project_id"]]["vercel_project_id"],
            result["id"],
        )

    def test_persisted_name_conflict_recovers_at_next_suffix(self):
        base = project_name_base(
            self.lead["business_name"], self.project["project_id"], self.project["lead_id"],
        )
        rejected = f"{base}-2"
        self.store.begin_vercel_project_provisioning(
            self.project["project_id"], rejected, base_name=base,
        )
        self.store.mark_vercel_project_name_conflict(
            self.project["project_id"], project_name=rejected,
        )
        collision = {
            "id": "prj_PriorSuffix", "name": rejected,
            "created_at": "2025-01-01T00:00:00Z",
        }
        self.client.projects_by_name[rejected] = dict(collision)
        self.client.projects_by_id[collision["id"]] = dict(collision)

        result = self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(result["name"], f"{base}-3")
        self.assertEqual(self.client.create_calls, [f"{base}-3"])

    def test_verified_historical_deployment_binding_migrates_without_new_project(self):
        name = "coastal-kitchen-preview"
        old = {
            "id": "prj_Historical123", "name": name,
            "created_at": "2025-10-01T00:00:00Z",
        }
        self.client.projects_by_id[old["id"]] = dict(old)
        self.client.projects_by_name[name] = dict(old)
        self.store.state["deployments"]["deployment-historical"] = {
            "deployment_id": "deployment-historical",
            "project_id": self.project["project_id"],
            "lead_id": self.lead["lead_id"],
            "deployment_status": "DEPLOYED",
            "vercel_project_id": old["id"],
            "deployment_url": "https://coastal-kitchen-preview.vercel.app",
            "metadata": {"project_name": name},
            "completed_at": "2025-10-01T00:00:00Z",
        }
        self.store._save()
        result = self.provisioner.ensure_project(self.project["project_id"])
        self.assertEqual(result["id"], old["id"])
        self.assertEqual(self.client.create_calls, [])
        self.assertEqual(
            self.store.state["projects"][self.project["project_id"]]["vercel_project_id"],
            old["id"],
        )


class VercelProjectsClientTests(unittest.TestCase):
    def test_deployment_readback_is_team_scoped_and_normalizes_authoritative_identity(self):
        recorded = {}
        def request(url, **kwargs):
            recorded["url"] = url
            recorded.update(kwargs)
            return {"status": 200, "json": {
                "id": "dpl_Verified123",
                "url": "coastal-kitchen-preview-abc.vercel.app",
                "projectId": "prj_Project123",
                "readyState": "READY",
            }, "text": ""}
        client = VercelProjectsClient(token="fixture-token", team_id="team_fixture", request=request)
        result = client.get_deployment_by_url("https://coastal-kitchen-preview-abc.vercel.app")
        self.assertEqual(result, {
            "id": "dpl_Verified123",
            "url": "coastal-kitchen-preview-abc.vercel.app",
            "project_id": "prj_Project123",
            "ready_state": "READY",
        })
        self.assertIn("/v13/deployments/coastal-kitchen-preview-abc.vercel.app?teamId=team_fixture", recorded["url"])
        self.assertEqual(recorded["method"], "GET")
        self.assertNotIn("fixture-token", recorded["url"])

    def test_deployment_readback_refuses_non_preview_host_before_network_call(self):
        calls = []
        client = VercelProjectsClient(
            token="fixture-token", team_id="team_fixture",
            request=lambda url, **kwargs: calls.append(url),
        )
        with self.assertRaises(SiteManagementError) as caught:
            client.get_deployment_by_url("https://production.example.com")
        self.assertEqual(getattr(caught.exception, "code", None), "VERCEL_DEPLOYMENT_URL_INVALID")
        self.assertEqual(calls, [])

    def test_create_project_uses_team_scoped_api_and_does_not_attach_domains(self):
        recorded = {}
        def request(url, **kwargs):
            recorded["url"] = url
            recorded.update(kwargs)
            return {"status": 200, "json": {
                "id": "prj_New123", "name": "coastal-kitchen-preview",
                "createdAt": 1790000000000,
            }, "text": ""}
        client = VercelProjectsClient(token="fixture-token", team_id="team_fixture", request=request)
        result = client.create_project("coastal-kitchen-preview")
        self.assertEqual(result["id"], "prj_New123")
        self.assertIn("/v11/projects?teamId=team_fixture", recorded["url"])
        self.assertEqual(recorded["method"], "POST")
        self.assertEqual(recorded["json_body"]["name"], "coastal-kitchen-preview")
        self.assertNotIn("gitRepository", recorded["json_body"])
        self.assertNotIn("domains", recorded["json_body"])
        self.assertNotIn("customDomains", recorded["json_body"])
        self.assertNotIn("fixture-token", recorded["url"])


if __name__ == "__main__":
    unittest.main()
