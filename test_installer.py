import sys
import tempfile
import unittest
from pathlib import Path

import installer
from createai import BridgeError


class AtomicWriteTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits are not meaningful on Windows")
    def test_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            installer.atomic_write(path, b"x")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_leaves_no_temporary_file_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "config.toml"
            installer.atomic_write(path, b"hello")
            self.assertEqual(path.read_bytes(), b"hello")
            self.assertEqual([p.name for p in path.parent.iterdir()], ["config.toml"])

    def test_replaces_existing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.json"
            installer.atomic_write(path, b"old")
            installer.atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"new")


class PortNumberTests(unittest.TestCase):
    def test_accepts_a_high_port(self):
        self.assertEqual(installer.port_number("41117"), 41117)

    def test_rejects_privileged_and_nonsense(self):
        for value in ("80", "0", "70000", "abc", None):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                installer.port_number(value)


class ReadJsonTests(unittest.TestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(installer.read_json(Path("/nonexistent/settings.json")), {})

    def test_invalid_json_is_refused_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{not json")
            with self.assertRaises(BridgeError):
                installer.read_json(path)


class HealthTests(unittest.TestCase):
    def test_no_service_reports_unavailable(self):
        # Port 1 is never our bridge; this must fail fast rather than hang.
        self.assertIsNone(installer.bridge_state(1, attempts=1, delay=0))
        self.assertFalse(installer.bridge_healthy(1, attempts=1, delay=0))


if __name__ == "__main__":
    unittest.main()
