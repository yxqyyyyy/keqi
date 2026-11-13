"""Optional private cloud mirror; school credentials never leave this Mac."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import now_iso
from .sync_status import platform_synced_at

PUBLIC_FIELDS = frozenset({
    "id", "title", "course", "source", "kind", "start_at", "due_at", "status",
    "url", "notes", "updated_at", "revision", "deleted",
})
MAX_RESPONSE_BYTES = 2_000_000


class CloudSyncError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A sign-in redirect must not receive the Mac's bridge credentials.
        return None


class CloudSync:
    def __init__(self, data_dir, store, transport=None):
        self.path = Path(data_dir) / "cloud-sync.json"
        self.store = store
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.transport = transport or self._post

    @staticmethod
    def validate_config(raw):
        if not isinstance(raw, dict):
            raise CloudSyncError("云同步配置格式不正确")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise CloudSyncError("云同步开关格式不正确")
        url = raw.get("url", "")
        if not isinstance(url, str):
            raise CloudSyncError("请填写手机云端网址")
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path not in {"", "/"}):
            raise CloudSyncError("云端地址须为 HTTPS 站点首页，不含凭证或其他路径")
        result = {"enabled": enabled, "url": url.rstrip("/")}
        for key in ("bridge_key", "sites_token"):
            value = raw.get(key, "")
            if not isinstance(value, str) or len(value) > 4096 or "\r" in value or "\n" in value:
                raise CloudSyncError("云同步连接凭证格式不正确")
            result[key] = value
        if len(result["bridge_key"]) < 32:
            raise CloudSyncError("云同步连接凭证不完整")
        return result

    def config(self):
        if not self.path.exists():
            return None
        try:
            return self.validate_config(json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            raise CloudSyncError("本机云同步配置无效，请在 Mac 上重新接入") from None

    def configure(self, raw):
        config = self.validate_config(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, ensure_ascii=False)
        temporary.replace(self.path)
        self.path.chmod(0o600)
        self.wake.set()
        return self.info()

    def info(self):
        status = self.store.get_value("cloud_sync_status", {})
        try:
            config = self.config()
        except CloudSyncError as exc:
            return {**status, "enabled": False, "configured": False, "message": str(exc)}
        return {**status, "configured": bool(config), "enabled": bool(config and config["enabled"]),
                "url": config["url"] if config else None, "running": self.lock.locked()}

    @staticmethod
    def _post(config, payload):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "Keqi/0.3 (Mac cloud sync)",
                   "X-Keqi-Bridge-Key": config["bridge_key"]}
        if config["sites_token"]:
            headers["OAI-Sites-Authorization"] = "Bearer " + config["sites_token"]
        request = Request(config["url"] + "/api/bridge/sync",
                          data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers, method="POST")
        try:
            with build_opener(NoRedirect()).open(request, timeout=25) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise CloudSyncError("云端返回过多内容，本次同步已暂停")
                value = json.loads(body)
        except HTTPError as exc:
            if exc.code == 403:
                raise CloudSyncError("云端访问网关拦截了连接，稍后会自动重试") from None
            if exc.code in {301, 302, 303, 307, 308, 401}:
                raise CloudSyncError("云端连接授权需要检查；本机任务已保留") from None
            if exc.code == 409:
                raise CloudSyncError("云端版本较新，本次未覆盖，请检查同步连接") from None
            raise CloudSyncError("云端暂时不可用，本机任务已保留") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise CloudSyncError("暂时无法连接云端，恢复网络后会自动重试") from None
        if not isinstance(value, dict) or not isinstance(value.get("operations"), list):
            raise CloudSyncError("云端返回格式不正确，本机任务已保留")
        if len(value["operations"]) > 500:
            raise CloudSyncError("待处理修改过多，本次同步已暂停")
        return value

    def _payload(self, receipts):
        sequence = int(self.store.get_value("cloud_sync_sequence", 0)) + 1
        self.store.set_value("cloud_sync_sequence", sequence)
        receipts = [{**receipt, **({"current": {key: value for key, value in receipt["current"].items()
                                                if key in PUBLIC_FIELDS}} if receipt.get("current") else {})}
                    for receipt in receipts]
        return {"sequence": sequence, "synced_at": now_iso(), "platform_synced_at": platform_synced_at(self.store),
                "tasks": [{key: value for key, value in task.items() if key in PUBLIC_FIELDS}
                          for task in self.store.tasks(include_deleted=True)], "receipts": receipts}

    def sync(self):
        if not self.lock.acquire(blocking=False):
            return {**self.info(), "applied": 0}
        applied = 0
        try:
            config = self.config()
            if not config or not config["enabled"]:
                return {**self.info(), "applied": 0}
            status = self.store.get_value("cloud_sync_status", {})
            status.update(last_attempt_at=now_iso(), message="正在交换手机修改")
            self.store.set_value("cloud_sync_status", status)
            response = self.transport(config, self._payload([]))
            receipts = []
            for operation in response["operations"]:
                receipt = self.store.apply_remote_operation(operation)
                receipts.append(receipt)
                applied += int(receipt["status"] == "applied")
            if receipts:
                response = self.transport(config, self._payload(receipts))
            conflicts = sum(receipt["status"] == "conflict" for receipt in receipts)
            status.update(last_success_at=now_iso(), ok=True,
                          message="已同步手机安排" if not conflicts else "部分修改有冲突，请在手机页处理",
                          pending_count=len(response["operations"]), conflict_count=conflicts)
            self.store.set_value("cloud_sync_status", status)
            return {**self.info(), "applied": applied}
        except CloudSyncError as exc:
            status = self.store.get_value("cloud_sync_status", {})
            status.update(last_attempt_at=now_iso(), ok=False, message=str(exc))
            self.store.set_value("cloud_sync_status", status)
            return {**self.info(), "applied": applied}
        finally:
            self.lock.release()
