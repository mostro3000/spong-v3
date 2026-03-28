"""Client check: CPU load average."""

import re
from ... import config
from ...safe_exec import safe_exec, safe_exec_str
from ...status_sender import send_status


def check_cpu(hostname: str) -> None:
    uptime_cmd = config.get_command("uptime", "/usr/bin/uptime")
    ps_cmd = config.get_command("ps", "/bin/ps ax")

    uptime_out = safe_exec_str(uptime_cmd, timeout=30)
    ps_lines = safe_exec(ps_cmd, timeout=30)

    cpu_warn = float(config.get("thresholds.cpu.warn", 7.0))
    cpu_crit = float(config.get("thresholds.cpu.crit", 8.0))

    # Parse uptime: "... up 3 days, 2:15, 2 users, load average: 0.10, 0.15, 0.20"
    up = ""
    users = "?"
    load = 0.0
    m = re.search(
        r"up\s+([^,]+),.*?(\d+)\s+user.*?"
        r"[\d.]+,\s*([\d.]+),\s*[\d.]+\s*$",
        uptime_out,
    )
    if m:
        up = m.group(1).strip()
        users = m.group(2)
        load = float(m.group(3))

    # Top 10 processes for message
    message = "".join(ps_lines[:11])
    jobs = max(0, len(ps_lines) - 1)

    color = "green"
    if "min" in up.lower():
        color = "yellow"   # recently rebooted
    if load > cpu_warn:
        color = "yellow"
    if load > cpu_crit:
        color = "red"

    summary = f"up {up}, load = {load}, {users} users, {jobs} jobs"
    send_status(hostname, "cpu", color, summary, message)
