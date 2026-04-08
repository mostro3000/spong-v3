"""SPONG network agent - checks remote services on monitored hosts."""

from __future__ import annotations
import argparse
import importlib
import logging
import os
import random
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .daemon import daemonize, write_pid, read_pid, is_running
from .status_sender import send_status

log = logging.getLogger(__name__)

PLUGIN_PKG = "spong.plugins.network"


class NetworkAgent:
    def __init__(self):
        self._running = True
        self._plugins: dict[str, callable] = {}
        # Per-host/service failure counter for escalation
        self._fail_counts: dict[str, int] = {}
        self._last_statuses: dict[str, str] = {}
        self._lock = threading.Lock()  # protects _fail_counts and _last_statuses

    def load_plugins(self) -> None:
        """Load all network check plugins needed by configured hosts."""
        needed: set[str] = set()
        hosts = config.get_hosts()
        for hostname, hdata in hosts.items():
            for svc, _ in config.host_services(hostname):
                needed.add(svc)

        for svc in needed:
            mod_name = f"{PLUGIN_PKG}.{svc}"
            try:
                mod = importlib.import_module(mod_name)
                func = getattr(mod, f"check_{svc}", None)
                if func:
                    self._plugins[svc] = func
                    log.debug("Loaded network plugin: %s", svc)
                else:
                    log.warning("No check_%s in plugin %s", svc, mod_name)
            except ImportError as e:
                log.debug("No plugin for service '%s': %s", svc, e)

    def do_check(self, hostname: str, service: str) -> bool:
        """Run a check and send status. Returns True if status is OK (green)."""
        key = f"{hostname}/{service}"
        plugin = self._plugins.get(service)
        if not plugin:
            log.debug("No plugin for %s, skipping", service)
            return True

        try:
            color, summary, message = plugin(hostname)
        except Exception as e:
            log.error("Error in check_%s for %s: %s", service, hostname, e)
            color, summary, message = "red", f"check error: {e}", ""

        crit_level = config.get("network.crit_warn_level", 1)

        with self._lock:
            last_status = self._last_statuses.get(key, "green")
            if color == "red":
                self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
                count = self._fail_counts[key]
                if count < crit_level:
                    if last_status == "green":
                        send_status(hostname, service, "yellow",
                                    f"({count}/{crit_level}) {summary}", message)
                    self._last_statuses[key] = "yellow"
                    return False  # needs recheck
                else:
                    self._last_statuses[key] = "red"
            else:
                self._fail_counts[key] = 0
                self._last_statuses[key] = color

        send_status(hostname, service, color, summary, message)
        return color == "green"

    def run_host(self, hostname: str) -> list:
        """Run all checks for a single host. Returns list of (host, svc) that failed."""
        services = config.host_services(hostname)
        if not services:
            return []

        bad_services = []
        for service, stop_after in services:
            log.debug("Checking %s/%s", hostname, service)
            ok = self.do_check(hostname, service)
            if stop_after and not ok:
                log.debug("Check %s failed with stop_after, skipping rest", service)
                remaining = False
                for s, _ in services:
                    if remaining:
                        send_status(hostname, s, "clear",
                                    "Test skipped - prior check failed with stop_after")
                    if s == service:
                        remaining = True
                break
            if not ok:
                bad_services.append((hostname, service))

        return bad_services

    def _check_hosts_parallel(self, hostnames: list[str], workers: int,
                               batch_size: int = 20) -> list:
        """Check hosts in parallel batches. Each batch runs fully before the next starts."""
        all_bad = []
        max_workers = min(workers, batch_size) if workers > 0 else batch_size
        for i in range(0, len(hostnames), batch_size):
            chunk = hostnames[i:i + batch_size]
            log.debug("Checking batch %d-%d of %d hosts",
                      i + 1, i + len(chunk), len(hostnames))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(self.run_host, h): h for h in chunk}
                for future in as_completed(futures):
                    hostname = futures[future]
                    try:
                        bad = future.result()
                        all_bad.extend(bad)
                    except Exception as e:
                        log.error("Unexpected error checking host %s: %s", hostname, e)
        return all_bad

    def run(self, nosleep: bool = False) -> None:
        self.load_plugins()
        sleep_time = config.sleep_for("spong-network")
        recheck_sleep = config.get("network.recheck_sleep", 15)
        crit_level = config.get("network.crit_warn_level", 1)
        workers = int(config.get("network.workers", 30))
        batch_size = int(config.get("network.batch_size", 20))
        last_check = time.time()

        while self._running:
            hosts = config.get_hosts()
            active_hosts = [h for h, d in hosts.items() if not d.get("skip_network_checks")]

            t0 = time.time()
            all_bad = self._check_hosts_parallel(active_hosts, workers, batch_size)
            elapsed = time.time() - t0
            log.info("Checked %d hosts in %.1fs (%d failures)", len(active_hosts), elapsed, len(all_bad))

            # Batch recheck for failures
            for _ in range(crit_level):
                if not all_bad:
                    break
                time.sleep(recheck_sleep)
                recheck_hosts = list({h for h, s in all_bad})
                all_bad = self._check_hosts_parallel(recheck_hosts, workers, batch_size)

            if nosleep:
                break

            next_time = last_check + sleep_time * (0.95 + random.uniform(0, 0.1))
            now = time.time()
            if next_time > now:
                time.sleep(next_time - now)
            last_check = next_time

    def stop(self) -> None:
        self._running = False


def main():
    parser = argparse.ArgumentParser(description="SPONG network monitoring agent")
    parser.add_argument("--config", default=None)
    parser.add_argument("--nodaemonize", action="store_true")
    parser.add_argument("--nosleep", "--refresh", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--kill", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config.load_all(config_file=args.config)
    pid_file = config.tmp_path() / "spong-network.pid"

    if args.restart or args.kill:
        pid = read_pid(pid_file)
        if not pid:
            print("spong-network: no pid file found", file=sys.stderr)
            sys.exit(1)
        sig = signal.SIGHUP if args.restart else signal.SIGQUIT
        os.kill(pid, sig)
        sys.exit(0)

    if not args.nodaemonize and not args.nosleep and not args.debug:
        pid = read_pid(pid_file)
        if pid and is_running(pid):
            print(f"spong-network: already running as pid {pid}", file=sys.stderr)
            sys.exit(1)
        daemonize()
        write_pid(pid_file)

    agent = NetworkAgent()

    def _hup_handler(signum, frame):
        if pid_file.exists():
            pid_file.unlink()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _quit_handler(signum, frame):
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
