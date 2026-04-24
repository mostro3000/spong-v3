"""File-based database for SPONG.

Directory layout (compatible with original Perl version):
  var/database/<host>/services/<service>-<color>
  var/database/<host>/acks/<rand>-<start>-<end>
  var/database/<host>/history/current
  var/database/<host>/history/status/<time>-<service>
"""

from __future__ import annotations
import os
import re
import time
import logging
import random
from pathlib import Path
from typing import Optional

from . import config
from .models import ServiceStatus, Acknowledgment, HistoryEntry

log = logging.getLogger(__name__)

COLORS = ("red", "yellow", "green", "purple", "clear")


def _db() -> Path:
    return config.db_path()


def _host_dir(host: str) -> Path:
    return _db() / host


def _services_dir(host: str) -> Path:
    return _host_dir(host) / "services"


def _acks_dir(host: str) -> Path:
    return _host_dir(host) / "acks"


def _history_dir(host: str) -> Path:
    return _host_dir(host) / "history"


def _ensure_dirs(host: str) -> None:
    for d in [_services_dir(host), _acks_dir(host),
              _history_dir(host) / "status"]:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Service status persistence
# ---------------------------------------------------------------------------

def save_status(
    host: str,
    service: str,
    color: str,
    report_time: float,
    summary: str,
    message: str,
    ttl: int = 0,
) -> bool:
    """Save a status update. Returns True if the service color changed."""
    _ensure_dirs(host)
    svc_dir = _services_dir(host)

    previous = load_service(host, service)
    start_time = report_time
    if previous and previous.color == color:
        start_time = previous.start_time

    color_changed = previous is None or previous.color != color

    # Remove old color files for this service
    for c in COLORS:
        old = svc_dir / f"{service}-{c}"
        if old.exists():
            try:
                old.unlink()
            except OSError:
                pass

    # Write the new status file
    status_file = svc_dir / f"{service}-{color}"
    expire = 0 if ttl == 0 else int(report_time) + ttl
    data = (f"timestamp {int(start_time)} {int(report_time)}\n"
            f"{int(report_time)} {summary}\n"
            f"{message}")
    try:
        status_file.write_text(data)
        if expire:
            # Store expire time in the filename? No – track via mtime or separate field.
            # We encode it in a comment line:
            pass
    except OSError as e:
        log.error("save_status: %s", e)

    return color_changed


def load_service(host: str, service: str) -> Optional[ServiceStatus]:
    """Load a service status from disk."""
    svc_dir = _services_dir(host)
    for color in COLORS:
        f = svc_dir / f"{service}-{color}"
        if f.exists():
            try:
                lines = f.read_text(errors="replace").splitlines()
                start_time = report_time = time.time()
                summary = ""
                message_lines = []
                for i, line in enumerate(lines):
                    m = re.match(r"timestamp (\d+) (\d+)", line)
                    if m:
                        start_time = float(m.group(1))
                        report_time = float(m.group(2))
                        continue
                    m2 = re.match(r"(\d+) (.+)", line)
                    if m2 and i <= 2:
                        report_time = float(m2.group(1))
                        summary = m2.group(2)
                        message_lines = lines[i + 1:]
                        break
                return ServiceStatus(
                    name=service,
                    color=color,
                    summary=summary,
                    message="\n".join(message_lines),
                    report_time=report_time,
                    start_time=start_time,
                )
            except Exception as e:
                log.error("load_service %s/%s: %s", host, service, e)
    return None


def load_all_services(host: str) -> dict[str, ServiceStatus]:
    """Load all service statuses for a host."""
    svc_dir = _services_dir(host)
    if not svc_dir.exists():
        return {}
    services: dict[str, ServiceStatus] = {}
    seen: set[str] = set()
    try:
        for f in svc_dir.iterdir():
            m = re.match(r"^([a-z0-9_\-\.]+)-(red|yellow|green|purple|clear)$",
                         f.name)
            if m:
                svc_name = m.group(1)
                if svc_name not in seen:
                    seen.add(svc_name)
                    status = load_service(host, svc_name)
                    if status:
                        services[svc_name] = status
    except OSError:
        pass
    return services


def list_hosts() -> list[str]:
    """Return all hosts that have a database directory."""
    db = _db()
    if not db.exists():
        return []
    return sorted(d.name for d in db.iterdir() if d.is_dir())


def service_names_for(host: str) -> list[str]:
    """Return service names stored for a host."""
    svc_dir = _services_dir(host)
    if not svc_dir.exists():
        return []
    names = set()
    for f in svc_dir.iterdir():
        m = re.match(r"^([a-z0-9_\-\.]+)-(red|yellow|green|purple|clear)$", f.name)
        if m:
            names.add(m.group(1))
    return sorted(names)


# ---------------------------------------------------------------------------
# Acknowledgments
# ---------------------------------------------------------------------------

def save_ack(
    host: str,
    services: str,
    start_time: float,
    end_time: float,
    contact: str,
    message: str,
) -> str:
    """Save an acknowledgment. Returns the ack_id."""
    _ensure_dirs(host)
    rand_part = random.randint(100000, 999999)
    ack_id = f"{rand_part}-{int(start_time)}-{int(end_time)}"
    ack_file = _acks_dir(host) / ack_id
    data = f"{contact} {services}\n{message}\n"
    ack_file.write_text(data)
    return f"{host}-{services}-{int(end_time)}"


def delete_service(host: str, service: str) -> None:
    """Remove all color files for a service (used to clean up unconfigured services)."""
    svc_dir = _services_dir(host)
    for color in COLORS:
        f = svc_dir / f"{service}-{color}"
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass


def delete_ack(host: str, end_time: int) -> None:
    """Delete ack files matching the given end time."""
    acks_dir = _acks_dir(host)
    if not acks_dir.exists():
        return
    for f in acks_dir.iterdir():
        if f.name.endswith(f"-{end_time}"):
            try:
                f.unlink()
            except OSError:
                pass


def load_acks(host: str) -> list[Acknowledgment]:
    """Load all active acknowledgments for a host."""
    acks_dir = _acks_dir(host)
    if not acks_dir.exists():
        return []
    result = []
    now = time.time()
    for f in acks_dir.iterdir():
        m = re.match(r"(\d+)-(\d+)-(\d+)$", f.name)
        if not m:
            continue
        end_time = float(m.group(3))
        if end_time != 0 and end_time < now:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        start_time = float(m.group(2))
        try:
            lines = f.read_text().splitlines()
            if not lines:
                continue
            parts = lines[0].split(None, 1)
            contact = parts[0] if parts else ""
            services = parts[1] if len(parts) > 1 else ".*"
            msg = "\n".join(lines[1:]) if len(lines) > 1 else ""
            result.append(Acknowledgment(
                ack_id=f.name,
                host=host,
                services=services,
                start_time=start_time,
                end_time=end_time,
                contact=contact,
                message=msg,
            ))
        except Exception as e:
            log.error("load_acks %s/%s: %s", host, f.name, e)
    return result


def is_acknowledged(host: str, service: str) -> bool:
    """Check if a service has an active acknowledgment."""
    for ack in load_acks(host):
        if ack.covers(service):
            return True
    return False


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def append_history(host: str, entry: HistoryEntry) -> None:
    """Append an entry to the host's history file."""
    _ensure_dirs(host)
    history_file = _history_dir(host) / "current"
    with open(history_file, "a") as f:
        f.write(entry.format_line())


def load_history(
    host: str,
    max_age_days: float = 7,
    *,
    status_changes_only: bool = False,
) -> list[HistoryEntry]:
    """Load history entries newer than max_age_days.

    When status_changes_only is True, only keep color-bearing entries and
    collapse repeated consecutive states per service.
    """
    history_file = _history_dir(host) / "current"
    if not history_file.exists():
        return []
    cutoff = time.time() - max_age_days * 86400
    entries = []
    last_color_by_service: dict[str, str] = {}
    try:
        for line in history_file.read_text().splitlines():
            entry = HistoryEntry.from_line(line)
            if not entry:
                continue
            if status_changes_only:
                if not entry.color:
                    continue
                prev_color = last_color_by_service.get(entry.service)
                last_color_by_service[entry.service] = entry.color
                if prev_color == entry.color:
                    continue
            if entry.timestamp < cutoff:
                continue
            entries.append(entry)
    except Exception as e:
        log.error("load_history %s: %s", host, e)
    return entries


def save_status_detail(
    host: str,
    service: str,
    color: str,
    start_time: float,
    report_time: float,
    summary: str,
    message: str,
) -> None:
    """Save detailed status file for history."""
    _ensure_dirs(host)
    fname = f"{int(report_time)}-{service}"
    detail_file = _history_dir(host) / "status" / fname
    data = (f"timestamp {int(start_time)} {int(report_time)}\n"
            f"color {color}\n"
            f"{int(report_time)} {summary}\n"
            f"{message}\n")
    try:
        detail_file.write_text(data)
    except OSError as e:
        log.error("save_status_detail: %s", e)


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def remove_stale_services(host: str, max_days: int = 20) -> int:
    """Remove service files older than max_days. Returns count removed."""
    svc_dir = _services_dir(host)
    if not svc_dir.exists():
        return 0
    cutoff = time.time() - max_days * 86400
    count = 0
    for f in svc_dir.iterdir():
        if f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    return count


def archive_old_history(host: str, max_days: int = 30) -> int:
    """Move old history entries to archive. Returns count removed."""
    history_file = _history_dir(host) / "current"
    if not history_file.exists():
        return 0
    cutoff = time.time() - max_days * 86400
    keep = []
    archive = []
    for line in history_file.read_text().splitlines():
        entry = HistoryEntry.from_line(line)
        if entry and entry.timestamp < cutoff:
            archive.append(line)
        else:
            keep.append(line)
    if archive:
        archive_dir = config.archive_path() / host
        archive_dir.mkdir(parents=True, exist_ok=True)
        arch_file = archive_dir / f"history-{int(time.time())}"
        arch_file.write_text("\n".join(archive) + "\n")
        history_file.write_text("\n".join(keep) + "\n")
    return len(archive)
