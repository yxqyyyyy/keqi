import io
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import package_release as release


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "source"
        self.root.mkdir()
        self.write("README.md", "# 课期\n")
        self.write("server.py", "print('ready')\n")
        self.write(".gitignore", "data/\ndist/\n")
        self.write("scripts/package_release.py", "# release tool\n")
        self.write("studyreminder/core.py", "NAME = '课期'\n")
        self.write("web/widgets/widget.js", "// widget\n")
        self.write("启动作业提醒.command", "#!/bin/sh\nexit 0\n")
        (self.root / "启动作业提醒.command").chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_allowlist_never_reads_private_directories_or_unknown_types(self):
        excluded = [
            "data/browser-session.json", "data/homework.db", "build/native.swift",
            ".venv/private.py", ".git/config", "dist/old.zip", "README-backup.md",
            "scripts/export.py", "tests/cache.pyc", "studyreminder/.secret.py",
            "studyreminder/__pycache__/cached.py", "web/data/private.js",
            "web/.cache/private.js", "web/cookies.js", "web/settings.json",
            "widgets/account.json", "native/secret.plist", "docs/archive/private.md",
        ]
        for name in excluded:
            self.write(name, "must never be published\n")
        original_iterdir = Path.iterdir

        def checked_iterdir(path):
            relative = path.relative_to(self.root)
            self.assertFalse(any(part in {"data", "build", ".venv", ".git", "dist", "__pycache__", ".cache"} for part in relative.parts))
            return original_iterdir(path)

        with patch.object(Path, "iterdir", checked_iterdir):
            sources = release.collect_sources(self.root)
        self.assertTrue(set(excluded).isdisjoint(sources))
        self.assertEqual(len(sources), 7)

    def test_archive_extracts_with_one_root_and_matches_source_bytes_and_modes(self):
        sources = release.collect_sources(self.root)
        output = self.root / "dist" / "keqi-source.zip"
        self.assertEqual(release.build_archive(self.root, output, sources), len(sources))
        release.audit_archive(output, sources)
        extracted = self.root.parent / "extracted"
        with zipfile.ZipFile(output) as archive:
            self.assertTrue(all(name.startswith("keqi/") for name in archive.namelist()))
            self.assertEqual((archive.getinfo("keqi/启动作业提醒.command").external_attr >> 16) & 0o777, 0o755)
            archive.extractall(extracted)
        for name, source in sources.items():
            self.assertEqual((extracted / "keqi" / name).read_bytes(), source.content)

    def test_allowed_source_symlink_is_rejected_without_reading_target(self):
        private = self.write("data/private.py", "private data")
        (self.root / "studyreminder" / "linked.py").symlink_to(private)
        with self.assertRaisesRegex(release.ReleaseError, "符号链接"):
            release.collect_sources(self.root)

    def test_source_directory_symlink_is_rejected(self):
        directory = self.root / "docs"
        private = self.root / "data"
        private.mkdir()
        directory.symlink_to(private, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "符号链接"):
            release.collect_sources(self.root)

    def test_unlisted_private_symlink_is_ignored(self):
        (self.root / "data").symlink_to(self.root.parent / "nonexistent", target_is_directory=True)
        self.assertEqual(len(release.collect_sources(self.root)), 7)

    def test_personal_absolute_path_blocks_with_only_file_and_line(self):
        personal = "/" + "Users/" + "release-person/Desktop/private"
        self.write("README.md", "# title\n" + personal)
        with self.assertRaises(release.ReleaseError) as raised:
            release.collect_sources(self.root)
        self.assertIn("README.md:2", str(raised.exception))
        self.assertNotIn(personal, str(raised.exception))

    def test_plausible_token_blocks_even_in_tests_and_never_echoes_value(self):
        token = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        self.write("tests/test_login.py", "VALUE = '" + token + "'\n")
        with self.assertRaises(release.ReleaseError) as raised:
            release.collect_sources(self.root)
        self.assertIn("tests/test_login.py:1", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))

    def test_obvious_fake_tokens_do_not_block_tests_or_documentation(self):
        self.write("tests/test_login.py", "VALUE = '" + "gh" + "p_" + "a" * 36 + "'\n")
        self.write("docs/example.md", "gh" + "p_" + "EXAMPLE" * 6)
        self.assertIn("tests/test_login.py", release.collect_sources(self.root))

    def test_actual_archive_is_audited_for_unexpected_paths_and_content_changes(self):
        sources = release.collect_sources(self.root)
        output = self.root / "dist" / "keqi-source.zip"
        release.build_archive(self.root, output, sources)
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr("keqi/data/private.json", "private")
        with self.assertRaises(release.ReleaseError):
            release.audit_archive(output, sources)
        with zipfile.ZipFile(output, "w") as archive:
            for name, source in sources.items():
                archive.writestr("keqi/" + name, b"tampered" if name == "README.md" else source.content)
        with self.assertRaises(release.ReleaseError):
            release.audit_archive(output, sources)

    def test_check_lists_only_sources_and_creates_nothing(self):
        with patch.object(release, "PROJECT_ROOT", self.root), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release.main(["--check"]), 0)
        self.assertIn("7 个源码文件", output.getvalue())
        self.assertFalse((self.root / "dist").exists())

    def test_output_symlink_is_rejected_without_touching_target(self):
        target = self.root.parent / "keep.zip"
        target.write_bytes(b"keep")
        output = self.root / "release.zip"
        output.symlink_to(target)
        with self.assertRaises(release.ReleaseError):
            release.build_archive(self.root, output)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_cli_failure_does_not_print_secret(self):
        personal = "/" + "home/" + "release-person/private"
        self.write("README.md", personal)
        with patch.object(release, "PROJECT_ROOT", self.root), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(release.main(["--check"]), 1)
        self.assertNotIn(personal, error.getvalue())
        self.assertIn("README.md:1", error.getvalue())

    def test_iphone_sources_allow_only_engineering_files_and_exclude_local_state(self):
        allowed = ["ios/README.md", "ios/KeqiAlarm/Info.plist", "ios/KeqiAlarm/ContentView.swift",
                   "ios/KeqiAlarm.xcodeproj/project.pbxproj",
                   "ios/KeqiAlarm.xcodeproj/xcshareddata/xcschemes/KeqiAlarm.xcscheme",
                   "ios/AlarmCore/Package.swift", "ios/AlarmCore/Sources/KeqiAlarmCore/Models.swift",
                   "ios/AlarmCore/Sources/AlarmCoreChecks/main.swift",
                   "ios/AlarmCore/Tests/KeqiAlarmCoreTests/PlannerTests.swift"]
        excluded = ["ios/AlarmCore/.build/cache.swift", "ios/AlarmCore/.swiftpm/configuration.json",
                    "ios/KeqiAlarm/managed-alarms.json", "ios/KeqiAlarm/secret.swift",
                    "ios/KeqiAlarm.xcodeproj/xcuserdata/name.xcuserdatad/UserInterfaceState.xcuserstate",
                    "ios/Personal.swift", "ios/private/backup.swift"]
        for name in allowed + excluded:
            self.write(name, "fixture\n")
        sources = release.collect_sources(self.root)
        self.assertTrue(set(allowed).issubset(sources))
        self.assertTrue(set(excluded).isdisjoint(sources))

    def test_iphone_nested_parent_symlink_is_rejected(self):
        private = self.root / "private-source"
        private.mkdir()
        (private / "Package.swift").write_text("must not open\n")
        (self.root / "ios").mkdir()
        (self.root / "ios" / "AlarmCore").symlink_to(private, target_is_directory=True)
        with self.assertRaisesRegex(release.ReleaseError, "符号链接"):
            release.collect_sources(self.root)


if __name__ == "__main__":
    unittest.main()
