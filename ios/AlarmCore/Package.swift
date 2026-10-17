// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "KeqiAlarmCore",
    products: [
        .library(name: "KeqiAlarmCore", targets: ["KeqiAlarmCore"]),
        .executable(name: "keqi-core-check", targets: ["AlarmCoreChecks"])
    ],
    targets: [
        .target(name: "KeqiAlarmCore"),
        .testTarget(name: "KeqiAlarmCoreTests", dependencies: ["KeqiAlarmCore"]),
        .executableTarget(name: "AlarmCoreChecks", dependencies: ["KeqiAlarmCore"])
    ]
)
