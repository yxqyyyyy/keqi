"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Check, CheckCheck, ChevronRight, Clock3, Plus, RefreshCw, Search, X } from "lucide-react";
import type { Task, Operation, Changes as EditableChanges } from "@/lib/keqi-model";

type Changes = EditableChanges & { source?: "manual" };
type AgendaTab = "pending" | "completed" | "all";
type MobileState = {
  tasks: Task[];
  canonical_tasks: Task[];
  operations: Operation[];
  last_mac_sync_at: string | null;
  platform_synced_at?: string | null;
  bridge_connected: boolean;
};
type Draft = {
  title: string;
  course: string;
  kind: Task["kind"];
  start: string;
  due: string;
  url: string;
  notes: string;
  completed: boolean;
};

const KIND_LABEL = { assignment: "作业", exam: "考试", lecture: "上课" };
const FIELD_LABEL: Record<string, string> = { title: "名称", course: "课程", kind: "类型", start_at: "开始时间", due_at: "结束／截止时间", status: "完成状态", url: "链接", notes: "备注" };
const DATE_FORMAT = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const FULL_DATE_FORMAT = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const INPUT_FORMAT = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

function validTime(value: string | null | undefined) {
  if (!value) return null;
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : null;
}

function anchor(task: Task) {
  return validTime(task.kind === "exam" ? task.start_at : task.due_at);
}

function dateLabel(value: string | null | undefined, full = false) {
  const time = validTime(value);
  return time === null ? "暂无记录" : (full ? FULL_DATE_FORMAT : DATE_FORMAT).format(time);
}

function inputDate(value: string | null) {
  const time = validTime(value);
  return time === null ? "" : INPUT_FORMAT.format(time).replace(" ", "T");
}

function isoDate(value: string) {
  if (!value) return null;
  return `${value.length === 16 ? `${value}:00` : value}+08:00`;
}

function emptyDraft(): Draft {
  return { title: "", course: "", kind: "assignment", start: "", due: "", url: "", notes: "", completed: false };
}

function draftFor(task: Task): Draft {
  return { title: task.title, course: task.course, kind: task.kind, start: inputDate(task.start_at), due: inputDate(task.due_at), url: task.url || "", notes: task.notes || "", completed: task.status === "completed" };
}

function sameValue(left: unknown, right: unknown, field: string) {
  if (field === "start_at" || field === "due_at") {
    if (!left && !right) return true;
    return validTime(left as string) === validTime(right as string);
  }
  return left === right;
}

function patchFor(task: Task, values: Changes): Operation | null {
  const changes: Changes = {};
  const base: Operation["base"] = {};
  for (const field of Object.keys(values) as (keyof Changes)[]) {
    const oldValue = task[field as keyof Task];
    if (!sameValue(oldValue, values[field], field)) {
      Object.assign(changes, { [field]: values[field] });
      Object.assign(base, { [field]: oldValue ?? null });
    }
  }
  if (!Object.keys(changes).length) return null;
  return { op_id: crypto.randomUUID(), action: "patch", task_id: task.id, base_revision: task.revision, base, changes };
}

function fieldValue(field: string, value: unknown, kind: Task["kind"] = "assignment") {
  if (field === "start_at" || field === "due_at") return value ? dateLabel(String(value), true) : "未填写";
  if (field === "status") return value === "completed" ? "已完成" : kind === "assignment" ? "待完成" : "已安排";
  if (field === "kind") return KIND_LABEL[value as Task["kind"]] || String(value);
  return value === null || value === undefined || value === "" ? "未填写" : String(value);
}

class RequestFailure extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

async function jsonRequest(path: string, body?: unknown): Promise<MobileState> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.headers.get("content-type")?.includes("application/json")) {
      if (response.status === 403) throw new Error("访问被网站网关拦截，请检查当前网络或稍后重试。");
      throw new Error("连接被登录或访问网关中断，请在浏览器中重新打开页面后重试。");
    }
    if (response.status === 401) throw new RequestFailure("登录已过期，请重新登录后继续。", 401);
    if (response.status === 403) throw new RequestFailure("当前账号没有访问这份安排的权限。", 403);
    const result = await response.json() as Partial<MobileState> & { error?: string };
    if (!response.ok) throw new RequestFailure(result?.error || "保存未完成，请稍后重试。", response.status);
    if (!result || !Array.isArray(result.tasks) || !Array.isArray(result.canonical_tasks) || !Array.isArray(result.operations) || typeof result.bridge_connected !== "boolean") {
      throw new Error("读取到的安排格式不完整，请刷新后重试。");
    }
    return result as MobileState;
  } catch (failure) {
    if (controller.signal.aborted) throw new Error("连接超时，请检查当前网络后重试。");
    throw failure;
  } finally {
    clearTimeout(timer);
  }
}

function countdown(task: Task, now: number) {
  if (task.kind === "assignment" && task.status === "completed") return "已完成";
  const time = anchor(task);
  if (time === null) return "补充开考时间";
  const remaining = time - now;
  if (remaining <= 0) {
    if (task.kind === "exam") return (validTime(task.due_at) ?? 0) > now ? "考试进行中" : "考试已结束";
    return task.kind === "lecture" ? "已开课" : "已逾期";
  }
  const verb = task.kind === "exam" ? "开考" : task.kind === "lecture" ? "开课" : "截止";
  if (remaining < 3600000) return `${Math.ceil(remaining / 60000)} 分钟后${verb}`;
  if (remaining <= 86400000) return `${Math.ceil(remaining / 3600000)} 小时后${verb}`;
  return "";
}

function isActionable(task: Task, now: number) {
  if (task.deleted || task.status === "completed") return false;
  if (task.kind === "assignment") return true;
  const end = validTime(task.due_at);
  return end === null || end > now;
}

function isUrgent(task: Task, now: number) {
  const time = anchor(task);
  return isActionable(task, now) && time !== null && time > now && time - now <= 86400000;
}

function agendaTasks(tasks: Task[], tab: AgendaTab, search: string, now: number) {
  const query = search.trim().toLocaleLowerCase();
  return tasks.filter(task => !task.deleted
    && (tab === "all" || (tab === "pending" ? isActionable(task, now) : task.status === "completed"))
    && (!query || `${task.title} ${task.course}`.toLocaleLowerCase().includes(query)))
    .sort((a, b) => (anchor(a) ?? Infinity) - (anchor(b) ?? Infinity));
}

export default function MobileClient() {
  const [data, setData] = useState<MobileState | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<AgendaTab>("pending");
  const [search, setSearch] = useState("");
  const [now, setNow] = useState(Date.now());
  const [editing, setEditing] = useState<Task | null | undefined>(undefined);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [formError, setFormError] = useState("");
  const [deleting, setDeleting] = useState<Task | null>(null);
  const [toast, setToast] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const deleteDialog = useRef<HTMLDialogElement>(null);
  const fetchVersion = useRef(0);
  const retryOperation = useRef<Operation | null>(null);

  const refresh = useCallback(async (visible = false) => {
    const version = ++fetchVersion.current;
    if (visible) setRefreshing(true);
    try {
      const state = await jsonRequest("/api/mobile");
      if (version === fetchVersion.current) { setData(state); setError(""); }
      return state;
    } catch (failure) {
      if (version === fetchVersion.current) setError(failure instanceof Error ? failure.message : "暂时无法连接，请检查网络。");
      return null;
    } finally {
      if (visible) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      setNow(Date.now());
      if (document.visibilityState === "visible") void refresh();
    }, 30000);
    const onVisible = () => {
      if (document.visibilityState === "visible") { setNow(Date.now()); void refresh(); }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); ++fetchVersion.current; };
  }, [refresh]);

  useEffect(() => {
    if (editing !== undefined && dialog.current && !dialog.current.open) dialog.current.showModal();
    if (editing === undefined) dialog.current?.close();
  }, [editing]);

  useEffect(() => {
    if (deleting && deleteDialog.current && !deleteDialog.current.open) deleteDialog.current.showModal();
    if (!deleting) deleteDialog.current?.close();
  }, [deleting]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 4500);
    return () => clearTimeout(timer);
  }, [toast]);

  function openEditor(task: Task | null) {
    setDraft(task ? draftFor(task) : emptyDraft());
    setFormError("");
    retryOperation.current = null;
    setEditing(task);
  }

  async function sendOperation(operation: Operation) {
    retryOperation.current = operation;
    try {
      const state = await jsonRequest("/api/mobile/operations", operation);
      ++fetchVersion.current;
      setData(state);
      setError("");
    } catch (failure) {
      if (failure instanceof RequestFailure && failure.status < 500) {
        retryOperation.current = null;
        throw failure;
      }
      // Read back the immutable ID after an uncertain network result.
      const state = await refresh();
      if (!state?.operations.some(item => item.op_id === operation.op_id)) throw failure;
    }
    retryOperation.current = null;
    setToast("已保存到云端，等待 Mac 接收");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setFormError("");
    try {
      const values: Changes = {
        title: draft.title.trim(), course: draft.course.trim(), kind: draft.kind,
        start_at: draft.kind === "lecture" ? isoDate(draft.due) : isoDate(draft.start),
        due_at: isoDate(draft.due)!, url: draft.url.trim(), notes: draft.notes,
        status: draft.completed ? "completed" : "pending",
      };
      if (!values.title) throw new Error("请填写安排名称。");
      if (!draft.due) throw new Error(draft.kind === "exam" ? "请填写考试结束时间。" : "请填写提醒时间。");
      if (values.start_at && validTime(values.start_at)! > validTime(values.due_at)!) throw new Error("开始时间不能晚于结束／截止时间。");
      const operation = retryOperation.current ?? (editing ? patchFor(editing, values) : {
        op_id: crypto.randomUUID(), action: "create" as const, task_id: crypto.randomUUID(),
        base_revision: null, base: {}, changes: { ...values, source: "manual" as const },
      });
      if (operation) await sendOperation(operation);
      setEditing(undefined);
    } catch (failure) {
      setFormError(failure instanceof Error ? failure.message : "保存未完成，填写内容已保留。");
    } finally { setBusy(false); }
  }

  async function toggleTask(task: Task) {
    if (busy) return;
    setBusy(true);
    try {
      const operation = patchFor(task, { status: task.status === "completed" ? "pending" : "completed" });
      if (operation) await sendOperation(operation);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "保存未完成，请重试。"); }
    finally { retryOperation.current = null; setBusy(false); }
  }

  async function deleteTask() {
    if (!deleting || busy) return;
    setBusy(true);
    try {
      const operation = retryOperation.current ?? {
        op_id: crypto.randomUUID(), action: "delete" as const, task_id: deleting.id,
        base_revision: deleting.revision, base: {}, changes: {},
      };
      await sendOperation(operation);
      setDeleting(null);
      setEditing(undefined);
    } catch (failure) { setFormError(failure instanceof Error ? failure.message : "删除未完成，请重试。"); }
    finally { setBusy(false); }
  }

  async function resolveConflict(operation: Operation, retry: boolean) {
    if (busy) return;
    setBusy(true);
    try {
      if (retry) {
        const current = data?.canonical_tasks.find(task => task.id === operation.task_id) ?? operation.current;
        let replacement: Operation | null = null;
        if (operation.action === "delete") {
          if (current && !current.deleted) replacement = { ...operation, op_id: crypto.randomUUID(), base_revision: current.revision, base: {}, changes: {} };
        } else if (current && !current.deleted) {
          const { source: _source, ...values } = operation.changes;
          replacement = patchFor(current, values);
        } else if (operation.action === "create") {
          replacement = { ...operation, op_id: crypto.randomUUID(), task_id: crypto.randomUUID(), base_revision: null, base: {} };
        } else {
          throw new Error("这项安排已被删除。请先保留 Mac 版本，再重新添加安排。");
        }
        if (replacement) await sendOperation(replacement);
      }
      const state = await jsonRequest("/api/mobile/resolve", { op_id: operation.op_id, action: "discard" });
      ++fetchVersion.current;
      setData(state);
      setError("");
      setToast(retry ? "已重新保存修改，等待 Mac 接收" : "已保留 Mac 中的安排");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "处理未完成，请重试。"); }
    finally { retryOperation.current = null; setBusy(false); }
  }

  const tasks = data?.tasks.filter(task => !task.deleted) ?? [];
  const pending = tasks.filter(task => isActionable(task, now));
  const queued = data?.operations.filter(item => item.status === "pending") ?? [];
  const conflicts = data?.operations.filter(item => item.status === "conflict" || item.status === "invalid") ?? [];
  const urgent = (task: Task) => isUrgent(task, now);
  const visible = agendaTasks(tasks, tab, search, now);
  const urgentTasks = tab === "pending" ? visible.filter(urgent) : [];
  const otherTasks = visible.filter(task => !urgentTasks.includes(task));
  const lastSync = validTime(data?.last_mac_sync_at);
  const fresh = lastSync !== null && now - lastSync < 5 * 60000;

  function taskRow(task: Task) {
    const taskQueued = queued.some(item => item.task_id === task.id);
    const taskConflict = conflicts.some(item => item.task_id === task.id);
    return <article className={`task-row${urgent(task) ? " is-urgent" : ""}${task.status === "completed" ? " is-completed" : ""}`} key={task.id}>
      <button type="button" className="complete" disabled={busy} aria-label={`${task.status === "completed" ? task.kind === "assignment" ? "恢复待完成" : "恢复安排" : "标为已完成"} ${task.title}`} aria-pressed={task.status === "completed"} onClick={() => void toggleTask(task)}>{task.status === "completed" && <Check size={16} strokeWidth={2.5} />}</button>
      <button type="button" className="task-content" disabled={busy} onClick={() => openEditor(task)} aria-label={`编辑 ${task.title}`}>
        <span className="task-topline"><span className="task-title">{task.title}</span><span className="task-date">{task.kind === "exam" ? task.start_at ? dateLabel(task.start_at) : "时间待补充" : dateLabel(task.due_at)}</span></span>
        <span className="task-meta"><span>{task.course || "未指定课程"}</span><span className="separator">·</span><span>{KIND_LABEL[task.kind]}</span><span className="time-note">{task.kind === "exam" ? task.start_at ? `${countdown(task, now) || "开考"} · ${dateLabel(task.due_at)} 结束` : "请补充开考时间以设置提醒" : countdown(task, now)}</span></span>
        {taskConflict ? <span className="pending-label conflict-label">有修改需要确认</span> : taskQueued && <span className="pending-label"><Clock3 size={12} />等待 Mac 接收</span>}
      </button>
    </article>;
  }

  return <main className="shell">
    <header className="topbar"><a className="brand" href="/"><span className="brand-mark">期</span><span>课期</span></a><span className="topbar-caption">手机安排</span></header>
    <div className="sync-strip" aria-live="polite"><span className={`sync-dot${fresh ? " fresh" : ""}`} /><div>{data ? data.bridge_connected ? `Mac 最近同步 ${dateLabel(data.last_mac_sync_at, true)}` : "等待 Mac 首次连接" : "正在读取安排"}<small>时间均为北京时间</small></div><button type="button" className="icon-button" aria-label="刷新安排" disabled={refreshing} onClick={() => void refresh(true)}><RefreshCw size={17} className={refreshing ? "spinner" : undefined} /></button></div>
    <div className="page-heading"><div><h1>我的安排</h1><p className={pending.some(urgent) ? "urgent-copy" : undefined}>{pending.some(urgent) ? `24 小时内有 ${pending.filter(urgent).length} 项安排` : `${pending.length} 项待办安排`}</p></div><button type="button" className="button primary" onClick={() => openEditor(null)} disabled={!data || busy}><Plus size={17} />新增</button></div>
    {(queued.length > 0 || conflicts.length > 0) && <div className="queue-notice" role="status"><p>{queued.length > 0 && `${queued.length} 项修改已保存到云端，等待 Mac 接收`}{conflicts.length > 0 && <a href="#conflicts">{conflicts.length} 项需要确认<ChevronRight size={12} style={{ display: "inline" }} /></a>}</p>{queued.length > 0 && <p>Mac 恢复运行并联网后，会接收修改并更新日历。</p>}</div>}
    {error && <div className="error-notice" role="alert">{error}{error.includes("登录") && <a href="/signin-with-chatgpt?return_to=%2F" target="_top">重新登录</a>}</div>}
    <div className="toolbar"><div className="tabs" aria-label="安排状态"><button type="button" aria-pressed={tab === "pending"} onClick={() => setTab("pending")}>待办</button><button type="button" aria-pressed={tab === "completed"} onClick={() => setTab("completed")}>已完成</button><button type="button" aria-pressed={tab === "all"} onClick={() => setTab("all")}>全部安排</button></div><label className="search"><Search size={18} /><span className="sr-only">搜索安排或课程</span><input value={search} onChange={event => setSearch(event.target.value)} type="search" placeholder="搜索课程或安排" /></label></div>
    {!data ? <div className="loading" role="status">{error ? "暂时无法读取安排" : "正在读取安排…"}</div> : !visible.length ? <section className="empty"><CheckCheck size={32} strokeWidth={1.4} /><h2>{search ? "没有找到相关安排" : tab === "completed" ? "还没有已完成的安排" : tab === "all" ? "暂时没有安排" : "暂时没有待办安排"}</h2><p>{search ? "试试其他名称或课程。" : data.bridge_connected ? "新增安排，或等待 Mac 读取课程。" : "Mac 首次连接后，这里会显示已有安排。"}</p>{!search && tab === "pending" && <button type="button" className="button secondary" onClick={() => openEditor(null)}>添加安排</button>}</section> : <>
      {urgentTasks.length > 0 && <section className="task-section urgent-section"><h2 className="group-heading">24 小时内<span>{urgentTasks.length} 项</span></h2><div className="task-list">{urgentTasks.map(taskRow)}</div></section>}
      {otherTasks.length > 0 && <section className="task-section"><h2 className="group-heading">{tab === "completed" ? "已完成" : tab === "all" ? "全部安排" : urgentTasks.length ? "其他安排" : "待办安排"}<span>{otherTasks.length} 项</span></h2><div className="task-list">{otherTasks.map(taskRow)}</div></section>}
    </>}
    {conflicts.length > 0 && <section className="conflicts" id="conflicts"><h2>需要确认的修改</h2>{conflicts.map(operation => {
      const current = data?.canonical_tasks.find(task => task.id === operation.task_id) ?? operation.current;
      return <article className="conflict-item" key={operation.op_id}><h3>{String(operation.changes.title || current?.title || "安排修改")}</h3><p>{operation.message || "Mac 中的安排已有其他修改，请选择保留哪个版本。"}</p>{operation.action === "delete" ? <p>手机操作为删除这项安排。</p> : <dl className="conflict-values">{Object.entries(operation.changes).filter(([field]) => field !== "source").map(([field, value]) => <div key={field}><dt>{FIELD_LABEL[field] || field}</dt><dd><span>Mac 当前</span>{current && !current.deleted ? fieldValue(field, current[field as keyof Task], current.kind) : "安排已删除"}</dd><dd><span>手机修改</span>{fieldValue(field, value, operation.changes.kind ?? current?.kind)}</dd></div>)}</dl>}<div className="conflict-actions"><button type="button" className="button secondary" disabled={busy} onClick={() => void resolveConflict(operation, false)}>保留 Mac 版本</button><button type="button" className="button subtle" disabled={busy} onClick={() => void resolveConflict(operation, true)}>{operation.action === "delete" ? "仍然删除" : "采用手机修改"}</button></div></article>;
    })}</section>}
    <footer className="footer">手机修改会先保存在云端。Mac 恢复运行后自动接收，并同步到日历。{data?.platform_synced_at && <><br />最近读取平台 {dateLabel(data.platform_synced_at, true)}</>}</footer>
    <dialog ref={dialog} aria-labelledby="edit-title" onCancel={event => { if (busy) event.preventDefault(); else { setEditing(undefined); retryOperation.current = null; } }}>
      <form onSubmit={submit}>
        <div className="dialog-heading"><h2 id="edit-title">{editing ? "编辑安排" : "新增安排"}</h2><button type="button" className="icon-button" aria-label="关闭" disabled={busy} onClick={() => { setEditing(undefined); retryOperation.current = null; }}><X size={21} /></button></div>
        <fieldset disabled={busy || !!retryOperation.current} style={{ border: 0, margin: 0, padding: 0 }}><div className="form-grid">
          <label className="full">安排名称<input autoFocus required maxLength={300} value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} placeholder="例如，概率论第三次作业" /></label>
          <label>课程<input maxLength={200} value={draft.course} onChange={event => setDraft({ ...draft, course: event.target.value })} placeholder="课程名称" /></label><label>类型<select value={draft.kind} onChange={event => setDraft({ ...draft, kind: event.target.value as Task["kind"] })}><option value="assignment">作业</option><option value="exam">考试</option><option value="lecture">上课</option></select></label>
          {draft.kind !== "lecture" && <label>{draft.kind === "exam" ? "开考时间" : "开始时间（可选）"}<input type="datetime-local" value={draft.start} onChange={event => setDraft({ ...draft, start: event.target.value })} /></label>}
          <label className={draft.kind === "lecture" ? "full" : undefined}>{draft.kind === "exam" ? "考试结束时间" : draft.kind === "lecture" ? "上课时间" : "截止时间"}<input type="datetime-local" required value={draft.due} onChange={event => setDraft({ ...draft, due: event.target.value })} /></label>
          <p className="form-hint full">{draft.kind === "exam" ? "考试按开考时间提醒。缺少开考时间时，请补充后再同步提醒。" : "以上时间均按北京时间保存。"}</p>
          <label className="full">平台链接（可选）<input type="url" maxLength={2000} value={draft.url} onChange={event => setDraft({ ...draft, url: event.target.value })} placeholder="https://" /></label><label className="full">备注（可选）<textarea rows={3} maxLength={6000} value={draft.notes} onChange={event => setDraft({ ...draft, notes: event.target.value })} /></label><label className="checkbox-label full"><input type="checkbox" checked={draft.completed} onChange={event => setDraft({ ...draft, completed: event.target.checked })} />已完成</label>
        </div></fieldset>
        {formError && <p className="form-error" role="alert">{formError}</p>}
        <div className="dialog-actions">{editing && <button type="button" className="button danger delete-button" disabled={busy || !!retryOperation.current} onClick={() => { setFormError(""); setDeleting(editing); }}>删除</button>}<button type="button" className="button secondary" disabled={busy} onClick={() => { setEditing(undefined); retryOperation.current = null; }}>取消</button><button type="submit" className="button primary" disabled={busy}>{busy ? "正在保存…" : retryOperation.current ? "重试保存" : "保存到云端"}</button></div>
      </form>
    </dialog>
    <dialog ref={deleteDialog} aria-labelledby="delete-title" onCancel={event => { if (busy) event.preventDefault(); else { setDeleting(null); retryOperation.current = null; } }}>
      <div className="dialog-heading"><h2 id="delete-title">删除安排</h2><button type="button" className="icon-button" aria-label="关闭" disabled={busy} onClick={() => { setDeleting(null); retryOperation.current = null; }}><X size={21} /></button></div><p className="delete-copy">删除“{deleting?.title}”后，Mac 接收修改时会取消对应提醒。</p>{formError && <p className="form-error" role="alert">{formError}</p>}<div className="dialog-actions"><button type="button" className="button secondary" disabled={busy} onClick={() => { setDeleting(null); retryOperation.current = null; }}>取消</button><button type="button" className="button danger" disabled={busy} onClick={() => void deleteTask()}>{busy ? "正在删除…" : "确认删除"}</button></div>
    </dialog>
    {toast && <div className="toast" role="status">{toast}</div>}
  </main>;
}
