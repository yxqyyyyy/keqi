import AlarmKit
import BackgroundTasks
import Combine
import CryptoKit
import EventKit
import Foundation
import KeqiAlarmCore
import SwiftUI

private struct DeadlineAlarmMetadata: AlarmMetadata {
    let taskID: String
    let isTest: Bool
}

private struct ManagedAlarm: Codable {
    let id: UUID
    let plan: DeadlineAlarmPlan
    let isTest: Bool
    var confirmed: Bool
}

private struct AlarmJournal: Codable {
    var version = 1
    var alarms: [ManagedAlarm] = []
}

private struct AlarmServiceError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// AlarmKit owns these fixed-date alarms on this iPhone. This service does not
/// modify Clock's personal alarms, calendar events, or the Mac's notifications.
@MainActor
final class AlarmCoordinator: ObservableObject {
    static let shared = AlarmCoordinator()
    static let refreshIdentifier = "local.studydeadline.KeqiAlarm.refresh"

    @Published private(set) var status = "尚未启用 iPhone 两小时前闹钟。"
    @Published private(set) var items: [DeadlineAlarmPlan] = []
    @Published private(set) var isEnabled = false
    @Published private(set) var isWorking = false
    @Published private(set) var lastSyncedAt: Date?
    @Published private(set) var calendarChoices: [CalendarChoice] = []
    @Published var selectedCalendarIdentifier: String {
        didSet { defaults.set(selectedCalendarIdentifier, forKey: "selectedCalendarIdentifier") }
    }

    private let manager = AlarmManager.shared
    private let reader = CalendarReader()
    private let defaults = UserDefaults.standard
    private var wantsEnabled = false
    private var journal = AlarmJournal()
    private var journalError: Error?
    private var resyncRequested = false
    private var changeObserver: Task<Void, Never>?
    private var authorizationObserver: Task<Void, Never>?
    private let journalURL: URL

    private init() {
        selectedCalendarIdentifier = UserDefaults.standard.string(forKey: "selectedCalendarIdentifier") ?? ""
        journalURL = URL.applicationSupportDirectory.appending(path: "KeqiAlarm", directoryHint: .isDirectory)
            .appending(path: "managed-alarms.json")
        wantsEnabled = defaults.bool(forKey: "alarmSyncEnabled")
        lastSyncedAt = defaults.object(forKey: "lastSyncedAt") as? Date
        do {
            if FileManager.default.fileExists(atPath: journalURL.path) {
                journal = try JSONDecoder().decode(AlarmJournal.self, from: Data(contentsOf: journalURL))
                guard journal.version == 1,
                      Set(journal.alarms.map(\.id)).count == journal.alarms.count,
                      journal.alarms.allSatisfy({ $0.id == Self.alarmID(for: $0.plan, isTest: $0.isTest) }) else {
                    throw AlarmServiceError(message: "本机闹钟记录格式无效，已停止自动修改闹钟。")
                }
            }
        } catch {
            journalError = error
            status = "本机闹钟记录无法读取，已停止自动修改：\(error.localizedDescription)"
        }
        updateEnabledState()
        if wantsEnabled && !isEnabled && journalError == nil {
            status = "自动同步尚未恢复，请检查日历和闹钟权限，再点“启用并同步”。"
        }
        // Mapping Notification to Bool avoids sending an EventKit/Notification
        // object between concurrency domains. The store stays on the main actor.
        let changes = NotificationCenter.default.notifications(named: .EKEventStoreChanged).map { _ in true }
        changeObserver = Task { [weak self] in
            for await _ in changes {
                guard let self else { return }
                await self.syncIfEnabled()
            }
        }
        authorizationObserver = Task { [weak self] in
            guard let self else { return }
            for await state in self.manager.authorizationUpdates {
                self.updateEnabledState()
                if self.wantsEnabled && state != .authorized {
                    self.status = "闹钟权限未授权，无法确认闹钟是否仍会响。请在 iPhone 设置中检查权限。"
                }
            }
        }
    }

    /// Only a direct user action calls either permission request.
    func enableAndSync() async {
        guard !isWorking else { return }
        isWorking = true
        defer { finishWork() }
        do {
            try ensureJournalReadable()
            try await reader.requestAccessFromUserAction()
            calendarChoices = try reader.choices()
            guard try await manager.requestAuthorization() == .authorized else {
                throw AlarmServiceError(message: "未获得闹钟权限，请在 iPhone 设置中允许后重试。")
            }
            wantsEnabled = true
            defaults.set(true, forKey: "alarmSyncEnabled")
            try await synchronize()
            scheduleBackgroundRefresh()
        } catch {
            status = "未完成启用或同步：\(error.localizedDescription) 已排定的闹钟不会因此全部删除。"
        }
    }

    /// Opening the app, foreground EventKit changes, and a best-effort background
    /// refresh call this method. It never prompts for permissions in background.
    func syncIfEnabled() async {
        updateEnabledState()
        guard wantsEnabled else { return }
        guard !isWorking else { resyncRequested = true; return }
        isWorking = true
        defer { finishWork() }
        do {
            try ensureJournalReadable()
            guard manager.authorizationState == .authorized else {
                throw AlarmServiceError(message: "闹钟权限已关闭，请在 iPhone 设置中允许后重试。")
            }
            guard reader.hasFullAccess else { throw CalendarReadError.permissionDenied }
            try await synchronize()
        } catch {
            status = "同步未完成：\(error.localizedDescription) 未因读取失败清空现有闹钟。"
        }
    }

    func disableAndCancel() async {
        guard !isWorking else { return }
        isWorking = true
        wantsEnabled = false
        defaults.set(false, forKey: "alarmSyncEnabled")
        updateEnabledState()
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: Self.refreshIdentifier)
        defer { finishWork() }
        do {
            try ensureJournalReadable()
            let actual = try manager.alarms
            let live = Set(actual.map(\.id))
            var failures = 0
            for entry in Array(journal.alarms) {
                do {
                    if live.contains(entry.id) { try manager.cancel(id: entry.id) }
                    journal.alarms.removeAll { $0.id == entry.id }
                    try saveJournal()
                } catch { failures += 1 }
            }
            items = []
            status = failures == 0 ? "已关闭自动同步，并取消本 App 记录的闹钟。" :
                "已关闭自动同步，但有 \(failures) 个闹钟未能确认取消。请恢复权限后再次点关闭，或在系统闹钟界面检查。"
        } catch {
            status = "已关闭自动同步，但无法确认闹钟已取消：\(error.localizedDescription)"
        }
    }

    /// Deliberately separate from enable: never schedule a surprise test alarm.
    func testAlarm() async {
        guard !isWorking else { return }
        isWorking = true
        defer { finishWork() }
        do {
            try ensureJournalReadable()
            guard try await manager.requestAuthorization() == .authorized else {
                throw AlarmServiceError(message: "未获得闹钟权限，未排定测试闹钟。")
            }
            let fire = Date().addingTimeInterval(60)
            let plan = DeadlineAlarmPlan(taskID: "__keqi_test__", title: "课期闹钟测试", course: "",
                                         kind: "assignment", due: fire.addingTimeInterval(7200), fireDate: fire)
            let keepID = try await install(plan, isTest: true)
            let actual = Set(try manager.alarms.map(\.id))
            let failures = try cancelObsolete(where: { $0.isTest && $0.id != keepID }, actual: actual)
            status = failures == 0 ? "已排定 1 分钟后的测试闹钟。请锁屏并确认实际响铃；这不会启用课程自动同步。" :
                "已排定测试闹钟，但旧测试闹钟未全部取消，请检查系统闹钟。"
        } catch {
            status = "测试未完成：\(error.localizedDescription)"
        }
    }

    func scheduleBackgroundRefresh() {
        guard wantsEnabled else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshIdentifier)
        request.earliestBeginDate = Date().addingTimeInterval(15 * 60)
        do {
            // Replace this app's own pending request, never other task identifiers.
            BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: Self.refreshIdentifier)
            try BGTaskScheduler.shared.submit(request)
        } catch {
            status += " 后台刷新申请未成功；打开 App 时仍可更新。iOS 不保证后台执行时间。"
        }
    }

    private func synchronize() async throws {
        try Task.checkCancellation()
        calendarChoices = try reader.choices()
        let now = Date()
        let snapshot = try reader.read(selectedIdentifier: selectedCalendarIdentifier, now: now)
        selectedCalendarIdentifier = snapshot.identifier
        let plans = try DeadlinePlanner.plan(records: snapshot.records, now: now)
        // Planning validates conflicting duplicate IDs before any mutations.
        // Identical duplicate calendar rows are harmless and collapse here.
        let recordsByID = snapshot.records.reduce(into: [String: AlarmDeadline]()) {
            $0[$1.taskID] = $1
        }
        let actual = try manager.alarms
        var desired = Set(plans.map { Self.alarmID(for: $0, isTest: false) })
        var installed: [DeadlineAlarmPlan] = []
        // Schedule first; if a replacement fails, its previous alarm survives.
        // The stable ID encodes the configuration, so no undocumented same-ID
        // update semantics are needed when a task's title or deadline changes.
        for plan in plans {
            try Task.checkCancellation()
            guard selectedCalendarIdentifier == snapshot.identifier else {
                throw AlarmServiceError(message: "选择的日历已改变，请重新同步；现有闹钟已保留。")
            }
            let id = Self.alarmID(for: plan, isTest: false)
            let current = actual.first(where: { $0.id == id })
            let recorded = journal.alarms.first(where: { $0.id == id })
            let sameDate = current?.schedule == .fixed(plan.fireDate)
            if sameDate {
                // Reconcile a crash after AlarmKit succeeded but before the
                // confirmation write. Its configuration-derived ID and actual
                // fixed date prove this exact alarm is already installed.
                if recorded?.confirmed != true {
                    journal.alarms.removeAll { $0.id == id }
                    journal.alarms.append(ManagedAlarm(id: id, plan: plan, isTest: false, confirmed: true))
                    try saveJournal()
                }
            } else if current != nil {
                throw AlarmServiceError(message: "系统中的某个闹钟时间与本机记录不一致，已保留现有闹钟。请关闭本 App 闹钟后重新启用。")
            } else {
                _ = try await install(plan, isTest: false)
            }
            installed.append(plan)
        }
        try Task.checkCancellation()
        guard selectedCalendarIdentifier == snapshot.identifier else {
            throw AlarmServiceError(message: "选择的日历已改变，请重新同步；现有闹钟已保留。")
        }
        guard reader.hasFullAccess, manager.authorizationState == .authorized else {
            throw AlarmServiceError(message: "同步期间权限已改变，已停止清理旧闹钟。请检查 iPhone 权限后重试。")
        }
        let refreshed = Set(try manager.alarms.map(\.id))
        // A fixed alarm disappears from the future plan as soon as its fire
        // point passes. That is not completion: it might currently be ringing.
        // Keep the actual system alarm while the calendar still contains the
        // same task and deadline. Do not schedule a replacement after its point.
        let retainedAtPoint = journal.alarms.filter { entry in
            !entry.isTest && entry.plan.fireDate <= now && refreshed.contains(entry.id) &&
                recordsByID[entry.plan.taskID]?.due == entry.plan.due
        }
        desired.formUnion(retainedAtPoint.map(\.id))
        let failures = try cancelObsolete(where: { !$0.isTest && !desired.contains($0.id) }, actual: refreshed)
        items = installed
        guard failures == 0 else {
            throw AlarmServiceError(message: "新闹钟已排定，但 \(failures) 个旧闹钟未能确认取消；下次同步会重试。")
        }
        lastSyncedAt = Date()
        defaults.set(lastSyncedAt, forKey: "lastSyncedAt")
        let passedCount = recordsByID.values.filter { $0.due.addingTimeInterval(-7200) <= now }.count
        let passedText = passedCount == 0 ? "" : " 另有 \(passedCount) 个任务已过两小时前提醒点，不补设迟到闹钟。"
        let retainedText = retainedAtPoint.isEmpty ? "" : " 已保留 \(retainedAtPoint.count) 个已到点且仍在系统中的闹钟，等待本人停止或作业状态更新。"
        status = "已核对并排定 \(plans.count) 个两小时前闹钟（未来 14 天，最多 30 个）。\(passedText)\(retainedText) 已提交取消取决于 iCloud 更新及本 App 下次获得运行机会。"
    }

    @discardableResult
    private func install(_ plan: DeadlineAlarmPlan, isTest: Bool) async throws -> UUID {
        let id = Self.alarmID(for: plan, isTest: isTest)
        let entry = ManagedAlarm(id: id, plan: plan, isTest: isTest, confirmed: false)
        journal.alarms.removeAll { $0.id == id }
        journal.alarms.append(entry)
        // Write intent before calling AlarmKit, so a crash immediately after
        // scheduling cannot leave an untracked alarm which this app cannot cancel.
        try saveJournal()
        let text = isTest ? "课期闹钟测试" : "还有 2 小时截止：\(plan.course.isEmpty ? "" : plan.course + " · ")\(plan.title)"
        let title = LocalizedStringResource(stringLiteral: text)
        // This initializer is available in the iOS 26.0 SDK bundled with
        // Xcode 26.0.1 (compatible with macOS 15.6). It remains available, though
        // deprecated in favor of a system stop control, in the iOS 26.1 SDK.
        // A runtime #available check alone cannot make a 26.1-only declaration
        // compile with the 26.0 SDK, so this project uses the compatible API.
        let alert = AlarmPresentation.Alert(title: title,
            stopButton: AlarmButton(text: "停止", textColor: .white, systemImageName: "stop.circle"))
        let attributes = AlarmAttributes(presentation: AlarmPresentation(alert: alert),
            metadata: DeadlineAlarmMetadata(taskID: plan.taskID, isTest: isTest), tintColor: .orange)
        let config = AlarmManager.AlarmConfiguration.alarm(schedule: .fixed(plan.fireDate), attributes: attributes)
        _ = try await manager.schedule(id: id, configuration: config)
        if let index = journal.alarms.firstIndex(where: { $0.id == id }) {
            journal.alarms[index].confirmed = true
        }
        try saveJournal()
        return id
    }

    /// Only journaled IDs are cancelled. A read/parse/schedule failure never
    /// reaches this cleanup phase. Each successful cancellation is persisted.
    private func cancelObsolete(where obsolete: (ManagedAlarm) -> Bool, actual: Set<UUID>) throws -> Int {
        var failures = 0
        for entry in journal.alarms.filter(obsolete) {
            do {
                if actual.contains(entry.id) { try manager.cancel(id: entry.id) }
                journal.alarms.removeAll { $0.id == entry.id }
                try saveJournal()
            } catch { failures += 1 }
        }
        return failures
    }

    private func updateEnabledState() {
        isEnabled = wantsEnabled && journalError == nil && manager.authorizationState == .authorized && reader.hasFullAccess
    }

    private func finishWork() {
        isWorking = false
        updateEnabledState()
        if resyncRequested && wantsEnabled && !Task.isCancelled {
            resyncRequested = false
            Task { [weak self] in await self?.syncIfEnabled() }
        } else {
            resyncRequested = false
        }
    }

    private func ensureJournalReadable() throws {
        if let journalError { throw journalError }
    }

    private func saveJournal() throws {
        let directory = journalURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(journal)
        try data.write(to: journalURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    private static func alarmID(for plan: DeadlineAlarmPlan, isTest: Bool) -> UUID {
        // Length-prefixed fields keep IDs unambiguous even with punctuation in a
        // course name. No unstable Swift Hasher values are persisted.
        let fields = ["local.studydeadline.KeqiAlarm.fixed.v1", isTest ? "test" : "deadline", plan.taskID,
                      plan.title, plan.course, plan.kind,
                      String(plan.due.timeIntervalSinceReferenceDate.bitPattern),
                      String(plan.fireDate.timeIntervalSinceReferenceDate.bitPattern)]
        let key = fields.map { "\($0.utf8.count):\($0)" }.joined()
        var bytes = Array(SHA256.hash(data: Data(key.utf8)).prefix(16))
        bytes[6] = (bytes[6] & 0x0f) | 0x50
        bytes[8] = (bytes[8] & 0x3f) | 0x80
        return UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
                           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]))
    }
}
