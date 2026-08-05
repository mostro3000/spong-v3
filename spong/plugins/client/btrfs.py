"""Client check: btrfs filesystem health.

Monitors every mounted btrfs filesystem (deduplicated by UUID, so multiple
subvolume mounts of the same filesystem count once):

- ``btrfs device stats``  — per-device error counters (write/read/flush/
  corruption/generation). Any non-zero counter -> red: these are cumulative
  lifetime counters and the primary btrfs health signal.
- ``btrfs filesystem show`` — a missing/degraded device -> red.
- ``btrfs scrub status``  — Uncorrectable > 0 -> red; Corrected > 0 -> yellow;
  never scrubbed ("no stats available") is fine.

Enable by adding ``btrfs`` to the host's ``checks:`` list in spong.yaml.
Hosts without btrfs (no mounts, or tools not installed) report green.
"""

import re

from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status

_SEV = {"green": 0, "yellow": 1, "red": 2}


def _worse(a: str, b: str) -> str:
    return b if _SEV[b] > _SEV[a] else a


def _parse_mounts(findmnt_lines: list[str]) -> list[str]:
    """From ``findmnt -t btrfs -n -o TARGET,UUID``: one mountpoint per UUID."""
    seen: set[str] = set()
    mounts: list[str] = []
    for line in findmnt_lines:
        parts = line.split()
        if not parts:
            continue
        target = parts[0]
        uuid = parts[1] if len(parts) > 1 else target
        if uuid in seen:
            continue
        seen.add(uuid)
        mounts.append(target)
    return mounts


def _parse_device_stats(lines: list[str]) -> list[tuple[str, str, int]]:
    """Return [(device, counter, value)] for every non-zero error counter."""
    bad = []
    for line in lines:
        m = re.match(r"\[([^\]]+)\]\.(\w+)\s+(\d+)", line.strip())
        if m and int(m.group(3)) > 0:
            dev = m.group(1).replace("/dev/", "")
            bad.append((dev, m.group(2), int(m.group(3))))
    return bad


def _parse_scrub(text: str) -> tuple[int, int]:
    """Return (corrected, uncorrectable) from ``btrfs scrub status`` output."""
    corrected = uncorrectable = 0
    m = re.search(r"Corrected:\s*(\d+)", text)
    if m:
        corrected = int(m.group(1))
    m = re.search(r"Uncorrectable:\s*(\d+)", text)
    if m:
        uncorrectable = int(m.group(1))
    return corrected, uncorrectable


def _evaluate(per_mount: list[dict]) -> tuple[str, str]:
    """per_mount items: {mount, dev_errors, missing, corrected, uncorrectable,
    probe_error}. Pure function -> (color, summary)."""
    if not per_mount:
        return "green", "sin filesystems btrfs"

    color = "green"
    issues: list[str] = []
    oks: list[str] = []

    for fs in per_mount:
        mnt = fs["mount"]
        if fs.get("probe_error"):
            color = _worse(color, "yellow")
            issues.append(f"{mnt}: {fs['probe_error']}")
            continue
        parts = []
        if fs.get("missing"):
            color = _worse(color, "red")
            parts.append("device MISSING")
        for dev, counter, val in fs.get("dev_errors", []):
            color = _worse(color, "red")
            parts.append(f"{dev} {counter}={val}")
        if fs.get("uncorrectable", 0) > 0:
            color = _worse(color, "red")
            parts.append(f"scrub uncorrectable={fs['uncorrectable']}")
        elif fs.get("corrected", 0) > 0:
            color = _worse(color, "yellow")
            parts.append(f"scrub corrected={fs['corrected']}")
        if parts:
            issues.append(f"{mnt}: " + ", ".join(parts))
        else:
            oks.append(f"{mnt} ok ({fs.get('ndevs', '?')} discos)")

    summary = "; ".join(issues + oks)
    return color, ("btrfs " + summary if summary else "btrfs ok")


def check_btrfs(hostname: str) -> None:
    btrfs_cmd = config.get_command("btrfs", "/usr/bin/btrfs")

    out = safe_exec("findmnt -t btrfs -n -o TARGET,UUID", timeout=15)
    joined = "".join(out)
    if "[command not found" in joined:
        send_status(hostname, "btrfs", "green", "sin filesystems btrfs", "")
        return
    mounts = _parse_mounts(out)

    per_mount: list[dict] = []
    message_parts: list[str] = []

    for mnt in mounts:
        fs: dict = {"mount": mnt}
        stats = safe_exec(f"{btrfs_cmd} device stats {mnt}", timeout=30)
        sj = "".join(stats)
        if "[command not found" in sj:
            send_status(hostname, "btrfs", "green", "btrfs-progs no instalado", "")
            return
        if "[timeout" in sj or "[error:" in sj:
            fs["probe_error"] = "sin respuesta de btrfs (timeout/error)"
            per_mount.append(fs)
            message_parts.append(f"== {mnt} ==\n{sj}")
            continue
        fs["dev_errors"] = _parse_device_stats(stats)
        fs["ndevs"] = len([l for l in stats if ".write_io_errs" in l])

        show = safe_exec(f"{btrfs_cmd} filesystem show {mnt}", timeout=30)
        fs["missing"] = "missing" in "".join(show).lower()

        scrub = safe_exec(f"{btrfs_cmd} scrub status {mnt}", timeout=30)
        fs["corrected"], fs["uncorrectable"] = _parse_scrub("".join(scrub))

        per_mount.append(fs)
        message_parts.append(f"== {mnt} ==\n" + "".join(stats) + "".join(show) + "".join(scrub))

    color, summary = _evaluate(per_mount)
    send_status(hostname, "btrfs", color, summary, "\n".join(message_parts))
