"""Snapshot publisher tests; no network calls."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

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

    def test_write_changed_preserves_unchanged_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            publisher.write_changed(path, b"hello")
            self.assertEqual(path.read_bytes(), b"hello")
            publisher.write_changed(path, b"hello")
            self.assertEqual(path.read_bytes(), b"hello")
            publisher.write_changed(path, b"updated")
            self.assertEqual(path.read_bytes(), b"updated")


if __name__ == "__main__":
    unittest.main()
