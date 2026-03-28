"""Client check: memory usage."""

import re
from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status


def check_memory(hostname: str) -> None:
    mem_cmd = config.get_command("memcheck", "/usr/bin/free")

    warn = int(config.get("thresholds.memory.warn", 90))
    crit = int(config.get("thresholds.memory.crit", 95))

    lines = safe_exec(mem_cmd, timeout=30)
    message = "".join(lines)
    color, summary = _parse_free(lines, warn, crit)
    send_status(hostname, "memory", color, summary, message)


def _parse_free(lines: list[str], warn: int, crit: int) -> tuple[str, str]:
    """Parse /usr/bin/free output."""
    mem_total = mem_used = mem_free = 0
    swap_total = swap_used = 0
    hard_used = 0

    for line in lines:
        # Modern 'free' format:
        # Mem:    total    used    free  shared  buff/cache  available
        m = re.match(
            r"^Mem:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)?", line
        )
        if m:
            mem_total = int(m.group(1))
            available = int(m.group(6)) if m.group(6) else int(m.group(3))
            hard_used = mem_total - available
            continue

        # Older format: Mem:  total used free shared buffers cached
        m2 = re.match(
            r"^Mem:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", line
        )
        if m2:
            mem_total = int(m2.group(1))
            mem_used = int(m2.group(2))
            buffers = int(m2.group(5))
            cached = int(m2.group(6))
            hard_used = mem_used - buffers - cached
            continue

        # Swap line
        m3 = re.match(r"^Swap:\s+(\d+)\s+(\d+)\s+(\d+)", line)
        if m3:
            swap_total = int(m3.group(1))
            swap_used = int(m3.group(2))

    if mem_total == 0:
        return "yellow", "Could not parse memory info"

    phys_pct = int(hard_used / mem_total * 100) if mem_total else 0
    virt_total = mem_total + swap_total
    virt_used = hard_used + swap_used
    virt_pct = int(virt_used / virt_total * 100) if virt_total else phys_pct

    color = "green"
    if virt_pct > warn:
        color = "yellow"
    if virt_pct > crit:
        color = "red"

    summary = f"{phys_pct}% phys mem used, {virt_pct}% virt mem used"
    return color, summary
