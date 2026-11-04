"""Bounded, read-only metadata collection from the user's Xiji session.

Only routes observed in the authenticated platform navigation are requested.
Observed assignment navigation is fetched only to extract title, absolute times
and submission status. Question/answer tables never contribute time or title.
Unknown routes are reported without query strings. No password, cookie, token,
answer or assignment-submission API is used here.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from .chaoxing_adapter import _Node, _Tree
from .core import TZ

ORIGIN = "https://cslabcg.whu.edu.cn"
MAIN = ORIGIN + "/main.jsp"
HOME = ORIGIN + "/"
_CAMPUS_LOGIN = "https://cas.whu.edu.cn/authserver/login?" + urlencode({"service": HOME})
COURSE_SCOPE = ("计算机组成", "离散数学")
_DATE = re.compile(r"20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?[ T\s]+\d{1,2}[:：]\d{2}(?:[:：]\d{2})?")
_ASSIGN_PATH = "/assignment/index.jsp"
_NAV = {"课程信息": "-1", "在线作业": "-2", "在线考试": "-3"}
_ACTION_TEXT = {"进入作业", "开始作业", "查看", "详情", "作答", "打开"}


def _route(url):
    try:
        parsed = urlparse(url)
        path = parsed.path
        # Redirects are untrusted. Tickets or servlet session IDs can appear
        # in a path as well as in the query, including percent-encoded paths.
        decoded = unquote(unquote(path))
        if re.search(r"(?i)(?:\b(?:ST|TGT)-|(?:ticket|jsessionid|sessionid|token)\s*=)", decoded):
            path = "/[redacted]"
        return {"host": (parsed.hostname or "")[:160],
                "path": re.sub(r"[\x00-\x20\x7f]", "", path)[:240] or "/"}
    except ValueError:
        return {"host": "invalid", "path": "/"}


def _params(url):
    return {key.lower(): values[0] for key, values in parse_qs(urlparse(html.unescape(url)).query).items() if len(values) == 1}


def _safe_url(url):
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "cslabcg.whu.edu.cn" or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.params):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path in {"/", "/main.jsp"}:
            return not query
        # The actual assignment menu uses courseID + assignID for metadata.
        # Only those numeric fields are admitted; question/answer routes are not.
        if parsed.path == _ASSIGN_PATH:
            return not query or (set(query) <= {"courseID", "assignID"} and "assignID" in query
                                 and all(len(values) == 1 and re.fullmatch(r"\d+", values[0]) for values in query.values()))
        return parsed.path == "/includes/redirect.jsp" and query in ({"tab": ["-1"]}, {"tab": ["-2"]}, {"tab": ["-3"]})
    except ValueError:
        return False


def _campus_ticket_url(url):
    """Accept one CAS ST ticket only at the originally authorized service."""
    try:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname != "cslabcg.whu.edu.cn"
                or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.params or parsed.path != "/" or len(parsed.query) > 4096):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        return (set(query) == {"ticket"} and len(query["ticket"]) == 1
                and re.fullmatch(r"ST-[A-Za-z0-9._~-]{1,2040}", query["ticket"][0]) is not None)
    except ValueError:
        return False


def _known_school_login(url):
    """Recognize the observed login destination, without requesting it."""
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname == "cslabcg.whu.edu.cn"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                and not parsed.fragment and not parsed.params and parsed.path == "/indexcs/simple.jsp")
    except ValueError:
        return False


def _campus_portal_redirect(url):
    """Permit a fixed MAIN recheck, never a request to this unknown target."""
    try:
        parsed = urlparse(url)
        path = unquote(unquote(parsed.path)).lower()
        return (parsed.scheme == "https" and parsed.hostname == "cslabcg.whu.edu.cn"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                and not parsed.fragment and not parsed.params and "\\" not in path
                and not re.search(r"[\x00-\x20\x7f]", path)
                and not any(word in path for word in ("logout", "logoff", "signout"))
                and not re.search(r"\b(?:st|tgt)-", path))
    except ValueError:
        return False


class _ReadError(Exception):
    pass


class _LoginRequired(_ReadError):
    pass


class _Reader:
    def __init__(self, context, budget=35):
        self.context, self.deadline = context, time.monotonic() + budget
        self.count, self.routes = 0, []
        self.request_limit = 30
        self.navigation_attempted = False
        self.empty_main_checked = False
        self.campus_session_attempted = False

    def diagnostics(self):
        return {"requests": self.count, "routes": self.routes}

    def reuse_campus_session(self):
        """Redeem an existing campus SSO session without credentials or UI.

        The only cross-origin request is the school's fixed CAS service URL.
        A returned service ticket may go only to the exact HTTPS service root;
        neither arbitrary redirects nor login forms are followed/submitted.
        """
        if self.campus_session_attempted:
            return False
        self.campus_session_attempted = True
        try:
            cookies = self.context.request.storage_state().get("cookies", [])
            has_campus_session = any(isinstance(cookie, dict)
                                     and str(cookie.get("domain", "")).lstrip(".").lower() == "cas.whu.edu.cn"
                                     for cookie in cookies)
        except Exception:
            return False
        if not has_campus_session:
            return False
        deadline = min(self.deadline, time.monotonic() + 15)
        url, ticket_received = _CAMPUS_LOGIN, False
        visited = set()
        for _ in range(4):
            if url in visited:
                raise _ReadError("校园免登录恢复出现重复跳转，本次已停止，请本人检查登录")
            visited.add(url)
            remaining = deadline - time.monotonic()
            if remaining < 1 or self.count >= self.request_limit:
                raise _ReadError("校园免登录恢复达到时间或请求数上限，请稍后重试")
            self.count += 1
            try:
                response = self.context.request.get(url, timeout=min(8000, max(500, int(remaining * 1000))), max_redirects=0)
            except Exception as exc:
                raise _ReadError("校园免登录恢复请求超时或网络失败，请稍后重试") from exc
            event = {**_route(url), "status": response.status, "purpose": "campus_session_reuse"}
            self.routes.append(event)
            if response.status == 200:
                # CAS HTML can require a password, captcha, or an explicit
                # confirmation. Never inspect field values or submit its form.
                # The platform response still must pass a fresh _probe below.
                return ticket_received and urlparse(url).hostname == "cslabcg.whu.edu.cn"
            if not 300 <= response.status < 400:
                if response.status in {401, 403}:
                    return False
                raise _ReadError("校园免登录恢复暂未成功（HTTP " + str(response.status) + "），请稍后重试")
            target = urljoin(url, response.headers.get("location", ""))
            if url == _CAMPUS_LOGIN:
                if not _campus_ticket_url(target):
                    event["result"] = "blocked_sso_redirect"
                    event["redirect"] = {"host": "unverified", "path": "/[redacted]"}
                    raise _ReadError("校园免登录恢复返回了未确认的跳转，本次已停止，请本人检查登录")
                ticket_received = True
            elif _known_school_login(target):
                event["result"] = "login_required"
                event["redirect"] = _route(target)
                raise _LoginRequired("校园免登录未能恢复希冀授权，请本人在专用窗口登录一次并完成保存；程序之后会复用有效会话")
            elif target not in {HOME, MAIN}:
                event["redirect"] = _route(target)
                if ticket_received and _campus_portal_redirect(target):
                    # The ticket request already updated the cookie jar. A
                    # portal redirect need not be followed to prove login:
                    # only the fixed, read-only MAIN probe may establish it.
                    event["result"] = "verify_main_after_service_redirect"
                    return True
                event["result"] = "blocked_sso_redirect"
                raise _ReadError("校园免登录恢复返回了未确认的跳转，本次已停止，请本人检查登录")
            # Query strings (including one-time CAS tickets) never enter logs.
            event["redirect"] = _route(target)
            url = target
        raise _ReadError("校园免登录恢复跳转次数异常，本次已停止，请本人检查登录")

    def get(self, url):
        for _ in range(5):
            if not _safe_url(url):
                route = _route(url)
                self.routes.append({**route, "result": "blocked_route"})
                raise _ReadError("希冀返回未确认的只读路径，已停止读取（" + route["host"] + route["path"] + "）")
            remaining = self.deadline - time.monotonic()
            if remaining < 1 or self.count >= self.request_limit:
                raise _ReadError("希冀采集达到时间或请求数上限，结果可能不完整")
            if self.count:
                time.sleep(min(0.2, max(0, remaining - 0.5)))
            self.count += 1
            try:
                response = self.context.request.get(url, timeout=min(10000, max(500, int(remaining * 1000))), max_redirects=0)
            except Exception as exc:
                raise _ReadError("希冀请求超时或网络失败，请检查网络后重试") from exc
            status = response.status
            event = {**_route(url), "status": status}
            self.routes.append(event)
            if 300 <= status < 400:
                target = urljoin(url, response.headers.get("location", ""))
                event["redirect"] = _route(target)
                # The deployed school's home route redirects an expired
                # session to this observed account-login page; main.jsp can
                # instead return an empty HTTP 200 for the same session.
                # Unknown paths containing "login" may be success portals;
                # an unsupported redirect is a read failure, not a logout.
                if _known_school_login(target):
                    raise _LoginRequired("本程序保存的希冀登录会话已失效，请从“打开登录窗口”本人登录，再点击“完成登录并关闭专用窗口”；平时浏览器的登录不会自动传入本程序")
                url = target
                continue
            if status in {401, 403}:
                raise _ReadError("希冀拒绝访问，请核对登录状态或课程权限")
            if status == 429:
                raise _ReadError("希冀提示访问过于频繁，本次已停止，请稍后重试")
            if status != 200:
                raise _ReadError("希冀未能返回课程列表（HTTP " + str(status) + "）")
            source = response.text()
            if not source.strip():
                if url == MAIN and not self.empty_main_checked:
                    self.empty_main_checked = True
                    try:
                        return self.get(HOME)
                    except _LoginRequired:
                        raise
                    except _ReadError:
                        pass
                raise _ReadError("希冀课程主页返回空白内容，暂时无法检查登录；这不代表账号未登录，请稍后点击“检查登录状态”")
            if len(source) > 4_000_000:
                raise _ReadError("希冀页面超出预期大小，已停止解析")
            tree = _Tree(source).root
            if any(node.tag == "input" and node.attrs.get("type", "").lower() == "password" for node in tree.walk()):
                raise _LoginRequired("希冀返回登录页，请在专用浏览器中本人重新登录")
            return tree
        raise _ReadError("希冀页面重定向次数异常，未能读取")

    def select_course(self, course_id, main):
        """Replay only the observed courseForm navigation, never task forms."""
        valid = next((form for form in main.walk() if form.tag == "form" and form.attrs.get("name") == "courseForm"
                      and form.attrs.get("method", "get").lower() == "post"
                      and urljoin(MAIN, form.attrs.get("action", "")) == MAIN
                      and {node.attrs.get("name") for node in form.walk() if node.tag == "input"} == {"changeCourse", "doChangeCourse"}
                      and any(node.tag == "input" and node.attrs.get("name") == "doChangeCourse" and node.attrs.get("value") == "true" for node in form.walk())), None)
        choices = {entry["id"] for entry in _course_entries(main, None)}
        binding = any(".dropdown-item-course" in "".join(child for child in node.children if isinstance(child, str))
                      and "document.courseForm.changeCourse.value = id" in "".join(child for child in node.children if isinstance(child, str))
                      and "document.courseForm.submit()" in "".join(child for child in node.children if isinstance(child, str)) for node in main.walk() if node.tag == "script")
        if not valid or not binding or course_id not in choices or not re.fullmatch(r"\d+", course_id or ""):
            raise _ReadError("希冀课程切换表单与已验证结构不一致，未执行切换")
        remaining = self.deadline - time.monotonic()
        if remaining < 1 or self.count >= self.request_limit:
            raise _ReadError("希冀采集达到时间或请求数上限，未切换课程")
        self.count += 1
        self.navigation_attempted = True
        try:
            response = self.context.request.post(MAIN, form={"changeCourse": course_id, "doChangeCourse": "true"},
                                                 max_redirects=0, timeout=min(10000, max(500, int(remaining * 1000))))
        except Exception as exc:
            raise _ReadError("希冀课程导航请求失败，请稍后重试") from exc
        event = {**_route(MAIN), "method": "POST", "purpose": "course_navigation", "status": response.status}
        self.routes.append(event)
        if 300 <= response.status < 400:
            target = urljoin(MAIN, response.headers.get("location", ""))
            event["redirect"] = _route(target)
            return self.get(target)
        if response.status != 200:
            raise _ReadError("希冀课程导航未成功（HTTP " + str(response.status) + "）")
        source = response.text()
        if len(source) > 4_000_000:
            raise _ReadError("希冀课程导航页面超出预期大小")
        tree = _Tree(source).root
        if any(node.tag == "input" and node.attrs.get("type", "").lower() == "password" for node in tree.walk()):
            raise _LoginRequired("希冀登录已失效，请本人重新登录")
        return tree


def _navigation(tree):
    found = {}
    for node in tree.walk():
        if node.tag != "a":
            continue
        name = "".join(node.text().split())
        if name not in _NAV:
            continue
        url = urljoin(MAIN, html.unescape(node.attrs.get("href", "")))
        if _safe_url(url) and _params(url).get("tab") == _NAV[name]:
            found[name] = url
    return found


def _probe(reader):
    tree = reader.get(MAIN)
    navigation = _navigation(tree)
    # Require the actual authenticated navigation plus an account menu/control;
    # the menu's displayed personal name is deliberately neither read nor kept.
    account = any(node.attrs.get("id") == "userDropdown" or (node.tag == "a" and "退出" in node.text()) for node in tree.walk())
    authenticated = len(navigation) == 3 and account
    return {"authenticated": authenticated, "status": "authenticated" if authenticated else "unverified",
            "message": "希冀登录已通过服务端课程导航校验" if authenticated else "希冀页面结构暂未识别，无法确认登录；请先检查专用窗口是否显示课程主页，再点击“检查登录状态”",
            "signals": (["course_navigation"] if len(navigation) == 3 else []) + (["account_control"] if account else [])}, tree


def _probe_with_campus_session(reader):
    try:
        return _probe(reader)
    except _LoginRequired:
        # The entire recovery, including final platform verification, gets at
        # most five requests and fifteen seconds. Normal collection afterwards
        # keeps its original, already elapsed overall budget.
        original_deadline, original_limit = reader.deadline, reader.request_limit
        reader.deadline = min(reader.deadline, time.monotonic() + 15)
        reader.request_limit = min(reader.request_limit, reader.count + 5)
        try:
            if not reader.reuse_campus_session():
                raise _LoginRequired("本程序保存的希冀会话目前需要本人验证，请在专用窗口登录一次并完成保存；程序会复用保存的会话，学校要求重新验证时仍需本人登录")
            reader.empty_main_checked = False
            auth, tree = _probe(reader)
        finally:
            reader.deadline, reader.request_limit = original_deadline, original_limit
    if auth["authenticated"]:
        auth["signals"].append("campus_session_reused")
        auth["message"] = "已复用保存的校园免登录会话，希冀登录已通过服务端课程导航校验"
    return auth, tree


def probe_xiji_auth(context):
    try:
        auth, _ = _probe_with_campus_session(_Reader(context, budget=20))
        return auth
    except _LoginRequired as exc:
        return {"authenticated": False, "status": "not_authenticated", "message": str(exc), "signals": []}
    except Exception as exc:
        return {"authenticated": False, "status": "error", "message": str(exc) if isinstance(exc, _ReadError) else "希冀登录状态检查失败，请重试", "signals": []}


def _scoped(name, course_scope=COURSE_SCOPE):
    return bool(name.strip()) if course_scope is None else any(part and part in name for part in course_scope)


def _course_controls(tree, course_scope=COURSE_SCOPE):
    """Inspect form structure, retaining only explicit course option values."""
    result = []
    for form in tree.walk():
        if form.tag != "form":
            continue
        fields = [node for node in form.walk() if node.tag in {"input", "select", "button"}]
        field_names = sorted({node.attrs.get("name", "")[:100] for node in fields if node.attrs.get("name")})
        options = []
        for select in fields:
            if select.tag != "select" or "course" not in (select.attrs.get("name", "") + select.attrs.get("id", "")).lower():
                continue
            for option in select.walk():
                if option.tag != "option":
                    continue
                text = " ".join(option.text().split())
                value = option.attrs.get("value", "")
                if _scoped(text, course_scope) and re.fullmatch(r"\d+", value):
                    options.append({"text": text[:200], "value": value, "selected": "selected" in option.attrs})
        # General forms contribute names and action route only. Never retain
        # input values: even hidden fields can be personal IDs or session data.
        result.append({"method": form.attrs.get("method", "get").lower()[:12],
                       "action": _route(urljoin(MAIN, form.attrs.get("action", ""))),
                       "fields": field_names, "course_options": options,
                       "switch_flags": {node.attrs["name"]: node.attrs.get("value", "") for node in fields
                                        if node.attrs.get("name") == "doChangeCourse" and node.attrs.get("value", "") in {"", "0", "1", "true", "false", "on", "yes", "no"}}})
    return result[:10]


def _course_contexts(tree, course_scope=COURSE_SCOPE):
    """Retain minimal course-labelled nodes and numeric course-control data."""
    result = []
    for node in tree.walk():
        if node.tag in {"root", "html", "body", "script", "style"}:
            continue
        text = " ".join(node.text().split())
        if not _scoped(text, course_scope) or any(isinstance(child, _Node) and _scoped(child.text(), course_scope) for child in node.children):
            continue
        # Long shell text is not a course label and can contain unrelated
        # personal data. Keep a short course-local fragment in that case.
        if len(text) > 150:
            first = min(text.index(part) for part in course_scope if part in text)
            text = text[first:first + 90]
        control = node
        for _ in range(3):
            if control.attrs.get("onclick") or not control.parent or control.parent.tag in {"html", "body", "root"}:
                break
            control = control.parent
        onclick = control.attrs.get("onclick", "")
        functions = []
        for match in re.finditer(r"([A-Za-z_$][\w.$]*)\s*\(([^()]*)\)", onclick):
            numbers = [arg.strip().strip("\"'") for arg in match.group(2).split(",")
                       if re.fullmatch(r"\s*['\"]?\d+['\"]?\s*", arg)]
            functions.append({"name": match.group(1)[:80], "numeric_args": numbers[:5]})
        assignments = []
        for match in re.finditer(r"\b(changeCourse|doChangeCourse)(?:\.value)?\s*=\s*['\"]?(\d+)['\"]?", onclick):
            assignments.append({"field": match.group(1), "value": match.group(2)})
        def attributes(element):
            if not element:
                return {}
            numeric_course_attrs = {}
            for key, value in element.attrs.items():
                is_course_attr = "course" in key.lower() or (element.has_class("dropdown-item-course") and key in {"value", "data-id"})
                if is_course_attr and re.fullmatch(r"\d+", value or ""):
                    numeric_course_attrs[key] = value
            return {"tag": element.tag, "attribute_names": sorted(element.attrs),
                    "numeric_course_attributes": numeric_course_attrs}
        result.append({"tag": node.tag, "id": node.attrs.get("id", "")[:100],
                       "class": node.attrs.get("class", "")[:160], "text": text[:150],
                       "attributes": attributes(node), "parent": attributes(node.parent),
                       "onclick": {"functions": functions[:5], "course_assignments": assignments[:5]}})
    return result[:20]


def _frame_course(context, course_scope=COURSE_SCOPE):
    """Read titles of already-loaded, observed course frames; never navigate."""
    names = set()
    try:
        for page in context.pages:
            parsed_page = urlparse(page.url)
            if parsed_page.scheme != "https" or parsed_page.hostname != "cslabcg.whu.edu.cn" or parsed_page.path != "/main.jsp":
                continue
            for frame in page.frames:
                parsed_frame = urlparse(frame.url)
                if parsed_frame.scheme != "https" or parsed_frame.hostname != "cslabcg.whu.edu.cn" or parsed_frame.path != "/includes/kgraph/index.jsp":
                    continue
                title = " ".join(frame.title().split())
                if title.endswith("-知识图谱"):
                    title = title[:-5].strip()
                if _scoped(title, course_scope) and len(title) <= 150:
                    names.add(title)
    except Exception:
        # Missing browser pages, a closed frame or a test-only request context
        # cannot establish a course; do not expose Playwright exception URLs.
        return ""
    return next(iter(names)) if len(names) == 1 else ""


def _current_course_names(tree, course_scope=COURSE_SCOPE):
    """Collect course-selection evidence without treating menu options as it."""
    # The live course dropdown marks its selected item with this exact pair.
    marked = {" ".join(node.text().split()) for node in tree.walk()
              if node.has_class("dropdown-item-course") and node.has_class("font-weight-bold") and _scoped(node.text(), course_scope) and len(node.text()) <= 200}
    if marked:
        return marked
    choices = []
    for node in tree.walk():
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or (node.tag == "option" and "selected" in node.attrs):
            text = " ".join(node.text().split())
            if _scoped(text, course_scope) and len(text) <= 200:
                choices.append(text)
        if node.tag == "iframe":
            title = " ".join(node.attrs.get("title", "").split())
            if title.endswith("-知识图谱"):
                title = title[:-5].strip()
            if _scoped(title, course_scope) and len(title) <= 150:
                choices.append(title)
        # Labels must live in a small text unit, not the complete document shell.
        if node.tag in {"span", "p", "label", "td", "div"}:
            text = " ".join(node.text().split())
            match = re.fullmatch(r"当前课程\s*[:：]\s*(.{1,150})", text)
            if match and _scoped(match.group(1), course_scope):
                choices.append(match.group(1))
    return set(choices)


def _current_course(tree, course_scope=COURSE_SCOPE):
    """Accept one explicit current-course label, selected option, or heading."""
    unique = _current_course_names(tree, course_scope)
    return next(iter(unique)) if len(unique) == 1 else ""


def _datetime(value):
    if not _DATE.fullmatch(value.strip()):
        return None
    numbers = [int(part) for part in re.findall(r"\d+", value)]
    try:
        return datetime(*numbers[:5], numbers[5] if len(numbers) == 6 else 0, tzinfo=TZ).isoformat(timespec="seconds")
    except ValueError:
        return None


def _dates(text, label):
    values = []
    for match in re.finditer(label + r"\s*[:：]?\s*(" + _DATE.pattern + ")", text):
        parsed = _datetime(match.group(1))
        if not parsed:
            return None
        values.append(parsed)
    values = set(values)
    return next(iter(values)) if len(values) == 1 else None


def _assignment_id(node, base_url=MAIN):
    href = node.attrs.get("href", "")
    try:
        parsed = urlparse(urljoin(base_url, html.unescape(href)))
        value = _params(href).get("assignid", "")
        if parsed.scheme == "https" and parsed.hostname == "cslabcg.whu.edu.cn" and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment and parsed.path == _ASSIGN_PATH and re.fullmatch(r"\d+", value):
            return value
    except ValueError:
        pass
    return ""


def _table_columns(row):
    parent = row.parent
    while parent and parent.tag != "table":
        parent = parent.parent
    if not parent:
        return {}
    headers = next(([node.text() for node in candidate.children if isinstance(node, _Node) and node.tag == "th"]
                    for candidate in parent.walk() if candidate.tag == "tr" and any(isinstance(node, _Node) and node.tag == "th" for node in candidate.children)), [])
    cells = [node.text() for node in row.children if isinstance(node, _Node) and node.tag == "td"]
    return dict(zip(headers, cells)) if len(headers) == len(cells) else {}


def _assignment_structure(tree):
    """Metadata-only schema diagnostics; omit answer text and arbitrary values."""
    tables, links, metadata = [], [], []
    for node in tree.walk():
        if node.tag == "table":
            headers = [" ".join(child.text().split())[:100] for child in node.walk() if child.tag == "th"]
            tables.append({"headers": headers[:20], "rows": sum(child.tag == "tr" for child in node.walk())})
        if node.tag == "a" and (assign_id := _assignment_id(node, ORIGIN + _ASSIGN_PATH)):
            ancestor = node
            for _ in range(5):
                if ancestor.tag in {"tr", "li", "article"} or not ancestor.parent or ancestor.parent.tag in {"body", "html", "root"}:
                    break
                ancestor = ancestor.parent
            links.append({"assign_id": assign_id, "container_tag": ancestor.tag,
                          "full_dates": _DATE.findall(ancestor.text())[:6]})
        parent, outside_table = node.parent, True
        while parent:
            if parent.tag == "table":
                outside_table = False
                break
            parent = parent.parent
        if not outside_table or node.tag in {"root", "html", "body", "script", "style"}:
            continue
        compact = " ".join(node.text().split())
        is_metadata = node.has_class("list-group-item") or node.tag == "option" or node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
        is_date = bool(_DATE.search(compact)) and not any(isinstance(child, _Node) and _DATE.search(child.text()) for child in node.children)
        if (is_metadata or is_date) and compact and len(compact) < 400:
            metadata.append({"tag": node.tag, "class": node.attrs.get("class", "")[:160], "id": node.attrs.get("id", "")[:100],
                             "text": compact, "attribute_names": sorted(node.attrs),
                             "numeric_attributes": {key: value for key, value in node.attrs.items() if ("assign" in key.lower() or key in {"value", "data-id"}) and re.fullmatch(r"\d+", value or "")},
                             "onclick": _sanitize_script(node.attrs.get("onclick", ""))[:500],
                             "href_fields": {key: value for key, value in _params(node.attrs.get("href", "")).items() if re.fullmatch(r"\d+", value) and key not in {"userid", "uid", "studentid"}},
                             "date_parent_text": " ".join(node.parent.text().split())[:400] if is_date and node.parent and len(node.parent.text()) < 500 else "",
                             "href_route": _route(urljoin(ORIGIN + _ASSIGN_PATH, node.attrs.get("href", ""))) if node.attrs.get("href") else None})
    empty = bool(re.search(r"暂无作业|没有作业|尚无作业|未布置作业", tree.text()))
    return {"tables": tables[:10], "assignment_links": links[:40], "explicit_empty": empty, "metadata": metadata[:70],
            "submission_status_texts": _submission_statuses(tree)[:30]}


def _sanitize_script(source):
    # Values of authentication/account fields are irrelevant to course metadata.
    source = re.sub(r"(?i)\b(token|access_token|enc|signature|password|pwd|user_?id|uid|student_?id|username|studentname|account|session)\b\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;)}]+)", r"\1=[redacted]", source)
    literals = {"", "0", "1", "true", "false", "on", "yes", "no", "GET", "POST", "get", "post", "json", "html",
                "changeCourse", "doChangeCourse", "value", "click", "submit", "val", "attr", "name", "title", "id", "status",
                "assignID", "assignId", "courseID", "courseId", "startTime", "endTime", "deadline", "data", "method", "url",
                "type", "rows", "page", "limit", "table", "list", "query", "count", "success", "error"}

    def replace(match):
        value = match.group()[1:-1]
        if value in literals or re.fullmatch(r"\d{1,5}", value):
            return match.group()
        if re.fullmatch(r"[.#][A-Za-z][\w.# >:-]{0,100}", value) and re.search(r"course|assign|table|list|form", value, re.I):
            return match.group()
        try:
            parsed = urlparse(value)
        except ValueError:
            return '"[redacted]"'
        path = parsed.path
        if re.fullmatch(r"(?:/?[\w.-]+/)*[\w.-]+\.(?:jsp|js|json|do|action)", path) and (not parsed.netloc or parsed.hostname == "cslabcg.whu.edu.cn"):
            return '"' + path + '"'
        return '"[redacted]"'

    source = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', replace, source)
    source = re.sub(r"\b\d{6,}\b", "[number]", source)
    return re.sub(r"\s+", " ", source).strip()


def _page_hints(tree, listing=False):
    scripts, snippets, containers = [], [], []
    pattern = r"changeCourse|doChangeCourse|dropdown-item-course"
    if listing:
        pattern += r"|ajax|fetch\s*\(|\.load\s*\(|\burl\s*:|assign(?:ment|List)"
    for node in tree.walk():
        if node.tag == "script":
            if node.attrs.get("src"):
                scripts.append(_route(urljoin(ORIGIN + _ASSIGN_PATH if listing else MAIN, node.attrs["src"])))
                continue
            source = "".join(child for child in node.children if isinstance(child, str))
            safe = _sanitize_script(source)
            ranges = []
            for match in re.finditer(pattern, safe, re.I):
                start, end = max(0, match.start() - 220), min(len(safe), match.end() + 400)
                if ranges and start <= ranges[-1][1]:
                    ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
                else:
                    ranges.append((start, end))
            for start, end in ranges[:8]:
                snippets.append(safe[start:end][:1800])
        if node.tag in {"table", "form"} or (node.tag in {"div", "section", "ul"} and re.search(r"assign|work|list|table|grid|view|content|container", node.attrs.get("id", "") + " " + node.attrs.get("class", ""), re.I)):
            containers.append({"tag": node.tag, "id": node.attrs.get("id", "")[:100], "class": node.attrs.get("class", "")[:160], "attribute_names": sorted(node.attrs)})
    return {"script_sources": scripts[:25], "inline_hints": snippets[:8], "containers": containers[:25]}


def _assignments(tree, course, warnings, course_scope=COURSE_SCOPE):
    found = {}
    for anchor in tree.walk():
        assign_id = _assignment_id(anchor) if anchor.tag == "a" else ""
        if not assign_id:
            continue
        container = anchor
        while container.parent and container.parent.tag not in {"body", "html", "root"}:
            parent_ids = {_assignment_id(node) for node in container.parent.walk() if node.tag == "a"} - {""}
            if parent_ids != {assign_id}:
                break
            container = container.parent
            if container.tag in {"tr", "li", "article"}:
                break
        columns = _table_columns(container) if container.tag == "tr" else {}
        title = next((value for key, value in columns.items() if key.strip() in {"作业名称", "任务名称", "作业", "名称"} and value.strip()), "")
        if not title and anchor.text().strip() not in _ACTION_TEXT:
            title = anchor.text().strip()
        if not title:
            titles = [node.text().strip() for node in container.walk() if node.tag in {"h2", "h3", "h4", "h5", "h6"}
                      and node.text().strip() and not _scoped(node.text(), course_scope)]
            title = titles[0] if len(titles) == 1 else ""
        text = " ".join(container.text().split())
        due = _dates(text, r"(?:截止时间|截止日期|结束时间|提交截止|作业截止)")
        start = _dates(text, r"(?:开始时间|开放时间)")
        for key, target in (("due", r"截止|结束"), ("start", r"开始|开放")):
            values = [_datetime(value.strip()) for label, value in columns.items() if re.search(target, label) and value.strip()]
            if values:
                parsed_values = set(values)
                parsed = next(iter(parsed_values)) if len(parsed_values) == 1 and None not in parsed_values else None
                if key == "due":
                    due = parsed if not due or due == parsed else None
                else:
                    start = parsed if not start or start == parsed else None
        if not title or not due:
            warnings.append("希冀作业 ID " + assign_id + " 缺少可核实的名称或完整年月日截止时间，已跳过；未进入作业作答页")
            continue
        if start and start > due:
            start = None
        status_text = next((value for key, value in columns.items() if "状态" in key), text)
        status = "completed" if any(word in status_text for word in ("已提交", "已完成", "已批阅", "已批改")) else "pending"
        found[assign_id] = {"title": title[:300], "course": course[:200], "source": "xiji", "kind": "assignment",
                           "due_at": due, "start_at": start, "status": status,
                           "source_key": "assignment:" + assign_id,
                           "url": ORIGIN + _ASSIGN_PATH + "?assignID=" + assign_id,
                           "notes": "希冀只读采集候选，首次使用请核对。\n时间依据：截止 " + due + "；开始 " + (start or "未知")}
    return list(found.values())


def _course_entries(tree, course_scope=COURSE_SCOPE):
    # Responsive menus may repeat the same option. They must still describe
    # one coherent ID/name mapping; never let the final duplicate silently
    # replace another course or hide a conflicting current-course marker.
    found, names = {}, {}
    for node in tree.walk():
        name, value = " ".join(node.text().split()), node.attrs.get("value", "")
        if not (node.has_class("dropdown-item-course") and re.fullmatch(r"\d+", value) and name):
            continue
        if ((value in found and found[value]["name"] != name)
                or (name in names and names[name] != value)):
            raise _ReadError("希冀课程菜单的课程ID与名称存在冲突，未执行课程切换")
        current = node.has_class("font-weight-bold") or found.get(value, {}).get("current", False)
        found[value] = {"id": value, "name": name, "current": current}
        names[name] = value
    if sum(entry["current"] for entry in found.values()) > 1:
        raise _ReadError("希冀课程菜单出现多个当前课程，未执行课程切换")
    return sorted((entry for entry in found.values() if _scoped(entry["name"], course_scope)),
                  key=lambda item: not item["current"])


def _assignment_entries(tree, course_id):
    found = {}
    for anchor in tree.walk():
        if anchor.tag != "a" or not anchor.has_class("list-group-item"):
            continue
        assign_id = _assignment_id(anchor, ORIGIN + _ASSIGN_PATH)
        fields = _params(anchor.attrs.get("href", ""))
        if not assign_id or (course_id and fields.get("courseid") != course_id):
            continue
        name = " ".join(anchor.text().split())
        name = re.sub(r"\s+(?:补交中|进行中|未开始|已结束|已完成|已提交)$", "", name).strip()
        query = "?" + ("courseID=" + course_id + "&" if course_id else "") + "assignID=" + assign_id
        found[assign_id] = {"id": assign_id, "title": name, "url": ORIGIN + _ASSIGN_PATH + query, "current": anchor.has_class("active")}
    return list(found.values())


def _metadata_text(node):
    if node.tag in {"table", "script", "style"}:
        return ""
    return " ".join(_metadata_text(child) if isinstance(child, _Node) else child for child in node.children)


_SUBMITTED_LABELS = ("已提交", "已完成", "已评阅", "已批阅", "已批改", "待评阅", "待批阅", "待批改", "已交卷", "已交")
_PENDING_LABELS = ("尚未提交", "未提交答案", "未提交", "无提交", "未完成", "待提交", "已撤回", "已退回", "未交")


def _explicitly_hidden(node):
    while node:
        if ("hidden" in node.attrs or node.attrs.get("aria-hidden", "").lower() == "true"
                or re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", node.attrs.get("style", ""), re.I)):
            return True
        node = node.parent
    return False


def _submission_cell_text(node):
    if node.tag in {"table", "script", "style", "input", "textarea", "select", "option", "pre", "code", "a", "button"}:
        return ""
    return " ".join(_submission_cell_text(child) if isinstance(child, _Node) else child for child in node.children)


def _normalized_submission_text(cell):
    """Keep explicit state words or the observed personal timestamp only."""
    text = " ".join(_submission_cell_text(cell).split())
    text = re.sub(r"^(?:提交状态|状态)\s*[:：]\s*", "", text)
    tokens = [token for token in re.split(r"[\s,，;；/|、()（）]+", text) if token]
    if tokens and all(token in _PENDING_LABELS + _SUBMITTED_LABELS for token in tokens):
        return " ".join(tokens)
    if any(label in text for label in _PENDING_LABELS):
        return ""
    dates = [match.group(1) for match in re.finditer(r"初次提交时间\s*[:：]\s*(" + _DATE.pattern + ")", text)
             if _datetime(match.group(1))]
    return "初次提交时间：" + dates[0] if len(set(dates)) == 1 else ""


def _badge_tone_matches(classes, tone):
    """Allow layout decoration while rejecting unknown/conflicting colors."""
    if not {"badge", "badge-" + tone} <= classes:
        return False
    colors = {value for value in classes if value.startswith(("badge-", "bg-", "text-bg-")) and value != "badge-pill"}
    if not colors <= {"badge-" + tone, "bg-" + tone, "text-bg-" + tone}:
        return False
    text_colors = {"text-" + value for value in ("primary", "secondary", "success", "danger", "warning", "info", "light", "dark", "muted")}
    return not (classes & text_colors - {"text-" + tone})


def _numbered_submission_statuses(tree):
    """Read the observed two-cell question layout, not question/answer text.

    Its personal state is one direct, leaf span.badge in the question's td.
    Reject incomplete numbering, extra tables, hidden rows and conflicting or
    mismatched badges rather than inferring completion from arbitrary text.
    """
    tables = [node for node in tree.walk() if node.tag == "table"]
    if len(tables) != 1:
        return []
    table = tables[0]
    ancestor = table.parent
    while ancestor:
        if ancestor.tag in {"table", "td", "th", "pre", "code", "textarea", "script", "style", "a", "button"}:
            return []
        ancestor = ancestor.parent
    rows = [node for node in table.walk() if node.tag == "tr"]
    if not rows or len(rows) > 100:
        return []
    cells_by_row, numbers = [], []
    for row in rows:
        cells = [node for node in row.children if isinstance(node, _Node) and node.tag in {"th", "td"}]
        if (len(cells) != 2 or [cell.tag for cell in cells] != ["th", "td"]
                or any(cell.attrs.get(name, "1") != "1" for cell in cells for name in ("colspan", "rowspan"))):
            return []
        match = re.fullmatch(r"([1-9]\d{0,2})[.．、]", " ".join(cells[0].text().split()))
        if not match:
            return []
        numbers.append(int(match.group(1)))
        cells_by_row.append(cells)
    if numbers != list(range(1, len(rows) + 1)):
        return []
    statuses = []
    for row, (number_cell, cell) in zip(rows, cells_by_row):
        markers = [node for node in cell.children if isinstance(node, _Node) and node.tag == "span" and node.has_class("badge")]
        status = ""
        if len(markers) == 1 and not _explicitly_hidden(row) and not _explicitly_hidden(number_cell):
            marker = markers[0]
            # The real status element has no nested markup. Do not borrow
            # badge-like markup from a nested question, answer or attachment.
            if not _explicitly_hidden(marker) and not any(isinstance(child, _Node) for child in marker.children):
                label = " ".join(marker.text().split())
                classes = set(marker.attrs.get("class", "").split())
                if label in _SUBMITTED_LABELS and _badge_tone_matches(classes, "success"):
                    status = label
                elif label in _PENDING_LABELS and any(_badge_tone_matches(classes, tone) for tone in ("danger", "warning")):
                    status = label
        statuses.append(status)
    return statuses


def _submission_statuses(tree):
    statuses = []
    for table in tree.walk():
        if table.tag != "table":
            continue
        ancestor, nested = table.parent, False
        while ancestor:
            if ancestor.tag in {"table", "td", "th", "pre", "code", "textarea", "script", "style", "a", "button"}:
                nested = True
                break
            ancestor = ancestor.parent
        if nested or _explicitly_hidden(table):
            continue
        rows = []
        for node in table.walk():
            if node.tag != "tr":
                continue
            parent = node.parent
            while parent and parent.tag != "table":
                parent = parent.parent
            if parent is table:
                rows.append(node)
        if not rows:
            continue
        header_cells = [node for node in rows[0].children if isinstance(node, _Node) and node.tag in {"th", "td"}]
        if (_explicitly_hidden(rows[0]) or any(_explicitly_hidden(cell) for cell in header_cells)
                or header_cells and re.fullmatch(r"[1-9]\d{0,2}[.．、]", " ".join(header_cells[0].text().split()))):
            continue
        headers = [" ".join(_submission_cell_text(node).split()) for node in header_cells]
        status_index = next((index for index, text in enumerate(headers) if "提交" in text and "状态" in text), None)
        if status_index is None:
            continue
        for row in rows[1:]:
            cells = [node for node in row.children if isinstance(node, _Node) and node.tag in {"th", "td"}]
            if (len(cells) <= status_index or _explicitly_hidden(row)
                    or _explicitly_hidden(cells[status_index])):
                statuses.append("")
            else:
                statuses.append(_normalized_submission_text(cells[status_index]))
    return statuses if statuses else _numbered_submission_statuses(tree)


def _submission_evidence(tree):
    """Emit bounded row shapes and allowlisted metadata, never task contents.

    Some assignment pages use numbered question rows rather than a named
    submission-status column. Preserve their structure for diagnosis without
    storing the question, answer, attachment, link, or form values.
    """
    labels = _PENDING_LABELS + _SUBMITTED_LABELS
    date_labels = {"initial_submitted_at": "初次提交时间", "last_modified_at": "最后一次修改时间"}
    marker_tags = {"td", "th", "span", "b", "strong", "p", "div", "label", "small", "i", "em"}
    marker_classes = {"badge", "label", "text-success", "text-danger", "text-warning", "text-muted", "text-info",
                      "badge-success", "badge-danger", "badge-warning", "badge-info", "label-success", "label-danger",
                      "label-warning", "label-info", "alert", "alert-success", "alert-danger", "alert-warning",
                      "bg-success", "bg-danger", "bg-warning", "bg-info", "text-bg-success", "text-bg-danger", "text-bg-warning"}
    safe_tags = marker_tags | {"tr", "li", "section", "a", "button", "input", "textarea", "pre", "code", "img", "br", "svg", "path", "form", "select", "script", "style"}

    def element_shape(node, cell):
        classes = set(node.attrs.get("class", "").split())
        child_tags = [child.tag for child in node.children if isinstance(child, _Node)]
        shape = {"tag": node.tag if node.tag in safe_tags else "other",
                 "classes": sorted(classes & marker_classes),
                 "class_count": len(classes), "unrecognized_class_count": len(classes - marker_classes),
                 "element_child_count": len(child_tags),
                 "element_child_tags": [tag if tag in safe_tags else "other" for tag in child_tags[:10]],
                 "direct_cell_child": node.parent is cell}
        text = " ".join(_submission_cell_text(node).split())
        if text in labels:
            shape["status_label"] = text
        return shape

    tables = [node for node in tree.walk() if node.tag == "table"]
    evidence = []
    for table_index, table in enumerate(tables[:10]):
        rows = []
        for node in table.walk():
            if node.tag != "tr":
                continue
            parent = node.parent
            while parent and parent.tag != "table":
                parent = parent.parent
            if parent is table:
                rows.append(node)
        row_evidence = []
        for row_index, row in enumerate(rows[:50]):
            cells = [child for child in row.children if isinstance(child, _Node) and child.tag in {"th", "td"}]
            item = {"row": row_index, "cell_count": len(cells), "cell_tags": [cell.tag for cell in cells[:20]],
                    "explicitly_hidden": _explicitly_hidden(row), "metadata_cells": []}
            for column, cell in enumerate(cells[:20]):
                text = " ".join(_submission_cell_text(cell).split())
                info = {"column": column, "tag": cell.tag}
                direct_elements = [element_shape(child, cell) for child in cell.children if isinstance(child, _Node)]
                if direct_elements:
                    info["direct_element_nodes"] = direct_elements[:15]
                if re.fullmatch(r"\d{1,3}[.．、]", text):
                    info["numbered_label"] = int(text[:-1])
                if text in {"提交状态", "题目", "题号", "#"}:
                    info["header_label"] = text
                found_labels = [label for label in labels if label in text]
                if found_labels:
                    info["status_labels"] = found_labels
                markers = []
                for marker in cell.walk():
                    if marker.tag not in marker_tags:
                        continue
                    ancestor, admissible = marker, True
                    while ancestor is not cell:
                        if ancestor.tag in {"table", "script", "style", "input", "textarea", "select", "option", "pre", "code", "a", "button"}:
                            admissible = False
                            break
                        ancestor = ancestor.parent
                    marker_text = " ".join(_submission_cell_text(marker).split())
                    if admissible and marker_text in labels:
                        prior = []
                        if marker.parent:
                            for sibling in marker.parent.children:
                                if sibling is marker:
                                    break
                                prior.append(_submission_cell_text(sibling) if isinstance(sibling, _Node) else sibling)
                        markers.append({**element_shape(marker, cell),
                                        "parent_tag": marker.parent.tag if marker.parent and marker.parent.tag in marker_tags | {"tr", "li", "section"} else "other",
                                        "submission_label_adjacent": bool(re.search(r"提交状态\s*[:：]\s*$", " ".join(prior)))})
                if markers:
                    info["exact_status_nodes"] = markers[:10]
                for field, label in date_labels.items():
                    dates = [_datetime(match.group(1)) for match in re.finditer(re.escape(label) + r"\s*[:：]\s*(" + _DATE.pattern + ")", text)]
                    if dates:
                        info[field] = list(dict.fromkeys(value for value in dates if value))[:5]
                if len(info) > 2:
                    item["metadata_cells"].append(info)
            row_evidence.append(item)
        evidence.append({"table": table_index, "row_count": len(rows), "rows": row_evidence, "rows_truncated": len(rows) > 50})
    return {"table_count": len(tables), "tables": evidence, "tables_truncated": len(tables) > 10,
            "recognized_summary": _submission_summary(tree)}


def _submission_summary(tree):
    states = []
    for status in _submission_statuses(tree):
        words = status.split()
        pending = any(word in _PENDING_LABELS for word in words)
        submitted = any(word in _SUBMITTED_LABELS for word in words)
        if pending and submitted:
            states.append("unverified")
        elif pending:
            states.append("pending")
        elif submitted:
            states.append("submitted")
        elif any(_datetime(match.group(1)) for match in re.finditer(r"初次提交时间\s*[:：]\s*(" + _DATE.pattern + ")", status)):
            states.append("submitted")
        else:
            states.append("unverified")
    return {state: states.count(state) for state in set(states)}


def _assignment_detail(tree, entry, course, course_id, warnings):
    text = " ".join(_metadata_text(tree).split())
    ranges = set()
    for match in re.finditer(r"作业时间\s*[:：]?\s*(" + _DATE.pattern + r")\s*(?:至|到|~|～|—|--|-)\s*(" + _DATE.pattern + ")", text):
        start, due = _datetime(match.group(1)), _datetime(match.group(2))
        if start and due and start <= due:
            ranges.add((start, due))
    if len(ranges) != 1:
        warnings.append(course + "：" + entry["title"][:60] + "未发现唯一且完整年月日的作业时间，已跳过")
        return None
    start, original_due = next(iter(ranges))
    late = _dates(text, r"(?:作业)?补交时间(?:截至|截止)")
    if re.search(r"补交时间(?:截至|截止)", text) and not late:
        warnings.append(course + "：" + entry["title"][:60] + "补交截止时间缺少年份、未知或存在冲突，已跳过")
        return None
    due = late if late and late >= original_due else original_due
    summary = _submission_summary(tree)
    status = "completed" if summary and summary.get("submitted", 0) and not summary.get("pending", 0) and not summary.get("unverified", 0) else "pending"
    notes = "希冀只读采集候选，首次使用请核对。\n原作业截止：" + original_due
    if late and late >= original_due:
        notes += "\n平台允许补交至：" + late + "；平台提示补交会扣分。本条提醒按补交截止时间设置。"
    if summary.get("unverified") or not summary:
        notes += "\n平台提交状态未能可靠确认，请本人核对并按实际情况标记完成。"
    title = entry["title"] + ("（补交截止）" if late and late >= original_due else "")
    return {"title": title[:300], "course": course[:200], "source": "xiji", "kind": "assignment",
            "due_at": due, "start_at": start, "status": status, "source_key": "course:" + course_id + ":assignment:" + entry["id"],
            "url": entry["url"], "notes": notes}


def collect_xiji(context, course_scope=COURSE_SCOPE):
    """Read configured courses and restore the initial selection when known."""
    course_scope = tuple(name.strip() for name in course_scope if isinstance(name, str) and name.strip())
    reader = _Reader(context)
    candidates, warnings = [], []
    auth = {"authenticated": False, "status": "unverified", "message": "尚未检查", "signals": []}
    coverage = {"courses_in_scope": list(course_scope), "current_course_identified": False,
                "initial_course_identified": False,
                "course_lists_read": 0, "assignment_links_found": 0, "assignment_candidates": 0,
                "assignment_details_read": 0, "assignment_statuses_verified": 0, "assignment_statuses_unverified": 0,
                "courses_completed": [], "disabled": not course_scope, "complete": False}
    controls, course_contexts, list_structure, main_hints, list_hints = [], [], {}, {}, {}
    structures, original, initial_main, switched = {}, None, None, False
    submission_evidence = {}
    if not course_scope:
        coverage["complete"] = True
        auth.update(status="disabled", message="未设置希冀课程，已跳过采集")
        return {"candidates": [], "warnings": [], "auth": auth,
                "diagnostics": {**reader.diagnostics(), "coverage": coverage}}
    try:
        auth, main = _probe_with_campus_session(reader)
        initial_main = main
        courses = []
        controls = _course_controls(main, course_scope)
        course_contexts = _course_contexts(main, course_scope)
        main_hints = _page_hints(main)
        if not auth["authenticated"]:
            warnings.append(auth["message"])
        else:
            # Keep the initial dropdown selection even when outside the user's
            # collection scope, so a scoped course can be visited and restored.
            all_courses = _course_entries(main, None)
            original = next((entry for entry in all_courses if entry["current"]), None)
            courses = [entry for entry in all_courses if _scoped(entry["name"], course_scope)]
            if len(_current_course_names(main, course_scope)) > 1:
                raise _ReadError("希冀主页出现相互冲突的当前课程名称，未执行课程切换")
            course = original["name"] if original and courses else _current_course(main, course_scope)
            frame_course = _frame_course(context, course_scope)
            if not course:
                course = frame_course
                if course:
                    coverage["course_name_source"] = "loaded_frame_title"
            elif any(node.has_class("dropdown-item-course") and node.has_class("font-weight-bold") and " ".join(node.text().split()) == course for node in main.walk()):
                # The GET response is the current server-side selection; an
                # already-loaded browser frame can still show a previous one.
                coverage["course_name_source"] = "selected_course_dropdown"
            elif frame_course and frame_course != course:
                raise _ReadError("希冀主页与已加载课程框架的课程名存在冲突，暂不导入，请打开目标课程后重试")
            coverage["current_course_identified"] = bool(course)
            coverage["initial_course_identified"] = bool(course)
            if not original and course:
                original = next((entry for entry in all_courses if entry["name"] == course), None)
            if not course and not courses:
                warnings.append("希冀主页未识别到唯一且明确的当前课程名称，暂不导入，请核对课程范围及当前课程")
            else:
                if not courses:
                    courses = [{"id": "", "name": course, "current": True}]
                active_id = original["id"] if original else ""
                if len(courses) > 4:
                    warnings.append("希冀匹配课程超过4门，本次按请求上限仅核对前4门，其余课程仍需人工核对")
                for target in courses[:4]:
                    try:
                        course_main = main
                        if target["id"] and target["id"] != active_id:
                            # A renewed platform session can be authenticated
                            # without a selected course. Select an observed,
                            # configured numeric option through the verified
                            # navigation form instead of guessing a default.
                            switched = True
                            course_main = reader.select_course(target["id"], initial_main)
                            returned_courses = _course_entries(course_main, None)
                            if (_current_course(course_main, None) != target["name"]
                                    or not any(entry["id"] == target["id"] and entry["name"] == target["name"]
                                               for entry in returned_courses)):
                                raise _ReadError("希冀课程切换结果未确认，已停止读取该课程")
                            active_id = target["id"]
                            coverage["current_course_identified"] = True
                            if not coverage["initial_course_identified"]:
                                coverage["course_name_source"] = "verified_course_selection"
                        navigation = _navigation(course_main)
                        if "在线作业" not in navigation:
                            raise _ReadError("希冀当前课程未提供已验证的在线作业导航")
                        listing = reader.get(navigation["在线作业"])
                        listed_course = _current_course(listing, course_scope)
                        if listed_course and listed_course != target["name"]:
                            raise _ReadError("希冀作业列表课程名与当前课程不一致，已跳过")
                        coverage["course_lists_read"] += 1
                        list_structure = _assignment_structure(listing)
                        structures[target["name"]] = list_structure
                        list_hints = _page_hints(listing, listing=True)
                        entries = _assignment_entries(listing, target["id"])
                        coverage["assignment_links_found"] += len(entries)
                        successful = 0
                        for entry in entries[:40]:
                            detail = listing if entry["current"] else reader.get(entry["url"])
                            detail_course = _current_course(detail, course_scope)
                            if detail_course and detail_course != target["name"]:
                                warnings.append(target["name"] + "：作业详情课程名不一致，已跳过")
                                continue
                            coverage["assignment_details_read"] += 1
                            evidence = _submission_evidence(detail)
                            submission_evidence["course:" + target["id"] + ":assignment:" + entry["id"]] = evidence
                            candidate = _assignment_detail(detail, entry, target["name"], target["id"], warnings)
                            if candidate:
                                candidates.append(candidate)
                                summary = evidence["recognized_summary"]
                                if summary and not summary.get("unverified", 0):
                                    coverage["assignment_statuses_verified"] += 1
                                    successful += 1
                                else:
                                    coverage["assignment_statuses_unverified"] += 1
                                    warnings.append(target["name"] + "：" + entry["title"][:60]
                                                    + "的提交状态尚未确认，已保留截止时间；本课程未计为完整核对，无需因此重新登录")
                        if not entries:
                            candidates.extend(_assignments(listing, target["name"], warnings, course_scope))
                        if entries and successful == len(entries) or list_structure["explicit_empty"]:
                            coverage["courses_completed"].append(target["name"])
                        if len(entries) > 40:
                            warnings.append(target["name"] + "：作业超过40项，本次结果截断")
                    except _LoginRequired:
                        raise
                    except _ReadError as exc:
                        warnings.append(target["name"] + "：" + str(exc))
            # Unknown link-based switch controls remain discovery-only.
            switches = [node for node in main.walk() if node.tag == "a" and _scoped(node.text(), course_scope) and _params(node.attrs.get("href", "")).get("courseid")]
            if switches:
                for switch in switches[:5]:
                    route = _route(urljoin(MAIN, switch.attrs.get("href", "")))
                    warnings.append("发现课程切换链接，尚未确认其只读行为，未访问（" + route["host"] + route["path"] + "）")
            coverage["complete"] = bool(courses and len(coverage["courses_completed"]) == len(courses)
                and all(any(scope in name for name in coverage["courses_completed"]) for scope in course_scope))
            if not coverage["complete"]:
                warnings.append("希冀配置范围内的作业尚未全部核对，未覆盖课程或时间缺失项仍需人工核对")
    except _LoginRequired as exc:
        auth = {"authenticated": False, "status": "not_authenticated", "message": str(exc), "signals": []}
        warnings.append(str(exc))
    except Exception as exc:
        warnings.append(str(exc) if isinstance(exc, _ReadError) else "希冀页面结构尚未支持，本次采集未完成，请人工核对")
    finally:
        if switched and reader.navigation_attempted and original and initial_main:
            restore = _Reader(context, budget=15)
            try:
                restored = restore.select_course(original["id"], initial_main)
                coverage["initial_course_restored"] = _current_course(restored, None) == original["name"]
                if not coverage["initial_course_restored"]:
                    warnings.append("希冀初始课程恢复未确认，请在平台核对当前课程选择")
            except Exception:
                coverage["initial_course_restored"] = False
                warnings.append("希冀初始课程恢复失败，请在平台重新选择原课程")
            reader.count += restore.count
            reader.routes.extend(restore.routes)
        elif switched and reader.navigation_attempted and not original:
            # No initial choice was established. Do not invent an ID to
            # restore, or mislabel successfully read courses as incomplete.
            coverage["initial_course_restored"] = None
            coverage["initial_course_restore_reason"] = "initial_course_not_identified"
    unique = {item["source_key"]: item for item in candidates}
    coverage["assignment_candidates"] = len(unique)
    return {"candidates": list(unique.values()), "warnings": list(dict.fromkeys(warnings)), "auth": auth,
            "diagnostics": {**reader.diagnostics(), "coverage": coverage, "course_controls": controls,
                            "course_contexts": course_contexts, "assignment_list_structure": list_structure,
                            "assignment_structures_by_course": structures,
                            "assignment_submission_evidence": submission_evidence,
                            "main_page_hints": main_hints, "assignment_page_hints": list_hints}}
