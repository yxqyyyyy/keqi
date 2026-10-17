import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import Application
from studyreminder.core import LEGACY_COURSE_SCOPE, Store


class CourseSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tasks.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def test_new_install_is_empty_and_stays_empty_after_reopen(self):
        store = Store(self.path)
        self.assertEqual(store.tasks(), [])
        self.assertFalse(store.settings()['physics']['enabled'])
        self.assertTrue(all(v == [] for v in store.settings()['collection']['course_scope'].values()))
        self.assertEqual(Store(self.path).settings(), store.settings())

    def test_legacy_migration_preserves_implicit_courses_and_physics(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE kv (key TEXT PRIMARY KEY, body TEXT NOT NULL);')
            db.execute('INSERT INTO kv VALUES (?, ?)', ('settings', json.dumps({'collection': {'homework_year': 2026}})))
        cfg = Store(self.path).settings()
        self.assertEqual(cfg['collection']['course_scope'], LEGACY_COURSE_SCOPE)
        self.assertEqual(cfg['collection']['homework_year'], 2026)
        self.assertTrue(cfg['physics']['enabled'])
        self.assertEqual(Store(self.path).settings(), cfg)

    def test_migration_keeps_explicitly_disabled_physics(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE kv (key TEXT PRIMARY KEY, body TEXT NOT NULL);')
            db.execute('INSERT INTO kv VALUES (?, ?)', ('settings', '{"physics":{"enabled":false}}'))
        self.assertFalse(Store(self.path).settings()['physics']['enabled'])

    def test_course_scope_is_normalized_independent_and_partial(self):
        first = Store(self.path)
        first.update_settings({'collection': {'course_scope': {'chaoxing_homework': [' 高等数学 ', '高等数学'], 'xiji': ['程序设计']}}})
        self.assertEqual(first.settings()['collection']['course_scope']['chaoxing_homework'], ['高等数学'])
        self.assertEqual(first.settings()['collection']['course_scope']['chaoxing_exams'], [])
        other = Store(Path(self.temp.name) / 'other.sqlite3')
        self.assertEqual(other.settings()['collection']['course_scope']['xiji'], [])

    def test_scope_and_year_changes_clear_binding_and_daily_success(self):
        store = Store(self.path)
        store.patch_settings({'collection': {'homework_year_binding': {'year': 2026, 'course': 'a', 'class': 'b'}}})
        store.set_value('daily_last_success', '2026-10-16T08:00:00+08:00')
        store.update_settings({'collection': {'course_scope': {'chaoxing_homework': ['概率论']}, 'homework_year': 2026}})
        self.assertIsNone(store.settings()['collection']['homework_year_binding'])
        self.assertIsNone(store.get_value('daily_last_success'))

    def test_invalid_scope_or_year_leaves_all_settings_unchanged(self):
        store = Store(self.path)
        before = store.settings()
        for invalid in ({'course_scope': []}, {'course_scope': {'bogus': []}},
                        {'course_scope': {'xiji': '课程'}}, {'course_scope': {'xiji': ['']}},
                        {'course_scope': {'xiji': ['A'] * 51}}, {'course_scope': {'xiji': [7]}},
                        {'homework_year': True}, {'homework_year': '2026'}, {'homework_year': 1999}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                store.update_settings({'physics': {'enabled': True}, 'collection': invalid})
            self.assertEqual(store.settings(), before)

    def test_scan_passes_scope_and_does_not_save_binding_after_scope_changes(self):
        with patch('server.local_ip', return_value='127.0.0.1'):
            app = Application(self.temp.name)
        scope = {'chaoxing_homework': ['课程甲'], 'chaoxing_exams': [], 'xiji': []}
        app.store.update_settings({'collection': {'course_scope': scope}})
        request = Mock(return_value={'candidates': [], 'coverage_complete': True})
        app.collector = SimpleNamespace(request=request)
        app.scan()
        self.assertEqual(request.call_args.args[1]['course_scope'], scope)

        def change_during_scan(*args):
            app.store.update_settings({'collection': {'course_scope': {'chaoxing_homework': ['课程乙']}}})
            return {'candidates': [], 'homework_year_binding': {'year': 2026, 'course': 'old'}}

        app.collector = SimpleNamespace(request=change_during_scan)
        with self.assertRaisesRegex(ValueError, '课程设置已改变'):
            app.scan()
        self.assertIsNone(app.store.settings()['collection'].get('homework_year_binding'))


if __name__ == '__main__':
    unittest.main()
