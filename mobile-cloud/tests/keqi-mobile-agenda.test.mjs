import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { JsxEmit, ModuleKind, ScriptTarget, transpileModule } from "typescript";

const source = readFileSync(new URL("../app/mobile-client.tsx", import.meta.url), "utf8")
  + "\nexport { agendaTasks, countdown, fieldValue, isActionable, isUrgent };";
const compiled = transpileModule(source, {
  compilerOptions: { target: ScriptTarget.ES2022, module: ModuleKind.CommonJS, jsx: JsxEmit.ReactJSX },
}).outputText;
const context = { exports: {}, require: () => ({}), Intl, Date };
runInNewContext(compiled, context);
const { agendaTasks, countdown, fieldValue, isActionable, isUrgent } = context.exports;

const now = Date.parse("2026-11-15T10:00:00+08:00");
const task = (kind, values = {}) => ({
  id: kind, kind, title: "安排", course: "课程", status: "pending", deleted: false,
  due_at: "2026-11-15T09:00:00+08:00", start_at: null, ...values,
});
const ids = (tasks) => Array.from(tasks, item => item.id);

test("lectures leave the default agenda exactly at their start time", () => {
  const lecture = task("lecture", { due_at: "2026-11-15T10:00:00+08:00" });
  assert.equal(isActionable(lecture, now - 1), true);
  assert.equal(isUrgent(lecture, now - 1), true);
  assert.equal(isActionable(lecture, now), false);
  assert.equal(isUrgent(lecture, now), false);
  assert.equal(countdown(lecture, now), "已开课");
  assert.equal(lecture.status, "pending");
});

test("ongoing exams remain visible through the end boundary without becoming overdue", () => {
  const exam = task("exam", { start_at: "2026-11-15T09:00:00+08:00", due_at: "2026-11-15T11:00:00+08:00" });
  const end = Date.parse(exam.due_at);
  assert.equal(isActionable(exam, now), true);
  assert.equal(isUrgent(exam, now), false);
  assert.equal(countdown(exam, now), "考试进行中");
  assert.equal(isActionable(exam, end - 1), true);
  assert.equal(isActionable(exam, end), false);
  assert.equal(countdown(exam, end), "考试已结束");
  assert.equal(exam.status, "pending");
});

test("pre-exam urgency uses the start time and missing starts remain actionable", () => {
  const exam = task("exam", { start_at: "2026-11-16T10:00:00+08:00", due_at: "2026-11-16T12:00:00+08:00" });
  assert.equal(isUrgent(exam, now), true);
  assert.equal(countdown(exam, now), "24 小时后开考");
  const missingStart = { ...exam, start_at: null };
  assert.equal(isActionable(missingStart, now), true);
  assert.equal(isUrgent(missingStart, now), false);
  assert.equal(countdown(missingStart, now), "补充开考时间");
  assert.equal(isActionable({ ...missingStart, due_at: "2026-11-14T12:00:00+08:00" }, now), false);
});

test("only unfinished assignments remain overdue", () => {
  const assignment = task("assignment");
  assert.equal(isActionable(assignment, now), true);
  assert.equal(countdown(assignment, now), "已逾期");
  const completed = { ...assignment, status: "completed" };
  assert.equal(isActionable(completed, now), false);
  assert.equal(isUrgent(completed, now), false);
  assert.equal(countdown(completed, now), "已完成");
  assert.equal(isActionable({ ...assignment, deleted: true }, now), false);
});

test("the all view preserves elapsed records and the explicit completed view keeps source status", () => {
  const tasks = [
    task("assignment"),
    task("lecture"),
    task("exam", { start_at: "2026-11-15T08:00:00+08:00" }),
    task("lecture", { id: "completed-lecture", status: "completed" }),
    task("assignment", { id: "deleted", deleted: true }),
  ];
  const before = JSON.stringify(tasks);
  assert.deepEqual(ids(agendaTasks(tasks, "pending", "", now)), ["assignment"]);
  assert.deepEqual(ids(agendaTasks(tasks, "all", "", now)).sort(), ["assignment", "completed-lecture", "exam", "lecture"]);
  assert.deepEqual(ids(agendaTasks(tasks, "completed", "", now)), ["completed-lecture"]);
  assert.equal(JSON.stringify(tasks), before);
  assert.equal(agendaTasks(tasks, "all", "不存在", now).length, 0);
});

test("uncompleted lecture and exam status copy describes an arrangement", () => {
  assert.equal(fieldValue("status", "pending", "lecture"), "已安排");
  assert.equal(fieldValue("status", "pending", "exam"), "已安排");
  assert.equal(fieldValue("status", "pending", "assignment"), "待完成");
});
