import { env } from "cloudflare:workers";
import {
  ApiError, canonicalJson, dateTime, identifier, normalizeOperation, object, overlayTasks,
  validateOperation, validateTask,
  type Operation, type Task,
} from "./keqi-model";

const DATASET_ID = "primary";
const MAX_BODY_BYTES = 2_000_000;
const MAX_ACTIVE_OPERATIONS = 1000;
type Dataset = {
  id: string;
  owner_id: string | null;
  sequence: number;
  tasks_json: string;
  snapshot_hash: string;
  last_mac_sync_at: string | null;
  platform_synced_at: string | null;
};
type OperationRow = {
  seq: number;
  op_id: string;
  payload_json: string;
  status: string;
  message: string | null;
  current_json: string | null;
  created_at: string;
};

function database(): D1Database {
  if (!env.DB) throw new ApiError(503, "云端存储尚未连接");
  return env.DB;
}

export function json(value: unknown, status = 200): Response {
  return Response.json(value, {
    status,
    headers: { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" },
  });
}

export async function api(action: () => Promise<unknown>): Promise<Response> {
  try {
    return json(await action());
  } catch (error) {
    if (error instanceof ApiError) return json({ error: error.message }, error.status);
    return json({ error: "云端操作未完成，请稍后重试" }, 500);
  }
}

export async function readJson(request: Request, browser = true): Promise<Record<string, unknown>> {
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") throw new ApiError(415, "请求需要使用 JSON");
  if (browser && request.headers.get("origin") !== new URL(request.url).origin) throw new ApiError(403, "拒绝跨站修改");
  const advertised = Number(request.headers.get("content-length") ?? 0);
  if (!Number.isFinite(advertised) || advertised > MAX_BODY_BYTES || advertised < 0) throw new ApiError(413, "内容过大");
  const reader = request.body?.getReader();
  if (!reader) throw new ApiError(400, "请求内容为空");
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new ApiError(413, "内容过大");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try { return object(JSON.parse(new TextDecoder().decode(bytes))); } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(400, "请求内容无法读取");
  }
}

async function dataset(): Promise<Dataset> {
  const db = database();
  await db.prepare("INSERT OR IGNORE INTO keqi_datasets (id) VALUES (?)").bind(DATASET_ID).run();
  const row = await db.prepare("SELECT * FROM keqi_datasets WHERE id = ?").bind(DATASET_ID).first<Dataset>();
  if (!row) throw new ApiError(503, "云端数据暂不可用");
  return row;
}

async function browserOwner(request: Request): Promise<Dataset> {
  const userId = request.headers.get("oai-authenticated-user-id");
  if (!userId || userId.length > 300) throw new ApiError(401, "请先登录后再打开手机安排");
  if (env.KEQI_OWNER_ID && env.KEQI_OWNER_ID !== userId) throw new ApiError(403, "此安排仅供本人访问");
  // The hosted Site must remain owner-private. Its access boundary authorizes
  // the first visitor to claim this dataset. Later identities cannot claim it.
  await dataset();
  await database().prepare("UPDATE keqi_datasets SET owner_id = ? WHERE id = ? AND owner_id IS NULL").bind(userId, DATASET_ID).run();
  const current = await dataset();
  if (current.owner_id !== userId) throw new ApiError(403, "此安排仅供本人访问");
  return current;
}

async function bridge(request: Request): Promise<void> {
  const expected = env.KEQI_BRIDGE_KEY;
  if (!expected || expected.length < 32) throw new ApiError(503, "Mac 连接尚未配置");
  const candidate = request.headers.get("x-keqi-bridge-key") ?? "";
  if (candidate.length > 1000) throw new ApiError(401, "Mac 连接凭据无效");
  const digest = async (text: string) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
  const [left, right] = await Promise.all([digest(expected), digest(candidate)]);
  let difference = 0;
  for (let i = 0; i < left.length; i++) difference |= left[i] ^ right[i];
  if (difference !== 0) throw new ApiError(401, "Mac 连接凭据无效");
}

function operation(row: OperationRow): Operation {
  return { ...JSON.parse(row.payload_json), seq: row.seq, status: row.status,
    message: row.message, current: row.current_json ? JSON.parse(row.current_json) : null, created_at: row.created_at };
}

async function activeOperations(): Promise<Operation[]> {
  const result = await database().prepare(
    "SELECT * FROM keqi_operations WHERE dataset_id = ? AND status IN ('pending', 'conflict', 'invalid') ORDER BY seq LIMIT ?",
  ).bind(DATASET_ID, MAX_ACTIVE_OPERATIONS).all<OperationRow>();
  return result.results.map(operation);
}

async function view(current: Dataset) {
  const tasks = JSON.parse(current.tasks_json) as Task[];
  const operations = await activeOperations();
  return {
    tasks: overlayTasks(tasks, operations), canonical_tasks: tasks.filter((task) => !task.deleted), operations,
    last_mac_sync_at: current.last_mac_sync_at, platform_synced_at: current.platform_synced_at,
    bridge_connected: current.sequence > 0, sequence: current.sequence,
  };
}

export async function mobileState(request: Request) {
  return view(await browserOwner(request));
}

export async function addOperation(request: Request) {
  const current = await browserOwner(request);
  const raw = await readJson(request);
  const opId = identifier(raw.op_id);
  const db = database();
  const existing = await db.prepare("SELECT * FROM keqi_operations WHERE dataset_id = ? AND op_id = ?").bind(DATASET_ID, opId).first<OperationRow>();
  if (existing) {
    const stored = JSON.parse(existing.payload_json) as Operation;
    const candidate = normalizeOperation(raw);
    if (canonicalJson(stored) !== canonicalJson(candidate)) throw new ApiError(409, "修改记录标识已用于其他内容");
    return { operation: operation(existing), ...await view(current) };
  }
  const active = await activeOperations();
  const item = validateOperation(raw, overlayTasks(JSON.parse(current.tasks_json), active));
  const createdAt = new Date().toISOString();
  const payload = canonicalJson(item);
  await db.prepare(
    "INSERT OR IGNORE INTO keqi_operations (dataset_id, op_id, payload_json, created_at) SELECT ?, ?, ?, ? WHERE (SELECT count(*) FROM keqi_operations WHERE dataset_id = ? AND status IN ('pending', 'conflict', 'invalid')) < ? AND (SELECT count(*) FROM keqi_operations WHERE dataset_id = ? AND created_at > ?) < 1000",
  ).bind(DATASET_ID, opId, payload, createdAt, DATASET_ID, MAX_ACTIVE_OPERATIONS, DATASET_ID, new Date(Date.now() - 3600_000).toISOString()).run();
  const saved = await db.prepare("SELECT * FROM keqi_operations WHERE dataset_id = ? AND op_id = ?").bind(DATASET_ID, opId).first<OperationRow>();
  if (!saved) throw new ApiError(429, "待处理修改较多，请等待 Mac 同步后继续");
  if (saved.payload_json !== payload) throw new ApiError(409, "修改记录标识已用于其他内容");
  return { operation: operation(saved), ...await view(await dataset()) };
}

export async function resolveOperation(request: Request) {
  await browserOwner(request);
  const raw = await readJson(request);
  const opId = identifier(raw.op_id);
  if (raw.action !== "discard") throw new ApiError(400, "请选择放弃此条修改");
  const result = await database().prepare(
    "UPDATE keqi_operations SET status = 'discarded', resolved_at = ? WHERE dataset_id = ? AND op_id = ? AND status IN ('conflict', 'invalid')",
  ).bind(new Date().toISOString(), DATASET_ID, opId).run();
  if (!result.meta.changes) {
    const existing = await database().prepare("SELECT status FROM keqi_operations WHERE dataset_id = ? AND op_id = ?").bind(DATASET_ID, opId).first<{ status: string }>();
    if (!existing) throw new ApiError(404, "修改记录不存在");
    if (existing.status !== "discarded") throw new ApiError(409, "Mac 尚未处理此条修改，请同步后再检查");
  }
  return view(await dataset());
}

export async function bridgeSync(request: Request) {
  await bridge(request);
  const raw = await readJson(request, false);
  if (!Number.isSafeInteger(raw.sequence) || (raw.sequence as number) < 1) throw new ApiError(400, "同步序号无效");
  if (!Array.isArray(raw.tasks) || raw.tasks.length > 5000 || !Array.isArray(raw.receipts) || raw.receipts.length > 500) throw new ApiError(400, "同步内容数量无效");
  const tasks = raw.tasks.map(validateTask);
  if (new Set(tasks.map((task) => task.id)).size !== tasks.length) throw new ApiError(400, "同步内容包含重复安排");
  const syncedAt = dateTime(raw.synced_at);
  const platformSyncedAt = dateTime(raw.platform_synced_at, true);
  const tasksJson = canonicalJson(tasks);
  const hashBytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(tasksJson)));
  const hash = Array.from(hashBytes).map((value) => value.toString(16).padStart(2, "0")).join("");
  const current = await dataset();
  const sequence = raw.sequence as number;
  if (current.sequence > sequence || (current.sequence === sequence && current.snapshot_hash !== hash)) throw new ApiError(409, "同步序号已过期，请重新读取后重试");
  const db = database();
  const statements: D1PreparedStatement[] = [db.prepare(
    "UPDATE keqi_datasets SET sequence = ?, tasks_json = ?, snapshot_hash = ?, last_mac_sync_at = ?, platform_synced_at = ? WHERE id = ? AND (sequence < ? OR (sequence = ? AND snapshot_hash = ?))",
  ).bind(sequence, tasksJson, hash, syncedAt, platformSyncedAt, DATASET_ID, sequence, sequence, hash)];
  for (const value of raw.receipts) {
    const receipt = object(value);
    const opId = identifier(receipt.op_id);
    if (!["applied", "conflict", "invalid"].includes(receipt.status as string)) throw new ApiError(400, "修改回执状态无效");
    const message = receipt.message == null ? null : String(receipt.message).slice(0, 600);
    const resultTask = receipt.current == null ? null : canonicalJson(validateTask(receipt.current));
    statements.push(db.prepare(
      "UPDATE keqi_operations SET status = ?, message = ?, current_json = ?, resolved_at = ? WHERE dataset_id = ? AND op_id = ? AND status = 'pending' AND EXISTS (SELECT 1 FROM keqi_datasets WHERE id = ? AND sequence = ? AND snapshot_hash = ?)",
    ).bind(receipt.status, message, resultTask, syncedAt, DATASET_ID, opId, DATASET_ID, sequence, hash));
  }
  await db.batch(statements);
  const saved = await dataset();
  if (saved.sequence !== sequence || saved.snapshot_hash !== hash) throw new ApiError(409, "有较新的同步正在完成，请稍后重试");
  const pending = await db.prepare("SELECT * FROM keqi_operations WHERE dataset_id = ? AND status = 'pending' ORDER BY seq LIMIT 500").bind(DATASET_ID).all<OperationRow>();
  return { operations: pending.results.map(operation), sequence: saved.sequence, server_time: new Date().toISOString() };
}
