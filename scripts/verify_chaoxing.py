"""Read-only verification using the already authorized local browser session.

No GUI, credential inspection, browser navigation, form submission or JavaScript
execution. Playwright loads the existing session directly into a request context.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from studyreminder.chaoxing_adapter import collect_chaoxing


def main():
    state = ROOT / "data" / "browser-session.json"
    target = ROOT / "data" / "chaoxing-live-result.json"
    if not state.is_file():
        print("正常浏览器会话文件尚不存在，请先本人登录并保存会话。")
        return 1
    with sync_playwright() as playwright:
        request = playwright.request.new_context(storage_state=str(state))
        try:
            result = collect_chaoxing(SimpleNamespace(request=request, pages=[]))
        finally:
            request.dispose()
    # Omit source links, which can contain per-student class identifiers. The
    # adapter already removes signatures and query values from diagnostics.
    fields = ("title", "course", "kind", "due_at", "start_at", "status", "source_key", "identity_kind", "notes")
    safe_result = {**result, "candidates": [{key: item[key] for key in fields if key in item} for item in result["candidates"]]}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(safe_result, output, ensure_ascii=False, indent=2)
    target.chmod(0o600)
    print("学习通登录状态：" + result["auth"]["message"])
    for item in safe_result["candidates"]:
        print(item["course"] + " | " + item["title"] + " | 开始 " + (item.get("start_at") or "未知") + " | 截止 " + item["due_at"])
    for warning in result["warnings"]:
        print("提示：" + warning)
    return 0 if result["auth"].get("authenticated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
