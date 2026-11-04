from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path

from .core import now_iso
from .scheduling import REMINDER_HOURS
from .sync_status import sync_signature

EVENT_LAYOUT_VERSION = 2


class CalendarSync:
    def __init__(self, root, store):
        self.root, self.store = Path(root), store
        self.lock = threading.Lock()
        self.helper = self.root / "build/CalendarBridge.app/Contents/MacOS/CalendarBridge"

    def info(self):
        value = self.store.get_value("calendar_sync", {"last_run": None, "message": "尚未授权日历"})
        success = value.get("last_success_at") or (value.get("last_run") if value.get("ok") is True else None)
        return {"available": self.helper.exists(), "needs_sync": self.signature(self.store.tasks(), **self.platform_metadata()) != self.store.get_value("synced_signature"), **value, "last_success_at": success}

    def platform_metadata(self):
        record = self.store.get_value("collection_sync", {})
        scope = sync_signature(self.store.settings()["collection"])
        return {"platform_synced_at": record.get("last_success_at") if record.get("success_scope") == scope else None,
                "platform_sync_attempted_at": record.get("last_attempt_at") if record.get("attempt_scope") == scope else None,
                "platform_sync_result": record.get("last_result") if record.get("attempt_scope") == scope else None}

    @staticmethod
    def signature(tasks, platform_synced_at=None, platform_sync_attempted_at=None, platform_sync_result=None):
        # A reminder-policy change must migrate existing calendar events even
        # when no task or deadline changed in the local database.
        payload = {"tasks": tasks, "event_layout_version": EVENT_LAYOUT_VERSION,
                   "reminder_hours": list(REMINDER_HOURS), "platform_synced_at": platform_synced_at,
                   "platform_sync_attempted_at": platform_sync_attempted_at, "platform_sync_result": platform_sync_result}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def record(self, value):
        previous = self.info()
        self.store.set_value("calendar_sync", {**value, "last_success_at": value.get("last_run") if value.get("ok") is True else previous.get("last_success_at")})

    def sync(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError("日历正在同步，请稍候")
        try:
            if not self.helper.exists():
                raise ValueError("日历组件尚未编译，请运行 scripts/build_calendar_bridge.sh")
            settings = self.store.settings()
            metadata = self.platform_metadata()
            payload = {"tasks": self.store.tasks(), **metadata, "calendar": {
                **settings["calendar"], "event_identifiers": self.store.get_value("event_identifiers", {})
            }}
            try:
                result = subprocess.run([str(self.helper)], input=json.dumps(payload, ensure_ascii=False),
                                        text=True, capture_output=True, timeout=120, cwd=self.root)
            except subprocess.TimeoutExpired as exc:
                raise ValueError("等待日历授权超时，请在系统设置中允许 CalendarBridge 完整访问日历后重试") from exc
            try:
                value = json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError) as exc:
                raise ValueError("日历组件未返回结果，请检查系统设置 → 隐私与安全性 → 日历") from exc
            if not value.get("ok"):
                self.record({"last_run": now_iso(), "ok": False, "message": value.get("message", "日历同步失败")})
                return value
            self.store.patch_settings({"calendar": {"identifier": value.get("calendar_identifier"), "auto_sync": True}})
            if "event_identifiers" in value:
                self.store.set_value("event_identifiers", value["event_identifiers"])
            self.record({"last_run": now_iso(), **value})
            self.store.set_value("synced_signature", self.signature(payload["tasks"], **metadata))
            return value
        except Exception as exc:
            self.record({"last_run": now_iso(), "ok": False, "message": str(exc)})
            raise
        finally:
            self.lock.release()

    def sync_if_enabled(self):
        if self.store.settings()["calendar"].get("auto_sync") and not self.lock.locked() and self.signature(self.store.tasks(), **self.platform_metadata()) != self.store.get_value("synced_signature"):
            def work():
                try:
                    self.sync()
                except Exception as exc:
                    self.record({"last_run": now_iso(), "ok": False, "message": str(exc)})
            threading.Thread(target=work, daemon=True, name="calendar-sync").start()
