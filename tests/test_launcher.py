"""The silent entry must remain usable while all of its output is bounded."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from launch_silent import LOG_MAX_BYTES, StartupLog, run_logged


class LauncherTests(unittest.TestCase):
    def setUp(self):
        root = ROOT / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="启动日志 test ", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "launcher.log"

    def test_continuous_output_keeps_only_two_bounded_files(self):
        log = StartupLog(self.path, max_bytes=256)
        for number in range(100):
            log.write(f"{number:03}: " + "x" * 50 + "\n")
        self.assertEqual(len(list(self.root.glob("launcher.log*"))), 3)  # log, backup, byte lock
        self.assertLessEqual(self.path.stat().st_size, 256)
        self.assertLessEqual(log.backup.stat().st_size, 256)
        self.assertIn("099:", self.path.read_text())

    def test_single_large_write_is_bounded_and_preserves_utf8(self):
        log = StartupLog(self.path, max_bytes=128)
        log.write("日志测试" * 100 + "最后一条")
        for path in (self.path, log.backup):
            self.assertLessEqual(path.stat().st_size, 128)
            path.read_text(encoding="utf-8")
        self.assertTrue(self.path.read_text(encoding="utf-8").endswith("最后一条"))

    def test_old_oversized_logs_are_trimmed(self):
        log = StartupLog(self.path, max_bytes=128)
        self.path.write_bytes(b"x" * 1000)
        log.backup.write_bytes(b"y" * 1000)
        log.write("new startup")
        self.assertLessEqual(self.path.stat().st_size, 128)
        self.assertLessEqual(log.backup.stat().st_size, 128)
        self.assertEqual(self.path.read_text(), "new startup")

    def test_child_stdout_stderr_and_exit_code(self):
        log = StartupLog(self.path, max_bytes=256)
        code = "import sys; print('x' * 2000); print('stdout-finished'); print('stderr-finished', file=sys.stderr); sys.exit(17)"
        self.assertEqual(run_logged([sys.executable, "-c", code], log), 17)
        text = self.path.read_text() + log.backup.read_text()
        self.assertIn("stdout-finished", text)
        self.assertIn("stderr-finished", text)
        self.assertLessEqual(self.path.stat().st_size, 256)
        self.assertLessEqual(log.backup.stat().st_size, 256)

    def test_rotation_recovers_from_temporary_windows_file_lock(self):
        log = StartupLog(self.path, max_bytes=128)
        log.write("x" * 128)
        replace = os.replace
        attempts = 0
        def temporarily_locked(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError("temporary file lock")
            return replace(source, destination)
        with patch("launch_silent.os.replace", side_effect=temporarily_locked):
            log.write("recovered")
        self.assertEqual(attempts, 3)
        self.assertEqual(self.path.read_text(), "recovered")
        self.assertEqual(log.backup.stat().st_size, 128)

    def test_rotation_stops_retrying_persistent_access_failure(self):
        log = StartupLog(self.path, max_bytes=128)
        log.write("x" * 128)
        with patch("launch_silent.os.replace", side_effect=PermissionError("denied")) as replace:
            with self.assertRaises(PermissionError):
                log.write("new")
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(self.path.stat().st_size, 128)

    def test_vbs_entry_in_directory_with_spaces_and_chinese(self):
        # Use the exact VBS/BAT/Python launchers, substituting a short-lived app.
        # This avoids opening a second monitor or changing personal settings.
        (self.root / "src").mkdir()
        for name in ("start_silent.vbs", "start_silent_inner.bat"):
            shutil.copy2(ROOT / name, self.root / name)
        shutil.copy2(ROOT / "src" / "launch_silent.py", self.root / "src" / "launch_silent.py")
        (self.root / "src" / "main.py").write_text(
            "import sys\nprint('x' * (2 * 1024 * 1024))\n"
            "print('VBS_CHAIN_OK')\nprint('STDERR_OK', file=sys.stderr)\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(Path(os.environ["WINDIR"]) / "System32" / "cscript.exe"),
             "//nologo", str(self.root / "start_silent.vbs")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = self.root / "logs"
        current, backup = logs / "launcher.log", logs / "launcher.log.1"
        self.assertTrue(backup.exists())
        self.assertLessEqual(current.stat().st_size, LOG_MAX_BYTES)
        self.assertLessEqual(backup.stat().st_size, LOG_MAX_BYTES)
        self.assertIn("VBS_CHAIN_OK", current.read_text(encoding="utf-8"))
        self.assertIn("STDERR_OK", current.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
