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
        self.assertEqual(store.settings()['collection']['all_course_types'], [])
        self.assertEqual(Store(self.path).settings(), store.settings())

    def test_legacy_migration_preserves_implicit_courses_and_physics(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('CREATE TABLE kv (key TEXT PRIMARY KEY, body TEXT NOT NULL);')
            db.execute('INSERT INTO kv VALUES (?, ?)', ('settings', json.dumps({'collection': {'homework_year': 2026}})))
        cfg = Store(self.path).settings()
        self.assertEqual(cfg['collection']['course_scope'], LEGACY_COURSE_SCOPE)
        self.assertEqual(cfg['collection']['homework_year'], 2026)
        self.assertTrue(cfg['physics']['enabled'])
        self.assertEqual(cfg['collection']['all_course_types'], [])
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

    def test_all_course_modes_preserve_keywords_and_proven_year_binding(self):
        store = Store(self.path)
        scope = {'chaoxing_homework': ['概率论'], 'chaoxing_exams': ['大学物理'], 'xiji': ['离散数学']}
        binding = {'year': 2026, 'courseid': 'course-a', 'clazzid': 'class-a'}
        store.patch_settings({'collection': {'course_scope': scope, 'homework_year': 2026,
                                            'homework_year_binding': binding}})
        store.set_value('daily_last_success', '2026-11-20T08:00:00+08:00')
        cfg = store.update_settings({'collection': {'all_course_types': ['xiji', 'chaoxing_homework']}})['collection']
        self.assertEqual(cfg['all_course_types'], ['chaoxing_homework', 'xiji'])
        self.assertEqual(cfg['course_scope'], scope)
        self.assertEqual(cfg['homework_year_binding'], binding)
        self.assertIsNone(store.get_value('daily_last_success'))
        self.assertEqual(Store(self.path).settings()['collection']['all_course_types'], cfg['all_course_types'])
        self.assertEqual(Store(Path(self.temp.name) / 'other.sqlite3').settings()['collection']['all_course_types'], [])
        # Changing another setting must not drop enabled all-course categories.
        store.update_settings({'collection': {'auto_scan': True}})
        self.assertEqual(store.settings()['collection']['all_course_types'], cfg['all_course_types'])
        store.update_settings({'collection': {'all_course_types': []}})
        self.assertEqual(store.settings()['collection']['course_scope'], scope)
        self.assertEqual(store.settings()['collection']['homework_year_binding'], binding)

    def test_invalid_all_course_modes_leave_all_settings_unchanged(self):
        store = Store(self.path)
        before = store.settings()
        for invalid in (None, True, 'xiji', {}, ['unknown'], [7], [[]],
                        ['xiji', 'xiji'], ['chaoxing_homework', 'chaoxing_exams', 'xiji', 'xiji']):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                store.update_settings({'physics': {'enabled': True}, 'collection': {'all_course_types': invalid}})
            self.assertEqual(store.settings(), before)

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
