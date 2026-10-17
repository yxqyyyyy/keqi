#!/bin/zsh
# Compile only. This script never starts the helper or requests calendar access.
set -euo pipefail

TASK_ROOT="${0:A:h:h}"
TASK_BUILD="$TASK_ROOT/build"
TASK_APP="$TASK_BUILD/CalendarBridge.app"
TASK_BUNDLE="$TASK_BUILD/.calendar-bridge-bundle"
TASK_CACHE="$TASK_BUILD/swift-module-cache"
TASK_STAGE="$(/usr/bin/mktemp -d /private/tmp/studydeadline-calendar-build.XXXXXX)"
TASK_STAGE_APP="$TASK_STAGE/CalendarBridge.app"
TASK_INSTALL=""

function cleanup {
  [[ -z "$TASK_INSTALL" ]] || /bin/rm -rf "$TASK_INSTALL"
  /bin/rm -rf "$TASK_STAGE"
}
trap cleanup EXIT

# Desktop can be managed by File Provider, which may attach FinderInfo while a
# bundle is being assembled. Build and sign on a local staging directory first.
mkdir -p "$TASK_STAGE_APP/Contents/MacOS" "$TASK_CACHE"
/bin/cp -X "$TASK_ROOT/native/Info.plist" "$TASK_STAGE_APP/Contents/Info.plist"
/usr/bin/plutil -lint "$TASK_STAGE_APP/Contents/Info.plist"

TASK_SWIFTC="$(/usr/bin/xcrun --find swiftc)"
TASK_SDK="$(/usr/bin/xcrun --show-sdk-path)"
TASK_ARCH="$(/usr/bin/uname -m)"
"$TASK_SWIFTC" \
  -parse-as-library \
  -O \
  -sdk "$TASK_SDK" \
  -target "$TASK_ARCH-apple-macosx12.0" \
  -module-cache-path "$TASK_CACHE" \
  -framework AppKit \
  -framework EventKit \
  "$TASK_ROOT/native/CalendarBridge.swift" \
  -o "$TASK_STAGE_APP/Contents/MacOS/CalendarBridge"

# A stable bundle identity is required for macOS privacy consent. Ad-hoc signing
# is sufficient for this personal, locally built helper; no developer login.
/usr/bin/codesign --force --sign - --identifier local.studydeadline.CalendarBridge "$TASK_STAGE_APP"
/usr/bin/codesign --verify --deep --strict "$TASK_STAGE_APP"

# Do not copy source/staging extended attributes into the installed bundle.
# Keep the physical bundle under a hidden directory without the .app suffix:
# Desktop File Provider repeatedly adds FinderInfo to physical .app directories,
# invalidating even a previously verified signature. The public .app path is a
# relative symlink; callers keep their existing executable path and bundle ID.
TASK_INSTALL="$(/usr/bin/mktemp -d "$TASK_BUILD/.calendar-bridge-install.XXXXXX")"
/usr/bin/ditto --norsrc --noextattr --noqtn "$TASK_STAGE_APP" "$TASK_INSTALL/new-bundle"
/usr/bin/codesign --verify --deep --strict "$TASK_INSTALL/new-bundle"
if [[ -e "$TASK_APP" || -L "$TASK_APP" ]]; then
  /bin/mv "$TASK_APP" "$TASK_INSTALL/previous.app"
fi
if [[ -e "$TASK_BUNDLE" || -L "$TASK_BUNDLE" ]]; then
  /bin/mv "$TASK_BUNDLE" "$TASK_INSTALL/previous-bundle"
fi
/bin/mv "$TASK_INSTALL/new-bundle" "$TASK_BUNDLE"
/bin/ln -s .calendar-bridge-bundle "$TASK_APP"
/usr/bin/codesign --verify --deep --strict "$TASK_BUNDLE"
/usr/bin/codesign --verify --deep --strict "$TASK_APP"
/usr/bin/plutil -lint "$TASK_APP/Contents/Info.plist"
print -r -- "Built: $TASK_APP"
print -r -- "未启动桥接程序；未请求日历授权；未写入日历事件。"
