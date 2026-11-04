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
