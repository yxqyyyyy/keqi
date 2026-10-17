#!/bin/zsh
set -eu
cd "${0:A:h:h}"
if [[ "$(uname -s)" != Darwin ]]; then
  print -u2 '当前版本需要 macOS：日历提醒和后台服务使用 Mac 系统组件。'
  exit 1
fi
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  print -u2 '请先安装 Python 3.10 或更新版本，再重新运行此脚本。'
  exit 1
fi
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -d '/Applications/Google Chrome.app' ]]; then
  .venv/bin/python -m playwright install chromium
fi
print '采集依赖已安装。关闭正在运行的程序，然后双击“启动作业提醒.command”。'
