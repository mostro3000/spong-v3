"""Client check: disk space usage."""

import re
from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status


def check_disk(hostname: str) -> None:
    df_cmd = config.get_command("df", "/bin/df")
    lines = safe_exec(df_cmd)

    thresholds = config.get("thresholds", {})
    disk_warn = thresholds.get("disk", {}).get("warn", {"ALL": 90})
    disk_crit = thresholds.get("disk", {}).get("crit", {"ALL": 95})
    ignore_patterns = thresholds.get("disk_ignore", [r"cd\d", "cdrom", ":", "proc"])

    color = "green"
    problems = []
    largest_pct = 0
    largest_name = ""
    message = ""

    # Skip header line
    for line in lines[1:]:
        line = line.rstrip()
        message += line + "\n"
        # Match: filesystem ... percent% mountpoint
        m = re.match(r"^(\S+)\s+.*?\s+(\d+)%\s+\S*\s*(/.*)$", line)
        if not m:
            continue
        rawfs, percent_str, name = m.group(1), m.group(2), m.group(3).strip()
        percent = int(percent_str)

        # Skip ignored filesystems
        skip = False
        for pat in ignore_patterns:
            if re.search(pat, rawfs) or re.search(pat, name):
                skip = True
                break
        if skip:
            continue

        warn = disk_warn.get(name, disk_warn.get(rawfs, disk_warn.get("ALL", 90)))
        crit = disk_crit.get(name, disk_crit.get(rawfs, disk_crit.get("ALL", 95)))

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

    # Check swap
    try:
        swap_info = _get_swap()
        if swap_info:
            pct, msg = swap_info
            message += f"\nSwap Space\n{msg}"
            warn = disk_warn.get("page", disk_warn.get("ALL", 90))
            crit = disk_crit.get("page", disk_crit.get("ALL", 95))
            if pct >= crit:
                color = "red"
                problems.append(f"swap {pct}%")
            elif pct >= warn:
                if color != "red":
                    color = "yellow"
                problems.append(f"swap {pct}%")
    except Exception:
        pass

    if not problems:
        summary = f"largest filesystem {largest_name} at {largest_pct}%"
    elif len(problems) == 1:
        summary = f"{problems[0]} full"
    else:
        summary = "multiple problems: " + ", ".join(problems)

    send_status(hostname, "disk", color, summary, message)


def _get_swap() -> tuple[int, str] | None:
    """Read swap usage from /proc/meminfo. Returns (pct_used, message)."""
    try:
        with open("/proc/meminfo") as f:
            content = f.read()
        m = re.search(r"SwapTotal:\s+(\d+)", content)
        m2 = re.search(r"SwapFree:\s+(\d+)", content)
        if m and m2:
            total = int(m.group(1))
            free = int(m2.group(1))
            if total == 0:
                return None
            used = total - free
            pct = int(used / total * 100)
            return pct, f"SwapTotal: {total} kB  SwapFree: {free} kB  Used: {pct}%\n"
    except Exception:
        pass
    return None
