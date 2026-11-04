import json
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from studyreminder.calendar_sync import CalendarSync
from studyreminder.core import Store
from studyreminder.sync_status import sync_signature


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

    def platform_time(self, value):
        self.store.set_value("collection_sync", {"last_success_at": value,
            "success_scope": sync_signature(self.store.settings()["collection"])})

    def test_platform_verification_time_alone_updates_existing_calendar_snapshot(self):
        self.platform_time("2026-10-18T08:00:00+08:00")
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()) as run:
            self.sync.sync()
            body = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(body["platform_synced_at"], "2026-10-18T08:00:00+08:00")
        self.assertFalse(self.sync.info()["needs_sync"])
        tasks = self.store.tasks()
        self.platform_time("2026-10-18T09:00:00+08:00")
        self.assertEqual(self.store.tasks(), tasks)
        self.assertTrue(self.sync.info()["needs_sync"])

    def test_partial_attempt_is_exported_without_fabricating_success(self):
        self.store.set_value("collection_sync", {"last_attempt_at": "2026-10-18T08:00:00+08:00", "last_result": "partial",
            "attempt_scope": sync_signature(self.store.settings()["collection"])})
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()) as run:
            self.sync.sync()
        body = json.loads(run.call_args.kwargs["input"])
        self.assertIsNone(body["platform_synced_at"])
        self.assertEqual(body["platform_sync_attempted_at"], "2026-10-18T08:00:00+08:00")
        self.assertEqual(body["platform_sync_result"], "partial")
        self.assertFalse(self.sync.info()["needs_sync"])

    def test_slow_calendar_write_does_not_claim_newer_platform_time_was_exported(self):
        self.platform_time("2026-10-18T08:00:00+08:00")
        def run(*args, **kwargs):
            self.assertEqual(json.loads(kwargs["input"])["platform_synced_at"], "2026-10-18T08:00:00+08:00")
            self.platform_time("2026-10-18T09:00:00+08:00")
            return self.result()
        with patch("studyreminder.calendar_sync.subprocess.run", side_effect=run):
            self.sync.sync()
        self.assertTrue(self.sync.info()["needs_sync"])

    def test_calendar_failure_keeps_prior_success_time(self):
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()), patch("studyreminder.calendar_sync.now_iso", return_value="2026-10-18T08:00:00+08:00"):
            self.sync.sync()
        bad = SimpleNamespace(stdout='{"ok":false,"message":"离线"}', returncode=1)
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=bad), patch("studyreminder.calendar_sync.now_iso", return_value="2026-10-18T09:00:00+08:00"):
            self.sync.sync()
        self.assertEqual(self.sync.info()["last_success_at"], "2026-10-18T08:00:00+08:00")
        self.assertEqual(self.sync.info()["last_run"], "2026-10-18T09:00:00+08:00")
        self.assertFalse(self.sync.info()["ok"])

    def test_old_course_scope_time_is_not_exported_to_new_scope(self):
        self.platform_time("2026-10-18T08:00:00+08:00")
        self.store.patch_settings({"collection": {"course_scope": {"xiji": ["新课程"]}}})
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()) as run:
            self.sync.sync()
        self.assertIsNone(json.loads(run.call_args.kwargs["input"])["platform_synced_at"])

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

    def test_manual_exam_source_and_sitting_times_reach_native_bridge(self):
        item = self.store.put_task({"title": "大数竞", "course": "高数", "source": "manual", "kind": "exam",
            "start_at": "2026-11-14T09:00:00+08:00", "due_at": "2026-11-14T11:30:00+08:00"})
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()) as run:
            self.sync.sync()
        body = json.loads(run.call_args.kwargs["input"])
        actual = next(task for task in body["tasks"] if task["id"] == item["id"])
        self.assertEqual(actual["source"], "manual")
        self.assertEqual(actual["start_at"], "2026-11-14T09:00:00+08:00")
        self.assertEqual(actual["due_at"], "2026-11-14T11:30:00+08:00")

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

    def test_event_layout_policy_migrates_unchanged_tasks(self):
        tasks = self.store.tasks()
        with patch("studyreminder.calendar_sync.EVENT_LAYOUT_VERSION", 1):
            self.store.set_value("synced_signature", self.sync.signature(tasks))
        self.assertTrue(self.sync.info()["needs_sync"])
        with patch("studyreminder.calendar_sync.subprocess.run", return_value=self.result()):
            self.sync.sync()
        self.assertEqual(self.store.tasks(), tasks)
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


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "native calendar policy requires macOS Swift tools")
class NativeCalendarPolicyTests(unittest.TestCase):
    """Exercise the actual Swift planner without reading or writing calendars."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="keqi-calendar-policy-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.helper = Path(cls.temp.name) / "CalendarBridge"
        compiler = subprocess.run(["xcrun", "--find", "swiftc"], capture_output=True, text=True, check=True).stdout.strip()
        sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True, check=True).stdout.strip()
        source = Path(__file__).resolve().parents[1] / "native" / "CalendarBridge.swift"
        build = subprocess.run([compiler, "-parse-as-library", "-sdk", sdk, "-target", platform.machine() + "-apple-macosx12.0",
                                "-module-cache-path", str(Path(cls.temp.name) / "module-cache"),
                                "-framework", "AppKit", "-framework", "EventKit", str(source), "-o", str(cls.helper)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            raise RuntimeError("Swift calendar policy test build failed:\n" + build.stderr)

    def preview(self, tasks, ok=True):
        result = subprocess.run([str(self.helper), "--preview"], input=json.dumps({"tasks": tasks}),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["ok"], ok)
        return value

    def exam(self, **changes):
        return {"id": "math-contest", "title": "大数竞", "course": "高数", "source": "manual", "kind": "exam",
                "start_at": "2026-11-14T09:00:00+08:00", "due_at": "2026-11-14T11:30:00+08:00", **changes}

    def test_manual_exam_shows_real_sitting_without_moving_deadline_alerts(self):
        task = self.preview([self.exam()])["tasks"][0]
        self.assertEqual(task["event_start"], "2026-11-14T01:00:00Z")
        self.assertEqual(task["event_end"], "2026-11-14T03:30:00Z")
        self.assertEqual(task["reminder_at"], ["2026-11-12T03:30:00Z", "2026-11-13T03:30:00Z", "2026-11-14T01:30:00Z"])
        self.assertEqual(task["reminder_offsets"], [-163800, -77400, 1800])
        self.assertIn("截止时间：2026-11-14 11:30（北京时间）", task["notes"])
        self.assertIn("考试开始：2026-11-14 09:00（北京时间）", task["notes"])

    def test_platform_or_legacy_exam_keeps_deadline_placeholder(self):
        for source in ("chaoxing", "xiji", None):
            with self.subTest(source=source):
                item = self.exam(source=source)
                if source is None:
                    del item["source"]
                task = self.preview([item])["tasks"][0]
                self.assertEqual(task["event_start"], "2026-11-14T03:30:00Z")
                self.assertEqual(task["event_end"], "2026-11-14T03:45:00Z")
                self.assertEqual(task["reminder_offsets"], [-172800, -86400, -7200])

    def test_assignment_and_lecture_do_not_become_exam_intervals(self):
        for kind in ("assignment", "lecture"):
            with self.subTest(kind=kind):
                task = self.preview([self.exam(kind=kind)])["tasks"][0]
                self.assertEqual(task["event_start"], "2026-11-14T03:30:00Z")
                self.assertEqual(task["event_end"], "2026-11-14T03:45:00Z")
                self.assertEqual(task["reminder_offsets"], [-172800, -86400, -7200])

    def test_missing_or_nonpositive_exam_interval_keeps_deadline_placeholder(self):
        for start in (None, "", "2026-11-14T11:30:00+08:00", "2026-11-14T12:00:00+08:00"):
            with self.subTest(start=start):
                task = self.preview([self.exam(start_at=start)])["tasks"][0]
                self.assertEqual(task["event_start"], "2026-11-14T03:30:00Z")
                self.assertEqual(task["event_end"], "2026-11-14T03:45:00Z")
                self.assertEqual(task["reminder_offsets"], [-172800, -86400, -7200])

    def test_invalid_exam_start_fails_before_accessing_calendar(self):
        result = self.preview([self.exam(start_at="invalid")], ok=False)
        self.assertIn("考试开始时间格式无效", result["message"])


if __name__ == "__main__":
    unittest.main()
