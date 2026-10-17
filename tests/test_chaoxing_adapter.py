"""Offline contract tests. Fixtures are synthetic, not captured account data."""
import json
import hashlib
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from studyreminder.chaoxing_adapter import (
    _Reader, _Tree, _confirmed_work_dates, _dates, _exam_rows, _node_params, _safe_url,
    collect_chaoxing, probe_chaoxing_auth,
)


class Response:
    def __init__(self, body="", status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    def text(self):
        return self.body


class Request:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        path = urlparse(url).path
        if path in self.overrides:
            result = self.overrides[path]
            if isinstance(result, Exception):
                raise result
            return result
        pages = {
            "/base": '<script>var uid="123";var fid="456";</script>'
                     '<a dataurl="https://mooc2-ans.chaoxing.com/visit/interaction">课程</a>',
            "/exam-ans/exam/test/examcode/examlist": '<table><tr class="dataTr">'
                '<td>1</td><td><a href="?examId=7&amp;courseId=11">大物期中考试</a></td>'
                '<td>2026-10-20 10:00 至 2026-10-20 12:00</td>'
                '<td>120分钟</td><td>未开始</td><td>未作答</td><td></td><td></td><td></td></tr></table>',
            "/mooc2-ans/visit/courselistdata": '<li><a href="https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=11&amp;clazzid=22&amp;cpi=33">'
                '<span class="course-name" title="概率论与数理统计A">概率论与数理统计A</span></a></li>',
            "/visit/stucoursemiddle": '<input id="workEnc" value="abcd">'
                '<input id="enc" value="efgh"><input id="openc" value="ijkl">',
            "/mooc2/work/list": '<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;cpi=33&amp;workId=44&amp;enc=private-signature">'
                '<p class="overHidden2 fl">微积分作业</p><p class="status fl">未提交</p></li>',
            "/mooc-ans/mooc2/work/task": '<h2>作业详情</h2>'
                '<p>作答时间: 2026-10-15 20:00 至 2026-10-20 21:00</p>',
        }
        if path not in pages:
            raise AssertionError("Unexpected read-only request path: " + path)
        return Response(pages[path])


class Context:
    def __init__(self, overrides=None):
        self.request = Request(overrides)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch("studyreminder.chaoxing_adapter.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_auth_requires_positive_user_and_authenticated_ui(self):
        self.assertTrue(probe_chaoxing_auth(Context())["authenticated"])
        for body in ('<script>var uid="123";</script>',
                     '<a dataurl="https://mooc2-ans.chaoxing.com/visit/interaction">课程</a>',
                     '<script>var uid="0";</script><a href="logout">退出</a>'):
            self.assertFalse(probe_chaoxing_auth(Context({"/base": Response(body)}))["authenticated"])

    def test_login_page_and_redirect_never_authenticate(self):
        for response in (Response('<input type="password">'), Response(status=302, headers={"location": "https://passport2.chaoxing.com/login"})):
            context = Context({"/base": response})
            result = collect_chaoxing(context)
            self.assertFalse(result["auth"]["authenticated"])
            self.assertEqual(result["candidates"], [])
            self.assertEqual(len(context.request.calls), 1)

    def test_network_failure_does_not_leak_private_url(self):
        result = probe_chaoxing_auth(Context({"/base": RuntimeError("timeout https://secret.invalid/?token=PRIVATE")}))
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["status"], "error")
        self.assertNotIn("PRIVATE", result["message"])

    def test_yearless_and_invalid_dates_are_not_guessed(self):
        for body in ('<p>作答时间: 10-15 20:00 至 10-20 21:00</p>',
                     '<p>截止时间: 2026-02-30 21:00</p>',
                     '<p>剩余时间: 24小时</p>'):
            self.assertIsNone(_dates(_Tree(body).root)[0])

    def test_full_range_and_server_epoch_retain_true_year(self):
        due, start = _dates(_Tree('<p>作答时间: 2026-12-25 20:00 至 2027-01-02 21:00</p>').root)
        self.assertEqual(due, "2027-01-02T21:00:00+08:00")
        self.assertEqual(start, "2026-12-25T20:00:00+08:00")
        due, _ = _dates(_Tree('<input name="endTime" value="0">').root)
        self.assertIsNone(due)
        due, _ = _dates(_Tree('<input name="endTime" value="1792501200000">').root)
        self.assertEqual(due, "2026-10-20T21:00:00+08:00")

    def test_conflicting_visible_and_hidden_times_require_review(self):
        for body in ('<p>截止时间: 2026-10-20 21:00</p><input id="endTime" value="2026-10-21 21:00">',
                     '<p>作答时间: 10-15 20:00 至 10-20 21:00</p><input id="endTime" value="2026-10-21 21:00">'):
            self.assertIsNone(_dates(_Tree(body).root)[0])

    def test_offline_collection_reads_lists_and_preserves_identity(self):
        context = Context()
        result = collect_chaoxing(context)
        self.assertTrue(result["auth"]["authenticated"])
        self.assertEqual({item["source_key"] for item in result["candidates"]},
                         {"course:11:exam:7", "course:11:class:22:work:44"})
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotIn("private-signature", str(result["candidates"]))
        self.assertTrue(all(options["max_redirects"] == 0 for _, options in context.request.calls))
        self.assertTrue(all("dowork" not in url and "startExam" not in url for url, _ in context.request.calls))
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])

    def test_failed_or_unrecognized_exams_do_not_complete_daily_coverage(self):
        for response in (
            Response(status=500),
            Response('<main>changed exam layout</main>'),
            Response('<tr class="dataTr"><td>1</td><td>大物考试</td></tr>'),
            Response('<tr class="dataTr"><td>1</td><td>大物考试</td><td>时间未知</td>'
                     '<td>120分钟</td><td>未作答</td></tr>'),
        ):
            with self.subTest(body=response.body, status=response.status):
                result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": response}))
                self.assertTrue(result["auth"]["authenticated"])
                self.assertEqual(result["diagnostics"]["coverage"]["work_lists_read"], 1)
                self.assertFalse(result["diagnostics"]["coverage"]["complete"])

    def test_partial_homework_read_does_not_complete_daily_coverage(self):
        for path, response in (
            ("/mooc2/work/list", Response('<main>changed homework layout</main>')),
            ("/mooc-ans/mooc2/work/task", Response('<p>metadata unavailable</p>')),
            ("/mooc-ans/mooc2/work/task", Response(status=500)),
        ):
            with self.subTest(path=path, body=response.body, status=response.status):
                result = collect_chaoxing(Context({path: response}))
                self.assertTrue(result["auth"]["authenticated"])
                self.assertFalse(result["diagnostics"]["coverage"]["complete"])

    def test_explicit_empty_homework_and_exam_lists_complete_coverage(self):
        for empty_exam in ("暂无考试", "暂无数据", "没有相关考试"):
            with self.subTest(empty_exam=empty_exam):
                result = collect_chaoxing(Context({
                    "/exam-ans/exam/test/examcode/examlist": Response('<p>' + empty_exam + '</p>'),
                    "/mooc2/work/list": Response('<p>暂无作业</p>'),
                }))
                coverage = result["diagnostics"]["coverage"]
                self.assertEqual(result["candidates"], [])
                self.assertEqual(coverage["work_lists_empty"], 1)
                self.assertTrue(coverage["complete"])

    def test_submitted_homework_can_complete_status_read_without_deadline(self):
        listing = Response('<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;workId=44">'
                           '<p class="overHidden2 fl">已交作业</p><p class="status fl">已提交</p></li>')
        result = collect_chaoxing(Context({"/mooc2/work/list": listing}))
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertTrue(any(update["status"] == "completed" for update in result["status_updates"]))
        self.assertFalse(any(item["kind"] == "assignment" for item in result["candidates"]))

    def test_missing_year_work_warns_and_is_not_added(self):
        result = collect_chaoxing(Context({"/mooc-ans/mooc2/work/task": Response('<p>作答时间: 10-15 20:00 至 10-20 21:00</p>')}))
        self.assertFalse(any(item["kind"] == "assignment" for item in result["candidates"]))
        self.assertTrue(any("年份" in warning for warning in result["warnings"]))

    def test_action_url_only_parsed_and_never_requested(self):
        node = _Tree('<a onclick="go(\'/exam/start?moocId=11&amp;refer=%2Ftake%3FexamId%3D7%26courseId%3D11\')">开始考试</a>').root
        fields = _node_params(next(child for child in node.walk() if child.tag == "a"))
        self.assertEqual(fields["examid"], "7")
        self.assertEqual(fields["courseid"], "11")
        self.assertFalse(_safe_url("https://mooc1.chaoxing.com/exam/start?examId=7"))
        self.assertFalse(_safe_url("https://chaoxing.com.evil.invalid/base"))
        self.assertFalse(_safe_url("https://user:pass@i.chaoxing.com/base"))

    def test_external_redirect_is_not_followed(self):
        context = Context({"/base": Response(status=302, headers={"location": "https://evil.invalid/base"})})
        self.assertFalse(probe_chaoxing_auth(context)["authenticated"])
        self.assertEqual(len(context.request.calls), 1)

    def test_blocked_route_diagnostics_exclude_query_and_credentials(self):
        context = Context({"/visit/stucoursemiddle": Response(status=302, headers={
            "location": "https://user:password@mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?enc=PRIVATE#FRAGMENT"
        })})
        result = collect_chaoxing(context)
        warning = next(item for item in result["warnings"] if "不在只读列表范围" in item)
        self.assertIn("mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse", warning)
        diagnostic = str(result["diagnostics"])
        for secret in ("PRIVATE", "FRAGMENT", "password", "user:"):
            self.assertNotIn(secret, warning)
            self.assertNotIn(secret, diagnostic)
        self.assertTrue(any(route.get("result") == "blocked_route" for route in result["diagnostics"]["routes"]))
        self.assertEqual(len(context.request.calls), 4)

    def test_course_coverage_can_exceed_thirty_without_disabling_budget(self):
        cards = "".join('<li><a href="https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=' + str(index)
                        + '&amp;clazzid=22&amp;cpi=33"><span class="course-name">课程' + str(index)
                        + '</span></a></li>' for index in range(1, 36))
        result = collect_chaoxing(Context({"/mooc2-ans/visit/courselistdata": Response(cards),
                                            "/mooc2/work/list": Response("<p>暂无作业</p>")}), homework_courses=("课程",))
        coverage = result["diagnostics"]["coverage"]
        self.assertEqual(coverage["courses_found"], 35)
        self.assertEqual(coverage["courses_attempted"], 35)
        self.assertEqual(coverage["work_lists_read"], 35)
        self.assertLessEqual(result["diagnostics"]["requests"], 100)
        self.assertFalse(any("前 30 门" in warning for warning in result["warnings"]))

    def test_only_configured_homework_and_exam_courses_are_collected(self):
        cards = '<li><a href="https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=99&amp;clazzid=22&amp;cpi=33">' \
                '<span class="course-name">高等数学A1</span></a></li>'
        context = Context({"/mooc2-ans/visit/courselistdata": Response(cards)})
        result = collect_chaoxing(context, exam_courses=("另一门课",))
        self.assertEqual(result["candidates"], [])
        self.assertFalse(any(urlparse(url).path == "/visit/stucoursemiddle" for url, _ in context.request.calls))
        self.assertEqual(result["diagnostics"]["coverage"]["courses_in_scope"], 0)
        self.assertEqual(result["diagnostics"]["coverage"]["courses_out_of_scope"], 1)
        self.assertFalse(any("未发现真实考试" in warning for warning in result["warnings"]))

    def test_empty_scope_disables_platform_without_checking_login_or_making_requests(self):
        context = Context({"/base": RuntimeError("must not request")})
        result = collect_chaoxing(context, homework_courses=[], exam_courses=[])
        self.assertEqual(context.request.calls, [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["warnings"], [])
        self.assertFalse(result["auth"]["authenticated"])
        self.assertEqual(result["auth"]["status"], "disabled")
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertTrue(result["diagnostics"]["coverage"]["disabled"])

    def test_homework_only_scope_skips_exam_request_and_still_completes(self):
        context = Context({"/exam-ans/exam/test/examcode/examlist": RuntimeError("must not request")})
        result = collect_chaoxing(context, exam_courses=[])
        self.assertEqual([item["kind"] for item in result["candidates"]], ["assignment"])
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertFalse(any("examlist" in url for url, _ in context.request.calls))

    def test_exam_only_scope_skips_course_traversal_and_needs_no_homework_fid(self):
        context = Context({"/base": Response('<script>var uid="123";</script><a href="logout">退出</a>'),
                           "/mooc2-ans/visit/courselistdata": RuntimeError("must not request")})
        result = collect_chaoxing(context, homework_courses=[])
        self.assertEqual([item["kind"] for item in result["candidates"]], ["exam"])
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        self.assertEqual(len(context.request.calls), 2)
        self.assertFalse(any("fid" in warning for warning in result["warnings"]))

    def test_custom_homework_scope_reads_own_course_and_requires_every_keyword_to_match(self):
        card = '<li><a href="https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=11&amp;clazzid=22&amp;cpi=33"><span class="course-name">高等数学A1</span></a></li>'
        context = Context({"/mooc2-ans/visit/courselistdata": Response(card)})
        result = collect_chaoxing(context, homework_courses=["高等数学"], exam_courses=[])
        self.assertEqual([item["course"] for item in result["candidates"]], ["高等数学A1"])
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])
        missing = collect_chaoxing(context, homework_courses=["高等数学", "线性代数"], exam_courses=[])
        self.assertFalse(missing["diagnostics"]["coverage"]["complete"])
        self.assertEqual(missing["diagnostics"]["coverage"]["homework_scope_missing"], ["线性代数"])

    def test_confirmed_year_does_not_apply_to_multiple_matching_courses(self):
        cards = ''.join('<li><a href="https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=' + value
                        + '&amp;clazzid=22&amp;cpi=33"><span class="course-name">概率论' + value + '</span></a></li>'
                        for value in ("11", "12"))
        context = Context({"/mooc2-ans/visit/courselistdata": Response(cards),
                           "/mooc-ans/mooc2/work/task": Response('<p>作答时间:10-14 12:15 至 10-21 09:50</p>')})
        for year in (2026, {"year": 2026, "courseid": "11", "clazzid": "22"}):
            result = collect_chaoxing(context, homework_year=year, exam_courses=[])
            self.assertEqual(result["candidates"], [])
            self.assertIsNone(result["diagnostics"]["homework_year_binding"])
            self.assertFalse(result["diagnostics"]["coverage"]["complete"])
            self.assertTrue(any("唯一" in warning for warning in result["warnings"]))

    def test_supported_course_page_redirect_is_bound_to_exact_host(self):
        self.assertTrue(_safe_url("https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?enc=private"))
        self.assertFalse(_safe_url("https://mooc1.chaoxing.com/mooc2-ans/mycourse/studentcourse"))
        self.assertFalse(_safe_url("https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentstudy"))
        context = Context({"/visit/stucoursemiddle": Response(status=302, headers={
            "location": "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?enc=PRIVATE"
        }), "/mooc2-ans/mycourse/studentcourse": Response('<input id="workEnc" value="abc">')})
        result = collect_chaoxing(context)
        self.assertTrue(any(item["kind"] == "assignment" for item in result["candidates"]))
        self.assertNotIn("PRIVATE", str(result["diagnostics"]))

    def test_live_observed_course_stu_redirect_is_read_only_and_exact_host(self):
        target = "https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=11&amp;clazzid=22&amp;enc=PRIVATE"
        self.assertTrue(_safe_url(target))
        self.assertFalse(_safe_url("https://mooc1.chaoxing.com/mooc2-ans/mycourse/stu"))
        self.assertFalse(_safe_url("https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu/submit"))
        context = Context({"/visit/stucoursemiddle": Response(status=302, headers={"location": target}),
                           "/mooc2-ans/mycourse/stu": Response('<input id="workEnc" value="abc">')})
        result = collect_chaoxing(context)
        self.assertTrue(any(item["kind"] == "assignment" for item in result["candidates"]))
        self.assertNotIn("PRIVATE", str(result["diagnostics"]))

    def test_homework_detail_redirect_requires_same_course_class_and_work(self):
        path = "/mooc-ans/mooc2/work/dowork"
        target = "https://mooc1.chaoxing.com" + path + "?courseId=11&classId=22&workId=44&enc=PRIVATE"
        self.assertFalse(_safe_url(target))
        responses = {"/mooc-ans/mooc2/work/task": Response(status=302, headers={"location": target}),
                     path: Response('<p>截止时间:2026-10-20 21:00</p><script>submitAnswers()</script>')}
        context = Context(responses)
        result = collect_chaoxing(context)
        self.assertTrue(any(item["kind"] == "assignment" for item in result["candidates"]))
        self.assertTrue(any(urlparse(url).path == path for url, _ in context.request.calls))
        self.assertNotIn("PRIVATE", str(result["diagnostics"]))
        direct = _Reader(Context(responses))
        with self.assertRaisesRegex(Exception, "不在只读列表范围"):
            direct.get(target)
        self.assertEqual(direct.context.request.calls, [])
        for mismatch in (target.replace("workId=44", "workId=99"), target.replace("courseId=11", "courseId=99"),
                         target.replace("classId=22", "classId=99"), target.replace("mooc1.", "mooc2-ans."),
                         target.replace("/work/dowork", "/exam/start")):
            responses["/mooc-ans/mooc2/work/task"] = Response(status=302, headers={"location": mismatch})
            context = Context(responses)
            result = collect_chaoxing(context)
            self.assertFalse(any(item["kind"] == "assignment" for item in result["candidates"]))
            self.assertFalse(any(urlparse(url).path == path for url, _ in context.request.calls))

    def test_explicit_list_deadline_avoids_opening_homework_detail(self):
        row = '<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;workId=44">' \
              '<p class="overHidden2">概率论作业</p><p class="status">未提交</p><p>截止时间:2026-10-20 21:00</p></li>'
        context = Context({"/mooc2/work/list": Response(row)})
        result = collect_chaoxing(context)
        assignment = next(item for item in result["candidates"] if item["kind"] == "assignment")
        self.assertEqual(assignment["due_at"], "2026-10-20T21:00:00+08:00")
        self.assertFalse(any("/work/task" in url for url, _ in context.request.calls))

    def test_work_time_evidence_contains_dates_only_and_no_question_or_signatures(self):
        body = '<p>PRIVATE_QUESTION</p><p>作答时间:10-15 20:00 至 10-20 21:00</p><p>PRIVATE_ANSWER</p>' \
               '<input id="endTime" value="2026-10-20 21:00"><input id="workId" value="1234567890">' \
               '<input id="startTime" value="1792501200000"><input id="endTimeToken" value="2026-10-20 21:00">' \
               '<input id="uid" value="PRIVATE_UID"><input id="enc" value="PRIVATE_ENC">' \
               '<script>var workEndTime="2026-10-20 21:00";var dateBegin=1792501200000;' \
               'var endTime="PRIVATE_SIGNATURE";var workId=1234567890;var enc="PRIVATE_TOKEN";</script>'
        result = collect_chaoxing(Context({"/mooc-ans/mooc2/work/task": Response(body)}))
        evidence = result["diagnostics"]["work_time_evidence"][0]
        self.assertTrue(any("10-20 21:00" in fragment for fragment in evidence["visible_time_fragments"]))
        self.assertEqual(evidence["time_inputs"], [{"field": "endtime", "value": "2026-10-20 21:00"},
                                                  {"field": "starttime", "value": "1792501200000"}])
        self.assertEqual(evidence["script_time_fields"], [{"field": "workEndTime", "value": "2026-10-20 21:00"},
                                                         {"field": "dateBegin", "value": "1792501200000"}])
        self.assertNotIn("PRIVATE", json.dumps(evidence))
        self.assertNotIn("1234567890", json.dumps(evidence))

    def test_completed_homework_updates_status_without_reading_due_or_detail(self):
        row = '<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;workId=44">' \
              '<p class="overHidden2">作业3-1</p><p class="status">待批阅</p></li>'
        context = Context({"/mooc2/work/list": Response(row)})
        result = collect_chaoxing(context)
        update = next(item for item in result["status_updates"] if item["source_key"].endswith(":work:44"))
        self.assertEqual(update["status"], "completed")
        self.assertEqual(update["status_evidence"], "待批阅")
        self.assertFalse(any("/work/task" in url for url, _ in context.request.calls))
        self.assertFalse(any(item["kind"] == "assignment" for item in result["candidates"]))
        self.assertEqual(result["diagnostics"]["work_status_evidence"][0]["recognized_status"], "completed")

    def test_pending_and_unknown_homework_states_are_not_inferred_from_deadline(self):
        for state in ("未提交", "未交", "已结束"):
            row = '<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;workId=44">' \
                  '<p class="overHidden2">作业3-1</p><p class="status">' + state + '</p></li>'
            result = collect_chaoxing(Context({"/mooc2/work/list": Response(row)}))
            updates = [item for item in result["status_updates"] if item["source_key"].endswith(":work:44")]
            if state in {"未提交", "未交"}:
                self.assertEqual(updates[0]["status"], "pending")
            else:
                self.assertEqual(updates, [])
                candidate = next(item for item in result["candidates"] if item["kind"] == "assignment")
                self.assertIn("平台提交状态未能可靠确认", candidate["notes"])
            self.assertEqual(result["diagnostics"]["work_status_evidence"][0]["status_evidence"], state)

    def test_all_list_completion_updates_survive_detail_failure(self):
        def row(work_id, state):
            return '<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;workId=' + str(work_id) + '">' \
                   '<p class="overHidden2">作业</p><p class="status">' + state + '</p></li>'
        rows = row(44, "未提交") + "".join(row(index, "已提交") for index in range(100, 185))
        result = collect_chaoxing(Context({"/mooc2/work/list": Response(rows), "/mooc-ans/mooc2/work/task": RuntimeError("network")}))
        completed = [item for item in result["status_updates"] if ":work:" in item["source_key"] and item["status"] == "completed"]
        self.assertEqual(len(completed), 85)

    def test_completed_exam_has_completed_candidate_and_status_update(self):
        body = '<tr class="dataTr"><td>1</td><td><a href="?examId=7&amp;courseId=11">大物测验</a></td>' \
               '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>已结束</td><td>已完成</td><td>100</td></tr>'
        result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": Response(body)}))
        exam = next(item for item in result["candidates"] if item["kind"] == "exam")
        update = next(item for item in result["status_updates"] if item["source_key"] == "course:11:exam:7")
        self.assertEqual(exam["status"], "completed")
        self.assertEqual(update["status"], "completed")
        self.assertEqual(update["status_evidence"], "已完成")

    def test_confirmed_year_is_explicit_bound_and_preserves_platform_range(self):
        body = '<p>作答时间:10-14 12:15 至 10-21 09:50</p>'
        context = Context({"/mooc-ans/mooc2/work/task": Response(body)})
        unconfirmed = collect_chaoxing(context)
        self.assertFalse(any(item["kind"] == "assignment" for item in unconfirmed["candidates"]))
        result = collect_chaoxing(context, homework_year=2026)
        work = next(item for item in result["candidates"] if item["kind"] == "assignment")
        self.assertEqual(work["due_at"], "2026-10-21T09:50:00+08:00")
        self.assertEqual(work["start_at"], "2026-10-14T12:15:00+08:00")
        self.assertIn("用户确认年份 2026", work["notes"])
        self.assertIn("课程 ID 11", work["notes"])
        self.assertIn("10-14 12:15 至 10-21 09:50", work["notes"])
        self.assertEqual(result["diagnostics"]["homework_year_binding"], {"year": 2026, "courseid": "11", "clazzid": "22"})
        mismatch = collect_chaoxing(context, homework_year={"year": 2026, "courseid": "99", "clazzid": "22"})
        self.assertFalse(any(item["kind"] == "assignment" for item in mismatch["candidates"]))

    def test_confirmed_work_year_handles_december_to_january_without_overriding_server_dates(self):
        due, start, original = _confirmed_work_dates(_Tree('<p>作答时间:12-28 20:00 至 01-03 09:50</p>').root, 2026)
        self.assertEqual(due, "2027-01-03T09:50:00+08:00")
        self.assertEqual(start, "2026-12-28T20:00:00+08:00")
        self.assertIn("12-28", original)
        for body in ('<p>作答时间:10-14 12:15 至 10-21 09:50</p><input id="endTime" value="2025-10-21 09:50">',
                     '<p>作答时间:10-21 12:15 至 10-14 09:50</p>'):
            self.assertEqual(_confirmed_work_dates(_Tree(body).root, 2026), (None, None, None))

    def test_request_budget_remains_enforced(self):
        reader = _Reader(Context())
        reader.count = 100
        with self.assertRaisesRegex(Exception, "请求数上限"):
            reader.get("https://i.chaoxing.com/base")
        self.assertEqual(reader.context.request.calls, [])

    def test_missing_target_exam_id_returns_only_redacted_local_structure(self):
        table = '<table><tr class="dataTr" id="exam_row_123"><td>1</td><td>大物单元测验 PRIVATE_NAME</td>' \
                '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>未开始</td><td>' \
                '<a id="examLink_456" class="examAction enc_PRIVATE_CLASS" ' \
                'href="https://user:PRIVATE_PASSWORD@mooc1.chaoxing.com/exam/notes?testId=22&amp;courseId=11&amp;uid=999&amp;enc=PRIVATE_ENC&amp;token=PRIVATE_TOKEN#PRIVATE_FRAGMENT" ' \
                'onclick="return showExam(123, &quot;456&quot;, &quot;PRIVATE_SIGNATURE,PRIVATE_FUNC(777)&quot;, this, &quot;123456789012345678901234567890&quot;);">进入</a>' \
                '<input id="token_PRIVATE_INPUT" value="PRIVATE_INPUT"></td></tr></table>'
        result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": Response(table)}))
        rows = result["diagnostics"]["unidentified_exam_rows"]
        self.assertEqual(len(rows), 1)
        encoded = json.dumps(rows)
        for secret in ("PRIVATE", "999", "123456789012345678901234567890", "2026-10-20", "777"):
            self.assertNotIn(secret, encoded)
        anchor = rows[0]["children"][5]["children"][0]
        self.assertEqual(anchor["id"], "examLink_{number}")
        self.assertEqual(anchor["onclick"], [{"function": "showExam", "argument_count": 5,
                                               "numeric_arguments": [{"position": 1, "value": "123"}, {"position": 2, "value": "456"}]}])
        self.assertEqual(anchor["href"]["numeric_ids"], {"courseId": "11"})
        self.assertEqual(anchor["href"]["host"], "mooc1.chaoxing.com")
        self.assertIn("token", anchor["href"]["query_fields"])
        exam = next(item for item in result["candidates"] if item["kind"] == "exam")
        self.assertEqual(exam["identity_kind"], "list_fingerprint")
        self.assertTrue(exam["source_key"].startswith("exam-list-fingerprint:"))

    def test_unrelated_missing_exam_id_produces_no_row_diagnostic(self):
        table = '<tr class="dataTr"><td>1</td><td>高等数学</td><td>时间</td><td>120</td><td>未开始</td></tr>'
        result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": Response(table)}))
        self.assertEqual(result["diagnostics"]["unidentified_exam_rows"], [])

    def test_unique_exam_without_platform_id_has_stable_fingerprint_when_due_changes(self):
        def table(due):
            return '<tr class="dataTr"><td>1</td><td>2026年秋季大物B磁学单元测验</td>' \
                   '<td>2026-10-20 10:00 至 ' + due + '</td><td>120</td><td>未开始</td><td>未作答</td><td></td><td></td><td><span>尚未开放</span></td></tr>'
        first, later = [], []
        warnings = []
        first = _exam_rows(_Tree(table("2026-10-20 12:00")).root, warnings, ("大物",))
        later = _exam_rows(_Tree(table("2026-10-21 12:00")).root, warnings, ("大物",))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["source_key"], later[0]["source_key"])
        self.assertNotEqual(first[0]["due_at"], later[0]["due_at"])
        self.assertEqual(first[0]["course"], "大学物理")
        self.assertIn("不是平台考试 ID", first[0]["notes"])
        self.assertEqual(warnings, [])

    def test_same_name_exams_without_id_or_start_require_confirmation(self):
        row = '<tr class="dataTr"><td>1</td><td>大物单元测验</td><td>截止时间:2026-10-20 12:00</td><td>120</td><td>未开始</td></tr>'
        warnings = []
        self.assertEqual(_exam_rows(_Tree(row + row).root, warnings, ("大物",)), [])
        self.assertTrue(any("列表内同名" in warning for warning in warnings))
        self.assertEqual(len(_exam_rows(_Tree(row).root, [], ("大物",))), 1)

    def test_conflicting_simultaneous_exam_fingerprints_do_not_override(self):
        def row(due):
            return '<tr class="dataTr"><td>1</td><td>大物单元测验</td><td>2026-10-20 10:00 至 ' + due + '</td><td>120</td><td>未开始</td></tr>'
        warnings = []
        result = _exam_rows(_Tree(row("2026-10-20 12:00") + row("2026-10-21 12:00")).root, warnings, ("大物",))
        self.assertEqual(result, [])
        self.assertTrue(any("相互冲突" in warning for warning in warnings))

    def test_shared_exam_keyword_keeps_actual_courses_and_submission_states_separate(self):
        rows = ''.join('<tr class="dataTr" coursename="' + course + '"><td>1</td><td>期中考试</td>'
                       '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>已发布</td><td>'
                       + status + '</td></tr>'
                       for course, status in (("高等数学", "未作答"), ("离散数学", "已完成")))
        result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": Response(rows)}),
                                  homework_courses=[], exam_courses=["数学"])
        self.assertEqual({item["course"]: item["status"] for item in result["candidates"]},
                         {"高等数学": "pending", "离散数学": "completed"})
        self.assertEqual(len({item["source_key"] for item in result["candidates"]}), 2)
        self.assertEqual(len(result["status_updates"]), 2)
        self.assertTrue(result["diagnostics"]["coverage"]["complete"])

    def test_exam_fingerprint_uses_platform_course_name_independent_of_filter_keyword(self):
        row = '<tr class="dataTr" data-course-name="高等数学A"><td>1</td><td>期中考试</td>' \
              '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>未开始</td><td>未作答</td></tr>'
        first = _exam_rows(_Tree(row).root, [], ["数学"])
        later = _exam_rows(_Tree(row).root, [], ["高等数学"])
        self.assertEqual(first[0]["course"], "高等数学A")
        self.assertEqual(first[0]["source_key"], later[0]["source_key"])

    def test_unidentified_exam_course_is_not_inferred_from_configured_keyword(self):
        row = '<tr class="dataTr"><td>1</td><td>高等数学期中考试</td>' \
              '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>未开始</td><td>未作答</td></tr>'
        result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": Response(row)}),
                                  homework_courses=[], exam_courses=["数学"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status_updates"], [])
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])
        self.assertTrue(any("唯一课程身份" in warning for warning in result["warnings"]))

    def test_conflicting_exam_submission_states_exclude_candidates_and_status_updates(self):
        for identity in (' coursename="高等数学"', ' data-examid="7" data-courseid="11" coursename="高等数学"'):
            with self.subTest(identity=identity):
                rows = ''.join('<tr class="dataTr"' + identity + '><td>1</td><td>期中考试</td>'
                               '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>已发布</td><td>'
                               + status + '</td></tr>' for status in ("未作答", "已完成"))
                result = collect_chaoxing(Context({"/exam-ans/exam/test/examcode/examlist": Response(rows)}),
                                          homework_courses=[], exam_courses=["数学"])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["status_updates"], [])
                self.assertFalse(result["diagnostics"]["coverage"]["complete"])

    def test_conflicting_homework_states_also_exclude_pending_date_candidate(self):
        rows = ''.join('<li onclick="goTask(this);" data="https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=11&amp;classId=22&amp;workId=44">'
                       '<p class="overHidden2">作业</p><p class="status">' + status
                       + '</p><p>截止时间:2026-10-20 21:00</p></li>' for status in ("未提交", "已完成"))
        result = collect_chaoxing(Context({"/mooc2/work/list": Response(rows)}), exam_courses=[])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status_updates"], [])
        self.assertFalse(result["diagnostics"]["coverage"]["complete"])

    def test_legacy_physics_fingerprint_is_preserved_without_explicit_course_name(self):
        row = '<tr class="dataTr"><td>1</td><td>2026年秋季大物B磁学单元测验</td>' \
              '<td>2026-10-20 10:00 至 2026-10-20 12:00</td><td>120</td><td>未开始</td><td>未作答</td></tr>'
        expected_payload = json.dumps(["大学物理", "2026年秋季大物B磁学单元测验", "2026-10-20T10:00:00+08:00"],
                                      ensure_ascii=False, separators=(",", ":"))
        expected_key = "exam-list-fingerprint:" + hashlib.sha256(expected_payload.encode()).hexdigest()
        for course_attribute in ('', ' coursename="大学物理"'):
            table = row.replace('<tr class="dataTr">', '<tr class="dataTr"' + course_attribute + '>')
            result = _exam_rows(_Tree(table).root, [], ["大物", "大学物理"])
            self.assertEqual(result[0]["source_key"], expected_key)


if __name__ == "__main__":
    unittest.main()
