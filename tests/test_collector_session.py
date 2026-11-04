import json
import copy
import tempfile
import unittest
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from studyreminder.collector import BrowserCollector, _coverage_complete


def synthetic_state(value="old", origins=None):
    return {"cookies": [{"name": "session", "domain": ".chaoxing.com", "value": value, "path": "/"}],
            "origins": origins if origins is not None else [
                {"origin": "https://i.chaoxing.com", "localStorage": [{"name": "synthetic", "value": value}]}]}


class FakeAPI:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.dispose = Mock()

    def storage_state(self):
        return copy.deepcopy(self.state)


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.frames = []
        self.bring_to_front = Mock()
        self.goto = Mock(side_effect=self.navigate)
        self.reload = Mock(side_effect=AssertionError("automatic collection must not refresh pages"))
        self.evaluate = Mock(side_effect=AssertionError("automatic collection must not evaluate pages"))

    def navigate(self, url, **kwargs):
        self.url = url


class FakeBrowser:
    def __init__(self):
        self.alive = True
        self.browser = SimpleNamespace(is_connected=lambda: self.alive)
        self.state = synthetic_state("profile")
        self.pages = []
        self.request = object()
        self.add_init_script = Mock()
        self.storage_state = Mock(side_effect=self.snapshot)
        self.close = Mock(side_effect=self.disconnect)

    def disconnect(self):
        self.alive = False

    def snapshot(self):
        return copy.deepcopy(self.state)

    def cookies(self):
        return copy.deepcopy(self.state["cookies"])

    def add_cookies(self, cookies):
        # BrowserContext.add_cookies merges supplied keys into the persistent
        # profile; it does not clear cookies from other domains or paths.
        def key(cookie):
            return cookie["name"], cookie["domain"], cookie["path"]
        merged = {key(cookie): cookie for cookie in self.state["cookies"]}
        merged.update({key(cookie): copy.deepcopy(cookie) for cookie in cookies})
        self.state["cookies"] = list(merged.values())

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


class ScriptedQueue:
    def __init__(self, jobs):
        self.jobs = deque(jobs)

    def get(self):
        while callable(self.jobs[0]):
            self.jobs.popleft()()
        return self.jobs.popleft()


class SessionTests(unittest.TestCase):
    def test_restart_marks_saved_platform_sessions_without_claiming_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            state = synthetic_state("saved")
            state["cookies"].append({"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn", "value": "saved", "path": "/"})
            (Path(directory) / "browser-session.json").write_text(json.dumps(state))
            with patch("playwright.sync_api.sync_playwright") as playwright:
                collector = BrowserCollector(directory)
            playwright.assert_not_called()
            self.assertIsNone(collector.thread)
            for source in ("chaoxing", "xiji"):
                self.assertEqual(collector.status[source]["status"], "session_saved")
                self.assertNotIn("authenticated", collector.status[source])
                self.assertIn("检查登录状态", collector.status[source]["message"])

    def test_startup_only_marks_platforms_with_saved_trusted_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            state = synthetic_state("saved")
            state["origins"].append({"origin": "https://cslabcg.whu.edu.cn", "localStorage": []})
            state["cookies"].append({"name": "other", "domain": "unrelated.example", "value": "not-trusted", "path": "/"})
            (Path(directory) / "browser-session.json").write_text(json.dumps(state))
            collector = BrowserCollector(directory)
            self.assertEqual(collector.status["chaoxing"]["status"], "session_saved")
            self.assertEqual(collector.status["xiji"]["status"], "not_connected")

    def test_startup_missing_or_invalid_saved_session_stays_not_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            for payload in (None, "not-json", '{"cookies": null}'):
                if payload is not None:
                    (Path(directory) / "browser-session.json").write_text(payload)
                collector = BrowserCollector(directory)
                self.assertTrue(all(item["status"] == "not_connected" for item in collector.status.values()))

    def test_daily_success_requires_both_adapters_strict_complete_flags(self):
        diagnostics = {"chaoxing": {"coverage": {"courses_in_scope": 1, "work_lists_read": 1}},
                       "xiji": {"coverage": {"complete": True}}}
        self.assertFalse(_coverage_complete(diagnostics, True, True))
        diagnostics["chaoxing"]["coverage"]["complete"] = False
        self.assertFalse(_coverage_complete(diagnostics, True, True))
        diagnostics["chaoxing"]["coverage"]["complete"] = True
        self.assertTrue(_coverage_complete(diagnostics, True, True))
        self.assertFalse(_coverage_complete(diagnostics, False, True))
        self.assertFalse(_coverage_complete(diagnostics, True, False))
        diagnostics["xiji"]["coverage"]["complete"] = False
        self.assertFalse(_coverage_complete(diagnostics, True, True))

    def test_scan_wait_covers_both_adapter_budgets_without_changing_other_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            collector.thread = Mock()
            collector.thread.is_alive.return_value = True
            future = Mock()
            with patch("studyreminder.collector.importlib.util.find_spec", return_value=object()), \
                    patch("studyreminder.collector.Future", return_value=future):
                for action, timeout in (("scan", 180), ("restore", 100), ("open", 100), ("save_session", 100)):
                    with self.subTest(action=action):
                        collector.request(action, "chaoxing")
                        future.result.assert_called_with(timeout=timeout)
            collector.thread.start.assert_not_called()

    def test_login_actions_fail_fast_while_scan_is_running_or_queued(self):
        for queued in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                collector = BrowserCollector(directory)
                running = Future()
                if queued:
                    collector._pending_requests[running] = ("scan", {})
                else:
                    collector._active_job = ("scan", {}, running)
                with patch("studyreminder.collector.importlib.util.find_spec", return_value=object()):
                    for action in ("open", "probe", "finish_login"):
                        with self.subTest(action=action, queued=queued), self.assertRaisesRegex(ValueError, "正在同步课程"):
                            collector.request(action, "xiji")
                self.assertEqual(collector.jobs.qsize(), 0)
                self.assertIsNone(collector.thread)

    def test_duplicate_or_overlapping_login_requests_are_not_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            outstanding = Future()
            collector._pending_requests[outstanding] = ("open", "xiji")
            with patch("studyreminder.collector.importlib.util.find_spec", return_value=object()):
                for action, source in (("open", "xiji"), ("probe", "xiji"), ("finish_login", "xiji"), ("open", "chaoxing")):
                    with self.subTest(action=action, source=source), self.assertRaisesRegex(ValueError, "正在处理登录"):
                        collector.request(action, source)
            self.assertEqual(collector.jobs.qsize(), 0)

    def test_queue_wait_timeout_cancels_job_and_clears_pending_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            collector.thread = Mock()
            collector.thread.is_alive.return_value = True
            queued = Future()
            with patch("studyreminder.collector.importlib.util.find_spec", return_value=object()), \
                    patch("studyreminder.collector.Future", return_value=queued), \
                    patch.object(queued, "result", side_effect=FutureTimeoutError):
                with self.assertRaisesRegex(ValueError, "尚未开始的请求已取消"):
                    collector.request("open", "xiji")
            self.assertTrue(queued.cancelled())
            self.assertEqual(collector._pending_requests, {})
            self.assertIs(collector.jobs.get_nowait()[2], queued)

    def test_running_timeout_keeps_guard_until_operation_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            collector.thread = Mock()
            collector.thread.is_alive.return_value = True
            running = Future()
            self.assertTrue(running.set_running_or_notify_cancel())
            with patch("studyreminder.collector.importlib.util.find_spec", return_value=object()), \
                    patch("studyreminder.collector.Future", return_value=running), \
                    patch.object(running, "result", side_effect=FutureTimeoutError):
                with self.assertRaisesRegex(ValueError, "仍在处理中"):
                    collector.request("open", "xiji")
                with self.assertRaisesRegex(ValueError, "正在处理登录"):
                    collector.request("open", "xiji")
            self.assertFalse(running.cancelled())
            self.assertIn(running, collector._pending_requests)
            running.set_result({"ok": True})
            self.assertEqual(collector._pending_requests, {})

    def test_session_keeps_only_task_platforms_and_uses_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            context = SimpleNamespace(storage_state=lambda: {
                "cookies": [
                    {"name": "test", "domain": ".chaoxing.com", "value": "synthetic", "path": "/"},
                    {"name": "test", "domain": "cslabcg.whu.edu.cn", "value": "synthetic", "path": "/"},
                    {"name": "test", "domain": "unrelated.example", "value": "synthetic", "path": "/"}
                ], "origins": [{"origin": "https://i.chaoxing.com", "localStorage": []},
                               {"origin": "https://unrelated.example", "localStorage": []}]
            })
            collector._save_session(context)
            path = Path(directory) / "browser-session.json"
            state = json.loads(path.read_text())
            self.assertEqual(len(state["cookies"]), 2)
            self.assertEqual(len(state["origins"]), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_saved_session_restores_cookies_without_password_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "browser-session.json"
            state = synthetic_state()
            path.write_text(json.dumps(state))
            calls = []
            context = SimpleNamespace(cookies=lambda: [], add_cookies=lambda values: calls.append(values), add_init_script=lambda value: None)
            BrowserCollector(directory)._restore_session(context)
            self.assertEqual(calls, [state["cookies"]])

    def test_restore_preserves_entire_existing_profile_platform_without_mixing_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = synthetic_state("saved-chaoxing")
            saved["cookies"].extend([
                {"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn", "path": "/", "value": "saved-old"},
                {"name": "saved-account-extra", "domain": "cas.whu.edu.cn", "path": "/", "value": "old-account"}])
            (Path(directory) / "browser-session.json").write_text(json.dumps(saved))
            browser = FakeBrowser()
            current = {"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn", "path": "/", "value": "profile-current"}
            browser.state = {"cookies": [current], "origins": []}
            BrowserCollector(directory)._restore_session(browser)
            self.assertEqual(BrowserCollector._platform_cookies(browser.state, "xiji"), [current])
            self.assertEqual(BrowserCollector._platform_cookies(browser.state, "chaoxing"),
                             BrowserCollector._platform_cookies(saved, "chaoxing"))

    def test_tracking_cookie_expiration_cannot_overwrite_refreshed_api_session(self):
        before = {"cookies": [
            {"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn", "path": "/", "value": "browser-old"},
            {"name": "tracking", "domain": "cslabcg.whu.edu.cn", "path": "/", "value": "tracking"}], "origins": []}
        saved = copy.deepcopy(before)
        saved["cookies"][0]["value"] = "api-refreshed"
        current = copy.deepcopy(before)
        current["cookies"].pop()
        merged, changed = BrowserCollector._merge_browser_snapshot(saved, before, current)
        self.assertEqual(merged, saved)
        self.assertEqual(changed, {"xiji"})

    def test_diverged_platforms_are_preserved_as_whole_candidates_instead_of_cookie_mix(self):
        before = synthetic_state("baseline")
        saved = synthetic_state("api-account")
        saved["cookies"].append({"name": "api-extra", "domain": ".chaoxing.com", "path": "/", "value": "api-account"})
        current = synthetic_state("browser-account")
        current["cookies"].append({"name": "browser-extra", "domain": ".chaoxing.com", "path": "/", "value": "browser-account"})
        merged, changed = BrowserCollector._merge_browser_snapshot(saved, before, current)
        self.assertEqual(merged, saved)
        self.assertEqual(changed, {"chaoxing"})

    def test_http_cookie_save_preserves_browser_local_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            path = Path(directory) / "browser-session.json"
            old = synthetic_state()
            path.write_text(json.dumps(old))
            api = FakeAPI(synthetic_state("refreshed", origins=[]))
            collector._save_session(api, preserve_origins=True)
            updated = json.loads(path.read_text())
            self.assertEqual(updated["cookies"], synthetic_state("refreshed")["cookies"])
            self.assertEqual(updated["origins"], old["origins"])
            # A request client cannot overwrite newer browser localStorage with
            # an old snapshot returned by its storage_state method either.
            api.state["origins"] = synthetic_state("stale")["origins"]
            collector._save_session(api, preserve_origins=True)
            self.assertEqual(json.loads(path.read_text())["origins"], old["origins"])

    def test_browser_save_keeps_unvisited_saved_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            old = synthetic_state()
            old["origins"].append({"origin": "https://cslabcg.whu.edu.cn", "localStorage": [{"name": "synthetic", "value": "keep"}]})
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(old))
            collector._save_session(FakeAPI(synthetic_state("new-login")))
            saved = json.loads(path.read_text())
            self.assertEqual(saved["origins"][0], synthetic_state("new-login")["origins"][0])
            self.assertEqual(saved["origins"][1], old["origins"][1])


class AutomaticCollectionTests(unittest.TestCase):
    def run_worker(self, collector, actions, browser=None, on_collect=None, probe_authenticated=True, on_probe=None):
        browser = browser or FakeBrowser()
        clients = []

        def new_api(**kwargs):
            client = FakeAPI(kwargs["storage_state"])
            clients.append(client)
            return client

        request_factory = Mock(side_effect=new_api)
        launch = Mock(return_value=browser)
        playwright = SimpleNamespace(request=SimpleNamespace(new_context=request_factory),
                                     chromium=SimpleNamespace(launch_persistent_context=launch))
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        results, jobs = [], []
        for action in actions:
            if callable(action):
                jobs.append(action)
                continue
            name, source = action
            future = Future()
            jobs.append((name, source, future))
            results.append(future)
        jobs.append(("close", None, Future()))
        collector.jobs = ScriptedQueue(jobs)

        def collected(context, **kwargs):
            if on_collect:
                result = on_collect(context)
                if result is not None:
                    return result
            return {"auth": {"authenticated": True, "status": "authenticated", "message": "synthetic"},
                    "candidates": [], "warnings": [], "status_updates": [],
                    "diagnostics": {"coverage": {"complete": True}}}

        def probed(context):
            if on_probe:
                result = on_probe(context)
                if result is not None:
                    return result
            return {"authenticated": probe_authenticated,
                    "status": "authenticated" if probe_authenticated else "not_authenticated", "message": "synthetic"}

        with patch("playwright.sync_api.sync_playwright", return_value=manager), \
                patch("studyreminder.collector.importlib.reload", side_effect=lambda module: module), \
                patch("studyreminder.chaoxing_adapter.probe_chaoxing_auth", side_effect=probed) as chaoxing_probe, \
                patch("studyreminder.xiji_adapter.probe_xiji_auth", side_effect=probed) as xiji_probe, \
                patch("studyreminder.chaoxing_adapter.collect_chaoxing", side_effect=collected) as chaoxing, \
                patch("studyreminder.xiji_adapter.collect_xiji", side_effect=collected) as xiji:
            collector._run()
        return SimpleNamespace(results=results, clients=clients, launch=launch, browser=browser,
                               request_factory=request_factory, chaoxing=chaoxing, xiji=xiji,
                               chaoxing_probe=chaoxing_probe, xiji_probe=xiji_probe)

    def test_automatic_restore_and_scan_never_launch_or_navigate_chrome(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(synthetic_state()))
            value = self.run_worker(collector, [("restore", "chaoxing"), ("restore", "xiji"), ("scan", {})])
            value.launch.assert_not_called()
            self.assertEqual(value.browser.pages, [])
            self.assertEqual(value.request_factory.call_count, 1)
            self.assertEqual(value.chaoxing.call_count, 1)
            self.assertEqual(value.xiji.call_count, 1)
            context = value.chaoxing.call_args.args[0]
            self.assertEqual(context.pages, [])
            self.assertIs(context.request, value.clients[0])
            self.assertTrue(value.results[2].result()["coverage_complete"])
            value.clients[0].dispose.assert_called_once()

    def test_periodic_save_without_context_is_noop_and_does_not_create_client(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_worker(BrowserCollector(directory), [("save_session", None), ("save_session", None)])
            value.launch.assert_not_called()
            value.request_factory.assert_not_called()
            self.assertTrue(all(f.result()["ok"] for f in value.results))
            self.assertFalse((Path(directory) / "browser-session.json").exists())

    def test_scan_without_restore_still_uses_both_http_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_worker(BrowserCollector(directory), [("scan", {})])
            value.launch.assert_not_called()
            self.assertEqual(value.chaoxing.call_count, 1)
            self.assertEqual(value.xiji.call_count, 1)
            self.assertTrue(value.results[0].result()["coverage_complete"])

    def test_invalid_platform_is_rejected_before_starting_any_client(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_worker(BrowserCollector(directory), [
                ("open", "unknown"), ("restore", "unknown"), ("probe", "unknown"), ("finish_login", "unknown")])
            value.launch.assert_not_called()
            value.request_factory.assert_not_called()
            for future in value.results:
                with self.assertRaisesRegex(ValueError, "不支持的平台"):
                    future.result()

    def test_manual_login_uses_visible_browser_then_returns_to_http_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(synthetic_state()))
            browser = FakeBrowser()
            browser.state = synthetic_state()
            contexts = []

            def login_completed():
                browser.state = synthetic_state("new-login")

            value = self.run_worker(collector, [
                ("restore", "chaoxing"), ("open", "chaoxing"), login_completed,
                ("save_session", None), ("restore", "xiji"), ("scan", {}),
                browser.disconnect, ("restore", "chaoxing"), ("scan", {})],
                browser=browser, on_collect=contexts.append)
            value.launch.assert_called_once()
            self.assertFalse(value.launch.call_args.kwargs["headless"])
            self.assertEqual(len(browser.pages), 1)
            browser.pages[0].bring_to_front.assert_called_once()
            # Automatic restore did not close the browser or navigate to Xiji
            # while the user had the manual login window open.
            browser.close.assert_not_called()
            self.assertEqual(len(value.clients), 2)
            value.clients[0].dispose.assert_called_once()
            self.assertIsNot(contexts[0], browser)
            self.assertIsNot(contexts[1], browser)
            self.assertEqual(contexts[0].pages, [])
            self.assertIs(contexts[0].request, value.clients[0])
            self.assertEqual(contexts[2].pages, [])
            self.assertEqual(value.clients[1].state["cookies"], synthetic_state("new-login")["cookies"])
            self.assertEqual(json.loads(path.read_text())["cookies"], synthetic_state("new-login")["cookies"])
            # Explicit open persists the dedicated profile's observed origins;
            # later automatic reads still do not evaluate browser localStorage.
            self.assertEqual(json.loads(path.read_text())["origins"], synthetic_state()["origins"])
            browser.pages[0].goto.assert_called_once()
            browser.pages[0].reload.assert_not_called()
            browser.pages[0].evaluate.assert_not_called()

    def test_open_browser_scan_captures_latest_login_and_preserves_http_cookie_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(synthetic_state("saved")))
            browser = FakeBrowser()
            browser.state = synthetic_state("saved")
            seeds, storage_reads = [], []

            def login_completed():
                browser.state = synthetic_state("live-login")
                storage_reads.append(browser.storage_state.call_count)

            def collect(context):
                self.assertEqual(context.pages, [])
                self.assertIsNot(context.request, browser.request)
                if not seeds or seeds[-1][0] is not context.request:
                    seeds.append((context.request, context.request.storage_state()))
                    context.request.state = synthetic_state("api-refreshed", origins=[])

            value = self.run_worker(collector, [
                ("open", "chaoxing"), login_completed,
                ("restore", "xiji"), ("scan", {}), ("save_session", None),
                ("save_session", None), ("scan", {})], browser=browser, on_collect=collect)
            self.assertEqual(len(value.clients), 2)
            self.assertEqual(seeds[0][1]["cookies"], synthetic_state("live-login")["cookies"])
            self.assertEqual(seeds[1][1]["cookies"], synthetic_state("api-refreshed")["cookies"])
            saved = json.loads(path.read_text())
            self.assertEqual(saved["cookies"], synthetic_state("api-refreshed")["cookies"])
            self.assertEqual(saved["origins"], synthetic_state("saved")["origins"])
            self.assertEqual(browser.storage_state.call_count, storage_reads[0])
            browser.pages[0].goto.assert_called_once()
            browser.pages[0].bring_to_front.assert_called_once()
            browser.pages[0].reload.assert_not_called()
            browser.pages[0].evaluate.assert_not_called()
            for client in value.clients:
                client.dispose.assert_called_once()

    def test_later_tracking_expiration_does_not_roll_back_an_earlier_http_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            state = synthetic_state("common-login")
            state["cookies"].append({"name": "tracking", "domain": ".chaoxing.com", "path": "/", "value": "tracking"})
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(state))
            browser = FakeBrowser()
            browser.state = copy.deepcopy(state)
            seeds = []

            def collect(context):
                if not seeds or seeds[-1][0] is not context.request:
                    seeds.append((context.request, context.request.storage_state()))
                    for cookie in context.request.state["cookies"]:
                        if cookie["name"] == "session":
                            cookie["value"] = "api-refreshed"

            def expire_tracking_only():
                browser.state["cookies"] = [cookie for cookie in browser.state["cookies"] if cookie["name"] != "tracking"]

            value = self.run_worker(BrowserCollector(directory), [
                ("open", "chaoxing"), ("scan", {}), expire_tracking_only,
                ("save_session", None), ("save_session", None), ("scan", {})], browser=browser, on_collect=collect)
            self.assertEqual(len(seeds), 2)
            self.assertEqual(next(cookie["value"] for cookie in seeds[1][1]["cookies"] if cookie["name"] == "session"), "api-refreshed")
            self.assertEqual(next(cookie["value"] for cookie in json.loads(path.read_text())["cookies"]
                                  if cookie["name"] == "session"), "api-refreshed")
            browser.pages[0].reload.assert_not_called()
            browser.pages[0].evaluate.assert_not_called()
            self.assertEqual(value.launch.call_count, 1)

    def test_new_manual_login_during_scan_wins_only_its_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            path = Path(directory) / "browser-session.json"
            state = synthetic_state("chaoxing-login")
            state["cookies"].append({"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn",
                                     "path": "/", "value": "xiji-login"})
            state["origins"].append({"origin": "https://cslabcg.whu.edu.cn", "localStorage": []})
            path.write_text(json.dumps(state))
            browser = FakeBrowser()
            browser.state = copy.deepcopy(state)
            calls, seeds = [], []

            def pages_loaded():
                browser.state = copy.deepcopy(state)

            def collect(context):
                self.assertEqual(context.pages, [])
                if not seeds or seeds[-1][0] is not context.request:
                    seeds.append((context.request, context.request.storage_state()))
                calls.append(context)
                if len(calls) == 1:
                    # A new Xiji account login completes while the HTTP scan,
                    # seeded with the previous account, is already in flight.
                    browser.state["cookies"][1]["value"] = "xiji-new-login"
                    browser.state["origins"][1]["localStorage"] = [{"name": "new", "value": "manual"}]
                    for cookie in context.request.state["cookies"]:
                        cookie["value"] = ("chaoxing-api-refreshed" if
                            BrowserCollector._cookie_platform(cookie) == "chaoxing" else "xiji-old-api-refreshed")
                    context.request.state["origins"] = []

            value = self.run_worker(collector, [
                ("open", "xiji"), pages_loaded, ("scan", {}), ("save_session", None), ("scan", {})],
                browser=browser, on_collect=collect)
            cookies = {BrowserCollector._cookie_platform(c): c["value"] for c in seeds[1][1]["cookies"]}
            self.assertEqual(cookies, {"chaoxing": "chaoxing-api-refreshed", "xiji": "xiji-new-login"})
            saved = json.loads(path.read_text())
            self.assertEqual({BrowserCollector._cookie_platform(c): c["value"] for c in saved["cookies"]}, cookies)
            # Automatic reads only snapshot cookies. localStorage remains owned
            # by explicit login/probe/finish actions, without page evaluation.
            self.assertEqual(saved["origins"][1]["localStorage"], [])
            self.assertEqual(len(value.clients), 2)

    def test_manual_logout_during_scan_is_not_undone_by_old_api_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(synthetic_state("logged-in")))
            browser = FakeBrowser()
            browser.state = synthetic_state("logged-in")

            def logout_during_collect(context):
                browser.state["cookies"] = []
                context.request.state = synthetic_state("old-response", origins=[])

            self.run_worker(collector, [("open", "chaoxing"), ("scan", {}), ("save_session", None)],
                            browser=browser, on_collect=logout_during_collect)
            self.assertEqual(json.loads(path.read_text())["cookies"], [])

    def test_scan_discards_old_account_auth_tasks_and_binding_after_manual_relogin(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            browser = FakeBrowser()
            calls = []
            due = "2026-10-22T23:59:00+08:00"

            def collect(context):
                calls.append(context)
                if len(calls) == 1:
                    browser.state = synthetic_state("new-account")
                    context.request.state = synthetic_state("old-expired-api", origins=[])
                    return {"auth": {"authenticated": False, "status": "not_authenticated", "message": "old session expired"},
                            "candidates": [{"source": "chaoxing", "source_key": "old-task", "due_at": due, "status": "pending"}],
                            "status_updates": [{"source": "chaoxing", "source_key": "old-submitted", "status": "completed"}],
                            "warnings": ["old login warning"],
                            "diagnostics": {"coverage": {"complete": True}, "homework_year_binding": {"old": "account"}}}
                return {"auth": {"authenticated": True, "status": "authenticated", "message": "current other platform"},
                        "candidates": [{"source": "xiji", "source_key": "valid-other-task", "due_at": due, "status": "pending"}],
                        "status_updates": [], "warnings": ["valid other warning"],
                        "diagnostics": {"coverage": {"complete": True}}}

            value = self.run_worker(collector, [("open", "chaoxing"), ("scan", {})],
                                    browser=browser, on_collect=collect)
            reply = value.results[1].result()
            self.assertFalse(reply["coverage_complete"])
            self.assertEqual(reply["source_results"]["chaoxing"], {
                "coverage_complete": False, "auth_status": "unverified", "deferred_reason": "session_changed_during_scan"})
            self.assertEqual(collector.status["chaoxing"]["status"], "unverified")
            self.assertEqual(reply["candidates"], [{"source": "xiji", "source_key": "valid-other-task", "due_at": due, "status": "pending"}])
            self.assertTrue(all(item["source"] == "xiji" for item in reply["status_updates"]))
            self.assertEqual(collector.last_candidates, reply["candidates"])
            self.assertEqual(collector.last_status_updates, reply["status_updates"])
            self.assertIsNone(reply["homework_year_binding"])
            self.assertNotIn("old login warning", reply["warnings"])
            self.assertIn("valid other warning", reply["warnings"])
            diagnostics = json.loads((Path(directory) / "collection-diagnostics.json").read_text())
            self.assertTrue(diagnostics["chaoxing"]["session_conflict"])
            self.assertFalse(diagnostics["chaoxing"]["coverage"]["complete"])
            self.assertNotIn("homework_year_binding", diagnostics["chaoxing"])
            self.assertTrue(diagnostics["xiji"]["coverage"]["complete"])
            self.assertEqual(json.loads((Path(directory) / "browser-session.json").read_text())["cookies"],
                             synthetic_state("new-account")["cookies"])

    def test_repeated_manual_open_keeps_existing_login_window(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_worker(BrowserCollector(directory), [("open", "chaoxing"), ("open", "xiji")])
            value.launch.assert_called_once()
            value.request_factory.assert_not_called()
            self.assertEqual(len(value.browser.pages), 2)
            value.browser.close.assert_called_once()

    def test_cancelled_queued_open_never_launches_or_navigates_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)

            def cancel_next_open():
                collector.jobs.jobs[0][2].cancel()

            value = self.run_worker(collector, [cancel_next_open, ("open", "xiji"), ("restore", "chaoxing")])
            self.assertTrue(value.results[0].cancelled())
            self.assertTrue(value.results[1].result()["ok"])
            value.launch.assert_not_called()
            self.assertEqual(value.browser.pages, [])
            self.assertIsNone(collector._active_job)

    def test_slow_login_navigation_shows_existing_window_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            browser = FakeBrowser()
            page = FakePage()

            def navigate(url, **kwargs):
                page.bring_to_front.assert_called_once()
                self.assertEqual(kwargs, {"wait_until": "commit", "timeout": 12000})
                page.url = url
                raise TimeoutError("synthetic platform timeout with private-url?ticket=not-for-display")

            page.goto = Mock(side_effect=navigate)

            def new_page():
                browser.pages.append(page)
                return page

            browser.new_page = new_page
            value = self.run_worker(collector, [("open", "xiji"), ("open", "xiji")], browser=browser)
            first = value.results[0].result()
            self.assertTrue(first["ok"])
            self.assertTrue(first["navigation_pending"])
            self.assertIn("登录窗口已打开", first["message"])
            self.assertNotIn("Chrome", first["message"])
            self.assertNotIn("private-url", first["message"])
            self.assertEqual(collector.status["xiji"]["status"], "browser_open")
            self.assertEqual(value.launch.call_args.kwargs["timeout"], 20000)
            page.goto.assert_called_once()
            self.assertEqual(page.bring_to_front.call_count, 2)
            page.reload.assert_not_called()

    def test_failed_login_navigation_does_not_leak_error_url_or_claim_launch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            browser = FakeBrowser()
            page = FakePage()
            page.goto = Mock(side_effect=RuntimeError("net::ERR_FAILED https://private.example/?ticket=hidden"))

            def new_page():
                browser.pages.append(page)
                return page

            browser.new_page = new_page
            value = self.run_worker(collector, [("open", "xiji")], browser=browser)
            reply = value.results[0].result()
            self.assertTrue(reply["ok"])
            self.assertTrue(reply["navigation_pending"])
            self.assertIn("登录窗口已打开", reply["message"])
            self.assertNotIn("private.example", reply["message"])
            self.assertNotIn("hidden", json.dumps(reply))
            self.assertEqual(collector.status["xiji"]["status"], "browser_open")
            page.bring_to_front.assert_called_once()
            page.reload.assert_not_called()

    def test_xiji_window_defers_course_collection_but_probes_login_without_page_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            browser = FakeBrowser()

            def after_scan():
                snapshots.append(browser.close.call_count)

            value = self.run_worker(BrowserCollector(directory), [
                ("open", "xiji"), ("scan", {}), after_scan], browser=browser)
            value.xiji.assert_not_called()
            value.xiji_probe.assert_called_once()
            self.assertEqual(value.xiji_probe.call_args.args[0].pages, [])
            reply = value.results[1].result()
            self.assertFalse(reply["coverage_complete"])
            self.assertEqual(reply["source_results"]["xiji"], {
                "coverage_complete": False, "auth_status": "authenticated", "deferred_reason": "login_window_open"})
            self.assertEqual(snapshots, [0])
            browser.pages[0].goto.assert_called_once()
            browser.pages[0].bring_to_front.assert_called_once()
            browser.pages[0].reload.assert_not_called()
            browser.pages[0].evaluate.assert_not_called()

    def test_finish_login_verifies_latest_session_then_closes_only_dedicated_context(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            browser = FakeBrowser()
            probe_states, close_counts = [], []

            def login_completed():
                browser.state = synthetic_state("live-login")

            def probe(context):
                self.assertEqual(context.pages, [])
                self.assertEqual(browser.close.call_count, 0)
                probe_states.append(context.request.storage_state())
                context.request.state = synthetic_state("verified-login", origins=[])

            def after_finish():
                close_counts.append(browser.close.call_count)

            value = self.run_worker(collector, [
                ("open", "xiji"), login_completed, ("finish_login", "xiji"), after_finish, ("scan", {})],
                browser=browser, on_probe=probe)
            self.assertTrue(value.results[1].result()["ok"])
            self.assertTrue(value.results[1].result()["closed_login_window"])
            self.assertEqual(probe_states[0]["cookies"], synthetic_state("live-login")["cookies"])
            self.assertEqual(value.clients[1].state["cookies"], synthetic_state("verified-login")["cookies"])
            self.assertEqual(json.loads((Path(directory) / "browser-session.json").read_text())["origins"],
                             synthetic_state("live-login")["origins"])
            self.assertEqual(close_counts, [1])
            browser.close.assert_called_once()
            value.xiji.assert_called_once()
            value.launch.assert_called_once()

    def test_finish_login_failure_keeps_window_open_and_scan_keeps_deferring(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = FakeBrowser()
            close_counts = []
            value = self.run_worker(BrowserCollector(directory), [
                ("open", "xiji"), ("finish_login", "xiji"),
                lambda: close_counts.append(browser.close.call_count), ("scan", {})],
                browser=browser, probe_authenticated=False)
            reply = value.results[1].result()
            self.assertFalse(reply["ok"])
            self.assertFalse(reply["closed_login_window"])
            self.assertEqual(close_counts, [0])
            value.xiji.assert_not_called()
            self.assertEqual(value.xiji_probe.call_count, 2)

    def test_finish_login_does_not_close_new_session_that_changed_during_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = FakeBrowser()
            close_counts = []

            def new_login_during_probe(context):
                browser.state = synthetic_state("different-login")

            value = self.run_worker(BrowserCollector(directory), [
                ("open", "chaoxing"), ("finish_login", "chaoxing"),
                lambda: close_counts.append(browser.close.call_count)], browser=browser, on_probe=new_login_during_probe)
            reply = value.results[1].result()
            self.assertFalse(reply["ok"])
            self.assertFalse(reply["closed_login_window"])
            self.assertEqual(reply["status"], "unverified")
            self.assertEqual(close_counts, [0])
            self.assertEqual(json.loads((Path(directory) / "browser-session.json").read_text())["cookies"],
                             synthetic_state("different-login")["cookies"])

    def test_finish_login_without_window_verifies_saved_session_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_worker(BrowserCollector(directory), [("finish_login", "chaoxing")])
            self.assertTrue(value.results[0].result()["ok"])
            self.assertFalse(value.results[0].result()["closed_login_window"])
            value.launch.assert_not_called()
            value.chaoxing_probe.assert_called_once()

    def test_explicit_open_bridges_profile_only_session_before_finish_login_and_scan(self):
        for existing_json in (False, True):
            with self.subTest(existing_json=existing_json), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "browser-session.json"
                if existing_json:
                    # A saved jar from another platform does not replace the
                    # dedicated profile's existing Xiji session.
                    saved = synthetic_state("saved-chaoxing")
                    saved["origins"].append({"origin": "https://cslabcg.whu.edu.cn",
                                             "localStorage": [{"name": "saved-only", "value": "keep"}]})
                    path.write_text(json.dumps(saved))
                collector = BrowserCollector(directory)
                browser = FakeBrowser()
                session_cookie = {"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn",
                                  "path": "/", "value": "synthetic-profile-only", "expires": -1}
                browser.state["cookies"].append(session_cookie)
                probe_states = []

                def probe(context):
                    probe_states.append(context.request.storage_state())

                value = self.run_worker(collector, [
                    ("open", "xiji"), ("finish_login", "xiji"), ("scan", {})],
                    browser=browser, on_probe=probe)
                self.assertTrue(value.results[1].result()["ok"])
                self.assertTrue(value.results[1].result()["closed_login_window"])
                self.assertIn(session_cookie, probe_states[0]["cookies"])
                self.assertIn(session_cookie, value.clients[1].state["cookies"])
                saved_after = json.loads(path.read_text())
                self.assertIn(session_cookie, saved_after["cookies"])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                if existing_json:
                    # The verified profile cookie bundle owns this platform.
                    # Do not combine it with unverified saved-only storage
                    # potentially belonging to a different account.
                    self.assertNotIn(saved["origins"][1], saved_after["origins"])
                value.launch.assert_called_once()
                browser.pages[0].goto.assert_called_once()
                browser.pages[0].reload.assert_not_called()
                browser.pages[0].evaluate.assert_not_called()

    def test_probe_uses_latest_login_without_closing_or_refreshing_window(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            browser = FakeBrowser()
            close_counts, states = [], []

            def login_completed():
                browser.state = synthetic_state("latest")

            def probe(context):
                self.assertEqual(context.pages, [])
                states.append(context.request.storage_state())

            value = self.run_worker(collector, [
                ("open", "xiji"), login_completed, ("probe", "xiji"),
                lambda: close_counts.append(browser.close.call_count)], browser=browser, on_probe=probe)
            reply = value.results[1].result()
            self.assertTrue(reply["authenticated"])
            self.assertEqual(reply["source"], "xiji")
            self.assertEqual(reply["status"], "authenticated")
            self.assertEqual(reply["auth"]["status"], "authenticated")
            self.assertFalse(reply["closed_login_window"])
            self.assertEqual(close_counts, [0])
            self.assertEqual(states[0]["cookies"], synthetic_state("latest")["cookies"])
            value.xiji.assert_not_called()
            browser.pages[0].goto.assert_called_once()
            browser.pages[0].bring_to_front.assert_called_once()
            browser.pages[0].reload.assert_not_called()
            browser.pages[0].evaluate.assert_not_called()

    def test_unchanged_profile_login_is_verified_and_adopted_without_saved_cookie_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = synthetic_state("saved-old-account")
            saved["cookies"].append({"name": "saved-extra", "domain": ".chaoxing.com", "path": "/", "value": "old-account"})
            saved["cookies"].append({"name": "JSESSIONID", "domain": "cslabcg.whu.edu.cn", "path": "/", "value": "other-platform"})
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(saved))
            browser = FakeBrowser()
            browser.state = synthetic_state("profile-current-account")
            browser.state["cookies"].append({"name": "profile-extra", "domain": ".chaoxing.com", "path": "/", "value": "current-account"})
            profile = copy.deepcopy(browser.state)
            seen = []

            def probe(context):
                seen.append(context.request.storage_state())

            value = self.run_worker(BrowserCollector(directory), [
                ("open", "chaoxing"), ("save_session", None), ("probe", "chaoxing")],
                browser=browser, on_probe=probe)
            self.assertTrue(value.results[2].result()["authenticated"])
            self.assertEqual(len(seen), 1)
            self.assertEqual(BrowserCollector._platform_cookies(seen[0], "chaoxing"),
                             BrowserCollector._platform_cookies(profile, "chaoxing"))
            persisted = json.loads(path.read_text())
            self.assertEqual(BrowserCollector._platform_snapshot(persisted, "chaoxing"),
                             BrowserCollector._platform_snapshot(profile, "chaoxing"))
            self.assertEqual(BrowserCollector._platform_cookies(persisted, "xiji"),
                             BrowserCollector._platform_cookies(saved, "xiji"))
            browser.pages[0].goto.assert_called_once()
            browser.pages[0].reload.assert_not_called()

    def test_failed_stale_profile_probe_falls_back_to_whole_saved_session_once(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = synthetic_state("working-saved")
            path = Path(directory) / "browser-session.json"
            path.write_text(json.dumps(saved))
            browser = FakeBrowser()
            browser.state = synthetic_state("stale-profile")
            seen = []

            def probe(context):
                candidate = context.request.storage_state()
                seen.append(candidate)
                valid = candidate["cookies"] == saved["cookies"]
                if not valid:
                    context.request.state["cookies"] = []
                return {"authenticated": valid, "status": "authenticated" if valid else "not_authenticated",
                        "message": "synthetic actual auth result"}

            value = self.run_worker(BrowserCollector(directory), [
                ("open", "chaoxing"), ("finish_login", "chaoxing")], browser=browser, on_probe=probe)
            reply = value.results[1].result()
            self.assertTrue(reply["authenticated"])
            self.assertTrue(reply["closed_login_window"])
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0]["cookies"], synthetic_state("stale-profile")["cookies"])
            self.assertEqual(seen[1]["cookies"], saved["cookies"])
            self.assertEqual(json.loads(path.read_text()), saved)
            for client in value.clients:
                client.dispose.assert_called_once()

    def test_cookie_presence_never_marks_either_failed_candidate_authenticated(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = synthetic_state("expired-saved")
            (Path(directory) / "browser-session.json").write_text(json.dumps(saved))
            browser = FakeBrowser()
            value = self.run_worker(BrowserCollector(directory), [
                ("open", "chaoxing"), ("probe", "chaoxing")], browser=browser, probe_authenticated=False)
            self.assertEqual(value.chaoxing_probe.call_count, 2)
            self.assertFalse(value.results[1].result()["authenticated"])
            self.assertEqual(value.results[1].result()["status"], "not_authenticated")

    def test_disabled_retry_does_not_replace_previous_login_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = BrowserCollector(directory)
            collector.status["xiji"] = {"status": "not_authenticated", "message": "need login"}
            result = {"auth": {"authenticated": False, "status": "disabled", "message": "disabled"},
                      "candidates": [], "warnings": [], "diagnostics": {"coverage": {"complete": True, "disabled": True}}}
            # Use on_collect to replace the second adapter's return only after
            # worker-level patches have been installed.
            def collect(context):
                from studyreminder import xiji_adapter
                xiji_adapter.collect_xiji.return_value = result
                xiji_adapter.collect_xiji.side_effect = None

            value = self.run_worker(collector, [("scan", {
                "course_scope": {"xiji": []}, "preserve_disabled_status": ["xiji"]})], on_collect=collect)
            self.assertEqual(collector.status["xiji"], {"status": "not_authenticated", "message": "need login"})
            self.assertEqual(value.results[0].result()["source_results"]["xiji"]["auth_status"], "disabled")


if __name__ == "__main__":
    unittest.main()
