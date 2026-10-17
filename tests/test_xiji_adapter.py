"""Offline synthetic fixtures; no captured account data or authentication."""
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from studyreminder.xiji_adapter import (
    MAIN, _Reader, _Tree, _assignment_detail, _assignments, _course_contexts, _current_course, _safe_url,
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
    def __init__(self, main=None, listing=None):
        self.calls = []
        self.main = main or Response(NAV + '<h2>计算机组成与体系结构</h2>')
        self.listing = listing or Response(ROW)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        path = urlparse(url).path
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

    def test_unknown_redirect_stops_without_fetching_or_leaking_query(self):
        context = Context(listing=Response(status=302, headers={"location": "https://cslabcg.whu.edu.cn/new/list.jsp?token=PRIVATE"}))
        result = collect_xiji(context)
        self.assertIn("cslabcg.whu.edu.cn/new/list.jsp", " ".join(result["warnings"]))
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(len(context.request.calls), 2)
        self.assertTrue(any(route.get("result") == "blocked_route" for route in result["diagnostics"]["routes"]))

    def test_assignment_metadata_allows_only_observed_numeric_navigation(self):
        self.assertTrue(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp"))
        self.assertTrue(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp?courseID=9&assignID=5712"))
        self.assertFalse(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp?assignID=5712&submit=true"))
        self.assertFalse(_safe_url("https://cslabcg.whu.edu.cn/assignment/index.jsp?token=PRIVATE"))

    def test_credential_and_external_routes_rejected(self):
        for url in ("https://user:pass@cslabcg.whu.edu.cn/main.jsp", "https://cslabcg.whu.edu.cn.evil.invalid/main.jsp", "http://cslabcg.whu.edu.cn/main.jsp", "https://cslabcg.whu.edu.cn/assignment/submit.jsp?assignID=5712", "https://cslabcg.whu.edu.cn/main.jsp?doSubmit=1"):
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


if __name__ == "__main__":
    unittest.main()
