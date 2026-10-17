import AppKit
import EventKit
import Foundation

// This helper runs from its .app bundle so macOS can associate calendar consent
// with a named application. It only writes events when explicitly invoked.
private let managedCalendarName = "作业与考试"
private let taskScheme = "studydeadline"
private let taskHost = "task"
private let studyTimeZone = TimeZone(identifier: "Asia/Shanghai")!
private let reminderOffsets: [TimeInterval] = [-172_800, -86_400, -7_200]

private struct SyncInput: Decodable {
    let tasks: [TaskInput]
    let calendar: CalendarInput?
}

private struct CalendarInput: Decodable {
    let identifier: String?
    let name: String?
    let event_identifiers: [String: String]?
}

private struct TaskInput: Decodable {
    let id: String
    let title: String
    let course: String?
    let due_at: String
    let kind: String?
    let start_at: String?
    let status: String?
    let url: String?
    let notes: String?
    let updated_at: String?
}

private struct PreparedTask {
    let id: String
    let title: String
    let due: Date
    let notes: String
    let marker: URL
    let pending: Bool
}

private struct SyncOutput: Encodable {
    let ok: Bool
    let message: String
    let calendar_identifier: String?
    let calendar_name: String
    let inserted: Int
    let updated: Int
    let removed: Int
    let event_identifiers: [String: String]
}

private struct BridgeError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private func fail(_ message: String) -> BridgeError { BridgeError(message: message) }

private func parseDate(_ value: String) -> Date? {
    let parser = ISO8601DateFormatter()
    parser.timeZone = studyTimeZone
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = parser.date(from: value) { return date }
    parser.formatOptions = [.withInternetDateTime]
    if let date = parser.date(from: value) { return date }

    // A timezone-less input is interpreted as the shared study timezone.
    for pattern in ["yyyy-MM-dd'T'HH:mm:ss.SSS", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        local.calendar = Calendar(identifier: .gregorian)
        local.timeZone = studyTimeZone
        local.dateFormat = pattern
        local.isLenient = false
        if let date = local.date(from: value) { return date }
    }
    return nil
}

private func displayDate(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.timeZone = studyTimeZone
    formatter.dateFormat = "yyyy-MM-dd HH:mm（北京时间）"
    return formatter.string(from: date)
}

private func markerURL(for id: String) throws -> URL {
    var allowed = CharacterSet.urlPathAllowed
    allowed.remove(charactersIn: "/%?#")
    guard let encoded = id.addingPercentEncoding(withAllowedCharacters: allowed),
          let url = URL(string: "\(taskScheme)://\(taskHost)/\(encoded)") else {
        throw fail("任务 ID 无法生成稳定标识：\(id)")
    }
    return url
}

private func managedTaskID(_ event: EKEvent) -> String? {
    guard let url = event.url,
          url.scheme?.lowercased() == taskScheme,
          url.host?.lowercased() == taskHost,
          url.user == nil, url.password == nil, url.port == nil,
          url.query == nil, url.fragment == nil,
          let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
          components.percentEncodedPath.hasPrefix("/"),
          let id = String(components.percentEncodedPath.dropFirst()).removingPercentEncoding,
          !id.isEmpty else { return nil }
    return id
}

private func prepare(_ input: SyncInput) throws -> [PreparedTask] {
    if let name = input.calendar?.name, name != managedCalendarName {
        throw fail("仅允许同步到程序专用日历“\(managedCalendarName)”。")
    }
    var ids = Set<String>()
    let finishedStates: Set<String> = ["completed", "deleted", "submitted", "done", "cancelled", "canceled"]
    return try input.tasks.map { task in
        guard !task.id.isEmpty, ids.insert(task.id).inserted else {
            throw fail("任务 ID 不能为空或重复：\(task.id)")
        }
        guard !task.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw fail("任务标题不能为空：\(task.id)")
        }
        let status = (task.status ?? "pending").lowercased()
        guard status == "pending" || finishedStates.contains(status) else {
            throw fail("未知任务状态“\(status)”：\(task.id)。未执行同步。")
        }
        guard let due = parseDate(task.due_at) else {
            throw fail("截止时间格式无效：\(task.id) / \(task.due_at)")
        }
        var lines = ["截止时间：\(displayDate(due))"]
        if let start = task.start_at, !start.isEmpty {
            guard let date = parseDate(start) else {
                throw fail("考试开始时间格式无效：\(task.id) / \(start)")
            }
            lines.append("考试开始：\(displayDate(date))")
        }
        if let course = task.course, !course.isEmpty { lines.append("课程：\(course)") }
        if let kind = task.kind, !kind.isEmpty { lines.append("类型：\(kind)") }
        if let url = task.url, !url.isEmpty { lines.append("原始页面：\(url)") }
        if let notes = task.notes, !notes.isEmpty { lines.append("\n\(notes)") }
        if let updated = task.updated_at, !updated.isEmpty { lines.append("\n本地记录更新时间：\(updated)") }
        let title = task.course.flatMap { $0.isEmpty ? nil : "\($0) · \(task.title)" } ?? task.title
        return PreparedTask(id: task.id, title: title, due: due,
                            notes: lines.joined(separator: "\n"),
                            marker: try markerURL(for: task.id), pending: status == "pending")
    }
}

private func isICloud(_ source: EKSource) -> Bool {
    source.sourceType == .calDAV && source.title.localizedCaseInsensitiveContains("iCloud")
}

private func calendarForSync(_ store: EKEventStore, input: CalendarInput?) throws -> EKCalendar {
    let sources = store.sources.filter(isICloud)
    guard !sources.isEmpty else {
        throw fail("未找到 iCloud 日历源。请先在 Mac 的系统设置中打开 iCloud 日历，并在日历 App 中确认账户已同步。")
    }
    if let identifier = input?.identifier, !identifier.isEmpty,
       let calendar = store.calendar(withIdentifier: identifier) {
        guard calendar.title == managedCalendarName, isICloud(calendar.source), calendar.allowsContentModifications else {
            throw fail("保存的日历标识不属于可写的 iCloud 专用日历“\(managedCalendarName)”。")
        }
        return calendar
    }
    let matches = store.calendars(for: .event).filter {
        $0.title == managedCalendarName && isICloud($0.source) && $0.allowsContentModifications
    }
    guard matches.count <= 1 else {
        throw fail("iCloud 中存在多个同名“\(managedCalendarName)”日历，请先保留一个，或指定原有日历标识。")
    }
    if let calendar = matches.first { return calendar }
    guard sources.count == 1 else {
        throw fail("检测到多个 iCloud 日历源，无法安全选择。请先创建“\(managedCalendarName)”日历后重试。")
    }
    let calendar = EKCalendar(for: .event, eventStore: store)
    calendar.title = managedCalendarName
    calendar.source = sources[0]
    calendar.cgColor = NSColor.systemTeal.cgColor
    try store.saveCalendar(calendar, commit: true)
    return calendar
}

private func ownedEvents(_ store: EKEventStore, calendar: EKCalendar, tasks: [PreparedTask], knownIDs: [String: String]) -> [EKEvent] {
    var gregorian = Calendar(identifier: .gregorian)
    gregorian.timeZone = studyTimeZone
    // EventKit limits each event predicate to a four-year interval. A narrow
    // recovery scan finds duplicates and identifiers changed by iCloud sync;
    // persisted identifiers allow cleanup outside that recovery window.
    let now = Date()
    var cursor = gregorian.date(byAdding: .year, value: -2, to: now)!
    var upper = gregorian.date(byAdding: .year, value: 2, to: now)!
    for task in tasks {
        cursor = min(cursor, task.due.addingTimeInterval(-86_400))
        upper = max(upper, task.due.addingTimeInterval(86_400))
    }
    var found: [EKEvent] = []
    var seen = Set<String>()
    for (taskID, eventID) in knownIDs {
        if let event = store.event(withIdentifier: eventID),
           event.calendar.calendarIdentifier == calendar.calendarIdentifier,
           managedTaskID(event) == taskID {
            let key = "\(event.calendarItemIdentifier)|\(event.startDate.timeIntervalSinceReferenceDate)"
            if seen.insert(key).inserted { found.append(event) }
        }
    }
    while cursor < upper {
        let next = min(gregorian.date(byAdding: .year, value: 4, to: cursor)!, upper)
        let predicate = store.predicateForEvents(withStart: cursor, end: next, calendars: [calendar])
        for event in store.events(matching: predicate) where managedTaskID(event) != nil {
            let key = "\(event.calendarItemIdentifier)|\(event.startDate.timeIntervalSinceReferenceDate)"
            if seen.insert(key).inserted { found.append(event) }
        }
        cursor = next
    }
    return found
}

private func eventNeedsUpdate(_ event: EKEvent, task: PreparedTask) -> Bool {
    let offsets = (event.alarms ?? []).filter { $0.absoluteDate == nil }.map(\.relativeOffset).sorted()
    return event.title != task.title || event.notes != task.notes || event.url != task.marker ||
        event.startDate != task.due || event.endDate != task.due.addingTimeInterval(900) ||
        event.isAllDay || event.timeZone?.identifier != studyTimeZone.identifier ||
        event.hasRecurrenceRules || (event.alarms ?? []).count != reminderOffsets.count || offsets != reminderOffsets.sorted()
}

private func configure(_ event: EKEvent, task: PreparedTask, calendar: EKCalendar) {
    event.calendar = calendar
    event.title = task.title
    event.notes = task.notes
    event.url = task.marker
    // DDL is deliberately DTSTART: both alarm offsets are relative to DDL,
    // even for an exam that has a different start_at in its notes.
    event.startDate = task.due
    event.endDate = task.due.addingTimeInterval(900)
    event.timeZone = studyTimeZone
    event.isAllDay = false
    event.recurrenceRules = nil
    event.alarms = reminderOffsets.map { EKAlarm(relativeOffset: $0) }
    event.availability = .free
}

private func synchronize(_ input: SyncInput, tasks: [PreparedTask], store: EKEventStore) throws -> SyncOutput {
    let calendar = try calendarForSync(store, input: input.calendar)
    let existing = ownedEvents(store, calendar: calendar, tasks: tasks,
                               knownIDs: input.calendar?.event_identifiers ?? [:])
    var byID: [String: [EKEvent]] = [:]
    for event in existing { byID[managedTaskID(event)!, default: []].append(event) }
    let pending = tasks.filter(\.pending)
    let pendingIDs = Set(pending.map(\.id))
    var inserted = 0
    var updated = 0
    var removed = 0
    var kept: [String: EKEvent] = [:]
    do {
        for event in existing where !pendingIDs.contains(managedTaskID(event)!) {
            try store.remove(event, span: .futureEvents, commit: false)
            removed += 1
        }
        for task in pending {
            let matches = (byID[task.id] ?? []).sorted { $0.calendarItemIdentifier < $1.calendarItemIdentifier }
            if let event = matches.first {
                kept[task.id] = event
                for duplicate in matches.dropFirst() {
                    try store.remove(duplicate, span: .futureEvents, commit: false)
                    removed += 1
                }
                if eventNeedsUpdate(event, task: task) {
                    configure(event, task: task, calendar: calendar)
                    try store.save(event, span: .futureEvents, commit: false)
                    updated += 1
                }
            } else {
                let event = EKEvent(eventStore: store)
                configure(event, task: task, calendar: calendar)
                try store.save(event, span: .thisEvent, commit: false)
                kept[task.id] = event
                inserted += 1
            }
        }
        if inserted + updated + removed > 0 { try store.commit() }
    } catch {
        store.reset()
        throw error
    }
    var eventIdentifiers: [String: String] = [:]
    for (taskID, event) in kept {
        if let identifier = event.eventIdentifier { eventIdentifiers[taskID] = identifier }
    }
    return SyncOutput(ok: true, message: "已同步到 iCloud 日历“\(managedCalendarName)”。",
                      calendar_identifier: calendar.calendarIdentifier, calendar_name: managedCalendarName,
                      inserted: inserted, updated: updated, removed: removed, event_identifiers: eventIdentifiers)
}

@MainActor
private func ensureAccess(_ store: EKEventStore) async throws {
    let current = EKEventStore.authorizationStatus(for: .event)
    if #available(macOS 14.0, *) {
        if current == .fullAccess { return }
        guard current == .notDetermined || current == .writeOnly else {
            throw fail("日历访问未获允许。请在系统设置 → 隐私与安全性 → 日历中允许“作业提醒日历桥接”完整访问。")
        }
        let granted: Bool = try await withCheckedThrowingContinuation { continuation in
            store.requestFullAccessToEvents { granted, error in
                if let error = error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: granted) }
            }
        }
        if !granted { throw fail("未授予日历完整访问权限，未同步事件。") }
    } else {
        if current == .authorized { return }
        guard current == .notDetermined else { throw fail("日历访问未获允许，请在系统设置中授权后重试。") }
        let granted: Bool = try await withCheckedThrowingContinuation { continuation in
            store.requestAccess(to: .event) { granted, error in
                if let error = error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: granted) }
            }
        }
        if !granted { throw fail("未授予日历权限，未同步事件。") }
    }
}

@main
private struct CalendarBridge {
    @MainActor
    static func main() async {
        // A diagnostic never requests access or opens the user's calendars.
        if CommandLine.arguments.dropFirst().contains("--status") {
            let current = EKEventStore.authorizationStatus(for: .event)
            let authorization: String
            if #available(macOS 14.0, *) {
                switch current {
                case .notDetermined: authorization = "not_determined"
                case .restricted: authorization = "restricted"
                case .denied: authorization = "denied"
                case .fullAccess: authorization = "full_access"
                case .writeOnly: authorization = "write_only"
                @unknown default: authorization = "unknown"
                }
            } else {
                switch current {
                case .notDetermined: authorization = "not_determined"
                case .restricted: authorization = "restricted"
                case .denied: authorization = "denied"
                case .authorized: authorization = "full_access"
                default: authorization = "unknown"
                }
            }
            let value = ["authorization": authorization,
                         "bundle_identifier": Bundle.main.bundleIdentifier ?? "unidentified"]
            if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data([10]))
            }
            return
        }
        var status: Int32 = 0
        let output: SyncOutput
        do {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            guard data.count <= 5_000_000 else { throw fail("输入超过 5 MB，未同步。") }
            let input = try JSONDecoder().decode(SyncInput.self, from: data)
            let tasks = try prepare(input)
            _ = NSApplication.shared
            NSApplication.shared.setActivationPolicy(.accessory)
            let store = EKEventStore()
            try await ensureAccess(store)
            output = try synchronize(input, tasks: tasks, store: store)
        } catch {
            status = 1
            output = SyncOutput(ok: false, message: error.localizedDescription,
                                calendar_identifier: nil, calendar_name: managedCalendarName,
                                inserted: 0, updated: 0, removed: 0, event_identifiers: [:])
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        if let data = try? encoder.encode(output) {
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data([10]))
        }
        exit(status)
    }
}
