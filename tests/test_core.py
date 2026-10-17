import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from studyreminder.core import DEFAULT_SETTINGS, Store, TZ, parse_datetime, physics_occurrences, to_ics
from studyreminder.collector import candidates_from_blocks, platform_for


def task(**kw):
    return {"title": "第一章作业", "course": "测试课程", "source": "chaoxing", "kind": "assignment",
            "due_at": "2026-10-18T00:00:00+08:00", **kw}


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "tasks.sqlite3")
        self.store.update_settings({"physics": {"enabled": True}})

    def tearDown(self):
        self.temp.cleanup()

    def test_saturday_24_means_sunday_midnight_and_alarm_days(self):
        values = physics_occurrences(self.store.settings(), datetime(2026, 10, 16, 12, tzinfo=TZ))
        due = parse_datetime(values[0]["due_at"])
        self.assertEqual(due, datetime(2026, 10, 18, 0, tzinfo=TZ))
        self.assertEqual(due - timedelta(hours=48), datetime(2026, 10, 16, 0, tzinfo=TZ))
        self.assertEqual(due - timedelta(hours=24), datetime(2026, 10, 17, 0, tzinfo=TZ))
        self.assertEqual(due - timedelta(hours=2), datetime(2026, 10, 17, 22, tzinfo=TZ))

    def test_sunday_after_midnight_targets_next_week(self):
        values = physics_occurrences(self.store.settings(), datetime(2026, 10, 18, 0, 1, tzinfo=TZ))
        self.assertEqual(values[0]["due_at"], "2026-10-25T00:00:00+08:00")

    def test_reimport_stable_identity_updates_ddl_without_duplicate(self):
        first = self.store.import_tasks([task(source_key="course1:work9")])[0]
        second = self.store.import_tasks([task(source_key="course1:work9", due_at="2026-10-20T00:00:00+08:00")])[0]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len([t for t in self.store.tasks() if t["source"] == "chaoxing"]), 1)

    def test_stale_import_does_not_reopen_completed_task(self):
        first = self.store.import_tasks([task(source_key="course1:work9")])[0]
        self.store.put_task({"status": "completed"}, first["id"])
        result = self.store.import_tasks([task(source_key="course1:work9")])[0]
        self.assertEqual(result["status"], "completed")

    def test_import_is_atomic_on_invalid_later_record(self):
        with self.assertRaises(ValueError):
            self.store.import_tasks([task(), task(due_at="2026-10-18T00:00:00")])
        self.assertEqual(len([t for t in self.store.tasks() if t["source"] == "chaoxing"]), 0)

    def test_physics_deleted_week_does_not_resurrect(self):
        first = next(t for t in self.store.tasks() if t["source"] == "physics")
        self.store.delete_task(first["id"])
        result = self.store.get_task(first["id"])
        self.assertEqual(result["status"], "completed")
        self.store.tasks()
        self.assertEqual(self.store.get_task(first["id"])["status"], "completed")

    def test_export_reimport_does_not_duplicate_weekly_tasks(self):
        before = self.store.tasks()
        self.store.import_tasks(json.loads(json.dumps(before)))
        after = self.store.tasks()
        self.assertEqual({t["id"] for t in before}, {t["id"] for t in after})

    def test_ics_has_three_alarms_utc_and_octet_folding(self):
        item = self.store.put_task(task(title="物理作业中文标题" * 15, notes="行一\n行二;含逗号,"))
        text = to_ics([item])
        self.assertIn("DTSTART:20261017T160000Z", text)
        self.assertEqual(text.count("BEGIN:VALARM"), 3)
        self.assertIn("TRIGGER:-PT48H", text)
        self.assertIn("TRIGGER:-PT24H", text)
        self.assertIn("TRIGGER:-PT2H", text)
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split("\r\n")))
        completed_text = to_ics([{**item, "status": "completed"}])
        self.assertNotIn("BEGIN:VEVENT", completed_text)
        self.assertNotIn("BEGIN:VALARM", completed_text)

    def test_bad_url_and_inverted_exam_range_rejected(self):
        with self.assertRaises(ValueError):
            self.store.put_task(task(url="javascript:alert(1)"))
        with self.assertRaises(ValueError):
            self.store.put_task(task(start_at="2026-10-19T00:00:00+08:00"))

    def test_browser_candidate_requires_label_and_full_year(self):
        blocks = [{"text": "第一章作业\n截止时间：2026-10-18 00:00", "title": "第一章作业"},
                  {"text": "第二章 剩余1天", "title": "第二章"},
                  {"text": "第三章 截止时间：10-20 23:00", "title": "第三章"}]
        found = candidates_from_blocks(blocks, "chaoxing", "https://i.chaoxing.com/")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["due_at"], "2026-10-18T00:00:00+08:00")

    def test_platform_hosts_cannot_be_spoofed(self):
        self.assertIsNone(platform_for("https://chaoxing.com.evil.example/"))
        self.assertEqual(platform_for("https://mooc1.chaoxing.com/"), "chaoxing")
        self.assertEqual(platform_for("https://cslabcg.whu.edu.cn/"), "xiji")


if __name__ == "__main__":
    unittest.main()
