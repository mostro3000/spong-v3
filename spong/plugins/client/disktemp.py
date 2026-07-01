"""Client check: disk temperatures via smartctl (smartmontools).

Modern replacement for the removed ``hddtemp`` tool. Reads each physical disk's
temperature from ``smartctl -j -i -A /dev/<disk>`` (``.temperature.current``),
which smartctl 7.x normalises for SATA / SAS / NVMe alike.

Enable by adding ``disktemp`` to the host's ``checks:`` list in spong.yaml.
Thresholds (degrees Celsius):
    thresholds.disktemp.warn  (default 50)  -> yellow
    thresholds.disktemp.crit  (default 60)  -> red

Disks whose temperature smartctl cannot read (virtio/VM disks, USB bridges, or
members behind a RAID HBA that need ``-d megaraid,N`` etc.) simply contribute no
reading; a host with no SMART-readable disk reports green (nothing to monitor),
not a perpetual yellow. A smartctl timeout/error on a disk IS surfaced (yellow),
since a hung probe is a classic dying-disk symptom.
"""

import json

from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status

_SEV = {"green": 0, "yellow": 1, "red": 2}


def _worse(a: str, b: str) -> str:
    return b if _SEV[b] > _SEV[a] else a


def _list_disks(lsblk_lines: list[str]) -> list[str]:
    """From ``lsblk -dn -o NAME,TYPE`` output, return real disk device names.

    TYPE==disk already excludes rom/loop/lvm/crypt/raid/part; we additionally
    drop pseudo block devices that still report TYPE=disk (zram swap, ram, nbd).
    """
    disks = []
    for line in lsblk_lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "disk":
            name = parts[0]
            if name.startswith(("zram", "ram", "nbd", "loop", "fd")):
                continue
            disks.append(name)
    return disks


def _parse_smartctl(text: str) -> tuple[int | None, str | None]:
    """Return (temperature_celsius, model) from smartctl -j output.

    Robust against: non-dict / null-valued fields, booleans, implausible
    readings (a broken sensor/bridge reporting 0 or a negative/huge value), and
    trailing stderr noise that safe_exec may append when smartctl exits with a
    non-zero SMART status bitmask.
    """
    raw = text.strip()
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        i = raw.find("{")
        if i < 0:
            return None, None
        try:
            data, _ = json.JSONDecoder().raw_decode(raw[i:])
        except json.JSONDecodeError:
            return None, None
    if not isinstance(data, dict):
        return None, None

    temp = (data.get("temperature") or {}).get("current")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)) or not (0 < temp < 120):
        temp = None
    else:
        temp = int(temp)

    model = data.get("model_name") or data.get("scsi_model_name") or data.get("product")
    model = model if isinstance(model, str) and model else None
    return temp, model


def _evaluate(readings: list[tuple[str, int]],
              errors: list[tuple[str, str]],
              warn: int, crit: int) -> tuple[str, str]:
    """readings: (disk, temp) with a valid temperature; errors: (disk, reason)."""
    if not readings and not errors:
        return "green", "sin discos con SMART legibles"

    color = "green"
    issues: list[str] = []
    oks: list[str] = []

    if readings:
        max_temp = max(t for _, t in readings)
        if max_temp >= crit:
            color = _worse(color, "red")
        elif max_temp >= warn:
            color = _worse(color, "yellow")
        oks.append("disk temps: " + ", ".join(f"{d}:{t}°C" for d, t in readings))

    if errors:
        color = _worse(color, "yellow")
        issues.append("sin lectura: " + ", ".join(f"{d} ({r})" for d, r in errors))

    return color, "; ".join(issues + oks)


def check_disktemp(hostname: str) -> None:
    smartctl = config.get_command("smartctl", "/usr/sbin/smartctl")
    warn = int(config.get("thresholds.disktemp.warn", 50))
    crit = int(config.get("thresholds.disktemp.crit", 60))

    lsblk_lines = safe_exec("lsblk -dn -o NAME,TYPE", timeout=15)
    disks = _list_disks(lsblk_lines)

    readings: list[tuple[str, int]] = []
    errors: list[tuple[str, str]] = []
    message_parts: list[str] = []

    for disk in disks:
        out = safe_exec(f"{smartctl} -j -i -A /dev/{disk}", timeout=30)
        joined = "".join(out)
        if "[command not found" in joined:
            # smartmontools is only a Recommends; a missing optional tool is not a
            # fault (matches sibling sensors.py).
            send_status(hostname, "disktemp", "green", "smartmontools no instalado", "")
            return
        if "[timeout after" in joined:
            errors.append((disk, "timeout"))
            message_parts.append(f"/dev/{disk}: timeout")
            continue
        if "[error:" in joined:
            errors.append((disk, "error"))
            message_parts.append(f"/dev/{disk}: error")
            continue
        temp, model = _parse_smartctl(joined)
        if temp is None:
            continue  # disk exposes no usable temperature -> benign, skip
        readings.append((disk, temp))
        label = f"/dev/{disk}" + (f" ({model})" if model else "")
        message_parts.append(f"{label}: {temp} C")

    color, summary = _evaluate(readings, errors, warn, crit)
    send_status(hostname, "disktemp", color, summary, "\n".join(message_parts))
