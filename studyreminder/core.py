from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from .scheduling import REMINDER_HOURS, validate_daily_time

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SETTINGS = {
    "physics": {"enabled": False, "time": "24:00", "until": None},
    "calendar": {"name": "作业与考试", "identifier": None, "auto_sync": False},
    "collection": {"auto_scan": False, "interval_minutes": 1440, "homework_year": None,
                   "course_scope": {"chaoxing_homework": [], "chaoxing_exams": [], "xiji": []}},
    "daily": {"enabled": True, "time": "08:00"},
}
LEGACY_COURSE_SCOPE = {"chaoxing_homework": ["概率论"], "chaoxing_exams": ["大物", "大学物理"],
                       "xiji": ["计算机组成", "离散数学"]}
SOURCES = {"chaoxing", "xiji", "manual", "physics"}
KINDS = {"assignment", "exam"}


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_datetime(value):
    if not isinstance(value, str):
        raise ValueError("截止时间必须是含时区的 ISO 日期时间")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("日期时间格式不正确") from exc
    if result.tzinfo is None:
        raise ValueError("日期时间必须包含时区，例如 +08:00")
    return result.astimezone(TZ)


def validate_task(raw, existing=None):
    if not isinstance(raw, dict):
        raise ValueError("任务必须是对象")
    item = dict(existing or {})
    item.update({k: v for k, v in raw.items() if k in {
        "title", "course", "source", "kind", "start_at", "due_at", "status", "url", "notes", "source_key"
    }})
    for field, limit in (("title", 300), ("course", 200), ("url", 2000), ("notes", 6000), ("source_key", 500)):
        value = item.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{field} 必须是长度不超过 {limit} 的文字")
        item[field] = value.strip()
    if not item["title"]:
        raise ValueError("请填写任务名称")
    item.setdefault("source", "manual")
    item.setdefault("kind", "assignment")
    item.setdefault("status", "pending")
    if item["source"] not in SOURCES or item["kind"] not in KINDS:
        raise ValueError("任务来源或类型不正确")
    if item["status"] not in {"pending", "completed"}:
        raise ValueError("任务状态不正确")
    item["due_at"] = parse_datetime(item.get("due_at")).isoformat(timespec="seconds")
    item["start_at"] = parse_datetime(item["start_at"]).isoformat(timespec="seconds") if item.get("start_at") else None
    if item["start_at"] and parse_datetime(item["start_at"]) > parse_datetime(item["due_at"]):
        raise ValueError("开始时间不能晚于截止时间")
    if item["url"] and (urlparse(item["url"]).scheme not in {"http", "https"} or not urlparse(item["url"]).netloc):
        raise ValueError("任务链接只支持 http 或 https")
    item["id"] = existing["id"] if existing else str(uuid.uuid4())
    item["updated_at"] = now_iso()
    item["calendar_id"] = (existing or {}).get("calendar_id")
    return item


def stable_key(item):
    # A verified platform ID survives title or deadline changes. No fuzzy matching.
    if item.get("source_key"):
        key = item["source"] + ":" + item["source_key"]
        if item["source_key"].startswith("page:"):
            key += ":" + hashlib.sha256(json.dumps([item["course"], item["kind"]], ensure_ascii=False).encode()).hexdigest()[:20]
        return key
    return "fallback:" + hashlib.sha256(json.dumps([
        item["source"], item["course"], item["kind"], item["title"], item["due_at"]
    ], ensure_ascii=False).encode()).hexdigest()


def physics_occurrences(settings, now=None, weeks=8):
    cfg = settings["physics"]
    if not cfg["enabled"]:
        return []
    now = (now or datetime.now(TZ)).astimezone(TZ)
    saturday = now.date() + timedelta(days=(5 - now.weekday()) % 7)
    # Early Sunday still belongs to the immediately preceding Saturday.
    if now.weekday() == 6 and now.time() == time(0):
        saturday = now.date() - timedelta(days=1)
    hh, mm = map(int, cfg["time"].split(":"))
    until = date.fromisoformat(cfg["until"]) if cfg.get("until") else None
    result = []
    for week in range(weeks + 1):
        day = saturday + timedelta(weeks=week)
        due = datetime.combine(day, time(0) if hh == 24 else time(hh, mm), TZ)
        if hh == 24:
            due += timedelta(days=1)
        if due < now or (until and day > until):
            continue
        result.append({
            "id": "physics-" + day.isoformat(), "title": "大物作业提交", "course": "大学物理",
            "source": "physics", "kind": "assignment", "start_at": None,
            "due_at": due.isoformat(timespec="seconds"), "status": "pending", "url": "",
            "notes": "固定每周六 " + cfg["time"] + " 截止（北京时间）。线下提交后，请在本程序勾选该周已完成，取消对应提醒。", "source_key": "",
            "updated_at": now_iso(), "calendar_id": None,
        })
        if len(result) == weeks:
            break
    return result


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='kv'").fetchone() is not None
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, dedup TEXT UNIQUE, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)
            row = db.execute("SELECT body FROM kv WHERE key='settings'").fetchone()
            saved = json.loads(row[0]) if row else {}
            if "course_scope" not in saved.get("collection", {}):
                # Existing installations used these implicit defaults. A fresh
                # installation starts empty and uses only its own configuration.
                saved.setdefault("collection", {})["course_scope"] = copy.deepcopy(
                    LEGACY_COURSE_SCOPE if legacy else DEFAULT_SETTINGS["collection"]["course_scope"])
                saved.setdefault("physics", {}).setdefault("enabled", legacy)
                db.execute("INSERT INTO kv VALUES ('settings', ?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                           (json.dumps(saved, ensure_ascii=False),))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_value(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT body FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    def set_value(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                       (key, json.dumps(value, ensure_ascii=False)))

    def settings(self):
        saved = self.get_value("settings", {})
        result = copy.deepcopy(DEFAULT_SETTINGS)
        for k in result:
            result[k].update(saved.get(k, {}))
        return result

    def patch_settings(self, patch, reset_future_physics=False):
        """Merge just the touched fields without overwriting another request."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM kv WHERE key='settings'").fetchone()
            current = copy.deepcopy(DEFAULT_SETTINGS)
            saved = json.loads(row[0]) if row else {}
            for key in current:
                current[key].update(saved.get(key, {}))
                current[key].update(patch.get(key, {}))
            if reset_future_physics:
                for task_id, body in db.execute("SELECT id, body FROM tasks WHERE id LIKE 'physics-%'").fetchall():
                    old = json.loads(body)
                    if old["status"] == "pending" and parse_datetime(old["due_at"]) >= datetime.now(TZ):
                        db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            db.execute("INSERT INTO kv VALUES ('settings', ?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                       (json.dumps(current, ensure_ascii=False),))
        return current

    def update_settings(self, patch):
        result = self.settings()
        previous_collection = copy.deepcopy(result["collection"])
        if not isinstance(patch, dict):
            raise ValueError("设置格式不正确")
        if "physics" in patch:
            if not isinstance(patch["physics"], dict):
                raise ValueError("大物设置格式不正确")
            result["physics"].update({k: v for k, v in patch["physics"].items() if k in {"enabled", "time", "until"}})
            cfg = result["physics"]
            if not isinstance(cfg["enabled"], bool) or not isinstance(cfg["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00", cfg["time"]):
                raise ValueError("请填写 00:00–23:59 或 24:00")
            if cfg.get("until"):
                try:
                    date.fromisoformat(cfg["until"])
                except (TypeError, ValueError) as exc:
                    raise ValueError("学期结束日期格式应为 YYYY-MM-DD") from exc
        if "collection" in patch:
            cfg = patch["collection"]
            if not isinstance(cfg, dict):
                raise ValueError("采集设置格式不正确")
            if "auto_scan" in cfg:
                if not isinstance(cfg["auto_scan"], bool):
                    raise ValueError("auto_scan 必须是布尔值")
                result["collection"]["auto_scan"] = cfg["auto_scan"]
            if "course_scope" in cfg:
                scope = cfg["course_scope"]
                fields = DEFAULT_SETTINGS["collection"]["course_scope"]
                if not isinstance(scope, dict) or set(scope) - set(fields):
                    raise ValueError("课程范围格式不正确")
                updated = copy.deepcopy(result["collection"]["course_scope"])
                for key, values in scope.items():
                    if (not isinstance(values, list) or len(values) > 50 or
                            any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values)):
                        raise ValueError("每类最多填写 50 个课程关键词，每个为 1–200 字")
                    updated[key] = list(dict.fromkeys(v.strip() for v in values))
                result["collection"]["course_scope"] = updated
            if "homework_year" in cfg:
                year = cfg["homework_year"]
                if year is not None and (type(year) is not int or not 2000 <= year <= 2100):
                    raise ValueError("作业年份应为 2000–2100 的整数，或留空")
                result["collection"]["homework_year"] = year
            if any(result["collection"].get(k) != previous_collection.get(k) for k in ("course_scope", "homework_year")):
                result["collection"]["homework_year_binding"] = None
        if "daily" in patch:
            if not isinstance(patch["daily"], dict):
                raise ValueError("每日同步设置格式不正确")
            result["daily"].update({k: v for k, v in patch["daily"].items() if k in {"enabled", "time"}})
            if not isinstance(result["daily"]["enabled"], bool):
                raise ValueError("每日同步开关格式不正确")
            result["daily"]["time"] = validate_daily_time(result["daily"]["time"])
        result = self.patch_settings({k: result[k] for k in ("physics", "collection", "daily") if k in patch}, reset_future_physics="physics" in patch)
        if any(result["collection"].get(k) != previous_collection.get(k) for k in ("course_scope", "homework_year")):
            self.set_value("daily_last_success", None)
        self.ensure_physics()
        return result

    def ensure_physics(self):
        with self.connect() as db:
            for item in physics_occurrences(self.settings()):
                db.execute("INSERT OR IGNORE INTO tasks VALUES (?, ?, ?)",
                           (item["id"], item["id"], json.dumps(item, ensure_ascii=False)))

    def tasks(self):
        self.ensure_physics()
        with self.connect() as db:
            result = [json.loads(row[0]) for row in db.execute("SELECT body FROM tasks")]
        return sorted(result, key=lambda t: (parse_datetime(t["due_at"]), t["id"]))

    def get_task(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        return json.loads(row[0])

    def put_task(self, raw, task_id=None):
        item = validate_task(raw, self.get_task(task_id) if task_id else None)
        if item["source"] == "physics" and not task_id:
            raise ValueError("请在每周大物设置中创建重复任务；临时安排可选择手动添加")
        with self.connect() as db:
            try:
                db.execute("INSERT INTO tasks VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET dedup=excluded.dedup, body=excluded.body",
                           (item["id"], item["id"] if item["source"] == "physics" else stable_key(item), json.dumps(item, ensure_ascii=False)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("同一任务已经存在，请编辑已有任务") from exc
        return item

    def delete_task(self, task_id):
        item = self.get_task(task_id)
        with self.connect() as db:
            if item["source"] == "physics":
                # Tombstone prevents weekly materialization from resurrecting a deleted week.
                item["status"] = "completed"
                item["updated_at"] = now_iso()
                db.execute("UPDATE tasks SET body=? WHERE id=?", (json.dumps(item, ensure_ascii=False), task_id))
            else:
                db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def import_tasks(self, values):
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError("每次最多导入 1000 条任务")
        # Validate the whole batch before any write.
        prepared = []
        for raw in values:
            item = validate_task(raw)
            # Retain deterministic weekly IDs when restoring our own JSON export.
            if item["source"] == "physics":
                task_id = raw.get("id", "")
                if not isinstance(task_id, str) or not re.fullmatch(r"physics-\d{4}-\d{2}-\d{2}", task_id):
                    raise ValueError("大物重复任务请在每周设置中创建，或导入本程序导出的备份")
                date.fromisoformat(task_id[len("physics-"):])
                item["id"] = task_id
            prepared.append(item)
        result = []
        with self.connect() as db:
            for item in prepared:
                key = item["id"] if item["source"] == "physics" else stable_key(item)
                row = db.execute("SELECT id, body FROM tasks WHERE dedup=?", (key,)).fetchone()
                if row:
                    existing = json.loads(row[1])
                    # A stale import must not undo a user's completed state.
                    item["id"] = row[0]
                    if existing["status"] == "completed":
                        item["status"] = "completed"
                db.execute("INSERT INTO tasks VALUES (?, ?, ?) ON CONFLICT(dedup) DO UPDATE SET body=excluded.body",
                           (item["id"], key, json.dumps(item, ensure_ascii=False)))
                result.append(item)
        return result


def ics_escape(value):
    return str(value).replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_ics(line):
    # RFC 5545: physical lines <= 75 octets, including continuation whitespace.
    lines, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            lines.append(current)
            current = " "
        current += char
    lines.append(current)
    return "\r\n".join(lines)


def to_ics(tasks):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Study Deadline//CN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:作业与考试"]
    for item in tasks:
        if item["status"] != "pending":
            continue
        due = parse_datetime(item["due_at"]).astimezone(timezone.utc)
        description = item.get("notes", "")
        if item.get("start_at"):
            description += "\n考试开始：" + parse_datetime(item["start_at"]).strftime("%Y-%m-%d %H:%M 北京时间")
        lines += ["BEGIN:VEVENT", "UID:" + item["id"] + "@study-deadline.local", "DTSTAMP:" + stamp,
                  "DTSTART:" + due.strftime("%Y%m%dT%H%M%SZ"),
                  "DTEND:" + (due + timedelta(minutes=15)).strftime("%Y%m%dT%H%M%SZ"),
                  "SUMMARY:" + ics_escape((item["course"] + " · " if item.get("course") else "") + item["title"]),
                  "DESCRIPTION:" + ics_escape(description)]
        if item.get("url"):
            lines.append("URL:" + ics_escape(item["url"]))
        for hours in REMINDER_HOURS:
            trigger = f"-PT{hours}H"
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + ics_escape(item["title"]), "TRIGGER:" + trigger, "END:VALARM"]
        lines += ["END:VEVENT"]
    lines += ["END:VCALENDAR"]
    return "\r\n".join(fold_ics(line) for line in lines) + "\r\n"
