"""Client check: system uptime."""

import re
from ... import config
from ...safe_exec import safe_exec_str
from ...status_sender import send_status


def check_uptime(hostname: str) -> None:
    uptime_cmd = config.get_command("uptime", "/usr/bin/uptime")
    output = safe_exec_str(uptime_cmd, timeout=30)

    color = "green"
    up_str = "unknown"

    m = re.search(r"up\s+([^,]+)", output)
    if m:
        up_str = m.group(1).strip()
        if "min" in up_str.lower():
            color = "yellow"   # recently rebooted

    summary = f"up {up_str}"
    send_status(hostname, "uptime", color, summary, output.strip())
