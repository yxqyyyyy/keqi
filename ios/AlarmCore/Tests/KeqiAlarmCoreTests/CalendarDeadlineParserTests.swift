import Foundation
import XCTest
@testable import KeqiAlarmCore

final class CalendarDeadlineParserTests: XCTestCase {
    private let marker = URL(string: "studydeadline://task/task-123")!

    private func date(_ iso: String) -> Date {
        ISO8601DateFormatter().date(from: iso)!
    }

    private func notes(due: String = "2026-10-21 09:50（北京时间）",
                       kind: String = "assignment",
                       updated: String = "2026-10-18T08:00:00+08:00") -> String {
        "截止时间：\(due)\n课程：概率论\n类型：\(kind)\n原始页面：https://example.invalid/homework\n\n本地记录更新时间：\(updated)"
    }

    private func parse(_ text: String, title: String = "概率论 · 作业3-1",
                        start: Date? = nil, url: URL? = nil) throws -> AlarmDeadline? {
        try CalendarDeadlineParser.parse(title: title, notes: text, url: url ?? marker,
                                         startDate: start ?? date("2026-10-21T01:50:00Z"))
    }

    func testOwnedHomeworkUsesBeijingAbsoluteDateAndStripsOnlyKnownCoursePrefix() throws {
        let result = try XCTUnwrap(parse(notes()))
        XCTAssertEqual(result.taskID, "task-123")
        XCTAssertEqual(result.id, result.taskID)
        XCTAssertEqual(result.title, "作业3-1")
        XCTAssertEqual(result.course, "概率论")
        XCTAssertEqual(result.kind, "assignment")
        XCTAssertEqual(result.due, date("2026-10-21T01:50:00Z"))
        let unprefixed = try XCTUnwrap(parse(notes(), title: "复习 · 第三章"))
        XCTAssertEqual(unprefixed.title, "复习 · 第三章")
    }

    func testPrivateEventsReturnNilEvenWithInvalidMetadata() throws {
        for url in [nil, URL(string: "https://example.invalid/task/a"),
                    URL(string: "studydeadline://other/a")] as [URL?] {
            let result = try CalendarDeadlineParser.parse(title: "私人活动", notes: "私人备注",
                url: url, startDate: date("2026-10-21T01:50:00Z"))
            XCTAssertNil(result)
        }
    }

    func testMalformedMarkersAreNeverManaged() throws {
        for url in ["studydeadline://task/", "studydeadline://task/a/b",
                    "studydeadline://task/a%2Fb", "studydeadline://task/a%5Cb",
                    "studydeadline://task/a%20b", "studydeadline://task/%0A",
                    "studydeadline://task/.", "studydeadline://task/..",
                    "studydeadline://user@task/a", "studydeadline://task:23/a",
                    "studydeadline://task/a?view=1", "studydeadline://task/a#alarm"] {
            let result = try CalendarDeadlineParser.parse(title: "", notes: "",
                url: URL(string: url), startDate: Date())
            XCTAssertNil(result, url)
        }
    }

    func testEncodedUnicodeIDIsPreserved() throws {
        let result = try XCTUnwrap(parse(notes(), url: URL(string: "studydeadline://task/%E4%BD%9C%E4%B8%9A-1")))
        XCTAssertEqual(result.taskID, "作业-1")
    }

    func testNoCourseMetadataKeepsWholeTitle() throws {
        let text = notes().replacingOccurrences(of: "课程：概率论\n", with: "")
        let result = try XCTUnwrap(parse(text, title: "期末考试"))
        XCTAssertEqual(result.title, "期末考试")
        XCTAssertEqual(result.course, "")
    }

    func testMissingRequiredMetadataThrows() throws {
        for field in ["截止时间", "类型", "本地记录更新时间"] {
            let text = notes().components(separatedBy: "\n")
                .filter { !$0.hasPrefix(field + "：") }.joined(separator: "\n")
            XCTAssertThrowsError(try parse(text)) { error in
                XCTAssertEqual(error as? AlarmCoreError, .invalidMetadata(field: field))
            }
        }
    }

    func testDuplicateMetadataThrowsEvenWhenValuesAgree() throws {
        for line in ["截止时间：2026-10-21 09:50（北京时间）", "类型：assignment",
                     "课程：概率论", "本地记录更新时间：2026-10-18T08:00:00+08:00"] {
            let name = String(line.prefix { $0 != "：" })
            XCTAssertThrowsError(try parse(notes() + "\n" + line)) { error in
                XCTAssertEqual(error as? AlarmCoreError, .duplicateMetadata(field: name))
            }
        }
    }

    func testStrictDateRejectsCalendarRolloverAndMissingZoneAnnotation() throws {
        for due in ["2026-02-30 09:50（北京时间）", "2026-02-29 09:50（北京时间）",
                    "2026-10-21 24:00（北京时间）", "2026-10-21 09:60（北京时间）",
                    "2026-13-21 09:50（北京时间）", "2026-10-21 09:50",
                    "2026-10-21 09:50 (北京时间)", "0000-10-21 09:50（北京时间）"] {
            XCTAssertThrowsError(try parse(notes(due: due)), due)
        }
        let validLeap = try XCTUnwrap(parse(notes(due: "2028-02-29 09:50（北京时间）"),
            start: date("2028-02-29T01:50:00Z")))
        XCTAssertEqual(validLeap.due, date("2028-02-29T01:50:00Z"))
    }

    func testUpdateTimestampMustIncludeValidDateTimeAndTimeZone() throws {
        for updated in ["", "not-a-date", "2026-10-18", "2026-10-18T08:00:00",
                        "2026-02-30T08:00:00+08:00", "2026-10-18T24:00:00Z",
                        "2026-10-18T08:00:00+08:61", "2026-10-18T08:00:00+99:00"] {
            XCTAssertThrowsError(try parse(notes(updated: updated)), updated)
        }
        for updated in ["2026-10-18T00:00:00Z", "2026-10-18T08:00:00.123456+08:00",
                        "2026-10-17T17:00:00-07:00"] {
            XCTAssertNotNil(try parse(notes(updated: updated)), updated)
        }
    }

    func testUnknownAndEmptyKindsThrow() throws {
        for kind in ["", "homework", "completed", "EXAM"] {
            XCTAssertThrowsError(try parse(notes(kind: kind)))
        }
        XCTAssertEqual(try parse(notes(kind: "exam"))?.kind, "exam")
    }

    func testEventStartMustBeStrictlyLessThanOneMinuteFromMetadata() throws {
        let due = date("2026-10-21T01:50:00Z")
        XCTAssertNotNil(try parse(notes(), start: due.addingTimeInterval(59.999)))
        XCTAssertNotNil(try parse(notes(), start: due.addingTimeInterval(-59.999)))
        for offset in [60.0, -60.0, 3600.0] {
            XCTAssertThrowsError(try parse(notes(), start: due.addingTimeInterval(offset))) { error in
                XCTAssertEqual(error as? AlarmCoreError, .eventDateMismatch)
            }
        }
    }

    func testEmptyTitleAfterRemovingCoursePrefixThrows() throws {
        XCTAssertThrowsError(try parse(notes(), title: "概率论 ·  "))
        XCTAssertThrowsError(try parse(notes(), title: " \n"))
    }

    func testCRLFMetadataAndFutureExamStartDoNotChangeDeadline() throws {
        let text = notes(kind: "exam").replacingOccurrences(of: "\n", with: "\r\n")
            + "\r\n考试开始：2026-10-20 14:00（北京时间）"
        XCTAssertEqual(try parse(text)?.due, date("2026-10-21T01:50:00Z"))
    }

    func testModelsRoundTripThroughJSONWithoutLosingAbsoluteDates() throws {
        let original = try XCTUnwrap(parse(notes()))
        let encoder = JSONEncoder()
        let decoded = try JSONDecoder().decode(AlarmDeadline.self, from: encoder.encode(original))
        XCTAssertEqual(decoded, original)
        let plan = try XCTUnwrap(DeadlinePlanner.plan(records: [original],
            now: date("2026-10-18T00:00:00Z")).first)
        XCTAssertEqual(try JSONDecoder().decode(DeadlineAlarmPlan.self,
            from: encoder.encode(plan)), plan)
    }
}
