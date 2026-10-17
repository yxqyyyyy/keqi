"""Read-only Chaoxing adapter for a user-authenticated Playwright BrowserContext.

Protocol reference: DanMo661/Xuexitong-mcp, MIT, commit
ebce2cc735bc7acc45fac7ccf32bcefe1a6b2a54, xuexitong_mcp/client.py.
This implementation does not import its login/password code. It reads homework
time metadata as inert HTML, never runs answering or exam-start actions, never
submits a form, and never fills a missing year with today's year.
The platform protocol is unofficial; collected deadlines require user review.
"""
from __future__ import annotations

import html
import hashlib
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from .core import TZ

BASE = "https://i.chaoxing.com/base"
COURSES = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
MIDDLE = "https://mooc1.chaoxing.com/visit/stucoursemiddle"
WORKS = "https://mooc1.chaoxing.com/mooc2/work/list"
EXAMS = "https://mooc1-api.chaoxing.com/exam-ans/exam/test/examcode/examlist"
ALLOWED_PATHS = {
    "/base", "/mooc2-ans/visit/courselistdata", "/visit/stucoursemiddle",
    "/mooc2/work/list", "/mooc-ans/mooc2/work/task", "/mooc2/work/task",
    "/exam-ans/exam/test/examcode/examlist", "/exam/test/examcode/examlist",
}
# A normal course page documented by the fixed reference commit's URL_CHAPTER.
# Keep this additional redirect destination bound to its observed host, rather
# than admitting similarly named learning/answer endpoints on every subdomain.
ALLOWED_ROUTES = {
    ("mooc2-ans.chaoxing.com", "/mooc2-ans/mycourse/studentcourse"),
    # Observed in the user's authenticated 302 from /visit/stucoursemiddle.
    ("mooc2-ans.chaoxing.com", "/mooc2-ans/mycourse/stu"),
}
HOMEWORK_COURSES = ("概率论",)
EXAM_COURSES = ("大物", "大学物理")
WORK_TASK_PATHS = {"/mooc-ans/mooc2/work/task", "/mooc2/work/task"}
WORK_METADATA_PATH = "/mooc-ans/mooc2/work/dowork"
FULL_DATE = r"20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?[ T\s]+\d{1,2}[:：]\d{2}(?:[:：]\d{2})?"
SHORT_DATE = r"\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}"
DATE_RE = re.compile(FULL_DATE)
DONE = ("已提交", "待批阅", "已批阅", "已批改", "已完成")
PENDING = ("未提交", "未交", "待提交", "未完成", "未作答", "待作答", "作答中", "答题中", "进行中", "未开始", "待完成", "已撤回", "已退回")


class _Node:
    def __init__(self, tag="root", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        return " ".join(x.text() if isinstance(x, _Node) else x for x in self.children).strip()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class _Tree(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, attrs, self.stack[-1]))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _inputs(tree):
    result = {}
    for node in tree.walk():
        if node.tag == "input":
            for key in (node.attrs.get("id"), node.attrs.get("name")):
                if key:
                    result[key.lower()] = node.attrs.get("value", "")
    return result


def _number(source, name):
    match = re.search(r'(?:var|let|const)\s+' + re.escape(name) + r'\s*=\s*[\'\"]?(\d+)', source)
    if not match:
        match = re.search(r'[\'\"]' + re.escape(name) + r'[\'\"]\s*:\s*[\'\"]?(\d+)', source)
    return match.group(1) if match and int(match.group(1)) > 0 else ""


def _params(url):
    return {key.lower(): values[0] for key, values in parse_qs(urlparse(html.unescape(url)).query).items() if values}


def _node_params(node):
    """Read URL attributes as data; do not run onclick or follow action links."""
    fields = {}
    values = [node.attrs.get(key, "") for key in ("href", "data", "data-url")]
    # Some tables put the URL inside an onclick function argument. Only read
    # quoted URL strings, and at most one nested refer URL, without executing JS.
    values.extend(re.findall(r"['\"]([^'\"]*[?&](?:courseId|examId|refer|moocId)=[^'\"]*)['\"]", node.attrs.get("onclick", ""), re.I))
    for value in values:
        parsed = _params(value)
        fields.update(parsed)
        if parsed.get("refer"):
            fields.update(_params(unquote(parsed["refer"])))
    if "moocid" in fields and "courseid" not in fields:
        fields["courseid"] = fields["moocid"]
    return fields


def _safe_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        known_path = (host == "chaoxing.com" or host.endswith(".chaoxing.com")) and parsed.path in ALLOWED_PATHS
        return parsed.scheme == "https" and not parsed.username and not parsed.password and (
            known_path or (host, parsed.path) in ALLOWED_ROUTES
        )
    except ValueError:
        return False


def _route(url):
    """Minimal local diagnostics: never retain query, fragment or credentials."""
    try:
        parsed = urlparse(url)
        return {"host": (parsed.hostname or "")[:160],
                "path": re.sub(r"[\x00-\x20\x7f]", "", parsed.path)[:240] or "/"}
    except ValueError:
        return {"host": "invalid", "path": "/"}


def _safe_work_redirect(origin, target):
    """Only follow the observed homework detail redirect for the same work.

    The destination serves the user's homework page. Read its server-returned
    time metadata as inert HTML; never execute its scripts or submit answers.
    It is intentionally absent from the general URL allowlist, so a direct
    answer-page URL or an exam action URL cannot enter this path.
    """
    try:
        first, final = urlparse(origin), urlparse(target)
        if (first.scheme != "https" or first.hostname != "mooc1.chaoxing.com" or first.path not in WORK_TASK_PATHS
                or final.scheme != "https" or final.hostname != first.hostname or final.path != WORK_METADATA_PATH
                or final.username or final.password):
            return False
        source, destination = _params(origin), _params(target)
        return all(re.fullmatch(r"\d+", source.get(key, "")) and source[key] == destination.get(key)
                   for key in ("courseid", "classid", "workid"))
    except ValueError:
        return False


class _ReadError(Exception):
    pass


class _LoginRequired(_ReadError):
    pass


class _Reader:
    def __init__(self, context, budget=65):
        self.context = context
        self.deadline = time.monotonic() + budget
        self.count = 0
        self.routes = []

    def diagnostics(self):
        return {"requests": self.count, "routes": self.routes}

    def get(self, url, params=None):
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        work_origin = url
        for _ in range(5):
            if not _safe_url(url) and not _safe_work_redirect(work_origin, url):
                route = _route(url)
                self.routes.append({**route, "result": "blocked_route"})
                raise _ReadError("平台返回了不在只读列表范围内的地址，已停止读取（" + route["host"] + route["path"] + "）")
            remaining = self.deadline - time.monotonic()
            if remaining < 1 or self.count >= 100:
                raise _ReadError("本次采集达到时间或请求数上限，结果可能不完整，请稍后重试")
            if self.count:
                time.sleep(min(0.2, max(0, remaining - 0.5)))
            self.count += 1
            try:
                response = self.context.request.get(url, timeout=min(10000, max(500, int(remaining * 1000))), max_redirects=0)
            except Exception as exc:
                raise _ReadError("学习通请求超时或网络失败，请检查网络后重试") from exc
            status = response.status
            event = {**_route(url), "status": status}
            self.routes.append(event)
            if 300 <= status < 400:
                target = urljoin(url, response.headers.get("location", ""))
                event["redirect"] = _route(target)
                if "passport" in (urlparse(target).hostname or "") or "/login" in urlparse(target).path.lower():
                    raise _LoginRequired("学习通登录已失效，请在专用浏览器中本人重新登录")
                url = target
                continue
            if status in {401, 403}:
                raise _ReadError("学习通拒绝访问；请核对登录状态、课程权限或验证码，不会自动重试登录")
            if status == 429:
                raise _ReadError("学习通提示访问过于频繁，本次已停止，请稍后再采集")
            if status != 200:
                raise _ReadError("学习通暂时未能返回列表（HTTP " + str(status) + "）")
            source = response.text()
            if len(source) > 4_000_000:
                raise _ReadError("平台页面超出预期大小，已停止解析")
            tree = _Tree(source).root
            if any(node.tag == "input" and node.attrs.get("type", "").lower() == "password" for node in tree.walk()):
                raise _LoginRequired("当前返回登录页面，请在专用浏览器中本人完成登录")
            if "没有此页面访问权限" in tree.text() or "无效的用户" in tree.text():
                raise _ReadError("学习通当前会话或课程权限无效，请本人重新登录并确认课程可访问")
            return source, tree
        raise _ReadError("学习通页面重定向次数异常，未能读取")


def _probe(reader):
    source, tree = reader.get(BASE)
    values = _inputs(tree)
    uid = _number(source, "uid") or _number(source, "_uid") or values.get("uid", "")
    positive_uid = bool(re.fullmatch(r"\d+", uid or "") and int(uid) > 0)
    menu = any("chaoxing.com" in node.attrs.get("dataurl", "") and any(
        part in node.attrs.get("dataurl", "") for part in ("interaction", "courselist", "mycourse", "examlist")
    ) for node in tree.walk())
    logout = any(node.tag == "a" and ("退出" in node.text() or "logout" in node.attrs.get("href", "").lower()) for node in tree.walk())
    signals = (["positive_user_id"] if positive_uid else []) + (["course_menu"] if menu else []) + (["logout_control"] if logout else [])
    authenticated = positive_uid and (menu or logout)
    return {"authenticated": authenticated, "status": "authenticated" if authenticated else "unverified",
            "message": "学习通登录已通过服务端个人空间校验" if authenticated else "未能确认学习通登录成功；请完成登录并打开个人空间后重试",
            "signals": signals}, source, tree


def probe_chaoxing_auth(context):
    """Verify server-side personal space; an open tab or cookie alone never passes."""
    try:
        result, _, _ = _probe(_Reader(context, budget=15))
        return result
    except _LoginRequired as exc:
        return {"authenticated": False, "status": "not_authenticated", "message": str(exc), "signals": []}
    except Exception as exc:
        return {"authenticated": False, "status": "error", "message": str(exc) if isinstance(exc, _ReadError) else "登录状态检查失败，请重试", "signals": []}


def _datetime(value):
    # Explicitly named time fields may contain Unix seconds or milliseconds.
    # Their year comes from the server value, never from the current clock.
    if re.fullmatch(r"\d{10}|\d{13}", value.strip()):
        try:
            stamp = int(value) / (1000 if len(value.strip()) == 13 else 1)
            parsed = datetime.fromtimestamp(stamp, tz=TZ)
            return parsed.isoformat(timespec="seconds") if 2000 <= parsed.year < 2100 else None
        except (ValueError, OverflowError, OSError):
            return None
    match = DATE_RE.fullmatch(value.strip())
    if not match:
        return None
    numbers = [int(part) for part in re.findall(r"\d+", match.group())]
    try:
        return datetime(*numbers[:5], numbers[5] if len(numbers) > 5 else 0, tzinfo=TZ).isoformat(timespec="seconds")
    except ValueError:
        return None


def _dates(tree):
    """Only explicitly labelled absolute times or named full-date input fields."""
    text = " ".join(tree.text().split())
    due_values, start_values = [], []
    for label, bucket in ((r"(?:截止时间|截止日期|结束时间|提交截止|作业截止|考试结束)", due_values),
                          (r"(?:开始时间|开放时间|开考时间)", start_values)):
        bucket.extend(match.group(1) for match in re.finditer(label + r"\s*[:：]?\s*(" + FULL_DATE + ")", text))
    match = re.search(r"(?:作答时间|考试时间)\s*[:：]?\s*(" + FULL_DATE + "|" + SHORT_DATE + r")\s*(?:至|到|~|～|—|--|\s-\s)\s*(" + FULL_DATE + "|" + SHORT_DATE + ")", text)
    if match:
        start_values.append(match.group(1))
        due_values.append(match.group(2))
    fields = _inputs(tree)
    for key in ("endtime", "deadline", "workendtime", "examendtime", "answerendtime"):
        if fields.get(key):
            due_values.append(fields[key])
    for key in ("starttime", "workstarttime", "examstarttime", "answerstarttime"):
        if fields.get(key):
            start_values.append(fields[key])
    due = {parsed for value in due_values if (parsed := _datetime(value))}
    start = {parsed for value in start_values if (parsed := _datetime(value))}
    for values, parsed_values in ((due_values, due), (start_values, start)):
        if len(parsed_values) == 1:
            parsed = datetime.fromisoformat(next(iter(parsed_values)))
            for value in values:
                if re.fullmatch(SHORT_DATE, value.strip()):
                    if [int(part) for part in re.findall(r"\d+", value)] != [parsed.month, parsed.day, parsed.hour, parsed.minute]:
                        parsed_values.clear()
                        break
    # Conflicting visible and hidden dates are review-required, never silently chosen.
    return (next(iter(due)) if len(due) == 1 else None,
            next(iter(start)) if len(start) == 1 else None)


def _courses(tree):
    found = {}
    for anchor in tree.walk():
        href = anchor.attrs.get("href", "")
        if "stucoursemiddle?" not in href:
            continue
        fields = _params(href)
        if not all(re.fullmatch(r"\d+", fields.get(key, "")) for key in ("courseid", "clazzid", "cpi")):
            continue
        container, names = anchor, []
        # Pick the smallest enclosing course card; never use another card's name.
        for _ in range(10):
            choices = [node.attrs.get("title") or node.text() for node in container.walk() if node.has_class("course-name")]
            if len(choices) == 1:
                names = choices
                break
            if len(choices) > 1 or not container.parent:
                break
            container = container.parent
        name = next((v.strip() for v in names if v.strip()), anchor.attrs.get("title") or anchor.text() or "课程 " + fields["courseid"])
        found[(fields["courseid"], fields["clazzid"])] = {**fields, "name": name[:200]}
    return list(found.values())


def _completion_status(value):
    if any(mark in value for mark in ("已撤回", "已退回", "需重交", "需要重交")):
        return "pending"
    if any(mark in value for mark in DONE):
        return "completed"
    return "pending" if any(mark in value for mark in PENDING) else None


def _confirmed_work_dates(tree, year):
    """Use a user-confirmed start year for one explicit MM-DD work range."""
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year < 2100:
        return None, None, None
    text = " ".join(tree.text().split())
    pattern = r"作答时间\s*[:：]?\s*(" + SHORT_DATE + r")\s*(?:至|到|~|～|—|--|\s-\s)\s*(" + SHORT_DATE + ")"
    ranges = {(match.group(1), match.group(2), match.group(0)) for match in re.finditer(pattern, text)}
    if len(ranges) != 1:
        return None, None, None
    # Explicit absolute server dates must not be overridden by year confirmation.
    if re.search(r"(?:作答时间|截止时间|截止日期|结束时间|提交截止|开始时间|开放时间)\s*[:：]?\s*" + FULL_DATE, text):
        return None, None, None
    inputs = _inputs(tree)
    if any(_datetime(inputs.get(key, "")) for key in (
            "endtime", "deadline", "workendtime", "examendtime", "answerendtime",
            "starttime", "workstarttime", "examstarttime", "answerstarttime")):
        return None, None, None
    first, last, original = next(iter(ranges))
    start_parts, end_parts = ([int(part) for part in re.findall(r"\d+", value)] for value in (first, last))
    end_year = year + 1 if start_parts[0] == 12 and end_parts[0] == 1 else year
    try:
        start = datetime(year, *start_parts, tzinfo=TZ)
        end = datetime(end_year, *end_parts, tzinfo=TZ)
        if end < start or end.year >= 2100:
            return None, None, None
        return end.isoformat(timespec="seconds"), start.isoformat(timespec="seconds"), original
    except ValueError:
        return None, None, None


def _work_rows(tree, homework_year=None):
    rows = []
    for node in tree.walk():
        if node.tag != "li" or "goTask" not in node.attrs.get("onclick", ""):
            continue
        title_node = next((child for child in node.walk() if child.has_class("overHidden2")), None)
        status_node = next((child for child in node.walk() if child.has_class("status")), None)
        due, start = _dates(node)
        confirmed_text = None
        if not due and homework_year is not None:
            due, start, confirmed_text = _confirmed_work_dates(node, homework_year)
        rows.append({"title": title_node.text() if title_node else "", "status": status_node.text() if status_node else "",
                     "url": unquote(html.unescape(node.attrs.get("data", ""))), "due_at": due, "start_at": start,
                     "confirmed_year_text": confirmed_text})
    return rows


def _work_time_evidence(source, tree):
    """Small local date-only evidence, excluding questions and opaque values."""
    time_value = re.compile(r"(?:" + FULL_DATE + r")(?:Z|[+-]\d{2}:\d{2})?|(?:" + SHORT_DATE + r")|\d{10}|\d{13}|0")

    def is_time_field(name):
        name = name.lower()
        if re.search(r"enc|token|password|secret|signature|cookie|uid|cpi", name):
            return False
        return bool(re.fullmatch(r"start|end|begin|finish|deadline", name) or (
            re.search(r"start|end|begin|finish|deadline|work", name) and re.search(r"time|date|deadline", name)))

    def safe_value(value):
        return value.strip() if len(value) <= 80 and time_value.fullmatch(value.strip()) else None

    visible = " ".join(tree.text().split())
    # Read only a clock/date alphabet immediately following a time label. Do
    # not retain a general surrounding excerpt, which could include a question.
    fragments = []
    for match in re.finditer(
        r"(?:作答时间|截止时间|截止日期|提交截止|开始时间|结束时间|开放时间|剩余时间|剩余)\s*[:：]?\s*"
        r"[0-9年月日星期周一二三四五六天小时分钟秒无不限已截止结束至到~～—:：/.\-T +]{0,110}", visible):
        fragment = match.group(0).strip()
        dates = list(re.finditer(FULL_DATE + "|" + SHORT_DATE, fragment))
        if dates:
            fragment = fragment[:dates[-1].end()]
        fragments.append(fragment)
    inputs = [{"field": name, "value": value} for name, raw in _inputs(tree).items()
              if is_time_field(name) and (value := safe_value(raw)) is not None]
    scripts = []
    # This is literal matching, never JavaScript evaluation. A quoted value
    # must end at the matching quote; an unquoted epoch must end at punctuation.
    binding = re.compile(r"(?<![\w$])['\"]?([A-Za-z_$][\w$]{0,59})['\"]?\s*[:=]\s*(?:"
                         r"'([^'\r\n]{0,80})'|\"([^\"\r\n]{0,80})\"|(\d{10}|\d{13}|0)(?=\s*[,;}\r\n]))")
    for node in tree.walk():
        if node.tag != "script":
            continue
        script = "".join(child for child in node.children if isinstance(child, str))
        for match in binding.finditer(script):
            name, raw = match.group(1), next(value for value in match.groups()[1:] if value is not None)
            if is_time_field(name) and (value := safe_value(raw)) is not None:
                scripts.append({"field": name, "value": value})
                if len(scripts) >= 20:
                    break
        if len(scripts) >= 20:
            break
    return {"visible_time_fragments": list(dict.fromkeys(fragments))[:10], "time_inputs": inputs[:20], "script_time_fields": scripts}


def _candidate(title, course, kind, due, start, key, url, status, evidence):
    if start and start > due:
        start = None
    completion = _completion_status(status)
    return {"title": title[:300], "course": course[:200], "source": "chaoxing", "kind": kind,
            "due_at": due, "start_at": start, "status": completion or "pending",
            "source_key": key, "url": url, "notes": "学习通只读采集候选，首次使用请核对。\n时间依据：" + evidence[:1200]
            + ("\n平台提交状态未能可靠确认，请本人核对" if completion is None else "\n平台状态：" + " ".join(status.split())[:160])}


def _structural_label(value):
    """Retain CSS label shape without dynamic identifiers or opaque values."""
    labels = []
    for label in value.split()[:12]:
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", label)
                or re.search(r"[a-fA-F0-9]{16,}|[A-Za-z0-9]{24,}", label)
                or re.search(r"(?:token|enc|signature|session|secret|password|cookie)[_-]", label, re.I)):
            labels.append("[redacted]")
        else:
            labels.append(re.sub(r"\d+", "{number}", label))
    return " ".join(labels)


def _onclick_structure(value):
    """Describe simple calls as data. Positions are one-based; never run JS."""
    calls, mask, quote, escaped = [], [], None, False
    for char in value[:12000]:
        if quote:
            mask.append(" ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
            mask.append(" ")
        else:
            mask.append(char)
    # Search only executable syntax positions. A function-looking sequence in a
    # quoted token or URL must not become a diagnostic function name.
    for match in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$.]{0,79})\s*\(", "".join(mask)):
        start, quote, escaped, depth = match.end(), None, False, 0
        arguments, current = [], []
        for char in value[start:12000]:
            if quote:
                current.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
                current.append(char)
            elif char in "([{":
                depth += 1
                current.append(char)
            elif char == ")" and depth == 0:
                if current or arguments:
                    arguments.append("".join(current).strip())
                numeric = []
                for index, argument in enumerate(arguments):
                    literal = argument.strip()
                    if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in "\"'":
                        literal = literal[1:-1]
                    if re.fullmatch(r"-?\d{1,20}", literal):
                        numeric.append({"position": index + 1, "value": literal})
                calls.append({"function": match.group(1), "argument_count": len(arguments), "numeric_arguments": numeric})
                break
            elif char in ")]}":
                depth -= 1
                current.append(char)
            elif char == "," and depth == 0:
                arguments.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if len(calls) >= 12:
            break
    return calls


def _href_structure(value):
    try:
        parsed = urlparse(html.unescape(value))
        if parsed.scheme not in {"", "https", "http"}:
            return {"scheme": parsed.scheme[:30]}
        fields = parse_qs(parsed.query, keep_blank_values=True)
        names = [key for key in fields if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,39}", key)]
        id_fields = {"examid", "examrelationid", "courseid", "moocid", "classid", "clazzid", "workid", "answerid"}
        numeric_ids = {key: values[0] for key, values in fields.items()
                       if key.lower() in id_fields and len(values) == 1 and re.fullmatch(r"\d{1,20}", values[0])}
        route = _route(value)
        route["path"] = re.sub(r"(?i)(/(?:token|enc|signature|sign|session|sid)/)[^/]+", r"\1[redacted]", route["path"])
        return {**route, "query_fields": sorted(names)[:40], "numeric_ids": numeric_ids}
    except ValueError:
        return {"host": "invalid", "path": "/"}


def _exam_row_structure(row):
    """Local structural diagnosis only: no text, input values or raw HTML/JS."""
    count = 0

    def describe(node, depth=0):
        nonlocal count
        count += 1
        result = {"tag": node.tag[:30]}
        for name in ("id", "class"):
            if node.attrs.get(name):
                result[name] = _structural_label(node.attrs[name])
        if node.attrs.get("onclick"):
            result["onclick"] = _onclick_structure(node.attrs["onclick"])
        if node.attrs.get("href"):
            result["href"] = _href_structure(node.attrs["href"])
        children = []
        if depth < 8:
            for child in node.children:
                if isinstance(child, _Node) and count < 160:
                    children.append(describe(child, depth + 1))
        if children:
            result["children"] = children
        return result

    return describe(row)


def _exam_rows(tree, warnings, course_names=None, unidentified_rows=None, status_updates=None, coverage=None):
    results, selected, title_counts, fingerprints, conflicting_keys, exam_updates = [], [], {}, {}, set(), []
    for row in tree.walk():
        if row.tag != "tr" or not row.has_class("dataTr"):
            continue
        cells = [node for node in row.children if isinstance(node, _Node) and node.tag == "td"]
        if len(cells) < 5:
            if coverage is not None:
                coverage["exam_rows_unrecognized"] += 1
            warnings.append("有考试行结构与参考版本不同，已跳过，请在考试列表人工核对")
            continue
        if coverage is not None:
            coverage["exam_list_recognized"] = True
        title = cells[1].text() or "考试"
        explicit_courses = set()
        fields = {}
        for node in row.walk():
            if node.has_class("course-name") and node.text().strip():
                explicit_courses.add(" ".join(node.text().split()))
            for attribute in ("coursename", "data-coursename", "data-course-name"):
                if node.attrs.get(attribute, "").strip():
                    explicit_courses.add(" ".join(node.attrs[attribute].split()))
            fields.update(_node_params(node))
            for key in ("examid", "courseid", "classid", "clazzid"):
                for attribute in (key, "data-" + key):
                    value = node.attrs.get(attribute)
                    if value and re.fullmatch(r"[\w-]+", value):
                        fields[key] = value
        course_labels = " ".join(sorted(explicit_courses))
        if course_names is not None and not any(name in title or name in course_labels for name in course_names if name):
            continue
        if coverage is not None:
            coverage["exam_rows_in_scope"] += 1
        if len(explicit_courses) == 1:
            course_label = next(iter(explicit_courses))
        elif not explicit_courses and any(alias in title for alias in ("大物", "大学物理")):
            # Preserve the label used by existing physics reminders when the
            # global exam list provides only the legacy physics title signal.
            course_label = "大学物理"
        elif not explicit_courses and fields.get("courseid"):
            course_label = "课程 ID " + fields["courseid"]
        else:
            course_label = ""
        title_identity = " ".join(title.split())
        identity = (course_label, title_identity)
        title_counts[identity] = title_counts.get(identity, 0) + 1
        selected.append((row, cells, title, identity, fields))

    for row, cells, title, identity, fields in selected:
        exam_id = fields.get("examid") or fields.get("examrelationid")
        course_id = fields.get("courseid")
        if not exam_id:
            if unidentified_rows is not None and len(unidentified_rows) < 10:
                unidentified_rows.append(_exam_row_structure(row))
        # Published table's time column is a range, not the per-attempt timer.
        times = DATE_RE.findall(cells[2].text())
        start, due = (_datetime(times[0]), _datetime(times[1])) if len(times) == 2 else (None, None)
        if not due:
            due, start = _dates(row)
        if start and due and start > due:
            start = None
        status = " ".join(cell.text() for cell in cells[4:7])
        if exam_id:
            key = "course:" + course_id + ":exam:" + exam_id if course_id else "global-exam:" + exam_id
        else:
            if not identity[0]:
                if coverage is not None:
                    coverage["exam_rows_incomplete"] += 1
                warnings.append("考试「" + title[:60] + "」没有平台考试 ID，且未能确认唯一课程身份，需本人确认，未自动导入")
                continue
            if not start and title_counts[identity] > 1:
                if coverage is not None:
                    coverage["exam_rows_incomplete"] += 1
                warnings.append("考试「" + title[:60] + "」没有平台 ID，且列表内同名、缺少开考时间，需本人确认，未自动合并")
                continue
            # A list fingerprint is explicitly distinct from a platform ID. Do
            # not include the deadline: an extension must update the same item.
            payload = json.dumps([*identity, start or ""], ensure_ascii=False, separators=(",", ":"))
            key = "exam-list-fingerprint:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if due and key in fingerprints and fingerprints[key] != due:
                if coverage is not None:
                    coverage["exam_rows_incomplete"] += 1
                conflicting_keys.add(key)
                warnings.append("考试「" + title[:60] + "」在列表中出现相互冲突的截止时间，需本人确认，未自动覆盖")
                continue
            if due:
                fingerprints[key] = due
        answer_status = cells[5].text() if len(cells) > 5 else status
        completion = _completion_status(answer_status) or _completion_status(status)
        if coverage is not None and (completion is None or (not due and completion != "completed")):
            coverage["exam_rows_incomplete"] += 1
        if completion:
            status_evidence = answer_status if _completion_status(answer_status) else status
            exam_updates.append({"source": "chaoxing", "source_key": key, "status": completion,
                                 "status_evidence": " ".join(status_evidence.split())[:160]})
        if not due:
            warnings.append("考试「" + title[:60] + "」没有可核实年份的明确截止时间，需手动核对")
            continue
        candidate = _candidate(title, identity[0] or "考试", "exam", due, start, key, EXAMS, status, cells[2].text())
        if completion:
            candidate["status"] = completion
        if not exam_id:
            candidate["identity_kind"] = "list_fingerprint"
            candidate["notes"] += "\n身份依据：列表指纹（不是平台考试 ID）；由固定课程标签、完整考试标题" + (
                "和可核实的开考时间生成，截止时间变化不会改变此指纹。" if start else "生成；列表内该名称唯一，未提供开考时间，首次使用请核对。")
        results.append(candidate)
    statuses = {}
    for update in exam_updates:
        statuses.setdefault(update["source_key"], set()).add(update["status"])
    status_conflicts = {key for key, values in statuses.items() if len(values) > 1}
    if status_conflicts:
        conflicting_keys.update(status_conflicts)
        if coverage is not None:
            coverage["exam_rows_incomplete"] += len(status_conflicts)
        warnings.append("部分考试的列表状态互相冲突，未自动导入或更新完成状态，请本人核对")
    if status_updates is not None:
        status_updates.extend(item for item in exam_updates if item["source_key"] not in conflicting_keys)
    return [item for item in results if item["source_key"] not in conflicting_keys]


def collect_chaoxing(context, homework_courses=HOMEWORK_COURSES, exam_courses=EXAM_COURSES, homework_year=None):
    """Return candidates and explicit limitations, using only authenticated GETs."""
    homework_courses = tuple(name.strip() for name in homework_courses if isinstance(name, str) and name.strip())
    exam_courses = tuple(name.strip() for name in exam_courses if isinstance(name, str) and name.strip())
    candidates, warnings = [], []
    unidentified_exam_rows = []
    work_time_evidence = []
    work_status_evidence, status_updates = [], []
    year_binding = None
    reader = _Reader(context)
    coverage = {"courses_found": 0, "courses_in_scope": 0, "courses_out_of_scope": 0, "courses_attempted": 0,
                "work_lists_read": 0, "work_lists_empty": 0, "work_lists_unrecognized": 0,
                "work_details_read": 0, "work_items_incomplete": 0, "courses_completed": 0,
                "courses_without_work_module": 0, "exam_lists_read": 0, "exam_list_recognized": False,
                "exam_rows_in_scope": 0, "exam_rows_unrecognized": 0, "exam_rows_incomplete": 0,
                "homework_scope_missing": list(homework_courses),
                "homework_disabled": not homework_courses, "exams_disabled": not exam_courses,
                "disabled": not homework_courses and not exam_courses, "complete": False}
    auth = {"authenticated": False, "status": "unverified", "message": "尚未检查", "signals": []}
    if coverage["disabled"]:
        coverage["complete"] = True
        auth.update(status="disabled", message="未设置学习通课程，已跳过采集")
        return {"candidates": [], "status_updates": [], "warnings": [], "auth": auth,
                "diagnostics": {**reader.diagnostics(), "coverage": coverage, "homework_year_binding": None}}
    try:
        auth, base_source, base_tree = _probe(reader)
        if not auth["authenticated"]:
            return {"candidates": [], "status_updates": [], "warnings": [auth["message"]], "auth": auth,
                    "diagnostics": {**reader.diagnostics(), "coverage": coverage, "unidentified_exam_rows": unidentified_exam_rows,
                                    "work_time_evidence": work_time_evidence, "work_status_evidence": work_status_evidence,
                                    "homework_year_binding": year_binding}}
        if exam_courses:
            # Read global exams before course traversal so a long homework list cannot starve them.
            try:
                _, exam_tree = reader.get(EXAMS)
                coverage["exam_lists_read"] += 1
                # A successful HTTP response alone cannot establish that an empty
                # list is really empty: an error/layout-change page can also be 200.
                coverage["exam_list_recognized"] = bool(re.search(
                    r"暂无(?:相关)?考试|没有(?:相关)?考试|暂无(?:相关)?数据|没有(?:相关)?数据|无考试记录", exam_tree.text()))
                candidates.extend(_exam_rows(exam_tree, warnings, exam_courses, unidentified_exam_rows, status_updates, coverage))
                if not coverage["exam_list_recognized"]:
                    warnings.append("考试列表结构未识别，不能确认考试是否为空，请人工核对")
            except _LoginRequired:
                raise
            except _ReadError as exc:
                warnings.append(str(exc))
        fid = _number(base_source, "fid") or _inputs(base_tree).get("fid", "")
        if not homework_courses:
            pass
        elif not re.fullmatch(r"\d+", fid or ""):
            warnings.append("个人空间未提供学校 fid，无法可靠读取我学的课；未使用其他学校的默认值")
        else:
            _, course_tree = reader.get(COURSES, {"courseType": 1, "courseFid": fid, "courseTeacherid": 0, "courseOrderBy": 0})
            courses = _courses(course_tree)
            coverage["courses_found"] = len(courses)
            if not courses:
                warnings.append("课程列表未识别到真实课程 ID；可能尚未加载、没有课程或平台页面已变化，这不表示没有作业")
            courses = [course for course in courses if any(name in course["name"] for name in homework_courses if name)]
            coverage["courses_in_scope"] = len(courses)
            coverage["homework_scope_missing"] = [keyword for keyword in homework_courses
                if not any(keyword in course["name"] for course in courses)]
            coverage["courses_out_of_scope"] = coverage["courses_found"] - len(courses)
            if coverage["homework_scope_missing"] and coverage["courses_found"]:
                warnings.append("课程列表中未发现指定的学习通作业课程（" + "、".join(coverage["homework_scope_missing"]) + "），请核对课程名配置")
            confirmed_year = None
            if homework_year is not None:
                if len(courses) == 1:
                    only_course = courses[0]
                    config = homework_year if isinstance(homework_year, dict) else {
                        "year": homework_year, "courseid": only_course["courseid"], "clazzid": only_course["clazzid"]}
                    year = config.get("year")
                    if (isinstance(year, int) and not isinstance(year, bool) and 2000 <= year < 2100
                            and config.get("courseid") == only_course["courseid"] and config.get("clazzid") == only_course["clazzid"]):
                        confirmed_year = year
                        year_binding = {"year": year, "courseid": only_course["courseid"], "clazzid": only_course["clazzid"]}
                    else:
                        warnings.append("用户确认年份的课程绑定或年份值与当前课程不符，未补年份")
                else:
                    warnings.append("用户确认的作业年份仅能绑定唯一课程；当前未能唯一匹配，未补年份")
            for course in courses:
                if time.monotonic() >= reader.deadline - 1 or reader.count >= 100:
                    warnings.append("本次采集达到时间或请求数上限，结果可能不完整，请稍后重试")
                    break
                coverage["courses_attempted"] += 1
                prefix = course["name"][:60] + "："
                try:
                    params = {key: course[key] for key in ("courseid", "clazzid", "cpi")}
                    _, middle = reader.get(MIDDLE, {**params, "ismooc2": 1, "v": 2})
                    hidden = _inputs(middle)
                    if not hidden.get("workenc"):
                        coverage["courses_without_work_module"] += 1
                        warnings.append(prefix + "未提供作业列表访问字段，可能没有作业模块或平台版本不同")
                        continue
                    _, works = reader.get(WORKS, {"courseId": course["courseid"], "classId": course["clazzid"],
                        "cpi": course["cpi"], "ut": "s", "enc": hidden["workenc"], "openc": hidden.get("openc", ""),
                        "t": int(time.time() * 1000), "stuenc": hidden.get("enc", ""), "isdisplaytable": 2})
                    rows = _work_rows(works, confirmed_year)
                    coverage["work_lists_read"] += 1
                    if not rows:
                        if re.search(r"暂无(?:作业|数据)|没有作业|0/0", works.text()):
                            coverage["work_lists_empty"] += 1
                        else:
                            coverage["work_lists_unrecognized"] += 1
                            warnings.append(prefix + "作业列表结构未识别，不能确认作业是否为空，请人工核对")
                    pending_rows = []
                    # Collect every list status before any detail request: a
                    # later timeout must not hide submitted-work updates.
                    for row in rows:
                        url = urljoin(WORKS, row["url"])
                        ids = _params(url)
                        work_id = ids.get("workid")
                        if not work_id or not re.fullmatch(r"\d+", work_id):
                            coverage["work_items_incomplete"] += 1
                            warnings.append(prefix + "有作业缺少真实 ID，已跳过")
                            continue
                        if ids.get("courseid") != course["courseid"] or ids.get("classid") != course["clazzid"]:
                            coverage["work_items_incomplete"] += 1
                            warnings.append(prefix + "详情课程 ID 与列表不一致，已跳过")
                            continue
                        key = "course:" + course["courseid"] + ":class:" + course["clazzid"] + ":work:" + work_id
                        completion = _completion_status(row["status"])
                        if completion is None:
                            coverage["work_items_incomplete"] += 1
                        evidence = " ".join(row["status"].split())[:160]
                        if len(work_status_evidence) < 80:
                            work_status_evidence.append({"title": row["title"][:120], "status_evidence": evidence,
                                                         "recognized_status": completion or "unknown"})
                        if completion:
                            status_updates.append({"source": "chaoxing", "source_key": key,
                                                   "status": completion, "status_evidence": evidence})
                        if completion == "completed":
                            continue
                        if not row["title"] or not _safe_url(url) or urlparse(url).path not in WORK_TASK_PATHS:
                            coverage["work_items_incomplete"] += 1
                            warnings.append(prefix + "有作业缺少只读详情链接，已跳过日期读取")
                            continue
                        pending_rows.append((row, url, key))
                    for row, url, key in pending_rows[:80]:
                        # Prefer explicit full-year metadata already on the
                        # list. Open the homework detail only when necessary.
                        due, start = row["due_at"], row["start_at"]
                        confirmed_text = row["confirmed_year_text"]
                        if not due:
                            detail_source, detail = reader.get(url)
                            coverage["work_details_read"] += 1
                            if len(work_time_evidence) < 20:
                                work_time_evidence.append({"title": row["title"][:120], **_work_time_evidence(detail_source, detail)})
                            due, start = _dates(detail)
                            if not due and confirmed_year is not None:
                                due, start, confirmed_text = _confirmed_work_dates(detail, confirmed_year)
                        if not due:
                            coverage["work_items_incomplete"] += 1
                            warnings.append(prefix + row["title"][:50] + "的截止时间缺少年份、未知或存在冲突；需手动核对，未猜测年份")
                            continue
                        # Link back to the course; do not persist expiring enc/signature values.
                        public_link = MIDDLE + "?" + urlencode(params)
                        candidate = _candidate(row["title"], course["name"], "assignment", due, start, key,
                                               public_link, row["status"], "开始 " + (start or "未知") + "；截止 " + due)
                        if confirmed_text:
                            candidate["notes"] += ("\n年份依据：用户确认年份 " + str(confirmed_year) + "；仅绑定本次课程 ID "
                                                   + course["courseid"] + "、班级 ID " + course["clazzid"] + "；平台原文：" + confirmed_text)
                        candidates.append(candidate)
                    if len(pending_rows) > 80:
                        coverage["work_items_incomplete"] += len(pending_rows) - 80
                        warnings.append(prefix + "未完成作业超过 80 项，本次仅截断日期详情读取，所有已识别列表状态均已检查")
                    coverage["courses_completed"] += 1
                except _LoginRequired:
                    raise
                except _ReadError as exc:
                    warnings.append(prefix + str(exc))
                    if time.monotonic() >= reader.deadline - 1 or reader.count >= 100:
                        break
    except _LoginRequired as exc:
        auth = {"authenticated": False, "status": "not_authenticated", "message": str(exc), "signals": []}
        warnings.append(str(exc))
    except Exception as exc:
        warnings.append(str(exc) if isinstance(exc, _ReadError) else "平台返回了未支持的页面结构，本次采集未完成，请人工核对")
    if coverage["courses_found"]:
        warnings.append("学习通指定作业课程覆盖：匹配 " + str(coverage["courses_in_scope"]) + " 门，已尝试 "
                        + str(coverage["courses_attempted"]) + " 门，读到作业列表 "
                        + str(coverage["work_lists_read"]) + " 门（其中明确无作业 "
                        + str(coverage["work_lists_empty"]) + " 门、结构未识别 "
                        + str(coverage["work_lists_unrecognized"]) + " 门），作业详情 "
                        + str(coverage["work_details_read"]) + " 项。其他课程按你的范围配置跳过。")
    if exam_courses:
        warnings.append("学习通仅采集指定课程的作业与考试；考试当前读取全局列表，课程内独立考试可能遗漏，候选仍需本人核对。")
    unique_updates, conflicting_updates = {}, set()
    for update in status_updates:
        key = update["source_key"]
        if key in unique_updates and unique_updates[key]["status"] != update["status"]:
            conflicting_updates.add(key)
        unique_updates[key] = update
    if conflicting_updates:
        warnings.append("部分作业或考试的列表状态互相冲突，未自动更新完成状态，请本人核对")
    unique = {item["source_key"]: item for item in candidates if item["source_key"] not in conflicting_updates}
    # Only a complete metadata/status read may satisfy the daily schedule.
    # Confirmed submissions need no deadline to cancel an existing reminder.
    homework_complete = not homework_courses or (coverage["courses_in_scope"] > 0 and not coverage["homework_scope_missing"]
        and coverage["courses_completed"] == coverage["courses_in_scope"]
        and coverage["work_lists_read"] == coverage["courses_in_scope"]
        and coverage["work_lists_unrecognized"] == 0 and coverage["work_items_incomplete"] == 0)
    exams_complete = not exam_courses or (coverage["exam_lists_read"] == 1 and coverage["exam_list_recognized"]
        and coverage["exam_rows_unrecognized"] == 0 and coverage["exam_rows_incomplete"] == 0)
    coverage["complete"] = bool(auth["authenticated"] and homework_complete and exams_complete and not conflicting_updates)
    return {"candidates": list(unique.values()), "status_updates": [item for key, item in unique_updates.items() if key not in conflicting_updates],
            "warnings": list(dict.fromkeys(warnings)), "auth": auth,
            "diagnostics": {**reader.diagnostics(), "coverage": coverage, "unidentified_exam_rows": unidentified_exam_rows,
                            "work_time_evidence": work_time_evidence, "work_status_evidence": work_status_evidence,
                            "homework_year_binding": year_binding}}
