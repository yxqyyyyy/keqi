export type Task = {
  id: string;
  title: string;
  course: string;
  source: "manual" | "chaoxing" | "xiji" | "physics";
  kind: "assignment" | "exam" | "lecture";
  start_at: string | null;
  due_at: string;
  status: "pending" | "completed";
  url: string;
  notes: string;
  revision: number;
  deleted?: boolean;
  updated_at?: string;
};

export type Changes = Partial<Pick<Task, "title" | "course" | "kind" | "start_at" | "due_at" | "status" | "url" | "notes">>;
export type Operation = {
  op_id: string;
  action: "create" | "patch" | "delete";
  task_id: string;
  base_revision: number | null;
  base: Changes;
  changes: Changes & { source?: "manual" };
  seq?: number;
  status?: string;
  created_at?: string;
  message?: string | null;
  current?: Task | null;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const EDITABLE = new Set(["title", "course", "kind", "start_at", "due_at", "status", "url", "notes"]);
const SOURCES = new Set(["manual", "chaoxing", "xiji", "physics"]);
const KINDS = new Set(["assignment", "exam", "lecture"]);
const LIMITS: Record<string, number> = { title: 300, course: 200, url: 2000, notes: 6000 };

export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError(400, "请求必须是对象");
  return value as Record<string, unknown>;
}

export function identifier(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(value)) throw new ApiError(400, "安排标识无效");
  return value;
}

export function dateTime(value: unknown, nullable = false): string | null {
  if (nullable && (value === null || value === "" || value === undefined)) return null;
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new ApiError(400, "日期时间需要包含时区");
  }
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(value)!;
  const [year, month, day, hour, minute, second] = parts.slice(1, 7).map((part) => Number(part ?? 0));
  const maxDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > maxDay || hour > 23 || minute > 59 || second > 59 || Number(parts[8] ?? 0) > 23 || Number(parts[9] ?? 0) > 59) {
    throw new ApiError(400, "日期时间无效");
  }
  const time = new Date(value);
  if (!Number.isFinite(time.getTime())) throw new ApiError(400, "日期时间无效");
  // Match the Mac store's canonical Beijing offset and second precision.
  const beijing = new Date(time.getTime() + 8 * 3600_000).toISOString();
  if (!/^\d{4}-/.test(beijing)) throw new ApiError(400, "日期时间超出支持范围");
  return beijing.slice(0, 19) + "+08:00";
}

function normalizeFields(raw: Record<string, unknown>, allowSource = false): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (allowSource && key === "source" && value === "manual") {
      result.source = "manual";
      continue;
    }
    if (!EDITABLE.has(key)) throw new ApiError(400, "包含不可修改的字段");
    if (key in LIMITS) {
      if (typeof value !== "string" || value.length > LIMITS[key]) throw new ApiError(400, "文字内容过长或格式错误");
      result[key] = value.trim();
    } else if (key === "kind") {
      if (!KINDS.has(value as string)) throw new ApiError(400, "安排类型无效");
      result[key] = value;
    } else if (key === "status") {
      if (value !== "pending" && value !== "completed") throw new ApiError(400, "完成状态无效");
      result[key] = value;
    } else {
      result[key] = dateTime(value, key === "start_at");
    }
  }
  if ("title" in result && !result.title) throw new ApiError(400, "请填写安排名称");
  if (result.url) {
    let url: URL;
    try { url = new URL(result.url as string); } catch { throw new ApiError(400, "链接格式无效"); }
    if (!["http:", "https:"].includes(url.protocol)) throw new ApiError(400, "链接只支持 http 或 https");
  }
  return result;
}

export function validateTask(value: unknown): Task {
  const raw = object(value);
  const fields = normalizeFields(Object.fromEntries([...EDITABLE].filter((key) => key in raw).map((key) => [key, raw[key]])));
  const item = {
    title: "", course: "", kind: "assignment", start_at: null,
    status: "pending", url: "", notes: "", ...fields,
    id: identifier(raw.id), source: raw.source ?? "manual", revision: raw.revision ?? 1,
  } as Task;
  if (!item.title || !item.due_at) throw new ApiError(400, "安排名称和截止时间不能为空");
  if (!SOURCES.has(item.source) || !Number.isSafeInteger(item.revision) || item.revision < 1) throw new ApiError(400, "安排版本或来源无效");
  if (item.start_at && Date.parse(item.start_at) > Date.parse(item.due_at)) throw new ApiError(400, "开始时间不能晚于截止时间");
  if (item.kind === "lecture" && item.start_at && item.start_at !== item.due_at) throw new ApiError(400, "上课安排的提醒时间需要与开课时间一致");
  if (raw.deleted === true) item.deleted = true;
  if (typeof raw.updated_at === "string") item.updated_at = dateTime(raw.updated_at) as string;
  return item;
}

export function normalizeOperation(value: unknown): Operation {
  const raw = object(value);
  const opId = identifier(raw.op_id);
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(opId)) throw new ApiError(400, "修改记录标识无效");
  if (!["create", "patch", "delete"].includes(raw.action as string)) throw new ApiError(400, "修改操作无效");
  const action = raw.action as Operation["action"];
  const taskId = identifier(raw.task_id);
  const changes = normalizeFields(object(raw.changes ?? {}), action === "create") as Operation["changes"];
  const base = normalizeFields(object(raw.base ?? {})) as Changes;
  if (action === "create") {
    validateTask({ ...changes, id: taskId, source: "manual" });
    if (Object.keys(base).length) throw new ApiError(400, "新安排无需原始内容");
    return { op_id: opId, action, task_id: taskId, base_revision: null, base: {}, changes: { ...changes, source: "manual" } };
  }
  if (!Number.isSafeInteger(raw.base_revision) || (raw.base_revision as number) < 1) throw new ApiError(400, "缺少原始版本");
  if (action === "delete") {
    if (Object.keys(changes).length) throw new ApiError(400, "删除操作无需新内容");
  } else {
    const keys = Object.keys(changes);
    if (!keys.length || keys.some((key) => !Object.hasOwn(base, key))) throw new ApiError(400, "请提供修改字段的原始内容");
    if (Object.keys(base).some((key) => !Object.hasOwn(changes, key))) throw new ApiError(400, "原始字段与修改字段不一致");
  }
  return { op_id: opId, action, task_id: taskId, base_revision: raw.base_revision as number, base, changes };
}

export function validateOperation(value: unknown, tasks: Task[]): Operation {
  const item = normalizeOperation(value);
  const current = tasks.find((task) => task.id === item.task_id);
  if (item.action === "create") {
    if (current) throw new ApiError(409, "此安排已存在，请刷新后编辑");
  } else {
    if (!current || current.deleted) throw new ApiError(409, "此安排已删除，请刷新后检查");
    if (item.action === "patch") validateTask({ ...current, ...item.changes });
  }
  return item;
}

export function overlayTasks(canonical: Task[], operations: Operation[]): Task[] {
  const tasks = new Map(canonical.map((task) => [task.id, { ...task }]));
  for (const operation of operations) {
    if (operation.status !== "pending") continue;
    if (operation.action === "delete") {
      tasks.delete(operation.task_id);
    } else if (operation.action === "create") {
      tasks.set(operation.task_id, validateTask({ ...operation.changes, id: operation.task_id, source: "manual" }));
    } else {
      const current = tasks.get(operation.task_id);
      if (!current || current.deleted) continue;
      // An earlier rejected operation can make a later edit temporarily invalid.
      // Its immutable record remains available for resolution after the Mac replies.
      try { tasks.set(operation.task_id, validateTask({ ...current, ...operation.changes, revision: current.revision + 1 })); } catch { /* Keep the last valid task. */ }
    }
  }
  return [...tasks.values()].filter((task) => !task.deleted);
}

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    return "{" + Object.keys(item).sort().map((key) => JSON.stringify(key) + ":" + canonicalJson(item[key])).join(",") + "}";
  }
  const serialized = JSON.stringify(value);
  if (serialized === undefined) throw new ApiError(400, "内容无法序列化");
  return serialized;
}
