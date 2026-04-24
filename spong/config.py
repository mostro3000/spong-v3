"""Configuration loading for SPONG."""

import os
import re
import yaml
from pathlib import Path
from typing import Any

BASE_DIR = Path("/usr/local/spong")
ETC_DIR = BASE_DIR / "etc"

_config: dict = {}
_hosts: dict = {}
_groups: dict = {}
_message_cfg: dict = {}


def load_all(
    config_file: str | None = None,
    hosts_file: str | None = None,
    groups_file: str | None = None,
    message_file: str | None = None,
) -> None:
    global _config, _hosts, _groups, _message_cfg
    _config = _load_yaml(config_file or ETC_DIR / "spong.yaml")
    h = _load_yaml(hosts_file or ETC_DIR / "hosts.yaml")
    _hosts = h.get("hosts", {})
    _message_cfg = _load_yaml(message_file or ETC_DIR / "message.yaml")
    g = _load_yaml(groups_file or ETC_DIR / "groups.yaml")
    _groups = g.get("groups", {})


def _load_yaml(path) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def get(key: str, default: Any = None) -> Any:
    """Get a top-level config value by dotted key path."""
    parts = key.split(".")
    obj = _config
    for p in parts:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(p, default)
        if obj is None:
            return default
    return obj


def server_host() -> str:
    return get("server.host", "localhost")


def update_port() -> int:
    return int(get("server.update_port", 1998))


def query_port() -> int:
    return int(get("server.query_port", 1999))


def bb_port() -> int:
    return int(get("server.bb_port", 1984))


def db_path() -> Path:
    return Path(get("database.path", "/usr/local/spong/var/database"))


def archive_path() -> Path:
    return Path(get("database.archive_path", "/usr/local/spong/var/archives"))


def tmp_path() -> Path:
    return Path(get("tmp_path", "/usr/local/spong/tmp"))


def sleep_for(program: str) -> int:
    sleep = get("sleep", {})
    return int(sleep.get(program, sleep.get("default", 300)))


def get_hosts() -> dict:
    return _hosts


def get_host(name: str) -> dict | None:
    return _hosts.get(name)


def get_groups() -> dict:
    return _groups


def get_contacts() -> dict:
    h = _load_yaml(ETC_DIR / "hosts.yaml")
    return h.get("contacts", {})


def get_message_config() -> dict:
    return _message_cfg


def get_checks() -> list[str]:
    checks_str = get("checks", "disk diski cpu processes logs memory uptime")
    return checks_str.split()


def get_threshold(category: str, key: str, default: Any = None) -> Any:
    thresholds = get("thresholds", {})
    cat = thresholds.get(category, {})
    if isinstance(cat, dict):
        return cat.get(key, default)
    return default


def commands() -> dict:
    return get("commands", {})


def get_command(name: str, default: str = "") -> str:
    return commands().get(name, default)


def http_urls_for(host: str) -> list[str]:
    http_cfg = get("http", {})
    urls = http_cfg.get("urls", {})
    host_urls = urls.get(host, urls.get("DEFAULT", [f"http://{host}/"]))
    return [u.replace("{host}", host) for u in host_urls]


def get_log_checks() -> list[dict]:
    return get("log_checks", [])


def get_processes() -> dict:
    return get("processes", {"crit": [], "warn": []})


def host_services(hostname: str) -> list[tuple[str, bool]]:
    """Return list of (service_name, stop_after) pairs for a host."""
    host = get_host(hostname)
    if not host:
        return []
    services_str = host.get("services", "")
    result = []
    for token in re.split(r"[\s,]+", services_str):
        token = token.strip()
        if not token:
            continue
        if token.endswith(":"):
            result.append((token[:-1], True))
        else:
            result.append((token, False))
    return result


def host_ips(hostname: str) -> list[str]:
    host = get_host(hostname)
    if not host:
        return [hostname]
    return host.get("ip_addr", [hostname])


def get_service_schedules(hostname: str, service: str) -> list[dict]:
    host = get_host(hostname)
    if not host:
        return []
    return host.get("schedules", {}).get(service, [])


def is_suppressed(hostname: str, service: str, ts: float | None = None) -> bool:
    """Return True if the service is in a configured suppression window.

    Schedule format (in hosts.yaml under host → schedules → service):
      - days: "1-5"   # 1=lunes … 7=domingo; rango o lista "1,2,3"
        from: "07:30"
        to:   "16:00"
    """
    import time as _time
    schedules = get_service_schedules(hostname, service)
    if not schedules:
        return False
    lt = _time.localtime(ts or _time.time())
    weekday = lt.tm_wday + 1          # tm_wday: 0=lun → 1-based: 1=lun, 7=dom
    now_min = lt.tm_hour * 60 + lt.tm_min

    for sched in schedules:
        days_str = str(sched.get("days", "1-7")).strip()
        if "-" in days_str:
            lo, hi = days_str.split("-", 1)
            in_days = int(lo) <= weekday <= int(hi)
        elif "," in days_str:
            in_days = weekday in {int(d) for d in days_str.split(",")}
        else:
            in_days = weekday == int(days_str)
        if not in_days:
            continue

        from_h, from_m = map(int, sched.get("from", "00:00").split(":"))
        to_h,   to_m   = map(int, sched.get("to",   "23:59").split(":"))
        if (from_h * 60 + from_m) <= now_min <= (to_h * 60 + to_m):
            return True
    return False
