import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request

from studyreminder.cloud_sync import CloudSync, CloudSyncError, NoRedirect
from studyreminder.core import Store


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "tasks.sqlite3")
        self.config = {"url": "https://example.test", "bridge_key": "testing-key-" * 4,
                       "sites_token": "testing-platform-access", "enabled": True}
        self.calls = []
        self.operations = []
        self.fail_ack = False
        def transport(config, payload):
            self.calls.append(payload)
            if payload["receipts"]:
                if self.fail_ack:
                    raise CloudSyncError("暂时无法连接云端")
                self.operations = []
            return {"operations": list(self.operations)}
        self.cloud = CloudSync(self.temp.name, self.store, transport)
        self.cloud.configure(self.config)
        self.task = self.store.put_task({"title": "云同步测试", "due_at": "2027-01-01T09:00:00+08:00"})

    def operation(self, **changes):
        return {"op_id": "operation-one", "task_id": self.task["id"], "action": "patch",
                "base_revision": self.task["revision"], "base": {key: self.task[key] for key in changes},
                "changes": changes}

    def test_sync_applies_phone_patch_and_acknowledges_updated_snapshot(self):
        self.operations = [self.operation(status="completed")]
        result = self.cloud.sync()
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.get_task(self.task["id"])["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertGreater(self.calls[1]["sequence"], self.calls[0]["sequence"])
        self.assertEqual(self.calls[1]["receipts"][0]["status"], "applied")
        self.assertEqual(self.calls[1]["tasks"][0]["status"], "completed")
        self.assertNotIn("calendar_id", self.calls[1]["receipts"][0]["current"])

    def test_lost_ack_retries_without_reapplying_phone_change(self):
        self.operations = [self.operation(title="手机改的标题")]
        self.fail_ack = True
        self.assertFalse(self.cloud.sync()["ok"])
        revision = self.store.get_task(self.task["id"])["revision"]
        self.fail_ack = False
        self.assertTrue(self.cloud.sync()["ok"])
        self.assertEqual(self.store.get_task(self.task["id"])["revision"], revision)
        self.assertEqual(self.calls[-1]["receipts"][0]["status"], "applied")

    def test_conflict_is_reported_without_overwriting_mac(self):
        self.operations = [self.operation(title="手机标题")]
        self.store.put_task({"title": "Mac更新标题"}, self.task["id"])
        result = self.cloud.sync()
        self.assertEqual(result["conflict_count"], 1)
        self.assertEqual(self.store.get_task(self.task["id"])["title"], "Mac更新标题")
        self.assertEqual(self.calls[-1]["receipts"][0]["status"], "conflict")

    def test_upload_contains_tombstones_but_no_local_identifiers_or_secrets(self):
        self.store.put_task({"calendar_id": "local-calendar-object"}, self.task["id"])
        self.store.delete_task(self.task["id"])
        self.cloud.sync()
        item = self.calls[0]["tasks"][0]
        self.assertTrue(item["deleted"])
        self.assertNotIn("calendar_id", item)
        self.assertNotIn("source_key", item)
        output = json.dumps(self.cloud.info()) + json.dumps(self.calls)
        self.assertNotIn(self.config["bridge_key"], output)
        self.assertNotIn(self.config["sites_token"], output)

    def test_disabled_and_unconfigured_bridges_make_no_network_request(self):
        self.cloud.configure({**self.config, "enabled": False})
        self.cloud.sync()
        self.assertFalse(self.calls)
        self.cloud.path.unlink()
        self.cloud.sync()
        self.assertFalse(self.calls)

    def test_credentials_saved_with_private_permissions_and_never_in_info(self):
        self.assertEqual(self.cloud.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("bridge_key", self.cloud.info())
        self.assertNotIn("sites_token", self.cloud.info())

    def test_sequence_continues_after_process_restart(self):
        self.cloud.sync()
        first = self.calls[-1]["sequence"]
        other = CloudSync(self.temp.name, self.store, self.cloud.transport)
        other.sync()
        self.assertGreater(self.calls[-1]["sequence"], first)

    def test_redirects_do_not_forward_private_credentials(self):
        handler = NoRedirect()
        self.assertIsNone(handler.redirect_request(Request("https://example.test"), None, 302, "Found", {}, "https://other.test"))

    def test_cloud_request_identifies_the_app_instead_of_default_library_agent(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"operations": []}'
        with patch("studyreminder.cloud_sync.build_opener", return_value=opener):
            self.cloud._post(self.config, {"tasks": []})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), "Keqi/0.3 (Mac cloud sync)")

    def test_config_rejects_insecure_or_credentialled_origins(self):
        for url in ["http://example.test", "https://user:pass@example.test", "https://example.test/a", "https://example.test/?token=x"]:
            with self.subTest(url=url), self.assertRaises(CloudSyncError):
                self.cloud.configure({**self.config, "url": url})


if __name__ == "__main__":
    unittest.main()
