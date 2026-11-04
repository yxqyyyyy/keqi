import Foundation
import XCTest
@testable import KeqiAlarmCore

final class DeadlinePlannerTests: XCTestCase {
    private let now = ISO8601DateFormatter().date(from: "2026-10-18T12:00:00Z")!

    private func record(_ id: String = "a", secondsUntilDue: TimeInterval,
                        title: String = "练习", kind: String = "assignment") -> AlarmDeadline {
        AlarmDeadline(taskID: id, title: title, course: "课程", kind: kind,
                      due: now.addingTimeInterval(secondsUntilDue))
    }

    func testTwoHoursAreSubtractedAcrossBeijingMidnight() throws {
        // 2026-10-19 01:00 Beijing => alarm on October 18 at 23:00 Beijing.
        let due = ISO8601DateFormatter().date(from: "2026-10-18T17:00:00Z")!
        let item = AlarmDeadline(taskID: "midnight", title: "作业", course: "大物",
                                 kind: "assignment", due: due)
        let plan = try XCTUnwrap(DeadlinePlanner.plan(records: [item], now: now).first)
        XCTAssertEqual(plan.fireDate, ISO8601DateFormatter().date(from: "2026-10-18T15:00:00Z")!)
        XCTAssertEqual(plan.due.timeIntervalSince(plan.fireDate), 7200)
        XCTAssertEqual(plan.id, item.taskID)
    }

    func testAbsolutePlanIsSameRegardlessOfDeviceTimeZone() throws {
        let item = record(secondsUntilDue: 3 * 3600)
        let plan = try XCTUnwrap(DeadlinePlanner.plan(records: [item], now: now).first)
        var beijing = Calendar(identifier: .gregorian)
        beijing.timeZone = TimeZone(secondsFromGMT: 8 * 3600)!
        var losAngeles = Calendar(identifier: .gregorian)
        losAngeles.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        XCTAssertEqual(beijing.component(.hour, from: plan.fireDate), 21)
        XCTAssertEqual(losAngeles.component(.hour, from: plan.fireDate), 6)
        XCTAssertEqual(plan.fireDate, now.addingTimeInterval(3600))
    }

    func testPassedReminderPointIsNeverBackfilledAndBoundaryIsExclusive() throws {
        let items = [record("expired", secondsUntilDue: -1),
                     record("soon", secondsUntilDue: 7199),
                     record("exact", secondsUntilDue: 7200),
                     record("future", secondsUntilDue: 7200.001)]
        let plans = try DeadlinePlanner.plan(records: items, now: now)
        XCTAssertEqual(plans.map(\.taskID), ["future"])
    }

    func testHorizonBoundaryIsInclusiveForFireDate() throws {
        let horizon: TimeInterval = 86400
        let items = [record("at", secondsUntilDue: horizon + 7200),
                     record("beyond", secondsUntilDue: horizon + 7200.001)]
        let plans = try DeadlinePlanner.plan(records: items, now: now, horizon: horizon)
        XCTAssertEqual(plans.map(\.taskID), ["at"])
    }

    func testIdenticalRecordsDeduplicate() throws {
        let item = record(secondsUntilDue: 86400)
        let plans = try DeadlinePlanner.plan(records: [item, item, item], now: now)
        XCTAssertEqual(plans.count, 1)
        XCTAssertEqual(plans.first?.title, item.title)
    }

    func testConflictingDuplicateFailsEvenOutsideHorizonOrCapacity() throws {
        let item = record(secondsUntilDue: -86400)
        let changed = record(secondsUntilDue: -86400, title: "有冲突的旧记录")
        XCTAssertThrowsError(try DeadlinePlanner.plan(records: [item, changed], now: now, maxCount: 0)) { error in
            XCTAssertEqual(error as? AlarmCoreError, .conflictingRecord(taskID: "a"))
        }
        let future = record(secondsUntilDue: 30 * 86400)
        let earlier = record(secondsUntilDue: 31 * 86400)
        XCTAssertThrowsError(try DeadlinePlanner.plan(records: [future, earlier], now: now))
    }

    func testSameIDWithDifferentCourseKindOrDeadlineThrows() throws {
        let item = record(secondsUntilDue: 86400)
        let conflicts = [
            AlarmDeadline(taskID: "a", title: item.title, course: "其他课", kind: item.kind, due: item.due),
            AlarmDeadline(taskID: "a", title: item.title, course: item.course, kind: "exam", due: item.due),
            AlarmDeadline(taskID: "a", title: item.title, course: item.course, kind: item.kind,
                          due: item.due.addingTimeInterval(1))
        ]
        for conflict in conflicts {
            XCTAssertThrowsError(try DeadlinePlanner.plan(records: [item, conflict], now: now))
        }
    }

    func testEarliestAlarmsWinCapacityAndTiesUseTaskID() throws {
        let items = [record("z", secondsUntilDue: 15000), record("b", secondsUntilDue: 12000),
                     record("a", secondsUntilDue: 12000), record("c", secondsUntilDue: 13000)]
        let plans = try DeadlinePlanner.plan(records: items, now: now, maxCount: 3)
        XCTAssertEqual(plans.map(\.taskID), ["a", "b", "c"])
        XCTAssertEqual(try DeadlinePlanner.plan(records: Array(items.reversed()), now: now, maxCount: 3), plans)
    }

    func testDefaultCapacityIsThirtyAndDefaultHorizonFourteenDays() throws {
        let items = (0..<40).map { record(String(format: "%02d", $0), secondsUntilDue: 86400 + Double($0)) }
            + [record("far", secondsUntilDue: 15 * 86400)]
        let plans = try DeadlinePlanner.plan(records: items, now: now)
        XCTAssertEqual(plans.count, 30)
        XCTAssertEqual(plans.first?.taskID, "00")
        XCTAssertEqual(plans.last?.taskID, "29")
        XCTAssertFalse(plans.contains { $0.taskID == "far" })
        XCTAssertTrue(try DeadlinePlanner.plan(records: items, now: now, maxCount: 0).isEmpty)
    }

    func testInvalidConfigurationAndRecordsThrow() throws {
        let valid = record(secondsUntilDue: 86400)
        for horizon in [0.0, -1.0, .infinity, .nan] {
            XCTAssertThrowsError(try DeadlinePlanner.plan(records: [valid], now: now, horizon: horizon))
        }
        XCTAssertThrowsError(try DeadlinePlanner.plan(records: [valid], now: now, maxCount: -1))
        XCTAssertThrowsError(try DeadlinePlanner.plan(records: [valid], now: Date(timeIntervalSince1970: .nan)))
        let invalid = [record(" ", secondsUntilDue: 86400), record(secondsUntilDue: 86400, title: " "),
                       record(secondsUntilDue: 86400, kind: "completed"),
                       AlarmDeadline(taskID: "bad", title: "a", course: "", kind: "assignment",
                                     due: Date(timeIntervalSince1970: .infinity))]
        for item in invalid {
            XCTAssertThrowsError(try DeadlinePlanner.plan(records: [item], now: now))
        }
    }
}
