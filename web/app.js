'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const SOURCE_NAMES = { chaoxing: '学习通', xiji: '希冀平台', manual: '手动添加', physics: '大物周作业' };
const ZONE = 'Asia/Shanghai';
let state = { tasks: [], settings: {}, platforms: {}, calendar_sync: {} };
let filters = { status: 'pending', source: 'all', kind: 'all', search: '', day: null };
let weekStart = mondayOf(beijingDay());
let currentView = 'agenda';
let candidates = [];
let importTasks = [];
let toastTimer;
let confirmResolver = null;
let lastCollectionScan = null;
const pendingPlatformActions = new Map();

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}
function beijingDay(value = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value));
  const get = type => parts.find(part => part.type === type)?.value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}
function addDays(day, count) {
  const date = new Date(`${day}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + count);
  return date.toISOString().slice(0, 10);
}
function mondayOf(day) {
  const weekday = new Date(`${day}T12:00:00Z`).getUTCDay();
  return addDays(day, -(weekday === 0 ? 6 : weekday - 1));
}
function validDate(value) { return Boolean(value) && Number.isFinite(new Date(value).getTime()); }
function formatDate(value, options = {}) {
  if (!validDate(value)) return '时间待核对';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: ZONE, ...options }).format(new Date(value));
}
function syncTime(value) {
  return validDate(value) ? formatDate(value, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }) : '尚无记录';
}
function syncResultLabel(result) {
  return ({ never: '尚未同步', running: '正在读取平台', success: '已成功读取', partial: '部分课程未完成', failed: '本次读取失败', scope_changed: '课程范围已改变，待核对', interrupted: '上次读取已中断', disabled: '未选择课程' })[result] || '状态待确认';
}
function renderSyncTimes() {
  const sync = state.collection?.sync || {};
  const calendar = state.calendar_sync || {};
  $('#syncResult').textContent = syncResultLabel(sync.scope_changed ? 'scope_changed' : sync.last_result);
  $('#platformSyncTime').textContent = `平台最近完整同步：${syncTime(sync.last_success_at)}${sync.scope_changed && sync.last_success_at ? '（旧课程范围）' : ''}`;
  $('#sourceSyncTimes').textContent = ['chaoxing', 'xiji'].map(source => {
    const info = sync.sources?.[source] || {};
    return `${sourceName(source)}：${info.configured === false ? '未选择课程' : `${syncTime(info.last_success_at)}${info.scope_changed && info.last_success_at ? '（旧范围）' : ''}`}`;
  }).join(' · ');
  const daily = state.settings?.daily || {};
  $('#syncAttemptTime').textContent = `最近尝试：${syncTime(sync.last_attempt_at)} · ${daily.enabled ? `每日 ${daily.time || '08:00'} 计划读取，提醒前额外核对` : '每日自动读取已关闭'}`;
  const calendarSuccess = calendar.last_success_at || (calendar.ok === true ? calendar.last_run : null);
  $('#calendarSyncTime').textContent = `最近写入 Apple 日历：${syncTime(calendarSuccess)}${calendar.needs_sync ? ' · 有更新待写入' : ''}${calendar.ok === false ? ' · 本次写入未完成' : ''}`;
}
function localInput(value) {
  if (!validDate(value)) return '';
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date(value));
  const get = type => parts.find(part => part.type === type)?.value;
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
}
function inputISO(value) {
  if (!value) return null;
  const date = new Date(`${value}${value.length === 16 ? ':00' : ''}+08:00`);
  if (!Number.isFinite(date.getTime())) throw new Error('请填写有效的北京时间。');
  return date.toISOString();
}
function shortDate(day) { return `${Number(day.slice(5, 7))}月${Number(day.slice(8, 10))}日`; }
function sourceName(source) { return SOURCE_NAMES[source] || '其他来源'; }
function safeURL(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
}
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  let data;
  const raw = await response.text();
  try { data = raw ? JSON.parse(raw) : {}; } catch { throw new Error('服务返回了无法识别的结果，请确认本地程序正在运行。'); }
  if (!response.ok) throw new Error(data.error || data.message || `请求未成功（${response.status}）`);
  return data;
}
function toast(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 6500);
}
async function busy(button, text, action) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = text;
  try { return await action(); } finally { button.disabled = false; button.textContent = original; }
}
async function refresh({ silent = false } = {}) {
  try {
    const data = await api('/api/state');
    state = { tasks: [], settings: {}, platforms: {}, calendar_sync: {}, ...data };
    if (!Array.isArray(state.tasks)) state.tasks = [];
    $('#loadError').hidden = true;
    render();
    return true;
  } catch (error) {
    $('#loadError').textContent = `暂时无法读取安排：${error.message} 请确认 Mac 上的本地程序已启动，然后点击右上角刷新。`;
    $('#loadError').hidden = false;
    if (!silent) toast('暂时未能连接本地程序，已有日历提醒不受此页面影响。');
    if ($('.skeleton')) renderTasks();
    return false;
  }
}
function setView(view) {
  if (!['agenda', 'connections', 'settings'].includes(view)) view = 'agenda';
  currentView = view;
  for (const name of ['agenda', 'connections', 'settings']) {
    const element = $(`#${name}View`);
    element.hidden = name !== view;
    element.classList.toggle('active', name === view);
  }
  $$('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === view));
  if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  window.scrollTo({ top: 0, behavior: 'instant' });
}
function render() {
  const pending = state.tasks.filter(task => task.status !== 'completed');
  const now = Date.now();
  const upcoming = pending.filter(task => validDate(task.due_at) && new Date(task.due_at).getTime() >= now);
  const overdue = pending.length - upcoming.length;
  $('#pendingCount').textContent = pending.length;
  $('#soonCount').innerHTML = `${upcoming.filter(task => new Date(task.due_at).getTime() - now <= 48 * 3600000).length}<small>项待办</small>`;
  $('#weekCount').innerHTML = `${upcoming.filter(task => new Date(task.due_at).getTime() - now <= 7 * 86400000).length}<small>项待办</small>`;
  $('#examCount').innerHTML = `${upcoming.filter(task => task.kind === 'exam').length}<small>场考试</small>`;
  $('#todayLabel').textContent = formatDate(new Date(), { month: 'long', day: 'numeric', weekday: 'long' });
  $('#accountNudge').hidden = ['chaoxing', 'xiji'].some(source => ['authenticated', 'session_detected', 'session_saved'].includes(state.platforms?.[source]?.status));
  if (!pending.length) $('#agendaSummary').textContent = state.tasks.length ? '所有安排都已完成，留一点时间给自己。' : '还没有安排。添加下一次截止，从容准备。';
  else if (overdue) $('#agendaSummary').textContent = `${pending.length} 项安排待完成，其中 ${overdue} 项已过截止。完成后记得勾选。`;
  else $('#agendaSummary').textContent = `${pending.length} 项安排待完成。按自己的节奏，一件一件来。`;
  renderWeek();
  renderTasks();
  renderPlatforms();
  renderSettings();
  renderSyncTimes();
  if (state.collection?.last_scan && state.collection.last_scan !== lastCollectionScan && !$('#candidateArea').contains(document.activeElement)) {
    lastCollectionScan = state.collection.last_scan;
    candidates = state.collection.candidates || [];
    $('#candidateWarnings').hidden = !state.collection.warnings?.length;
    $('#candidateWarnings').textContent = (state.collection.warnings || []).join('\n');
    renderCandidates();
  }
}
function renderWeek() {
  const today = beijingDay();
  const end = addDays(weekStart, 6);
  $('#weekHeading').textContent = `${weekStart === mondayOf(today) ? '本周' : '截止日历'} · ${shortDate(weekStart)} 至 ${shortDate(end)}`;
  const weekdayNames = ['一', '二', '三', '四', '五', '六', '日'];
  $('#weekStrip').innerHTML = weekdayNames.map((name, index) => {
    const day = addDays(weekStart, index);
    const count = state.tasks.filter(task => task.status !== 'completed' && validDate(task.due_at) && beijingDay(task.due_at) === day).length;
    return `<button class="week-day${day === today ? ' today' : ''}${filters.day === day ? ' selected' : ''}" data-day="${day}" aria-pressed="${filters.day === day}" aria-label="${shortDate(day)}，周${name}，${count}项待办">${day === today ? '<span class="today-label-mini">今天</span>' : ''}<span class="week-name">周${name}</span><span class="week-date">${Number(day.slice(8))}</span><span class="week-dot${count ? '' : ' blank'}" aria-hidden="true"></span></button>`;
  }).join('');
  $('#clearDateFilter').hidden = !filters.day;
  $('#clearDateFilter').textContent = filters.day ? `${shortDate(filters.day)} ×` : '';
}
function countdown(task) {
  if (task.status === 'completed') return { text: '已完成', urgent: false };
  const delta = new Date(task.due_at).getTime() - Date.now();
  if (!Number.isFinite(delta)) return { text: '请核对时间', urgent: true };
  if (delta < 0) return { text: task.kind === 'lecture' ? '已开始' : '已过截止', urgent: true };
  if (delta < 3600000) return { text: `剩余 ${Math.max(1, Math.ceil(delta / 60000))} 分钟`, urgent: true };
  if (delta <= 48 * 3600000) return { text: `剩余 ${Math.ceil(delta / 3600000)} 小时`, urgent: true };
  return { text: `还有 ${Math.ceil(delta / 86400000)} 天`, urgent: false };
}
function renderTasks() {
  let tasks = state.tasks.filter(task => {
    if (filters.status !== 'all' && (task.status === 'completed' ? 'completed' : 'pending') !== filters.status) return false;
    if (filters.source !== 'all' && task.source !== filters.source) return false;
    if (filters.kind !== 'all' && task.kind !== filters.kind) return false;
    if (filters.day && (!validDate(task.due_at) || beijingDay(task.due_at) !== filters.day)) return false;
    if (filters.search && !`${task.title || ''} ${task.course || ''} ${task.notes || ''}`.toLowerCase().includes(filters.search.toLowerCase())) return false;
    return true;
  });
  tasks.sort((a, b) => (new Date(a.due_at).getTime() || Infinity) - (new Date(b.due_at).getTime() || Infinity));
  $('#resultCount').textContent = `${tasks.length} 项安排`;
  if (!tasks.length) {
    const noTasks = !state.tasks.length;
    $('#taskList').innerHTML = `<div class="empty-state"><div class="empty-calendar" aria-hidden="true">✓</div><h2>${noTasks ? '下一次截止，从这里记起' : filters.status === 'completed' ? '还没有已完成的安排' : '这里暂时没有待办'}</h2><p>${noTasks ? '登录学习通或希冀平台读取安排，也可以先手动记下一项。' : '可以切换日期、来源或完成状态，查看其他安排。'}</p><button class="button secondary" id="emptyAction">${noTasks ? '登录并读取平台安排' : '查看全部安排'}</button></div>`;
    $('#emptyAction').addEventListener('click', () => {
      if (noTasks) setView('connections');
      else { filters = { ...filters, status: 'all', source: 'all', kind: 'all', day: null, search: '' }; syncFilterControls(); renderWeek(); renderTasks(); }
    });
    return;
  }
  const grouped = new Map();
  const today = beijingDay();
  for (const task of tasks) {
    const key = validDate(task.due_at) ? beijingDay(task.due_at) : 'unknown';
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(task);
  }
  $('#taskList').innerHTML = Array.from(grouped, ([day, entries]) => {
    let title = day === today ? '今天' : day === addDays(today, 1) ? '明天' : day === 'unknown' ? '时间待核对' : shortDate(day);
    const isPast = day !== 'unknown' && day < today;
    const weekday = day === 'unknown' ? '' : formatDate(`${day}T12:00:00+08:00`, { weekday: 'long' });
    return `<section class="task-group"><h2 class="group-heading${isPast ? ' overdue' : ''}"><strong>${title}</strong><span>${weekday}${day === today || day === addDays(today, 1) ? ` · ${shortDate(day)}` : ''}</span></h2>${entries.map(taskRow).join('')}</section>`;
  }).join('');
}
function taskRow(task) {
  const count = countdown(task);
  const completed = task.status === 'completed';
  const start = task.kind !== 'lecture' && validDate(task.start_at) ? ` · 开始 ${formatDate(task.start_at, { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })}` : '';
  const kindLabel = task.kind === 'exam' ? '考试' : task.kind === 'lecture' ? '上课' : '';
  return `<article class="task-row${completed ? ' completed' : ''}"><button class="complete-button" data-complete="${escapeHTML(task.id)}" aria-label="${completed ? '标记为待完成' : '标记为已完成'}：${escapeHTML(task.title)}" aria-pressed="${completed}">✓</button><div class="task-content"><button class="task-title" data-edit="${escapeHTML(task.id)}">${escapeHTML(task.title || '未命名安排')}</button><div class="task-meta"><span>${escapeHTML(task.course || '未填写课程')}</span><span class="source-label">${sourceName(task.source)}</span>${kindLabel ? `<span class="kind-label">${kindLabel}</span>` : ''}${start ? `<span>${escapeHTML(start.slice(3))}</span>` : ''}${safeURL(task.url) ? `<a href="${escapeHTML(safeURL(task.url))}" target="_blank" rel="noopener noreferrer" aria-label="打开${escapeHTML(task.title)}的平台页面">打开平台 ↗</a>` : ''}</div></div><div class="task-timing"><span class="task-time">${task.kind === 'lecture' ? '上课 ' : ''}${formatDate(task.due_at, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })}</span><span class="task-countdown${count.urgent ? ' urgent' : ''}">${count.text}</span></div></article>`;
}
function syncFilterControls() {
  $$('[data-status]').forEach(button => button.classList.toggle('active', button.dataset.status === filters.status));
  $('#sourceFilter').value = filters.source;
  $('#kindFilter').value = filters.kind;
  $('#searchInput').value = filters.search;
}
function renderPlatforms() {
  const error = state.collection?.last_error?.message;
  $('#collectionError').hidden = !error;
  $('#collectionError').textContent = error ? `最近同步未完成：${error}` : '';
  const daily = state.settings?.daily || {};
  const pending = state.collection?.pending_count || 0;
  const retry = state.collection?.automatic_retry || {};
  const paused = Object.keys(retry.blocked_sources || {}).map(sourceName);
  const retryText = paused.length ? ` ${paused.join('、')}登录已失效，自动读取已暂停；本人登录后点击检查即可恢复。` : retry.daily?.attempts >= 3 ? ' 今日自动读取已达 3 次上限，可手动同步。' : '';
  $('#collectionModeMessage').textContent = `${daily.enabled ? `每天 ${daily.time || '08:00'} 自动同步（北京时间），提醒前额外核对提交状态。` : '每日自动同步已关闭，可点击立即同步。'}${pending ? ` 当前有 ${pending} 项需核对的安排。` : ''}${state.collection?.completed_count ? ` 已识别 ${state.collection.completed_count} 项已提交记录，不设置提醒。` : ''}${retryText} Mac 需运行并联网。`;
  $('#platformList').innerHTML = ['chaoxing', 'xiji'].map(source => {
    const platform = state.platforms?.[source] || {};
    const authenticated = platform.status === 'authenticated';
    const statuses = { disabled: '未选择课程', authenticated: '已确认登录', session_saved: '会话已保存 · 待检查', session_detected: '已识别登录页面 · 待验证课程', browser_open: '登录窗口已打开 · 待检查', browser_opened: '登录窗口已打开 · 待检查', not_authenticated: '登录已失效', unverified: '页面暂未识别 · 待检查', error: '验证未完成', not_connected: '尚未接入' };
    const status = statuses[platform.status] || '登录状态待确认';
    const description = platform.message || '请在 Mac 上打开平台登录页，由你本人输入账号、密码和验证码。';
    const sync = state.collection?.sync?.sources?.[source] || {};
    const syncDetail = `最近成功读取：${syncTime(sync.last_success_at)}${sync.scope_changed && sync.last_success_at ? '（旧课程范围）' : ''}`;
    const attemptDetail = `最近尝试：${syncTime(sync.last_attempt_at)} · ${syncResultLabel(sync.scope_changed ? 'scope_changed' : sync.last_result)}`;
    const pending = pendingPlatformActions.get(source);
    const disabled = pending ? ' disabled' : '';
    const hint = platform.status === 'error' ? '<p class="fine-print">本次网页读取未完成，尚不能判断会话是否有效。请先确认平台网页能正常加载，再检查登录状态。</p>' : '';
    return `<article class="platform-card"><div class="platform-top"><span class="platform-mark" aria-hidden="true">${source === 'chaoxing' ? '学' : '希'}</span><div><h3>${sourceName(source)}</h3><p>${source === 'chaoxing' ? '作业 · 考试' : '武汉大学 · 在线作业'}</p></div></div><span class="status-pill${authenticated ? '' : ' waiting'}">${status}</span><p>${escapeHTML(description)}</p>${hint}<div class="platform-sync-detail"><p>${escapeHTML(syncDetail)}</p><p>${escapeHTML(attemptDetail)}</p></div><div class="platform-actions"><button class="button secondary" data-check-login="${source}"${disabled}>${pending === 'probe' ? '正在检查…' : '检查登录状态'}</button><button class="button secondary" data-connect="${source}"${disabled}>${pending === 'open' ? '正在打开窗口…' : '打开登录窗口 ↗'}</button><button class="button secondary" data-finish-login="${source}"${disabled}>${pending === 'finish' ? '正在确认登录…' : '完成登录并关闭专用窗口'}</button></div></article>`;
  }).join('');
}
function renderSettings() {
  const collection = state.settings?.collection || {};
  const scope = collection.course_scope || {};
  $('#chaoxingHomeworkCourses').value = (scope.chaoxing_homework || []).join('\n');
  $('#chaoxingExamCourses').value = (scope.chaoxing_exams || []).join('\n');
  $('#xijiCourses').value = (scope.xiji || []).join('\n');
  $('#homeworkYear').value = collection.homework_year || '';
  const parts = [['学习通作业', scope.chaoxing_homework], ['学习通考试', scope.chaoxing_exams], ['希冀', scope.xiji]]
    .filter(([, values]) => values?.length).map(([name, values]) => `${name}：${values.join('、')}`);
  $('#courseScopeSummary').textContent = parts.length ? parts.join('；') : '尚未设置课程，请先到“设置 → 我的课程”填写。';
  const sync = state.calendar_sync || {};
  const calendar = state.settings?.calendar || {};
  $('#calendarName').textContent = calendar.name || '作业与考试';
  const synced = Boolean(sync.last_run) && sync.ok !== false && !sync.needs_sync;
  $('#calendarStatus').textContent = synced ? '已同步' : sync.available === false ? '暂不可用' : '待同步';
  $('#calendarStatus').classList.toggle('waiting', !synced);
  $('#lastSync').textContent = `最近成功写入：${syncTime(sync.last_success_at || (sync.ok === true ? sync.last_run : null))}`;
  $('#calendarMessage').textContent = sync.message || '首次同步将在 Mac 上请求日历访问权限。';
  $('#syncCalendarButton').disabled = sync.available === false;
  const physics = state.settings?.physics || {};
  $('#physicsEnabled').checked = Boolean(physics.enabled);
  $('#physicsTimeMode').value = physics.time === '24:00' ? '24:00' : 'custom';
  $('#physicsCustomTime').value = physics.time && physics.time !== '24:00' ? physics.time : '';
  $('#physicsCustomLabel').hidden = $('#physicsTimeMode').value !== 'custom';
  $('#physicsUntil').value = physics.until || '';
  const daily = state.settings?.daily || {};
  $('#dailyEnabled').checked = daily.enabled !== false;
  $('#dailyTime').value = daily.time || '08:00';
  const collectionSync = state.collection?.sync || {};
  $('#dailyLastSuccess').textContent = `平台最近完整同步：${syncTime(collectionSync.last_success_at)}${collectionSync.scope_changed && collectionSync.last_success_at ? '（旧课程范围）' : ''}；最近尝试：${syncTime(collectionSync.last_attempt_at)}。以上为北京时间。`;
  const collectionError = state.collection?.last_error?.message;
  $('#dailySyncWarning').hidden = !collectionError;
  $('#dailySyncWarning').textContent = collectionError ? `最近同步未完成：${collectionError}` : '';
  const deviceURL = state.connection?.url;
  $('#deviceAccessLink').hidden = !deviceURL;
  $('#deviceURL').value = deviceURL || '';
  $('#deviceAccessMessage').textContent = deviceURL ? '将设备与 Mac 连接到同一 Wi-Fi，在 Safari 打开下方专属链接。链接包含访问凭证，请仅分享给自己的设备。' : '当前仅限此 Mac 访问。双击“启动作业提醒.command”后，手机与平板访问链接会显示在这里。';
}
function openTask(id = null) {
  $('#taskForm').reset();
  $('#taskError').textContent = '';
  const task = id ? state.tasks.find(item => String(item.id) === String(id)) : null;
  $('#taskId').value = task?.id || '';
  $('#taskDialogTitle').textContent = task ? '编辑安排' : '添加安排';
  $('#taskTitle').value = task?.title || '';
  $('#taskCourse').value = task?.course || '';
  const physicsOption = $('#taskSource option[value="physics"]');
  if (physicsOption) physicsOption.remove();
  if (task?.source === 'physics') {
    const option = document.createElement('option');
    option.value = 'physics';
    option.textContent = '大物周作业';
    $('#taskSource').append(option);
  }
  $('#taskSource').value = task?.source || 'manual';
  $('#taskSource').disabled = task?.source === 'physics';
  $('#taskKind').value = task?.kind || 'assignment';
  $('#taskStart').value = localInput(task?.start_at);
  $('#taskDue').value = localInput(task?.due_at);
  $('#taskURL').value = task?.url || '';
  $('#taskNotes').value = task?.notes || '';
  $('#taskCompleted').checked = task?.status === 'completed';
  $('#deleteTaskButton').hidden = !task;
  updateTaskKindFields();
  $('#taskDialog').showModal();
  requestAnimationFrame(() => $('#taskTitle').focus());
}
function updateTaskKindFields() {
  const lecture = $('#taskKind').value === 'lecture';
  $('#taskStartField').hidden = lecture;
  $('#taskDueLabel').textContent = lecture ? '上课时间' : '截止时间';
  $('#taskTimeHint').textContent = lecture ? '以上时间为北京时间。仅记录开课时间，不推测课程时长。' : '以上时间均为北京时间。考试可填写开放时间及最晚提交时间。';
  if (lecture) $('#taskSource').value = 'manual';
  $('#taskSource').disabled = lecture || $('#taskSource').value === 'physics';
}
function confirmDelete(task) {
  $('#confirmTitle').textContent = '删除这项安排？';
  $('#confirmText').textContent = `“${task.title}”将从此程序移除。若已同步日历，请同步后确认日历事件也已更新。`;
  $('#confirmDialog').showModal();
  return new Promise(resolve => { confirmResolver = resolve; });
}
function finishConfirmation(accepted) {
  $('#confirmDialog').close();
  if (confirmResolver) { confirmResolver(accepted); confirmResolver = null; }
}
async function syncCalendar(button) {
  await busy(button, '正在同步…', async () => {
    $('#calendarMessage').textContent = '正在请求日历访问并更新安排，请留意 Mac 上的系统授权提示。';
    try {
      const result = await api('/api/calendar/sync', { method: 'POST', body: '{}' });
      await refresh({ silent: true });
      const message = result.message || (result.ok ? '日历已同步。请确认各设备已开启 iCloud 日历与通知。' : '日历暂未同步，请检查 Mac 上的授权。');
      $('#calendarMessage').textContent = message;
      toast(message);
    } catch (error) { $('#calendarMessage').textContent = error.message; toast(error.message); }
  });
}
async function platformAction(source, action, work) {
  if (pendingPlatformActions.has(source)) return;
  pendingPlatformActions.set(source, action);
  renderPlatforms();
  try { return await work(); }
  finally { pendingPlatformActions.delete(source); renderPlatforms(); }
}
async function connectPlatform(source, button) {
  await platformAction(source, 'open', async () => {
    try {
      const result = await api('/api/collection/open', { method: 'POST', body: JSON.stringify({ source }) });
      $('#collectionMessage').textContent = result.message || '已在 Mac 上打开平台登录页。请本人完成账号登录，再打开作业或考试列表。手机和平板可查看已保存的安排。';
      await refresh({ silent: true });
      toast(result.ok === false || result.navigation_pending ? result.message || '未能打开登录页。' : '本人登录后，点击“完成登录并关闭专用窗口”，再立即同步。');
    } catch (error) { $('#collectionMessage').textContent = error.message; toast(error.message); }
  });
}
async function checkPlatformLogin(source, button, finish = false) {
  await platformAction(source, finish ? 'finish' : 'probe', async () => {
    try {
      const result = await api(`/api/collection/${finish ? 'finish-login' : 'probe'}`, { method: 'POST', body: JSON.stringify({ source }) });
      let message = result.message || '登录状态已检查。';
      if (result.authenticated && finish) message = result.closed_login_window ? '已确认登录并保存会话，专用窗口已关闭。可点击“立即同步”。' : '已确认登录，会话已保存。可点击“立即同步”。';
      else if (result.authenticated) message += ' 登录窗口若仍开着，请完成登录并关闭专用窗口后再同步。';
      $('#collectionMessage').textContent = message;
      await refresh({ silent: true });
      toast(message);
    } catch (error) { $('#collectionMessage').textContent = error.message; toast(error.message); }
  });
}
function renderCandidates() {
  $('#candidateArea').hidden = !candidates.length;
  const isCurrent = candidate => candidate.status !== 'completed' && validDate(candidate.due_at) && new Date(candidate.due_at).getTime() >= Date.now();
  $('#selectAllCandidates').checked = candidates.length > 0 && candidates.every(isCurrent);
  $('#selectAllCandidates').indeterminate = candidates.some(isCurrent) && !candidates.every(isCurrent);
  $('#candidateList').innerHTML = candidates.map((candidate, index) => `<article class="candidate-card" data-candidate="${index}">
    <label class="checkbox-label"><input type="checkbox" class="candidate-selected"${isCurrent(candidate) ? ' checked' : ''}> ${isCurrent(candidate) ? '加入此安排' : '历史已截止或已完成（可手动勾选）'}<span class="candidate-source">${sourceName(candidate.source)}</span></label>
    <div class="candidate-fields">
      <label class="full-width">任务名称<input class="candidate-title" value="${escapeHTML(candidate.title || '')}" maxlength="300" required></label>
      <label>课程名称<input class="candidate-course" value="${escapeHTML(candidate.course || '')}" maxlength="200"></label>
      <label>类型<select class="candidate-kind"><option value="assignment"${candidate.kind === 'exam' ? '' : ' selected'}>作业</option><option value="exam"${candidate.kind === 'exam' ? ' selected' : ''}>考试</option></select></label>
      <label>开始时间（北京时间，可选）<input type="datetime-local" class="candidate-start" value="${localInput(candidate.start_at)}"></label>
      <label>截止时间（北京时间）<input type="datetime-local" class="candidate-due" value="${localInput(candidate.due_at)}" required></label>
      <label class="checkbox-label full-width"><input type="checkbox" class="candidate-completed"${candidate.status === 'completed' ? ' checked' : ''}> 我已完成此项（保存记录，不设置提醒）</label>
    </div>
    ${/提交状态未能可靠确认/.test(candidate.notes || '') ? '<p class="fine-print">平台未能可靠返回整项提交状态，请核对是否已完成。</p>' : ''}
    ${candidate.notes ? `<details class="candidate-evidence"><summary>查看时间依据与来源说明</summary><p>${escapeHTML(candidate.notes)}</p></details>` : ''}
  </article>`).join('');
}
function selectedCandidates() {
  const selected = [];
  for (const row of $$('.candidate-card')) {
    if (!$('.candidate-selected', row).checked) continue;
    for (const input of $$('input', row)) { if (!input.reportValidity()) return null; }
    const original = candidates[Number(row.dataset.candidate)];
    const start = inputISO($('.candidate-start', row).value);
    const due = inputISO($('.candidate-due', row).value);
    if (start && due && new Date(start) > new Date(due)) throw new Error('开始时间不能晚于截止时间，请核对选中的安排。');
    selected.push({ ...original, title: $('.candidate-title', row).value.trim(), course: $('.candidate-course', row).value.trim(), kind: $('.candidate-kind', row).value, start_at: start, due_at: due, status: $('.candidate-completed', row).checked ? 'completed' : 'pending' });
  }
  return selected;
}

document.addEventListener('click', async event => {
  const viewButton = event.target.closest('[data-view]');
  if (viewButton) { setView(viewButton.dataset.view); return; }
  const closeButton = event.target.closest('[data-close]');
  if (closeButton) { if (closeButton.dataset.close === 'confirmDialog') finishConfirmation(false); else $(`#${closeButton.dataset.close}`).close(); return; }
  const dayButton = event.target.closest('[data-day]');
  if (dayButton) { filters.day = filters.day === dayButton.dataset.day ? null : dayButton.dataset.day; renderWeek(); renderTasks(); return; }
  const statusButton = event.target.closest('[data-status]');
  if (statusButton) { filters.status = statusButton.dataset.status; syncFilterControls(); renderTasks(); return; }
  const editButton = event.target.closest('[data-edit]');
  if (editButton) { openTask(editButton.dataset.edit); return; }
  const completeButton = event.target.closest('[data-complete]');
  if (completeButton) {
    const task = state.tasks.find(item => String(item.id) === completeButton.dataset.complete);
    if (!task) return;
    completeButton.disabled = true;
    try { await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'PUT', body: JSON.stringify({ status: task.status === 'completed' ? 'pending' : 'completed' }) }); await refresh({ silent: true }); toast(task.status === 'completed' ? '已恢复为待完成。' : '已完成，又做好了一件事。'); }
    catch (error) { completeButton.disabled = false; toast(error.message); }
    return;
  }
  const connectButton = event.target.closest('[data-connect]');
  if (connectButton) await connectPlatform(connectButton.dataset.connect, connectButton);
  const checkButton = event.target.closest('[data-check-login]');
  if (checkButton) await checkPlatformLogin(checkButton.dataset.checkLogin, checkButton);
  const finishButton = event.target.closest('[data-finish-login]');
  if (finishButton) await checkPlatformLogin(finishButton.dataset.finishLogin, finishButton, true);
});
$('#addTaskButton').addEventListener('click', () => openTask());
$('#refreshButton').addEventListener('click', async event => { await busy(event.currentTarget, '↻', async () => { if (await refresh()) toast('安排已更新。'); }); });
$('#previousWeek').addEventListener('click', () => { weekStart = addDays(weekStart, -7); renderWeek(); });
$('#nextWeek').addEventListener('click', () => { weekStart = addDays(weekStart, 7); renderWeek(); });
$('#todayButton').addEventListener('click', () => { weekStart = mondayOf(beijingDay()); filters.day = null; renderWeek(); renderTasks(); });
$('#clearDateFilter').addEventListener('click', () => { filters.day = null; renderWeek(); renderTasks(); });
$('#sourceFilter').addEventListener('change', event => { filters.source = event.target.value; renderTasks(); });
$('#kindFilter').addEventListener('change', event => { filters.kind = event.target.value; renderTasks(); });
$('#searchInput').addEventListener('input', event => { filters.search = event.target.value.trim(); renderTasks(); });
$('#taskKind').addEventListener('change', updateTaskKindFields);
$('#taskForm').addEventListener('submit', async event => {
  event.preventDefault();
  $('#taskError').textContent = '';
  await busy($('#saveTaskButton'), '正在保存…', async () => {
    try {
      const id = $('#taskId').value;
      const task = { title: $('#taskTitle').value.trim(), course: $('#taskCourse').value.trim(), source: $('#taskSource').value, kind: $('#taskKind').value, start_at: inputISO($('#taskStart').value), due_at: inputISO($('#taskDue').value), url: $('#taskURL').value.trim(), notes: $('#taskNotes').value.trim(), status: $('#taskCompleted').checked ? 'completed' : 'pending' };
      if (!task.title) throw new Error('请填写任务名称。');
      if (task.kind === 'lecture') { task.source = 'manual'; task.start_at = null; }
      if (!task.due_at) throw new Error(task.kind === 'lecture' ? '请填写上课时间。' : '请填写截止时间。');
      if (task.start_at && new Date(task.start_at) > new Date(task.due_at)) throw new Error('开始时间不能晚于截止时间。');
      await api(id ? `/api/tasks/${encodeURIComponent(id)}` : '/api/tasks', { method: id ? 'PUT' : 'POST', body: JSON.stringify(task) });
      $('#taskDialog').close();
      await refresh({ silent: true });
      toast('安排已保存。首次使用请到设置中同步 Apple 日历。');
    } catch (error) { $('#taskError').textContent = error.message; }
  });
});
$('#deleteTaskButton').addEventListener('click', async event => {
  const button = event.currentTarget;
  const task = state.tasks.find(item => String(item.id) === $('#taskId').value);
  if (!task || !await confirmDelete(task)) return;
  await busy(button, '正在删除…', async () => {
    try { await api(`/api/tasks/${encodeURIComponent(task.id)}`, { method: 'DELETE' }); $('#taskDialog').close(); await refresh({ silent: true }); toast('安排已删除。'); }
    catch (error) { $('#taskError').textContent = error.message; }
  });
});
$('#confirmAccept').addEventListener('click', () => finishConfirmation(true));
$('#confirmCancel').addEventListener('click', () => finishConfirmation(false));
$('#confirmDialog').addEventListener('cancel', event => { event.preventDefault(); finishConfirmation(false); });
$('#syncCalendarButton').addEventListener('click', event => syncCalendar(event.currentTarget));
$('#sidebarSync').addEventListener('click', event => { setView('settings'); syncCalendar(event.currentTarget); });
$('#physicsTimeMode').addEventListener('change', () => { $('#physicsCustomLabel').hidden = $('#physicsTimeMode').value !== 'custom'; });
$('#courseScopeForm').addEventListener('submit', async event => {
  event.preventDefault();
  $('#courseScopeError').textContent = '';
  await busy($('button[type="submit"]', event.currentTarget), '正在保存…', async () => {
    try {
      const lines = selector => [...new Set($(selector).value.split(/\r?\n/).map(s => s.trim()).filter(Boolean))];
      const course_scope = { chaoxing_homework: lines('#chaoxingHomeworkCourses'), chaoxing_exams: lines('#chaoxingExamCourses'), xiji: lines('#xijiCourses') };
      const homework_year = $('#homeworkYear').value ? Number($('#homeworkYear').value) : null;
      if (homework_year !== null && homework_year !== Number(beijingDay().slice(0, 4))) throw new Error('仅可确认当年的缺年份日期；其他年份请手动填写完整截止时间。');
      await api('/api/settings', { method: 'PUT', body: JSON.stringify({ collection: { course_scope, homework_year } }) });
      await refresh({ silent: true });
      toast('课程范围已保存，可前往“平台接入”立即同步。');
    } catch (error) { $('#courseScopeError').textContent = error.message; }
  });
});
$('#physicsForm').addEventListener('submit', async event => {
  event.preventDefault();
  $('#physicsError').textContent = '';
  await busy($('button[type="submit"]', event.currentTarget), '正在保存…', async () => {
    try {
      const enabled = $('#physicsEnabled').checked;
      const time = $('#physicsTimeMode').value === '24:00' ? '24:00' : $('#physicsCustomTime').value || null;
      if (enabled && !time) throw new Error('启用每周作业前，请填写准确的截止时间。');
      await api('/api/settings', { method: 'PUT', body: JSON.stringify({ physics: { enabled, time, until: $('#physicsUntil').value || null } }) });
      await refresh({ silent: true });
      toast('每周大物安排已保存。');
    } catch (error) { $('#physicsError').textContent = error.message; }
  });
});
$('#scanButton').addEventListener('click', async event => {
  await busy(event.currentTarget, '正在同步…', async () => {
    $('#collectionMessage').textContent = '正在核对所选课程的作业、考试和提交状态…';
    try {
      const result = await api('/api/collection/scan', { method: 'POST', body: '{}' });
      candidates = Array.isArray(result.candidates) ? result.candidates : [];
      $('#candidateWarnings').hidden = !result.warnings?.length;
      $('#candidateWarnings').textContent = (result.warnings || []).join('\n');
      $('#collectionMessage').textContent = result.coverage_complete ? `已同步 ${result.synced || 0} 项记录，新增 ${result.added || 0} 项。已提交的安排不会提醒。` : candidates.length ? `识别到 ${candidates.length} 项候选安排，请在下方核对。` : '本次未完成全部课程读取，已有安排已保留。请查看平台的检查结果。';
      if (!candidates.length && result.warnings?.length) $('#collectionMessage').textContent += ` ${result.warnings.join(' ')}`;
      renderCandidates();
      await refresh({ silent: true });
    } catch (error) { $('#collectionMessage').textContent = error.message; }
  });
});
$('#dailyForm').addEventListener('submit', async event => {
  event.preventDefault();
  $('#dailyError').textContent = '';
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ daily: { enabled: $('#dailyEnabled').checked, time: $('#dailyTime').value } }) });
    await refresh({ silent: true });
    toast('每天同步时间已保存。');
  } catch (error) { $('#dailyError').textContent = error.message; }
});
$('#selectAllCandidates').addEventListener('change', event => { $$('.candidate-selected').forEach(input => { input.checked = event.target.checked; }); });
$('#candidateList').addEventListener('change', event => {
  if (!event.target.matches('.candidate-selected')) return;
  const boxes = $$('.candidate-selected');
  $('#selectAllCandidates').checked = boxes.every(input => input.checked);
  $('#selectAllCandidates').indeterminate = boxes.some(input => input.checked) && !boxes.every(input => input.checked);
});
$('#importCandidatesButton').addEventListener('click', async event => {
  try {
    const tasks = selectedCandidates();
    if (!tasks) return;
    if (!tasks.length) { toast('请至少勾选一项安排。'); return; }
    await busy(event.currentTarget, '正在加入…', async () => {
      const result = await api('/api/import', { method: 'POST', body: JSON.stringify({ tasks }) });
      candidates = [];
      renderCandidates();
      await refresh({ silent: true });
      $('#collectionMessage').textContent = `已确认加入 ${result.imported ?? tasks.length} 项安排。首次使用请前往设置同步 Apple 日历。`;
      toast('安排已加入。请查看设置中的日历同步状态。');
    });
  } catch (error) { $('#collectionMessage').textContent = error.message; toast(error.message); }
});
$('#importJSONButton').addEventListener('click', () => { $('#importJSONInput').value = ''; $('#importJSONInput').click(); });
$('#importJSONInput').addEventListener('change', async event => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    if (file.size > 1900000) throw new Error('文件过大，请使用 1.9 MB 以内的 JSON 备份。');
    const parsed = JSON.parse(await file.text());
    const tasks = Array.isArray(parsed) ? parsed : parsed.tasks;
    if (!Array.isArray(tasks) || !tasks.length) throw new Error('文件中没有可导入的 tasks 数组。');
    if (tasks.some(task => !task || typeof task !== 'object' || typeof task.title !== 'string' || !task.title.trim() || !validDate(task.due_at))) throw new Error('每项安排都需要 title 名称和有效的 due_at 截止时间。请先检查备份。');
    importTasks = tasks;
    $('#importSummary').textContent = `将导入 ${tasks.length} 项安排，以下时间均按北京时间显示。请核对后继续。`;
    $('#importPreview').innerHTML = tasks.map(task => `<div>${escapeHTML(task.title)}<small>${escapeHTML(task.course || '未填写课程')} · ${formatDate(task.due_at, { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })}</small></div>`).join('');
    $('#importError').textContent = '';
    $('#importDialog').showModal();
  } catch (error) { toast(error instanceof SyntaxError ? 'JSON 格式有误，请选择有效的备份文件。' : error.message); }
});
$('#confirmImportButton').addEventListener('click', async event => {
  await busy(event.currentTarget, '正在导入…', async () => {
    try {
      const result = await api('/api/import', { method: 'POST', body: JSON.stringify({ tasks: importTasks }) });
      $('#importDialog').close();
      await refresh({ silent: true });
      toast(`已导入 ${result.imported ?? importTasks.length} 项安排。请查看日历同步状态。`);
      importTasks = [];
    } catch (error) { $('#importError').textContent = error.message; }
  });
});
window.addEventListener('hashchange', () => setView(location.hash.slice(1)));
$('#copyDeviceURL').addEventListener('click', async () => {
  const input = $('#deviceURL');
  try { if (!navigator.clipboard?.writeText) throw new Error('manual'); await navigator.clipboard.writeText(input.value); toast('访问链接已复制，可发送给自己的设备。'); }
  catch { input.focus(); input.select(); toast('请复制已选中的链接，在自己的 iPhone 或 iPad 打开。'); }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh({ silent: true }); });
setInterval(() => {
  if (!document.hidden && currentView !== 'settings' && !document.querySelector('dialog[open]') && !document.activeElement.matches('input,textarea,select')) refresh({ silent: true });
}, 60000);
setView(location.hash.slice(1) || 'agenda');
refresh({ silent: true });
