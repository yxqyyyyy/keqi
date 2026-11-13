import assert from "node:assert/strict";
import test from "node:test";
import {
  canonicalJson, dateTime, normalizeOperation, overlayTasks, validateOperation, validateTask,
} from "../lib/keqi-model.ts";

const task = validateTask({
  id: "task-a", title: "作业", course: "课程", source: "xiji", kind: "assignment",
  start_at: null, due_at: "2026-11-14T10:00:00+08:00", status: "pending", revision: 5,
});
const opId = "7f914a21-e4a1-4075-91a3-8918e383ad85";
const patch = (changes, base) => ({ op_id: opId, task_id: task.id, action: "patch", base_revision: 5, changes, base });

test("dates match the Mac store and reject rollover or missing timezone", () => {
  assert.equal(dateTime("2026-11-14T02:00:00Z"), "2026-11-14T10:00:00+08:00");
  for (const value of ["2026-02-30T10:00+08:00", "2026-13-01T10:00+08:00", "2026-11-14T25:00+08:00", "2026-11-14T10:00", "0000-11-14T10:00+08:00"]) {
    assert.throws(() => dateTime(value));
  }
});

test("the mobile editor cannot change platform identity or inject arbitrary fields", () => {
  for (const changes of [{ source: "manual" }, { source_key: "other" }, { calendar_id: "private" }, { revision: 100 }, { deleted: true }, { cookie: "session" }]) {
    assert.throws(() => validateOperation(patch(changes, changes), [task]));
  }
  assert.throws(() => validateOperation(patch({ url: "javascript:alert(1)" }, { url: "" }), [task]));
});

test("patches require exact base fields and validate the resulting task", () => {
  assert.throws(() => validateOperation(patch({ title: "新标题" }, {}), [task]));
  assert.throws(() => validateOperation(patch({ title: "新标题" }, { title: "作业", status: "pending" }), [task]));
  assert.throws(() => validateOperation(patch({ start_at: "2026-11-15T10:00+08:00" }, { start_at: null }), [task]));
  const item = validateOperation(patch({ status: "completed" }, { status: "pending" }), [task]);
  assert.deepEqual(item.changes, { status: "completed" });
});

test("creates are manual and immutable payloads normalize for safe retry", () => {
  const raw = { op_id: opId, task_id: "new-task", action: "create", changes: { title: " 新安排 ", due_at: "2026-11-14T02:00Z" }, base: {} };
  const item = validateOperation(raw, [task]);
  assert.equal(item.changes.source, "manual");
  assert.equal(item.changes.title, "新安排");
  assert.equal(item.changes.due_at, "2026-11-14T10:00:00+08:00");
  assert.equal(canonicalJson(normalizeOperation(raw)), canonicalJson(item));
  assert.throws(() => validateOperation({ ...raw, changes: { ...raw.changes, source: "xiji" } }, []));
  assert.throws(() => validateOperation({ ...raw, task_id: task.id }, [task]));
});

test("pending operations overlay in order while conflicts retain Mac content", () => {
  const first = { ...validateOperation(patch({ title: "新标题" }, { title: task.title }), [task]), status: "pending" };
  const second = { ...first, op_id: "2874d853-70a8-47b4-958b-52d69c56b43a", changes: { status: "completed" }, base: { status: "pending" } };
  const merged = overlayTasks([task], [first, second]);
  assert.equal(merged[0].title, "新标题");
  assert.equal(merged[0].status, "completed");
  assert.equal(task.title, "作业");
  assert.equal(overlayTasks([task], [{ ...first, status: "conflict" }])[0].title, "作业");
  assert.equal(overlayTasks([task], [{ ...first, action: "delete" }]).length, 0);
});

test("tombstones stay hidden and snapshots drop unknown private metadata", () => {
  const sanitized = validateTask({ ...task, deleted: true, source_key: "platform-id", calendar_id: "calendar-id", credentials: "never-copy" });
  assert.equal("credentials" in sanitized, false);
  assert.equal("calendar_id" in sanitized, false);
  assert.deepEqual(overlayTasks([sanitized], []), []);
});

test("canonical serialization is independent of object key insertion order", () => {
  assert.equal(canonicalJson({ b: [2, { c: 3, a: 1 }], a: 0 }), canonicalJson({ a: 0, b: [2, { a: 1, c: 3 }] }));
});
