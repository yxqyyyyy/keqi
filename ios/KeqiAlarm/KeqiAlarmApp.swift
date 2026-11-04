import SwiftUI
import BackgroundTasks

@main
@MainActor
struct KeqiAlarmApp: App {
    @StateObject private var coordinator = AlarmCoordinator.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            ContentView(coordinator: coordinator)
                .task {
                    await coordinator.syncIfEnabled()
                    coordinator.scheduleBackgroundRefresh()
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        Task {
                            await coordinator.syncIfEnabled()
                            coordinator.scheduleBackgroundRefresh()
                        }
                    }
                }
        }
        .backgroundTask(.appRefresh("local.studydeadline.KeqiAlarm.refresh")) {
            await AlarmCoordinator.shared.syncIfEnabled()
            await AlarmCoordinator.shared.scheduleBackgroundRefresh()
        }
    }
}
