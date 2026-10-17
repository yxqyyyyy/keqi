import Foundation

public enum DeadlinePlanner {
    /// Uses absolute dates, independently of the phone's current time zone.
    /// Expired two-hour reminder points are intentionally never scheduled late.
    public static func plan(records: [AlarmDeadline], now: Date,
                            horizon: TimeInterval = 14 * 86400,
                            maxCount: Int = 30) throws -> [DeadlineAlarmPlan] {
        let latest = now.addingTimeInterval(horizon)
        guard now.timeIntervalSinceReferenceDate.isFinite, horizon.isFinite, horizon > 0,
              latest.timeIntervalSinceReferenceDate.isFinite, maxCount >= 0 else {
            throw AlarmCoreError.invalidPlanningConfiguration
        }
        var unique: [String: AlarmDeadline] = [:]
        for record in records {
            guard !record.taskID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  !record.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  ["assignment", "exam"].contains(record.kind),
                  record.due.timeIntervalSinceReferenceDate.isFinite else {
                throw AlarmCoreError.invalidRecord(taskID: record.taskID)
            }
            if let previous = unique[record.taskID], previous != record {
                throw AlarmCoreError.conflictingRecord(taskID: record.taskID)
            }
            unique[record.taskID] = record
        }
        let plans = unique.values.compactMap { record -> DeadlineAlarmPlan? in
            let fireDate = record.due.addingTimeInterval(-7200)
            guard fireDate > now, fireDate <= latest else { return nil }
            return DeadlineAlarmPlan(taskID: record.taskID, title: record.title,
                                     course: record.course, kind: record.kind,
                                     due: record.due, fireDate: fireDate)
        }.sorted {
            if $0.fireDate != $1.fireDate { return $0.fireDate < $1.fireDate }
            return $0.taskID < $1.taskID
        }
        return Array(plans.prefix(maxCount))
    }
}
