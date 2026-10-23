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

    def test_request_does_not_delete_and_wrong_fingerprint_is_refused(self):
        site = self.service.list_sites()[0]
        answer = self.service.request_delete(site["site_id"], actor="telegram:1001")
        self.assertFalse(answer["deletion_performed"])
        self.assertEqual(self.client.deleted, [])
        with self.assertRaises(SiteManagementError) as caught:
            self.service.confirm_delete(site["site_id"], "wrong-fingerprint", actor="telegram:1001")
        self.assertEqual(caught.exception.code, "SITE_DELETE_CONFIRMATION_STALE")
        self.assertEqual(self.client.deleted, [])

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
