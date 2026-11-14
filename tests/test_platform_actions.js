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
vm.runInContext('const pendingPlatformActions = new Map();\n' +
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
