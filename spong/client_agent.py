"""SPONG client agent - runs local checks on monitored hosts."""

from __future__ import annotations
import argparse
import importlib
import logging
import os
import random
import signal
import socket
import sys
import time
from pathlib import Path

from . import config
from .daemon import daemonize, write_pid, read_pid, is_running

log = logging.getLogger(__name__)

PLUGIN_PKG = "spong.plugins.client"
BINDIR = Path("/usr/local/spong/bin")


class ClientAgent:
    def __init__(self, hostname: str | None = None):
        self.hostname = hostname or config.get("hostname") or socket.gethostname()
        self._running = True
        self._check_funcs: dict[str, callable] = {}

    def load_checks(self) -> None:
        check_names = config.get_checks()
        for name in check_names:
            try:
                mod = importlib.import_module(f"{PLUGIN_PKG}.{name}")
                func = getattr(mod, f"check_{name}", None)
                if func:
                    self._check_funcs[name] = func
                    log.debug("Loaded client check: %s", name)
                else:
                    log.warning("No check_%s function in plugin %s", name, name)
            except ImportError as e:
                log.error("Could not load client check '%s': %s", name, e)

    def run_checks(self) -> None:
        for name, func in self._check_funcs.items():
            try:
                log.debug("Running check: %s", name)
                func(self.hostname)
            except Exception as e:
                log.error("Error running check %s: %s", name, e)

    def run(self, nosleep: bool = False) -> None:
        self.load_checks()
        sleep_time = config.sleep_for("spong-client")

        while self._running:
            self.run_checks()
            if nosleep:
                break
            # Add ±5% jitter to avoid thundering herd
            jitter = sleep_time * 0.1
            actual_sleep = sleep_time - jitter / 2 + random.uniform(0, jitter)
            log.debug("Sleeping for %.0f seconds", actual_sleep)
            time.sleep(actual_sleep)

    def stop(self) -> None:
        self._running = False


def main():
    parser = argparse.ArgumentParser(description="SPONG local monitoring agent")
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--nodaemonize", action="store_true")
    parser.add_argument("--nosleep", "--refresh", action="store_true",
                        help="Run one cycle and exit")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--restart", action="store_true",
                        help="Signal running agent to restart")
    parser.add_argument("--kill", action="store_true",
                        help="Signal running agent to stop")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config.load_all(config_file=args.config)
    pid_file = config.tmp_path() / "spong-client.pid"

    if args.restart or args.kill:
        pid = read_pid(pid_file)
        if not pid:
            print("spong-client: no pid file found", file=sys.stderr)
            sys.exit(1)
        sig = signal.SIGHUP if args.restart else signal.SIGQUIT
        os.kill(pid, sig)
        sys.exit(0)

    if not args.nodaemonize and not args.nosleep and not args.debug:
        # Check if already running
        pid = read_pid(pid_file)
        if pid and is_running(pid):
            print(f"spong-client: already running as pid {pid}", file=sys.stderr)
            sys.exit(1)
        daemonize()
        write_pid(pid_file)

    agent = ClientAgent()

    def _hup_handler(signum, frame):
        log.info("Received HUP, restarting...")
        if pid_file.exists():
            pid_file.unlink()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _quit_handler(signum, frame):
        log.info("Received QUIT, exiting...")
        if pid_file.exists():
            pid_file.unlink()
        sys.exit(0)

    signal.signal(signal.SIGHUP, _hup_handler)
    signal.signal(signal.SIGQUIT, _quit_handler)

    try:
        agent.run(nosleep=args.nosleep)
    finally:
        if pid_file.exists() and not args.nosleep:
            pid_file.unlink()


if __name__ == "__main__":
    main()
