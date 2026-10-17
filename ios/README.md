# 课期 · iPhone 原生闹钟工程

这是 **iOS 26 或更新版本**的配套 App 源码。使用 AlarmKit 系统闹钟，在课期任务截止前两小时响铃。它读取本机已同步的 iCloud“作业与考试”日历，不接收平台密码，不改写日历，也不把手机数据发回 Mac。

**目前是待真机验收的工程，尚无可直接安装的 IPA 或 TestFlight。**当前 Mac 没有完整 Xcode，但已在 GitHub 的 Xcode 26.3 环境中使用 iPhone SDK 完成未签名编译，24 个 XCTest 和 43 项 Foundation 核心检查通过。尚未进行本人签名、手机安装或实际响铃测试。

## 安装前准备

1. 在 iPhone 的“日历”确认能看到 Mac 已同步的“作业与考试”记录。
2. 安装与 Mac 系统兼容、包含 iOS 26 或更新 SDK 的完整 Xcode。请按 Apple 的[系统兼容表](https://developer.apple.com/xcode/system-requirements)选择，旧版从[Apple Developer 下载页](https://developer.apple.com/download/all/)获取。例如 macOS 15.6 可使用 Xcode 26.3；App Store 的当前最新版本可能要求更新的 macOS。
3. 给 Xcode、平台 SDK、解压和构建留出空间。当前开发机仅约 10 GiB 可用，尚未开始下载。首次打开 Xcode，按其界面完成组件安装与许可，由本人登录 Apple Account。
4. 用数据线连接 iPhone，在手机上本人确认信任电脑和开发者模式。

Xcode Personal Team 可供本人测试安装，但免费签名存在时效：Apple 当前规定 provisioning profile 7 天到期，需要重新构建安装。长期使用必须维持有效签名；本源码不包含开发证书或他人的签名。[Apple 个人团队说明](https://developer.apple.com/help/account/basics/about-your-developer-account)

## 在 Xcode 安装

1. 打开 `ios/KeqiAlarm.xcodeproj`，选择 **KeqiAlarm** target。
2. 在 **Signing & Capabilities** 中选择自己的 Team，保留自动签名。如果默认 Bundle Identifier 被占用，改成自己的唯一标识；不要把 Apple ID、证书或配置文件提交到 GitHub。
3. 运行设备选择连接的 iPhone，点击 Run。云端未签名构建已通过，但这一步的本人签名与手机部署仍需在本机完成。
4. 手机打开“课期闹钟”，点击 **授权并开启 2 小时闹钟**，本人允许完整日历访问和闹钟权限。存在多个同名 iCloud 日历时，先明确选择一个再同步。
5. 先点击 **设一个 1 分钟测试闹钟**，锁屏确认实际响铃和停止行为。测试只由本人点击创建，不会自动开始，也不会把作业标为完成。
6. 核对清单里每个任务的响铃时刻确为 DDL−2 小时，再用一条实际任务验证更新与取消。

这是课期 App 管理的系统闹钟，不是向“时钟”App 的个人闹钟列表写入。原有 48／24／2 小时日历通知保留；小组件脚本本身不创建闹钟。[Apple AlarmKit](https://developer.apple.com/documentation/alarmkit/scheduling-an-alarm-with-alarmkit)

## 自动同步与取消的实际范围

打开 App、App 运行时收到日历变化、系统准许后台刷新时，会重新核对。Mac 仍负责平台提交状态：**Mac 确认提交 → iCloud 日历移除任务 → iPhone App 重新读取 → 取消对应闹钟**。

iOS 不保证后台执行的具体时刻。提交后如果已经临近响铃，请打开 App 点击“立即核对并更新闹钟”；不能承诺只在 Mac 交完作业后，手机即刻取消。[Apple 后台刷新时间说明](https://developer.apple.com/documentation/backgroundtasks/bgtaskrequest/earliestbegindate)

闹钟按绝对日期安排，跨日也以 DDL−2 小时为准。每次最多排定未来 14 天内最近的 30 个，之后需继续同步。已经错过两小时前提醒点的任务不追补闹钟，应直接查看待办。日历权限、指定日历或数据格式异常时停止本次更新，保留已排定记录并显示错误。

新闹钟成功后再移除旧配置；每项写本机 journal，重复同步核对系统实际记录，避免重复创建。关闭按钮取消本 App 记录的闹钟，不触碰私人闹钟。取消失败会提示，可再次点击关闭重试。

## 已运行的检查

```sh
cd ios/AlarmCore
swift run --scratch-path .build keqi-core-check
```

Foundation 验证器实际编译运行，43 项检查通过，覆盖专用日历标识、严格日期、跨日、时区、两小时边界和冲突拒绝。本机 CLT 不提供 XCTest；GitHub 的完整 Xcode 环境已执行 24 个 XCTest，全部通过。

安装完整 Xcode 后还需要执行：

```sh
# 仅编译，不签名、不安装；使用真机 SDK，避免为这项检查下载模拟器运行时
xcodebuild -project ios/KeqiAlarm.xcodeproj -scheme KeqiAlarm \
  -sdk iphoneos -destination 'generic/platform=iOS' \
  -derivedDataPath build/iphone-derived CODE_SIGNING_ALLOWED=NO build
```

云端已执行同样的未签名构建。编译成功后仍须按上面的步骤完成本人签名与真机验收。[构建记录](https://github.com/yxqyyyyy/keqi/actions/workflows/verify-iphone.yml)与测试记录分别说明源码检查、SDK 编译和真机结果。
