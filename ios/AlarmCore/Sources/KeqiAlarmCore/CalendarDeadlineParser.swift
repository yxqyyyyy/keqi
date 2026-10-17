import Foundation

public enum CalendarDeadlineParser {
    /// Returns nil for events outside the app's strictly identified task namespace.
    /// A managed event with damaged metadata throws so callers cannot mistake it for completion.
    public static func parse(title: String, notes: String, url: URL?,
                             startDate: Date) throws -> AlarmDeadline? {
        guard let taskID = managedTaskID(url) else { return nil }

        let lines = notes.components(separatedBy: .newlines)
        let dueText = try field("截止时间", in: lines, required: true)!
        guard let due = parseBeijingDeadline(dueText) else {
            throw AlarmCoreError.invalidMetadata(field: "截止时间")
        }
        let kind = try field("类型", in: lines, required: true)!
        guard ["assignment", "exam"].contains(kind) else {
            throw AlarmCoreError.invalidMetadata(field: "类型")
        }
        let updated = try field("本地记录更新时间", in: lines, required: true)!
        guard validTimestamp(updated) else {
            throw AlarmCoreError.invalidMetadata(field: "本地记录更新时间")
        }
        guard startDate.timeIntervalSinceReferenceDate.isFinite,
              abs(startDate.timeIntervalSince(due)) < 60 else {
            throw AlarmCoreError.eventDateMismatch
        }

        let course = try field("课程", in: lines, required: false) ?? ""
        var taskTitle = String(title.drop(while: { $0.isWhitespace }))
        if !course.isEmpty, taskTitle.hasPrefix(course + " · ") {
            taskTitle = String(taskTitle.dropFirst(course.count + 3))
        }
        taskTitle = taskTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !taskTitle.isEmpty else { throw AlarmCoreError.invalidTitle }
        return AlarmDeadline(taskID: taskID, title: taskTitle, course: course,
                             kind: kind, due: due)
    }

    private static func managedTaskID(_ url: URL?) -> String? {
        guard let url,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == "studydeadline",
              components.host?.lowercased() == "task",
              components.user == nil, components.password == nil, components.port == nil,
              components.query == nil, components.fragment == nil else { return nil }
        let path = components.percentEncodedPath
        guard path.hasPrefix("/"),
              let id = String(path.dropFirst()).removingPercentEncoding,
              !id.isEmpty, id != ".", id != "..",
              !id.contains("/"), !id.contains("\\"),
              id.rangeOfCharacter(from: .whitespacesAndNewlines) == nil,
              id.rangeOfCharacter(from: .controlCharacters) == nil else { return nil }
        return id
    }

    private static func field(_ name: String, in lines: [String], required: Bool) throws -> String? {
        let prefix = name + "："
        let values = lines.filter { $0.hasPrefix(prefix) }
            .map { String($0.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces) }
        guard values.count < 2 else { throw AlarmCoreError.duplicateMetadata(field: name) }
        guard let value = values.first else {
            if required { throw AlarmCoreError.invalidMetadata(field: name) }
            return nil
        }
        guard !value.isEmpty else { throw AlarmCoreError.invalidMetadata(field: name) }
        return value
    }

    private static func captures(_ text: String, pattern: String) -> [String]? {
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              match.range.length == text.utf16.count else { return nil }
        return (1..<match.numberOfRanges).map { index in
            guard let range = Range(match.range(at: index), in: text) else { return "" }
            return String(text[range])
        }
    }

    private static func parseBeijingDeadline(_ value: String) -> Date? {
        guard let parts = captures(value,
            pattern: "^([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2})（北京时间）$") else {
            return nil
        }
        return strictDate(parts: parts + ["00"], offset: 8 * 3600)
    }

    private static func validTimestamp(_ value: String) -> Bool {
        guard let parts = captures(value,
            pattern: "^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$") else {
            return false
        }
        let zone = parts[6]
        var offset = 0
        if zone != "Z" {
            let digits = zone.dropFirst().split(separator: ":")
            guard digits.count == 2, let hours = Int(digits[0]), let minutes = Int(digits[1]),
                  hours <= 23, minutes < 60 else { return false }
            offset = (hours * 3600 + minutes * 60) * (zone.hasPrefix("-") ? -1 : 1)
        }
        return strictDate(parts: Array(parts.prefix(6)), offset: offset) != nil
    }

    private static func strictDate(parts: [String], offset: Int) -> Date? {
        let values = parts.compactMap(Int.init)
        guard values.count == 6, (1...9999).contains(values[0]),
              (1...12).contains(values[1]), (1...31).contains(values[2]),
              (0...23).contains(values[3]), (0...59).contains(values[4]),
              (0...59).contains(values[5]), let zone = TimeZone(secondsFromGMT: offset) else {
            return nil
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = Locale(identifier: "en_US_POSIX")
        calendar.timeZone = zone
        let components = DateComponents(year: values[0], month: values[1], day: values[2],
                                        hour: values[3], minute: values[4], second: values[5])
        guard let date = calendar.date(from: components) else { return nil }
        let result = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        guard [result.year, result.month, result.day, result.hour, result.minute, result.second]
            .compactMap({ $0 }) == values else { return nil }
        return date
    }
}
