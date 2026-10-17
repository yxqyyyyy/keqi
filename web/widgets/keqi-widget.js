// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: deep-green; icon-glyph: calendar-alt;

// 课期：只读 Apple 日历的作业小组件。请先在 Scriptable 内运行一次并授权日历。
// 无网络请求、无文件读写、无日历写入；不会创建通知或把操作传回 Mac。
const CALENDAR_NAME = "作业与考试";
const CALENDAR_IDENTIFIER = ""; // 仅遇到同名日历时按 README 指定；各设备标识可能不同。
const PREVIEW_SIZE = "medium";
const DAY = 86400000;
const now = new Date();
const palette = {
  background: Color.dynamic(new Color("F7F7F0"), new Color("1C2420")),
  ink: Color.dynamic(new Color("24382E"), new Color("EDF1E9")),
  secondary: Color.dynamic(new Color("68786C"), new Color("A8B7AB")),
  green: Color.dynamic(new Color("456D55"), new Color("AED0B4")),
  urgent: Color.dynamic(new Color("9B583D"), new Color("EAB599")),
  line: Color.dynamic(new Color("DDE5DA"), new Color("35443A")),
};

function options() {
  const parameter = typeof args.widgetParameter === "string" ? args.widgetParameter.trim() : "";
  let value = {};
  if (parameter.startsWith("{")) {
    try { value = JSON.parse(parameter); }
    catch (_) { throw new Error("小组件参数不是有效 JSON。请清空参数，或参照安装说明填写。"); }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("小组件参数应为一个 JSON 对象，或 small / medium / large / extraLarge。");
    }
  } else if (parameter) { value.size = parameter; }
  const supported = ["small", "medium", "large", "extraLarge"];
  const query = args.queryParameters || {};
  const family = config.widgetFamily || (config.runsInApp && query.preview) || value.size || PREVIEW_SIZE;
  if (!supported.includes(family)) {
    throw new Error("请选择主屏幕的小、中、大号组件；此脚本未设计锁屏组件。");
  }
  if (value.calendarId !== undefined && typeof value.calendarId !== "string") {
    throw new Error("calendarId 应是文字格式的日历标识。请参照安装说明。");
  }
  return { family, calendarId: value.calendarId || (config.runsInApp && query.calendarId) || CALENDAR_IDENTIFIER };
}

// 通过 UTC 字段加固定 UTC+8 显示北京时间，避免设备处于其他时区时偏移。
function beijingParts(date) {
  const d = new Date(date.getTime() + 8 * 3600000);
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1,
    day: d.getUTCDate(), hour: d.getUTCHours(), minute: d.getUTCMinutes(), weekday: d.getUTCDay() };
}
function pad(n) { return String(n).padStart(2, "0"); }
function dateText(date, includeYear = false) {
  const p = beijingParts(date);
  return `${includeYear ? p.year + "/" : ""}${pad(p.month)}/${pad(p.day)} ${pad(p.hour)}:${pad(p.minute)}`;
}
function dateStamp(date) {
  const p = beijingParts(date);
  return `${p.year}-${pad(p.month)}-${pad(p.day)}`;
}
function timeText(date) {
  const p = beijingParts(date);
  return `${pad(p.hour)}:${pad(p.minute)}`;
}
function parseBeijing(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})（北京时间）$/.exec(value || "");
  if (!m) return null;
  const result = new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:00+08:00`);
  if (!Number.isFinite(result.getTime())) return null;
  const p = beijingParts(result);
  if (p.year !== Number(m[1]) || p.month !== Number(m[2]) || p.day !== Number(m[3]) ||
      p.hour !== Number(m[4]) || p.minute !== Number(m[5])) return null;
  return result;
}
function field(notes, name) {
  const prefix = `${name}：`;
  const line = notes.split(/\r?\n/).find(item => item.startsWith(prefix));
  return line ? line.slice(prefix.length).trim() : "";
}
function shortCourse(course) {
  if (/概率/.test(course)) return "概率论";
  if (/离散/.test(course)) return "离散数学";
  if (/计算机组成|组成与体系/.test(course)) return "计算机组成";
  if (/大学物理|大物/.test(course)) return "大物";
  return course || "课程";
}
function taskFromEvent(event) {
  const notes = String(event.notes || "");
  // CalendarEvent 没有公开 URL 属性：仅识别本程序固定备注格式，不展示其他私人事件。
  if (!notes.startsWith("截止时间：") || !/^本地记录更新时间：.+$/m.test(notes) ||
      !/^类型：(assignment|exam)$/m.test(notes)) return null;
  const due = parseBeijing(field(notes, "截止时间"));
  const originalDue = event.startDate;
  if (!due || !(originalDue instanceof Date) || !Number.isFinite(originalDue.getTime()) ||
      Math.abs(due.getTime() - originalDue.getTime()) >= 60000) {
    throw new Error("存在截止时间格式异常的课期记录。请在 Mac 程序重新同步日历后重试。");
  }
  const course = field(notes, "课程");
  let title = String(event.title || "").trim();
  if (course && title.startsWith(course + " · ")) title = title.slice(course.length + 3);
  if (!title) throw new Error("存在标题缺失的课期记录。请在 Mac 程序重新同步日历。");
  const kind = field(notes, "类型");
  // 旧桥接也会将作业的发布时刻写为“考试开始”；仅考试使用此字段。
  const startText = kind === "exam" ? field(notes, "考试开始") : "";
  const start = startText ? parseBeijing(startText) : null;
  if (startText && !start) {
    throw new Error("存在考试开始时间格式异常的记录。请在 Mac 程序重新同步日历。");
  }
  return { id: event.identifier, course: shortCourse(course), title,
    kind, due, start, updated: field(notes, "本地记录更新时间"),
    offline: /线下|纸质/.test(notes) && /大学物理|大物/.test(course),
    overdue: due.getTime() < now.getTime() };
}
async function readTasks(calendarId) {
  let calendars;
  try { calendars = await Calendar.forEvents(); }
  catch (_) {
    throw new Error("无法读取日历。请先在 Scriptable 内运行脚本，并在系统设置允许 Scriptable 完整访问日历。");
  }
  const matches = calendars.filter(calendar => calendar.title === CALENDAR_NAME);
  if (!matches.length) {
    throw new Error("找不到“作业与考试”日历。请在系统日历确认 iCloud 已开启，且该日历已出现在本机。");
  }
  let calendar;
  if (calendarId) {
    calendar = matches.find(item => item.identifier === calendarId);
    if (!calendar) throw new Error("指定的日历标识在本机不存在。请核对标识，或清空参数后重试。");
  } else if (matches.length > 1) {
    const ids = matches.map((item, index) => `${index + 1}. ${item.identifier}`).join("\n");
    if (config.runsInApp) {
      const alert = new Alert();
      alert.title = "请选择唯一的课期日历";
      alert.message = `本机有 ${matches.length} 个同名日历，脚本不会任意选取。请先在系统日历核对并为其他日历改名；也可按 README 在本机参数指定 calendarId。\n\n日历标识：\n${ids}`;
      alert.addAction("知道了");
      await alert.presentAlert();
    }
    throw new Error("存在多个“作业与考试”日历。请打开 Scriptable 运行脚本，按提示明确选择目标日历。");
  } else { calendar = matches[0]; }
  let events;
  try { events = await CalendarEvent.between(new Date(now.getTime() - 30 * DAY),
      new Date(now.getTime() + 60 * DAY), [calendar]); }
  catch (_) {
    throw new Error("日历读取失败。请检查 Scriptable 日历权限，并在系统日历确认“作业与考试”可查看后再运行。");
  }
  const tasks = events.map(taskFromEvent).filter(Boolean);
  const unique = new Map();
  tasks.forEach(task => unique.set(task.id, task));
  return Array.from(unique.values());
}
function remaining(task) {
  const ms = Math.abs(task.due.getTime() - now.getTime());
  let label;
  if (ms < 3600000) label = `${Math.max(1, Math.ceil(ms / 60000))} 分钟`;
  else if (ms < 48 * 3600000) label = `${Math.ceil(ms / 3600000)} 小时`;
  else label = `${Math.ceil(ms / DAY)} 天`;
  return `${task.overdue ? "逾期 " : "剩 "}${label}`;
}
function deadline(task) {
  const year = beijingParts(task.due).year !== beijingParts(now).year;
  const prefix = task.overdue ? "已过截止 " : "截止 ";
  return prefix + dateText(task.due, year);
}
function examStart(task, compact) {
  if (!task.start) return "";
  const same = dateStamp(task.start) === dateStamp(task.due);
  return `${compact && same ? timeText(task.start) : dateText(task.start,
    beijingParts(task.start).year !== beijingParts(now).year)} 开考`;
}
function midnightNote(task) {
  const p = beijingParts(task.due);
  return task.offline && p.weekday === 0 && p.hour === 0 && p.minute === 0 ? "周六 24:00 · 线下" : "";
}
function text(container, value, size, color = palette.ink, bold = false, lines = 1) {
  const element = container.addText(value);
  element.font = bold ? Font.semiboldSystemFont(size) : Font.systemFont(size);
  element.textColor = color;
  element.lineLimit = lines;
  element.minimumScaleFactor = 0.85;
  return element;
}
function header(widget, family, futureCount, overdueCount) {
  const row = widget.addStack();
  row.centerAlignContent();
  text(row, "课期", family === "small" ? 15 : 16, palette.green, true);
  row.addSpacer();
  const summary = family === "small" ? "北京时间" : futureCount === 0 && overdueCount > 0 ?
    `未来暂无 · 逾期 ${overdueCount}` : `待办 ${futureCount}${overdueCount ? ` · 逾期 ${overdueCount}` : ""}`;
  text(row, summary, 10, overdueCount ? palette.urgent : palette.secondary);
}
function compactRow(container, task, family) {
  const row = container.addStack();
  row.layoutVertically();
  const top = row.addStack();
  const color = task.overdue ? palette.urgent : palette.green;
  const kind = task.kind === "exam" ? "考试 · " : "";
  text(top, `${task.course} · ${kind}${task.title}`, 12, palette.ink, true);
  top.addSpacer(4);
  text(top, remaining(task), 10, color);
  row.addSpacer(2);
  const second = `${deadline(task)}${task.start ? " · " + examStart(task, true) : ""}`;
  text(row, second, 10, palette.secondary);
  if (family !== "medium") {
    const note = midnightNote(task);
    if (note) { row.addSpacer(1); text(row, note, 9, palette.secondary); }
  }
}
function smallBody(widget, task, overdueCount) {
  widget.addSpacer(5);
  text(widget, `${task.course}${task.kind === "exam" ? " · 考试" : ""}`, 10, palette.secondary, true);
  widget.addSpacer(2);
  text(widget, task.title, 14, palette.ink, true, 2);
  widget.addSpacer(4);
  text(widget, remaining(task), 19, task.overdue ? palette.urgent : palette.green, true);
  widget.addSpacer(2);
  text(widget, dateText(task.due, beijingParts(task.due).year !== beijingParts(now).year) + " 截止", 10, palette.secondary);
  if (task.start) {
    widget.addSpacer(2);
    text(widget, examStart(task, false), 9, palette.secondary);
  } else if (midnightNote(task)) {
    widget.addSpacer(2);
    text(widget, midnightNote(task), 9, palette.secondary);
  }
  widget.addSpacer();
  if (overdueCount && !task.overdue) text(widget, `另有 ${overdueCount} 项逾期`, 9, palette.urgent);
  else text(widget, `读取 ${timeText(now)}`, 9, palette.secondary);
}
function message(widget, title, body, family) {
  widget.addSpacer();
  text(widget, title, family === "small" ? 15 : 18, palette.ink, true, 2);
  widget.addSpacer(8);
  text(widget, body, family === "small" ? 10 : 12, palette.secondary, false, family === "small" ? 5 : 6);
  widget.addSpacer();
}
function makeWidget(tasks, family, error, calendarId) {
  const widget = new ListWidget();
  widget.backgroundColor = palette.background;
  widget.setPadding(family === "small" ? 10 : 14, 14, family === "small" ? 8 : 12, 14);
  widget.refreshAfterDate = new Date(now.getTime() + 15 * 60000);
  // Scriptable 官方 URL：点击打开脚本预览，不修改日历，也不访问本地 Mac 服务。
  const runURL = URLScheme.forRunningScript();
  widget.url = `${runURL}${runURL.includes("?") ? "&" : "?"}preview=large` +
    (calendarId ? `&calendarId=${encodeURIComponent(calendarId)}` : "");
  const future = tasks.filter(task => !task.overdue).sort((a, b) => a.due - b.due);
  const past = tasks.filter(task => task.overdue).sort((a, b) => b.due - a.due);
  header(widget, family, future.length, past.length);
  if (error) { message(widget, "需要检查日历", error, family); return widget; }
  if (!tasks.length) {
    message(widget, "近期没有课期待办", "查询范围：过去 30 天至未来 60 天。以本机已收到的日历记录为准。", family);
    return widget;
  }
  const capacity = { small: 1, medium: 3, large: 5, extraLarge: 8 }[family];
  let visible = future.slice(0, capacity);
  if (!future.length || family === "large" || family === "extraLarge") {
    visible = visible.concat(past.slice(0, capacity - visible.length));
  }
  if (family === "small") { smallBody(widget, visible[0], past.length); return widget; }
  widget.addSpacer(family === "medium" ? 7 : 12);
  if (!future.length && family !== "medium") {
    text(widget, "未来 60 天暂无待办 · 以下为逾期记录", 10, palette.urgent);
    widget.addSpacer(5);
  }
  if (family === "extraLarge") {
    const columns = widget.addStack();
    const left = columns.addStack(); left.layoutVertically();
    columns.addSpacer(22);
    const right = columns.addStack(); right.layoutVertically();
    visible.forEach((task, index) => {
      const column = index < 4 ? left : right;
      compactRow(column, task, family);
      if (index !== 3 && index !== visible.length - 1) column.addSpacer(14);
    });
    columns.addSpacer();
  } else {
    visible.forEach((task, index) => {
      compactRow(widget, task, family);
      if (index < visible.length - 1) widget.addSpacer(family === "medium" ? 6 : 10);
    });
  }
  widget.addSpacer();
  const remainingCount = Math.max(0, future.length + past.length - visible.length);
  const footer = family === "medium" ?
    `北京时间 · 读取 ${timeText(now)}${remainingCount ? ` · 另 ${remainingCount} 项` : ""}` :
    `北京时间 · 读取 ${dateText(now)} · 逾期统计近 30 天${remainingCount ? ` · 另 ${remainingCount} 项` : ""}`;
  text(widget, footer, 9, palette.secondary);
  return widget;
}

let settings = { family: config.widgetFamily || PREVIEW_SIZE, calendarId: "" };
let tasks = [], error = "";
try { settings = options(); tasks = await readTasks(settings.calendarId); }
catch (failure) { error = failure && failure.message ? failure.message : "读取失败，请在 Scriptable 内运行并检查日历权限。"; }
if (!["small", "medium", "large", "extraLarge"].includes(settings.family)) settings.family = "medium";
const widget = makeWidget(tasks, settings.family, error, settings.calendarId);
Script.setWidget(widget);
if (config.runsInApp) {
  if (settings.family === "small") await widget.presentSmall();
  else if (settings.family === "large") await widget.presentLarge();
  else if (settings.family === "extraLarge") await widget.presentExtraLarge();
  else await widget.presentMedium();
}
Script.complete();
