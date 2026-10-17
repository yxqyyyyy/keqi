import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studyreminder.calendar_sync import CalendarSync
from studyreminder.core import Store


class CalendarSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data.sqlite3")
        self.store.update_settings({"physics": {"enabled": True}})
        self.sync = CalendarSync(self.root, self.store)
        self.sync.helper.parent.mkdir(parents=True)
        self.sync.helper.touch()

    def tearDown(self):
        self.temp.cleanup()

    def result(self, mapping=None):
        return SimpleNamespace(stdout=json.dumps({"ok": True, "message": "测试返回", "calendar_identifier": "test-calendar",
                                                 "event_identifiers": mapping or {}}), returncode=0)

    def test_slow_sync_does_not_overwrite_new_settings_and_detects_new_tasks(self):
        def run(*args, **kwargs):
            self.store.update_settings({"physics": {"time": "23:00"}, "collection": {"auto_scan": True}})
            return self.result()
        with patch("studyreminder.calendar_sync.subprocess.run", side_effect=run):
            self.sync.sync()
        self.assertEqual(self.store.settings()["physics"]["time"], "23:00")
        self.assertTrue(self.store.settings()["collection"]["auto_scan"])
        self.assertTrue(self.sync.info()["needs_sync"])

    def test_completed_task_and_old_mapping_are_in_full_snapshot(self):
        item = self.store.put_task({"title": "测试", "source": "manual", "due_at": "2027-01-01T00:00:00+08:00"})
        self.store.put_task({"status": "completed"}, item["id"])
        self.store.set_value("event_identifiers", {item["id"]: "old-event"})
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()) as run:
            self.sync.sync()
        body = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(next(t for t in body["tasks"] if t["id"] == item["id"])["status"], "completed")
        self.assertEqual(body["calendar"]["event_identifiers"][item["id"]], "old-event")
        self.assertEqual(self.store.get_value("event_identifiers"), {})

    def test_rejected_settings_are_non_mutating(self):
        first = self.store.tasks()[0]
        self.store.put_task({"notes": "保留本周备注"}, first["id"])
        with self.assertRaises(ValueError):
            self.store.update_settings({"physics": {"time": "23:00"}, "collection": {"auto_scan": "bad"}})
        self.assertEqual(self.store.get_task(first["id"])["notes"], "保留本周备注")
        self.assertEqual(self.store.settings()["physics"]["time"], "24:00")

    def test_reminder_policy_change_migrates_unchanged_tasks(self):
        tasks = self.store.tasks()
        with patch("studyreminder.calendar_sync.REMINDER_HOURS", (48, 24)):
            self.store.set_value("synced_signature", self.sync.signature(tasks))
        self.assertTrue(self.sync.info()["needs_sync"])
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()):
            self.sync.sync()
        self.assertFalse(self.sync.info()["needs_sync"])

    def test_authorization_timeout_remains_visible_and_does_not_enable_sync(self):
        with patch("studyreminder.calendar_sync.subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 120)):
            with self.assertRaisesRegex(ValueError, "授权超时"):
                self.sync.sync()
        info = self.sync.info()
        self.assertFalse(info["ok"])
        self.assertIn("授权超时", info["message"])
        self.assertTrue(info["needs_sync"])
        self.assertFalse(self.store.settings()["calendar"]["auto_sync"])
        self.assertFalse(self.sync.lock.locked())


if __name__ == "__main__":
    unittest.main()
