import SwiftUI
import KeqiAlarmCore

@MainActor
struct ContentView: View {
    @ObservedObject var coordinator: AlarmCoordinator

    private func display(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "MM月dd日 HH:mm"
        return formatter.string(from: date)
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label("截止前 2 小时响铃", systemImage: "alarm.fill")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.red)
                    Text(coordinator.status)
                        .font(.callout)
                        .accessibilityIdentifier("alarmStatus")
                    if coordinator.isWorking { ProgressView("正在核对日历和闹钟…") }
                    if let date = coordinator.lastSyncedAt {
                        Text("上次核对：\(display(date))（北京时间）")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                } header: { Text("课期 · iPhone 闹钟") }

                Section {
                    if coordinator.calendarChoices.count > 1 {
                        Picker("选择作业与考试日历", selection: $coordinator.selectedCalendarIdentifier) {
                            Text("请选择").tag("")
                            ForEach(coordinator.calendarChoices) { calendar in
                                Text(calendar.title).tag(calendar.id)
                            }
                        }
                    }
                    if coordinator.isEnabled {
                        Button("立即核对并更新闹钟") {
                            Task { await coordinator.syncIfEnabled() }
                        }
                    } else {
                        Button("授权并开启 2 小时闹钟") {
                            Task { await coordinator.enableAndSync() }
                        }
                    }
                    Button("关闭并取消课期闹钟", role: .destructive) {
                        Task { await coordinator.disableAndCancel() }
                    }
                    Button("设一个 1 分钟测试闹钟") {
                        Task { await coordinator.testAlarm() }
                    }
                } footer: {
                    Text("首次开启需要完整日历访问和闹钟权限。测试闹钟只响一次，不会把作业标记为完成。")
                }
                .disabled(coordinator.isWorking)

                Section("接下来排定的闹钟") {
                    if coordinator.items.isEmpty {
                        Text("尚无排定的任务闹钟。核对成功后的空列表只表示未来 14 天内没有符合条件的响铃时间。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    ForEach(coordinator.items) { item in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(item.title).font(.headline)
                            Text(item.course + (item.kind == "exam" ? " · 考试" : " · 作业"))
                                .font(.caption).foregroundStyle(.secondary)
                            Text("响铃 \(display(item.fireDate))")
                                .font(.subheadline.weight(.semibold)).foregroundStyle(.red)
                            Text("截止 \(display(item.due)) · 北京时间")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding(.vertical, 3)
                    }
                }

                Section("提交后怎样取消") {
                    Text("Mac 确认提交后，会移除 iCloud 日历中的任务。iPhone 收到更新后，打开此 App 点击核对，可取消对应闹钟。")
                    Text("App 在运行时监听日历变化，并申请系统后台刷新。iOS 不保证后台执行时刻；提交后如临近响铃，请主动打开 App 核对。")
                    Text("已排定的闹钟可由系统响铃；它属于课期 App 管理的系统闹钟，不是写入“时钟”App 的闹钟。原有 48／24／2 小时日历通知继续保留。")
                }.font(.footnote)
            }
            .navigationTitle("课期闹钟")
        }
    }
}
