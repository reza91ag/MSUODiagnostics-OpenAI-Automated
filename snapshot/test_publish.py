"""Snapshot publisher tests; no network calls."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).with_name("publish.py")
spec = importlib.util.spec_from_file_location("snapshot_publisher", MODULE)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class SnapshotPublisherTests(unittest.TestCase):
    def test_generated_timestamp_does_not_change_signature(self):
        root = Path(__file__).resolve().parents[1]
        a = publisher.signature({"generated_utc": "one", "total_requests": 1}, root)
        b = publisher.signature({"generated_utc": "two", "total_requests": 1}, root)
        c = publisher.signature({"generated_utc": "two", "total_requests": 2}, root)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_invalid_pdf_rejected(self):
        for payload in (b"", b"Not a PDF", b"%PDF-1.4\n" + b"x" * 600):
            with self.assertRaises(ValueError):
                publisher.validate_pdf(payload)

    def test_no_commit_if_measurements_unchanged(self):
        source = Path(__file__).resolve().parents[1]
        data = {"generated_utc": "changing timestamp", "total_requests": 10}
        sig = publisher.signature(data, source)

        class GithubStub:
            def current_version(self):
                return sig

            def commit_files(self, files):
                raise AssertionError("Should not commit unchanged PDF")

        with patch.object(publisher, "fetch_bytes", return_value=__import__("json").dumps(data).encode()):
            self.assertFalse(publisher.publish(source, GithubStub()))

    def test_commit_overwrites_one_pdf_and_viewer(self):
        source = Path(__file__).resolve().parents[1]
        data = {"generated_utc": "now", "total_requests": 12}
        pdf = b"%PDF-1.4\n" + b"x" * 600 + b"\n%%EOF\n"

        class GithubStub:
            files = None

            def current_version(self):
                return ""

            def commit_files(self, files):
                self.files = files
                return "test-commit"

        github = GithubStub()
        with patch.object(publisher, "fetch_bytes", side_effect=[
            __import__("json").dumps(data).encode(), pdf
        ]):
            self.assertTrue(publisher.publish(source, github))
        self.assertEqual(github.files["MSUO-Snapshot.pdf"], pdf)
        self.assertIn("index.html", github.files)
        self.assertIn("msuo-logo.svg", github.files)
        self.assertNotIn("server.py", github.files)
        self.assertNotIn("msuo_live_config.json", github.files)


if __name__ == "__main__":
    unittest.main()
