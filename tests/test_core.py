import hashlib
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


def exam(**kw):
    return task(**{"title": "磁学", "course": "大学物理", "kind": "exam",
                   "start_at": "2026-10-17T18:00:00+08:00", "source_key": "course:266326215:exam:10922878", **kw})


def legacy_exam(**kw):
    item = exam(**kw)
    payload = [item["course"], " ".join(item["title"].split()), item.get("start_at")]
    item["source_key"] = "exam-list-fingerprint:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return item


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

    def test_ics_includes_real_platform_time_without_changing_task_update_time(self):
        item = self.store.put_task(task(notes="原备注;换行\n内容,保留"))
        original = dict(item)
        timestamp = "2026-10-18T08:02:00+08:00"
        text = to_ics([item], platform_synced_at=timestamp).replace("\r\n ", "")
        self.assertIn("平台最近成功同步：" + timestamp, text)
        self.assertIn("原备注\\;换行\\n内容\\,保留", text)
        self.assertEqual(item, original)
        self.assertNotIn("平台最近成功同步：", to_ics([item]))

    def test_exam_ics_uses_opening_and_preserves_closing_for_all_sources(self):
        for source in ("manual", "chaoxing", "xiji"):
            with self.subTest(source=source):
                item = self.store.put_task(exam(source=source, start_at="2026-11-14T09:00:00+08:00",
                                                due_at="2026-11-14T11:30:00+08:00"))
                before = dict(item)
                text = to_ics([item]).replace("\r\n ", "")
                self.assertIn("DTSTART:20261114T010000Z", text)
                self.assertIn("DTEND:20261114T033000Z", text)
                self.assertNotIn("DTSTART:20261114T033000Z", text)
                self.assertIn("考试开始：2026-11-14 09:00 北京时间", text)
                self.assertIn("考试结束：2026-11-14 11:30 北京时间", text)
                self.assertIn("提醒基准：考试开始", text)
                self.assertEqual(text.count("BEGIN:VALARM"), 3)
                for hours in (48, 24, 2):
                    self.assertIn(f"TRIGGER:-PT{hours}H", text)
                self.assertEqual(item, before)

    def test_exam_without_start_stays_pending_but_does_not_export_end_reminders(self):
        item = self.store.put_task(exam(start_at=None))
        assignment = self.store.put_task(task(source_key="work:other"))
        text = to_ics([item, assignment])
        self.assertNotIn("UID:" + item["id"], text)
        self.assertIn("UID:" + assignment["id"], text)
        self.assertEqual(text.count("BEGIN:VALARM"), 3)
        self.assertEqual(self.store.get_task(item["id"])["status"], "pending")
        self.assertIsNone(self.store.get_task(item["id"])["start_at"])

    def test_distinct_exams_with_same_start_keep_their_own_end_times(self):
        first = self.store.put_task(exam(source_key="course:1:exam:2",
                                        start_at="2026-11-14T09:00:00+08:00",
                                        due_at="2026-11-14T11:30:00+08:00"))
        second = self.store.put_task(exam(source_key="course:1:exam:3",
                                         start_at="2026-11-14T09:00:00+08:00",
                                         due_at="2026-11-14T12:30:00+08:00"))
        text = to_ics([first, second])
        self.assertEqual(text.count("DTSTART:20261114T010000Z"), 2)
        self.assertIn("DTEND:20261114T033000Z", text)
        self.assertIn("DTEND:20261114T043000Z", text)
        self.assertIn("UID:" + first["id"], text)
        self.assertIn("UID:" + second["id"], text)
        self.assertEqual(text.count("BEGIN:VALARM"), 6)
        self.assertEqual(self.store.get_task(first["id"])["due_at"], "2026-11-14T11:30:00+08:00")
        self.assertEqual(self.store.get_task(second["id"])["due_at"], "2026-11-14T12:30:00+08:00")

    def test_invalid_exam_start_in_raw_export_never_falls_back_to_deadline(self):
        item = self.store.put_task(exam())
        for value in ("invalid", "2026-10-17T18:00:00", "2026-10-19T18:00:00+08:00"):
            with self.subTest(start=value):
                self.assertNotIn("BEGIN:VEVENT", to_ics([{**item, "start_at": value}]))

    def test_task_order_uses_exam_opening_and_missing_starts_sort_last(self):
        assignment = self.store.put_task(task(source_key="order:assignment", due_at="2026-11-14T10:00:00+08:00"))
        known = self.store.put_task(exam(source_key="order:exam", start_at="2026-11-14T09:00:00+08:00",
                                        due_at="2026-11-14T11:30:00+08:00"))
        missing = self.store.put_task(exam(source_key="order:missing", start_at=None,
                                          due_at="2026-11-14T08:00:00+08:00"))
        ids = {item["id"] for item in (assignment, known, missing)}
        ordered = [item for item in self.store.tasks() if item["id"] in ids]
        self.assertEqual([item["id"] for item in ordered], [known["id"], assignment["id"], missing["id"]])
        self.assertEqual(self.store.get_task(known["id"])["due_at"], "2026-11-14T11:30:00+08:00")

    def test_zero_duration_exam_still_anchors_alarms_to_confirmed_start(self):
        item = self.store.put_task(exam(start_at="2026-11-14T09:00:00+08:00", due_at="2026-11-14T09:00:00+08:00"))
        text = to_ics([item])
        self.assertIn("DTSTART:20261114T010000Z", text)
        self.assertIn("DTEND:20261114T011500Z", text)

    def test_lecture_reminders_use_the_supplied_class_time_in_beijing(self):
        lecture = {"title": "Lecture 3（上课）", "course": "拔尖课程", "source": "manual", "kind": "lecture",
                   "due_at": "2026-10-31T20:00:00+08:00", "source_key": "lecture:ba-jian:3:2026-10-31"}
        item = self.store.import_tasks([lecture])[0]
        repeated = self.store.import_tasks([lecture])[0]
        self.assertEqual(repeated["id"], item["id"])
        self.assertIsNone(item["start_at"])
        text = to_ics([item]).replace("\r\n ", "")
        self.assertIn("DTSTART:20261031T120000Z", text)
        self.assertIn("上课时间：2026-10-31 20:00 北京时间", text)
        self.assertNotIn("考试开始", text)
        self.assertIn("不代表课长", text)
        self.assertEqual(text.count("BEGIN:VALARM"), 3)
        self.assertIn("TRIGGER:-PT48H", text)
        self.assertIn("TRIGGER:-PT24H", text)
        self.assertIn("TRIGGER:-PT2H", text)
        completed = self.store.put_task({"status": "completed"}, item["id"])
        self.assertNotIn("BEGIN:VEVENT", to_ics([completed]))

    def test_lecture_cannot_silently_remind_at_a_different_start_time(self):
        with self.assertRaisesRegex(ValueError, "开课时间一致"):
            self.store.put_task(task(kind="lecture", start_at="2026-10-17T18:00:00+08:00"))

    def test_partial_ics_attempt_does_not_claim_success(self):
        item = self.store.put_task(task())
        text = to_ics([item], platform_sync_attempted_at="2026-10-18T08:00:00+08:00", platform_sync_result="partial").replace("\r\n ", "")
        self.assertIn("平台最近尝试同步：2026-10-18T08:00:00+08:00", text)
        self.assertIn("平台同步结果：partial", text)
        self.assertNotIn("平台最近成功同步：", text)

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


class LegacyExamMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_legacy_pending_exam_upgrades_in_place_to_completed_canonical(self):
        old = self.store.put_task(legacy_exam(notes="我自己的复习备注"))
        imported = self.store.import_tasks([exam(status="completed", notes="平台已交卷")])[0]
        self.assertEqual(imported["id"], old["id"])
        self.assertEqual(imported["source_key"], exam()["source_key"])
        self.assertEqual(imported["status"], "completed")
        self.assertIn("我自己的复习备注", imported["notes"])
        self.assertIn("平台已交卷", imported["notes"])
        self.assertEqual(len(self.store.tasks()), 1)
        repeated = self.store.import_tasks([exam(status="pending", notes="平台已交卷")])[0]
        self.assertEqual(repeated["id"], old["id"])
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(repeated["notes"], imported["notes"])
        self.assertEqual(len(self.store.tasks()), 1)

    def test_completed_legacy_status_survives_pending_canonical_import(self):
        old = self.store.put_task(legacy_exam(status="completed"))
        result = self.store.import_tasks([exam()])[0]
        self.assertEqual((result["id"], result["status"]), (old["id"], "completed"))

    def test_existing_canonical_duplicate_merges_into_original_preserving_both_notes_and_completion(self):
        for completed_owner in ("legacy", "canonical"):
            with self.subTest(completed_owner=completed_owner), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "tasks.sqlite3")
                old = store.put_task(legacy_exam(status="completed" if completed_owner == "legacy" else "pending", notes="旧任务的用户备注"))
                duplicate = store.put_task(exam(status="completed" if completed_owner == "canonical" else "pending", notes="新任务的用户备注"))
                result = store.import_tasks([exam(notes="平台说明")])[0]
                self.assertEqual((result["id"], result["status"]), (old["id"], "completed"))
                for note in ("旧任务的用户备注", "新任务的用户备注", "平台说明"):
                    self.assertIn(note, result["notes"])
                self.assertEqual(len(store.tasks()), 1)
                with self.assertRaises(KeyError):
                    store.get_task(duplicate["id"])
                repeated = store.import_tasks([exam(notes="平台说明")])[0]
                self.assertEqual(repeated["notes"], result["notes"])

    def test_different_title_course_start_or_due_never_migrates(self):
        variants = ({"title": "磁学测验"}, {"title": "磁  学"}, {"course": "大物"},
                    {"start_at": "2026-10-17T19:00:00+08:00"}, {"due_at": "2026-10-19T00:00:00+08:00"})
        for change in variants:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "tasks.sqlite3")
                old = store.put_task(legacy_exam())
                result = store.import_tasks([{**exam(), **change, "status": "completed"}])[0]
                self.assertNotEqual(result["id"], old["id"])
                self.assertEqual(store.get_task(old["id"])["status"], "pending")
                self.assertEqual(len(store.tasks()), 2)

    def test_source_kind_missing_start_or_noncanonical_id_cannot_enable_migration(self):
        variants = ({"source": "xiji"}, {"kind": "assignment"}, {"start_at": None},
                    {"source_key": "course:266326215:exam:unknown"},
                    {"source_key": "course:266326215:exam:１０９２２８７８"})
        for change in variants:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "tasks.sqlite3")
                old_raw = {**legacy_exam(), **{k: v for k, v in change.items() if k != "source_key"}}
                old = store.put_task(old_raw)
                result = store.import_tasks([{**exam(), **change, "status": "completed"}])[0]
                self.assertNotEqual(result["id"], old["id"])
                self.assertEqual(len(store.tasks()), 2)
                self.assertEqual(store.get_task(old["id"])["status"], "pending")

    def test_collapsed_fingerprint_is_insufficient_without_exact_title_equality(self):
        old = self.store.put_task(legacy_exam(title="磁学  测验"))
        result = self.store.import_tasks([{**exam(), "title": "磁学 测验", "status": "completed"}])[0]
        self.assertNotEqual(result["id"], old["id"])
        self.assertEqual(len(self.store.tasks()), 2)

    def test_legacy_fingerprint_itself_must_match_recomputed_identity(self):
        old = self.store.put_task({**legacy_exam(), "source_key": "exam-list-fingerprint:" + "a" * 64})
        result = self.store.import_tasks([exam(status="completed")])[0]
        self.assertNotEqual(result["id"], old["id"])
        self.assertEqual(len(self.store.tasks()), 2)

    def test_multiple_canonical_keys_in_one_batch_refuse_migration_in_either_order(self):
        first, second = exam(status="completed"), {**exam(), "source_key": "course:266326215:exam:10922879", "status": "completed"}
        for incoming in ([first, second], [second, first]):
            with self.subTest(keys=[item["source_key"] for item in incoming]), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "tasks.sqlite3")
                old = store.put_task(legacy_exam())
                results = store.import_tasks(incoming)
                self.assertTrue(all(item["id"] != old["id"] for item in results))
                self.assertEqual(store.get_task(old["id"])["status"], "pending")
                self.assertEqual(len(store.tasks()), 3)

    def test_other_existing_canonical_for_same_fingerprint_refuses_migration_even_if_due_differs(self):
        old = self.store.put_task(legacy_exam())
        self.store.put_task({**exam(), "source_key": "course:266326215:exam:10922879", "due_at": "2026-10-19T00:00:00+08:00"})
        result = self.store.import_tasks([exam(status="completed")])[0]
        self.assertNotEqual(result["id"], old["id"])
        self.assertEqual(self.store.get_task(old["id"])["status"], "pending")
        self.assertEqual(len(self.store.tasks()), 3)

    def test_nonidentical_existing_canonical_duplicate_does_not_delete_original(self):
        old = self.store.put_task(legacy_exam())
        duplicate = self.store.put_task({**exam(), "due_at": "2026-10-19T00:00:00+08:00", "notes": "新任务用户备注"})
        result = self.store.import_tasks([exam(status="completed")])[0]
        self.assertEqual(result["id"], duplicate["id"])
        self.assertEqual(self.store.get_task(old["id"])["status"], "pending")
        self.assertIn("新任务用户备注", result["notes"])
        self.assertEqual(len(self.store.tasks()), 2)

    def test_over_limit_notes_refuse_destructive_merge_and_keep_both_user_notes(self):
        for completed_owner in ("legacy", "canonical", "incoming", None):
            with self.subTest(completed_owner=completed_owner), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "tasks.sqlite3")
                old = store.put_task(legacy_exam(status="completed" if completed_owner == "legacy" else "pending",
                                                 notes="甲" * 3500))
                duplicate = store.put_task(exam(status="completed" if completed_owner == "canonical" else "pending",
                                                notes="乙" * 3500))
                result = store.import_tasks([exam(status="completed" if completed_owner == "incoming" else "pending",
                                                  notes="平台说明")])[0]
                self.assertEqual(result["id"], duplicate["id"])
                original = store.get_task(old["id"])
                self.assertEqual(original["notes"], "甲" * 3500)
                self.assertIn("乙" * 3500, result["notes"])
                self.assertIn("平台说明", result["notes"])
                expected_status = "completed" if completed_owner else "pending"
                self.assertEqual((original["status"], result["status"]), (expected_status, expected_status))
                self.assertEqual(len(store.tasks()), 2)
                if completed_owner:
                    self.assertNotIn("BEGIN:VEVENT", to_ics(store.tasks()))
                repeated = store.import_tasks([exam(notes="平台说明")])[0]
                self.assertEqual(repeated, {**result, "updated_at": repeated["updated_at"]})

    def test_over_limit_notes_without_existing_canonical_still_cancel_original_reminder(self):
        old = self.store.put_task(legacy_exam(notes="甲" * 3500))
        result = self.store.import_tasks([exam(status="completed", notes="乙" * 3500)])[0]
        self.assertNotEqual(result["id"], old["id"])
        self.assertEqual(self.store.get_task(old["id"])["notes"], "甲" * 3500)
        self.assertEqual(result["notes"], "乙" * 3500)
        self.assertTrue(all(item["status"] == "completed" for item in self.store.tasks()))
        self.assertEqual(len(self.store.tasks()), 2)
        self.assertNotIn("BEGIN:VEVENT", to_ics(self.store.tasks()))

    def test_over_limit_notes_cannot_propagate_completion_across_conflicting_metadata(self):
        old = self.store.put_task(legacy_exam(notes="甲" * 3500))
        duplicate = self.store.put_task(exam(due_at="2026-10-19T00:00:00+08:00", notes="乙" * 3500))
        result = self.store.import_tasks([exam(status="completed", notes="平台说明")])[0]
        self.assertEqual(result["id"], duplicate["id"])
        self.assertEqual(self.store.get_task(old["id"])["status"], "pending")
        self.assertEqual(self.store.get_task(old["id"])["notes"], "甲" * 3500)
        self.assertIn("乙" * 3500, result["notes"])
        self.assertEqual(len(self.store.tasks()), 2)

    def test_identical_repeated_canonical_in_same_batch_is_not_ambiguous(self):
        old = self.store.put_task(legacy_exam())
        results = self.store.import_tasks([exam(), exam(status="completed")])
        self.assertEqual([item["id"] for item in results], [old["id"], old["id"]])
        self.assertEqual(self.store.get_task(old["id"])["status"], "completed")
        self.assertEqual(len(self.store.tasks()), 1)


if __name__ == "__main__":
    unittest.main()
