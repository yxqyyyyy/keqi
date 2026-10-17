import io
import os
import plistlib
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import manage_background_service as service


class BackgroundServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.path = Path(self.temp.name) / "LaunchAgents" / f"{service.LABEL}.plist"
        self.patches = [patch.object(service, "PROJECT_ROOT", self.root),
                        patch.object(service, "USER_HOME", Path(self.temp.name) / "home"),
                        patch.object(service, "plist_path", return_value=self.path),
                        patch.object(service.sys, "platform", "darwin")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_existing(self, config=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(plistlib.dumps(config if config is not None else service.configuration()))

    def runtime(self):
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.touch()
        python.chmod(0o700)
        (self.root / "server.py").touch()

    @staticmethod
    def completed(code=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def test_configuration_roundtrip_has_only_fixed_project_command(self):
        config = plistlib.loads(service.serialized_configuration())
        self.assertEqual(config["Label"], "local.studydeadline.server")
        self.assertEqual(config["ProgramArguments"], [str((self.root / ".venv/bin/python").resolve()), str(self.root / "server.py"), "--lan"])
        self.assertEqual(config["WorkingDirectory"], str(service.USER_HOME))
        self.assertIs(config["RunAtLoad"], True)
        self.assertIs(config["KeepAlive"], True)
        self.assertEqual(config["ThrottleInterval"], 30)
        self.assertEqual(config["Umask"], 0o077)
        self.assertEqual(config["StandardOutPath"], str(service.USER_HOME / "Library/Logs/StudyDeadline/stdout.log"))
        self.assertEqual(config["StandardErrorPath"], str(service.USER_HOME / "Library/Logs/StudyDeadline/stderr.log"))
        self.assertNotIn("EnvironmentVariables", config)

    def test_default_does_not_touch_files_or_run_commands(self):
        with patch.object(service.subprocess, "run") as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(service.main([]), 0)
        run.assert_not_called()
        self.assertIn("不安装", out.getvalue())
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / "data").exists())

    def test_dry_run_lints_but_does_not_install(self):
        with patch.object(service.subprocess, "run", return_value=self.completed()) as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(service.main(["--install", "--dry-run"]), 0)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/plutil", "-lint", "-"])
        self.assertIn("plistlib: OK; plutil: OK", out.getvalue())
        self.assertIn("<key>ProgramArguments</key>", out.getvalue())
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / "data").exists())

    def test_refuses_other_project_without_running_launchctl(self):
        other = service.configuration()
        other["ProgramArguments"][1] = "/other/project/server.py"
        self.write_existing(other)
        before = self.path.read_bytes()
        self.runtime()
        with patch.object(service.subprocess, "run") as run:
            with self.assertRaises(service.ServiceError):
                service.install()
            with self.assertRaises(service.ServiceError):
                service.uninstall()
        run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_refuses_other_executable_label_program_or_extra_arguments(self):
        for change in ({"Label": "another.label"},
                       {"ProgramArguments": ["/bin/sh", str(self.root / "server.py")]},
                       {"ProgramArguments": service.configuration()["ProgramArguments"] + ["--extra"]},
                       {"Program": "/bin/sh"},
                       {"WorkingDirectory": "/other/project"}):
            with self.subTest(change=change):
                config = service.configuration()
                config.update(change)
                self.write_existing(config)
                with self.assertRaises(service.ServiceError):
                    service.existing_owned_configuration()

    def test_refuses_symlink_or_malformed_existing_plist(self):
        self.write_existing()
        target = self.path.with_suffix(".original")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(service.ServiceError):
            service.existing_owned_configuration()
        self.path.unlink()
        self.path.write_bytes(b"not a plist")
        with self.assertRaises(service.ServiceError):
            service.existing_owned_configuration()

    def test_status_outputs_only_safe_fields(self):
        raw = "\tstate = running\n\tpid = 1234\n\tenvironment = { SECRET_TOKEN => do-not-show }\n"
        with patch.object(service.subprocess, "run", return_value=self.completed(stdout=raw)) as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(service.main(["--status"]), 0)
        self.assertIn('"pid": 1234', out.getvalue())
        self.assertIn('"running": true', out.getvalue())
        self.assertNotIn("SECRET", out.getvalue())
        self.assertNotIn("do-not-show", out.getvalue())
        self.assertEqual(run.call_args.args[0], ["/bin/launchctl", "print", service.service_target()])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertFalse(self.path.exists())

    def test_status_error_does_not_echo_child_output(self):
        with patch.object(service.subprocess, "run", return_value=self.completed(code=1, stderr="SECRET_TOKEN=hidden")), redirect_stderr(io.StringIO()) as out:
            self.assertEqual(service.main(["--status"]), 1)
        self.assertNotIn("SECRET", out.getvalue())

    def test_launchctl_timeout_has_bounded_wait_and_safe_chinese_error(self):
        expired = service.subprocess.TimeoutExpired(["launchctl", "print"], 15,
                                                     output="SECRET_TOKEN=hidden", stderr="PRIVATE=value")
        with patch.object(service.subprocess, "run", side_effect=expired) as run, redirect_stderr(io.StringIO()) as out:
            self.assertEqual(service.main(["--status"]), 1)
        self.assertIn("超过 15 秒", out.getvalue())
        self.assertNotIn("SECRET", out.getvalue())
        self.assertNotIn("PRIVATE", out.getvalue())
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_missing_service_status(self):
        with patch.object(service, "_launchctl", return_value=self.completed(code=113)):
            self.assertEqual(service.read_status(), {"loaded": False, "running": False, "state": "not loaded", "pid": None})

    def test_unknown_state_is_not_echoed(self):
        status = service.parse_status("\tstate = SECRET VALUE\n\tpid = 123\n")
        self.assertEqual(status["state"], "unknown")
        self.assertFalse(status["running"])

    def test_install_creates_private_config_and_uses_launchctl_without_shell(self):
        self.runtime()
        loaded = {"loaded": True, "running": True, "state": "running", "pid": 77}
        with patch.object(service, "read_status", side_effect=[{"loaded": False}, loaded]), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service, "_launchctl", return_value=self.completed()) as run:
            self.assertEqual(service.install(), loaded)
        commands = [call.args for call in run.call_args_list]
        self.assertEqual(commands, [("enable", service.service_target()),
                                    ("bootstrap", f"gui/{os.getuid()}", str(self.path))])
        self.assertEqual(plistlib.loads(self.path.read_bytes()), service.configuration())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "data").stat().st_mode), 0o700)
        for log in ("stdout.log", "stderr.log"):
            self.assertEqual(stat.S_IMODE((service.USER_HOME / "Library/Logs/StudyDeadline" / log).stat().st_mode), 0o600)

    def test_install_does_not_take_over_loaded_unknown_service(self):
        self.runtime()
        with patch.object(service, "read_status", return_value={"loaded": True}), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service, "_launchctl") as run:
            with self.assertRaises(service.ServiceError):
                service.install()
        run.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / "data").exists())

    def test_log_symlink_is_not_followed(self):
        data = self.root / "data"
        data.mkdir()
        protected = self.root / "protected.txt"
        protected.write_text("untouched", encoding="utf-8")
        protected.chmod(0o644)
        log = service.USER_HOME / "Library/Logs/StudyDeadline/stdout.log"
        log.parent.mkdir(parents=True)
        log.symlink_to(protected)
        with self.assertRaises(service.ServiceError):
            service._prepare_private_data()
        self.assertEqual(protected.read_text(), "untouched")
        self.assertEqual(stat.S_IMODE(protected.stat().st_mode), 0o644)

    def test_configuration_changed_to_foreign_during_install_is_not_replaced(self):
        other = service.configuration()
        other["ProgramArguments"][1] = "/other/project/server.py"
        self.write_existing(other)
        before = self.path.read_bytes()
        with self.assertRaises(service.ServiceError):
            service._write_configuration(service.serialized_configuration())
        self.assertEqual(self.path.read_bytes(), before)

    def test_reinstall_stops_owned_service_before_bootstrapping(self):
        self.runtime()
        self.write_existing()
        with patch.object(service, "read_status", return_value={"loaded": True}), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service, "_launchctl", return_value=self.completed()) as run:
            service.install()
        self.assertEqual(run.call_args_list[0].args, ("bootout", service.service_target()))
        self.assertEqual(run.call_args_list[1].args, ("enable", service.service_target()))
        self.assertEqual(run.call_args_list[2].args[0], "bootstrap")
        self.assertFalse(any(call.args[0] == "kickstart" for call in run.call_args_list))

    def test_reload_retries_transient_bootstrap_io_error_only_while_unloaded(self):
        self.runtime()
        self.write_existing()
        loaded = {"loaded": True, "running": True, "state": "running", "pid": 77}
        with patch.object(service, "read_status", side_effect=[loaded, {"loaded": False}, loaded]), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service.time, "sleep") as sleep, \
                patch.object(service, "_launchctl", side_effect=[self.completed(), self.completed(),
                                                                self.completed(code=5), self.completed()]) as run:
            self.assertEqual(service.install(), loaded)
        sleep.assert_called_once_with(0.5)
        self.assertEqual([call.args[0] for call in run.call_args_list], ["bootout", "enable", "bootstrap", "bootstrap"])

    def test_reload_bootstrap_retry_is_bounded(self):
        self.runtime()
        self.write_existing()
        with patch.object(service, "read_status", side_effect=[{"loaded": True}] + [{"loaded": False}] * 3), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service.time, "sleep") as sleep, \
                patch.object(service, "_launchctl", side_effect=[self.completed(), self.completed()] + [self.completed(code=5)] * 4) as run:
            with self.assertRaisesRegex(service.ServiceError, "bootstrap.*5"):
                service.install()
        self.assertEqual([call.args for call in sleep.call_args_list], [(0.5,), (1,), (2,)])
        self.assertEqual(sum(call.args[0] == "bootstrap" for call in run.call_args_list), 4)

    def test_reload_does_not_retry_if_target_became_loaded(self):
        self.runtime()
        self.write_existing()
        with patch.object(service, "read_status", return_value={"loaded": True}), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service.time, "sleep"), \
                patch.object(service, "_launchctl", side_effect=[self.completed(), self.completed(), self.completed(code=5)]) as run:
            with self.assertRaisesRegex(service.ServiceError, "bootstrap.*5"):
                service.install()
        self.assertEqual(sum(call.args[0] == "bootstrap" for call in run.call_args_list), 1)

    def test_reload_revalidates_ownership_before_retry(self):
        self.runtime()
        self.write_existing()

        def command(*arguments):
            if arguments[0] == "bootstrap":
                foreign = service.configuration()
                foreign["ProgramArguments"][1] = "/other/project/server.py"
                self.write_existing(foreign)
                return self.completed(code=5)
            return self.completed()

        with patch.object(service, "read_status", return_value={"loaded": True}) as status, \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service.time, "sleep"), \
                patch.object(service, "_launchctl", side_effect=command) as run:
            with self.assertRaisesRegex(service.ServiceError, "不属于本项目"):
                service.install()
        self.assertEqual(status.call_count, 1)
        self.assertEqual(sum(call.args[0] == "bootstrap" for call in run.call_args_list), 1)

    def test_reload_non_io_bootstrap_error_is_not_retried(self):
        self.runtime()
        self.write_existing()
        with patch.object(service, "read_status", return_value={"loaded": True}), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service.time, "sleep") as sleep, \
                patch.object(service, "_launchctl", side_effect=[self.completed(), self.completed(), self.completed(code=1)]) as run:
            with self.assertRaisesRegex(service.ServiceError, "bootstrap.*1"):
                service.install()
        sleep.assert_not_called()
        self.assertEqual(sum(call.args[0] == "bootstrap" for call in run.call_args_list), 1)

    def test_fresh_install_io_bootstrap_error_is_not_retried(self):
        self.runtime()
        with patch.object(service, "read_status", return_value={"loaded": False}), \
                patch.object(service, "lint_configuration", return_value="OK"), \
                patch.object(service.time, "sleep") as sleep, \
                patch.object(service, "_launchctl", side_effect=[self.completed(), self.completed(code=5)]) as run:
            with self.assertRaisesRegex(service.ServiceError, "bootstrap.*5"):
                service.install()
        sleep.assert_not_called()
        self.assertEqual(sum(call.args[0] == "bootstrap" for call in run.call_args_list), 1)

    def test_uninstall_only_removes_owned_plist_and_preserves_data(self):
        self.write_existing()
        data = self.root / "data"
        data.mkdir()
        secret = data / "session.json"
        secret.write_text("keep this", encoding="utf-8")
        unrelated = self.path.parent / "other.plist"
        unrelated.write_bytes(b"keep this too")
        with patch.object(service, "_launchctl", return_value=self.completed()) as run:
            self.assertTrue(service.uninstall())
        run.assert_called_once_with("bootout", service.service_target())
        self.assertFalse(self.path.exists())
        self.assertEqual(secret.read_text(), "keep this")
        self.assertEqual(unrelated.read_bytes(), b"keep this too")

    def test_uninstall_without_owned_plist_does_not_stop_any_service(self):
        with patch.object(service, "_launchctl") as run:
            self.assertFalse(service.uninstall())
        run.assert_not_called()

    def test_uninstall_failure_preserves_configuration(self):
        self.write_existing()
        before = self.path.read_bytes()
        with patch.object(service, "_launchctl", return_value=self.completed(code=1)):
            with self.assertRaises(service.ServiceError):
                service.uninstall()
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
