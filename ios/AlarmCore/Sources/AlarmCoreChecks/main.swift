// Foundation-only verification for Macs whose Command Line Tools lack XCTest.
// The XCTest suite beside this target remains the full automated test suite.
import Foundation
import KeqiAlarmCore

struct CheckFailure: Error, CustomStringConvertible {
    let description: String
}

var checks = 0
func check(_ description: String, _ condition: @autoclosure () throws -> Bool) throws {
    guard try condition() else { throw CheckFailure(description: description) }
    checks += 1
}

func expectError(_ description: String, _ body: () throws -> Void) throws {
    do { try body() } catch { checks += 1; return }
    throw CheckFailure(description: description)
}

func date(_ text: String) -> Date { ISO8601DateFormatter().date(from: text)! }
let now = date("2026-10-18T12:00:00Z")
let due = date("2026-10-21T01:50:00Z")
let marker = URL(string: "studydeadline://task/task-1")!
let notes = "截止时间：2026-10-21 09:50（北京时间）\n课程：概率论\n类型：assignment\n\n本地记录更新时间：2026-10-18T08:00:00+08:00"

func parse(_ text: String = notes, title: String = "概率论 · 作业3-1",
           url: URL? = marker, start: Date = due) throws -> AlarmDeadline? {
    try CalendarDeadlineParser.parse(title: title, notes: text, url: url, startDate: start)
}

func record(_ id: String, after: TimeInterval, title: String = "作业") -> AlarmDeadline {
    AlarmDeadline(taskID: id, title: title, course: "课程", kind: "assignment",
                  due: now.addingTimeInterval(after))
}

do {
    let parsed = try parse()!
    try check("parse owned event", parsed.taskID == "task-1" && parsed.course == "概率论"
              && parsed.title == "作业3-1" && parsed.kind == "assignment" && parsed.due == due)
    try check("private event ignored", try parse("private", url: nil) == nil)
    try check("foreign URL ignored", try parse("private", url: URL(string: "https://example.invalid/a")) == nil)
    for value in ["studydeadline://task/a/b", "studydeadline://task/a%2Fb",
                  "studydeadline://task/a%20b", "studydeadline://task/a?x=1",
                  "studydeadline://user@task/a", "studydeadline://task/a#x",
                  "studydeadline://task/", "studydeadline://task/.."] {
        try check("malformed marker ignored: \(value)", try parse("invalid", url: URL(string: value)) == nil)
    }
    for field in ["截止时间", "类型", "本地记录更新时间"] {
        let missing = notes.components(separatedBy: "\n").filter { !$0.hasPrefix(field + "：") }
            .joined(separator: "\n")
        try expectError("missing \(field) must fail") { _ = try parse(missing) }
        let original = notes.components(separatedBy: "\n").first { $0.hasPrefix(field + "：") }!
        try expectError("duplicate \(field) must fail") { _ = try parse(notes + "\n" + original) }
    }
    for invalid in ["2026-02-30 09:50（北京时间）", "2026-10-21 24:00（北京时间）",
                    "2026-10-21 09:50", "0000-10-21 09:50（北京时间）"] {
        try expectError("invalid calendar date must fail") {
            _ = try parse(notes.replacingOccurrences(of: "2026-10-21 09:50（北京时间）", with: invalid))
        }
    }
    for invalid in ["2026-02-30T08:00:00+08:00", "2026-10-18T08:00:00",
                    "2026-10-18T08:00:00+99:00"] {
        try expectError("invalid update timestamp must fail") {
            _ = try parse(notes.replacingOccurrences(of: "2026-10-18T08:00:00+08:00", with: invalid))
        }
    }
    try check("sub-minute rounding accepted", try parse(start: due.addingTimeInterval(59.999)) != nil)
    for offset in [60.0, -60.0] {
        try expectError("one-minute mismatch rejected") { _ = try parse(start: due.addingTimeInterval(offset)) }
    }
    try expectError("empty stripped title rejected") { _ = try parse(title: "概率论 ·  ") }
    try expectError("unknown kind rejected") { _ = try parse(notes.replacingOccurrences(of: "assignment", with: "done")) }
    let midnight = AlarmDeadline(taskID: "midnight", title: "作业", course: "课程", kind: "assignment",
                                 due: date("2026-10-18T17:00:00Z"))
    let midnightPlan = try DeadlinePlanner.plan(records: [midnight], now: now)
    try check("Beijing midnight crossing uses absolute two hours",
              midnightPlan.first?.fireDate == date("2026-10-18T15:00:00Z"))
    let boundaryPlans = try DeadlinePlanner.plan(records: [record("expired", after: -1),
        record("late", after: 7199), record("exact", after: 7200), record("next", after: 7200.001)], now: now)
    try check("never backfill passed or current reminder point", boundaryPlans.map(\.taskID) == ["next"])
    let horizonPlans = try DeadlinePlanner.plan(records: [record("at", after: 93600),
        record("outside", after: 93600.001)], now: now, horizon: 86400)
    try check("horizon inclusive", horizonPlans.map(\.taskID) == ["at"])
    let a = record("a", after: 86400)
    try check("identical records deduplicate", try DeadlinePlanner.plan(records: [a, a], now: now).count == 1)
    try expectError("conflicts outside horizon fail before capacity") {
        _ = try DeadlinePlanner.plan(records: [record("old", after: -86400),
            record("old", after: -86400, title: "changed")], now: now, maxCount: 0)
    }
    let sorted = try DeadlinePlanner.plan(records: [record("c", after: 14000),
        record("b", after: 12000), record("a", after: 12000)], now: now, maxCount: 2)
    try check("capacity keeps earliest with stable ID tie", sorted.map(\.taskID) == ["a", "b"])
    let many = (0..<40).map { record(String($0), after: 86400 + Double($0)) }
    try check("default capacity thirty", try DeadlinePlanner.plan(records: many, now: now).count == 30)
    try check("zero capacity allowed", try DeadlinePlanner.plan(records: many, now: now, maxCount: 0).isEmpty)
    for horizon in [0.0, -1.0, .nan, .infinity] {
        try expectError("invalid planning horizon rejected") {
            _ = try DeadlinePlanner.plan(records: [a], now: now, horizon: horizon)
        }
    }
    let encoder = JSONEncoder()
    try check("deadline JSON round trip", try JSONDecoder().decode(AlarmDeadline.self,
        from: encoder.encode(parsed)) == parsed)
    try check("plan JSON round trip", try JSONDecoder().decode([DeadlineAlarmPlan].self,
        from: encoder.encode(sorted)) == sorted)
    print("KeqiAlarmCore Foundation checks passed: \(checks)")
} catch {
    FileHandle.standardError.write(Data("KeqiAlarmCore verification failed: \(error)\n".utf8))
    exit(1)
}
