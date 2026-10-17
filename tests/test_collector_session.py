import json
import copy
import tempfile
import unittest
from collections import deque
from concurrent.futures import Future
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

    def goto(self, url, **kwargs):
        self.url = url


class FakeBrowser:
    def __init__(self):
        self.alive = True
        self.browser = SimpleNamespace(is_connected=lambda: self.alive)
        self.state = synthetic_state("profile")
        self.pages = []
        self.request = object()
        self.add_init_script = Mock()
        self.close = Mock(side_effect=self.disconnect)

    def disconnect(self):
        self.alive = False

    def storage_state(self):
        return copy.deepcopy(self.state)

    def add_cookies(self, cookies):
        self.state["cookies"] = copy.deepcopy(cookies)

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
            context = SimpleNamespace(add_cookies=lambda values: calls.append(values), add_init_script=lambda value: None)
            BrowserCollector(directory)._restore_session(context)
            self.assertEqual(calls, [state["cookies"]])

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
    def run_worker(self, collector, actions, browser=None, on_collect=None):
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
                on_collect(context)
            return {"auth": {"authenticated": True, "status": "authenticated", "message": "synthetic"},
                    "candidates": [], "warnings": [], "status_updates": [],
                    "diagnostics": {"coverage": {"complete": True}}}

        with patch("playwright.sync_api.sync_playwright", return_value=manager), \
                patch("studyreminder.collector.importlib.reload", side_effect=lambda module: module), \
                patch("studyreminder.chaoxing_adapter.collect_chaoxing", side_effect=collected) as chaoxing, \
                patch("studyreminder.xiji_adapter.collect_xiji", side_effect=collected) as xiji:
            collector._run()
        return SimpleNamespace(results=results, clients=clients, launch=launch, browser=browser,
                               request_factory=request_factory, chaoxing=chaoxing, xiji=xiji)

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
            value = self.run_worker(BrowserCollector(directory), [("open", "unknown"), ("restore", "unknown")])
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
            self.assertIs(contexts[0], browser)
            self.assertIs(contexts[1], browser)
            self.assertEqual(contexts[2].pages, [])
            self.assertEqual(value.clients[1].state["cookies"], synthetic_state("new-login")["cookies"])
            self.assertEqual(json.loads(path.read_text())["cookies"], synthetic_state("new-login")["cookies"])
            self.assertEqual(json.loads(path.read_text())["origins"], synthetic_state("new-login")["origins"])

    def test_repeated_manual_open_keeps_existing_login_window(self):
        with tempfile.TemporaryDirectory() as directory:
            value = self.run_worker(BrowserCollector(directory), [("open", "chaoxing"), ("open", "xiji")])
            value.launch.assert_called_once()
            value.request_factory.assert_not_called()
            self.assertEqual(len(value.browser.pages), 2)
            value.browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
