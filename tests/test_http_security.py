import io
import json
import tempfile
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

from server import Application, Handler


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch("server.local_ip", return_value="192.168.1.20"):
            self.app = Application(self.temp.name, lan=True)

    def tearDown(self):
        self.temp.cleanup()

    def handler(self, host="127.0.0.1:8765", peer="127.0.0.1", origin=None, content_type="application/json", path="/api/tasks", cookie=None):
        h = Handler.__new__(Handler)
        h.server = SimpleNamespace(app=self.app)
        h.client_address = (peer, 9999)
        h.command, h.path = "POST", path
        h.headers = Message()
        h.headers["Host"] = host
        h.headers["Content-Type"] = content_type
        raw = json.dumps({"title": "HTTP测试", "due_at": "2027-01-01T00:00:00+08:00"}).encode()
        h.headers["Content-Length"] = str(len(raw))
        if origin:
            h.headers["Origin"] = origin
        if cookie:
            h.headers["Cookie"] = cookie
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        h.responses = []
        h.send = lambda *args, **kwargs: h.responses.append((args, kwargs))
        return h

    def test_host_rebinding_rejected(self):
        h = self.handler(host="evil.example:8765")
        h.mutate()
        self.assertEqual(h.responses[0][0][0], 403)

    def test_cross_origin_write_rejected(self):
        h = self.handler(origin="https://evil.example")
        h.mutate()
        self.assertEqual(h.responses[0][0][0], 403)

    def test_simple_form_content_type_rejected(self):
        h = self.handler(content_type="text/plain")
        h.mutate()
        self.assertEqual(h.responses[0][0][0], 400)

    def test_lan_requires_pairing_and_sets_httponly_cookie(self):
        h = self.handler(peer="192.168.1.40", host="192.168.1.20:8765")
        h.mutate()
        self.assertEqual(h.responses[0][0][0], 401)
        h = self.handler(peer="192.168.1.40", host="192.168.1.20:8765", path="/?token=" + self.app.token)
        h.command = "GET"
        h.do_GET()
        args, kwargs = h.responses[0]
        self.assertEqual(args[0], 303)
        self.assertIn("HttpOnly; SameSite=Strict", kwargs["extra"]["Set-Cookie"])

    def test_paired_lan_can_read(self):
        h = self.handler(peer="192.168.1.40", host="192.168.1.20:8765", cookie="study_pair=" + self.app.token, path="/api/state")
        h.command = "GET"
        h.do_GET()
        self.assertEqual(h.responses[0][0][0], 200)


if __name__ == "__main__":
    unittest.main()
