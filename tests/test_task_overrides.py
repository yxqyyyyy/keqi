import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from studyreminder.core import Store, TaskConflictError


def platform_task(**changes):
    return {"title": "第一章作业", "course": "计算机组成", "source": "xiji",
            "source_key": "course:1:work:9", "kind": "assignment", "start_at": None,
            "due_at": "2026-12-20T23:00:00+08:00", "status": "pending",
            "url": "https://example.edu/work/9", "notes": "平台备注", **changes}


class TemporaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()


class TaskOverrideTests(TemporaryStoreTests):
    def test_user_fields_survive_platform_changes_and_reopening_store(self):
        original = self.store.import_tasks([platform_task()])[0]
        changes = {"title": "复习测验", "course": "自定课程", "kind": "exam",
                   "start_at": "2026-12-21T09:00:00+08:00", "due_at": "2026-12-21T11:00:00+08:00",
                   "status": "completed", "url": "https://example.edu/mine", "notes": "我的备注"}
        updated = self.store.apply_task_patch(original["id"], changes, original["revision"])
        self.store = Store(self.path)
        imported = self.store.import_tasks([platform_task(title="新平台标题", course="平台课程", notes="新备注")])[0]
        for key, value in changes.items():
            self.assertEqual(imported[key], value, key)
        self.assertEqual(imported["revision"], updated["revision"])
        self.assertEqual(imported["source_key"], original["source_key"])
        self.assertNotIn("manual_overrides", imported)
        self.assertNotIn("overrides", imported)

    def test_automatic_import_only_updates_fields_without_manual_override(self):
        original = self.store.import_tasks([platform_task()])[0]
        self.store.apply_task_patch(original["id"], {"title": "我的标题"})
        updated = self.store.import_tasks([platform_task(title="平台标题", notes="新增说明")])[0]
        self.assertEqual(updated["title"], "我的标题")
        self.assertEqual(updated["notes"], "新增说明")
        updated_again = self.store.import_tasks([platform_task(title="再次改变", notes="再次说明")])[0]
        self.assertEqual(updated_again["notes"], "再次说明")

    def test_completed_platform_status_cannot_be_reopened_by_stale_import(self):
        original = self.store.import_tasks([platform_task()])[0]
        self.store.put_task({"status": "completed"}, original["id"], mark_overrides=False)
        self.assertEqual(self.store.import_tasks([platform_task()])[0]["status"], "completed")

    def test_positive_submission_cancels_a_manually_reopened_task(self):
        original = self.store.import_tasks([platform_task(status="completed")])[0]
        self.store.apply_task_patch(original["id"], {"status": "pending"})
        result = self.store.import_tasks([platform_task(status="completed")])[0]
        self.assertEqual(result["status"], "completed")

    def test_edit_of_old_fallback_identity_does_not_create_duplicate(self):
        original = self.store.import_tasks([platform_task(source_key="")])[0]
        self.store.apply_task_patch(original["id"], {"title": "我的标题", "course": "我的课程"})
        result = self.store.import_tasks([platform_task(source_key="")])[0]
        self.assertEqual(result["id"], original["id"])
        self.assertEqual(len(self.store.tasks()), 1)

    def test_revisions_change_only_for_semantic_changes(self):
        first = self.store.import_tasks([platform_task()])[0]
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.store.import_tasks([platform_task()])[0], first)
        self.assertEqual(self.store.apply_task_patch(first["id"], {"title": first["title"]}), first)
        changed = self.store.import_tasks([platform_task(notes="新增说明")])[0]
        self.assertEqual(changed["revision"], 2)
        edited = self.store.put_task({"notes": "我的说明"}, first["id"])
        self.assertEqual(edited["revision"], 3)
        self.assertEqual(self.store.import_tasks([platform_task(notes="其他说明")])[0], edited)

    def test_legacy_body_gets_revision_without_changing_its_content(self):
        item = self.store.put_task(platform_task())
        with self.store.connect() as db:
            original_body = db.execute("SELECT body FROM tasks WHERE id=?", (item["id"],)).fetchone()[0]
            db.execute("DELETE FROM task_state WHERE id=?", (item["id"],))
        reopened = Store(self.path)
        self.assertEqual(reopened.get_task(item["id"])["revision"], 1)
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT body FROM tasks WHERE id=?", (item["id"],)).fetchone()[0], original_body)

    def test_stale_record_patch_conflicts_and_exposes_safe_current_record(self):
        item = self.store.import_tasks([platform_task()])[0]
        latest = self.store.put_task({"title": "Mac 修改"}, item["id"])
        with self.assertRaises(TaskConflictError) as caught:
            self.store.apply_task_patch(item["id"], {"notes": "手机修改"}, item["revision"])
        self.assertEqual(caught.exception.current_task, latest)
        self.assertEqual(self.store.get_task(item["id"]), latest)

    def test_disjoint_changes_merge_with_field_bases_despite_stale_revision(self):
        item = self.store.import_tasks([platform_task()])[0]
        self.store.put_task({"title": "Mac 修改"}, item["id"])
        merged = self.store.apply_task_patch(item["id"], {"notes": "手机修改"}, item["revision"],
                                             base_fields={"notes": item["notes"]})
        self.assertEqual(merged["title"], "Mac 修改")
        self.assertEqual(merged["notes"], "手机修改")
        self.assertEqual(merged["revision"], 3)
        self.assertEqual(self.store.apply_task_patch(item["id"], {"notes": "手机修改"}, item["revision"],
                                                    base_fields={"notes": item["notes"]}), merged)

    def test_same_field_divergence_rejects_entire_patch(self):
        item = self.store.import_tasks([platform_task()])[0]
        latest = self.store.put_task({"notes": "Mac 修改"}, item["id"])
        with self.assertRaises(TaskConflictError):
            self.store.apply_task_patch(item["id"], {"notes": "手机修改", "title": "新标题"}, item["revision"],
                                        base_fields={"notes": item["notes"], "title": item["title"]})
        self.assertEqual(self.store.get_task(item["id"]), latest)

    def test_phone_edit_matching_latest_source_still_preserves_user_intent(self):
        item = self.store.import_tasks([platform_task()])[0]
        self.store.import_tasks([platform_task(title="共同的新标题")])
        merged = self.store.apply_task_patch(item["id"], {"title": "共同的新标题"}, item["revision"],
                                             base_fields={"title": item["title"]})
        result = self.store.import_tasks([platform_task(title="之后的平台标题")])[0]
        self.assertEqual(result, merged)

    def test_parallel_stale_patches_cannot_both_overwrite(self):
        item = self.store.import_tasks([platform_task()])[0]

        def patch(title):
            try:
                self.store.apply_task_patch(item["id"], {"title": title}, item["revision"])
                return "applied"
            except TaskConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(patch, ["手机修改", "Mac 修改"]))
        self.assertCountEqual(results, ["applied", "conflict"])
        self.assertEqual(self.store.get_task(item["id"])["revision"], 2)

    def test_patch_cannot_change_source_metadata_or_internal_fields(self):
        item = self.store.import_tasks([platform_task()])[0]
        for field in ("source", "source_key", "id", "revision", "deleted", "overrides"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.apply_task_patch(item["id"], {field: "changed"})
        self.assertEqual(self.store.get_task(item["id"]), item)

    def test_deleted_platform_item_stays_hidden_after_import_and_restart(self):
        item = self.store.import_tasks([platform_task()])[0]
        deleted = self.store.delete_task(item["id"], item["revision"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["status"], "completed")
        self.assertEqual(deleted["revision"], 2)
        self.assertEqual(self.store.delete_task(item["id"]), deleted)
        self.store = Store(self.path)
        imported = self.store.import_tasks([platform_task(title="更新标题")])[0]
        self.assertEqual(imported, deleted)
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.tasks(include_deleted=True), [deleted])
        with self.assertRaises(TaskConflictError):
            self.store.apply_task_patch(item["id"], {"status": "pending"})

    def test_weekly_tombstone_survives_materialization_and_setting_changes(self):
        self.store.update_settings({"physics": {"enabled": True}})
        item = self.store.tasks()[0]
        self.store.delete_task(item["id"])
        self.store.update_settings({"physics": {"time": "23:00"}})
        self.assertNotIn(item["id"], {task["id"] for task in self.store.tasks()})
        self.assertTrue(self.store.get_task(item["id"])["deleted"])

    def test_weekly_revision_and_edits_survive_configuration_refresh(self):
        self.store.update_settings({"physics": {"enabled": True}})
        item = self.store.tasks()[0]
        edited = self.store.apply_task_patch(item["id"], {"title": "线下习题"})
        self.store.update_settings({"physics": {"time": "23:00"}})
        refreshed = self.store.get_task(item["id"])
        self.assertEqual(refreshed["title"], "线下习题")
        self.assertGreater(refreshed["revision"], edited["revision"])
        self.assertIn("T23:00:00", refreshed["due_at"])
        self.store.update_settings({"physics": {"time": "23:00"}})
        self.assertEqual(self.store.get_task(item["id"]), refreshed)
        self.store.update_settings({"physics": {"enabled": False}})
        self.store.update_settings({"physics": {"enabled": True}})
        self.assertGreater(self.store.get_task(item["id"])["revision"], refreshed["revision"])

    def test_disabled_weekly_lecture_keeps_valid_schedule_on_recreation(self):
        self.store.update_settings({"physics": {"enabled": True}})
        item = self.store.tasks()[0]
        edited = self.store.apply_task_patch(item["id"], {"kind": "lecture", "start_at": item["due_at"]})
        self.store.update_settings({"physics": {"enabled": False}})
        self.store.update_settings({"physics": {"time": "23:00"}})
        self.store.update_settings({"physics": {"enabled": True}})
        restored = self.store.get_task(item["id"])
        self.assertEqual(restored["kind"], "lecture")
        self.assertEqual(restored["start_at"], edited["start_at"])
        self.assertEqual(restored["due_at"], edited["due_at"])

    def test_automatic_put_can_avoid_setting_manual_override(self):
        item = self.store.import_tasks([platform_task()])[0]
        self.store.put_task({"notes": "自动信息"}, item["id"], mark_overrides=False)
        self.assertEqual(self.store.import_tasks([platform_task(notes="新平台信息")])[0]["notes"], "新平台信息")

    def test_source_time_change_cannot_break_manually_changed_lecture(self):
        item = self.store.import_tasks([platform_task()])[0]
        edited = self.store.apply_task_patch(item["id"], {"kind": "lecture", "start_at": item["due_at"]})
        result = self.store.import_tasks([platform_task(due_at="2026-12-21T23:00:00+08:00", notes="新增说明")])[0]
        self.assertEqual(result["start_at"], edited["start_at"])
        self.assertEqual(result["due_at"], edited["due_at"])
        self.assertEqual(result["kind"], "lecture")
        self.assertEqual(result["notes"], "新增说明")

    def test_source_start_change_cannot_break_personal_exam_end_time(self):
        item = self.store.import_tasks([platform_task(kind="exam", start_at="2026-12-20T09:00:00+08:00")])[0]
        edited = self.store.apply_task_patch(item["id"], {"due_at": "2026-12-20T10:00:00+08:00"})
        result = self.store.import_tasks([platform_task(kind="exam", start_at="2026-12-20T11:00:00+08:00")])[0]
        self.assertEqual(result["start_at"], edited["start_at"])
        self.assertEqual(result["due_at"], edited["due_at"])


class RemoteOperationTests(TemporaryStoreTests):
    def operation(self, item, **changes):
        return {"op_id": "operation-1", "action": "patch", "task_id": item["id"],
                "base_revision": item["revision"], "base": {"notes": item["notes"]},
                "changes": {"notes": "手机修改"}, **changes}

    def test_repeated_operation_returns_receipt_without_reapplying(self):
        item = self.store.import_tasks([platform_task()])[0]
        operation = self.operation(item)
        receipt = self.store.apply_remote_operation(operation)
        self.assertEqual(receipt["status"], "applied")
        self.store.put_task({"title": "之后的 Mac 修改"}, item["id"])
        latest = self.store.get_task(item["id"])
        self.store = Store(self.path)
        self.assertEqual(self.store.apply_remote_operation(operation), receipt)
        self.assertEqual(self.store.apply_remote_operation({**operation, "changes": {"notes": "被替换的内容"}}), receipt)
        self.assertEqual(self.store.get_task(item["id"]), latest)
        self.assertEqual(self.store.remote_receipts(), [receipt])

    def test_remote_create_uses_phone_identity_and_manual_source(self):
        operation = {"op_id": "create-1", "action": "create", "task_id": "phone-task-1",
                     "changes": {"title": "补充安排", "due_at": "2026-12-25T20:00:00+08:00"}}
        receipt = self.store.apply_remote_operation(operation)
        self.assertEqual(receipt["status"], "applied")
        self.assertEqual(receipt["current"]["id"], operation["task_id"])
        self.assertEqual(receipt["current"]["source"], "manual")
        self.assertEqual(self.store.apply_remote_operation(operation), receipt)
        self.assertEqual(len(self.store.tasks()), 1)

    def test_remote_conflict_is_durable_and_does_not_overwrite_newer_edit(self):
        item = self.store.import_tasks([platform_task()])[0]
        latest = self.store.put_task({"notes": "Mac 修改"}, item["id"])
        operation = self.operation(item)
        receipt = self.store.apply_remote_operation(operation)
        self.assertEqual(receipt["status"], "conflict")
        self.assertEqual(receipt["current"], latest)
        self.assertEqual(self.store.get_task(item["id"]), latest)
        self.assertEqual(self.store.remote_receipts(), [receipt])

    def test_remote_delete_conflicts_with_newer_record_then_tombstones(self):
        item = self.store.import_tasks([platform_task()])[0]
        latest = self.store.put_task({"notes": "Mac 修改"}, item["id"])
        operation = self.operation(item, action="delete")
        self.assertEqual(self.store.apply_remote_operation(operation)["status"], "conflict")
        receipt = self.store.apply_remote_operation({**operation, "op_id": "delete-2", "base_revision": latest["revision"]})
        self.assertEqual(receipt["status"], "applied")
        self.assertTrue(receipt["current"]["deleted"])
        self.assertEqual(self.store.tasks(), [])

    def test_invalid_remote_operations_are_receipted_without_partial_write(self):
        item = self.store.import_tasks([platform_task()])[0]
        receipt = self.store.apply_remote_operation(self.operation(item, changes={"source_key": "injected"}))
        self.assertEqual(receipt["status"], "invalid")
        self.assertEqual(self.store.get_task(item["id"]), item)
        self.assertEqual(self.store.remote_receipts(), [receipt])
        missing = self.store.apply_remote_operation(self.operation(item, op_id="missing", task_id="unknown"))
        self.assertEqual(missing["status"], "conflict")
        self.assertIsNone(missing["current"])

    def test_malformed_remote_values_return_invalid_receipts(self):
        item = self.store.import_tasks([platform_task()])[0]
        for index, changes in enumerate(({"action": []}, {"changes": {"kind": []}},
                                          {"changes": {"status": []}}, {"changes": None})):
            with self.subTest(changes=changes):
                receipt = self.store.apply_remote_operation(self.operation(item, op_id=f"invalid-{index}", **changes))
                self.assertEqual(receipt["status"], "invalid")
                self.assertEqual(self.store.get_task(item["id"]), item)

    def test_receipt_insertion_failure_rolls_back_task_modification(self):
        item = self.store.import_tasks([platform_task()])[0]
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON cloud_receipts BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.apply_remote_operation(self.operation(item))
        self.assertEqual(self.store.get_task(item["id"]), item)
        self.assertEqual(self.store.remote_receipts(), [])

    def test_partial_task_write_failure_keeps_only_invalid_receipt(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_state BEFORE INSERT ON task_state BEGIN SELECT RAISE(ABORT, 'test'); END")
        receipt = self.store.apply_remote_operation({
            "op_id": "create-failing", "action": "create", "task_id": "new-phone-task",
            "changes": {"title": "安排", "due_at": "2026-12-25T20:00:00+08:00"},
        })
        self.assertEqual(receipt["status"], "invalid")
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.remote_receipts(), [receipt])


if __name__ == "__main__":
    unittest.main()
