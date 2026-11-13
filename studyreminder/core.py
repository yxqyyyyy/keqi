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
from .scheduling import REMINDER_HOURS, task_anchor, validate_daily_time

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
KINDS = {"assignment", "exam", "lecture"}
EDITABLE_TASK_FIELDS = {"title", "course", "kind", "start_at", "due_at", "status", "url", "notes"}


class TaskConflictError(ValueError):
    def __init__(self, current_task):
        super().__init__("任务已在另一台设备修改，请查看最新内容后重试")
        self.current_task = copy.deepcopy(current_task)


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
    if (not isinstance(item["source"], str) or item["source"] not in SOURCES
            or not isinstance(item["kind"], str) or item["kind"] not in KINDS):
        raise ValueError("任务来源或类型不正确")
    if not isinstance(item["status"], str) or item["status"] not in {"pending", "completed"}:
        raise ValueError("任务状态不正确")
    item["due_at"] = parse_datetime(item.get("due_at")).isoformat(timespec="seconds")
    item["start_at"] = parse_datetime(item["start_at"]).isoformat(timespec="seconds") if item.get("start_at") else None
    if item["start_at"] and parse_datetime(item["start_at"]) > parse_datetime(item["due_at"]):
        raise ValueError("开始时间不能晚于截止时间")
    if item["kind"] == "lecture" and item["start_at"] and item["start_at"] != item["due_at"]:
        raise ValueError("上课安排的提醒时间必须与开课时间一致")
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


def _canonical_chaoxing_exam_fingerprint(item):
    """Reconstruct only the old, fully dated exam-list identity."""
    if (item.get("source") != "chaoxing" or item.get("kind") != "exam"
            or not re.fullmatch(r"course:[0-9]+:exam:[0-9]+", item.get("source_key", ""))
            or not item.get("course") or not item.get("start_at")):
        return None
    try:
        start = parse_datetime(item["start_at"]).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None
    if start != item["start_at"]:
        return None
    payload = [item["course"], " ".join(item["title"].split()), start]
    return "exam-list-fingerprint:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _same_exam_metadata(left, right):
    return (left.get("source") == right.get("source") == "chaoxing"
            and left.get("kind") == right.get("kind") == "exam"
            and all(left.get(key) == right.get(key) for key in ("course", "title", "start_at", "due_at")))


def _preserved_notes(*values):
    """Keep every distinct note intact, or refuse a merge beyond our limit."""
    result = ""
    for value in values:
        if value and value not in result:
            result += ("\n\n" if result else "") + value
    return result if len(result) <= 6000 else None


def _apply_task_overrides(incoming, existing, overrides):
    item = {**incoming, **overrides}
    start = item.get("start_at")
    if start and (parse_datetime(start) > parse_datetime(item["due_at"])
                  or item["kind"] == "lecture" and start != item["due_at"]):
        # The source and a personal time edit can each be valid while their
        # combination is invalid. Retain the last valid time combination.
        for field in ("kind", "start_at", "due_at"):
            if field not in overrides:
                item[field] = existing.get(field)
    return validate_task(item, existing)


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
                CREATE TABLE IF NOT EXISTS task_state (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    overrides TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO task_state (id, revision, overrides, deleted)
                    SELECT id, 1, '{}', 0 FROM tasks;
                CREATE TABLE IF NOT EXISTS cloud_receipts (op_id TEXT PRIMARY KEY, body TEXT NOT NULL);
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
                occurrences = {item["id"]: item for item in physics_occurrences(current)}
                for task_id, body in db.execute("SELECT id, body FROM tasks WHERE id LIKE 'physics-%'").fetchall():
                    old = json.loads(body)
                    if old["status"] == "pending" and parse_datetime(old["due_at"]) >= datetime.now(TZ):
                        previous, dedup, overrides = self._task_record(db, task_id)
                        if task_id in occurrences:
                            item = _apply_task_overrides(occurrences[task_id], previous, overrides)
                            self._write_task(db, item, dedup, previous, overrides)
                        else:
                            if {"kind", "start_at", "due_at"}.intersection(overrides):
                                # Retain companion time fields while the weekly
                                # item is disabled, so its edited schedule can
                                # be restored as a valid whole later.
                                overrides = {**{field: previous.get(field) for field in ("kind", "start_at", "due_at")},
                                             **overrides}
                                self._write_task(db, previous, dedup, previous, overrides)
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
            db.execute("BEGIN IMMEDIATE")
            for item in physics_occurrences(self.settings()):
                if db.execute("SELECT id FROM tasks WHERE id=?", (item["id"],)).fetchone():
                    continue
                self._write_task(db, item, item["id"])

    def _task_record(self, db, task_id):
        row = db.execute("SELECT dedup, body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        state = db.execute("SELECT revision, overrides, deleted FROM task_state WHERE id=?", (task_id,)).fetchone()
        item = json.loads(row[1])
        item["revision"] = state[0] if state else 1
        item["deleted"] = bool(state[2]) if state else False
        return item, row[0], json.loads(state[1]) if state else {}

    def _write_task(self, db, item, dedup, existing=None, overrides=None, deleted=False):
        """Persist task content and its edit policy in the caller's transaction."""
        state = db.execute("SELECT revision, overrides, deleted FROM task_state WHERE id=?", (item["id"],)).fetchone()
        if existing is None and state:
            # A weekly configuration change can temporarily remove a future
            # occurrence. Its revision and personal edits survive recreation.
            overrides = json.loads(state[1])
            item = validate_task({**item, **overrides}, item)
            deleted = bool(state[2])
        overrides = overrides or {}
        body = {key: value for key, value in item.items() if key not in {"revision", "deleted"}}
        if deleted:
            body["status"] = "completed"
        revision = state[0] + 1 if existing is None and state else 1
        if existing is not None:
            previous, _, previous_overrides = self._task_record(db, existing["id"])
            previous_body = {key: value for key, value in previous.items()
                             if key not in {"revision", "deleted", "updated_at"}}
            content = {key: value for key, value in body.items() if key != "updated_at"}
            changed = (content != previous_body or overrides != previous_overrides
                       or deleted != previous["deleted"])
            revision = previous["revision"] + int(changed)
            body["updated_at"] = now_iso() if changed else previous["updated_at"]
        db.execute("INSERT INTO tasks VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET dedup=excluded.dedup, body=excluded.body",
                   (body["id"], dedup, json.dumps(body, ensure_ascii=False)))
        db.execute("INSERT INTO task_state VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, overrides=excluded.overrides, deleted=excluded.deleted",
                   (body["id"], revision, json.dumps(overrides, ensure_ascii=False), int(deleted)))
        return {**body, "revision": revision, "deleted": deleted}

    def tasks(self, include_deleted=False):
        self.ensure_physics()
        with self.connect() as db:
            result = [self._task_record(db, row[0])[0] for row in db.execute("SELECT id FROM tasks").fetchall()]
        if not include_deleted:
            result = [item for item in result if not item["deleted"]]
        return sorted(result, key=lambda t: (task_anchor(t) or datetime.max.replace(tzinfo=TZ), t["id"]))

    def get_task(self, task_id):
        with self.connect() as db:
            return self._task_record(db, task_id)[0]

    def _put_task(self, db, raw, task_id=None, mark_overrides=True, override_fields=None):
        existing, dedup, overrides = self._task_record(db, task_id) if task_id else (None, None, {})
        if existing and existing["deleted"]:
            raise TaskConflictError(existing)
        item = validate_task(raw, existing)
        if item["source"] == "physics" and not task_id:
            raise ValueError("请在每周大物设置中创建重复任务；临时安排可选择手动添加")
        if existing and mark_overrides and existing["source"] in {"chaoxing", "xiji", "physics"}:
            overrides.update({key: item[key] for key in EDITABLE_TASK_FIELDS.intersection(raw)
                              if item.get(key) != existing.get(key) or key in (override_fields or ())})
        # Keep the collected identity when display fields are edited, including
        # old records whose source identity was derived from their original text.
        if not existing or any(item.get(key) != existing.get(key) for key in ("source", "source_key")):
            dedup = item["id"] if item["source"] == "physics" else stable_key(item)
        return self._write_task(db, item, dedup, existing, overrides)

    def put_task(self, raw, task_id=None, *, mark_overrides=True, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if expected_revision is not None:
                    if type(expected_revision) is not int or expected_revision < 1 or not task_id:
                        raise ValueError("任务版本不正确")
                    current = self._task_record(db, task_id)[0]
                    if current["revision"] != expected_revision:
                        raise TaskConflictError(current)
                return self._put_task(db, raw, task_id, mark_overrides)
            except sqlite3.IntegrityError as exc:
                raise ValueError("同一任务已经存在，请编辑已有任务") from exc

    def apply_task_patch(self, task_id, changes, expected_revision=None, base_fields=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._apply_task_patch(db, task_id, changes, expected_revision, base_fields)

    def _apply_task_patch(self, db, task_id, changes, expected_revision=None, base_fields=None):
        if not isinstance(changes, dict) or set(changes) - EDITABLE_TASK_FIELDS:
            raise ValueError("任务修改字段不正确")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("任务版本不正确")
        if base_fields is not None and (not isinstance(base_fields, dict) or set(changes) - set(base_fields)):
            raise ValueError("请提供每个修改字段原来的值")
        current, _, _ = self._task_record(db, task_id)
        proposed = validate_task(changes, current)
        if current["deleted"]:
            raise TaskConflictError(current)
        if base_fields is not None:
            if any(current.get(key) not in (base_fields[key], proposed.get(key)) for key in changes):
                raise TaskConflictError(current)
        elif expected_revision is not None and expected_revision != current["revision"]:
            raise TaskConflictError(current)
        override_fields = {key for key in changes if proposed.get(key) != base_fields[key]} if base_fields is not None else None
        return self._put_task(db, changes, task_id, override_fields=override_fields)

    def delete_task(self, task_id, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._delete_task(db, task_id, expected_revision)

    def _delete_task(self, db, task_id, expected_revision=None):
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("任务版本不正确")
        item, dedup, overrides = self._task_record(db, task_id)
        if expected_revision is not None and item["revision"] != expected_revision:
            raise TaskConflictError(item)
        item["status"] = "completed"
        return self._write_task(db, item, dedup, item, overrides, deleted=True)

    def apply_remote_operation(self, operation):
        """Commit an immutable phone operation and its receipt atomically."""
        if not isinstance(operation, dict):
            return {"op_id": None, "status": "invalid", "message": "手机修改格式不正确"}
        op_id = operation.get("op_id")
        if not isinstance(op_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", op_id):
            return {"op_id": None, "status": "invalid", "message": "修改编号不正确"}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT body FROM cloud_receipts WHERE op_id=?", (op_id,)).fetchone()
            if saved:
                return json.loads(saved[0])
            db.execute("SAVEPOINT remote_change")
            try:
                task_id = operation.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                    raise ValueError("任务编号不正确")
                action = operation.get("action")
                if not isinstance(action, str):
                    raise ValueError("手机修改操作不正确")
                if action == "create":
                    changes = operation.get("changes")
                    if (not isinstance(changes, dict) or set(changes) - EDITABLE_TASK_FIELDS - {"source"}
                            or changes.get("source", "manual") != "manual" or task_id.startswith("physics-")):
                        raise ValueError("手机新增任务只能使用手动来源")
                    existing = db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if existing:
                        raise TaskConflictError(self._task_record(db, task_id)[0])
                    item = validate_task({**changes, "source": "manual"})
                    item["id"] = task_id
                    current = self._write_task(db, item, stable_key(item))
                elif action in {"patch", "delete"}:
                    revision = operation.get("base_revision")
                    if type(revision) is not int or revision < 1:
                        raise ValueError("手机修改缺少任务原版本")
                    if action == "patch":
                        current = self._apply_task_patch(db, task_id, operation.get("changes"), revision,
                                                         operation.get("base"))
                    else:
                        current = self._delete_task(db, task_id, revision)
                else:
                    raise ValueError("手机修改操作不正确")
                receipt = {"op_id": op_id, "status": "applied", "message": "已保存到 Mac", "current": current}
            except TaskConflictError as exc:
                receipt = {"op_id": op_id, "status": "conflict", "message": str(exc), "current": exc.current_task}
            except KeyError:
                receipt = {"op_id": op_id, "status": "conflict", "message": "Mac 上找不到该任务", "current": None}
            except (ValueError, sqlite3.IntegrityError) as exc:
                message = "同一任务已经存在，请编辑已有任务" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
                receipt = {"op_id": op_id, "status": "invalid", "message": message}
            if receipt["status"] != "applied":
                db.execute("ROLLBACK TO remote_change")
            db.execute("RELEASE remote_change")
            db.execute("INSERT INTO cloud_receipts VALUES (?, ?)", (op_id, json.dumps(receipt, ensure_ascii=False)))
            return receipt

    def remote_receipts(self, limit=500):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("同步回执数量不正确")
        with self.connect() as db:
            rows = db.execute("SELECT body FROM cloud_receipts ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

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
            db.execute("BEGIN IMMEDIATE")
            # A fingerprint may describe multiple real exams. Check the whole
            # existing database and whole incoming batch before migrating any
            # item, so input order cannot silently bind an ambiguous identity.
            canonical_keys = {}
            existing_items = [json.loads(row[0]) for row in db.execute("SELECT body FROM tasks")]
            for candidate in existing_items + prepared:
                fingerprint = _canonical_chaoxing_exam_fingerprint(candidate)
                if fingerprint:
                    canonical_keys.setdefault(fingerprint, set()).add(candidate["source_key"])
            for item in prepared:
                migrated_overrides = {}
                migrated_deleted = False
                key = item["id"] if item["source"] == "physics" else stable_key(item)
                row = db.execute("SELECT id, body FROM tasks WHERE dedup=?", (key,)).fetchone()
                fingerprint = _canonical_chaoxing_exam_fingerprint(item)
                if fingerprint and len(canonical_keys[fingerprint]) == 1:
                    old = db.execute("SELECT id, body FROM tasks WHERE dedup=?",
                                     ("chaoxing:" + fingerprint,)).fetchone()
                    if old:
                        legacy = json.loads(old[1])
                        duplicate = json.loads(row[1]) if row else None
                        notes = _preserved_notes(legacy.get("notes", ""),
                                                 duplicate.get("notes", "") if duplicate else "",
                                                 item.get("notes", ""))
                        if (legacy.get("source_key") == fingerprint and _same_exam_metadata(legacy, item)
                                and (duplicate is None or _same_exam_metadata(duplicate, item))):
                            completed = (item["status"] == "completed" or legacy["status"] == "completed"
                                         or (duplicate is not None and duplicate["status"] == "completed"))
                            if notes is None:
                                # Keeping both records protects over-limit user
                                # notes. Their exact identity is still proven,
                                # so a completed exam must not leave an old
                                # pending reminder behind.
                                if completed:
                                    item["status"] = "completed"
                                    if legacy["status"] != "completed":
                                        previous, legacy_key, legacy_overrides = self._task_record(db, old[0])
                                        legacy["status"] = "completed"
                                        self._write_task(db, legacy, legacy_key, previous, legacy_overrides,
                                                         deleted=previous["deleted"])
                            else:
                                # Keep the original task/calendar identity.
                                # Remove an exact duplicate in this transaction
                                # before moving its unique dedup key.
                                previous, _, migrated_overrides = self._task_record(db, old[0])
                                migrated_deleted = previous["deleted"]
                                if row:
                                    duplicate_task, _, duplicate_overrides = self._task_record(db, row[0])
                                    migrated_overrides = {**duplicate_overrides, **migrated_overrides}
                                    migrated_deleted = migrated_deleted or duplicate_task["deleted"]
                                    db.execute("DELETE FROM tasks WHERE id=?", (row[0],))
                                    db.execute("DELETE FROM task_state WHERE id=?", (row[0],))
                                db.execute("UPDATE tasks SET dedup=? WHERE id=?", (key, old[0]))
                                legacy["notes"] = notes
                                if "notes" in migrated_overrides:
                                    migrated_overrides["notes"] = notes
                                if completed:
                                    legacy["status"] = "completed"
                                if not legacy.get("calendar_id") and duplicate:
                                    legacy["calendar_id"] = duplicate.get("calendar_id")
                                row = (old[0], json.dumps(legacy, ensure_ascii=False))
                if row:
                    existing = json.loads(row[1])
                    previous, _, overrides = self._task_record(db, row[0])
                    overrides.update(migrated_overrides)
                    if previous["deleted"] or migrated_deleted:
                        # The source may keep advertising an item after it was
                        # deleted locally. Retain its identity as a tombstone.
                        result.append(self._write_task(db, previous, key, previous, overrides, deleted=True))
                        continue
                    # A stale import must not undo a user's completed state.
                    item["id"] = row[0]
                    source_completed = item["status"] == "completed"
                    if existing["status"] == "completed":
                        item["status"] = "completed"
                    if fingerprint:
                        # Keep user notes on later reimports after the legacy
                        # key has disappeared; repeating a sync is idempotent.
                        item["notes"] = _preserved_notes(existing.get("notes", ""), item["notes"])
                        if item["notes"] is None:
                            item["notes"] = existing.get("notes", "")
                    item["calendar_id"] = existing.get("calendar_id")
                    item = _apply_task_overrides(item, previous, overrides)
                    # A positive submission result always cancels reminders,
                    # including after a user previously reopened the task.
                    if existing["status"] == "completed" or source_completed:
                        item["status"] = "completed"
                    result.append(self._write_task(db, item, key, previous, overrides))
                else:
                    result.append(self._write_task(db, item, key))
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


def to_ics(tasks, platform_synced_at=None, platform_sync_attempted_at=None, platform_sync_result=None):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Study Deadline//CN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:作业与考试"]
    for item in tasks:
        if item["status"] != "pending":
            continue
        anchor = task_anchor(item)
        if anchor is None:
            # Keep the task in the application until its start is confirmed;
            # exporting an exam at its end would create the wrong reminders.
            continue
        start = anchor.astimezone(timezone.utc)
        due = parse_datetime(item["due_at"]).astimezone(timezone.utc)
        end = due if item.get("kind") == "exam" and due > start else start + timedelta(minutes=15)
        description = item.get("notes", "")
        metadata = []
        if platform_synced_at:
            metadata.append("平台最近成功同步：" + platform_synced_at)
        if platform_sync_attempted_at:
            metadata.append("平台最近尝试同步：" + platform_sync_attempted_at)
            if platform_sync_result in {"never", "running", "success", "partial", "failed", "scope_changed", "interrupted"}:
                metadata.append("平台同步结果：" + platform_sync_result)
        description = "\n".join(metadata + ([description] if description else []))
        if item.get("kind") == "lecture":
            description += "\n类型：lecture\n上课时间：" + parse_datetime(item["due_at"]).strftime("%Y-%m-%d %H:%M 北京时间")
            description += "\n仅记录开课时间；日历事件时长为提醒占位，不代表课长。"
        elif item.get("kind") == "exam":
            description += "\n考试开始：" + anchor.strftime("%Y-%m-%d %H:%M 北京时间")
            description += "\n考试结束：" + parse_datetime(item["due_at"]).strftime("%Y-%m-%d %H:%M 北京时间")
            description += "\n提醒基准：考试开始"
        lines += ["BEGIN:VEVENT", "UID:" + item["id"] + "@study-deadline.local", "DTSTAMP:" + stamp,
                  "DTSTART:" + start.strftime("%Y%m%dT%H%M%SZ"),
                  "DTEND:" + end.strftime("%Y%m%dT%H%M%SZ"),
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
