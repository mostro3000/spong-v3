"""Client check: hardware sensors (lm-sensors)."""

import re
from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status


def check_sensors(hostname: str) -> None:
    lines = safe_exec("sensors", timeout=30)
    if not lines or "[command not found" in lines[0]:
        send_status(hostname, "sensors", "green",
                    "sensors not installed", "")
        return

    message = "".join(lines)
    color = "green"
    issues = []

    for line in lines:
        # Look for ALARM annotations
        if "ALARM" in line:
            color = "red"
            issues.append(line.strip())

    summary = ("sensor alarms: " + "; ".join(issues)) if issues else "sensors ok"
    send_status(hostname, "sensors", color, summary, message)
