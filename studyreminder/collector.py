"""Read platform metadata using the user's saved, authorized session.

Automatic collection uses an HTTP request context and never starts Chrome.
Only an explicit login action opens the dedicated browser. No password handling,
exam actions, hidden endpoints or automatic submissions.
"""
from __future__ import annotations

import hashlib
import importlib.util
import importlib
import json
import queue
import re
import threading
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from .core import TZ

PLATFORMS = {"chaoxing": "https://i.chaoxing.com/", "xiji": "https://cslabcg.whu.edu.cn/"}
DATE = r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?\s+(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?"
DUE_RE = re.compile(r"(?:提交截止时间|作业截止时间|考试结束时间|截止时间|截止日期|结束时间|提交截止|截止|结束)\s*[:：]?\s*" + DATE)
START_RE = re.compile(r"(?:开始时间|开考时间|开放时间)\s*[:：]?\s*" + DATE)


def platform_for(url):
    host = (urlparse(url).hostname or "").lower()
    if host == "cslabcg.whu.edu.cn":
        return "xiji"
    if host == "chaoxing.com" or host.endswith(".chaoxing.com"):
        return "chaoxing"
    return None


def _coverage_complete(diagnostics, verified_chaoxing, verified_xiji):
    """Never upgrade a partial adapter result into today's successful read."""
    chaoxing_coverage = diagnostics.get("chaoxing", {}).get("coverage", {})
    xiji_coverage = diagnostics.get("xiji", {}).get("coverage", {})
    return bool((verified_chaoxing or chaoxing_coverage.get("disabled") is True)
                and (verified_xiji or xiji_coverage.get("disabled") is True)
                and diagnostics.get("chaoxing", {}).get("coverage", {}).get("complete") is True
                and diagnostics.get("xiji", {}).get("coverage", {}).get("complete") is True)


def match_datetime(match):
    y, m, d, hh, mm, ss = [int(v or 0) for v in match.groups()]
    # Full dates only: never infer an exact time from "剩余1天".
    return datetime(y, m, d, hh, mm, ss, tzinfo=TZ).isoformat(timespec="seconds")


def candidates_from_blocks(blocks, source, page_url, page_title=""):
    results, seen = [], set()
    for block in blocks:
        text = block.get("text", "")
        matches = list(DUE_RE.finditer(text))
        if len(matches) != 1:
            continue
        try:
            due = match_datetime(matches[0])
            start_match = START_RE.search(text)
            start = match_datetime(start_match) if start_match else None
        except ValueError:
            continue
        title = block.get("title", "").strip()
        if not title or re.fullmatch(r"查看|进入|详情|开始考试|作答|答题|提交|继续", title):
            lines = [v.strip() for v in text.splitlines() if v.strip()]
            title = next((v for v in lines if not DUE_RE.search(v) and not START_RE.search(v) and len(v) <= 120), "待核对任务名称")
        href = block.get("url") or page_url
        if platform_for(href) != source:
            href = page_url
        identity = page_url + "|" + (block.get("key") or href)
        # An unverified DOM candidate is never trusted as an automatic update key.
        source_key = "page:" + hashlib.sha256((identity + "|" + title).encode()).hexdigest()[:24]
        key = (source_key, due, source)
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": title[:300], "course": "", "source": source,
                        "kind": "exam" if re.search(r"考试|试卷|测验", title + " " + page_title) else "assignment",
                        "due_at": due, "start_at": start, "status": "pending", "url": href,
                        "source_key": source_key,
                        "notes": "网页采集候选，需核对课程、标题和截止时间。\n原文：" + text[:1600]})
    return results


EXTRACT = r"""() => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const selectors = 'tr,.el-table__row,.task-item,.homework-item,.work-item,.exam-item,[data-task-id],li';
  const nodes = [...document.querySelectorAll(selectors)].filter(visible);
  const blocks = [];
  for (const node of nodes) {
    const text = node.innerText || '';
    if (text.length > 4000 || !/(截止|结束)/.test(text)) continue;
    const links = [...node.querySelectorAll('a')].filter(visible);
    const anchor = links.find(a => a.textContent.trim().length > 2 && !/^(查看|进入|详情|开始考试|作答|答题|提交|继续)$/.test(a.textContent.trim()));
    const heading = node.querySelector('h2,h3,h4,.title,.name');
    blocks.push({text, title: anchor?.innerText?.trim() || heading?.textContent?.trim() || '',
      url: anchor?.href || location.href, key: node.getAttribute('data-task-id') || ''});
  }
  if (!blocks.length) {
    const text = document.body?.innerText || '';
    // Only short blocks with an explicitly labelled absolute deadline.
    for (const section of text.split(/\n\s*\n/)) {
      if (section.length < 1800 && /(截止|结束)/.test(section)) blocks.push({text:section,title:'',url:location.href,key:''});
    }
  }
  return blocks.slice(0,500);
}"""


class BrowserCollector:
    def __init__(self, data_dir):
        self.profile = Path(data_dir) / "browser-profile"
        self.jobs = queue.Queue()
        self.thread = None
        self.lock = threading.Lock()
        self.status = {k: {"status": "not_connected", "message": "需本人登录后接入；尚未验证课程页面"} for k in PLATFORMS}
        self.last_candidates = []
        self.last_warnings = []
        self.last_status_updates = []
        self.session_warning = None

    @staticmethod
    def _trusted_session(state):
        cookies, origins = [], []
        for cookie in state.get("cookies", []):
            domain = cookie.get("domain", "").lstrip(".")
            if domain.endswith(".chaoxing.com") or domain in {"chaoxing.com", "whu.edu.cn", "cslabcg.whu.edu.cn", "cas.whu.edu.cn"}:
                cookies.append(cookie)
        for origin in state.get("origins", []):
            value = origin.get("origin", "")
            if platform_for(value) or urlparse(value).hostname == "cas.whu.edu.cn":
                origins.append(origin)
        return {"cookies": cookies, "origins": origins}

    def _load_session(self):
        try:
            state = json.loads((self.profile.parent / "browser-session.json").read_text())
            return self._trusted_session(state)
        except (OSError, ValueError, TypeError, AttributeError):
            # A missing/corrupt session can be replaced by a normal manual login.
            return {"cookies": [], "origins": []}

    def _save_session(self, context, preserve_origins=False):
        state = self._trusted_session(context.storage_state())
        saved_origins = self._load_session()["origins"]
        # HTTP requests cannot update localStorage. Keep the browser's last
        # authorized values, even if APIRequestContext returns empty origins.
        # A browser may also not have visited every saved origin this session.
        ordered = state["origins"] + saved_origins if preserve_origins else saved_origins + state["origins"]
        state["origins"] = list({item["origin"]: item for item in ordered}.values())
        path = self.profile.parent / "browser-session.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(path)

    def _restore_session(self, context):
        try:
            state = self._load_session()
            context.add_cookies(state.get("cookies", []))
            origins = {o["origin"]: o.get("localStorage", []) for o in state.get("origins", [])}
            context.add_init_script("(() => { const saved = " + json.dumps(origins) + "; for (const pair of saved[location.origin] || []) { if (localStorage.getItem(pair.name) === null) localStorage.setItem(pair.name, pair.value); } })();")
        except Exception:
            # Corrupt/expired session data does not prevent a fresh normal login.
            pass

    def request(self, action, source=None):
        if importlib.util.find_spec("playwright") is None:
            raise ValueError("尚未安装采集依赖。请先运行 scripts/安装采集依赖.command，再重启程序")
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="browser-collector")
                self.thread.start()
        future = Future()
        self.jobs.put((action, source, future))
        # Both adapters and course restoration run serially. Their combined
        # request budgets can exceed 100 seconds even without a stuck request.
        return future.result(timeout=180 if action == "scan" else 100)

    def _run(self):
        from playwright.sync_api import sync_playwright
        from . import chaoxing_adapter
        adapter_mtime = Path(chaoxing_adapter.__file__).stat().st_mtime_ns
        browser_context = None
        api_context = None
        with sync_playwright() as pw:
            def dispose_api():
                nonlocal api_context
                if api_context is not None:
                    try:
                        api_context.dispose()
                    finally:
                        api_context = None

            def request_context():
                nonlocal api_context
                if api_context is None:
                    api_context = pw.request.new_context(storage_state=self._load_session())
                return SimpleNamespace(request=api_context, pages=[])

            while True:
                action, source, future = self.jobs.get()
                try:
                    if action not in {"open", "restore", "scan", "save_session", "inspect", "close"}:
                        raise ValueError("未知采集动作")
                    if action in {"open", "restore"} and source not in PLATFORMS:
                        raise ValueError("不支持的平台")
                    if browser_context is not None and browser_context.browser and not browser_context.browser.is_connected():
                        browser_context = None
                    if action == "close":
                        try:
                            if browser_context is not None:
                                try:
                                    self._save_session(browser_context)
                                finally:
                                    browser_context.close()
                            elif api_context is not None:
                                self._save_session(api_context, preserve_origins=True)
                        finally:
                            dispose_api()
                        future.set_result({"ok": True})
                        return
                    if action == "open" and browser_context is None:
                        # Save any refreshed HTTP cookies before handing control
                        # to the user's visible login browser. The stale client
                        # must never overwrite cookies from that later login.
                        if api_context is not None:
                            self._save_session(api_context, preserve_origins=True)
                            dispose_api()
                        self.profile.mkdir(parents=True, exist_ok=True)
                        self.profile.chmod(0o700)
                        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                        options = {"headless": False, "viewport": {"width": 1280, "height": 900}}
                        if chrome.exists():
                            options["executable_path"] = str(chrome)
                        browser_context = pw.chromium.launch_persistent_context(str(self.profile), **options)
                        self._restore_session(browser_context)
                    # A live manual-login browser remains under the user's
                    # control. Otherwise automatic reads use HTTP, with no page
                    # creation, navigation, Chrome process or visible window.
                    context = browser_context
                    if context is None and action in {"restore", "scan"}:
                        context = request_context()
                    if action == "open":
                        page = next((p for p in context.pages if platform_for(p.url) == source), None) or context.new_page()
                        if platform_for(page.url) != source:
                            page.goto(PLATFORMS[source], wait_until="domcontentloaded", timeout=60000)
                        page.bring_to_front()
                        self.status[source] = {"status": "browser_open", "message": "浏览器已打开；请登录并进入作业或考试列表，随后采集核对"}
                        reply = {"ok": True, "message": "请在专用浏览器中本人登录，打开各课程的作业/考试列表。不会自动开始考试或提交作业。"}
                    elif action == "restore":
                        reply = {"ok": True, "message": "已准备使用保存的登录会话在后台读取；不会打开浏览器。"}
                    elif action == "scan":
                        candidates, warnings, seen = [], [], set()
                        diagnostics = {}
                        status_updates = []
                        options = source if isinstance(source, dict) else {}
                        course_scope = options.get("course_scope", {})
                        if not isinstance(course_scope, dict):
                            raise ValueError("课程范围配置格式不正确")
                        verified_chaoxing = False
                        verified_xiji = False
                        current_mtime = Path(chaoxing_adapter.__file__).stat().st_mtime_ns
                        if current_mtime != adapter_mtime:
                            chaoxing_adapter = importlib.reload(chaoxing_adapter)
                            adapter_mtime = current_mtime
                        result = chaoxing_adapter.collect_chaoxing(context,
                            homework_courses=course_scope.get("chaoxing_homework", chaoxing_adapter.HOMEWORK_COURSES),
                            exam_courses=course_scope.get("chaoxing_exams", chaoxing_adapter.EXAM_COURSES),
                            homework_year=options.get("homework_year"))
                        status_updates.extend(result.get("status_updates", []))
                        diagnostics["chaoxing"] = result.get("diagnostics", {})
                        auth = result["auth"]
                        self.status["chaoxing"] = {"status": auth["status"], "message": auth["message"]}
                        verified_chaoxing = auth.get("authenticated", False)
                        candidates.extend(result["candidates"])
                        warnings.extend(result["warnings"])
                        seen.update((item["source"], item["source_key"], item["due_at"]) for item in candidates)
                        try:
                            from . import xiji_adapter
                            xiji_adapter = importlib.reload(xiji_adapter)
                            result = xiji_adapter.collect_xiji(context, course_scope=course_scope.get("xiji", xiji_adapter.COURSE_SCOPE))
                            diagnostics["xiji"] = result.get("diagnostics", {})
                            auth = result["auth"]
                            self.status["xiji"] = {"status": auth["status"], "message": auth["message"]}
                            verified_xiji = auth.get("authenticated", False)
                            candidates.extend(result["candidates"])
                            status_updates.extend({"source": t["source"], "source_key": t["source_key"], "status": t["status"]} for t in result["candidates"])
                            warnings.extend(result["warnings"])
                            seen.update((item["source"], item["source_key"], item["due_at"]) for item in candidates)
                        except ImportError:
                            warnings.append("希冀专项适配尚未加载，先读取当前页面供核对")
                        for page in context.pages:
                            source = platform_for(page.url)
                            if not source:
                                continue
                            # Generic DOM blocks do not establish a course
                            # identity. Explicit user scopes must stay within
                            # the adapters' verified course selection.
                            if "course_scope" in options:
                                continue
                            if source == "chaoxing" and verified_chaoxing:
                                continue
                            if source == "xiji" and verified_xiji:
                                continue
                            for frame in page.frames:
                                if platform_for(frame.url) not in {source, None}:
                                    continue
                                try:
                                    blocks = frame.evaluate(EXTRACT)
                                    if source == "xiji":
                                        auth_signal = frame.evaluate(r"""() => {
                                          const visible = e => !!e.getClientRects().length;
                                          const password = [...document.querySelectorAll('input[type=password]')].some(visible);
                                          const logout = [...document.querySelectorAll('a,button')].filter(visible).some(e => /退出登录|安全退出|注销|退出系统/.test(e.innerText || '') || /logout|logoff/i.test(e.getAttribute('href') || e.getAttribute('onclick') || ''));
                                          const body = document.body?.innerText || '';
                                          return {password, logout, course: /我的课程|当前课程|已选课程|我的作业|我的考试|个人信息|课程列表/.test(body)};
                                        }""")
                                        if auth_signal["password"]:
                                            self.status[source] = {"status": "not_authenticated", "message": "当前仍显示登录表单，请本人完成平台登录或校园统一认证"}
                                        elif auth_signal["logout"] and auth_signal["course"]:
                                            self.status[source] = {"status": "session_detected", "message": "已识别登录后的课程页面；希冀尚需用真实作业列表验证字段"}
                                    items = candidates_from_blocks(blocks, source, page.url, page.title())
                                    for item in items:
                                        identity = (item["source"], item["source_key"], item["due_at"])
                                        if identity not in seen:
                                            seen.add(identity)
                                            candidates.append(item)
                                except Exception:
                                    warnings.append("有页面尚未加载或已失效，请打开课程列表后重试")
                        if not candidates and not _coverage_complete(diagnostics, verified_chaoxing, verified_xiji):
                            warnings.append("未识别到带完整日期和时间的截止条目。这不表示没有作业；请核对登录、列表展开和页面字段，或手动录入。")
                        self.last_candidates = candidates
                        self.last_warnings = list(dict.fromkeys(warnings))
                        self.last_status_updates = status_updates
                        target = self.profile.parent / "collection-diagnostics.json"
                        target.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2))
                        target.chmod(0o600)
                        complete = _coverage_complete(diagnostics, verified_chaoxing, verified_xiji)
                        reply = {"candidates": candidates, "warnings": list(dict.fromkeys(warnings)), "status_updates": status_updates,
                                 "coverage_complete": complete,
                                 "homework_year_binding": diagnostics.get("chaoxing", {}).get("homework_year_binding")}
                    elif action == "save_session":
                        # A periodic save never creates a request client or
                        # browser merely because no collection is active.
                        reply = {"ok": True}
                    elif action == "inspect":
                        # Local diagnostics for adapting the user's actual list pages.
                        # Do not export cookies, password/input values or page scripts.
                        pages = []
                        for page in context.pages if context else []:
                            if not platform_for(page.url):
                                continue
                            for frame in page.frames:
                                if platform_for(frame.url) != platform_for(page.url):
                                    continue
                                try:
                                    summary = frame.evaluate(r"""() => {
                                      const visible = e => !!e.getClientRects().length;
                                      return {title:document.title, url:location.href,
                                        links:[...document.querySelectorAll('a,button,[onclick]')].filter(visible).slice(0,160).map(e => ({tag:e.tagName,text:(e.innerText||'').trim().slice(0,180),href:e.getAttribute('href'),onclick:e.getAttribute('onclick'),id:e.id,class:e.className})),
                                        rows:[...document.querySelectorAll('tr,li,.el-table__row')].filter(visible).filter(e => /作业|考试|截止|结束|课程/.test(e.innerText||'')).filter(e => (e.innerText||'').length<3000).slice(0,100).map(e => ({text:e.innerText,html:e.outerHTML.slice(0,12000)})),
                                        inputs:[...document.querySelectorAll('input')].map(e => ({name:e.name,id:e.id,type:e.type}))};
                                    }""")
                                    pages.append(summary)
                                except Exception:
                                    pass
                        target = self.profile.parent / "page-diagnostics.json"
                        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
                        allowed_query = {"tab", "assignID", "courseId", "courseid", "classId", "clazzid", "examID", "examid", "id"}
                        def clean_url(value):
                            if not value:
                                return value
                            p = urlsplit(value)
                            return urlunsplit((p.scheme, p.netloc, p.path, urlencode({k:v for k,v in parse_qsl(p.query) if k in allowed_query and re.fullmatch(r"-?\d+", v)}), ""))
                        def redact(value):
                            return re.sub(r"[A-Za-z0-9_-]{60,}(?:\.[A-Za-z0-9_-]+)*", "[redacted]", value or "")
                        for info in pages:
                            info["url"] = clean_url(info["url"])
                            for link in info["links"]:
                                link["href"] = clean_url(link.get("href"))
                                link["onclick"] = redact(link.get("onclick"))
                            for row in info["rows"]:
                                row["html"] = redact(row["html"])
                        target.write_text(json.dumps(pages, ensure_ascii=False, indent=2))
                        target.chmod(0o600)
                        reply = {"ok": True, "pages": len(pages), "message": "仅保存本机页面导航与列表结构，未读取密码和Cookie"}
                    if browser_context is not None or (api_context is not None and action != "restore"):
                        try:
                            if browser_context is not None:
                                self._save_session(browser_context)
                                dispose_api()
                            else:
                                self._save_session(api_context, preserve_origins=True)
                            self.session_warning = None
                        except Exception:
                            self.session_warning = "本次登录会话未能保存，重启后可能需要本人重新登录"
                    if action == "scan" and api_context is not None:
                        # APIRequestContext retains response bodies until it is
                        # disposed. Each collection owns only one scan's data.
                        dispose_api()
                    future.set_result(reply)
                except Exception as exc:
                    message = str(exc)
                    if "Target" in message or "closed" in message:
                        browser_context = None
                        dispose_api()
                    # Browser exceptions can contain private URLs; keep diagnostics local and concise.
                    if not isinstance(exc, ValueError):
                        message = ("登录浏览器未能打开。请确认 Chrome 已安装后重试。" if action == "open" else
                                   "后台读取未能完成，请检查网络后重试；登录失效时请本人点击平台登录按钮。")
                    if not future.done():
                        future.set_exception(ValueError(message))
                    if action == "close":
                        return

    def close(self):
        if self.thread and self.thread.is_alive():
            future = Future()
            self.jobs.put(("close", None, future))
            try:
                future.result(timeout=5)
            except Exception:
                pass
