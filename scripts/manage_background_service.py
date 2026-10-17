#!/usr/bin/env python3
"""Review, install, or remove this project's per-user macOS LaunchAgent.

No arguments and --dry-run never install a service. This program has no option
to supply a different project, executable, launchd label, or plist destination.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.parsers.expat import ExpatError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
USER_HOME = Path.home()
LABEL = "local.studydeadline.server"
LAUNCHCTL = "/bin/launchctl"
LAUNCHCTL_TIMEOUT = 15


class ServiceError(RuntimeError):
    pass


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def configuration() -> dict:
    logs = USER_HOME / "Library" / "Logs" / "StudyDeadline"
    config = {
        "Label": LABEL,
        "ProgramArguments": [
            str((PROJECT_ROOT / ".venv" / "bin" / "python").resolve()),
            str(PROJECT_ROOT / "server.py"),
            "--lan",
        ],
        "WorkingDirectory": str(USER_HOME),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "stdout.log"),
        "StandardErrorPath": str(logs / "stderr.log"),
        "Umask": 0o077,
    }
    vendor = sorted((PROJECT_ROOT / ".venv" / "lib").glob("python*/site-packages"))
    if vendor:
        config["EnvironmentVariables"] = {"PYTHONPATH": str(vendor[0])}
    return config


def serialized_configuration() -> bytes:
    payload = plistlib.dumps(configuration(), fmt=plistlib.FMT_XML, sort_keys=True)
    if plistlib.loads(payload) != configuration():
        raise ServiceError("后台服务配置校验失败。")
    return payload


def lint_configuration(payload: bytes) -> str:
    plistlib.loads(payload)
    if sys.platform != "darwin":
        return "plistlib: OK（当前不是 macOS，未运行 plutil）"
    result = subprocess.run(
        ["/usr/bin/plutil", "-lint", "-"], input=payload,
        capture_output=True, check=False,
    )
    if result.returncode:
        raise ServiceError(f"plutil 校验失败（退出码 {result.returncode}）。")
    return "plistlib: OK; plutil: OK"


def existing_owned_configuration() -> bytes | None:
    """Never replace or remove an unrelated job, even with the same label."""
    path = plist_path()
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ServiceError("同名 LaunchAgent 配置不是普通文件，拒绝接管。")
    try:
        payload = path.read_bytes()
        current = plistlib.loads(payload)
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as exc:
        raise ServiceError("同名 LaunchAgent 配置无法验证，拒绝覆盖或删除。") from exc
    expected = configuration()
    args = current.get("ProgramArguments") if isinstance(current, dict) else None
    legacy = [str(PROJECT_ROOT / ".venv/bin/python"), str(PROJECT_ROOT / "server.py"), "--lan"]
    allowed_args = (expected["ProgramArguments"], expected["ProgramArguments"][:2], legacy, legacy[:2])
    if (not isinstance(current, dict) or current.get("Label") != LABEL
            or args not in allowed_args
            or current.get("WorkingDirectory") not in (None, str(PROJECT_ROOT), str(USER_HOME))
            or "Program" in current):
        raise ServiceError("同名 LaunchAgent 不属于本项目，拒绝覆盖、停止或删除。")
    return payload


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [LAUNCHCTL, *arguments], capture_output=True, text=True, check=False,
            timeout=LAUNCHCTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        # The launchd operation may still finish after its client times out.
        # Do not print its captured output or automatically repeat a mutation.
        raise ServiceError(f"launchctl {arguments[0]} 等待超过 {LAUNCHCTL_TIMEOUT} 秒，请查询后台服务状态后重试。") from exc


def _require_success(result: subprocess.CompletedProcess, action: str) -> None:
    if result.returncode:
        # launchctl print can contain environment variables. Never echo its raw
        # output (or any child-process stderr) into status/errors.
        raise ServiceError(f"launchctl {action} 失败（退出码 {result.returncode}）。")


def _missing_service(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == 113 or "could not find service" in (result.stderr or "").lower()


def parse_status(output: str) -> dict:
    state_match = re.search(r"(?m)^\s*state = ([a-zA-Z ]+)\s*$", output)
    pid_match = re.search(r"(?m)^\s*pid = ([0-9]+)\s*$", output)
    state = state_match.group(1).strip() if state_match else "unknown"
    if state not in {"running", "not running", "waiting", "spawn scheduled", "exited"}:
        state = "unknown"
    pid = int(pid_match.group(1)) if pid_match else None
    return {"loaded": True, "running": state == "running" and pid is not None,
            "state": state, "pid": pid}


def read_status() -> dict:
    result = _launchctl("print", service_target())
    if result.returncode:
        if _missing_service(result):
            return {"loaded": False, "running": False, "state": "not loaded", "pid": None}
        _require_success(result, "print")
    return parse_status(result.stdout)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ServiceError("后台服务安装、卸载和状态查询仅支持 macOS；可使用 --dry-run 检查配置。")


def _validate_runtime() -> None:
    args = configuration()["ProgramArguments"]
    if not Path(args[0]).is_file() or not os.access(args[0], os.X_OK):
        raise ServiceError("本项目的 .venv/bin/python 不存在或不可执行。")
    if not Path(args[1]).is_file():
        raise ServiceError("本项目的 server.py 不存在。")


def _prepare_private_data() -> None:
    data = PROJECT_ROOT / "data"
    if data.is_symlink():
        raise ServiceError("data 目录是符号链接，拒绝修改其权限。")
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    data.chmod(0o700)
    logs = Path(configuration()["StandardOutPath"]).parent
    if logs.is_symlink():
        raise ServiceError("服务日志目录是符号链接，拒绝写入。")
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    logs.chmod(0o700)
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = Path(configuration()[key])
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ServiceError("服务日志路径不是普通文件，拒绝写入。")
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def _write_configuration(payload: bytes) -> None:
    path = plist_path()
    if path.parent.is_symlink():
        raise ServiceError("LaunchAgents 目录是符号链接，拒绝写入。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{LABEL}.", suffix=".plist", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        existing_owned_configuration()
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install() -> dict:
    _require_macos()
    _validate_runtime()
    previous = existing_owned_configuration()
    payload = serialized_configuration()
    lint_configuration(payload)
    status = read_status()
    if status["loaded"] and previous is None:
        raise ServiceError("同名服务已加载但没有本项目配置，拒绝接管。")
    _prepare_private_data()
    if status["loaded"]:
        _require_success(_launchctl("bootout", service_target()), "bootout")
    _write_configuration(payload)
    _require_success(_launchctl("enable", service_target()), "enable")
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path()))
    if result.returncode == 5 and previous is not None and status["loaded"]:
        # A removed job can briefly remain in teardown. Retry only this
        # verified project's reload, never a fresh/foreign/loaded service.
        for delay in (0.5, 1, 2):
            time.sleep(delay)
            if existing_owned_configuration() is None:
                raise ServiceError("后台服务配置已消失，无法验证归属，停止重试。")
            if read_status()["loaded"]:
                break
            result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path()))
            if result.returncode != 5:
                break
    _require_success(result, "bootstrap")
    # RunAtLoad/KeepAlive already start the job. An immediate kickstart -k can
    # kill that new process and trigger another throttled launch instead.
    return read_status()


def uninstall() -> bool:
    _require_macos()
    if existing_owned_configuration() is None:
        return False
    result = _launchctl("bootout", service_target())
    if result.returncode and not _missing_service(result):
        _require_success(result, "bootout")
    # Revalidate immediately before deleting. Application data and logs remain.
    existing_owned_configuration()
    plist_path().unlink(missing_ok=True)
    return True


def print_dry_run(action: str = "install") -> None:
    if action in {"install", "uninstall"}:
        existing_owned_configuration()
    payload = serialized_configuration()
    lint = lint_configuration(payload)
    print("DRY RUN：未安装、停止或更改任何后台服务。")
    print(f"操作: {action}\nLabel: {LABEL}\n目标: {service_target()}\nPlist: {plist_path()}")
    print(f"数据目录: {PROJECT_ROOT / 'data'}（权限 0700）；日志: {configuration()['StandardOutPath']}（0600）")
    print(f"Plist lint: {lint}")
    print(payload.decode("utf-8"), end="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本项目的 macOS 登录自启后台服务；默认仅显示说明，不安装。")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--install", dest="action", action="store_const", const="install", help="安装并启动本项目的用户后台服务")
    actions.add_argument("--uninstall", dest="action", action="store_const", const="uninstall", help="停止并删除本项目的后台服务配置，保留数据")
    actions.add_argument("--status", dest="action", action="store_const", const="status", help="只读查看服务是否运行及 PID")
    parser.add_argument("--dry-run", action="store_true", help="仅检查和展示非敏感配置，不更改服务")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            print_dry_run(args.action or "install")
        elif args.action == "install":
            status = install()
            print("已安装本项目的登录自启后台服务。")
            print(json.dumps({"label": LABEL, **status}, ensure_ascii=False))
        elif args.action == "uninstall":
            print("已停止并移除本项目的后台服务；保留全部数据。" if uninstall() else "未找到本项目的后台服务配置，未更改任何服务。")
        elif args.action == "status":
            _require_macos()
            print(json.dumps({"label": LABEL, **read_status()}, ensure_ascii=False))
        else:
            parser.print_help()
    except (ServiceError, OSError, ValueError) as exc:
        # Do not print arbitrary subprocess output or existing plist contents.
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
