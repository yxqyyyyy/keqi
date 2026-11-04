import io
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import Application, Handler
from studyreminder.core import TZ


def outcome(chaoxing=False, xiji=False, xiji_auth="authenticated"):
    return {"candidates": [], "warnings": [], "status_updates": [],
            "coverage_complete": chaoxing and xiji,
            "source_results": {
                "chaoxing": {"coverage_complete": chaoxing, "auth_status": "authenticated"},
                "xiji": {"coverage_complete": xiji, "auth_status": xiji_auth}}}


class BackgroundRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = self.application()
        self.now = datetime(2026, 10, 18, 8, tzinfo=TZ)
        self.app.store.patch_settings({"collection": {"course_scope": {
            "chaoxing_homework": ["概率论"], "chaoxing_exams": [], "xiji": ["离散数学"]}}})

    def application(self):
        with patch("server.local_ip", return_value="127.0.0.1"):
            app = Application(self.temp.name)
        app.calendar.sync_if_enabled = Mock()
        app.collector = SimpleNamespace(request=Mock(return_value=outcome()))
        return app

    def tearDown(self):
        self.temp.cleanup()

    def scans(self):
        return [call for call in self.app.collector.request.call_args_list if call.args[0] == "scan"]

    def test_daily_failure_retries_after_five_then_thirty_minutes_only_three_times(self):
        for minutes in (0, 1, 4, 5, 6, 34, 35, 36, 120, 900):
            self.app.automatic_collection(self.now + timedelta(minutes=minutes))
        self.assertEqual(len(self.scans()), 3)
        self.assertIsNone(self.app.store.get_value("daily_last_success"))
        state = self.app.store.get_value("collection_retry")
        self.assertEqual(state["daily"]["attempts"], 3)
        self.assertIsNotNone(self.app.store.get_value("daily_last_attempt"))

    def test_service_restart_preserves_attempt_budget(self):
        self.app.automatic_collection(self.now)
        self.app = self.application()
        self.app.automatic_collection(self.now + timedelta(minutes=1))
        self.assertEqual(len(self.scans()), 0)
        self.app.automatic_collection(self.now + timedelta(minutes=5))
        self.assertEqual(len(self.scans()), 1)
        self.assertEqual(self.app.store.get_value("collection_retry")["daily"]["attempts"], 2)

    def test_failed_network_call_consumes_attempt_and_keeps_existing_tasks(self):
        task = self.app.store.put_task({"title": "已有作业", "source": "xiji", "due_at": "2026-10-21T09:50:00+08:00"})
        self.app.collector.request.side_effect = RuntimeError("网络不可用")
        result = self.app.automatic_collection(self.now)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(self.app.store.get_value("collection_retry")["daily"]["attempts"], 1)
        self.assertEqual(self.app.store.get_task(task["id"])["status"], "pending")
        self.assertIsNone(self.app.store.get_value("daily_last_success"))

    def test_next_day_has_new_daily_budget(self):
        for minutes in (0, 5, 35):
            self.app.automatic_collection(self.now + timedelta(minutes=minutes))
        self.app.automatic_collection(self.now + timedelta(days=1))
        self.assertEqual(len(self.scans()), 4)
        self.assertEqual(self.app.store.get_value("collection_retry")["daily"]["attempts"], 1)

    def test_completed_platform_is_skipped_while_other_platform_retries(self):
        self.app.collector.request.return_value = outcome(chaoxing=True)
        self.app.automatic_collection(self.now)
        self.app.automatic_collection(self.now + timedelta(minutes=5))
        second = self.scans()[1].args[1]
        self.assertEqual(second["course_scope"]["chaoxing_homework"], [])
        self.assertEqual(second["course_scope"]["xiji"], ["离散数学"])
        self.assertEqual(second["preserve_disabled_status"], ["chaoxing"])
        self.assertIsNone(self.app.store.get_value("daily_last_success"))

    def test_daily_success_accumulates_across_platform_specific_attempts(self):
        self.app.collector.request.return_value = outcome(chaoxing=True)
        self.app.automatic_collection(self.now)
        self.app.collector.request.return_value = outcome(xiji=True)
        self.app.automatic_collection(self.now + timedelta(minutes=5))
        self.assertEqual(self.app.store.get_value("daily_last_success"), (self.now + timedelta(minutes=5)).isoformat())
        self.app.automatic_collection(self.now + timedelta(hours=1))
        self.assertEqual(len(self.scans()), 2)

    def test_auth_required_stops_automatic_retries_until_explicit_login(self):
        self.app.collector.request.return_value = outcome(chaoxing=True, xiji_auth="not_authenticated")
        self.app.automatic_collection(self.now)
        self.app.automatic_collection(self.now + timedelta(minutes=5))
        self.app.automatic_collection(self.now + timedelta(days=1))
        self.assertEqual(len(self.scans()), 2)  # Next day checks only the healthy platform.
        self.assertEqual(self.scans()[1].args[1]["course_scope"]["xiji"], [])
        self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
        self.assertNotIn("xiji", self.app.store.get_value("collection_retry")["blocked_sources"])
        self.app.automatic_collection(self.now + timedelta(days=1, minutes=1))
        self.assertEqual(len(self.scans()), 3)
        self.assertEqual(self.scans()[-1].args[1]["course_scope"]["xiji"], ["离散数学"])

    def test_unverified_layout_does_not_become_login_required(self):
        self.app.collector.request.return_value = outcome(xiji_auth="unverified")
        self.app.automatic_collection(self.now)
        self.assertEqual(self.app.store.get_value("collection_retry")["blocked_sources"], {})

    def test_manual_scan_bypasses_exhausted_budget_and_auth_pause(self):
        self.app.refresh_auth_retry("xiji", "not_authenticated")
        self.app.scan(update_existing=True, import_new=True)
        self.assertEqual(self.scans()[0].args[1]["course_scope"]["xiji"], ["离散数学"])

    def test_exam_identity_upgrade_completes_original_without_reporting_new_task(self):
        task = {"title": "大物磁学测验", "course": "大学物理", "source": "chaoxing", "kind": "exam",
                "start_at": "2026-10-18T14:00:00+08:00", "due_at": "2026-10-18T16:20:00+08:00"}
        fingerprint = hashlib.sha256(json.dumps(
            [task["course"], task["title"], task["start_at"]], ensure_ascii=False,
            separators=(",", ":")).encode()).hexdigest()
        old = self.app.store.put_task({**task, "source_key": "exam-list-fingerprint:" + fingerprint})
        verified = {**task, "source_key": "course:11:exam:7", "status": "completed"}
        result = outcome(chaoxing=True, xiji=True)
        result["candidates"] = [verified]
        result["status_updates"] = [{"source": "chaoxing", "source_key": verified["source_key"], "status": "completed"}]
        self.app.collector.request.return_value = result
        scan = self.app.scan(update_existing=True, import_new=True)
        self.assertEqual((scan["added"], scan["completed"]), (0, 1))
        tasks = self.app.store.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["id"], tasks[0]["status"], tasks[0]["source_key"]),
                         (old["id"], "completed", verified["source_key"]))
        self.app.calendar.sync_if_enabled.assert_called()
        repeated = self.app.scan(update_existing=True, import_new=True)
        self.assertEqual((repeated["added"], repeated["completed"]), (0, 0))

    def test_reminder_retries_are_bounded_and_do_not_count_as_success(self):
        self.app.store.set_value("daily_last_success", self.now.isoformat())
        task = self.app.store.put_task({"title": "作业", "source": "xiji", "due_at": "2026-10-20T08:10:00+08:00"})
        for minutes in range(11):
            self.app.automatic_collection(self.now + timedelta(minutes=minutes))
        self.assertEqual(len(self.scans()), 3)
        key = f"{task['id']}|{task['due_at']}|48"
        self.assertNotIn(key, self.app.reminder_checked)
        self.assertEqual(self.app.store.get_value("collection_retry")["reminders"][key]["attempts"], 3)

    def test_only_successful_sources_reminder_keys_are_checked(self):
        self.app.store.set_value("daily_last_success", self.now.isoformat())
        first = self.app.store.put_task({"title": "概率论", "source": "chaoxing", "due_at": "2026-10-20T08:10:00+08:00"})
        second = self.app.store.put_task({"title": "离散数学", "source": "xiji", "due_at": first["due_at"]})
        self.app.collector.request.return_value = outcome(chaoxing=True)
        self.app.automatic_collection(self.now)
        self.assertIn(f"{first['id']}|{first['due_at']}|48", self.app.reminder_checked)
        self.assertNotIn(f"{second['id']}|{second['due_at']}|48", self.app.reminder_checked)

    def test_disabled_daily_also_disables_reminder_reads_and_startup_scan(self):
        self.app.store.patch_settings({"daily": {"enabled": False}})
        (Path(self.temp.name) / "browser-session.json").write_text("{}")
        self.app.resume_sessions()
        self.app.automatic_collection(self.now)
        self.app.collector.request.assert_not_called()

    def test_startup_only_prepares_sessions_without_unbudgeted_scan(self):
        (Path(self.temp.name) / "browser-session.json").write_text("{}")
        self.app.resume_sessions()
        self.assertEqual([call.args[0] for call in self.app.collector.request.call_args_list], ["restore", "restore"])

    def test_new_course_configuration_resets_daily_attempt_budget(self):
        self.app.collector.request.return_value = outcome(chaoxing=True)
        self.app.automatic_collection(self.now)
        self.app.store.patch_settings({"collection": {"course_scope": {"chaoxing_homework": ["新课程"], "chaoxing_exams": [], "xiji": []}}})
        self.app.automatic_collection(self.now + timedelta(minutes=1))
        self.assertEqual(len(self.scans()), 2)
        self.assertEqual(self.scans()[-1].args[1]["course_scope"]["chaoxing_homework"], ["新课程"])

    def handler(self, path, source="xiji"):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(app=self.app)
        handler.client_address = ("127.0.0.1", 9999)
        handler.command, handler.path = "POST", path
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8765"
        handler.headers["Content-Type"] = "application/json"
        body = json.dumps({"source": source}).encode()
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.responses = []
        handler.send = lambda *args, **kwargs: handler.responses.append(args)
        return handler

    def test_login_probe_validates_source_and_does_not_full_scan(self):
        handler = self.handler("/api/collection/probe", "invalid")
        handler.mutate()
        self.assertEqual(handler.responses[0][0], 400)
        self.app.collector.request.assert_not_called()
        handler = self.handler("/api/collection/probe")
        self.app.collector.request.return_value = {"authenticated": True, "status": "authenticated"}
        handler.mutate()
        self.assertEqual(handler.responses[0][0], 200)
        self.app.collector.request.assert_called_once_with("probe", "xiji")
        self.assertEqual(self.scans(), [])
        self.assertIsNone(self.app.sync_metadata()["last_attempt_at"])
        self.assertIsNone(self.app.sync_metadata()["last_success_at"])

    def test_finish_login_resumes_budget_only_after_confirmed_authentication(self):
        self.app.refresh_auth_retry("xiji", "not_authenticated")
        self.app.collector.request.return_value = {"authenticated": False, "status": "not_authenticated"}
        self.handler("/api/collection/finish-login").mutate()
        self.assertIn("xiji", self.app.store.get_value("collection_retry")["blocked_sources"])
        self.app.collector.request.return_value = {"authenticated": True, "status": "authenticated"}
        self.handler("/api/collection/finish-login").mutate()
        self.assertNotIn("xiji", self.app.store.get_value("collection_retry")["blocked_sources"])
        self.assertEqual(self.scans(), [])
        self.assertIsNone(self.app.sync_metadata()["last_attempt_at"])
        self.assertIsNone(self.app.sync_metadata()["last_success_at"])

    def test_login_repair_after_scan_return_is_not_reblocked_by_old_failure(self):
        self.app.collector.request.return_value = outcome(chaoxing=True, xiji_auth="not_authenticated")
        original_scan = self.app.scan

        def scan_then_repair(*args, **kwargs):
            result = original_scan(*args, **kwargs)
            self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
            return result

        with patch.object(self.app, "scan", side_effect=scan_then_repair):
            result = self.app.automatic_collection(self.now)
        state = self.app.store.get_value("collection_retry")
        self.assertNotIn("xiji", state["blocked_sources"])
        self.assertEqual(state["daily"]["attempts"], 0)
        self.assertIsNone(state["daily"]["next_attempt_at"])
        self.assertEqual(state["daily"]["completed_sources"], ["chaoxing"])
        self.assertIsNone(self.app.store.get_value("daily_last_success"))
        self.assertFalse(result["source_results"]["xiji"]["coverage_complete"])

    def test_login_repair_after_scan_return_does_not_accept_old_complete(self):
        self.app.collector.request.return_value = outcome(chaoxing=True, xiji=True)
        task = self.app.store.put_task({"title": "待核验", "source": "xiji", "due_at": "2026-10-20T08:10:00+08:00"})
        original_scan = self.app.scan

        def scan_then_repair(*args, **kwargs):
            result = original_scan(*args, **kwargs)
            self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
            return result

        with patch.object(self.app, "scan", side_effect=scan_then_repair):
            self.app.automatic_collection(self.now)
        state = self.app.store.get_value("collection_retry")
        key = f"{task['id']}|{task['due_at']}|48"
        self.assertEqual(state["daily"]["completed_sources"], ["chaoxing"])
        self.assertNotIn(key, self.app.reminder_checked)
        self.assertNotIn(key, state["reminders"])
        self.assertEqual(state["daily"]["attempts"], 0)
        self.assertIsNone(self.app.store.get_value("daily_last_success"))

    def test_login_repair_before_collector_returns_discards_old_auth_and_metadata(self):
        old = outcome(chaoxing=True, xiji=True)
        old["source_results"]["xiji"]["auth_status"] = "not_authenticated"
        old["candidates"] = [{"title": "旧会话条目", "source": "xiji", "source_key": "course:9:assignment:99",
                              "status": "pending", "due_at": "2026-10-21T09:50:00+08:00"}]

        def request(action, *args):
            if action == "scan":
                self.app.refresh_auth_retry("xiji", "authenticated", explicit=True)
                return old
            return {"ok": True}

        self.app.collector.request.side_effect = request
        result = self.app.automatic_collection(self.now)
        state = self.app.store.get_value("collection_retry")
        self.assertNotIn("xiji", state["blocked_sources"])
        self.assertNotIn("xiji", state["daily"]["completed_sources"])
        self.assertEqual(state["daily"]["attempts"], 0)
        self.assertEqual(result["source_results"]["xiji"]["deferred_reason"], "session_changed_during_scan")
        self.assertFalse(any(task.get("source_key") == "course:9:assignment:99" for task in self.app.store.tasks()))
        self.assertIsNone(self.app.store.get_value("daily_last_success"))


if __name__ == "__main__":
    unittest.main()
