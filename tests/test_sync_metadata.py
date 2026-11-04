import itertools
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import Application
from studyreminder.core import TZ
from studyreminder.sync_status import platform_synced_at, sync_info


START = "2026-10-18T08:00:00+08:00"
FINISH = "2026-10-18T08:02:00+08:00"
LATER = "2026-10-18T08:10:00+08:00"


def outcome(chaoxing=True, xiji=True, *, complete=None):
    return {"candidates": [], "status_updates": [], "warnings": [],
            "coverage_complete": chaoxing and xiji if complete is None else complete,
            "source_results": {
                "chaoxing": {"coverage_complete": chaoxing, "auth_status": "authenticated"},
                "xiji": {"coverage_complete": xiji, "auth_status": "authenticated"}}}


class SyncMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = self.application()
        self.app.store.patch_settings({"collection": {"course_scope": {
            "chaoxing_homework": ["概率论"], "chaoxing_exams": [], "xiji": ["离散数学"]}}})

    def application(self):
        with patch("server.local_ip", return_value="127.0.0.1"):
            app = Application(self.temp.name)
        app.calendar = SimpleNamespace(sync_if_enabled=Mock(), info=Mock(return_value={}))
        app.collector = SimpleNamespace(request=Mock(return_value=outcome()), last_candidates=[],
                                        last_warnings=[], session_warning=None, status={})
        return app

    def tearDown(self):
        self.temp.cleanup()

    def scan(self, result=None, start=START, finish=FINISH, **kwargs):
        self.app.collector.request.return_value = result or outcome()
        with patch("server.now_iso", side_effect=itertools.chain([start], itertools.repeat(finish))):
            return self.app.scan(update_existing=True, import_new=True, **kwargs)

    def test_never_scanned_does_not_invent_time_from_task_or_login(self):
        self.app.store.put_task({"title": "手工作业", "due_at": "2026-10-21T09:50:00+08:00"})
        self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
        info = self.app.state()["collection"]["sync"]
        self.assertEqual(info["last_result"], "never")
        self.assertIsNone(info["last_attempt_at"])
        self.assertIsNone(info["last_success_at"])
        self.assertIsNone(platform_synced_at(self.app.store))

    def test_success_uses_finish_time_and_persists_across_restart(self):
        self.scan()
        self.app = self.application()
        info = self.app.sync_metadata()
        self.assertEqual(info["last_attempt_at"], START)
        self.assertEqual(info["last_success_at"], FINISH)
        self.assertEqual(info["sources"]["xiji"]["last_success_at"], FINISH)
        self.assertFalse(info["scope_changed"])
        self.assertEqual(platform_synced_at(self.app.store), FINISH)

    def test_authenticated_partial_read_does_not_update_global_or_failed_source_time(self):
        self.scan(outcome(xiji=False))
        info = self.app.sync_metadata()
        self.assertEqual(info["last_result"], "partial")
        self.assertIsNone(info["last_success_at"])
        self.assertEqual(info["sources"]["chaoxing"]["last_success_at"], FINISH)
        self.assertIsNone(info["sources"]["xiji"]["last_success_at"])

    def test_later_failure_preserves_verified_time_and_existing_tasks(self):
        self.scan()
        task = self.app.store.put_task({"title": "已有作业", "source": "xiji", "due_at": "2026-10-21T09:50:00+08:00"})
        self.app.collector.request.side_effect = RuntimeError("网络不可用")
        with patch("server.now_iso", return_value=LATER), self.assertRaises(RuntimeError):
            self.app.scan()
        info = self.app.sync_metadata()
        self.assertEqual(info["last_success_at"], FINISH)
        self.assertEqual(info["last_attempt_at"], LATER)
        self.assertEqual(info["last_result"], "failed")
        self.assertEqual(self.app.store.get_task(task["id"])["status"], "pending")

    def test_automatic_restore_failure_is_an_attempt_without_a_success(self):
        self.app.collector.request.side_effect = RuntimeError("会话恢复失败")
        with patch("server.now_iso", return_value=FINISH):
            self.app.automatic_collection(datetime(2026, 10, 18, 8, tzinfo=TZ))
        info = self.app.sync_metadata()
        self.assertEqual(info["last_attempt_at"], FINISH)
        self.assertEqual(info["sources"]["xiji"]["last_result"], "failed")
        self.assertIsNone(info["last_success_at"])

    def test_skipped_source_is_not_marked_as_verified(self):
        result = outcome()
        result["source_results"]["chaoxing"]["auth_status"] = "disabled"
        self.scan(result, sources={"xiji"})
        info = self.app.sync_metadata()
        self.assertEqual(info["sources"]["xiji"]["last_success_at"], FINISH)
        self.assertIsNone(info["sources"]["chaoxing"]["last_attempt_at"])
        self.assertIsNone(info["last_success_at"])

    def test_single_configured_source_can_complete_global_scope(self):
        self.app.store.patch_settings({"collection": {"course_scope": {"chaoxing_homework": ["概率论"], "chaoxing_exams": [], "xiji": []}}})
        result = outcome()
        result["source_results"]["xiji"]["auth_status"] = "disabled"
        self.scan(result)
        info = self.app.sync_metadata()
        self.assertEqual(info["last_success_at"], FINISH)
        self.assertEqual(info["sources"]["xiji"]["last_result"], "disabled")
        self.assertIsNone(info["sources"]["xiji"]["last_success_at"])

    def test_no_configured_courses_cannot_produce_success(self):
        self.app.store.patch_settings({"collection": {"course_scope": {
            "chaoxing_homework": [], "chaoxing_exams": [], "xiji": []}}})
        self.scan()
        info = self.app.sync_metadata()
        self.assertIsNone(info["last_attempt_at"])
        self.assertIsNone(info["last_success_at"])

    def test_configuration_change_keeps_history_but_marks_it_stale(self):
        self.scan()
        self.app.store.patch_settings({"collection": {"course_scope": {"chaoxing_homework": ["概率论"], "chaoxing_exams": [], "xiji": ["新课程"]}}})
        info = self.app.sync_metadata()
        self.assertEqual(info["last_success_at"], FINISH)
        self.assertTrue(info["scope_changed"])
        self.assertTrue(info["sources"]["xiji"]["scope_changed"])
        self.assertFalse(info["sources"]["chaoxing"]["scope_changed"])
        self.assertIsNone(platform_synced_at(self.app.store))

    def test_year_change_invalidates_chaoxing_and_global_but_not_xiji(self):
        self.scan()
        self.app.store.patch_settings({"collection": {"homework_year": 2026}})
        info = self.app.sync_metadata()
        self.assertTrue(info["scope_changed"])
        self.assertTrue(info["sources"]["chaoxing"]["scope_changed"])
        self.assertFalse(info["sources"]["xiji"]["scope_changed"])

    def test_scope_changed_during_read_does_not_record_success(self):
        def request(*args):
            self.app.store.patch_settings({"collection": {"course_scope": {"chaoxing_homework": ["概率论"], "chaoxing_exams": [], "xiji": ["新课程"]}}})
            return outcome()
        self.app.collector.request.side_effect = request
        with self.assertRaises(ValueError):
            self.app.scan()
        info = self.app.sync_metadata()
        self.assertEqual(info["last_result"], "scope_changed")
        self.assertTrue(info["scope_changed"])
        self.assertIsNone(info["last_success_at"])

    def test_session_replaced_during_read_does_not_mark_source_success(self):
        def request(*args):
            self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
            return outcome()
        self.app.collector.request.side_effect = request
        self.app.scan()
        info = self.app.sync_metadata()
        self.assertIsNone(info["last_success_at"])
        self.assertIsNone(info["sources"]["xiji"]["last_success_at"])
        self.assertIsNotNone(info["sources"]["chaoxing"]["last_success_at"])

    def test_coverage_without_authenticated_or_with_deferral_is_not_success(self):
        for auth_status, deferred in (("unverified", None), ("disabled", None), ("authenticated", "session_changed_during_scan")):
            result = outcome()
            result["source_results"]["xiji"].update(auth_status=auth_status, deferred_reason=deferred)
            self.scan(result)
            self.assertIsNone(self.app.sync_metadata()["sources"]["xiji"]["last_success_at"])
            self.assertIsNone(self.app.sync_metadata()["last_success_at"])

    def test_daily_accumulation_records_actual_finish_not_scheduler_start(self):
        scheduled = datetime(2026, 10, 18, 8, tzinfo=TZ)
        self.app.collector.request.return_value = outcome(xiji=False)
        with patch("server.now_iso", return_value=FINISH):
            self.app.automatic_collection(scheduled)
        self.assertIsNone(self.app.sync_metadata()["last_success_at"])
        self.app.collector.request.return_value = outcome(chaoxing=False)
        with patch("server.now_iso", return_value=LATER):
            self.app.automatic_collection(scheduled + timedelta(minutes=5))
        info = self.app.sync_metadata()
        self.assertEqual(info["last_success_at"], LATER)
        self.assertEqual(info["last_result"], "success")
        self.assertEqual(info["sources"]["chaoxing"]["last_success_at"], FINISH)
        self.assertEqual(info["sources"]["xiji"]["last_success_at"], LATER)

    def test_unchanged_task_read_sends_persisted_freshness_to_calendar(self):
        observed = []
        self.app.calendar.sync_if_enabled.side_effect = lambda: observed.append(platform_synced_at(self.app.store))
        self.scan()
        self.assertEqual(observed, [FINISH])

    def test_complete_historical_result_is_marked_unknown_scope(self):
        self.app.store.set_value("last_collection_scan", FINISH)
        self.app.store.set_value("last_collection_result", outcome())
        info = self.app.sync_metadata()
        self.assertEqual(info["last_success_at"], FINISH)
        self.assertTrue(info["scope_changed"])
        self.assertIsNone(platform_synced_at(self.app.store))

    def test_partial_historical_result_only_migrates_verified_source(self):
        self.app.store.set_value("last_collection_scan", FINISH)
        self.app.store.set_value("last_collection_result", outcome(xiji=False))
        info = self.app.sync_metadata()
        self.assertIsNone(info["last_success_at"])
        self.assertEqual(info["sources"]["chaoxing"]["last_success_at"], FINISH)
        self.assertIsNone(info["sources"]["xiji"]["last_success_at"])

    def test_historical_omitted_source_does_not_look_like_full_scope_success(self):
        result = outcome()
        result["source_results"]["chaoxing"]["auth_status"] = "disabled"
        self.app.store.set_value("last_collection_scan", FINISH)
        self.app.store.set_value("last_collection_result", result)
        info = self.app.sync_metadata()
        self.assertIsNone(info["last_success_at"])
        self.assertEqual(info["sources"]["xiji"]["last_success_at"], FINISH)

    def test_auth_repair_does_not_erase_or_advance_verified_times(self):
        self.scan()
        with patch("server.now_iso", return_value=LATER):
            self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
        info = self.app.sync_metadata()
        self.assertEqual(info["last_attempt_at"], START)
        self.assertEqual(info["last_success_at"], FINISH)

    def test_disabled_historical_read_and_legacy_daily_time_do_not_invent_success(self):
        result = outcome()
        for source in result["source_results"].values():
            source["auth_status"] = "disabled"
        self.app.store.set_value("last_collection_scan", FINISH)
        self.app.store.set_value("last_collection_result", result)
        self.app.store.set_value("daily_last_success", LATER)
        info = self.app.sync_metadata()
        self.assertIsNone(info["last_attempt_at"])
        self.assertIsNone(info["last_success_at"])

    def test_restart_marks_an_unfinished_scan_interrupted(self):
        with patch("server.now_iso", return_value=START):
            self.app.begin_sync()
        self.assertEqual(self.app.sync_metadata()["last_result"], "running")
        self.app = self.application()
        info = self.app.sync_metadata()
        self.assertEqual(info["last_result"], "interrupted")
        self.assertEqual(info["last_attempt_at"], START)
        self.assertIsNone(info["last_success_at"])


if __name__ == "__main__":
    unittest.main()
