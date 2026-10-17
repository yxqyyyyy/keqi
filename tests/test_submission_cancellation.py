import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import Application


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch("server.local_ip", return_value="127.0.0.1"):
            self.app = Application(self.temp.name)
        self.item = self.app.store.put_task({"title": "作业3-1", "source": "chaoxing", "course": "概率论",
                                             "source_key": "course:1:class:2:work:3", "due_at": "2026-10-21T09:50:00+08:00"})
        self.app.calendar.sync_if_enabled = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def test_submission_cancels_existing_task_even_without_deadline_candidate(self):
        result = {"candidates": [], "warnings": [], "status_updates": [
            {"source": "chaoxing", "source_key": self.item["source_key"], "status": "completed"}
        ]}
        self.app.collector = SimpleNamespace(request=lambda *args: result)
        value = self.app.scan()
        self.assertEqual(value["completed"], 1)
        self.assertEqual(self.app.store.get_task(self.item["id"])["status"], "completed")
        self.app.calendar.sync_if_enabled.assert_called_once()

    def test_unknown_or_other_source_does_not_cancel(self):
        result = {"candidates": [], "warnings": [], "status_updates": [
            {"source": "chaoxing", "source_key": self.item["source_key"], "status": "unknown"},
            {"source": "xiji", "source_key": self.item["source_key"], "status": "completed"}
        ]}
        self.app.collector = SimpleNamespace(request=lambda *args: result)
        value = self.app.scan()
        self.assertEqual(value["completed"], 0)
        self.assertEqual(self.app.store.get_task(self.item["id"])["status"], "pending")
        self.app.calendar.sync_if_enabled.assert_not_called()

    def test_completed_records_not_offered_as_new_reminders(self):
        done = {**self.item, "status": "completed"}
        self.app.collector = SimpleNamespace(request=lambda *args: {"candidates": [done], "warnings": [], "status_updates": []})
        value = self.app.scan()
        self.assertEqual(value["candidates"], [])

    def test_daily_sync_adds_new_records_and_preserves_completed_status(self):
        new = {"title": "新作业", "source": "xiji", "course": "离散数学", "kind": "assignment", "source_key": "course:87:assignment:999",
               "status": "completed", "due_at": "2026-10-21T23:59:00+08:00"}
        self.app.collector = SimpleNamespace(request=lambda *args: {"candidates": [new], "warnings": [], "status_updates": [], "coverage_complete": True})
        first = self.app.scan(update_existing=True, import_new=True)
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["candidates"], [])
        stored = next(t for t in self.app.store.tasks() if t.get("source_key") == new["source_key"])
        self.assertEqual(stored["status"], "completed")
        self.assertIsNotNone(self.app.store.get_value("daily_last_success"))
        again = self.app.scan(update_existing=True, import_new=True)
        self.assertEqual(again["added"], 0)

    def test_incomplete_scan_does_not_count_as_daily_success(self):
        self.app.collector = SimpleNamespace(request=lambda *args: {"candidates": [], "warnings": ["未登录"], "status_updates": [], "coverage_complete": False})
        self.app.scan(update_existing=True, import_new=True)
        self.assertIsNone(self.app.store.get_value("daily_last_success"))
        self.assertIn("未能完整读取", self.app.store.get_value("collection_last_error")["message"])


if __name__ == "__main__":
    unittest.main()
