"""Source configuration requires a password for both reading and modification."""
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class SourcePasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_config = server.CONFIG
        self.old_path = server.CONFIG_PATH
        self.old_auth = (server.SOURCE_PASSWORD_SALT, server.SOURCE_PASSWORD_HASH,
                         server.SOURCE_PASSWORD_ITERATIONS)
        self.password = "test-secret-only"
        server.SOURCE_PASSWORD_SALT = b"test-salt-16-byte"
        server.SOURCE_PASSWORD_ITERATIONS = 1000
        server.SOURCE_PASSWORD_HASH = hashlib.pbkdf2_hmac(
            "sha256", self.password.encode(), server.SOURCE_PASSWORD_SALT, 1000
        )
        server.CONFIG_PATH = Path(self.temp.name) / "config.json"
        server.CONFIG = server.default_config()
        with server.SOURCE_AUTH_LOCK:
            server.SOURCE_AUTH_FAILURES.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        server.CONFIG = self.old_config
        server.CONFIG_PATH = self.old_path
        (server.SOURCE_PASSWORD_SALT, server.SOURCE_PASSWORD_HASH,
         server.SOURCE_PASSWORD_ITERATIONS) = self.old_auth
        with server.SOURCE_AUTH_LOCK:
            server.SOURCE_AUTH_FAILURES.clear()
        server.invalidate_cache()
        self.temp.cleanup()

    def request(self, method, path, password=None, data=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        headers = {}
        if password is not None:
            headers["X-MSUO-Source-Password"] = password
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(data) if data is not None else None,
                     headers=headers)
        response = conn.getresponse()
        result = (response.status, json.loads(response.read().decode()))
        conn.close()
        return result

    def test_get_requires_password(self):
        self.assertEqual(self.request("GET", "/api/config")[0], 401)
        self.assertEqual(self.request("GET", "/api/config", "wrong")[0], 401)
        status, body = self.request("GET", "/api/config", self.password)
        self.assertEqual(status, 200)
        self.assertEqual(body["sources"], [])

    def test_post_requires_password_and_does_not_change_config(self):
        source = {"name": "Example", "path": "C:\\Logs", "persistence": "direct",
                  "startup": "auto"}
        for password in (None, "wrong"):
            status, _ = self.request("POST", "/api/config", password, {"sources": [source]})
            self.assertEqual(status, 401)
            self.assertEqual(server.CONFIG["sources"], [])
        status, _ = self.request("POST", "/api/config", self.password, {"sources": [source]})
        self.assertEqual(status, 200)
        self.assertEqual(len(server.CONFIG["sources"]), 1)
        self.assertTrue(server.CONFIG_PATH.exists())

    def test_removing_sources_also_requires_password(self):
        source = {"name": "Example", "path": "C:\\Logs", "persistence": "direct",
                  "startup": "auto"}
        self.assertEqual(self.request("POST", "/api/config", self.password,
                                      {"sources": [source]})[0], 200)
        self.assertEqual(self.request("POST", "/api/config", None,
                                      {"sources": []})[0], 401)
        self.assertEqual(len(server.CONFIG["sources"]), 1)
        self.assertEqual(self.request("POST", "/api/config", self.password,
                                      {"sources": []})[0], 200)
        self.assertEqual(server.CONFIG["sources"], [])

    def test_repeated_wrong_passwords_are_throttled(self):
        for _ in range(server.SOURCE_AUTH_MAX_FAILURES):
            self.assertEqual(self.request("GET", "/api/config", "wrong")[0], 401)
        self.assertEqual(self.request("GET", "/api/config", "wrong")[0], 429)
        self.assertEqual(self.request("GET", "/api/config", self.password)[0], 429)


if __name__ == "__main__":
    unittest.main()
