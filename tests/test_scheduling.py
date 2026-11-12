import unittest
from datetime import datetime, timedelta, timezone

from studyreminder.scheduling import TZ, daily_due, reminder_recheck_keys, task_anchor, validate_daily_time


def pending_task(**changes):
    return {"id": "task-1", "source": "chaoxing", "status": "pending",
            "due_at": "2026-10-21T09:50:00+08:00", **changes}


class DailySchedulingTests(unittest.TestCase):
    def test_valid_daily_times_and_rejects_non_HHMM_values(self):
        for value in ("00:00", "08:00", "23:59"):
            self.assertEqual(validate_daily_time(value), value)
        for value in (None, 800, "8:00", "08:0", "24:00", "08:60", "08:00 ", "０８:００"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_daily_time(value)

    def test_before_and_at_scheduled_time(self):
        self.assertFalse(daily_due(datetime(2026, 10, 16, 7, 59, 59, tzinfo=TZ), "08:00", None))
        self.assertTrue(daily_due(datetime(2026, 10, 16, 8, tzinfo=TZ), "08:00", None))

    def test_manual_success_today_prevents_duplicate_automatic_run(self):
        self.assertFalse(daily_due(datetime(2026, 10, 16, 8, tzinfo=TZ), "08:00",
                                   "2026-10-16T07:00:00+08:00"))
        self.assertFalse(daily_due(datetime(2026, 10, 16, 23, 59, tzinfo=TZ), "08:00",
                                   "2026-10-16T08:00:00+08:00"))

    def test_success_yesterday_waits_until_today_schedule(self):
        last = "2026-10-15T08:00:00+08:00"
        self.assertFalse(daily_due(datetime(2026, 10, 16, 7, tzinfo=TZ), "08:00", last))
        self.assertTrue(daily_due(datetime(2026, 10, 16, 8, tzinfo=TZ), "08:00", last))

    def test_waking_after_scheduled_time_catches_up_today_once(self):
        wake = datetime(2026, 10, 16, 12, tzinfo=TZ)
        self.assertTrue(daily_due(wake, "08:00", "2026-10-15T08:00:00+08:00"))
        self.assertFalse(daily_due(wake + timedelta(minutes=1), "08:00", wake.isoformat()))
        self.assertTrue(daily_due(wake + timedelta(days=1), "08:00", wake.isoformat()))

    def test_midnight_and_utc_success_use_beijing_calendar_day(self):
        local_midnight = datetime(2026, 10, 15, 16, tzinfo=timezone.utc)
        self.assertTrue(daily_due(local_midnight, "00:00", "2026-10-15T15:59:59Z"))
        self.assertFalse(daily_due(local_midnight + timedelta(hours=8), "08:00", "2026-10-15T23:00:00Z"))
        self.assertTrue(daily_due(local_midnight + timedelta(hours=8), "08:00", "2026-10-15T15:59:59Z"))

    def test_invalid_or_naive_schedule_datetimes_are_explicit_errors(self):
        with self.assertRaises(ValueError):
            daily_due(datetime(2026, 10, 16, 8), "08:00", None)
        with self.assertRaises(ValueError):
            daily_due(datetime(2026, 10, 16, 8, tzinfo=TZ), "08:00", "2026-10-16T07:00:00")


class ReminderRecheckTests(unittest.TestCase):
    def setUp(self):
        self.task = pending_task()
        self.due = datetime(2026, 10, 21, 9, 50, tzinfo=TZ)

    def key(self, hours, task=None):
        item = task or self.task
        return f"{item['id']}|{item['due_at']}|{hours}"

    def test_each_alarm_only_in_ten_minute_window_including_endpoints(self):
        for hours in (48, 24):
            at = self.due - timedelta(hours=hours)
            for offset in (-600, -300, -120, -60, 0):
                with self.subTest(hours=hours, seconds=offset):
                    self.assertEqual(reminder_recheck_keys([self.task], at + timedelta(seconds=offset), set()),
                                     {self.key(hours)})
            for offset in (-601, 1, 120):
                with self.subTest(hours=hours, seconds=offset):
                    self.assertEqual(reminder_recheck_keys([self.task], at + timedelta(seconds=offset), set()), set())

    def test_checked_and_duplicate_tasks_do_not_repeat(self):
        at = self.due - timedelta(hours=48, minutes=1)
        checked = {self.key(48)}
        self.assertEqual(reminder_recheck_keys([self.task, self.task], at, set()), {self.key(48)})
        self.assertEqual(reminder_recheck_keys([self.task], at, checked), set())
        self.assertEqual(checked, {self.key(48)})

    def test_two_hour_alarm_gets_initial_and_final_submission_checks(self):
        at = self.due - timedelta(hours=2)
        key = self.key(2)
        final = key + "|final"
        self.assertEqual(reminder_recheck_keys([self.task], at - timedelta(minutes=10), set()), {key})
        self.assertEqual(reminder_recheck_keys([self.task], at - timedelta(minutes=4), {key}), set())
        self.assertEqual(reminder_recheck_keys([self.task], at - timedelta(minutes=3), {key}), {final})
        self.assertEqual(reminder_recheck_keys([self.task], at, set()), {key, final})
        self.assertEqual(reminder_recheck_keys([self.task], at, {key, final}), set())
        self.assertEqual(reminder_recheck_keys([self.task], at + timedelta(seconds=1), set()), set())

    def test_two_hour_check_skips_submitted_and_offline_work(self):
        at = self.due - timedelta(hours=2, minutes=2)
        tasks = [pending_task(status="completed"), pending_task(source="physics")]
        self.assertEqual(reminder_recheck_keys(tasks, at, set()), set())

    def test_new_deadline_gets_new_check_even_if_old_deadline_checked(self):
        changed = pending_task(due_at="2026-10-22T09:50:00+08:00")
        at = datetime(2026, 10, 20, 9, 49, tzinfo=TZ)
        self.assertEqual(reminder_recheck_keys([changed], at, {self.key(48)}), {self.key(48, changed)})

    def test_completed_offline_manual_and_unknown_tasks_are_skipped(self):
        tasks = [pending_task(status="completed"), pending_task(source="physics"),
                 pending_task(source="manual"), pending_task(status="unknown")]
        self.assertEqual(reminder_recheck_keys(tasks, self.due - timedelta(hours=48), set()), set())

    def test_xiji_and_utc_now_are_supported(self):
        task = pending_task(source="xiji")
        at = (self.due - timedelta(hours=24, minutes=1)).astimezone(timezone.utc)
        self.assertEqual(reminder_recheck_keys([task], at, set()), {self.key(24)})

    def test_midnight_deadline_window_crosses_local_calendar_day(self):
        task = pending_task(due_at="2026-10-18T00:00:00+08:00")
        at = datetime(2026, 10, 15, 23, 59, tzinfo=TZ)
        self.assertEqual(reminder_recheck_keys([task], at, set()), {self.key(48, task)})

    def test_malformed_task_is_skipped_without_losing_other_checks(self):
        tasks = [None, {}, pending_task(due_at="invalid"),
                 pending_task(due_at="2026-10-21T09:50:00"), self.task]
        self.assertEqual(reminder_recheck_keys(tasks, self.due - timedelta(hours=48), set()), {self.key(48)})

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ValueError):
            reminder_recheck_keys([self.task], datetime(2026, 10, 19, 9, 49), set())


class ExamStartReminderTests(unittest.TestCase):
    def setUp(self):
        self.task = pending_task(kind="exam", start_at="2026-11-14T09:00:00+08:00",
                                 due_at="2026-11-14T11:30:00+08:00")
        self.start = datetime(2026, 11, 14, 9, tzinfo=TZ)

    def key(self, hours, start="2026-11-14T09:00:00+08:00"):
        return f"task-1|exam-start:{start}|{hours}"

    def test_all_three_prechecks_use_exam_start_and_not_exam_end(self):
        for hours in (48, 24, 2):
            with self.subTest(hours=hours):
                at = self.start - timedelta(hours=hours, minutes=5)
                self.assertEqual(reminder_recheck_keys([self.task], at, set()), {self.key(hours)})
                end_based = datetime(2026, 11, 14, 11, 30, tzinfo=TZ) - timedelta(hours=hours, minutes=5)
                self.assertEqual(reminder_recheck_keys([self.task], end_based, set()), set())

    def test_final_two_hour_precheck_is_before_opening_not_closing(self):
        at = self.start - timedelta(hours=2, minutes=2)
        initial = self.key(2)
        self.assertEqual(reminder_recheck_keys([self.task], at, {initial}), {initial + "|final"})
        self.assertEqual(reminder_recheck_keys([self.task], at, {initial, initial + "|final"}), set())

    def test_start_change_gets_new_key_and_old_deadline_keys_do_not_suppress_it(self):
        old_end_key = f"task-1|{self.task['due_at']}|48"
        self.assertEqual(reminder_recheck_keys([self.task], self.start - timedelta(hours=48), {old_end_key}),
                         {self.key(48)})
        changed = {**self.task, "start_at": "2026-11-14T10:00:00+08:00"}
        self.assertEqual(reminder_recheck_keys([changed], self.start - timedelta(hours=47), {self.key(48)}),
                         {self.key(48, changed["start_at"])})

    def test_end_only_change_keeps_same_reminder_and_checked_key(self):
        changed = {**self.task, "due_at": "2026-11-14T12:30:00+08:00"}
        at = self.start - timedelta(hours=24, minutes=1)
        self.assertEqual(reminder_recheck_keys([changed], at, set()), {self.key(24)})
        self.assertEqual(reminder_recheck_keys([changed], at, {self.key(24)}), set())

    def test_missing_or_invalid_exam_start_never_uses_end_time(self):
        at = datetime(2026, 11, 12, 11, 29, tzinfo=TZ)
        for value in (None, "", "invalid", 0, "2026-11-14T09:00:00", "2026-11-14T12:00:00+08:00"):
            item = {**self.task, "start_at": value}
            with self.subTest(start=value):
                self.assertIsNone(task_anchor(item))
                self.assertEqual(reminder_recheck_keys([item], at, set()), set())

    def test_utc_start_is_normalized_and_equal_start_end_is_valid(self):
        item = {**self.task, "start_at": "2026-11-14T01:00:00Z"}
        self.assertEqual(task_anchor(item), self.start)
        self.assertEqual(reminder_recheck_keys([item], self.start - timedelta(hours=48), set()), {self.key(48)})
        self.assertEqual(task_anchor({**self.task, "due_at": self.task["start_at"]}), self.start)

    def test_assignment_and_lecture_continue_using_due_even_with_a_start_field(self):
        for kind in ("assignment", "lecture"):
            item = {**self.task, "kind": kind}
            with self.subTest(kind=kind):
                self.assertEqual(task_anchor(item), datetime(2026, 11, 14, 11, 30, tzinfo=TZ))


if __name__ == "__main__":
    unittest.main()
