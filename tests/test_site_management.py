"""Offline tests for the managed Vercel site registry and explicit deletion confirmation."""
import tempfile
import unittest
from pathlib import Path

from tracking.store import TrackingStore
from deployment.site_management import ManagedSiteService, SiteManagementError


class FakeVercelClient:
    def __init__(self):
        self.project = {"id": "prj_Test123", "name": "pitstop-lagos-preview"}
        self.deleted = []
        self.get_calls = 0

    def get_project(self, project_id):
        self.get_calls += 1
        if project_id != self.project["id"]:
            raise SiteManagementError("wrong project", code="VERCEL_PROJECT_IDENTITY_MISMATCH")
        return dict(self.project)

    def delete_project(self, project_id, *, expected_name):
        if project_id != self.project["id"] or expected_name != self.project["name"]:
            raise SiteManagementError("identity changed", code="VERCEL_PROJECT_CHANGED")
        self.deleted.append(project_id)
        return {"deleted": True, "project_id": project_id, "project_name": expected_name, "http_status": 204}


class SiteManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TrackingStore(Path(self.tmp.name) / "state.json")
        self.store.state["leads"]["lead-1"] = {"lead_id": "lead-1", "business_name": "Pitstop Lagos"}
        self.store.state["projects"]["project-1"] = {"project_id": "project-1", "lead_id": "lead-1"}
        self.store.state["deployments"]["deployment-1"] = {
            "deployment_id": "deployment-1", "project_id": "project-1", "lead_id": "lead-1",
            "deployment_status": "DEPLOYED", "deployment_url": "https://pitstop-lagos-preview.vercel.app",
            "vercel_project_id": "prj_Test123", "metadata": {"project_name": "pitstop-lagos-preview"},
            "completed_at": "2026-10-20T10:00:00Z",
        }
        self.store._save()
        self.client = FakeVercelClient()
        self.service = ManagedSiteService(self.store, client=self.client)

    def test_list_uses_stable_project_id_and_retains_history(self):
        rows = self.service.list_sites()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site_id"], "site-" + __import__("hashlib").sha256(b"prj_Test123").hexdigest()[:10])
        self.assertEqual(rows[0]["vercel_project_id"], "prj_Test123")
        self.assertEqual(rows[0]["deployment_history_count"], 1)

    def test_provisioned_project_can_be_reviewed_and_deleted_before_first_deployment(self):
        self.store.state["deployments"].clear()
        self.store.state["projects"]["project-1"].update({
            "vercel_project_id": "prj_Test123",
            "isolated_preview_project": "pitstop-lagos-preview",
            "vercel_project_status": "READY",
            "vercel_project_bound_at": "2026-10-23T10:00:00Z",
            "vercel_project_intent_id": "provision-intent-1",
            "vercel_project_binding_history": [{
                "vercel_project_id": "prj_Test123",
                "project_name": "pitstop-lagos-preview",
                "bound_at": "2026-10-23T10:00:00Z",
                "source": "api-created",
            }],
        })
        self.store._save()

        rows = self.service.list_sites()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site_status"], "PROJECT_PROVISIONED")
        self.assertEqual(rows[0]["deployment_status"], "NOT_DEPLOYED")
        self.assertIsNone(rows[0]["deployment_url"])

        review = self.service.request_delete(rows[0]["site_id"], actor="telegram:1001")
        self.assertFalse(review["deletion_performed"])
        self.assertEqual(review["request"]["deletion_target"], "PROJECT_ONLY")
        self.assertEqual(self.client.deleted, [])

        answer = self.service.confirm_delete(
            rows[0]["site_id"], review["request"]["fingerprint"], actor="telegram:1001"
        )
        self.assertEqual(answer["site_status"], "DELETED")
        self.assertEqual(self.client.deleted, ["prj_Test123"])
        self.assertEqual(self.service.list_sites(), [])
        self.assertEqual(self.service.list_sites(include_deleted=True)[0]["site_status"], "DELETED")

    def test_request_does_not_delete_and_wrong_fingerprint_is_refused(self):
        site = self.service.list_sites()[0]
        answer = self.service.request_delete(site["site_id"], actor="telegram:1001")
        self.assertFalse(answer["deletion_performed"])
        self.assertEqual(self.client.deleted, [])
        with self.assertRaises(SiteManagementError) as caught:
            self.service.confirm_delete(site["site_id"], "wrong-fingerprint", actor="telegram:1001")
        self.assertEqual(caught.exception.code, "SITE_DELETE_CONFIRMATION_STALE")
        self.assertEqual(self.client.deleted, [])

    def test_new_deployment_after_request_invalidates_old_confirmation(self):
        site = self.service.list_sites()[0]
        request = self.service.request_delete(site["site_id"], actor="telegram:1001")["request"]
        self.store.state["deployments"]["deployment-2"] = {
            "deployment_id": "deployment-2", "project_id": "project-1", "lead_id": "lead-1",
            "deployment_status": "DEPLOYED", "deployment_url": "https://pitstop-lagos-preview-new.vercel.app",
            "vercel_project_id": "prj_Test123", "metadata": {"project_name": "pitstop-lagos-preview"},
            "completed_at": "2026-10-23T11:00:00Z",
        }
        self.store._save()

        with self.assertRaises(SiteManagementError) as caught:
            self.service.confirm_delete(site["site_id"], request["fingerprint"], actor="telegram:1001")
        self.assertEqual(caught.exception.code, "SITE_DELETE_REQUEST_STALE")
        self.assertEqual(self.client.deleted, [])
        record = self.store.state["managed_sites"][site["site_id"]]
        self.assertEqual(record["site_status"], "DELETE_REVIEW_STALE")
        self.assertEqual(record["pending_deletion"]["status"], "STALE")

    def test_exact_confirmation_deletes_once_and_retains_tombstone(self):
        site = self.service.list_sites()[0]
        request = self.service.request_delete(site["site_id"], actor="telegram:1001")["request"]
        answer = self.service.confirm_delete(site["site_id"], request["fingerprint"], actor="telegram:1001")
        self.assertEqual(answer["site_status"], "DELETED")
        self.assertEqual(self.client.deleted, ["prj_Test123"])
        self.assertEqual(self.service.list_sites(), [])
        self.assertEqual(self.service.list_sites(include_deleted=True)[0]["site_status"], "DELETED")
        with self.assertRaises(SiteManagementError):
            self.service.confirm_delete(site["site_id"], request["fingerprint"], actor="telegram:1001")
        self.assertEqual(len(self.client.deleted), 1)


if __name__ == "__main__":
    unittest.main()
