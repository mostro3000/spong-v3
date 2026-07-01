"""SPONG TCP protocol encode/decode.

Update protocol (port 1998):
    status <host> <service> <color> <timestamp[:ttl]> <summary>\\n<message>\\n
    ack <host> <services> <start> <end> <contact>\\n<message>\\n
    ack-del <host>-<service>-<endtime>\\n
    event <host> <service> <color> <timestamp> <summary>\\n<message>\\n
    page <host> <service> <color> <timestamp> <summary>\\n<message>\\n

Query protocol (port 1999):
    <cmd> [<hostlist>] <type> <view> [<extra>]\\n
    Commands: summary, problems, history, host, services, acks, service, grpsummary
"""

from __future__ import annotations
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

VALID_COLORS = frozenset(("red", "yellow", "green", "purple", "clear"))
VALID_HOST_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
VALID_SVC_RE = re.compile(r"^[a-z0-9_\-\.]+$")


def valid_host(host: str) -> bool:
    """True si el host es seguro para usar como componente de ruta.

    VALID_HOST_RE acepta cadenas compuestas sólo de puntos ("." / ".."), que
    usadas como nombre de directorio permiten escapar de var/database, var/rrd,
    etc. Se rechazan explícitamente.
    """
    return bool(VALID_HOST_RE.match(host)) and host not in (".", "..")


def valid_service(service: str) -> bool:
    """True si el service es seguro como componente de ruta."""
    return bool(VALID_SVC_RE.match(service)) and service not in (".", "..")


@dataclass
class StatusMessage:
    cmd: str
    host: str
    service: str
    color: str
    timestamp: float
    ttl: int
    summary: str
    message: str


@dataclass
class AckMessage:
    host: str
    services: str
    start_time: float
    end_time: float
    contact: str
    message: str


@dataclass
class AckDelMessage:
    ack_id: str          # host-services-endtime or host-ackfileid
    host: str
    services: str
    end_time: int
    ack_file_id: str | None = None


@dataclass
class QueryMessage:
    command: str
    hosts: str           # hostlist string or "all"
    fmt_type: str        # text | html | wml
    view: str            # brief | standard | full
    extra: str = ""


def parse_update(header: str, body: str) -> Optional[StatusMessage | AckMessage | AckDelMessage]:
    """Parse an incoming update message."""
    header = header.strip()

    # status / event / page
    m = re.match(
        r"^(status|event|page)\s+(\S+)\s+(\w+)\s+(\w+)\s+([\d:]+)\s*(.*)$",
        header,
    )
    if m:
        cmd, host, service, color, ts_raw, summary = m.groups()
        if not valid_host(host):
            log.warning("parse_update: invalid host [%s]", host)
            return None
        if not valid_service(service):
            log.warning("parse_update: invalid service [%s]", service)
            return None
        if color not in VALID_COLORS:
            log.warning("parse_update: invalid color [%s]", color)
            return None
        ttl = 0
        if ":" in ts_raw:
            ts_part, ttl_part = ts_raw.split(":", 1)
            ts = float(ts_part)
            try:
                ttl = int(ttl_part)
            except ValueError:
                ttl = 0
        else:
            ts = float(ts_raw)
        return StatusMessage(
            cmd=cmd, host=host, service=service, color=color,
            timestamp=ts, ttl=ttl, summary=summary, message=body,
        )

    # ack-del by exact on-disk ack id: host-rand-start-end
    m = re.match(r"^ack-del\s+([a-zA-Z0-9_\-\.]+)-(\d+-\d+-\d+)\s*$", header)
    if m:
        host, ack_file_id = m.group(1), m.group(2)
        if not valid_host(host):
            log.warning("parse_update: ack-del invalid host [%s]", host)
            return None
        return AckDelMessage(
            ack_id=f"{host}-{ack_file_id}",
            host=host, services="", end_time=0, ack_file_id=ack_file_id,
        )

    # Legacy ack-del by host-services-endtime. Keep it for CLI compatibility.
    m = re.match(r"^ack-del\s+([a-zA-Z0-9_\-\.]+)-([^\s]+)-(\d+)\s*$", header)
    if m:
        host, services, end_time = m.group(1), m.group(2), int(m.group(3))
        if not valid_host(host):
            log.warning("parse_update: legacy ack-del invalid host [%s]", host)
            return None
        return AckDelMessage(
            ack_id=f"{host}-{services}-{end_time}",
            host=host, services=services, end_time=end_time,
        )

    # ack
    m = re.match(r"^ack\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s*$", header)
    if m:
        host, services, start, end, contact = m.groups()
        if not valid_host(host):
            log.warning("parse_update: ack invalid host [%s]", host)
            return None
        return AckMessage(
            host=host, services=services,
            start_time=float(start), end_time=float(end),
            contact=contact, message=body,
        )

    log.warning("parse_update: unrecognized header [%s]", header)
    return None


def parse_query(line: str) -> Optional[QueryMessage]:
    """Parse a query request line."""
    line = line.strip().rstrip("\r")
    # Format: <cmd> [<hostlist>] <type> <view> [extra]
    m = re.match(
        r"^(\w+)\s+\[([^\]]*)\]\s+(\w+)\s+(\w+)\b\s*(.*)$", line
    )
    if m:
        return QueryMessage(
            command=m.group(1),
            hosts=m.group(2),
            fmt_type=m.group(3),
            view=m.group(4),
            extra=m.group(5).strip(),
        )
    log.warning("parse_query: can't parse [%s]", line)
    return None


def format_status_update(
    host: str, service: str, color: str, summary: str, message: str = "",
    ttl: int = 0,
) -> bytes:
    """Encode a status update message for sending to the server."""
    ts = int(time.time())
    ts_str = f"{ts}:{ttl}" if ttl else str(ts)
    header = f"status {host} {service} {color} {ts_str} {summary}\n"
    return (header + message + "\n").encode()


def format_ack_update(
    host: str, services: str, start_time: float, end_time: float,
    contact: str, message: str,
) -> bytes:
    """Encode an acknowledgment message."""
    header = (f"ack {host} {services} {int(start_time)} "
              f"{int(end_time)} {contact}\n")
    return (header + message + "\n").encode()


def format_ack_del(ack_id: str) -> bytes:
    """Encode an ack-delete message."""
    return f"ack-del {ack_id}\n".encode()
