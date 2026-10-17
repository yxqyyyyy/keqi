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
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from studyreminder.calendar_sync import CalendarSync
from studyreminder.collector import BrowserCollector
from studyreminder.core import Store, TZ, now_iso, to_ics
from studyreminder.scheduling import daily_due, reminder_recheck_keys

ROOT = Path(__file__).resolve().parent


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
        self.next_scan_retry = 0.0
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
                               "last_error": self.store.get_value("collection_last_error"),
                               "message": "按设置的课程范围每日自动同步；提醒前额外核对提交状态，已提交不提醒"},
                "connection": {"mode": "lan" if self.lan else "local_only",
                               "url": f"http://{self.ip}:{self.port}/?token={self.token}" if self.lan else None},
                "server_time": now_iso(), "timezone": "Asia/Shanghai"}

    def background(self):
        # Calendar alarms live in iCloud, not in this timer. This only materializes
        # future weekly occurrences and retries pending calendar changes.
        while not self.stop.wait(60):
            checks = set()
            try:
                self.store.ensure_physics()
                self.calendar.sync_if_enabled()
                if self.collector.thread and self.collector.thread.is_alive():
                    self.collector.request("save_session")
                now = datetime.now(TZ)
                cfg = self.store.settings()["daily"]
                due = cfg["enabled"] and daily_due(now, cfg["time"], self.store.get_value("daily_last_success"))
                checks = reminder_recheck_keys(self.store.tasks(), now, self.reminder_checked) if cfg["enabled"] else set()
                if (due or checks) and time.monotonic() >= self.next_scan_retry:
                    self.ensure_collection()
                    result = self.scan(update_existing=True, import_new=True)
                    if result.get("coverage_complete"):
                        self.reminder_checked.update(checks)
                        self.store.set_value("reminder_checked", sorted(self.reminder_checked)[-1000:])
                    else:
                        self.next_scan_retry = time.monotonic() + (60 if checks else 300)
            except Exception as exc:
                self.store.set_value("collection_last_error", {"at": now_iso(), "message": str(exc)})
                self.next_scan_retry = time.monotonic() + (60 if checks else 300)

    def scan(self, update_existing=False, import_new=False):
        self.last_scan = time.monotonic()
        cfg = self.store.settings()["collection"]
        year = cfg.get("homework_year_binding") or cfg.get("homework_year")
        confirmed_year = year.get("year") if isinstance(year, dict) else year
        if confirmed_year != datetime.now(TZ).year:
            year = None
        result = self.collector.request("scan", {"homework_year": year, "course_scope": cfg["course_scope"]})
        current = self.store.settings()["collection"]
        if any(current.get(k) != cfg.get(k) for k in ("course_scope", "homework_year")):
            raise ValueError("读取期间课程设置已改变，请按新范围重新同步")
        if result.get("homework_year_binding"):
            self.store.patch_settings({"collection": {"homework_year_binding": result["homework_year_binding"]}})
        self.store.set_value("last_collection_scan", now_iso())
        known = {(t["source"], t.get("source_key")): t for t in self.store.tasks() if t.get("source_key")}
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
        added = 0
        if import_new:
            imports = [t for t in result["candidates"] if t.get("source_key") and not t["source_key"].startswith("page:")]
            added = sum((t["source"], t["source_key"]) not in known for t in imports)
            if imports:
                self.store.import_tasks(imports)
                self.calendar.sync_if_enabled()
        if result.get("coverage_complete"):
            self.store.set_value("daily_last_success", now_iso())
            self.store.set_value("collection_last_error", None)
        else:
            self.store.set_value("collection_last_error", {
                "at": now_iso(), "message": "本次未能完整读取所选课程，稍后会重试；已确认的提交状态仍已更新。"})
        self.store.set_value("last_collection_result", result)
        # Completed platform records remain available for matching/cancellation,
        # but are not presented as new arrangements that need reminders.
        return {**result, "candidates": [] if import_new else [t for t in result["candidates"] if t["status"] != "completed"],
                "completed": completed, "synced": len(result["candidates"]), "added": added}

    def ensure_collection(self):
        for source in ("chaoxing", "xiji"):
            self.collector.request("restore", source)

    def resume_sessions(self):
        if not (self.data_dir / "browser-session.json").exists():
            return
        for source in ("chaoxing", "xiji"):
            try:
                self.collector.request("restore", source)
            except Exception:
                continue
        try:
            self.scan(update_existing=True, import_new=True)
        except Exception as exc:
            self.store.set_value("collection_last_error", {"at": now_iso(), "message": str(exc)})


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
                self.send(200, to_ics(self.app.store.tasks()), "text/calendar; charset=utf-8",
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
                result = self.app.collector.request("open", raw.get("source"))
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
