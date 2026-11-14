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
// 暖橙代表 24 小时内，红色代表 2 小时内。颜色和文字同时提示，避免仅靠颜色区分。
const urgencyPalette = {
  soon: {
    background: Color.dynamic(new Color("FFE2BA"), new Color("583218")),
    ink: Color.dynamic(new Color("44260D"), new Color("FFF0DA")),
    secondary: Color.dynamic(new Color("704418"), new Color("F3CEA7")),
    accent: Color.dynamic(new Color("913800"), new Color("FFD6A0")),
  },
  critical: {
    background: Color.dynamic(new Color("AB242C"), new Color("92252D")),
    ink: new Color("FFFFFF"), secondary: new Color("FFE8E0"), accent: new Color("FFFFFF"),
  },
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
// 平台同步是 Mac 完整读取平台的时间；设备这次读取日历不代表平台刚同步。
function parsePlatformSync(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value || "");
  if (!m || Number(m[1]) < 1) return null;
  if (m[8] !== "Z") {
    const hours = Number(m[8].slice(1, 3)), minutes = Number(m[8].slice(4, 6));
    if (hours > 14 || minutes > 59 || (hours === 14 && minutes !== 0)) return null;
  }
  // Date 会把 2 月 30 日等值滚到下个月；先核对原时区的年月日时分秒。
  const local = new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`);
  if (!Number.isFinite(local.getTime()) || local.getUTCFullYear() !== Number(m[1]) ||
      local.getUTCMonth() + 1 !== Number(m[2]) || local.getUTCDate() !== Number(m[3]) ||
      local.getUTCHours() !== Number(m[4]) || local.getUTCMinutes() !== Number(m[5]) ||
      local.getUTCSeconds() !== Number(m[6])) return null;
  const result = new Date(value);
  return Number.isFinite(result.getTime()) && result <= now ? result : null;
}
function uniqueMetadataField(notes, name) {
  const prefix = `${name}：`;
  const lines = notes.split(/\r?\n/).filter(line => line.startsWith(prefix));
  return lines.length === 1 ? lines[0].slice(prefix.length).trim() : "";
}
function platformSyncFromNotes(notes) {
  return parsePlatformSync(uniqueMetadataField(notes, "平台最近成功同步"));
}
function platformResultFromNotes(notes) {
  const value = uniqueMetadataField(notes, "平台同步结果");
  return ["success", "partial", "failed", "running", "interrupted", "scope_changed", "never"].includes(value) ? value : "";
}
function platformSyncText(tasks, family) {
  // 传播过程中可能混有旧备注。只有所有记录都可验证才报告最早时间，绝不取最新值。
  const complete = tasks.length && tasks.every(task => task.platformSyncedAt);
  const attempted = tasks.length && tasks.every(task => task.platformAttemptedAt && task.platformSyncResult);
  const oldest = name => new Date(Math.min(...tasks.map(task => task[name].getTime())));
  const resultLabel = () => {
    const results = tasks.map(task => task.platformSyncResult);
    if (results.every(value => value === "success")) return "已完成";
    if (results.every(value => value === "partial")) return "部分";
    if (results.every(value => value === "running")) return "读取中";
    return "未完成";
  };
  if (complete) {
    const syncedAt = oldest("platformSyncedAt");
    const recentIncomplete = attempted && oldest("platformAttemptedAt") >= syncedAt &&
      tasks.some(task => task.platformSyncResult !== "success");
    return `平台同步 ${dateText(syncedAt, beijingParts(syncedAt).year !== beijingParts(now).year)}` +
      (recentIncomplete ? ` · ${resultLabel()}` : "");
  }
  if (attempted) {
    const attemptedAt = oldest("platformAttemptedAt");
    const date = dateText(attemptedAt, beijingParts(attemptedAt).year !== beijingParts(now).year);
    return family === "small" ? `尝试 ${date}（${resultLabel()}）` : `平台尝试 ${date} · ${resultLabel()}`;
  }
  return "平台同步时间未知";
}
function syncFooter(widget, tasks, family, readAt, color = palette.secondary, suffix = "", invalidCount = 0) {
  const size = family === "small" ? 8 : 9;
  const conflicts = tasks.filter(task => task.calendarTimeChanged).length;
  const warning = invalidCount ? ` · ${invalidCount} 项异常` : conflicts ? ` · ${conflicts} 项时间待核对` : "";
  const readText = readAt ? `日历读取 ${timeText(readAt)}${warning}${suffix}` : "日历读取失败";
  const platformText = invalidCount ? "平台同步时间未知" : platformSyncText(tasks, family);
  // 中号维持三项容量：两个来源合并在同一行；小号分两行保留可读性。
  if (family === "medium") {
    text(widget, `${platformText} · ${readText}`, size, color);
  } else {
    text(widget, platformText, size, color);
    widget.addSpacer(1);
    text(widget, readText, size, color);
  }
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
  if (!/^(截止时间|上课时间)：/.test(notes) || !/^本地记录更新时间：.+$/m.test(notes) ||
      !/^类型：(assignment|exam|lecture)$/m.test(notes)) return null;
  const kind = field(notes, "类型");
  const timeField = kind === "lecture" ? "上课时间" : "截止时间";
  if (!notes.startsWith(timeField + "：")) return null;
  const due = parseBeijing(field(notes, timeField));
  const originalDue = event.startDate;
  if (!due || !(originalDue instanceof Date) || !Number.isFinite(originalDue.getTime())) {
    throw new Error(`存在${timeField}格式异常的课期记录。请在 Mac 程序重新同步日历后重试。`);
  }
  const course = field(notes, "课程");
  let title = String(event.title || "").trim();
  if (course && title.startsWith(course + " · ")) title = title.slice(course.length + 3);
  if (!title) throw new Error("存在标题缺失的课期记录。请在 Mac 程序重新同步日历。");
  // 旧桥接也会将作业的发布时刻写为“考试开始”；仅考试使用此字段。
  const startText = kind === "exam" ? field(notes, "考试开始") : "";
  const start = startText ? parseBeijing(startText) : null;
  if (startText && !start) {
    throw new Error("存在考试开始时间格式异常的记录。请在 Mac 程序重新同步日历。");
  }
  if (start && start > due) {
    throw new Error("存在开考时间晚于结束时间的记录。请在 Mac 程序核对后重新同步日历。");
  }
  const sameMinute = (a, b) => a instanceof Date && Number.isFinite(a.getTime()) &&
    Math.abs(a.getTime() - b.getTime()) < 60000;
  // 读取备注中的开考时刻，同时兼容旧截止占位及新版真实考试区间。
  const examEnd = start && start.getTime() === due.getTime() ? new Date(start.getTime() + 15 * 60000) : due;
  const examInterval = kind === "exam" && start && !event.isAllDay &&
    sameMinute(originalDue, start) && sameMinute(event.endDate, examEnd);
  const calendarTimeChanged = !examInterval && (!sameMinute(originalDue, due) || Boolean(event.isAllDay));
  const anchor = kind === "exam" ? start : due;
  const examPhase = kind !== "exam" ? "" : !start ? "missing" : now <= start ? "upcoming" : now < due ? "ongoing" : "ended";
  return { id: event.identifier, course: shortCourse(course), title,
    kind, due, start, anchor, examPhase, calendarTimeChanged, updated: field(notes, "本地记录更新时间"),
    platformSyncedAt: platformSyncFromNotes(notes),
    platformAttemptedAt: parsePlatformSync(uniqueMetadataField(notes, "平台最近尝试同步")),
    platformSyncResult: platformResultFromNotes(notes),
    offline: /线下|纸质/.test(notes) && /大学物理|大物/.test(course),
    overdue: kind === "assignment" && due.getTime() < now.getTime() };
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
  // 一条损坏的课期备注不能阻止其他课程显示，也不能伪装成没有待办。
  const tasks = [];
  let invalidCount = 0;
  for (const event of events) {
    try {
      const task = taskFromEvent(event);
      if (task) tasks.push(task);
    } catch (_) { invalidCount += 1; }
  }
  const unique = new Map();
  tasks.forEach(task => unique.set(task.id, task));
  return { tasks: Array.from(unique.values()), readAt: new Date(), invalidCount };
}
function urgency(task) {
  if (!task.anchor) return 0;
  const left = task.anchor.getTime() - now.getTime();
  if (task.overdue || left < 0 || left > 24 * 3600000) return 0;
  return left <= 2 * 3600000 ? 2 : 1;
}
function taskColors(task) {
  const level = urgency(task);
  return level ? urgencyPalette[level === 2 ? "critical" : "soon"] : palette;
}
function pendingLabel(task) {
  if (task.kind === "exam") return { missing: "待补开考时间", ongoing: "考试中", ended: "已结束", upcoming: "待考" }[task.examPhase];
  return task.kind === "lecture" ? "待上课" : task.offline ? "线下待交" : "待交";
}
function remaining(task) {
  if (!task.anchor) return "开考时间待补充";
  if (task.anchor.getTime() === now.getTime()) return task.kind === "exam" ? "现在开考" : task.kind === "lecture" ? "现在上课" : "现在截止";
  if (task.kind === "exam" && task.examPhase === "ongoing") return "考试中";
  if (task.kind === "exam" && task.examPhase === "ended") return "已结束";
  const ms = Math.abs(task.anchor.getTime() - now.getTime());
  let label;
  if (ms < 3600000) label = `${Math.max(1, Math.ceil(ms / 60000))} 分钟`;
  else if (ms < 48 * 3600000) label = `${Math.ceil(ms / 3600000)} 小时`;
  else label = `${Math.ceil(ms / DAY)} 天`;
  return `${task.overdue ? "逾期 " : "剩 "}${label}`;
}
function deadline(task) {
  if (!task.anchor) return "开考时间待补充";
  const year = beijingParts(task.anchor).year !== beijingParts(now).year;
  if (task.kind === "exam") return (task.calendarTimeChanged ? "备注 " : "") + dateText(task.anchor, year) + " 开考";
  const prefix = task.kind === "lecture" ? "上课 " : task.overdue ? "已过截止 " : "截止 ";
  return (task.calendarTimeChanged ? "备注" : "") + prefix + dateText(task.due, year);
}
function examEnd(task, compact) {
  const same = task.start && dateStamp(task.start) === dateStamp(task.due);
  return `${compact && same ? timeText(task.due) : dateText(task.due,
    beijingParts(task.due).year !== beijingParts(now).year)} 结束`;
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
function header(widget, family, future, overdueCount, smallColors, activeCount = 0, missingCount = 0) {
  const row = widget.addStack();
  row.centerAlignContent();
  text(row, "课期", family === "small" ? 15 : 16, smallColors ? smallColors.ink : palette.green, true);
  row.addSpacer();
  const soon = future.filter(task => urgency(task));
  const dueAssignments = soon.filter(task => task.kind === "assignment").length;
  const dueExams = soon.filter(task => task.kind === "exam").length;
  const dueLectures = soon.filter(task => task.kind === "lecture").length;
  if (soon.length) {
    const badge = row.addStack();
    const colors = smallColors || urgencyPalette[soon.some(task => urgency(task) === 2) ? "critical" : "soon"];
    badge.backgroundColor = colors.background;
    badge.cornerRadius = 5;
    badge.setPadding(2, 5, 2, 5);
    const counts = [dueAssignments ? `待交 ${dueAssignments}` : "", dueExams ? `待考 ${dueExams}` : "",
      dueLectures ? `上课 ${dueLectures}` : ""].filter(Boolean).join(" · ");
    text(badge, `24h ${counts}`, family === "small" ? 10 : 12, colors.ink, true);
    return;
  }
  const summary = family === "small" ? "北京时间" : future.length === 0 && overdueCount > 0 && !activeCount && !missingCount ?
    `未来暂无 · 逾期 ${overdueCount}` : `待办 ${future.length}${activeCount ? ` · 考试中 ${activeCount}` : ""}` +
    `${missingCount ? ` · 待补 ${missingCount}` : ""}${overdueCount ? ` · 逾期 ${overdueCount}` : ""}`;
  text(row, summary, 10, overdueCount ? palette.urgent : palette.secondary);
}
function compactRow(container, task, family) {
  const row = container.addStack();
  row.layoutVertically();
  const level = urgency(task);
  const colors = taskColors(task);
  // 每项保持两行；紧急卡只增加 2pt 高度，中号仍可显示 3 项。
  if (level) {
    row.backgroundColor = colors.background;
    row.cornerRadius = 6;
    row.setPadding(1, 5, 1, 5);
  }
  const top = row.addStack();
  const kind = level || task.kind === "exam" || task.kind === "lecture" ? `${pendingLabel(task)} · ` : "";
  text(top, `${task.course} · ${kind}${task.title}`, 12, colors.ink, true);
  top.addSpacer(4);
  text(top, remaining(task), level ? 12 : 10, level ? colors.accent : task.overdue ? palette.urgent : palette.green, Boolean(level));
  row.addSpacer(2);
  const cue = level === 2 && task.kind === "assignment" ? "尽快提交 · " : "";
  const second = `${cue}${deadline(task)}${task.kind === "exam" ? " · " + examEnd(task, true) : ""}`;
  text(row, second, 10, colors.secondary);
  if (family !== "medium") {
    const note = midnightNote(task);
    if (note) { row.addSpacer(1); text(row, note, 9, colors.secondary); }
  }
}
function smallBody(widget, task) {
  const level = urgency(task);
  const colors = taskColors(task);
  widget.addSpacer(3);
  const label = level === 2 && task.kind === "assignment" ? "尽快提交" : pendingLabel(task);
  text(widget, `${task.course} · ${label}`, 10, colors.secondary, true);
  widget.addSpacer(1);
  text(widget, task.title, 14, colors.ink, true, 2);
  widget.addSpacer(2);
  text(widget, remaining(task), !task.anchor ? 13 : level ? 22 : 19, level ? colors.accent : task.overdue ? palette.urgent : palette.green, true);
  widget.addSpacer(1);
  text(widget, task.kind === "exam" ? deadline(task) : (task.calendarTimeChanged ? "备注 " : "") + dateText(task.due,
    beijingParts(task.due).year !== beijingParts(now).year) + (task.kind === "lecture" ? " 上课" : " 截止"), 10, colors.secondary);
  if (task.kind === "exam") {
    widget.addSpacer(2);
    text(widget, examEnd(task, false), 9, colors.secondary);
  } else if (midnightNote(task)) {
    widget.addSpacer(2);
    text(widget, midnightNote(task), 9, colors.secondary);
  }
}
function message(widget, title, body, family) {
  widget.addSpacer();
  text(widget, title, family === "small" ? 15 : 18, palette.ink, true, 2);
  widget.addSpacer(8);
  text(widget, body, family === "small" ? 10 : 12, palette.secondary, false, family === "small" ? 5 : 6);
  widget.addSpacer();
}
function makeWidget(tasks, family, error, calendarId, readAt, invalidCount = 0) {
  const widget = new ListWidget();
  widget.backgroundColor = palette.background;
  widget.setPadding(family === "small" ? 8 : family === "medium" ? 10 : 14, 14,
    family === "small" ? 6 : family === "medium" ? 8 : 12, 14);
  widget.refreshAfterDate = new Date(now.getTime() + 15 * 60000);
  // Scriptable 官方 URL：点击打开脚本预览，不修改日历，也不访问本地 Mac 服务。
  const runURL = URLScheme.forRunningScript();
  widget.url = `${runURL}${runURL.includes("?") ? "&" : "?"}preview=large` +
    (calendarId ? `&calendarId=${encodeURIComponent(calendarId)}` : "");
  const future = tasks.filter(task => task.anchor && task.anchor >= now && (task.kind !== "exam" || task.examPhase === "upcoming"))
    .sort((a, b) => a.anchor - b.anchor);
  const active = tasks.filter(task => task.examPhase === "ongoing").sort((a, b) => a.anchor - b.anchor);
  const missing = tasks.filter(task => task.kind === "exam" && !task.anchor && task.due >= now);
  const past = tasks.filter(task => task.overdue).sort((a, b) => b.anchor - a.anchor);
  const overdueCount = past.length;
  const primary = [...active, ...future, ...missing];
  const smallColors = !error && family === "small" && primary.length && urgency(primary[0]) ? taskColors(primary[0]) : null;
  if (smallColors) widget.backgroundColor = smallColors.background;
  header(widget, family, error ? [] : future, error ? 0 : overdueCount, smallColors,
    error ? 0 : active.length, error ? 0 : missing.length);
  if (error) {
    message(widget, "需要检查日历", error, family);
    syncFooter(widget, [], family, null);
    return widget;
  }
  if (!primary.length && !past.length) {
    message(widget, invalidCount ? "有记录需要核对" : "近期没有课期待办", invalidCount ?
      `${invalidCount} 项课期记录格式异常。请检查日历备注，或在 Mac 程序核对后同步。` :
      "查询范围：过去 30 天至未来 60 天。以本机已收到的日历记录为准。", family);
    syncFooter(widget, tasks, family, readAt, palette.secondary, "", invalidCount);
    return widget;
  }
  const capacity = { small: 1, medium: 3, large: 5, extraLarge: 8 }[family];
  let visible = primary.slice(0, capacity);
  if (!primary.length || family === "large" || family === "extraLarge") {
    visible = visible.concat(past.slice(0, capacity - visible.length));
  }
  if (family === "small") {
    smallBody(widget, visible[0]);
    widget.addSpacer();
    const suffix = overdueCount && !visible[0].overdue ? ` · 逾期 ${overdueCount}` : "";
    syncFooter(widget, tasks, family, readAt, taskColors(visible[0]).secondary, suffix, invalidCount);
    return widget;
  }
  widget.addSpacer(family === "medium" ? 5 : 12);
  if (!primary.length && family !== "medium") {
    text(widget, "未来 60 天暂无待办 · 以下为逾期作业", 10, palette.urgent);
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
      if (index < visible.length - 1) widget.addSpacer(family === "medium" ? 4 : 10);
    });
  }
  widget.addSpacer();
  const remainingCount = Math.max(0, primary.length + past.length - visible.length);
  syncFooter(widget, tasks, family, readAt, palette.secondary,
    remainingCount ? ` · 另 ${remainingCount} 项` : "", invalidCount);
  return widget;
}

let settings = { family: config.widgetFamily || PREVIEW_SIZE, calendarId: "" };
let tasks = [], error = "", readAt = null, invalidCount = 0;
try { settings = options(); ({ tasks, readAt, invalidCount } = await readTasks(settings.calendarId)); }
catch (failure) { error = failure && failure.message ? failure.message : "读取失败，请在 Scriptable 内运行并检查日历权限。"; }
if (!["small", "medium", "large", "extraLarge"].includes(settings.family)) settings.family = "medium";
const widget = makeWidget(tasks, settings.family, error, settings.calendarId, readAt, invalidCount);
Script.setWidget(widget);
if (config.runsInApp) {
  if (settings.family === "small") await widget.presentSmall();
  else if (settings.family === "large") await widget.presentLarge();
  else if (settings.family === "extraLarge") await widget.presentExtraLarge();
  else await widget.presentMedium();
}
Script.complete();
