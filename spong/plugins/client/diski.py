"""Client check: disk inode usage."""

import re
from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status


def check_diski(hostname: str) -> None:
    dfi_cmd = config.get_command("dfi", "/bin/df -i")
    lines = safe_exec(dfi_cmd)

    thresholds = config.get("thresholds", {})
    warn_map = thresholds.get("disk", {}).get("warn", {"ALL": 90})
    crit_map = thresholds.get("disk", {}).get("crit", {"ALL": 95})
    ignore_patterns = thresholds.get("disk_ignore", [r"cd\d", "cdrom", ":", "proc"])

    color = "green"
    problems = []
    largest_pct = 0
    largest_name = ""
    message = ""

    for line in lines[1:]:
        line = line.rstrip()
        message += line + "\n"
        m = re.match(r"^(\S+)\s+.*?\s+(\d+)%\s+\S*\s*(/.*)$", line)
        if not m:
            continue
        rawfs, percent_str, name = m.group(1), m.group(2), m.group(3).strip()
        percent = int(percent_str)

        skip = False
        for pat in ignore_patterns:
            if re.search(pat, rawfs) or re.search(pat, name):
                skip = True
                break
        if skip:
            continue

        warn = warn_map.get(name, warn_map.get(rawfs, warn_map.get("ALL", 90)))
        crit = crit_map.get(name, crit_map.get(rawfs, crit_map.get("ALL", 95)))

        if percent > largest_pct:
            largest_pct = percent
            largest_name = name

        if percent >= crit:
            color = "red"
            problems.append(f"{name} {percent}%")
        elif percent >= warn:
            if color != "red":
                color = "yellow"
            problems.append(f"{name} {percent}%")

    if not problems:
        summary = f"largest inode use {largest_name} at {largest_pct}%"
    elif len(problems) == 1:
        summary = f"inode: {problems[0]} full"
    else:
        summary = "inode problems: " + ", ".join(problems)

    send_status(hostname, "diski", color, summary, message)
