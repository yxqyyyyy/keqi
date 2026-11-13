import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, test } from "node:test";

const environment = { KEQI_BRIDGE_KEY: "test-bridge-key-for-protocol-validation-only" };
globalThis.__keqiTestEnvironment = environment;
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "cloudflare:workers") return {
      url: "data:text/javascript,export const env = globalThis.__keqiTestEnvironment", shortCircuit: true,
    };
    if (specifier === "./keqi-model" && context.parentURL?.endsWith("/lib/keqi-sync.ts")) {
      return { url: new URL("./keqi-model.ts", context.parentURL).href, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});

const { addOperation, api, bridgeSync, mobileState, resolveOperation } = await import("../lib/keqi-sync.ts");
const migration = readFileSync(new URL("../drizzle/0000_short_chameleon.sql", import.meta.url), "utf8");

class Statement {
  constructor(db, sql, args = []) { this.db = db; this.sql = sql; this.args = args; }
  bind(...args) { return new Statement(this.db, this.sql, args); }
  async run() {
    const result = this.db.prepare(this.sql).run(...this.args);
    return { success: true, meta: { changes: Number(result.changes), last_row_id: Number(result.lastInsertRowid) } };
  }
  async first(column) {
    const row = this.db.prepare(this.sql).get(...this.args);
    return column ? row?.[column] ?? null : row ?? null;
  }
  async all() { return { success: true, results: this.db.prepare(this.sql).all(...this.args) }; }
}

class Database {
  constructor() { this.sqlite = new DatabaseSync(":memory:"); this.sqlite.exec(migration); }
  prepare(sql) { return new Statement(this.sqlite, sql); }
  async batch(statements) {
    this.sqlite.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
      this.sqlite.exec("COMMIT");
      return results;
    } catch (error) {
      this.sqlite.exec("ROLLBACK");
      throw error;
    }
  }
}

beforeEach(() => { environment.DB = new Database(); delete environment.KEQI_OWNER_ID; });
afterEach(() => environment.DB.sqlite.close());

const origin = "https://keqi.test";
const task = {
  id: "task-a", title: "作业", course: "课程", source: "xiji", kind: "assignment",
  start_at: null, due_at: "2026-11-14T10:00:00+08:00", status: "pending", revision: 1,
};
const operation = {
  op_id: "7f914a21-e4a1-4075-91a3-8918e383ad85", action: "patch", task_id: task.id,
  base_revision: 1, base: { status: "pending" }, changes: { status: "completed" },
};

function browser(path = "/api/mobile", body, user = "owner-a", headers = {}) {
  return new Request(origin + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "oai-authenticated-user-id": user, "content-type": "application/json", origin, ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
function sync(sequence, tasks = [task], receipts = [], extra = {}) {
  return bridgeSync(new Request(origin + "/api/bridge/sync", {
    method: "POST", headers: { "content-type": "application/json", "x-keqi-bridge-key": environment.KEQI_BRIDGE_KEY },
    body: JSON.stringify({ sequence, tasks, receipts, synced_at: "2026-11-12T12:00:00+08:00", ...extra }),
  }));
}
function rejectsStatus(action, status) { return assert.rejects(action, (error) => error.status === status); }

test("private owner bootstrap survives bridge-first startup and rejects another identity", async () => {
  await sync(1);
  const state = await mobileState(browser());
  assert.equal(state.bridge_connected, true);
  assert.equal(state.tasks[0].id, task.id);
  await rejectsStatus(() => mobileState(browser("/api/mobile", undefined, "other-user")), 403);
  const anonymous = new Request(origin + "/api/mobile");
  await rejectsStatus(() => mobileState(anonymous), 401);
  assert.equal((await environment.DB.prepare("SELECT owner_id FROM keqi_datasets").first()).owner_id, "owner-a");
});

test("explicit owner configuration also restricts the first claim", async () => {
  environment.KEQI_OWNER_ID = "owner-b";
  await rejectsStatus(() => mobileState(browser()), 403);
  assert.equal((await mobileState(browser("/api/mobile", undefined, "owner-b"))).tasks.length, 0);
});

test("queued changes survive Mac offline and two-phase acknowledgement updates the mirror atomically", async () => {
  await sync(1);
  const queued = await addOperation(browser("/api/mobile/operations", operation));
  assert.equal(queued.tasks[0].status, "completed");
  assert.equal(queued.canonical_tasks[0].status, "pending");
  assert.equal(queued.operations[0].status, "pending");
  const pull = await sync(2);
  assert.equal(pull.operations[0].op_id, operation.op_id);
  const current = { ...task, status: "completed", revision: 2 };
  const acknowledgement = await sync(3, [current], [{ op_id: operation.op_id, status: "applied", current }]);
  assert.equal(acknowledgement.operations.length, 0);
  const state = await mobileState(browser());
  assert.equal(state.tasks[0].status, "completed");
  assert.equal(state.canonical_tasks[0].revision, 2);
  assert.equal(state.operations.length, 0);
});

test("operation retries before and after acknowledgement are idempotent", async () => {
  await sync(1);
  await addOperation(browser("/api/mobile/operations", operation));
  await addOperation(browser("/api/mobile/operations", operation));
  assert.equal((await mobileState(browser())).operations.length, 1);
  await sync(2, [{ ...task, status: "completed", revision: 2 }], [{ op_id: operation.op_id, status: "applied" }]);
  const retried = await addOperation(browser("/api/mobile/operations", operation));
  assert.equal(retried.operation.status, "applied");
  assert.equal((await environment.DB.prepare("SELECT count(*) AS count FROM keqi_operations").first()).count, 1);
  await rejectsStatus(() => addOperation(browser("/api/mobile/operations", {
    ...operation, changes: { status: "pending" },
  })), 409);
});

test("old snapshots cannot roll back data or acknowledge pending edits", async () => {
  await sync(1);
  await addOperation(browser("/api/mobile/operations", operation));
  await sync(3, [{ ...task, title: "Mac的新标题", revision: 2 }]);
  await rejectsStatus(() => sync(2, [task], [{ op_id: operation.op_id, status: "applied" }]), 409);
  const state = await mobileState(browser());
  assert.equal(state.canonical_tasks[0].title, "Mac的新标题");
  assert.equal(state.operations[0].status, "pending");
  await rejectsStatus(() => sync(3, [task]), 409);
});

test("a database failure during acknowledgement rolls the mirror and operation back together", async () => {
  await sync(1);
  await addOperation(browser("/api/mobile/operations", operation));
  environment.DB.sqlite.exec("CREATE TRIGGER fail_ack BEFORE UPDATE ON keqi_operations WHEN NEW.status = 'applied' BEGIN SELECT RAISE(ABORT, 'forced failure'); END");
  await assert.rejects(() => sync(2, [{ ...task, status: "completed", revision: 2 }], [{ op_id: operation.op_id, status: "applied" }]));
  const state = await mobileState(browser());
  assert.equal(state.sequence, 1);
  assert.equal(state.canonical_tasks[0].status, "pending");
  assert.equal(state.operations[0].status, "pending");
});

test("conflicts show Mac data and can be discarded without changing that data", async () => {
  await sync(1);
  await addOperation(browser("/api/mobile/operations", operation));
  const current = { ...task, title: "另一台设备修改", revision: 2 };
  await sync(2, [current], [{ op_id: operation.op_id, status: "conflict", message: "安排已更新", current }]);
  const state = await mobileState(browser());
  assert.equal(state.tasks[0].status, "pending");
  assert.equal(state.operations[0].status, "conflict");
  assert.equal(state.operations[0].current.title, current.title);
  const resolved = await resolveOperation(browser("/api/mobile/resolve", { op_id: operation.op_id, action: "discard" }));
  assert.equal(resolved.operations.length, 0);
  assert.equal(resolved.tasks[0].title, current.title);
});

test("writes reject cross-origin requests and bridge endpoints reject missing credentials", async () => {
  await sync(1);
  await rejectsStatus(() => addOperation(browser("/api/mobile/operations", operation, "owner-a", { origin: "https://other.test" })), 403);
  const request = new Request(origin + "/api/bridge/sync", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  await rejectsStatus(() => bridgeSync(request), 401);
  const response = await api(() => mobileState(new Request(origin + "/api/mobile")));
  assert.equal(response.status, 401);
  assert.equal(response.headers.get("cache-control"), "private, no-store");
});
