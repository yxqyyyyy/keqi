"""Pure scheduling decisions; callers own persistence and platform requests."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Set
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
PLATFORM_SOURCES = frozenset({"chaoxing", "xiji"})
REMINDER_HOURS = (48, 24, 2)
RECHECK_WINDOW = timedelta(minutes=10)
FINAL_RECHECK_WINDOW = timedelta(minutes=3)


def validate_daily_time(value: str) -> str:
    """Return a valid 24-hour HH:MM value, or raise ValueError.

    Midnight is 00:00. Unlike a deadline labelled Saturday 24:00, a daily
    collection time has to identify a time inside one calendar day.
    """
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("每日读取时间应为 00:00–23:59，格式为 HH:MM")
    return value


def _local_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("调度日期时间必须包含时区")
    return value.astimezone(TZ)


def _parse_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("调度日期时间必须是含时区的 ISO 日期时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("调度日期时间格式不正确") from exc
    return _local_datetime(parsed)


def task_anchor(task: Mapping) -> datetime | None:
    """Return the instant reminders and ordering use, without guessing dates.

    Exams are due to be attended at ``start_at``. Their closing/deadline time
    remains separate metadata and must never stand in for a missing start.
    Assignments and lectures keep their stored deadline/class-time anchor.
    """
    if not isinstance(task, Mapping):
        return None
    try:
        due = _parse_datetime(task.get("due_at"))
        if task.get("kind") != "exam":
            return due
        start = _parse_datetime(task.get("start_at"))
        return start if start <= due else None
    except (ValueError, OverflowError):
        return None


def daily_due(now: datetime, timeHHMM: str, last_success_iso: str | None) -> bool:
    """Whether today's full collection is due in Beijing time.

    A successful manual or automatic full collection already made today counts
    as today's collection. A failed attempt must not be passed as success.
    Evaluating after the chosen time also catches a Mac waking from sleep;
    before that time an older successful day does not trigger an early run.
    """
    local_now = _local_datetime(now)
    hh, mm = map(int, validate_daily_time(timeHHMM).split(":"))
    if last_success_iso is not None:
        if _parse_datetime(last_success_iso).date() == local_now.date():
            return False
    return local_now >= local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def reminder_recheck_keys(
    tasks: Iterable[Mapping], now: datetime, checked_set: Set[str]
) -> set[str]:
    """Select pending platform tasks for a status check just before each alarm.

    ``tasks`` must be locally confirmed, stored tasks, rather than unreviewed
    collection candidates. A task is eligible from ten minutes before its
    48-hour, 24-hour or 2-hour reminder through the reminder instant, inclusive. Once
    the reminder instant has passed no delayed pre-reminder check is requested.

    Keys contain task identity, its reminder anchor, and reminder lead hours.
    Exam anchors include an explicit policy prefix so old deadline checks never
    suppress start-time checks, even when start and end happen to be equal.
    Changing an exam start permits a new check. The caller persists successful
    keys in ``checked_set``; this function never mutates it. Invalid task dates
    are skipped so one malformed record cannot stop checks for other tasks.
    The 2-hour reminder also gets a final check in the last three minutes,
    so a submission after the initial check can still cancel its event.
    Offline physics and manual tasks cannot have platform submission checks.
    """
    local_now = _local_datetime(now)
    result = set()
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        if task.get("status") != "pending" or task.get("source") not in PLATFORM_SOURCES:
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        anchor = task_anchor(task)
        if anchor is None:
            continue
        # Preserve existing assignment keys while migrating every exam away
        # from the old end-time policy. Server callers use only the first | part.
        anchor_key = ("exam-start:" + anchor.isoformat(timespec="seconds")
                      if task.get("kind") == "exam" else task["due_at"])
        for hours in REMINDER_HOURS:
            reminder_at = anchor - timedelta(hours=hours)
            key = f"{task_id}|{anchor_key}|{hours}"
            if reminder_at - RECHECK_WINDOW <= local_now <= reminder_at and key not in checked_set:
                result.add(key)
            if hours == 2:
                final_key = key + "|final"
                if reminder_at - FINAL_RECHECK_WINDOW <= local_now <= reminder_at and final_key not in checked_set:
                    result.add(final_key)
    return result
