#!/usr/bin/env python3
"""Build a source-only release without opening the application's runtime data.

Run with --check to review the exact file list without creating an archive.
The allowlist is intentionally independent of .gitignore and Git state.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import stat
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_ROOT = "keqi"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
ROOT_FILES = frozenset({
    ".gitignore", "README.md", "THIRD_PARTY_NOTICES.md", "LICENSE",
    "requirements.txt", "server.py", "启动作业提醒.command",
})
SCRIPT_FILES = frozenset({
    "package_release.py", "manage_background_service.py", "verify_chaoxing.py",
    "verify_xiji.py", "安装采集依赖.command", "build_calendar_bridge.sh",
})
TREE_SUFFIXES = {
    "studyreminder": frozenset({".py"}),
    "tests": frozenset({".py", ".js"}),
    "web": frozenset({".html", ".css", ".js", ".svg", ".webmanifest"}),
    "widgets": frozenset({".js"}),
    "native": frozenset({".swift"}),
    "docs": frozenset({".md"}),
}
FORBIDDEN_PARTS = frozenset({
    "data", "build", "dist", "node_modules", "__pycache__", "logs", "log",
    "backup", "backups", "exports", "browser-profile", "browser-session",
})
PRIVATE_NAME = re.compile(
    r"(?:^|[_.-])(?:cookies?|sessions?|credentials?|secrets?|tokens?)(?:[_.-]|$)",
    re.IGNORECASE,
)
SAFE_NAMED_SOURCES = frozenset({"tests/test_collector_session.py"})
# Compose these prefixes so the scanner's own source contains no example home path.
PERSONAL_PATH = re.compile(
    r"(?:" + re.escape("/" + "Users/") + r"|" + re.escape("/" + "home/")
    + r")[\w.\-]+/|[A-Za-z]:[\\/]Users[\\/][\w.\-]+[\\/]",
    re.UNICODE,
)
TOKEN_PATTERNS = (
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("GitHub token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,255}\b")),
    ("API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,255}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{24,255}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{20,}\b")


class ReleaseError(RuntimeError):
    """A safe error message which never includes a secret value or source excerpt."""


@dataclass(frozen=True)
class SourceFile:
    content: bytes
    mode: int = 0o644


def _safe_parts(relative: PurePosixPath) -> bool:
    return all(
        part not in {"", ".", ".."}
        and not part.startswith(".")
        and part.lower() not in FORBIDDEN_PARTS
        for part in relative.parts
    )


def allowed_source(relative: PurePosixPath) -> bool:
    """Keep an explicit source allowlist; no runtime file type is allowed."""
    parts = relative.parts
    if relative.is_absolute() or not parts:
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if not _safe_parts(relative) or (PRIVATE_NAME.search(relative.name) and str(relative) not in SAFE_NAMED_SOURCES):
        return False
    tree = parts[0]
    if tree == "scripts":
        return len(parts) == 2 and relative.name in SCRIPT_FILES
    if tree == "native" and relative.name == "Info.plist":
        return len(parts) == 2
    if tree == "widgets" and relative.name == "README.md":
        return len(parts) == 2
    suffixes = TREE_SUFFIXES.get(tree, ())
    # Only web assets have a nested tree (web/widgets). Python and docs stay flat.
    return relative.suffix in suffixes and (tree == "web" or len(parts) == 2)


def _looks_real(token: str) -> bool:
    """Ignore obvious documentation placeholders and low-entropy test fixtures.

    No test directory is exempt from checking. A plausible secret in a test file
    blocks release just as it would in an application file.
    """
    lowered = token.lower()
    placeholders = ("example", "placeholder", "changeme", "yourtoken", "your_token", "dummy", "fake", "testing")
    if any(marker in lowered for marker in placeholders):
        return False
    # Prefixes are predictable; measure the value rather than its product name.
    value = re.sub(r"^(?:github_pat_|gh[pousr]_|sk-(?:proj-|svcacct-)?|xox[baprs]-|AKIA|ASIA)", "", token)
    if len(set(value)) < 10:
        return False
    counts = Counter(value)
    entropy = -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())
    return entropy >= 3.0


def audit_content(relative: str, payload: bytes) -> None:
    if len(payload) > MAX_FILE_BYTES:
        raise ReleaseError(f"文件超过源码体积限制：{relative}")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ReleaseError(f"源码不是 UTF-8 文本：{relative}") from None
    if "\x00" in text:
        raise ReleaseError(f"源码包含二进制内容：{relative}")
    for line_number, line in enumerate(text.splitlines(), 1):
        if PERSONAL_PATH.search(line):
            raise ReleaseError(f"包含个人绝对路径：{relative}:{line_number}")
        for label, pattern in TOKEN_PATTERNS:
            if any(_looks_real(match.group()) for match in pattern.finditer(line)):
                raise ReleaseError(f"发现疑似 {label}，请在本地检查：{relative}:{line_number}")
        for match in JWT_PATTERN.finditer(line):
            header = match.group().split(".", 1)[0]
            try:
                decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(decoded, dict) and "alg" in decoded and _looks_real(match.group()):
                raise ReleaseError(f"发现疑似会话令牌，请在本地检查：{relative}:{line_number}")


def _read_source(path: Path, relative: str) -> SourceFile:
    # Reject links before opening and again at open time. Never resolve a source
    # link into a runtime folder or another user's directory.
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ReleaseError(f"源码必须是普通文件且不能是链接：{relative}")
        if info.st_size > MAX_FILE_BYTES:
            raise ReleaseError(f"文件超过源码体积限制：{relative}")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ReleaseError(f"源码必须是普通文件且不能是链接：{relative}")
            payload = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ReleaseError(f"不能安全读取源码：{relative}") from None
    audit_content(relative, payload)
    return SourceFile(payload, 0o755 if opened.st_mode & 0o111 else 0o644)


def collect_sources(root: Path) -> dict[str, SourceFile]:
    """Inspect only known source roots; private directories are never traversed."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError("源码根目录必须是实际目录。")
    files: dict[str, SourceFile] = {}

    def add(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        if allowed_source(PurePosixPath(relative)):
            files[relative] = _read_source(path, relative)

    for name in sorted(ROOT_FILES):
        path = root / name
        if path.exists() or path.is_symlink():
            add(path)

    def scan(directory: Path, *, recursive: bool = False) -> None:
        relative = directory.relative_to(root).as_posix()
        if directory.is_symlink():
            raise ReleaseError(f"源码目录不能是符号链接：{relative}")
        if not directory.exists():
            return
        if not directory.is_dir():
            raise ReleaseError(f"源码目录不是实际目录：{relative}")
        for path in sorted(directory.iterdir()):
            child = PurePosixPath(path.relative_to(root).as_posix())
            if not _safe_parts(child):
                continue
            if path.is_symlink():
                if allowed_source(child) or recursive:
                    raise ReleaseError(f"源码不能包含符号链接：{child.as_posix()}")
                continue
            if path.is_dir():
                if recursive:
                    scan(path, recursive=True)
            else:
                add(path)

    for name in sorted(set(TREE_SUFFIXES) | {"scripts"}):
        scan(root / name, recursive=name == "web")
    if not files:
        raise ReleaseError("未找到可发布的源码文件。")
    if sum(len(source.content) for source in files.values()) > MAX_TOTAL_BYTES:
        raise ReleaseError("源码总大小超过发布限制。")
    return dict(sorted(files.items()))


def audit_archive(path: Path, sources: dict[str, SourceFile]) -> None:
    """Read back the actual zip and compare every byte with its audited source."""
    expected = {f"{ARCHIVE_ROOT}/{name}": source for name, source in sources.items()}
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != len(expected) or {member.filename for member in members} != set(expected):
                raise ReleaseError("压缩包清单与允许的源码清单不一致。")
            for member in members:
                relative = PurePosixPath(member.filename)
                if relative.parts[0] != ARCHIVE_ROOT or not allowed_source(PurePosixPath(*relative.parts[1:])):
                    raise ReleaseError("压缩包包含不允许的路径。")
                source = expected[member.filename]
                if stat.S_ISLNK(member.external_attr >> 16) or member.file_size != len(source.content):
                    raise ReleaseError("压缩包文件类型或大小不符合源码清单。")
                payload = archive.read(member)
                if payload != source.content:
                    raise ReleaseError("压缩包内容与已审核的源码不一致。")
                audit_content("/".join(relative.parts[1:]), payload)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError("无法完成压缩包回读检查。") from None


def build_archive(root: Path, output: Path, sources: dict[str, SourceFile] | None = None) -> int:
    sources = collect_sources(root) if sources is None else sources
    output = Path(output)
    if output.suffix.lower() != ".zip":
        raise ReleaseError("输出文件必须使用 .zip 后缀。")
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ReleaseError("输出位置不能包含符号链接。")
    if any(part.lower() in FORBIDDEN_PARTS - {"dist"} or part.startswith(".") for part in output.parts if part not in {"/", "."}):
        raise ReleaseError("输出位置不能是私有数据、隐藏或缓存目录。")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix="keqi-release-", suffix=".zip", dir=output.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, source in sources.items():
                audit_content(relative, source.content)
                if not allowed_source(PurePosixPath(relative)):
                    raise ReleaseError("源码清单包含未允许的路径。")
                member = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = (stat.S_IFREG | source.mode) << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, source.content)
        audit_archive(temporary, sources)
        os.replace(temporary, output)
        return len(sources)
    except OSError:
        raise ReleaseError("不能创建源码压缩包，请检查输出目录权限。") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审核并打包课期源码；不会读取账号、作业库或浏览器会话。")
    parser.add_argument("--check", action="store_true", help="只审核并列出源码，不生成压缩包")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist" / "keqi-source.zip", help="输出 .zip 路径")
    args = parser.parse_args(argv)
    try:
        sources = collect_sources(PROJECT_ROOT)
        for name in sources:
            print(name)
        if args.check:
            print(f"审核通过：{len(sources)} 个源码文件；未生成压缩包。")
        else:
            count = build_archive(PROJECT_ROOT, args.output, sources)
            print(f"审核并回读通过：{count} 个源码文件。输出：{args.output}")
        return 0
    except ReleaseError as exc:
        print(f"打包停止：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
