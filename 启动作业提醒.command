#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ -x .venv/bin/python ]]; then
  runtime="${PWD}/.venv/bin/python"
else
  runtime="$(command -v python3)"
fi

app_url="http://127.0.0.1:8765"
service_plist="${HOME}/Library/LaunchAgents/local.studydeadline.server.plist"

# Return only an application identity, never the API's private pairing URL.
server_state() {
  "$runtime" - <<'PY'
import errno
import json
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

try:
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8765/api/state", timeout=2) as response:
        if response.status != 200 or not response.headers.get("Server", "").startswith("StudyDeadline/"):
            print("occupied")
        else:
            state = json.load(response)
            print("running" if state.get("timezone") == "Asia/Shanghai" and isinstance(state.get("tasks"), list)
                  and isinstance(state.get("settings"), dict) else "occupied")
except URLError as exc:
    print("stopped" if isinstance(exc.reason, OSError) and exc.reason.errno == errno.ECONNREFUSED else "occupied")
except (OSError, ValueError, AttributeError):
    print("occupied")
PY
}

current_state="$(server_state)"
if [[ "$current_state" == running ]]; then
  /usr/bin/open "$app_url"
  exit 0
elif [[ "$current_state" != stopped ]]; then
  print -u2 "8765 端口已有服务或尚未就绪，无法确认是课期；未启动第二份服务。"
  exit 1
fi

if [[ ! -x build/CalendarBridge.app/Contents/MacOS/CalendarBridge ]]; then
  /bin/zsh scripts/build_calendar_bridge.sh
fi

if [[ -e "$service_plist" || -L "$service_plist" ]]; then
  # The manager checks project ownership before replacing/stopping any job.
  "$runtime" scripts/manage_background_service.py --install
  for attempt in {1..20}; do
    current_state="$(server_state)"
    if [[ "$current_state" == running ]]; then
      /usr/bin/open "$app_url"
      exit 0
    fi
    /bin/sleep 1
  done
  print -u2 "后台服务已请求恢复，但暂未通过健康检查。请查看 ~/Library/Logs/StudyDeadline/。"
  exit 1
fi

print "尚未安装登录自启；本次为前台运行，请保留终端窗口。"
print "需要后台运行时，请按 README 执行 manage_background_service.py --install。"
print "界面：${app_url}"
exec "$runtime" server.py --lan
