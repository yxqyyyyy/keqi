"""Offline synthetic fixtures; no captured account data or authentication."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin, urlparse

from studyreminder.xiji_adapter import (
    HOME, MAIN, ORIGIN, _CAMPUS_LOGIN, _Reader, _Tree, _assignment_detail, _assignments, _campus_ticket_url, _course_contexts, _current_course, _safe_url, _submission_evidence, _submission_summary,
    collect_xiji, probe_xiji_auth,
)


NAV = '<a id="userDropdown">本人</a><a href="/includes/redirect.jsp?tab=-1">课程信息</a><a href="/includes/redirect.jsp?tab=-2">在线作业</a><a href="/includes/redirect.jsp?tab=-3">在线考试</a>'
ROW = '<table><tr><th>作业名称</th><th>截止时间</th><th>开始时间</th><th>状态</th><th>操作</th></tr><tr><td>存储器作业</td><td>2026-10-20 23:59</td><td>2026-10-16 08:00</td><td>未提交</td><td><a href="assignment/index.jsp?assignID=5712&amp;token=PRIVATE">进入作业</a></td></tr></table>'


class Response:
    def __init__(self, body="", status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    def text(self):
        return self.body


class Request:
    def __init__(self, main=None, listing=None, home=None):
        self.calls = []
        self.main = main or Response(NAV + '<h2>计算机组成与体系结构</h2>')
        self.listing = listing or Response(ROW)
        self.home = home or Response("")

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        path = urlparse(url).path
        if path == "/":
            return self.home
        if path == "/main.jsp":
            return self.main
        if path == "/includes/redirect.jsp":
            return self.listing
        raise AssertionError("Unexpected request path")


class Context:
    def __init__(self, **kwargs):
        self.request = Request(**kwargs)


def course_main(current):
    choices = ''.join('<span class="dropdown-item-course' + (' font-weight-bold' if value == current else '') + '" value="' + value + '">' + name + '</span>'
                      for value, name in (("9", "计算机组成与体系结构"), ("87", "离散数学")))
    form = '<form name="courseForm" method="post" action="/main.jsp"><input name="changeCourse" value=""><input name="doChangeCourse" value="true"></form>'
    script = '<script>$(document).on("click", ".dropdown-item-course", function(){ var id = $(this).attr("value"); document.courseForm.changeCourse.value = id; document.courseForm.submit(); });</script>'
    return NAV + choices + form + script


def course_detail(current, assign_id):
    menu = (("5712", "第二次作业"), ("5625", "第一次作业")) if current == "9" else (("71", "离散第一周作业"),)
    links = ''.join('<a class="list-group-item' + (' active' if value == assign_id else '') + '" href="index.jsp?courseID=' + current + '&amp;assignID=' + value + '">' + name + '</a>' for value, name in menu)
    title = dict(menu)[assign_id]
    times = '<p>作业时间：<b>2026-09-21 23:24:00</b> 至 <b>2026-09-29 00:00:00</b></p>'
    late = '<p>现在是补交时段！作业补交时间截至：<b>2026-10-18 00:00:00</b></p>' if assign_id == "5712" else ''
    submitted = '<table><tr><th>#</th><th>题目</th><th>提交状态</th></tr><tr><th>1</th><td>题目中伪造作业时间：2027-01-01 00:00 至 2027-01-02 00:00</td><td>初次提交时间: 2026-09-28 08:26:35 最后一次修改时间: 2026-09-28 08:26:35</td></tr></table>'
    return course_main(current) + links + '<h4>' + title + '</h4>' + times + late + submitted


def numbered_submission_table(statuses):
    rows = ''.join('<tr><th>' + str(index) + '.</th><td><div>PRIVATE QUESTION CONTENT</div>'
                   '<span class="badge badge-' + ('success' if label in {'已提交', '待评阅', '待批阅', '已交'} else 'danger')
                   + '">' + label + '</span></td></tr>' for index, label in enumerate(statuses, 1))
    return '<table>' + rows + '</table>'


class CourseRequest:
    def __init__(self, fail_course=None):
        self.current, self.calls, self.posts = "9", [], []
        self.fail_course = fail_course

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parsed = urlparse(url)
        if parsed.path == "/main.jsp":
            return Response(course_main(self.current))
        if parsed.path == "/includes/redirect.jsp":
            return Response(status=302, headers={"location": "/assignment/index.jsp"})
        if parsed.path == "/assignment/index.jsp":
            assign_id = parse_qs(parsed.query).get("assignID", ["5712" if self.current == "9" else "71"])[0]
            return Response(course_detail(self.current, assign_id))
        raise AssertionError("Unobserved request route")

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        assert set(kwargs) == {"form", "max_redirects", "timeout"}
        assert set(kwargs["form"]) == {"changeCourse", "doChangeCourse"}
        assert kwargs["form"]["doChangeCourse"] == "true"
        self.current = kwargs["form"]["changeCourse"]
        if self.current == self.fail_course:
            raise RuntimeError("network failure after state change ?token=PRIVATE")
        return Response(course_main(self.current))


class CampusRequest:
    """Synthetic session handoff; no password or real account material."""
    ticket = HOME + "?ticket=ST-123-SYNTHETIC.private-cas01"

    def __init__(self, *, authenticated=False, cookies=True, cas=None, service=None,
                 listing=None, blank_main=False, final_main=None):
        self.authenticated, self.has_cookies = authenticated, cookies
        self.cas = cas or Response(status=302, headers={"location": self.ticket})
        self.service = service or Response("service response")
        self.listing = listing or Response(ROW)
        self.blank_main, self.final_main = blank_main, final_main
        self.calls, self.storage_reads = [], 0

    def storage_state(self):
        self.storage_reads += 1
        return {"cookies": [{"domain": ".cas.whu.edu.cn", "name": "synthetic"}] if self.has_cookies else []}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == _CAMPUS_LOGIN:
            if isinstance(self.cas, Exception):
                raise self.cas
            return self.cas
        if url == self.ticket:
            self.authenticated = True
            return self.service
        if url == MAIN:
            if self.authenticated:
                return self.final_main or Response(NAV + '<h2>计算机组成与体系结构</h2>')
            return Response("") if self.blank_main else Response('<input type="password">')
        if url == HOME:
            return Response(NAV) if self.authenticated else Response(status=302, headers={"location": "/indexcs/simple.jsp?loginErr=0"})
        if urlparse(url).path == "/includes/redirect.jsp":
            return self.listing
        raise AssertionError("Unapproved SSO request")


class XijiTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch("studyreminder.xiji_adapter.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_authenticated_nav_and_control_required(self):
        self.assertTrue(probe_xiji_auth(Context())["authenticated"])
        for body in (NAV.replace('id="userDropdown"', ''), '<a id="userDropdown">本人</a>'):
            self.assertFalse(probe_xiji_auth(Context(main=Response(body)))["authenticated"])

    def test_login_page_is_not_authenticated(self):
        context = Context(main=Response('<input type="password">'))
        result = collect_xiji(context)
        self.assertFalse(result["auth"]["authenticated"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(context.request.calls), 1)

    def test_empty_main_is_unavailable_instead_of_logged_out(self):
        for body in ("", " \n\t"):
            result = probe_xiji_auth(Context(main=Response(body)))
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["authenticated"])
            self.assertIn("空白", result["message"])
            self.assertNotIn("重新登录", result["message"])

    def test_empty_main_checks_home_once_for_observed_login_redirect(self):
        context = Context(main=Response(""), home=Response(status=302, headers={"location": "/indexcs/simple.jsp?loginErr=0"}))
        result = probe_xiji_auth(context)
        self.assertEqual(result["status"], "not_authenticated")
        self.assertIn("本程序保存", result["message"])
        self.assertEqual([urlparse(url).path for url, _ in context.request.calls], ["/main.jsp", "/"])

    def test_empty_main_home_loop_is_bounded_and_not_treated_as_logout(self):
        context = Context(main=Response(""), home=Response(status=302, headers={"location": "/main.jsp"}))
        result = probe_xiji_auth(context)
        self.assertEqual(result["status"], "error")
        self.assertIn("空白", result["message"])
        self.assertEqual(len(context.request.calls), 3)

    def test_unknown_redirect_stops_without_fetching_or_leaking_query(self):
        context = Context(listing=Response(status=302, headers={"location": "https://cslabcg.whu.edu.cn/new/list.jsp?token=PRIVATE"}))
        result = collect_xiji(context)
        self.assertIn("cslabcg.whu.edu.cn/new/list.jsp", " ".join(result["warnings"]))
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(len(context.request.calls), 2)
        self.assertTrue(any(route.get("result") == "blocked_route" for route in result["diagnostics"]["routes"]))

    def test_unknown_login_named_redirect_is_read_error_instead_of_logout(self):
        for target in ("/loginSuccess.jsp?ticket=PRIVATE", "/signin-complete.jsp", "https://other.invalid/login"):
            with self.subTest(target=target):
                context = Context(main=Response(status=302, headers={"location": target}))
                result = probe_xiji_auth(context)
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["authenticated"])
                self.assertEqual([url for url, _ in context.request.calls], [MAIN])
                self.assertNotIn("PRIVATE", str(result))

    def test_unknown_ticket_path_is_redacted_without_request(self):
        for path in ("/ST-SYNTHETIC/portal.jsp", "/%53%54-SYNTHETIC/portal.jsp",
                     "/%2553%2554-SYNTHETIC/portal.jsp", "/portal;jsessionid=SYNTHETIC/path.jsp"):
            with self.subTest(path=path):
                context = Context(listing=Response(status=302, headers={"location": path + "?ticket=PRIVATE"}))
                result = collect_xiji(context)
                self.assertNotIn("SYNTHETIC", str(result))
                self.assertNotIn("PRIVATE", str(result))
                self.assertEqual(len(context.request.calls), 2)
                self.assertTrue(any(route.get("redirect", {}).get("path") == "/[redacted]"
                                    for route in result["diagnostics"]["routes"]))

    def test_assignment_metadata_allows_only_observed_numeric_navigation(self):
        self.assertTrue(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp"))
        self.assertTrue(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp?courseID=9&assignID=5712"))
        self.assertFalse(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp?assignID=5712&submit=true"))
        self.assertFalse(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp?token=PRIVATE"))

    def test_credential_and_external_routes_rejected(self):
        for url in ("https://user:pass@cslabcg.whu.edu.cn/main.jsp", "https://:@cslabcg.whu.edu.cn/main.jsp",
                    "https://cslabcg.whu.edu.cn:444/main.jsp", "https://cslabcg.whu.edu.cn/main.jsp;session=PRIVATE",
                    "https://cslabcg.whu.edu.cn.evil.invalid/main.jsp", "http://cslabcg.whu.edu.cn/main.jsp",
                    "https://cslabcg.whu.edu.cn/assignment/submit.jsp?assignID=5712", "https://cslabcg.whu.edu.cn/main.jsp?doSubmit=1"):
            self.assertFalse(_safe_url(url))

    def test_collect_metadata_never_enters_assignment_or_submits(self):
        context = Context()
        result = collect_xiji(context)
        self.assertEqual(len(result["candidates"]), 1)
        item = result["candidates"][0]
        self.assertEqual(item["title"], "存储器作业")
        self.assertEqual(item["due_at"], "2026-10-20T23:59:00+08:00")
        self.assertEqual(item["source_key"], "assignment:5712")
        self.assertNotIn("PRIVATE", str(result))
        self.assertTrue(all(options["max_redirects"] == 0 for _, options in context.request.calls))
        self.assertFalse(any("assignment/index.jsp" in url for url, _ in context.request.calls))
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])

    def test_missing_or_invalid_year_never_invents_deadline(self):
        for deadline in ("10-20 23:59", "2026-02-30 23:59", "剩余24小时"):
            warnings = []
            self.assertEqual(_assignments(_Tree(ROW.replace("2026-10-20 23:59", deadline)).root, "计算机组成", warnings), [])
            self.assertTrue(warnings)

    def test_unscoped_or_ambiguous_course_is_not_collected(self):
        for body in ('<h2>高等数学</h2>', '<h2>计算机组成</h2><h2>离散数学</h2>', '<a>计算机组成</a>'):
            self.assertEqual(_current_course(_Tree(body).root), "")
            result = collect_xiji(Context(main=Response(NAV + body)))
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["diagnostics"]["requests"], 1)

    def test_actual_course_link_is_discovered_but_not_visited(self):
        result = collect_xiji(Context(main=Response(NAV + '<h2>计算机组成</h2><a href="/switch.jsp?courseID=2&amp;token=PRIVATE">离散数学</a>')))
        self.assertTrue(any("切换链接" in warning for warning in result["warnings"]))
        self.assertNotIn("PRIVATE", str(result))

    def test_form_diagnostics_keep_only_course_values(self):
        form = '<form method="post" action="/choose.jsp?token=PRIVATE"><input name="person" value="PERSONAL"><input name="doChangeCourse" value="SECRET"><select name="changeCourse"><option value="31" selected>计算机组成</option><option value="32">离散数学</option><option value="33">其他课程</option></select></form>'
        result = collect_xiji(Context(main=Response(NAV + form)))
        control = result["diagnostics"]["course_controls"][0]
        self.assertEqual(control["action"], {"host": "cslabcg.whu.edu.cn", "path": "/choose.jsp"})
        self.assertEqual([item["value"] for item in control["course_options"]], ["31", "32"])
        for value in ("PRIVATE", "PERSONAL", "SECRET", "其他课程"):
            self.assertNotIn(value, str(result))

    def test_loaded_course_frame_identifies_current_course_without_navigation(self):
        class Frame:
            url = "https://cslabcg.whu.edu.cn/includes/kgraph/index.jsp"

            def title(self):
                return "计算机组成与体系结构-知识图谱"

        class Page:
            url = MAIN
            frames = [Frame()]

        context = Context(main=Response(NAV))
        context.pages = [Page()]
        result = collect_xiji(context)
        self.assertEqual(result["candidates"][0]["course"], "计算机组成与体系结构")
        self.assertEqual(result["diagnostics"]["coverage"]["course_name_source"], "loaded_frame_title")
        self.assertEqual(len(context.request.calls), 2)

    def test_course_contexts_redact_nonnumeric_onclick_arguments(self):
        tree = _Tree('<div><a id="choice" onclick="chooseCourse(31, \'PRIVATE\'); changeCourse.value=31;"><span>离散数学</span></a></div>').root
        contexts = _course_contexts(tree)
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["text"], "离散数学")
        self.assertNotIn("PRIVATE", str(contexts))
        self.assertEqual(contexts[0]["onclick"]["functions"], [{"name": "chooseCourse", "numeric_args": ["31"]}])

    def test_observed_dropdown_current_marker_and_numeric_course_attributes(self):
        tree = _Tree('<div data-user="PRIVATE"><span class="dropdown-item dropdown-item-course font-weight-bold" data-courseid="31" data-token="PRIVATE">计算机组成与体系结构</span><span class="dropdown-item dropdown-item-course" data-courseid="32">离散数学</span></div>').root
        self.assertEqual(_current_course(tree), "计算机组成与体系结构")
        contexts = _course_contexts(tree)
        self.assertEqual(contexts[0]["attributes"]["numeric_course_attributes"], {"data-courseid": "31"})
        self.assertIn("data-user", contexts[0]["parent"]["attribute_names"])
        self.assertIn("data-token", contexts[0]["attributes"]["attribute_names"])
        self.assertNotIn("PRIVATE", str(contexts))

    def test_request_budget(self):
        reader = _Reader(Context())
        reader.count = 30
        with self.assertRaisesRegex(Exception, "请求数上限"):
            reader.get(MAIN)
        self.assertEqual(reader.context.request.calls, [])

    def test_two_real_course_menus_and_course_form_restore(self):
        context = Context()
        context.request = CourseRequest()
        result = collect_xiji(context)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertTrue(result["diagnostics"]["coverage"]["initial_course_restored"])
        self.assertEqual(context.request.current, "9")
        self.assertEqual([options["form"]["changeCourse"] for _, options in context.request.posts], ["87", "9"])
        self.assertTrue(all(url == MAIN for url, _ in context.request.posts))
        self.assertTrue(all(item["status"] == "completed" for item in result["candidates"]))
        item = next(item for item in result["candidates"] if "5712" in item["source_key"])
        self.assertEqual(item["title"], "第二次作业（补交截止）")
        self.assertEqual(item["due_at"], "2026-10-18T00:00:00+08:00")
        self.assertIn("2026-09-29", item["notes"])
        self.assertIn("扣分", item["notes"])
        self.assertEqual(item["source_key"], "course:9:assignment:5712")
        self.assertEqual(result["warnings"], [])

    def test_authenticated_session_without_selected_course_reads_configured_courses(self):
        class UnselectedRequest(CourseRequest):
            def __init__(self):
                super().__init__()
                self.current = None

            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                if url == MAIN and self.current is None:
                    # The live responsive menu repeats each ID/name option.
                    response.body += course_main(None)
                return response

        context = Context()
        context.request = UnselectedRequest()
        result = collect_xiji(context)
        coverage = result["diagnostics"]["coverage"]
        self.assertTrue(result["auth"]["authenticated"])
        self.assertTrue(coverage["complete"])
        self.assertFalse(coverage["initial_course_identified"])
        self.assertTrue(coverage["current_course_identified"])
        self.assertEqual(coverage["course_name_source"], "verified_course_selection")
        self.assertIsNone(coverage["initial_course_restored"])
        self.assertEqual(coverage["initial_course_restore_reason"], "initial_course_not_identified")
        self.assertEqual([options["form"]["changeCourse"] for _, options in context.request.posts], ["9", "87"])
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["warnings"], [])

    def test_unselected_session_starts_with_only_configured_course(self):
        context = Context()
        context.request = CourseRequest()
        context.request.current = None
        result = collect_xiji(context, course_scope=["离散数学"])
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertEqual([options["form"]["changeCourse"] for _, options in context.request.posts], ["87"])
        self.assertEqual({item["course"] for item in result["candidates"]}, {"离散数学"})

    def test_unselected_session_requires_verified_form_and_binding(self):
        valid = course_main(None)
        for body in (valid.replace('value="true"', 'value="false"'),
                     valid.replace("document.courseForm.submit();", "otherForm.submit();"),
                     NAV):
            with self.subTest(body=body):
                context = Context(main=Response(body))
                result = collect_xiji(context)
                self.assertTrue(result["auth"]["authenticated"])
                self.assertFalse(result["diagnostics"]["coverage"]["complete"])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(len(context.request.calls), 1)

    def test_conflicting_course_id_name_or_current_markers_never_switch(self):
        options = ('<span class="dropdown-item-course" value="9">离散数学</span>',
                   '<span class="dropdown-item-course" value="99">离散数学</span>',
                   '<span class="dropdown-item-course font-weight-bold" value="9">计算机组成与体系结构</span>'
                   '<span class="dropdown-item-course font-weight-bold" value="87">离散数学</span>',
                   '<h2>计算机组成与体系结构</h2><h2>离散数学</h2>')
        for addition in options:
            context = Context(main=Response(course_main(None) + addition))
            result = collect_xiji(context)
            self.assertEqual(result["candidates"], [])
            self.assertEqual(len(context.request.calls), 1)
            self.assertFalse(result["diagnostics"]["coverage"]["complete"])
            self.assertTrue(any("冲突" in warning or "多个当前课程" in warning for warning in result["warnings"]))

    def test_unselected_session_requires_returned_course_confirmation(self):
        class UnconfirmedRequest(CourseRequest):
            def post(self, url, **kwargs):
                super().post(url, **kwargs)
                return Response(course_main(None))

        context = Context()
        context.request = UnconfirmedRequest()
        context.request.current = None
        result = collect_xiji(context)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["diagnostics"]["coverage"]["course_lists_read"], 0)
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])
        self.assertTrue(any("切换结果未确认" in warning for warning in result["warnings"]))

    def test_course_options_do_not_replace_authentication_verification(self):
        context = Context(main=Response(course_main(None).replace(NAV, '<a id="userDropdown">本人</a>')))
        result = collect_xiji(context)
        self.assertEqual(result["auth"]["status"], "unverified")
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(context.request.calls), 1)

    def test_empty_scope_disables_platform_without_login_or_requests(self):
        context = Context(main=Response('<input type="password">'))
        result = collect_xiji(context, course_scope=[])
        self.assertEqual(context.request.calls, [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["auth"]["status"], "disabled")
        self.assertFalse(result["auth"]["authenticated"])
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])

    def test_single_course_scope_can_start_outside_scope_and_restores_it(self):
        context = Context()
        context.request = CourseRequest()
        result = collect_xiji(context, course_scope=["离散数学"])
        self.assertEqual([item["course"] for item in result["candidates"]], ["离散数学"])
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertTrue(result["diagnostics"]["coverage"]["initial_course_restored"])
        self.assertEqual([options["form"]["changeCourse"] for _, options in context.request.posts], ["87", "9"])
        self.assertEqual(context.request.current, "9")

    def test_configurable_course_keyword_does_not_mutate_defaults(self):
        class CustomRequest(CourseRequest):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                response.body = response.body.replace("计算机组成与体系结构", "编译原理")
                return response

        context = Context()
        context.request = CustomRequest()
        result = collect_xiji(context, course_scope=["编译原理"])
        self.assertEqual({item["course"] for item in result["candidates"]}, {"编译原理"})
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertEqual(context.request.posts, [])
        # A following default call still reads the original two courses.
        normal = Context()
        normal.request = CourseRequest()
        self.assertEqual(len(collect_xiji(normal)["candidates"]), 3)

    def test_matching_one_of_several_configured_keywords_is_incomplete(self):
        context = Context()
        context.request = CourseRequest()
        result = collect_xiji(context, course_scope=["离散数学", "编译原理"])
        self.assertEqual([item["course"] for item in result["candidates"]], ["离散数学"])
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])
        self.assertEqual(context.request.current, "9")

    def test_course_restore_after_network_failure_and_no_secret_leak(self):
        context = Context()
        context.request = CourseRequest(fail_course="87")
        result = collect_xiji(context)
        self.assertEqual(context.request.current, "9")
        self.assertTrue(result["diagnostics"]["coverage"]["initial_course_restored"])
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertTrue(any("导航请求失败" in warning for warning in result["warnings"]))

    def test_changed_course_form_does_not_post(self):
        context = Context(main=Response(course_main("9").replace('value="true"', 'value="false"')))
        reader = _Reader(context)
        with self.assertRaisesRegex(Exception, "表单与已验证结构不一致"):
            reader.select_course("87", _Tree(context.request.main.text()).root)
        self.assertFalse(reader.navigation_attempted)
        self.assertEqual(context.request.calls, [])

    def test_partial_submission_stays_pending_and_yearless_late_is_not_guessed(self):
        entry = {"id": "5712", "title": "第二次作业", "url": "https://cslabcg.whu.edu.cn/assignment/index.jsp?courseID=9&assignID=5712"}
        source = course_detail("9", "5712")
        warnings = []
        pending = source.replace("初次提交时间: 2026-09-28 08:26:35 最后一次修改时间: 2026-09-28 08:26:35", "未提交")
        candidate = _assignment_detail(_Tree(pending).root, entry, "计算机组成", "9", warnings)
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(warnings, [])
        source = source.replace("2026-10-18 00:00:00", "10-18 00:00:00")
        self.assertIsNone(_assignment_detail(_Tree(source).root, entry, "计算机组成", "9", warnings))
        self.assertTrue(any("补交截止时间" in warning for warning in warnings))

    def test_submission_diagnostics_identify_numbered_rows_without_storing_contents(self):
        rows = ''.join('<tr><th>' + str(index) + '.</th><td>PRIVATE QUESTION</td><td>'
                       + ('初次提交时间：2026-10-23 10:00:00 最后一次修改时间：2026-10-23 10:01:00' if index < 5 else '未提交')
                       + '</td></tr>' for index in range(1, 6))
        evidence = _submission_evidence(_Tree('<table>' + rows + '</table>').root)
        self.assertEqual(evidence['recognized_summary'], {})
        self.assertEqual(evidence['tables'][0]['row_count'], 5)
        self.assertEqual(evidence['tables'][0]['rows'][0]['cell_count'], 3)
        self.assertEqual(evidence['tables'][0]['rows'][0]['cell_tags'], ['th', 'td', 'td'])
        self.assertEqual(evidence['tables'][0]['rows'][0]['metadata_cells'][0]['numbered_label'], 1)
        self.assertEqual(evidence['tables'][0]['rows'][0]['metadata_cells'][1]['initial_submitted_at'], ['2026-10-23T10:00:00+08:00'])
        self.assertEqual(evidence['tables'][0]['rows'][4]['metadata_cells'][1]['status_labels'], ['未提交'])
        self.assertNotIn('PRIVATE QUESTION', str(evidence))

    def test_submission_diagnostics_ignore_answer_fields_and_show_explicit_hidden_rows(self):
        html = ('<table><tr hidden><th>1.</th><td>'
                '<textarea>PRIVATE ANSWER 初次提交时间：2027-01-01 12:00:00 已提交</textarea>'
                '<input type="hidden" value="PRIVATE TOKEN">'
                '<a href="https://invalid/private">PRIVATE ATTACHMENT 已提交</a>'
                '<script>PRIVATE SCRIPT</script><code>PRIVATE CODE 未提交</code>'
                '<span>待评阅</span></td></tr></table>')
        evidence = _submission_evidence(_Tree(html).root)
        row = evidence['tables'][0]['rows'][0]
        self.assertTrue(row['explicitly_hidden'])
        self.assertEqual(row['metadata_cells'][1]['status_labels'], ['待评阅'])
        self.assertNotIn('initial_submitted_at', str(evidence))
        self.assertNotIn('2027', str(evidence))
        self.assertNotIn('PRIVATE', str(evidence))
        self.assertNotIn('invalid', str(evidence))

    def test_collection_diagnostics_record_each_assignment_under_stable_key(self):
        context = Context()
        context.request = CourseRequest()
        result = collect_xiji(context)
        evidence = result['diagnostics']['assignment_submission_evidence']
        self.assertEqual(set(evidence), {'course:9:assignment:5712', 'course:9:assignment:5625', 'course:87:assignment:71'})
        self.assertTrue(all(item['recognized_summary'] == {'submitted': 1} for item in evidence.values()))
        self.assertNotIn('题目中伪造', str(evidence))

    def test_submission_diagnostics_show_only_exact_status_node_shapes(self):
        body = ('<table><tr><th>1.</th><td>PRIVATE QUESTION 已提交 SUBSTRING'
                '<p><span class="badge badge-success PRIVATE-CLASS">已提交</span></p>'
                '<textarea><span>未提交</span></textarea><a href="/private"><span>未提交</span></a>'
                '</td></tr></table>')
        evidence = _submission_evidence(_Tree(body).root)
        marker = evidence['tables'][0]['rows'][0]['metadata_cells'][1]['exact_status_nodes']
        self.assertEqual(marker, [
            {'tag': 'p', 'status_label': '已提交', 'classes': [], 'class_count': 0, 'unrecognized_class_count': 0,
             'element_child_count': 1, 'element_child_tags': ['span'], 'direct_cell_child': True,
             'parent_tag': 'td', 'submission_label_adjacent': False},
            {'tag': 'span', 'status_label': '已提交', 'classes': ['badge', 'badge-success'], 'class_count': 3, 'unrecognized_class_count': 1,
             'element_child_count': 0, 'element_child_tags': [], 'direct_cell_child': False,
             'parent_tag': 'p', 'submission_label_adjacent': False},
        ])
        self.assertNotIn('PRIVATE', str(evidence))
        self.assertNotIn('SUBSTRING', str(evidence))

    def test_unknown_submission_status_keeps_deadline_but_not_complete_coverage(self):
        class UnknownStatusRequest(CourseRequest):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                response.body = response.body.replace('初次提交时间: 2026-09-28 08:26:35 最后一次修改时间: 2026-09-28 08:26:35', '未知状态')
                return response

        context = Context()
        context.request = UnknownStatusRequest()
        result = collect_xiji(context)
        self.assertTrue(result['auth']['authenticated'])
        self.assertEqual(result['auth']['status'], 'authenticated')
        self.assertEqual(len(result['candidates']), 3)
        self.assertTrue(all(item['status'] == 'pending' and item['due_at'] for item in result['candidates']))
        coverage = result['diagnostics']['coverage']
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['assignment_statuses_verified'], 0)
        self.assertEqual(coverage['assignment_statuses_unverified'], 3)
        self.assertTrue(any('提交状态尚未确认' in warning for warning in result['warnings']))

    def test_explicit_pending_submission_is_complete_coverage(self):
        class PendingStatusRequest(CourseRequest):
            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                response.body = response.body.replace('初次提交时间: 2026-09-28 08:26:35 最后一次修改时间: 2026-09-28 08:26:35', '未提交')
                return response

        context = Context()
        context.request = PendingStatusRequest()
        result = collect_xiji(context)
        self.assertTrue(result['auth']['authenticated'])
        self.assertTrue(result['diagnostics']['coverage']['complete'])
        self.assertEqual(result['diagnostics']['coverage']['assignment_statuses_verified'], 3)
        self.assertEqual(result['diagnostics']['coverage']['assignment_statuses_unverified'], 0)
        self.assertTrue(all(item['status'] == 'pending' for item in result['candidates']))

    def test_numbered_question_rows_require_all_five_personal_badges_submitted(self):
        submitted = numbered_submission_table(['已提交'] * 5)
        self.assertEqual(_submission_summary(_Tree(submitted).root), {'submitted': 5})
        entry = {'id': '6091', 'title': '第三章作业', 'url': ORIGIN + '/assignment/index.jsp?courseID=9&assignID=6091'}
        dates = '<p>作业时间：2026-10-20 08:00:00 至 2026-10-24 00:00:00</p>'
        candidate = _assignment_detail(_Tree(dates + submitted).root, entry, '计算机组成', '9', [])
        self.assertEqual(candidate['status'], 'completed')
        pending = numbered_submission_table(['已提交'] * 4 + ['未提交'])
        self.assertEqual(_submission_summary(_Tree(pending).root), {'submitted': 4, 'pending': 1})
        candidate = _assignment_detail(_Tree(dates + pending).root, entry, '计算机组成', '9', [])
        self.assertEqual(candidate['status'], 'pending')

    def test_numbered_question_rows_recognize_explicit_awaiting_review_badges(self):
        table = numbered_submission_table(['待评阅', '待批阅', '已交'])
        self.assertEqual(_submission_summary(_Tree(table).root), {'submitted': 3})

    def test_numbered_question_rows_reject_ambiguous_or_mismatched_badges(self):
        valid = numbered_submission_table(['已提交'] * 5)
        first = '<span class="badge badge-success">已提交</span>'
        invalid_markers = (
            first + '<span class="badge badge-danger">未提交</span>',
            '<span class="badge badge-danger">已提交</span>',
            '<span class="badge badge-success">未提交</span>',
            '<span class="badge badge-success">已提交，但需要重交</span>',
            '<span class="badge badge-success" hidden>已提交</span>',
            '<span class="badge badge-success" style="display:none">已提交</span>',
            '<span class="badge badge-success"><strong>已提交</strong></span>',
            '<span class="badge badge-success badge-danger">已提交</span>',
            '<span class="badge badge-success bg-danger">已提交</span>',
            '<span class="badge badge-success text-bg-warning">已提交</span>',
            '<span class="badge badge-success text-danger">已提交</span>',
            '<span class="badge badge-success badge-unknown">已提交</span>',
            '<span class="badge badge-success bg-unknown">已提交</span>',
        )
        for marker in invalid_markers:
            with self.subTest(marker=marker):
                summary = _submission_summary(_Tree(valid.replace(first, marker, 1)).root)
                self.assertEqual(summary, {'submitted': 4, 'unverified': 1})

    def test_numbered_question_badges_allow_layout_decoration_without_changing_tone(self):
        for extra in ('badge-pill', 'ml-2', 'badge-pill ml-2', 'bg-success text-success'):
            with self.subTest(extra=extra):
                source = numbered_submission_table(['已提交'] * 5).replace('class="badge badge-success"', 'class="badge badge-success ' + extra + '"')
                self.assertEqual(_submission_summary(_Tree(source).root), {'submitted': 5})
        source = numbered_submission_table(['未提交答案']).replace('class="badge badge-danger"', 'class="badge badge-danger badge-pill mr-1"')
        self.assertEqual(_submission_summary(_Tree(source).root), {'pending': 1})

    def test_numbered_question_rows_reject_gaps_duplicates_hidden_or_extra_tables(self):
        valid = numbered_submission_table(['已提交'] * 5)
        malformed = (
            valid.replace('<th>3.</th>', '<th>7.</th>'),
            valid.replace('<th>3.</th>', '<th>2.</th>'),
            valid.replace('<th>3.</th>', '<th>题目</th>'),
            valid.replace('<th>3.</th>', '<td>3.</td>'),
            valid.replace('<table>', '<table hidden>', 1),
            valid.replace('<tr>', '<tr style="display: none">', 1),
            valid + '<table><tr><td>other</td></tr></table>',
        )
        for source in malformed:
            with self.subTest(source=source):
                summary = _submission_summary(_Tree(source).root)
                self.assertTrue(not summary or summary.get('unverified', 0))

    def test_numbered_question_rows_do_not_use_question_answer_or_button_text(self):
        valid = numbered_submission_table(['已提交'] * 5)
        first = '<span class="badge badge-success">已提交</span>'
        distractors = (
            '题干说明：已提交',
            '<div>' + first + '</div>',
            '<textarea>' + first + '</textarea>',
            '<pre>' + first + '</pre>',
            '<a href="/answer">' + first + '</a>',
            '<button class="badge badge-success">已提交</button>',
        )
        for text in distractors:
            with self.subTest(text=text):
                summary = _submission_summary(_Tree(valid.replace(first, text, 1)).root)
                self.assertEqual(summary, {'submitted': 4, 'unverified': 1})

    def test_numbered_layout_completion_and_unknown_status_affect_collection_coverage(self):
        class NumberedStatusRequest(CourseRequest):
            unknown = False

            def get(self, url, **kwargs):
                response = super().get(url, **kwargs)
                start = response.body.find('<table>')
                if start >= 0:
                    end = response.body.index('</table>', start) + len('</table>')
                    table = numbered_submission_table(['已提交'] * 5)
                    if self.unknown:
                        table = table.replace('<span class="badge badge-success">已提交</span>', '<span>未知状态</span>', 1)
                    response.body = response.body[:start] + table + response.body[end:]
                return response

        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                context = Context()
                context.request = NumberedStatusRequest()
                context.request.unknown = unknown
                result = collect_xiji(context)
                self.assertTrue(result['auth']['authenticated'])
                self.assertEqual(len(result['candidates']), 3)
                self.assertEqual(result['diagnostics']['coverage']['complete'], not unknown)
                self.assertTrue(all(item['status'] == ('pending' if unknown else 'completed') for item in result['candidates']))
                self.assertEqual(result['diagnostics']['coverage']['assignment_statuses_unverified'], 3 if unknown else 0)

    def test_header_layout_ignores_hidden_tables_and_answer_tables(self):
        visible = '<table><tr><th>#</th><th>提交状态</th></tr><tr><td>1</td><td>已提交</td></tr></table>'
        fixtures = (
            visible.replace('<table>', '<table hidden>', 1),
            '<pre>' + visible + '</pre>',
            '<table><tr><td>PRIVATE QUESTION' + visible + '</td></tr></table>',
            '<pre>' + numbered_submission_table(['已提交'] * 5) + '</pre>',
        )
        for source in fixtures:
            with self.subTest(source=source):
                self.assertEqual(_submission_summary(_Tree(source).root), {})

    def test_header_layout_missing_or_hidden_status_cell_does_not_discard_question(self):
        prefix = '<table><tr><th>#</th><th>提交状态</th></tr><tr><td>1</td><td>已提交</td></tr>'
        second_rows = (
            '<tr><td>2</td></tr>',
            '<tr hidden><td>2</td><td>已提交</td></tr>',
            '<tr><td>2</td><td hidden>已提交</td></tr>',
        )
        for second in second_rows:
            with self.subTest(second=second):
                self.assertEqual(_submission_summary(_Tree(prefix + second + '</table>').root), {'submitted': 1, 'unverified': 1})

    def test_header_layout_uses_independent_status_not_sentence_substrings(self):
        for status in ('题干写到已提交字样', '<pre>已提交</pre>', '尚未提交但备注已提交', '已提交 未提交'):
            with self.subTest(status=status):
                source = '<table><tr><th>#</th><th>提交状态</th></tr><tr><td>1</td><td>' + status + '</td></tr></table>'
                self.assertEqual(_submission_summary(_Tree(source).root), {'unverified': 1})

    def test_observed_not_submitted_answer_label_is_pending(self):
        source = '<table><tr><th>#</th><th>提交状态</th></tr><tr><td>1</td><td>未提交答案</td></tr></table>'
        self.assertEqual(_submission_summary(_Tree(source).root), {'pending': 1})
        self.assertEqual(_submission_summary(_Tree(numbered_submission_table(['未提交答案'])).root), {'pending': 1})

    def test_marker_shape_diagnostics_count_unknown_classes_and_children_without_values(self):
        source = ('<table><tr><th>1.</th><td><span class="badge badge-success PRIVATE-DECORATION">'
                  '<i class="PRIVATE-ICON"></i>已提交</span></td></tr></table>')
        evidence = _submission_evidence(_Tree(source).root)
        marker = next(item for item in evidence['tables'][0]['rows'][0]['metadata_cells'][1]['exact_status_nodes'] if item['tag'] == 'span')
        self.assertEqual(marker['class_count'], 3)
        self.assertEqual(marker['unrecognized_class_count'], 1)
        self.assertEqual(marker['element_child_count'], 1)
        self.assertEqual(marker['element_child_tags'], ['i'])
        self.assertTrue(marker['direct_cell_child'])
        self.assertNotIn('PRIVATE', str(evidence))

    def test_campus_session_reuse_recovers_empty_main_and_rechecks_authentication(self):
        request = CampusRequest(blank_main=True)
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertTrue(result["authenticated"])
        self.assertIn("campus_session_reused", result["signals"])
        self.assertIn("复用", result["message"])
        self.assertEqual([url for url, _ in request.calls], [MAIN, HOME, _CAMPUS_LOGIN, request.ticket, MAIN])
        self.assertTrue(all(options["max_redirects"] == 0 for _, options in request.calls))
        self.assertNotIn("SYNTHETIC", str(result))

    def test_campus_ticket_service_redirect_is_bounded_and_logs_no_ticket(self):
        request = CampusRequest(service=Response(status=302, headers={"location": "/"}))
        result = collect_xiji(SimpleNamespace(request=request), course_scope=("计算机组成",))
        self.assertTrue(result["auth"]["authenticated"])
        self.assertEqual([url for url, _ in request.calls][:5], [MAIN, _CAMPUS_LOGIN, request.ticket, HOME, MAIN])
        self.assertNotIn("SYNTHETIC", str(result))
        routes = result["diagnostics"]["routes"]
        self.assertEqual(sum(route.get("purpose") == "campus_session_reuse" for route in routes), 3)

    def test_campus_service_portal_redirect_rechecks_fixed_main_without_fetching_portal(self):
        for target in ("/indexstudent.jsp", "/portal/loginSuccess.jsp?ticket=PRIVATE", ORIGIN + ":443/portal.jsp"):
            with self.subTest(target=target):
                request = CampusRequest(service=Response(status=302, headers={"location": target}))
                result = collect_xiji(SimpleNamespace(request=request), course_scope=("计算机组成",))
                self.assertTrue(result["auth"]["authenticated"])
                self.assertIn("campus_session_reused", result["auth"]["signals"])
                self.assertEqual([url for url, _ in request.calls][:4], [MAIN, _CAMPUS_LOGIN, request.ticket, MAIN])
                self.assertNotIn(urljoin(HOME, target), [url for url, _ in request.calls])
                self.assertNotIn("PRIVATE", str(result))
                redirect = next(route for route in result["diagnostics"]["routes"]
                                if route.get("result") == "verify_main_after_service_redirect")
                self.assertEqual(redirect["redirect"]["path"], urlparse(target).path)

    def test_campus_service_portal_redirect_is_not_login_evidence(self):
        request = CampusRequest(service=Response(status=302, headers={"location": "/portal.jsp"}),
                                final_main=Response('<a id="userDropdown">本人</a>'))
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["status"], "unverified")
        self.assertNotIn("campus_session_reused", result["signals"])
        self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN, request.ticket, MAIN])

    def test_campus_service_portal_recheck_can_confirm_real_login_failure(self):
        request = CampusRequest(service=Response(status=302, headers={"location": "/portal.jsp"}),
                                final_main=Response(status=302, headers={"location": "/indexcs/simple.jsp?loginErr=0"}))
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["status"], "not_authenticated")
        self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN, request.ticket, MAIN])

    def test_campus_service_unsafe_redirects_never_fetch_target_or_recheck_main(self):
        targets = ("/logout", "/accounts/logoff.jsp", "/auth/signout.jsp", "/%6cogout",
                   "https://other.invalid/portal.jsp", "http://cslabcg.whu.edu.cn/portal.jsp",
                   "https://user:password@cslabcg.whu.edu.cn/portal.jsp",
                   "https://:@cslabcg.whu.edu.cn/portal.jsp", "https://cslabcg.whu.edu.cn:444/portal.jsp",
                   "/portal.jsp#fragment", "/portal.jsp;state=PRIVATE", "/ST-SYNTHETIC/portal.jsp",
                   "/%53%54-SYNTHETIC/portal.jsp")
        for target in targets:
            with self.subTest(target=target):
                request = CampusRequest(service=Response(status=302, headers={"location": target}))
                result = collect_xiji(SimpleNamespace(request=request))
                self.assertFalse(result["auth"]["authenticated"])
                self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN, request.ticket])
                self.assertTrue(any(route.get("result") == "blocked_sso_redirect"
                                    for route in result["diagnostics"]["routes"]))
                self.assertNotIn("SYNTHETIC", str(result))
                self.assertNotIn("PRIVATE", str(result))

    def test_campus_form_or_captcha_requires_person_and_never_submits(self):
        for body in ('<form><input type="password" name="password"></form>', '<form><input name="captcha"></form>'):
            request = CampusRequest(cas=Response(body))
            result = probe_xiji_auth(SimpleNamespace(request=request))
            self.assertEqual(result["status"], "not_authenticated")
            self.assertIn("本人", result["message"])
            self.assertNotIn("campus_session_reused", result["signals"])
            self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN])

    def test_campus_handoff_requires_final_authenticated_navigation(self):
        request = CampusRequest(final_main=Response('<a id="userDropdown">本人</a>'))
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["status"], "unverified")
        self.assertNotIn("campus_session_reused", result["signals"])

    def test_campus_redirect_never_leaks_ticket_to_external_or_unknown_routes(self):
        targets = [
            "https://evil.invalid/?ticket=ST-SYNTHETIC",
            HOME + "?ticket=ST-SYNTHETIC&other=1",
            MAIN + "?ticket=ST-SYNTHETIC",
            "http://cslabcg.whu.edu.cn/?ticket=ST-SYNTHETIC",
            "https://user:pass@cslabcg.whu.edu.cn/?ticket=ST-SYNTHETIC",
            _CAMPUS_LOGIN.replace("cslabcg", "other"),
            HOME + "?ticket=ST-SYNTHETIC#fragment",
        ]
        for target in targets:
            request = CampusRequest(cas=Response(status=302, headers={"location": target}))
            result = collect_xiji(SimpleNamespace(request=request))
            self.assertFalse(result["auth"]["authenticated"])
            self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN])
            self.assertNotIn("SYNTHETIC", str(result))
            self.assertNotIn("evil.invalid", str(result))
            self.assertTrue(any(route.get("result") == "blocked_sso_redirect" for route in result["diagnostics"]["routes"]))

    def test_campus_service_cannot_redirect_ticket_to_a_second_service(self):
        request = CampusRequest(service=Response(status=302, headers={"location": "https://evil.invalid/ST-SYNTHETIC"}))
        result = collect_xiji(SimpleNamespace(request=request))
        self.assertFalse(result["auth"]["authenticated"])
        self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN, request.ticket])
        self.assertNotIn("SYNTHETIC", str(result))

    def test_campus_service_returns_known_login_without_fetching_it(self):
        request = CampusRequest(service=Response(status=302, headers={"location": "/indexcs/simple.jsp?loginErr=0"}))
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertEqual(result["status"], "not_authenticated")
        self.assertIn("本人", result["message"])
        self.assertEqual([url for url, _ in request.calls], [MAIN, _CAMPUS_LOGIN, request.ticket])

    def test_campus_recovery_and_verification_share_five_request_limit(self):
        class VerifyLoopRequest(CampusRequest):
            def get(self, url, **kwargs):
                if self.authenticated and url in {MAIN, HOME}:
                    self.calls.append((url, kwargs))
                    return Response(status=302, headers={"location": HOME if url == MAIN else MAIN})
                return super().get(url, **kwargs)
        request = VerifyLoopRequest()
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(request.calls), 6)  # Initial probe + five recovery requests.
        self.assertEqual(sum(url == _CAMPUS_LOGIN for url, _ in request.calls), 1)

    def test_campus_sso_redirect_loop_and_explicit_logout_do_not_retry(self):
        for redirect in (_CAMPUS_LOGIN, HOME):
            request = CampusRequest(cas=Response(status=302, headers={"location": redirect}))
            result = probe_xiji_auth(SimpleNamespace(request=request))
            self.assertEqual(result["status"], "error")
            self.assertEqual(len(request.calls), 2)
        request = CampusRequest(service=Response(status=302, headers={"location": "/logout"}))
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(request.calls), 3)

    def test_campus_network_failure_is_sanitized_and_not_repeated(self):
        request = CampusRequest(cas=RuntimeError("network secret ST-SYNTHETIC"))
        result = probe_xiji_auth(SimpleNamespace(request=request))
        self.assertEqual(result["status"], "error")
        self.assertIn("网络", result["message"])
        self.assertNotIn("SYNTHETIC", str(result))
        self.assertEqual(len(request.calls), 2)

    def test_no_campus_cookie_or_normal_login_makes_no_sso_request(self):
        request = CampusRequest(cookies=False)
        self.assertEqual(probe_xiji_auth(SimpleNamespace(request=request))["status"], "not_authenticated")
        self.assertEqual([url for url, _ in request.calls], [MAIN])
        request = CampusRequest(authenticated=True)
        self.assertTrue(probe_xiji_auth(SimpleNamespace(request=request))["authenticated"])
        self.assertEqual(request.storage_reads, 0)
        self.assertEqual([url for url, _ in request.calls], [MAIN])

    def test_collection_does_not_repeat_sso_if_session_expires_during_collection(self):
        request = CampusRequest(listing=Response('<input type="password">'))
        result = collect_xiji(SimpleNamespace(request=request), course_scope=("计算机组成",))
        self.assertEqual(result["auth"]["status"], "not_authenticated")
        self.assertEqual(sum(url == _CAMPUS_LOGIN for url, _ in request.calls), 1)
        self.assertEqual(request.storage_reads, 1)

    def test_campus_ticket_validation_rejects_ambiguous_or_oversized_tokens(self):
        self.assertTrue(_campus_ticket_url(HOME + "?ticket=ST-abc_12.~cas01"))
        for query in ("ticket=", "ticket=TGT-abc", "ticket=ST-abc&ticket=ST-def", "ticket=ST-abc&", "ticket=ST-" + "x" * 2041,
                      "ticket=ST-abc%0A", "ticket=ST-abc%26other%3D1"):
            self.assertFalse(_campus_ticket_url(HOME + "?" + query))
        self.assertFalse(_campus_ticket_url("https://:@cslabcg.whu.edu.cn/?ticket=ST-abc"))


if __name__ == "__main__":
    unittest.main()
