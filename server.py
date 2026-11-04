#!/usr/bin/env python3
"""Local homework dashboard. No external service or account database required."""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import re
import secrets
import signal
import socket
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from studyreminder.calendar_sync import CalendarSync
from studyreminder.collector import BrowserCollector
from studyreminder.core import Store, TZ, now_iso, to_ics
from studyreminder.scheduling import daily_due, reminder_recheck_keys
from studyreminder.sync_status import platform_synced_at, sync_info, sync_signature

ROOT = Path(__file__).resolve().parent
PLATFORM_SOURCES = frozenset({"chaoxing", "xiji"})
MAX_AUTOMATIC_ATTEMPTS = 3


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))  # Routing lookup only; no datagram is sent.
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class Application:
    def __init__(self, data_dir, lan=False, port=8765):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.store = Store(self.data_dir / "tasks.sqlite3")
        self.store.patch_settings({"collection": {"interval_minutes": 1440}})
        self.calendar = CalendarSync(ROOT, self.store)
        self.collector = BrowserCollector(self.data_dir)
        self.lan, self.port, self.ip = lan, port, local_ip()
        self.token = self.store.get_value("pairing_token")
        if not self.token:
            self.token = secrets.token_urlsafe(32)
            self.store.set_value("pairing_token", self.token)
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1", self.ip}
        self.stop = threading.Event()
        self.last_scan = 0.0
        self.retry_lock = threading.RLock()
        self.active_sync_attempts = set()
        self.reminder_checked = set(self.store.get_value("reminder_checked", []))

    def state(self):
        tasks = self.store.tasks()
        confirmed = {(t["source"], t.get("source_key")) for t in tasks if t.get("source_key")}
        pending = [t for t in self.collector.last_candidates if t["status"] != "completed" and (t["source"], t.get("source_key")) not in confirmed]
        return {"tasks": tasks, "settings": self.store.settings(),
                "platforms": self.collector.status, "calendar_sync": self.calendar.info(),
                "collection": {"pending_count": len(pending), "candidates": pending,
                               "completed_count": sum(t["status"] == "completed" for t in self.collector.last_candidates),
                               "warnings": [re.sub(r"（[A-Za-z0-9.-]+/[^）]*）", "", v) for v in self.collector.last_warnings],
                               "mode": "daily_sync", "last_scan": self.store.get_value("last_collection_scan"),
                               "session_warning": self.collector.session_warning,
                               "daily_last_success": self.store.get_value("daily_last_success"),
                               "daily_last_attempt": self.store.get_value("daily_last_attempt"),
                               "sync": self.sync_metadata(),
                               "automatic_retry": self.store.get_value("collection_retry", {}),
                               "last_error": self.store.get_value("collection_last_error"),
                               "message": "按设置的课程范围每日自动同步；提醒前额外核对提交状态，已提交不提醒"},
                "connection": {"mode": "lan" if self.lan else "local_only",
                               "url": f"http://{self.ip}:{self.port}/?token={self.token}" if self.lan else None},
                "server_time": now_iso(), "timezone": "Asia/Shanghai"}

    def background(self):
        # Calendar alarms live in iCloud, not in this timer. This only materializes
        # future weekly occurrences and retries pending calendar changes.
        while not self.stop.wait(60):
            try:
                self.store.ensure_physics()
                self.calendar.sync_if_enabled()
                if self.collector.thread and self.collector.thread.is_alive():
                    self.collector.request("save_session")
                self.automatic_collection()
            except Exception as exc:
                self.store.set_value("collection_last_error", {"at": now_iso(), "message": str(exc)})

    @staticmethod
    def configured_sources(scope):
        return {source for source, fields in {
            "chaoxing": ("chaoxing_homework", "chaoxing_exams"), "xiji": ("xiji",)
        }.items() if any(scope.get(field) for field in fields)}

    def _sync_record(self):
        record = self.store.get_value("collection_sync")
        if not isinstance(record, dict):
            record = {"version": 1, "last_attempt_at": None, "last_success_at": None,
                      "last_result": "never", "sources": {}}
            # Older versions saved the completion of a fully covered read, but
            # did not save its course configuration. Preserve that history as
            # an unknown scope instead of claiming today's scope was verified.
            old = self.store.get_value("last_collection_result", {})
            old_at = self.store.get_value("last_collection_scan")
            try:
                valid_at = isinstance(old_at, str) and datetime.fromisoformat(old_at).tzinfo is not None
            except ValueError:
                valid_at = False
            observed_read = (isinstance(old, dict) and any(
                isinstance(outcome, dict) and outcome.get("auth_status") not in {None, "disabled"}
                for outcome in old.get("source_results", {}).values()))
            if valid_at and observed_read:
                record.update(last_attempt_at=old_at, last_result="partial")
                old_verified = {source for source, outcome in old.get("source_results", {}).items()
                                if source in PLATFORM_SOURCES and self.verified_source(outcome)}
                configured = self.configured_sources(self.store.settings()["collection"]["course_scope"])
                if (isinstance(old, dict) and old.get("coverage_complete") is True and
                        configured and configured <= old_verified):
                    record.update(last_success_at=old_at, last_result="success")
                if isinstance(old, dict):
                    for source, outcome in old.get("source_results", {}).items():
                        if source in PLATFORM_SOURCES and self.verified_source(outcome):
                            record["sources"][source] = {"last_attempt_at": old_at, "last_success_at": old_at,
                                                         "last_result": "success"}
            self.store.set_value("collection_sync", record)
        interrupted = False
        for item in [record, *record.get("sources", {}).values()]:
            if item.get("last_result") == "running" and item.get("active_attempt") not in self.active_sync_attempts:
                item["last_result"] = "interrupted"
                item.pop("active_attempt", None)
                interrupted = True
        if interrupted:
            self.store.set_value("collection_sync", record)
        return record

    @staticmethod
    def verified_source(outcome):
        return (isinstance(outcome, dict) and outcome.get("coverage_complete") is True
                and outcome.get("auth_status") == "authenticated" and not outcome.get("deferred_reason"))

    def sync_metadata(self):
        """Expose actual platform reads, independent of login probes and timers."""
        with self.retry_lock:
            self._sync_record()
            return sync_info(self.store)

    def begin_sync(self, sources=None):
        with self.retry_lock:
            collection = self.store.settings()["collection"]
            scope = collection["course_scope"]
            active = self.configured_sources(scope) & (PLATFORM_SOURCES if sources is None else set(sources))
            generations = self.store.get_value("collection_retry", {}).get("auth_generations", {})
            attempt = {"id": secrets.token_hex(8), "at": now_iso(), "config": collection, "sources": sorted(active),
                       "auth_generations": {source: generations.get(source, 0) for source in active}}
            record = self._sync_record()
            if not active:
                return attempt
            self.active_sync_attempts.add(attempt["id"])
            record.update(last_attempt_at=attempt["at"], last_result="running", active_attempt=attempt["id"],
                          attempt_scope=sync_signature(collection))
            for source in active:
                item = record.setdefault("sources", {}).setdefault(source, {})
                item.update(last_attempt_at=attempt["at"], last_result="running", active_attempt=attempt["id"],
                            attempt_scope=sync_signature(collection, source))
            self.store.set_value("collection_sync", record)
            return attempt

    def finish_sync(self, attempt, result=None, failed=False):
        """Failure never erases a previous verified time or existing task data."""
        with self.retry_lock:
            record = self._sync_record()
            self.active_sync_attempts.discard(attempt["id"])
            if not attempt["sources"] or record.get("active_attempt") != attempt["id"]:
                return False
            collection = self.store.settings()["collection"]
            scope = collection["course_scope"]
            changed = sync_signature(collection) != sync_signature(attempt["config"])
            result = result or {}
            outcomes = result.get("source_results", {})
            generations = self.store.get_value("collection_retry", {}).get("auth_generations", {})
            verified = {source for source in attempt["sources"]
                        if not failed and not changed and self.verified_source(outcomes.get(source))
                        and generations.get(source, 0) == attempt["auth_generations"].get(source, 0)}
            finished_at = now_iso()
            for source in attempt["sources"]:
                item = record["sources"][source]
                if item.get("active_attempt") != attempt["id"]:
                    continue
                if source in verified:
                    item.update(last_success_at=finished_at, success_scope=sync_signature(collection, source), last_result="success")
                else:
                    item["last_result"] = "scope_changed" if changed else ("failed" if failed else "partial")
                item.pop("active_attempt", None)
            full = bool(verified and self.configured_sources(scope) <= verified and result.get("coverage_complete") is True)
            if full:
                record.update(last_success_at=finished_at, success_scope=sync_signature(collection), last_result="success")
            else:
                record["last_result"] = "scope_changed" if changed else ("failed" if failed else "partial")
            record.pop("active_attempt", None)
            self.store.set_value("collection_sync", record)
            return bool(verified)

    def complete_daily_sync(self, collection, completed_sources):
        """A daily read may verify healthy platforms in separate attempts."""
        with self.retry_lock:
            current = self.store.settings()["collection"]
            configured = self.configured_sources(current["course_scope"])
            if sync_signature(current) != sync_signature(collection) or not configured or not configured <= completed_sources:
                return False
            record = self._sync_record()
            items = record.get("sources", {})
            if not all(items.get(source, {}).get("last_success_at") and
                       items[source].get("success_scope") == sync_signature(current, source) for source in configured):
                return False
            record.update(last_success_at=now_iso(), success_scope=sync_signature(current), last_result="success")
            self.store.set_value("collection_sync", record)
            return True

    def retry_state(self, now, daily_time):
        """Persistent attempt budgets survive service restarts and laptop sleep."""
        state = self.store.get_value("collection_retry", {})
        daily = state.setdefault("daily", {})
        day = now.astimezone(TZ).date().isoformat()
        if daily.get("date") != day or daily.get("time") != daily_time:
            state["daily"] = {"date": day, "time": daily_time, "attempts": 0, "completed_sources": []}
        state.setdefault("reminders", {})
        state.setdefault("blocked_sources", {})
        state.setdefault("auth_generations", {})
        return state

    @staticmethod
    def retry_available(record, now):
        if record.get("attempts", 0) >= MAX_AUTOMATIC_ATTEMPTS:
            return False
        try:
            next_at = datetime.fromisoformat(record["next_attempt_at"])
        except (KeyError, TypeError, ValueError):
            return True
        return next_at.tzinfo is not None and now >= next_at

    def refresh_auth_retry(self, source, auth_status=None, explicit=False, expected_generation=None):
        """Only genuine login failure pauses a platform; a user login can resume it."""
        if source not in PLATFORM_SOURCES:
            raise ValueError("请选择学习通或希冀平台")
        with self.retry_lock:
            cfg = self.store.settings()["daily"]
            state = self.store.get_value("collection_retry", {}) or self.retry_state(datetime.now(TZ), cfg["time"])
            generations = state.setdefault("auth_generations", {})
            if expected_generation is not None and generations.get(source, 0) != expected_generation:
                return False
            if explicit:
                generations[source] = generations.get(source, 0) + 1
            blocked = state["blocked_sources"]
            if auth_status == "not_authenticated":
                blocked[source] = {"at": now_iso(), "reason": "not_authenticated"}
            elif explicit or auth_status == "authenticated":
                blocked.pop(source, None)
                # An explicit repair is allowed to retry immediately. Normal
                # successful reads do not reset a failing daily attempt budget.
                if explicit:
                    self.store.set_value("daily_last_success", None)
                    state["daily"].update(attempts=0, next_attempt_at=None)
                    state["daily"]["completed_sources"] = [s for s in state["daily"].get("completed_sources", []) if s != source]
                    task_ids = {t["id"] for t in self.store.tasks() if t.get("source") == source}
                    state["reminders"] = {k: v for k, v in state["reminders"].items() if k.split("|", 1)[0] not in task_ids}
            self.store.set_value("collection_retry", state)
            return True

    def automatic_collection(self, now=None):
        """At most three attempts per day and per reminder, never an infinite loop."""
        now = now or datetime.now(TZ)
        cfg = self.store.settings()["daily"]
        if not cfg["enabled"]:
            return None
        collection_cfg = self.store.settings()["collection"]
        scope = collection_cfg["course_scope"]
        configured = self.configured_sources(scope)
        tasks = self.store.tasks()
        task_sources = {t["id"]: t.get("source") for t in tasks}
        with self.retry_lock:
            state = self.retry_state(now, cfg["time"])
            signature = json.dumps(scope, sort_keys=True, ensure_ascii=True)
            if state["daily"].get("scope") != signature:
                state["daily"].update(scope=signature, attempts=0, completed_sources=[], next_attempt_at=None)
            blocked = set(state["blocked_sources"])
            daily = state["daily"]
            due = daily_due(now, cfg["time"], self.store.get_value("daily_last_success"))
            daily_sources = configured - set(daily.get("completed_sources", [])) - blocked
            daily_attempt = bool(due and daily_sources and self.retry_available(daily, now))
            keys = reminder_recheck_keys(tasks, now, self.reminder_checked)
            checks = {key for key in keys if task_sources.get(key.split("|", 1)[0]) in configured - blocked
                      and self.retry_available(state["reminders"].get(key, {}), now)}
            sources = (daily_sources if daily_attempt else set()) | {
                task_sources[key.split("|", 1)[0]] for key in checks}
            if not sources:
                self.store.set_value("collection_retry", state)
                return None
            auth_generations = {source: state["auth_generations"].get(source, 0) for source in sources}
            # Commit before network I/O: interruption/restart consumes an
            # attempt rather than silently restoring an unlimited retry loop.
            if daily_attempt:
                daily["attempts"] = daily.get("attempts", 0) + 1
                delay = (5, 30, 30)[daily["attempts"] - 1]
                daily["next_attempt_at"] = (now + timedelta(minutes=delay)).isoformat()
            for key in checks:
                record = state["reminders"].setdefault(key, {})
                record["attempts"] = record.get("attempts", 0) + 1
                record["next_attempt_at"] = (now + timedelta(minutes=min(record["attempts"], 2))).isoformat()
            # Keep current reminder budgets and recent records for restarts.
            state["reminders"] = dict(list(state["reminders"].items())[-1000:])
            self.store.set_value("collection_retry", state)
        sync_attempt = self.begin_sync(sources)
        try:
            self.ensure_collection(sources)
            result = self.scan(update_existing=True, import_new=True, sources=sources,
                               auth_generations=auth_generations, _sync_attempt=sync_attempt)
        except Exception as exc:
            self.finish_sync(sync_attempt, failed=True)
            self.store.set_value("collection_last_error", {"at": now_iso(), "message": str(exc)})
            return {"coverage_complete": False, "error": True}
        outcomes = result.get("source_results", {})
        complete = {s for s in sources if self.verified_source(outcomes.get(s))}
        sync_completed = False
        with self.retry_lock:
            state = self.retry_state(now, cfg["time"])
            # The scan has already applied authentication outcomes atomically.
            # A login repaired after its return must not be reblocked here, or
            # have its fresh retry budget consumed by an older completion.
            stale = {source for source in sources
                     if state["auth_generations"].get(source, 0) != auth_generations[source]}
            complete -= stale
            if stale:
                result["coverage_complete"] = False
                result["source_results"] = {**result.get("source_results", {}), **{
                    source: {"coverage_complete": False, "auth_status": "unverified",
                             "deferred_reason": "session_changed_during_scan"} for source in stale}}
            # A healthy platform is not repeatedly reread because another
            # platform's layout or session needs attention.
            if due:
                state["daily"]["completed_sources"] = sorted(set(state["daily"].get("completed_sources", [])) | complete)
                if configured and configured <= set(state["daily"]["completed_sources"]):
                    self.store.set_value("daily_last_success", now.isoformat())
                    self.store.set_value("collection_last_error", None)
                    sync_completed = self.complete_daily_sync(collection_cfg, set(state["daily"]["completed_sources"]))
            self.reminder_checked.update(key for key in checks if task_sources.get(key.split("|", 1)[0]) in complete)
            self.store.set_value("reminder_checked", sorted(self.reminder_checked)[-1000:])
            self.store.set_value("collection_retry", state)
        if sync_completed:
            self.calendar.sync_if_enabled()
        return result

    def scan(self, update_existing=False, import_new=False, sources=None, auth_generations=None, _sync_attempt=None):
        attempt = _sync_attempt or self.begin_sync(sources)
        try:
            result = self._scan(update_existing, import_new, sources, auth_generations)
        except Exception:
            self.finish_sync(attempt, failed=True)
            raise
        if self.finish_sync(attempt, result):
            # Even unchanged assignments need a new freshness timestamp in
            # calendar notes and read-only phone widgets.
            self.calendar.sync_if_enabled()
        return result

    def _scan(self, update_existing=False, import_new=False, sources=None, auth_generations=None):
        self.last_scan = time.monotonic()
        self.store.set_value("daily_last_attempt", now_iso())
        cfg = self.store.settings()["collection"]
        year = cfg.get("homework_year_binding") or cfg.get("homework_year")
        confirmed_year = year.get("year") if isinstance(year, dict) else year
        if confirmed_year != datetime.now(TZ).year:
            year = None
        scope = dict(cfg["course_scope"])
        omitted = PLATFORM_SOURCES - sources if sources is not None else set()
        with self.retry_lock:
            current_generations = self.store.get_value("collection_retry", {}).get("auth_generations", {})
            auth_generations = (dict(auth_generations) if auth_generations is not None else
                                {source: current_generations.get(source, 0) for source in PLATFORM_SOURCES - omitted})
        for source in omitted:
            for field in (("chaoxing_homework", "chaoxing_exams") if source == "chaoxing" else ("xiji",)):
                scope[field] = []
        result = self.collector.request("scan", {"homework_year": year, "course_scope": scope,
                                                "preserve_disabled_status": sorted(omitted)})
        def discard_replaced_sessions():
            current_generations = self.store.get_value("collection_retry", {}).get("auth_generations", {})
            stale = {source for source, generation in auth_generations.items()
                     if current_generations.get(source, 0) != generation}
            if stale:
                result["coverage_complete"] = False
                result["source_results"] = {**result.get("source_results", {}), **{
                    source: {"coverage_complete": False, "auth_status": "unverified",
                             "deferred_reason": "session_changed_during_scan"} for source in stale}}
                result["candidates"] = [item for item in result.get("candidates", []) if item.get("source") not in stale]
                result["status_updates"] = [item for item in result.get("status_updates", []) if item.get("source") not in stale]
                if "chaoxing" in stale:
                    result.pop("homework_year_binding", None)
        with self.retry_lock:
            discard_replaced_sessions()
        for source, outcome in result.get("source_results", {}).items():
            if source not in omitted:
                self.refresh_auth_retry(source, outcome.get("auth_status"),
                                        expected_generation=auth_generations.get(source))
        current = self.store.settings()["collection"]
        if any(current.get(k) != cfg.get(k) for k in ("course_scope", "homework_year")):
            raise ValueError("读取期间课程设置已改变，请按新范围重新同步")
        if result.get("homework_year_binding"):
            self.store.patch_settings({"collection": {"homework_year_binding": result["homework_year_binding"]}})
        self.store.set_value("last_collection_scan", now_iso())
        tasks_before = {t["id"]: t for t in self.store.tasks()}
        known = {(t["source"], t.get("source_key")): t for t in tasks_before.values() if t.get("source_key")}
        completed = 0
        for update in result.get("status_updates", []):
            existing = known.get((update.get("source"), update.get("source_key")))
            if existing and update.get("status") == "completed" and existing["status"] != "completed":
                self.store.put_task({"status": "completed"}, existing["id"])
                completed += 1
        if completed:
            self.calendar.sync_if_enabled()
        if update_existing:
            verified = {(t["source"], t.get("source_key")) for t in self.store.tasks() if t["source"] in {"chaoxing", "xiji"} and t.get("source_key") and not t["source_key"].startswith("page:")}
            updates = [t for t in result["candidates"] if (t["source"], t.get("source_key")) in verified]
            if updates:
                self.store.import_tasks(updates)
                self.calendar.sync_if_enabled()
        imported_ids = set()
        if import_new:
            imports = [t for t in result["candidates"] if t.get("source_key") and not t["source_key"].startswith("page:")]
            if imports:
                imported_ids.update(t["id"] for t in self.store.import_tasks(imports))
                self.calendar.sync_if_enabled()
        # A verified exam ID can replace an older list fingerprint while
        # preserving its local task ID. Report actual task transitions rather
        # than counting that identity upgrade as a new arrangement.
        tasks_after = {t["id"]: t for t in self.store.tasks()}
        added = len(imported_ids - tasks_before.keys())
        completed = sum(t["status"] != "completed" and tasks_after.get(task_id, {}).get("status") == "completed"
                        for task_id, t in tasks_before.items())
        full_scope = sources is None or self.configured_sources(cfg["course_scope"]) <= sources
        with self.retry_lock:
            discard_replaced_sessions()
            if result.get("coverage_complete") and full_scope:
                self.store.set_value("daily_last_success", now_iso())
                self.store.set_value("collection_last_error", None)
            else:
                login_required = any(outcome.get("auth_status") == "not_authenticated"
                                     for outcome in result.get("source_results", {}).values())
                self.store.set_value("collection_last_error", {
                    "at": now_iso(), "message": "本次未能完整读取所选课程；已确认的提交状态仍已更新。"
                    + ("有平台登录已失效，请在对应平台重新登录。" if login_required else
                       "程序会继续使用保存的会话有限重试，具体原因见平台检查结果。")})
        self.store.set_value("last_collection_result", result)
        # Completed platform records remain available for matching/cancellation,
        # but are not presented as new arrangements that need reminders.
        return {**result, "coverage_complete": bool(result.get("coverage_complete") and full_scope),
                "candidates": [] if import_new else [t for t in result["candidates"] if t["status"] != "completed"],
                "completed": completed, "synced": len(result["candidates"]), "added": added}

    def ensure_collection(self, sources=PLATFORM_SOURCES):
        for source in sorted(sources):
            self.collector.request("restore", source)

    def resume_sessions(self):
        if not self.store.settings()["daily"]["enabled"] or not (self.data_dir / "browser-session.json").exists():
            return
        for source in ("chaoxing", "xiji"):
            try:
                self.collector.request("restore", source)
            except Exception:
                continue
        # The regular scheduler owns scans and its persisted retry budget.
        # Service restarts must never trigger an extra, unbudgeted full scan.


class Handler(BaseHTTPRequestHandler):
    server_version = "StudyDeadline/1.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # URLs can contain pairing tokens or personal task names.
        if len(args) >= 2:
            print(f"HTTP {self.command} {urlsplit(self.path).path} {args[1]}")

    def send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode() if content_type.startswith("application/json") else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authorized(self):
        host = urlsplit("http://" + self.headers.get("Host", "")).hostname
        if host not in self.app.allowed_hosts:
            self.send(403, {"error": "不接受此主机地址"})
            return False
        if self.client_address[0] in {"127.0.0.1", "::1"}:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        candidate = cookie.get("study_pair")
        if candidate and hmac.compare_digest(candidate.value, self.app.token):
            return True
        self.send(401, "请在 Mac 中打开本程序的“手机访问链接”后，用同一 Wi-Fi 的 Safari 访问。", "text/plain; charset=utf-8")
        return False

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 JSON")
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            raise PermissionError("拒绝跨站修改")
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 2_000_000:
            raise ValueError("导入文件过大，最大 2 MB")
        value = json.loads(self.rfile.read(size) if size else b"{}")
        if not isinstance(value, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return value

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            parts = urlsplit(self.path)
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            candidate = parse_qs(parts.query).get("token", [""])[0]
            if parts.path == "/" and candidate and host in self.app.allowed_hosts:
                if hmac.compare_digest(candidate, self.app.token):
                    self.send(303, b"", extra={"Location": "/", "Set-Cookie": "study_pair=" + self.app.token + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
                    return
            if not self.authorized():
                return
            if parts.path == "/api/state":
                self.send(200, self.app.state())
            elif parts.path == "/api/export":
                self.send(200, {"version": 1, "timezone": "Asia/Shanghai", "tasks": self.app.store.tasks()},
                          extra={"Content-Disposition": 'attachment; filename="study-deadlines.json"'})
            elif parts.path == "/calendar.ics":
                self.send(200, to_ics(self.app.store.tasks(), **self.app.calendar.platform_metadata()), "text/calendar; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="study-deadlines.ics"'})
            else:
                relative = unquote(parts.path).lstrip("/") or "index.html"
                file = (ROOT / "web" / relative).resolve()
                if not file.is_relative_to(ROOT / "web") or not file.is_file():
                    self.send(404, {"error": "页面不存在"})
                    return
                mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                self.send(200, file.read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") else ""))
        except (ValueError, TypeError) as exc:
            self.send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def mutate(self):
        try:
            if not self.authorized():
                return
            raw = self.read_json() if self.command != "DELETE" else self.read_json()
            path = urlsplit(self.path).path
            result, changed = None, False
            if self.command == "POST" and path == "/api/tasks":
                result, changed = self.app.store.put_task(raw), True
            elif path.startswith("/api/tasks/"):
                task_id = unquote(path[len("/api/tasks/"):])
                if self.command == "PUT":
                    result, changed = self.app.store.put_task(raw, task_id), True
                elif self.command == "DELETE":
                    self.app.store.delete_task(task_id)
                    result, changed = {"ok": True}, True
            elif self.command == "PUT" and path == "/api/settings":
                self.app.store.update_settings(raw)
                result, changed = self.app.state(), True
            elif self.command == "POST" and path == "/api/import":
                items = self.app.store.import_tasks(raw.get("tasks"))
                if any(t["source"] in {"chaoxing", "xiji"} and t.get("source_key") and not t["source_key"].startswith("page:") for t in items):
                    self.app.store.patch_settings({"collection": {"auto_scan": True}})
                result, changed = {"imported": len(items), "tasks": items}, True
            elif self.command == "POST" and path == "/api/calendar/sync":
                result = self.app.calendar.sync()
            elif self.command == "POST" and path == "/api/collection/open":
                source = raw.get("source")
                if source not in PLATFORM_SOURCES:
                    raise ValueError("请选择学习通或希冀平台")
                result = self.app.collector.request("open", source)
                self.app.refresh_auth_retry(source, explicit=True)
            elif self.command == "POST" and path in {"/api/collection/probe", "/api/collection/finish-login"}:
                source = raw.get("source")
                if source not in PLATFORM_SOURCES:
                    raise ValueError("请选择学习通或希冀平台")
                action = "probe" if path.endswith("/probe") else "finish_login"
                result = self.app.collector.request(action, source)
                self.app.refresh_auth_retry(source, result.get("status"),
                                            explicit=result.get("authenticated") is True)
            elif self.command == "POST" and path == "/api/collection/scan":
                result = self.app.scan(update_existing=True, import_new=True)
            elif self.command == "POST" and path == "/api/collection/inspect":
                result = self.app.collector.request("inspect")
            if result is None:
                self.send(404, {"error": "接口不存在"})
                return
            if changed:
                self.app.calendar.sync_if_enabled()
            self.send(200, result)
        except PermissionError as exc:
            self.send(403, {"error": str(exc)})
        except KeyError:
            self.send(404, {"error": "任务不存在"})
        except (ValueError, TypeError) as exc:
            self.send(400, {"error": str(exc)})
        except TimeoutError:
            self.send(504, {"error": "操作超时，请回到 Mac 检查浏览器或日历授权"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send(500, {"error": "本地操作未完成，请重试；已有任务仍保存在本机"})

    do_POST = mutate
    do_PUT = mutate
    do_DELETE = mutate


def main():
    parser = argparse.ArgumentParser(description="作业与考试提醒 · 本地第一版")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lan", action="store_true", help="允许同一 Wi-Fi 的设备通过配对链接访问")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    app = Application(args.data_dir, args.lan, args.port)
    server = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.app = app
    threading.Thread(target=app.background, daemon=True, name="weekly-calendar-maintenance").start()
    threading.Thread(target=app.resume_sessions, daemon=True, name="resume-saved-platform-sessions").start()
    print(f"作业提醒已启动：http://127.0.0.1:{args.port}", flush=True)
    if args.lan:
        print("同一 Wi-Fi 的手机/平板请使用界面设置中的“手机访问链接”。", flush=True)
    print("停止：Ctrl+C。首次日历同步需点击界面的同步按钮并允许日历访问。", flush=True)
    def stop(signum, frame):
        app.stop.set()
        app.collector.close()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        app.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
