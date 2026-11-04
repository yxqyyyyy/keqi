import EventKit
import Foundation
import KeqiAlarmCore

struct CalendarChoice: Identifiable, Equatable {
    let id: String
    let name: String
    let title: String
}

enum CalendarReadError: LocalizedError {
    case permissionRequired, permissionDenied, missingCalendar, selectCalendar, selectedCalendarMissing

    var errorDescription: String? {
        switch self {
        case .permissionRequired: return "请先点“启用并同步”，授权读取 iCloud 日历。"
        case .permissionDenied: return "未获得日历完整访问权限，请在 iPhone 设置中允许后重试。"
        case .missingCalendar: return "未找到 iCloud 的“作业与考试”日历。请先让 Mac 同步，并在 iPhone 日历 App 中确认已出现。"
        case .selectCalendar: return "发现多个同名 iCloud 日历，请先明确选择一个；现有闹钟已保留。"
        case .selectedCalendarMissing: return "原来选定的 iCloud 日历暂不可用，请确认账户同步或重新选择；现有闹钟已保留。"
        }
    }
}

/// Reads only the dedicated iCloud calendar and never saves or deletes events.
/// Full access is needed because EventKit has no read-only permission.
@MainActor
final class CalendarReader {
    private let store = EKEventStore()
    private let calendarName = "作业与考试"

    var hasFullAccess: Bool {
        EKEventStore.authorizationStatus(for: .event) == .fullAccess
    }

    func requestAccessFromUserAction() async throws {
        if hasFullAccess { return }
        guard try await store.requestFullAccessToEvents() else {
            throw CalendarReadError.permissionDenied
        }
        // No reads take place before permission, but reset also handles upgrades
        // from a previous write-only grant without using cached EventKit objects.
        store.reset()
        guard hasFullAccess else { throw CalendarReadError.permissionDenied }
    }

    func choices() throws -> [CalendarChoice] {
        guard hasFullAccess else { throw CalendarReadError.permissionRequired }
        return eligibleCalendars().map { calendar in
            CalendarChoice(id: calendar.calendarIdentifier, name: calendar.title,
                           title: "\(calendar.title) · \(calendar.source.title) · \(calendar.calendarIdentifier.suffix(8))")
        }.sorted { $0.id < $1.id }
    }

    func read(selectedIdentifier: String, now: Date) throws -> (identifier: String, records: [AlarmDeadline]) {
        guard hasFullAccess else { throw CalendarReadError.permissionRequired }
        let candidates = eligibleCalendars()
        let calendar: EKCalendar
        if !selectedIdentifier.isEmpty {
            guard let selected = candidates.first(where: { $0.calendarIdentifier == selectedIdentifier }) else {
                throw CalendarReadError.selectedCalendarMissing
            }
            calendar = selected
        } else {
            guard !candidates.isEmpty else { throw CalendarReadError.missingCalendar }
            guard candidates.count == 1 else { throw CalendarReadError.selectCalendar }
            calendar = candidates[0]
        }
        let predicate = store.predicateForEvents(withStart: now.addingTimeInterval(-86400),
                                                 end: now.addingTimeInterval(16 * 86400),
                                                 calendars: [calendar])
        let records = try store.events(matching: predicate).compactMap { event in
            // The core parser ignores unrelated events and throws for malformed
            // owned events. Any such error aborts the snapshot before cancellation.
            try CalendarDeadlineParser.parse(title: event.title ?? "", notes: event.notes ?? "",
                                              url: event.url, startDate: event.startDate ?? .distantPast)
        }
        return (calendar.calendarIdentifier, records)
    }

    private func eligibleCalendars() -> [EKCalendar] {
        store.calendars(for: .event).filter {
            $0.title == calendarName && $0.source.sourceType == .calDAV &&
                $0.source.title.localizedCaseInsensitiveContains("iCloud")
        }
    }
}
