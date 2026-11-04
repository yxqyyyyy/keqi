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
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
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
        self._pending_requests = {}
        self._active_job = None
        self.status = {k: {"status": "not_connected", "message": "需本人登录后接入；尚未验证课程页面"} for k in PLATFORMS}
        # A restart has not verified platform authentication. Distinguish a
        # saved cookie jar from no connection without any browser or network.
        saved = self._load_session()
        for source in PLATFORMS:
            if self._platform_cookies(saved, source):
                self.status[source] = {"status": "session_saved", "message": "已保存会话，请点击“检查登录状态”确认是否仍有效"}
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
        self._write_session(state)

    def _write_session(self, state):
        path = self.profile.parent / "browser-session.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(path)

    @staticmethod
    def _cookie_platform(cookie):
        domain = cookie.get("domain", "").lower().lstrip(".")
        if domain == "chaoxing.com" or domain.endswith(".chaoxing.com"):
            return "chaoxing"
        if domain in {"whu.edu.cn", "cslabcg.whu.edu.cn", "cas.whu.edu.cn"}:
            return "xiji"
        return None

    @classmethod
    def _platform_cookies(cls, state, source):
        # Cookie order is not semantically significant. Include all attributes
        # so a login, logout, expiration or cookie removal advances the version.
        return sorted((cookie for cookie in state["cookies"] if cls._cookie_platform(cookie) == source),
                      key=lambda cookie: json.dumps(cookie, sort_keys=True))

    @staticmethod
    def _origin_platform(origin):
        return platform_for(origin) or ("xiji" if urlparse(origin).hostname == "cas.whu.edu.cn" else None)

    @classmethod
    def _platform_origins(cls, state, source):
        return sorted((item for item in state["origins"] if cls._origin_platform(item["origin"]) == source),
                      key=lambda item: json.dumps(item, sort_keys=True))

    @classmethod
    def _platform_snapshot(cls, state, source):
        return cls._platform_cookies(state, source), cls._platform_origins(state, source)

    @classmethod
    def _merge_browser_snapshot(cls, saved, before, current):
        """Apply only changes since this browser's last observed snapshot.

        A visible browser can retain old cookies after HTTP collection refreshed
        its independent jar. An unchanged browser must never overwrite them.
        A whole platform jar is the unit of reconciliation: cookie-by-cookie
        merging could combine credentials from different accounts. If both
        HTTP and browser jars changed since the common baseline, neither is
        known to be newer. Keep the saved jar until an explicit auth probe
        verifies the browser candidate. Even a tracking-cookie expiration must
        not restore an old browser JSESSIONID over refreshed HTTP credentials.
        """
        changed = {source for source in PLATFORMS
                   if cls._platform_cookies(before, source) != cls._platform_cookies(current, source)
                   or cls._platform_origins(before, source) != cls._platform_origins(current, source)}
        writable = {source for source in changed
                    if cls._platform_snapshot(saved, source) in
                    (cls._platform_snapshot(before, source), cls._platform_snapshot(current, source))}
        cookies = [cookie for cookie in saved["cookies"] if cls._cookie_platform(cookie) not in writable]
        cookies.extend(cookie for cookie in current["cookies"] if cls._cookie_platform(cookie) in writable)
        origins = [item for item in saved["origins"] if cls._origin_platform(item["origin"]) not in writable]
        origins.extend(item for item in current["origins"] if cls._origin_platform(item["origin"]) in writable)
        return {"cookies": cookies, "origins": origins}, changed

    def _restore_session(self, context):
        try:
            state = self._load_session()
            # This is the program's dedicated persistent profile, never the
            # user's ordinary Chrome profile. Preserve its coherent platform
            # jar when it already exists; the JSON jar can be stale and must
            # not overwrite a login completed before the last periodic save.
            current = self._trusted_session({"cookies": context.cookies(), "origins": []})
            missing = {source for source in PLATFORMS if not self._platform_cookies(current, source)}
            context.add_cookies([cookie for cookie in state.get("cookies", [])
                                 if self._cookie_platform(cookie) in missing])
            origins = {o["origin"]: o.get("localStorage", []) for o in state.get("origins", [])
                       if self._origin_platform(o["origin"]) in missing}
            context.add_init_script("(() => { const saved = " + json.dumps(origins) + "; for (const pair of saved[location.origin] || []) { if (localStorage.getItem(pair.name) === null) localStorage.setItem(pair.name, pair.value); } })();")
        except Exception:
            # Corrupt/expired session data does not prevent a fresh normal login.
            pass

    def request(self, action, source=None):
        if importlib.util.find_spec("playwright") is None:
            raise ValueError("尚未安装采集依赖。请先运行 scripts/安装采集依赖.command，再重启程序")
        with self.lock:
            if action in {"open", "probe", "finish_login"}:
                jobs = list(self._pending_requests.values())
                if self._active_job is not None:
                    jobs.append(self._active_job[:2])
                if any(name == "scan" for name, _ in jobs):
                    raise ValueError("正在同步课程，请等本次同步完成后再操作登录；不会重复打开登录窗口。")
                if any(name in {"open", "probe", "finish_login"} for name, _ in jobs):
                    raise ValueError("正在处理登录窗口或检查登录，请等待当前操作完成，不要重复点击。")
            future = Future()
            self._pending_requests[future] = (action, source)
            future.add_done_callback(self._forget_request)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="browser-collector")
                self.thread.start()
            self.jobs.put((action, source, future))
        # Both adapters and course restoration run serially. Their combined
        # request budgets can exceed 100 seconds even without a stuck request.
        try:
            return future.result(timeout=180 if action == "scan" else 100)
        except FutureTimeoutError:
            if future.cancel():
                raise ValueError("等待操作超时，尚未开始的请求已取消，不会稍后自动打开登录窗口。") from None
            raise ValueError("本次操作仍在处理中，请稍后查看状态，不要重复打开登录窗口。") from None
        finally:
            if future.done():
                self._forget_request(future)

    def _forget_request(self, future):
        with self.lock:
            self._pending_requests.pop(future, None)

    def _run(self):
        from playwright.sync_api import sync_playwright
        from . import chaoxing_adapter
        adapter_mtime = Path(chaoxing_adapter.__file__).stat().st_mtime_ns
        browser_context = None
        browser_snapshot = None
        browser_versions = {source: 0 for source in PLATFORMS}
        api_context = None
        api_seed = None
        api_guard = None
        api_versions = None
        api_browser_source = None
        with sync_playwright() as pw:
            def dispose_api():
                nonlocal api_context, api_seed, api_guard, api_versions, api_browser_source
                if api_context is not None:
                    try:
                        api_context.dispose()
                    finally:
                        api_context = None
                        api_seed = None
                        api_guard = None
                        api_versions = None
                        api_browser_source = None

            def capture_browser(include_origins=False):
                nonlocal browser_snapshot
                if browser_context is None:
                    return
                # cookies() is a passive jar snapshot. storage_state() also
                # reads localStorage and may involve page evaluation; reserve
                # that work for explicit user login actions.
                if include_origins:
                    current = self._trusted_session(browser_context.storage_state())
                else:
                    current = self._trusted_session({"cookies": browser_context.cookies(),
                                                     "origins": browser_snapshot["origins"] if browser_snapshot else []})
                if browser_snapshot is not None:
                    saved, changed = self._merge_browser_snapshot(self._load_session(), browser_snapshot, current)
                    self._write_session(saved)
                    for source in changed:
                        browser_versions[source] += 1
                browser_snapshot = current

            def finish_api(include_origins=False, skip_sources=(), adopt_browser_origins=False):
                if api_context is None:
                    return set()
                try:
                    # The user may finish another login while a scan is in
                    # progress. Capture it before committing HTTP Set-Cookie.
                    capture_browser(include_origins=include_origins)
                    saved = self._load_session()
                    current = self._trusted_session(api_context.storage_state())
                    writable = {source for source in PLATFORMS
                                if api_versions[source] == browser_versions[source]
                                and self._platform_snapshot(saved, source) == self._platform_snapshot(api_guard, source)
                                and source not in skip_sources}
                    saved["cookies"] = [cookie for cookie in saved["cookies"]
                                        if self._cookie_platform(cookie) not in writable]
                    saved["cookies"].extend(cookie for cookie in current["cookies"]
                                            if self._cookie_platform(cookie) in writable)
                    if adopt_browser_origins and api_browser_source in writable:
                        # Commit the verified candidate as a coherent account
                        # bundle, including its own localStorage, rather than
                        # keeping another account's saved browser origins.
                        saved["origins"] = [item for item in saved["origins"]
                                            if self._origin_platform(item["origin"]) != api_browser_source]
                        saved["origins"].extend(item for item in api_seed["origins"]
                                                if self._origin_platform(item["origin"]) == api_browser_source)
                    # HTTP cannot update localStorage; the browser owns origins.
                    self._write_session(saved)
                    self.session_warning = None
                    return writable
                finally:
                    dispose_api()

            def request_context(include_origins=False, browser_source=None):
                nonlocal api_context, api_seed, api_guard, api_versions, api_browser_source
                # Every scan gets a fresh, independent cookie jar, including
                # when the user is still using the visible login browser.
                dispose_api()
                capture_browser(include_origins=include_origins)
                api_guard = self._load_session()
                api_seed = {"cookies": list(api_guard["cookies"]), "origins": list(api_guard["origins"])}
                if browser_context is not None and browser_source is not None:
                    api_browser_source = browser_source
                    # Explicit checks verify the actual dedicated window,
                    # including an unchanged login jar after a mere reload.
                    # Never infer authentication from cookie presence or age.
                    api_seed["cookies"] = [cookie for cookie in api_seed["cookies"]
                                           if self._cookie_platform(cookie) != browser_source]
                    api_seed["cookies"].extend(cookie for cookie in browser_snapshot["cookies"]
                                              if self._cookie_platform(cookie) == browser_source)
                    api_seed["origins"] = [item for item in api_seed["origins"]
                                           if self._origin_platform(item["origin"]) != browser_source]
                    api_seed["origins"].extend(item for item in browser_snapshot["origins"]
                                              if self._origin_platform(item["origin"]) == browser_source)
                api_versions = dict(browser_versions)
                api_context = pw.request.new_context(storage_state=api_seed)
                return SimpleNamespace(request=api_context, pages=[])

            while True:
                action, source, future = self.jobs.get()
                # A request that timed out while queued must never open a
                # browser later. Running jobs retain their in-flight guard.
                if not future.set_running_or_notify_cancel():
                    continue
                with self.lock:
                    self._active_job = (action, source, future)
                try:
                    if action not in {"open", "probe", "finish_login", "restore", "scan", "save_session", "inspect", "close"}:
                        raise ValueError("未知采集动作")
                    if action in {"open", "probe", "finish_login", "restore"} and source not in PLATFORMS:
                        raise ValueError("不支持的平台")
                    if browser_context is not None and browser_context.browser and not browser_context.browser.is_connected():
                        browser_context = None
                        browser_snapshot = None
                    if action == "close":
                        try:
                            if browser_context is not None:
                                try:
                                    capture_browser()
                                finally:
                                    browser_context.close()
                            elif api_context is not None:
                                finish_api()
                        finally:
                            dispose_api()
                        future.set_result({"ok": True})
                        return
                    if action == "open" and browser_context is None:
                        if api_context is not None:
                            finish_api()
                        self.profile.mkdir(parents=True, exist_ok=True)
                        self.profile.chmod(0o700)
                        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                        options = {"headless": False, "viewport": {"width": 1280, "height": 900}, "timeout": 20000}
                        if chrome.exists():
                            options["executable_path"] = str(chrome)
                        browser_context = pw.chromium.launch_persistent_context(str(self.profile), **options)
                        self._restore_session(browser_context)
                        # The dedicated persistent profile can contain cookies
                        # absent from the JSON jar (for example after the user
                        # closed the window before its last periodic save).
                        # There is no common baseline after a restart. Keep
                        # distinct non-empty platform jars as separate coherent
                        # candidates until a read-only probe verifies one.
                        browser_snapshot = self._trusted_session(browser_context.storage_state())
                        saved = self._load_session()
                        missing = {platform for platform in PLATFORMS if not self._platform_cookies(saved, platform)}
                        cookies = saved["cookies"] + [cookie for cookie in browser_snapshot["cookies"]
                                                       if self._cookie_platform(cookie) in missing]
                        origins = {item["origin"]: item for item in saved["origins"]}
                        for item in browser_snapshot["origins"]:
                            platform = self._origin_platform(item["origin"])
                            if platform in missing:
                                origins[item["origin"]] = item
                        self._write_session({"cookies": cookies,
                                             "origins": list(origins.values())})
                    # Automatic collection never uses browser_context.request:
                    # it shares cookies with visible pages and can refresh them.
                    context = browser_context
                    if action in {"scan", "probe", "finish_login"}:
                        context = request_context(include_origins=action in {"probe", "finish_login"},
                                                  browser_source=source if action in {"probe", "finish_login"} else None)
                    if action == "open":
                        page = next((p for p in context.pages if platform_for(p.url) == source), None) or context.new_page()
                        page.bring_to_front()
                        self.status[source] = {"status": "browser_open", "message": "专用登录窗口已打开，平台页面正在加载；请本人登录后检查登录状态。"}
                        navigation_pending = False
                        if platform_for(page.url) != source:
                            try:
                                # Commit does not wait for slow third-party
                                # resources or all platform scripts to finish.
                                page.goto(PLATFORMS[source], wait_until="commit", timeout=12000)
                            except Exception as exc:
                                if "closed" in str(exc).lower():
                                    raise
                                navigation_pending = True
                        message = ("登录窗口已打开，但平台连接较慢或暂时失败；请在原窗口等待页面加载后再检查登录，无需反复打开。"
                                   if navigation_pending else "请在专用浏览器中本人登录，随后点击“完成登录并关闭专用窗口”。")
                        self.status[source] = {"status": "browser_open", "message": message}
                        reply = {"ok": True, "navigation_pending": navigation_pending, "message": message}
                    elif action == "restore":
                        # Preparing a saved session requires no network client,
                        # browser navigation or cookie injection into pages.
                        reply = {"ok": True, "message": "已准备使用保存的登录会话在后台读取；不会打开浏览器。"}
                    elif action in {"probe", "finish_login"}:
                        # Only this explicit user action closes the dedicated
                        # login window. Never close Chrome during auto reads.
                        if source == "chaoxing":
                            probe_auth = chaoxing_adapter.probe_chaoxing_auth
                        else:
                            from . import xiji_adapter
                            xiji_adapter = importlib.reload(xiji_adapter)
                            probe_auth = xiji_adapter.probe_xiji_auth
                        auth = probe_auth(context)
                        # An existing dedicated profile and a saved HTTP jar
                        # can disagree after a crash or prior HTTP refresh.
                        # A failed profile probe must not destroy a possibly
                        # working saved login. Verify that original jar once,
                        # using a separate complete platform cookie candidate.
                        differs = self._platform_cookies(api_seed, source) != self._platform_cookies(api_guard, source)
                        committed = finish_api(include_origins=True,
                                               skip_sources={source} if differs and not auth.get("authenticated") else (),
                                               adopt_browser_origins=bool(auth.get("authenticated")))
                        if differs and not auth.get("authenticated"):
                            context = request_context(include_origins=True)
                            auth = probe_auth(context)
                            committed = finish_api(include_origins=True)
                        if source not in committed:
                            # Do not verify an old session and then close a
                            # different account's newly completed login page.
                            auth = {"authenticated": False, "status": "unverified", "signals": [],
                                    "message": "登录状态在检查期间发生变化，请再检查一次；专用登录窗口已保留。"}
                        self.status[source] = {"status": auth["status"], "message": auth["message"]}
                        authenticated = bool(auth.get("authenticated"))
                        closed = False
                        if action == "finish_login" and authenticated and browser_context is not None:
                            browser_context.close()
                            browser_context = None
                            browser_snapshot = None
                            closed = True
                        reply = {"ok": authenticated, "authenticated": authenticated,
                                 "source": source, "status": auth["status"], "auth": auth,
                                 "closed_login_window": closed, "message": auth["message"]}
                    elif action == "scan":
                        candidates, warnings, seen = [], [], set()
                        diagnostics = {}
                        source_results = {}
                        source_statuses = {}
                        source_warnings = {}
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
                        source_statuses["chaoxing"] = {"status": auth["status"], "message": auth["message"]}
                        source_results["chaoxing"] = {"coverage_complete": diagnostics["chaoxing"].get("coverage", {}).get("complete") is True,
                                                       "auth_status": auth["status"]}
                        verified_chaoxing = auth.get("authenticated", False)
                        candidates.extend(result["candidates"])
                        source_warnings["chaoxing"] = result["warnings"]
                        seen.update((item["source"], item["source_key"], item["due_at"]) for item in candidates)
                        try:
                            from . import xiji_adapter
                            xiji_adapter = importlib.reload(xiji_adapter)
                            xiji_scope = course_scope.get("xiji", xiji_adapter.COURSE_SCOPE)
                            xiji_window_open = browser_context is not None and any(
                                platform_for(page.url) == "xiji" for page in browser_context.pages)
                            if xiji_window_open and xiji_scope:
                                # Independent cookie jars can still share the
                                # same server-side JSESSIONID/course state.
                                # Never POST course switches while the user is
                                # using that session in the visible Xiji page.
                                result = {"auth": xiji_adapter.probe_xiji_auth(context),
                                          "candidates": [], "status_updates": [],
                                          "warnings": ["希冀登录窗口仍打开，已暂停切换课程采集；请点击“完成登录并关闭专用窗口”后再读取。"],
                                          "diagnostics": {"coverage": {"complete": False, "deferred": True,
                                                                        "reason": "login_window_open"}}}
                            else:
                                result = xiji_adapter.collect_xiji(context, course_scope=xiji_scope)
                            diagnostics["xiji"] = result.get("diagnostics", {})
                            auth = result["auth"]
                            source_statuses["xiji"] = {"status": auth["status"], "message": auth["message"]}
                            source_results["xiji"] = {"coverage_complete": diagnostics["xiji"].get("coverage", {}).get("complete") is True,
                                                       "auth_status": auth["status"],
                                                       "deferred_reason": diagnostics["xiji"].get("coverage", {}).get("reason")}
                            verified_xiji = auth.get("authenticated", False)
                            candidates.extend(result["candidates"])
                            status_updates.extend({"source": t["source"], "source_key": t["source_key"], "status": t["status"]} for t in result["candidates"])
                            source_warnings["xiji"] = result["warnings"]
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
                        # Commit the cookie snapshot before publishing any
                        # account-dependent results. A manual re-login during
                        # this scan invalidates that source's old auth, tasks,
                        # submission updates and date binding together.
                        committed = finish_api()
                        conflicted = set(source_results) - committed
                        if conflicted:
                            candidates = [item for item in candidates if item.get("source") not in conflicted]
                            status_updates = [item for item in status_updates if item.get("source") not in conflicted]
                            for platform in conflicted:
                                source_statuses[platform] = {"status": "unverified",
                                    "message": "读取期间登录状态发生变化，本次旧会话结果未采用；请重新检查登录状态后读取。"}
                                source_results[platform] = {"coverage_complete": False, "auth_status": "unverified",
                                                            "deferred_reason": "session_changed_during_scan"}
                                diagnostics[platform] = {"coverage": {"complete": False, "deferred": True,
                                                                     "reason": "session_changed_during_scan"},
                                                         "session_conflict": True}
                                source_warnings.pop(platform, None)
                                warnings.append(source_statuses[platform]["message"])
                            if "chaoxing" in conflicted:
                                verified_chaoxing = False
                            if "xiji" in conflicted:
                                verified_xiji = False
                        for platform, status in source_statuses.items():
                            if not (status["status"] == "disabled" and platform in options.get("preserve_disabled_status", [])):
                                self.status[platform] = status
                        for items in source_warnings.values():
                            warnings.extend(items)
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
                                 "source_results": source_results,
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
                    try:
                        if action == "scan":
                            # Dispose response bodies after each individual scan.
                            finish_api()
                        elif action in {"open", "save_session"}:
                            capture_browser(include_origins=action == "open")
                        self.session_warning = None
                    except Exception:
                        self.session_warning = "本次登录会话未能保存，重启后可能需要本人重新登录"
                    future.set_result(reply)
                except Exception as exc:
                    message = str(exc)
                    if "Target" in message or "closed" in message:
                        browser_context = None
                        browser_snapshot = None
                    if api_context is not None:
                        try:
                            finish_api()
                        except Exception:
                            self.session_warning = "本次登录会话未能保存，重启后可能需要本人重新登录"
                    # Browser exceptions can contain private URLs; keep diagnostics local and concise.
                    if not isinstance(exc, ValueError):
                        message = (("专用登录窗口已关闭，请重新打开登录窗口。" if "closed" in str(exc).lower() else
                                    "专用登录窗口暂未启动，请稍后重试；已保存的登录会话会保留。") if action == "open" else
                                   "后台读取未能完成，请检查网络后重试；登录失效时请本人点击平台登录按钮。")
                    if action == "open" and source in PLATFORMS:
                        self.status[source] = {"status": "error", "message": message}
                    if not future.done():
                        future.set_exception(ValueError(message))
                    if action == "close":
                        return
                finally:
                    with self.lock:
                        if self._active_job is not None and self._active_job[2] is future:
                            self._active_job = None

    def close(self):
        if self.thread and self.thread.is_alive():
            future = Future()
            self.jobs.put(("close", None, future))
            try:
                future.result(timeout=5)
            except Exception:
                pass
