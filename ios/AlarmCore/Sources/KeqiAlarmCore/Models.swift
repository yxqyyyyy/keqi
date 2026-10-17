import Foundation

public struct AlarmDeadline: Identifiable, Codable, Equatable, Sendable {
    public let taskID: String
    public let title: String
    public let course: String
    public let kind: String
    public let due: Date
    public var id: String { taskID }

    public init(taskID: String, title: String, course: String, kind: String, due: Date) {
        self.taskID = taskID
        self.title = title
        self.course = course
        self.kind = kind
        self.due = due
    }
}

public struct DeadlineAlarmPlan: Identifiable, Codable, Equatable, Sendable {
    public let taskID: String
    public let title: String
    public let course: String
    public let kind: String
    public let due: Date
    public let fireDate: Date
    public var id: String { taskID }

    public init(taskID: String, title: String, course: String, kind: String,
                due: Date, fireDate: Date) {
        self.taskID = taskID
        self.title = title
        self.course = course
        self.kind = kind
        self.due = due
        self.fireDate = fireDate
    }
}

public enum AlarmCoreError: Error, Equatable, LocalizedError, Sendable {
    case invalidMetadata(field: String)
    case duplicateMetadata(field: String)
    case invalidTitle
    case eventDateMismatch
    case invalidRecord(taskID: String)
    case conflictingRecord(taskID: String)
    case invalidPlanningConfiguration

    public var errorDescription: String? {
        switch self {
        case .invalidMetadata(let field):
            return "课期日历的\(field)缺失或格式无效，请先在 Mac 重新同步日历。"
        case .duplicateMetadata(let field):
            return "课期日历存在重复的\(field)，已停止更新闹钟。"
        case .invalidTitle:
            return "课期日历的任务标题无效，已停止更新闹钟。"
        case .eventDateMismatch:
            return "课期日历的事件时间与备注截止时间不一致，已停止更新闹钟。"
        case .invalidRecord(let id):
            return "课期任务 \(id) 的闹钟资料无效，已停止更新闹钟。"
        case .conflictingRecord(let id):
            return "课期任务 \(id) 有不一致的日历副本，已停止更新闹钟。"
        case .invalidPlanningConfiguration:
            return "课期闹钟的时间范围或容量设置无效。"
        }
    }
}
