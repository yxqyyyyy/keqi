import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from server import Application, Handler
from studyreminder.core import LEGACY_COURSE_SCOPE, Store, homework_year_bindings


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

    def test_multiple_year_bindings_preserve_existing_settings_and_override_only_same_identity(self):
        store = Store(self.path)
        legacy = {'year': 2026, 'courseid': '11', 'clazzid': '22'}
        english = {'year': 2026, 'courseid': '33', 'clazzid': '44'}
        store.patch_settings({'collection': {'homework_year_binding': legacy}})
        store.update_settings({'collection': {'homework_year_bindings': [english]}})
        self.assertEqual(homework_year_bindings(store.settings()['collection']), [legacy, english])
        store.update_settings({'collection': {'homework_year': 2027,
                                               'course_scope': {'chaoxing_homework': ['英语']}}})
        self.assertEqual(store.settings()['collection']['homework_year_bindings'], [english])
        self.assertEqual(Store(self.path).settings()['collection']['homework_year_bindings'], [english])
        store.patch_settings({'collection': {'homework_year_binding': legacy,
                                            'homework_year_bindings': [{**legacy, 'year': 2027}, english]}})
        self.assertEqual(homework_year_bindings(store.settings()['collection']), [{**legacy, 'year': 2027}, english])

    def test_invalid_multiple_year_bindings_do_not_write_partial_settings(self):
        store = Store(self.path)
        record = {'year': 2026, 'courseid': '11', 'clazzid': '22'}
        for values in (None, {}, [record, record], [{**record, 'year': True}],
                       [{**record, 'year': 2100}], [{**record, 'clazzid': ''}],
                       [{**record, 'courseid': 'course-a'}], [{**record, 'courseid': 11}]):
            before = store.settings()
            with self.subTest(values=values), self.assertRaises(ValueError):
                store.update_settings({'physics': {'enabled': True},
                                       'collection': {'homework_year_bindings': values}})
            self.assertEqual(store.settings(), before)

    def application(self):
        with patch('server.local_ip', return_value='127.0.0.1'):
            app = Application(self.temp.name)
        app.calendar = SimpleNamespace(sync_if_enabled=Mock(), info=Mock(return_value={}))
        app.collector = SimpleNamespace(request=Mock(return_value={'candidates': [], 'coverage_complete': True}),
                                        last_candidates=[], last_warnings=[], session_warning=None, status={})
        return app

    def unresolved(self, **changes):
        return {'course': '英美现代戏剧原著赏读', 'title': '练习', 'courseid': '33', 'clazzid': '44',
                'source': 'chaoxing', 'status': 'pending',
                'source_key': 'course:33:class:44:work:55', 'reason': 'missing_year',
                'time_text': '作答时间:11-13 15:18 至 11-26 15:19',
                'year_confirmation_available': True, **changes}

    def save_unresolved(self, app, rows):
        app.store.set_value('last_collection_result', {'unresolved_homework': rows,
            'unresolved_homework_verification': {'auth_status': 'authenticated', 'auth_generation': 0,
                                               'read_at': '2026-11-24T23:15:00+08:00'}})

    def test_confirm_year_requires_observed_exact_work_and_preserves_legacy_binding(self):
        app = self.application()
        legacy = {'year': 2026, 'courseid': '11', 'clazzid': '22'}
        app.store.patch_settings({'collection': {'homework_year_binding': legacy}})
        self.save_unresolved(app, [self.unresolved()])
        app.store.set_value('daily_last_success', '2026-11-24T08:00:00+08:00')
        binding = {'year': 2026, 'courseid': '33', 'clazzid': '44'}
        result = app.confirm_homework_year(binding)
        self.assertTrue(result['ok'])
        self.assertFalse(result['needs_scan'])
        self.assertEqual(result['imported'], 1)
        self.assertEqual(homework_year_bindings(app.store.settings()['collection']), [legacy, binding])
        self.assertEqual(app.store.settings()['collection']['homework_year_binding'], legacy)
        self.assertIsNone(app.store.get_value('daily_last_success'))
        app.collector.request.assert_not_called()
        task = app.store.tasks()[0]
        self.assertEqual((task['source'], task['source_key'], task['status']),
                         ('chaoxing', 'course:33:class:44:work:55', 'pending'))
        self.assertEqual(task['due_at'], '2026-11-26T15:19:00+08:00')
        self.assertEqual(task['start_at'], '2026-11-13T15:18:00+08:00')
        self.assertIn('用户确认 2026 年', task['notes'])
        self.assertIn('2026-11-24T23:15:00+08:00', task['notes'])
        self.assertEqual(app.state()['collection']['unresolved_homework'], [])
        app.calendar.sync_if_enabled.assert_called_once()
        self.assertTrue(app.cloud.wake.is_set())
        # Reconfirmation replaces only this course's year, without duplicates.
        self.save_unresolved(app, [self.unresolved()])
        app.confirm_homework_year({**binding, 'year': 2027})
        self.assertEqual(homework_year_bindings(app.store.settings()['collection']), [legacy, {**binding, 'year': 2027}])

    def test_confirm_year_rejects_missing_conflicted_or_unproven_work(self):
        app = self.application()
        binding = {'year': 2026, 'courseid': '33', 'clazzid': '44'}
        for row in (None, self.unresolved(reason='unknown_deadline_or_conflict'),
                    self.unresolved(year_confirmation_available=False), self.unresolved(source_key=''),
                    self.unresolved(source_key='course:11:class:44:work:55'),
                    self.unresolved(clazzid='other-class')):
            self.save_unresolved(app, [row] if row else [])
            with self.subTest(row=row), self.assertRaises(ValueError):
                app.confirm_homework_year(binding)
        self.save_unresolved(app, [self.unresolved()])
        for bad in ({**binding, 'year': True}, {**binding, 'year': '2026'},
                    {**binding, 'year': 2100}, {**binding, 'courseid': ''}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.confirm_homework_year(bad)
        self.assertEqual(app.store.settings()['collection']['homework_year_bindings'], [])

    def test_confirm_year_checks_actual_short_date_against_year(self):
        app = self.application()
        self.save_unresolved(app, [self.unresolved(time_text='作答时间:02-28 15:18 至 02-29 15:19')])
        with self.assertRaisesRegex(ValueError, '日期不匹配'):
            app.confirm_homework_year({'year': 2026, 'courseid': '33', 'clazzid': '44'})
        self.assertTrue(app.confirm_homework_year({'year': 2028, 'courseid': '33', 'clazzid': '44'})['ok'])

    def test_confirm_year_keeps_existing_completed_work_and_updates_calendar_without_platform_reads(self):
        app = self.application()
        old = app.store.import_tasks([{'title': '练习', 'course': '英美现代戏剧原著赏读',
            'source': 'chaoxing', 'source_key': 'course:33:class:44:work:55', 'status': 'completed',
            'start_at': '2026-11-13T15:18:00+08:00', 'due_at': '2026-11-26T15:19:00+08:00'}])[0]
        self.save_unresolved(app, [self.unresolved()])
        app.cloud.wake.clear()
        result = app.confirm_homework_year({'year': 2026, 'courseid': '33', 'clazzid': '44'})
        task = app.store.get_task(old['id'])
        self.assertEqual((task['id'], task['status']), (old['id'], 'completed'))
        self.assertEqual(result['imported'], 1)
        self.assertFalse(result['needs_scan'])
        app.calendar.sync_if_enabled.assert_called_once()
        self.assertTrue(app.cloud.wake.is_set())
        app.collector.request.assert_not_called()

    def test_scan_passes_multiple_bindings_without_current_year_inference(self):
        app = self.application()
        values = [{'year': 2025, 'courseid': '11', 'clazzid': '22'},
                  {'year': 2026, 'courseid': '33', 'clazzid': '44'}]
        app.store.patch_settings({'collection': {'homework_year_bindings': values}})
        app.scan()
        self.assertEqual(app.collector.request.call_args.args[1]['homework_year'], values)

        def change_during_scan(*args):
            app.store.patch_settings({'collection': {'homework_year_bindings': values[:1]}})
            return {'candidates': [], 'coverage_complete': True}
        app.collector.request.side_effect = change_during_scan
        with self.assertRaisesRegex(ValueError, '课程设置已改变'):
            app.scan()

    def test_unresolved_homework_survives_restart_and_unrelated_source_scan(self):
        app = self.application()
        unresolved = [self.unresolved()]
        self.save_unresolved(app, unresolved)
        self.assertEqual(self.application().state()['collection']['unresolved_homework'], unresolved)
        app.scan(sources={'xiji'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], unresolved)
        self.assertEqual(app.state()['collection']['unresolved_homework_read_at'], '2026-11-24T23:15:00+08:00')

    def test_confirm_year_requires_authenticated_observation_of_current_session(self):
        app = self.application()
        binding = {'year': 2026, 'courseid': '33', 'clazzid': '44'}
        app.store.set_value('last_collection_result', {'unresolved_homework': [self.unresolved()]})
        with self.assertRaisesRegex(ValueError, '登录会话尚未验证'):
            app.confirm_homework_year(binding)
        self.save_unresolved(app, [self.unresolved()])
        retry = app.store.get_value('collection_retry', {})
        retry['auth_generations'] = {'chaoxing': 1}
        app.store.set_value('collection_retry', retry)
        with self.assertRaisesRegex(ValueError, '登录会话尚未验证'):
            app.confirm_homework_year(binding)

    def test_temporary_failure_retains_unresolved_until_authenticated_read_clears_it(self):
        app = self.application()
        unresolved = [self.unresolved()]
        self.save_unresolved(app, unresolved)
        app.collector.request.return_value = {'candidates': [], 'coverage_complete': False,
            'source_results': {'chaoxing': {'auth_status': 'error', 'coverage_complete': False}}}
        app.scan(sources={'chaoxing'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], unresolved)
        app.collector.request.return_value = {'candidates': [], 'unresolved_homework': [], 'coverage_complete': True,
            'source_results': {'chaoxing': {'auth_status': 'authenticated', 'coverage_complete': True}}}
        app.scan(sources={'chaoxing'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], [])

    def test_authenticated_partial_scan_retains_unread_work_without_allowing_year_confirmation(self):
        app = self.application()
        old = self.unresolved()
        self.save_unresolved(app, [old])
        app.collector.request.return_value = {'candidates': [], 'unresolved_homework': [], 'coverage_complete': False,
            'source_results': {'chaoxing': {'auth_status': 'authenticated', 'coverage_complete': False}}}
        app.scan(sources={'chaoxing'})
        unread = app.state()['collection']['unresolved_homework']
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]['time_text'], old['time_text'])
        self.assertEqual(unread[0]['reason'], 'not_rechecked')
        self.assertFalse(unread[0]['year_confirmation_available'])
        with self.assertRaisesRegex(ValueError, '未找到可确认年份'):
            app.confirm_homework_year({'year': 2026, 'courseid': '33', 'clazzid': '44'})

        # A fresh observation of the same work replaces the older placeholder.
        app.collector.request.return_value['unresolved_homework'] = [old]
        app.scan(sources={'chaoxing'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], [old])
        self.assertTrue(app.confirm_homework_year({'year': 2026, 'courseid': '33', 'clazzid': '44'})['ok'])

    def test_partial_scan_removes_only_work_resolved_by_verified_metadata_or_completion(self):
        app = self.application()
        old = [self.unresolved(), self.unresolved(source_key='course:33:class:44:work:56', title='已交练习'),
               self.unresolved(source_key='course:33:class:44:work:57', title='只知道未交'),
               self.unresolved(source_key='course:33:class:44:work:58', title='其他来源同名编号')]
        self.save_unresolved(app, old)
        app.collector.request.return_value = {'coverage_complete': False, 'unresolved_homework': [],
            'source_results': {'chaoxing': {'auth_status': 'authenticated', 'coverage_complete': False}},
            'candidates': [
                {'source': 'chaoxing', 'source_key': old[0]['source_key'], 'status': 'pending'},
                {'source': 'xiji', 'source_key': old[3]['source_key'], 'status': 'pending'}],
            'status_updates': [
                {'source': 'chaoxing', 'source_key': old[1]['source_key'], 'status': 'completed'},
                {'source': 'chaoxing', 'source_key': old[2]['source_key'], 'status': 'pending'}]}
        app.scan(sources={'chaoxing'})
        unread = app.state()['collection']['unresolved_homework']
        self.assertEqual({item['source_key'] for item in unread}, {old[2]['source_key'], old[3]['source_key']})
        self.assertTrue(all(item['reason'] == 'not_rechecked' and not item['year_confirmation_available'] for item in unread))

    def test_partial_scan_deduplicates_unidentified_rows_and_never_merges_another_session(self):
        app = self.application()
        old = self.unresolved(source_key='', reason='missing_id', year_confirmation_available=False)
        self.save_unresolved(app, [old, old])
        fresh = {**old, 'reason': 'missing_detail_link'}
        result = {'candidates': [], 'unresolved_homework': [fresh], 'coverage_complete': False,
            'source_results': {'chaoxing': {'auth_status': 'authenticated', 'coverage_complete': False}}}
        app.collector.request.return_value = result
        app.scan(sources={'chaoxing'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], [fresh])
        retry = app.store.get_value('collection_retry', {})
        retry['auth_generations'] = {'chaoxing': 1}
        app.store.set_value('collection_retry', retry)
        app.collector.request.return_value = {**result, 'unresolved_homework': []}
        app.scan(sources={'chaoxing'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], [])

    def test_binding_change_during_scan_keeps_new_task_and_other_unresolved_work(self):
        app = self.application()
        old = self.unresolved()
        other = self.unresolved(courseid='66', clazzid='77', source_key='course:66:class:77:work:88')
        self.save_unresolved(app, [old, other])
        binding = {'year': 2026, 'courseid': '33', 'clazzid': '44'}
        def request(*args):
            app.confirm_homework_year(binding)
            return {'candidates': [], 'unresolved_homework': [], 'coverage_complete': False,
                'source_results': {'chaoxing': {'auth_status': 'authenticated', 'coverage_complete': False}}}
        app.collector.request.side_effect = request
        with self.assertRaisesRegex(ValueError, '课程设置已改变'):
            app.scan(sources={'chaoxing'})
        self.assertEqual(app.state()['collection']['unresolved_homework'], [other])
        self.assertIn(binding, app.store.settings()['collection']['homework_year_bindings'])
        self.assertEqual(app.store.tasks()[0]['source_key'], old['source_key'])

    def test_confirm_year_http_action_saves_without_slow_scan(self):
        app = self.application()
        self.save_unresolved(app, [self.unresolved()])
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(app=app)
        handler.path = '/api/collection/confirm-year'
        handler.command = 'POST'
        handler.authorized = Mock(return_value=True)
        handler.read_json = Mock(return_value={'year': 2026, 'courseid': '33', 'clazzid': '44'})
        handler.send = Mock()
        handler.mutate()
        self.assertEqual(handler.send.call_args.args[0], 200)
        self.assertFalse(handler.send.call_args.args[1]['needs_scan'])
        app.collector.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
