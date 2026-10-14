"""Run the silent startup chain with bounded output, including native stderr."""

import codecs
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

LOG_MAX_BYTES = 512 * 1024


class StartupLog:
    """Keep one current log and one backup, each at most 512 KiB.

    Close files after each write so rotation also works on Windows. A byte lock
    serializes writes from repeated VBS launches before the app's instance check.
    """

    def __init__(self, path, max_bytes=LOG_MAX_BYTES):
        if max_bytes < 4:
            raise ValueError("Log limit must fit a UTF-8 character")
        self.path = Path(path)
        self.backup = self.path.with_name(self.path.name + ".1")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text):
        data = text.encode("utf-8", errors="replace")
        with self.lock_path.open("a+b") as lock:
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                # Also bound logs left over from the previous unbounded launcher.
                for path in (self.path, self.backup):
                    if path.exists() and path.stat().st_size > self.max_bytes:
                        with path.open("rb+") as stream:
                            stream.seek(-self.max_bytes, os.SEEK_END)
                            tail = stream.read().decode("utf-8", errors="ignore").encode("utf-8")
                            stream.seek(0)
                            stream.write(tail)
                            stream.truncate()
                while data:
                    chunk = data[:self.max_bytes].decode("utf-8", errors="ignore").encode("utf-8")
                    data = data[len(chunk):]
                    size = self.path.stat().st_size if self.path.exists() else 0
                    if size + len(chunk) > self.max_bytes:
                        # Windows file scanners may briefly hold a just-written log.
                        for attempt in range(5):
                            try:
                                os.replace(self.path, self.backup)
                                break
                            except PermissionError:
                                if attempt == 4:
                                    raise
                                time.sleep(0.05 * (attempt + 1))
                    with self.path.open("ab") as stream:
                        stream.write(chunk)
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def run_logged(command, log):
    """Drain output continuously, without retaining the whole child output in RAM."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := process.stdout.read1(4096):
            log.write(decoder.decode(chunk))
        log.write(decoder.decode(b"", final=True))
        return process.wait()


def main():
    root = Path(__file__).resolve().parents[1]
    log = StartupLog(root / "logs" / "launcher.log")
    try:
        log.write("\n--- System Monitor silent startup ---\n")
        python = sys.executable
        if run_logged([python, "-c", "import PyQt6.QtWidgets"], log):
            log.write("Installing PyQt6...\n")
            if run_logged([python, "-m", "pip", "install", "PyQt6"], log):
                return 1
        return run_logged([python, str(root / "src" / "main.py")], log)
    except Exception:
        log.write(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
