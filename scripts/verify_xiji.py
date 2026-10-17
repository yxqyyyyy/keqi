"""Read-only Xiji integration probe using the application's opaque session."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from studyreminder.xiji_adapter import collect_xiji


def main():
    session = ROOT / "data" / "browser-session.json"
    destination = ROOT / "data" / "xiji-live-diagnostics.json"
    if not session.is_file():
        raise SystemExit("应用未保存可用会话，请先本人登录")
    with sync_playwright() as playwright:
        request = playwright.request.new_context(storage_state=str(session))
        try:
            result = collect_xiji(SimpleNamespace(request=request, pages=[]))
        finally:
            request.dispose()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps({"authenticated": result["auth"]["authenticated"], "candidates": len(result["candidates"]),
                      "dates": [item["due_at"] for item in result["candidates"]], "warnings": result["warnings"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
