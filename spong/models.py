"""Data models for SPONG."""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import time

COLORS = ("green", "yellow", "red", "blue", "purple", "clear")
COLOR_PRIORITY = {c: i for i, c in enumerate(COLORS)}


def worst_color(colors: list[str]) -> str:
    """Return the most severe color from a list."""
    if not colors:
        return "green"
    # red > purple > yellow > blue > green > clear
    priority = {"red": 5, "purple": 4, "yellow": 3, "blue": 2, "green": 1, "clear": 0}
    return max(colors, key=lambda c: priority.get(c, 0))


@dataclass
class ServiceStatus:
    """Status of a single service on a host."""
    name: str
    color: str = "green"
    summary: str = ""
    message: str = ""
    report_time: float = field(default_factory=time.time)
    start_time: float = field(default_factory=time.time)
    expire_time: float = 0          # 0 = no expiry

    @property
    def duration(self) -> float:
        return self.report_time - self.start_time

    @property
    def is_expired(self) -> bool:
        return self.expire_time > 0 and time.time() > self.expire_time

    @property
    def is_stale(self, max_age: float = 900) -> bool:
        return (time.time() - self.report_time) > max_age


@dataclass
class HostStatus:
    """Aggregated status for a host."""
    name: str
    services: dict[str, ServiceStatus] = field(default_factory=dict)

    @property
    def color(self) -> str:
        if not self.services:
            return "green"
        return worst_color([s.color for s in self.services.values()])

    def has_problems(self) -> bool:
        return any(s.color not in ("green", "clear") for s in self.services.values())


@dataclass
class Acknowledgment:
    """An acknowledgment that suppresses alerts for a service."""
    ack_id: str
    host: str
    services: str           # comma-separated service names, or ".*" for all
    start_time: float
    end_time: float
    contact: str
    message: str = ""

    @property
    def is_expired(self) -> bool:
        return self.end_time != 0 and time.time() > self.end_time

    def covers(self, service: str) -> bool:
        import re
        for svc in self.services.split(","):
            svc = svc.strip()
            if svc in ("all", ".*", "*", ""):
                return True
            try:
                if re.fullmatch(svc, service):
                    return True
            except re.error:
                if svc == service:
                    return True
        return False


@dataclass
class HistoryEntry:
    """A single history event."""
    event_type: str         # status | ack | event | page
    timestamp: float
    service: str
    color: str = ""
    summary: str = ""
    user: str = ""

    def format_line(self) -> str:
        if self.event_type == "ack":
            return f"ack {int(self.timestamp)} {self.service} {self.user}\n"
        return (f"{self.event_type} {int(self.timestamp)} {self.service} "
                f"{self.color} {self.summary}\n")

    @classmethod
    def from_line(cls, line: str) -> "HistoryEntry | None":
        parts = line.strip().split(None, 5)
        if len(parts) < 3:
            return None
        etype = parts[0]
        try:
            ts = float(parts[1])
        except ValueError:
            return None
        svc = parts[2]
        color = parts[3] if len(parts) > 3 else ""
        summary = parts[4] if len(parts) > 4 else ""
        user = parts[3] if etype == "ack" and len(parts) > 3 else ""
        return cls(
            event_type=etype,
            timestamp=ts,
            service=svc,
            color=color,
            summary=summary,
            user=user,
        )
