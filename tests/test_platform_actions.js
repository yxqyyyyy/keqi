// Exercise the real platform rendering/action functions without platform accounts or a browser.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert(from >= 0 && to > from);
  return source.slice(from, to);
}
const elements = new Map();
const sandbox = {
  state: {settings: {}, collection: {}, platforms: {}},
  $: key => {
    if (!elements.has(key)) elements.set(key, {});
    return elements.get(key);
  },
  sourceName: value => value,
  syncTime: () => '尚无记录',
  syncResultLabel: () => '待检查',
  escapeHTML: value => String(value),
};
vm.createContext(sandbox);
vm.runInContext('const pendingPlatformActions = new Map(); const pendingYearConfirmations = new Set();\n' +
  section('function renderPlatforms()', 'function renderSettings()') +
  section('async function platformAction(', 'async function connectPlatform('), sandbox);
const html = () => elements.get('#platformList').innerHTML;
const card = name => html().split('<article class="platform-card">').find(value => value.includes(`<h3>${name}</h3>`));
(async () => {
  let calls = 0, release;
  const deferred = new Promise(resolve => {release = resolve;});
  const running = sandbox.platformAction('xiji', 'open', () => {calls++; return deferred;});
  assert.equal((card('xiji').match(/ disabled/g) || []).length, 3);
  assert(card('xiji').includes('正在打开窗口'));
  assert(!card('chaoxing').includes(' disabled'));
  // A refresh rebuilds the buttons while the original action remains pending.
  sandbox.renderPlatforms();
  assert.equal((card('xiji').match(/ disabled/g) || []).length, 3);
  await sandbox.platformAction('xiji', 'probe', () => {calls++;});
  assert.equal(calls, 1, 'duplicate clicks must not enqueue another request after a re-render');
  release();
  await running;
  assert(!card('xiji').includes(' disabled'));
  await assert.rejects(sandbox.platformAction('chaoxing', 'probe', async () => {throw Error('offline');}), /offline/);
  assert(!card('chaoxing').includes(' disabled'), 'failed actions must release the rendered controls');
  sandbox.state.platforms.xiji = {status: 'error', message: '请求超时'};
  sandbox.renderPlatforms();
  assert(card('xiji').includes('验证未完成'));
  assert(card('xiji').includes('尚不能判断会话是否有效'));
  assert(!card('xiji').includes('登录已失效'));
  sandbox.state.platforms.chaoxing = {status: 'verification_required'};
  sandbox.state.collection.automatic_retry = {blocked_sources: {chaoxing: {reason: 'verification_required'}}};
  sandbox.renderPlatforms();
  assert(card('chaoxing').includes('需要本人完成平台验证'));
  assert(!card('chaoxing').includes('登录已失效'));
  assert(elements.get('#collectionModeMessage').textContent.includes('平台要求验证码，自动读取已暂停'));
  assert(!elements.get('#collectionModeMessage').textContent.includes('登录已失效'));
  console.log('Platform action refresh, duplicate-click, failure recovery and unknown-login checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});

// Exercise the rendered agenda so an exam's closing time cannot drive its countdown or day.
let scenarioNow = Date.parse('2026-11-14T08:00:00+08:00');
class ScenarioDate extends Date {
  constructor(...values) { super(...(values.length ? values : [scenarioNow])); }
  static now() { return scenarioNow; }
}
const timingElements = new Map();
const timing = {
  Date: ScenarioDate,
  Intl,
  state: {tasks: [], settings: {}, platforms: {}},
  filters: {status: 'pending', source: 'all', kind: 'all', search: '', day: null},
  weekStart: '2026-11-09',
  sourceName: value => value,
  safeURL: () => null,
  renderPlatforms() {}, renderSettings() {}, renderSyncTimes() {},
  $: key => {
    if (!timingElements.has(key)) timingElements.set(key, {addEventListener() {}});
    return timingElements.get(key);
  },
};
vm.createContext(timing);
vm.runInContext("const ZONE = 'Asia/Shanghai'; let lastCollectionScan = null;\n" +
  section('function escapeHTML(', 'function syncTime(') +
  section('function shortDate(', 'function sourceName(') +
  section('function render() {', 'function syncFilterControls()'), timing);
const exam = {id: 'exam', title: '考试', kind: 'exam', source: 'manual', status: 'pending',
  start_at: '2026-11-14T09:00:00+08:00', due_at: '2026-11-14T11:30:00+08:00'};
assert.equal(timing.countdown(exam).text, '剩余 1 小时');
assert(timing.taskRow(exam).includes('开考 09:00'));
assert(timing.taskRow(exam).includes('结束 11/14 11:30'));
scenarioNow = Date.parse('2026-11-14T10:00:00+08:00');
assert.equal(timing.countdown(exam).text, '考试中');
scenarioNow = Date.parse('2026-11-14T12:00:00+08:00');
assert.equal(timing.countdown(exam).text, '已结束');
assert.equal(timing.countdown(exam).urgent, false);
assert.equal(timing.isActionable(exam), false);
for (const start_at of [null, '', 'invalid', '2026-11-14T12:00:00+08:00']) {
  assert.equal(timing.countdown({...exam, start_at}).text, '开考时间待补充');
}
assert.equal(timing.countdown({...exam, status: 'completed'}).text, '已完成');
scenarioNow = Date.parse('2026-11-13T22:00:00+08:00');
const overnight = {...exam, start_at: '2026-11-13T23:30:00+08:00', due_at: '2026-11-14T01:00:00+08:00'};
const assignment = {id: 'assignment', title: '作业', kind: 'assignment', source: 'manual', status: 'pending', due_at: '2026-11-14T00:00:00+08:00'};
timing.state.tasks = [assignment, overnight];
timing.render();
let agenda = timingElements.get('#taskList').innerHTML;
assert(agenda.indexOf('data-edit="exam"') < agenda.indexOf('data-edit="assignment"'));
timing.filters.day = '2026-11-13';
timing.renderTasks();
agenda = timingElements.get('#taskList').innerHTML;
assert(agenda.includes('data-edit="exam"'));
assert(!agenda.includes('data-edit="assignment"'));
timing.filters.day = null;
scenarioNow = Date.parse('2026-11-14T08:00:00+08:00');
timing.state.tasks = [{...exam, start_at: null}, {...exam, start_at: '2026-11-13T09:00:00+08:00'}];
timing.render();
assert(timingElements.get('#soonCount').innerHTML.startsWith('0'));
assert(timingElements.get('#agendaSummary').textContent.includes('1 场考试待补充开考时间'));
assert(timingElements.get('#agendaSummary').textContent.includes('1 场考试进行中'));
// Historical attendance records remain queryable without becoming overdue work.
scenarioNow = Date.parse('2026-11-15T10:00:00+08:00');
const pastLecture = {id: 'past-lecture', title: '过去的上课安排', kind: 'lecture', source: 'manual', status: 'pending', due_at: '2026-11-14T20:00:00+08:00'};
const pastExam = {...exam, title: '过去的考试'};
const oldWork = {...assignment, title: '未交作业'};
const doneWork = {...assignment, id: 'done', status: 'completed'};
assert.equal(timing.countdown(pastLecture).urgent, false);
assert.equal(timing.countdown(pastLecture).text, '已开课');
assert.equal(timing.isOverdueAssignment(pastLecture), false);
assert.equal(timing.isOverdueAssignment(pastExam), false);
assert.equal(timing.isOverdueAssignment(oldWork), true);
assert.equal(timing.isOverdueAssignment(doneWork), false);
timing.state.tasks = [pastLecture, pastExam, oldWork, doneWork];
timing.render();
assert.equal(timingElements.get('#pendingCount').textContent, 1);
assert(timingElements.get('#agendaSummary').textContent.includes('1 项作业已逾期'));
agenda = timingElements.get('#taskList').innerHTML;
assert(agenda.includes('未交作业'));
assert(!agenda.includes('过去的上课安排'));
assert(!agenda.includes('过去的考试'));
timing.filters.status = 'all';
timing.state.tasks = [pastLecture, pastExam];
timing.render();
agenda = timingElements.get('#taskList').innerHTML;
assert(agenda.includes('过去的上课安排'));
assert(agenda.includes('过去的考试'));
assert(!agenda.includes('group-heading overdue'));
assert(!agenda.includes('task-countdown urgent'));
assert(!timingElements.get('#agendaSummary').textContent.includes('已完成'));
assert.equal(pastLecture.status, 'pending');
assert.equal(pastExam.status, 'pending');
scenarioNow = Date.parse('2026-11-14T10:00:00+08:00');
assert.equal(timing.isActionable(exam), true);
console.log('Exam start countdown, overnight grouping, missing-start and agenda summary checks passed');

// Phone writes must become visible even while the desktop search keeps focus.
const refreshEvents = { document: new Map(), window: new Map() };
let refreshCalls = 0, dialogOpen = false, refreshInterval;
const visibleRefresh = {
  document: {
    hidden: false,
    activeElement: { matches: () => true },
    querySelector: () => dialogOpen ? {} : null,
    addEventListener: (name, callback) => refreshEvents.document.set(name, callback),
  },
  window: { addEventListener: (name, callback) => refreshEvents.window.set(name, callback) },
  currentView: 'agenda',
  refresh: () => { refreshCalls++; },
  setInterval: (callback, ms) => { refreshInterval = { callback, ms }; },
};
vm.createContext(visibleRefresh);
vm.runInContext(section('function refreshWhenVisible()', 'setView(location.hash.slice(1)'), visibleRefresh);
assert.equal(refreshInterval.ms, 15000);
refreshInterval.callback();
assert.equal(refreshCalls, 1, 'focused search must not stop phone updates');
refreshEvents.window.get('focus')();
refreshEvents.window.get('pageshow')();
refreshEvents.document.get('visibilitychange')();
assert.equal(refreshCalls, 4);
visibleRefresh.document.hidden = true;
refreshInterval.callback();
assert.equal(refreshCalls, 4);
visibleRefresh.document.hidden = false;
dialogOpen = true;
refreshEvents.window.get('focus')();
assert.equal(refreshCalls, 4, 'an open edit must keep its unsaved inputs');
dialogOpen = false;
visibleRefresh.currentView = 'settings';
refreshInterval.callback();
assert.equal(refreshCalls, 4, 'automatic refresh must preserve unsaved settings');
visibleRefresh.currentView = 'agenda';
refreshEvents.window.get('focus')();
assert.equal(refreshCalls, 5);
console.log('Desktop phone-update polling and focus refresh checks passed');

(async () => {
  const requests = [];
  let rendered = 0;
  const latestState = {
    state: {}, refreshVersion: 0,
    api: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
    $: () => ({}), render: () => { rendered++; }, toast() {},
  };
  vm.createContext(latestState);
  vm.runInContext(section('async function refresh(', 'function setView('), latestState);
  const oldRead = latestState.refresh({ silent: true });
  const newRead = latestState.refresh({ silent: true });
  requests[1].resolve({ tasks: [{ id: 'phone-task', status: 'completed' }] });
  await newRead;
  requests[0].resolve({ tasks: [{ id: 'phone-task', status: 'pending' }] });
  await oldRead;
  assert.equal(latestState.state.tasks[0].status, 'completed');
  assert.equal(rendered, 1, 'an older response cannot restore stale phone state');
  console.log('Concurrent desktop reads preserve the latest phone state');
})().catch(error => { console.error(error); process.exitCode = 1; });

// A discovered assignment without a deadline stays visible even with no import candidates.
(async () => {
  const controls = new Map();
  const input = { value: '2026' }, button = { disabled: false, textContent: '确认年份并加入安排' }, errorBox = {};
  const form = { dataset: { courseid: '11', clazzid: '22' } };
  const homework = { course: '英美现代戏剧原著赏读', title: '<img src=x onerror=alert(1)>', source_key: 'course:11:class:22:work:44',
    courseid: '11', clazzid: '22', time_text: '作答时间:11-13 15:18 至 11-26 15:19',
    status: 'pending', reason: 'missing_year', year_confirmation_available: true };
  let posted, syncs = 0, refreshes = 0, requests = 0, fail = false, release, needsScan = true, message;
  const ui = {
    state: { collection: { candidates: [], unresolved_homework: [homework] } },
    document: { activeElement: null },
    beijingDay: () => '2026-11-24',
    $: (selector, root) => {
      if (root === form) return selector === 'input[name="year"]' ? input : selector === '.form-error' ? errorBox : button;
      if (!controls.has(selector)) controls.set(selector, { contains: element => element === input });
      return controls.get(selector);
    },
    busy: async (target, _label, action) => { target.disabled = true; try { return await action(); } finally { target.disabled = false; } },
    api: async (url, options) => {
      requests++;
      assert.equal(url, '/api/collection/confirm-year');
      posted = JSON.parse(options.body);
      if (fail) throw Error('年份确认失败');
      if (release !== undefined) await new Promise(resolve => { release = resolve; });
      return { needs_scan: needsScan, message: needsScan ? undefined : '已加入 1 项安排，日历已更新。' };
    },
    syncCollection: async () => { syncs++; },
    refresh: async options => { assert.equal(options.silent, true); refreshes++; return true; },
    toast: value => { message = value; },
  };
  vm.createContext(ui);
  vm.runInContext('const pendingYearConfirmations = new Set();\n' +
    section('function escapeHTML(', 'function beijingDay(') +
    section('function unresolvedHomework()', 'function renderSettings()'), ui);
  ui.renderUnresolvedHomework();
  const list = controls.get('#unresolvedList');
  assert.equal(controls.get('#unresolvedArea').hidden, false);
  assert.equal(controls.get('#unresolvedNudge').hidden, false);
  assert(controls.get('#unresolvedSummary').textContent.includes('1 项'));
  assert(list.innerHTML.includes('英美现代戏剧原著赏读'));
  assert(list.innerHTML.includes('11-26 15:19'));
  assert(list.innerHTML.includes('&lt;img'));
  assert(!list.innerHTML.includes('<img'));
  assert(list.innerHTML.includes('value="2026"'));
  assert(list.innerHTML.includes('确认年份并加入安排'));
  list.innerHTML = 'editing';
  ui.document.activeElement = input;
  ui.renderUnresolvedHomework();
  assert.equal(list.innerHTML, 'editing', 'periodic refresh must preserve a year being edited');
  ui.document.activeElement = null;
  ui.state.collection.unresolved_homework = [{ ...homework, reason: 'missing_id', year_confirmation_available: false }];
  ui.renderUnresolvedHomework();
  assert(!list.innerHTML.includes('confirm-year-form'), 'an unidentified assignment cannot establish a year binding');
  ui.state.collection.unresolved_homework = [homework];
  input.value = '2026.5';
  await ui.confirmHomeworkYear(form);
  assert.equal(requests, 0);
  assert(errorBox.textContent.includes('2000 至 2099'));
  input.value = '2026'; fail = true;
  await ui.confirmHomeworkYear(form);
  assert.equal(errorBox.textContent, '年份确认失败');
  assert.equal(syncs, 0, 'failed confirmation must not start a collection');
  fail = false; release = () => {};
  const first = ui.confirmHomeworkYear(form);
  await ui.confirmHomeworkYear(form);
  assert.equal(requests, 2, 'double-click must not duplicate confirmation');
  release(); await first;
  assert.deepEqual(posted, { courseid: '11', clazzid: '22', year: 2026 });
  assert.equal(syncs, 1, 'confirmed year must be followed by the normal scan');
  assert.equal(refreshes, 0, 'a requested scan handles its own refresh');
  assert.equal(button.disabled, false);
  release = undefined; needsScan = false;
  await ui.confirmHomeworkYear(form);
  assert.equal(syncs, 1, 'a direct import must not trigger a second platform scan');
  assert.equal(refreshes, 1, 'a direct import must refresh the saved arrangements');
  assert.equal(message, '已加入 1 项安排，日历已更新。');
  ui.state.collection.unresolved_homework = [];
  ui.renderUnresolvedHomework();
  assert.equal(controls.get('#unresolvedArea').hidden, true);
  assert.equal(controls.get('#unresolvedNudge').hidden, true);
  assert.equal(list.innerHTML, '');
  ui.state = {};
  ui.renderUnresolvedHomework();
  assert.equal(controls.get('#unresolvedArea').hidden, true, 'older state without collection remains compatible');
  console.log('Unresolved homework visibility, safe year confirmation and input preservation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

// Exercise the settings form through its real submit handler, including all-only categories.
(async () => {
  const controls = new Map();
  let posted;
  const settingsUI = {
    state: { settings: { collection: { course_scope: { chaoxing_homework: ['概率论'], chaoxing_exams: [], xiji: [] },
      all_course_types: ['chaoxing_homework', 'chaoxing_exams', 'xiji'], homework_year: 2026 } } },
    $: selector => {
      if (!controls.has(selector)) controls.set(selector, { value: '', checked: false, classList: { toggle() {} },
        handlers: {}, addEventListener(name, callback) { this.handlers[name] = callback; } });
      return controls.get(selector);
    },
    syncTime: () => '', beijingDay: () => '2026-11-21',
    busy: async (_button, _label, action) => action(),
    api: async (_path, options) => { posted = JSON.parse(options.body); },
    refresh: async () => true, toast() {},
  };
  vm.createContext(settingsUI);
  vm.runInContext(section('const COURSE_SELECTIONS =', 'const ZONE =') +
    section('function renderSettings()', 'function openTask(') +
    section("$('#courseScopeForm').addEventListener", "$('#physicsForm').addEventListener"), settingsUI);
  settingsUI.renderSettings();
  assert.equal(controls.get('#allXijiAssignments').checked, true);
  assert.equal(controls.get('#xijiCourses').disabled, true);
  assert.equal(controls.get('#chaoxingHomeworkCourses').value, '概率论');
  assert.equal((controls.get('#courseScopeSummary').textContent.match(/全部课程/g) || []).length, 3);
  await controls.get('#courseScopeForm').handlers.submit({ preventDefault() {}, currentTarget: {} });
  assert.deepEqual(posted.collection.all_course_types, ['chaoxing_homework', 'chaoxing_exams', 'xiji']);
  assert.deepEqual(posted.collection.course_scope.xiji, []);
  console.log('All-course settings render and save checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
