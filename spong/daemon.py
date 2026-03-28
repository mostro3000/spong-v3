"""Daemonization utilities."""

import os
import sys
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def daemonize() -> None:
    """Fork the process and become a proper daemon."""
    # First fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.setsid()

    # Second fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open(os.devnull, "a+") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())


def write_pid(pid_file: Path | str) -> None:
    Path(pid_file).write_text(str(os.getpid()) + "\n")


def read_pid(pid_file: Path | str) -> int | None:
    try:
        return int(Path(pid_file).read_text().strip())
    except (OSError, ValueError):
        return None


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
