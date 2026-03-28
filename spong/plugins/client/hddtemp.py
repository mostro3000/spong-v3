"""Client check: hard disk temperatures via hddtemp."""

import re
from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status


def check_hddtemp(hostname: str) -> None:
    cmd = config.get_command("hddtemp", "/usr/sbin/hddtemp")
    warn = int(config.get("thresholds.hddtemp.warn", 50))
    crit = int(config.get("thresholds.hddtemp.crit", 60))

    # Run hddtemp on all block devices that might have SMART
    lines = safe_exec(f"{cmd} /dev/sda /dev/sdb /dev/sdc /dev/sdd", timeout=30)
    output = "".join(lines)

    temps = []
    for line in lines:
        # Format: /dev/sdb: HP SSD S700 500GB: 39°C
        m = re.match(r"(/dev/\w+):\s+(.+?):\s+(\d+)°[CF]", line)
        if m:
            dev = m.group(1)
            label = m.group(2)
            temp = int(m.group(3))
            temps.append((dev, label, temp))

    if not temps:
        send_status(hostname, "hddtemp", "yellow", "No disk temperatures available", output)
        return

    max_temp = max(t for _, _, t in temps)
    color = "green"
    if max_temp >= warn:
        color = "yellow"
    if max_temp >= crit:
        color = "red"

    parts = [f"{dev.replace('/dev/','')}:{temp}°C" for dev, _, temp in temps]
    summary = "disk temps: " + ", ".join(parts)

    send_status(hostname, "hddtemp", color, summary, output)
