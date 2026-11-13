import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { JsxEmit, ModuleKind, ScriptTarget, transpileModule } from "typescript";

const source = readFileSync(new URL("../app/mobile-client.tsx", import.meta.url), "utf8")
  + "\nexport { jsonRequest, RequestFailure };";
const compiled = transpileModule(source, {
  compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.CommonJS, jsx: JsxEmit.ReactJSX },
}).outputText;

function client(fetcher) {
  let onTimeout;
  let timeoutMs;
  let cleared = false;
  const context = {
    exports: {}, require: () => ({}), Intl, Date, AbortController,
    fetch: fetcher,
    setTimeout(callback, delay) { onTimeout = callback; timeoutMs = delay; return 1; },
    clearTimeout(id) { assert.equal(id, 1); cleared = true; },
  };
  runInNewContext(compiled, context);
  return {
    request: context.exports.jsonRequest,
    timeout() { assert.equal(timeoutMs, 15000); onTimeout(); },
    isCleared: () => cleared,
  };
}

function aborted(signal) {
  return new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
  });
}

test("a stalled fetch aborts after 15 seconds with a network error", async () => {
  const api = client((_path, options) => aborted(options.signal));
  const request = api.request("/api/mobile");
  api.timeout();
  await assert.rejects(request, error => /连接超时/.test(error.message) && error.status === undefined);
  assert.equal(api.isCleared(), true);
});

test("the same deadline covers a stalled response body", async () => {
  let notifyBody;
  const bodyStarted = new Promise(resolve => { notifyBody = resolve; });
  const api = client(async (_path, options) => ({
    status: 200, ok: true, headers: new Headers({ "content-type": "application/json" }),
    json() { notifyBody(); return aborted(options.signal); },
  }));
  const request = api.request("/api/mobile");
  await bodyStarted;
  api.timeout();
  await assert.rejects(request, /连接超时/);
  assert.equal(api.isCleared(), true);
});

test("an HTML 403 reports a network gateway block", async () => {
  const api = client(async () => new Response("Blocked", { status: 403, headers: { "content-type": "text/html" } }));
  await assert.rejects(api.request("/api/mobile"), error => /网站网关拦截/.test(error.message) && error.status === undefined);
  assert.equal(api.isCleared(), true);
});

test("a JSON 403 retains the owner permission error", async () => {
  const api = client(async () => new Response('{"error":"denied"}', { status: 403, headers: { "content-type": "application/json" } }));
  await assert.rejects(api.request("/api/mobile"), error => /账号没有访问/.test(error.message) && error.status === 403);
  assert.equal(api.isCleared(), true);
});

test("a login HTML response reports interrupted access", async () => {
  const api = client(async () => new Response("Sign in", { headers: { "content-type": "text/html" } }));
  await assert.rejects(api.request("/api/mobile"), error => /连接被登录或访问网关中断/.test(error.message) && error.status === undefined);
});

test("a complete JSON state cancels the timeout", async () => {
  const state = { tasks: [], canonical_tasks: [], operations: [], bridge_connected: true, last_mac_sync_at: null };
  const api = client(async () => Response.json(state));
  assert.deepEqual(await api.request("/api/mobile"), state);
  assert.equal(api.isCleared(), true);
});
