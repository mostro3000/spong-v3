"""
rrd.py — Update existing RRD files from SPONG service status data and generate graph images.
"""

import logging
import os
import re
import subprocess
import math
import tempfile
import time

log = logging.getLogger(__name__)

RRD_BASE = "/usr/local/spong/var/rrd"

PERIOD_MAP = {
    "1h":  "-1h",
    "24h": "-1d",
    "7d":  "-7d",
    "30d": "-30d",
    "1y":  "-1y",
}

GRAPH_COLORS = [
    "--color", "BACK#f5f5ff",
    "--color", "CANVAS#ffffff",
    "--color", "GRID#cccccc",
]


_TCP_TIME_GRAPH_META = {
    "dns": ("DNS", "#ff6600"),
    "ftp": ("FTP", "#0277bd"),
    "http": ("HTTP", "#0077cc"),
    "https": ("HTTPS", "#00aa44"),
    "imap": ("IMAP", "#6a1b9a"),
    "mysql": ("MySQL", "#0077cc"),
    "ntp": ("NTP", "#00695c"),
    "pop": ("POP3", "#3949ab"),
    "poppassd": ("POPPASSD", "#6d4c41"),
    "proxy2": ("HTTP Proxy", "#546e7a"),
    "proxy": ("HTTP Proxy", "#546e7a"),
    "proxy_google": ("Google via proxy", "#1e88e5"),
    "rtsp": ("RTSP", "#00838f"),
    "smtp": ("SMTP", "#2e7d32"),
    "spamd": ("SPAMD", "#c0392b"),
    "ssh": ("SSH", "#8e24aa"),
    "telnet": ("Telnet", "#e65100"),
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _valid_host(host):
    """Un componente de host seguro para usar como nombre de directorio."""
    return bool(host) and "/" not in host and "\x00" not in host and host not in (".", "..")


def _rrd_dir(host):
    """Return (and create if necessary) the RRD directory for a host."""
    if not _valid_host(host):
        raise ValueError("invalid host for rrd dir: {!r}".format(host))
    path = os.path.join(RRD_BASE, host)
    os.makedirs(path, exist_ok=True)
    return path


def _run(cmd):
    """Run a command with subprocess.run; return CompletedProcess or None on error."""
    try:
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            log.warning("rrdtool error (cmd=%s): %s", cmd[1] if len(cmd) > 1 else cmd,
                        result.stderr.decode(errors="replace").strip())
        return result
    except Exception as exc:
        log.error("Failed to run %s: %s", cmd, exc)
        return None


def _rrd_exists(path):
    return os.path.isfile(path)


def _rrd_ds_count(path):
    """Número de DS en un RRD existente, o -1 si `rrdtool info` falla.

    Se devuelve -1 (no 0) ante error para que quien decide migrar/recrear pueda
    distinguir un fallo transitorio de `rrdtool info` de un RRD realmente viejo:
    un 0 hacía que _update_ping borrara y recreara un RRD sano, perdiendo hasta
    720 días de historial de ping.
    """
    result = _run(["rrdtool", "info", path])
    if result is None or result.returncode != 0:
        return -1
    return len(re.findall(rb"^ds\[\w+\]\.index\s*=", result.stdout, re.MULTILINE))


def _create_rrd(path, step, ds_args, rra_args, timestamp=None):
    """Create an RRD file with the given DS and RRA definitions."""
    cmd = ["rrdtool", "create", path, "--step", str(step)]
    if timestamp is not None:
        cmd += ["--start", str(int(timestamp) - 1)]
    cmd += ds_args + rra_args
    log.info("Creating RRD: %s", path)
    _run(cmd)


def _update_rrd(path, timestamp, values):
    """Update an RRD file.  values is a list of numbers/strings (U for unknown)."""
    ts = str(int(timestamp))
    val_str = ":".join(str(v) for v in values)
    cmd = ["rrdtool", "update", path, "{}:{}".format(ts, val_str)]
    log.debug("Updating RRD %s  %s:%s", path, ts, val_str)
    _run(cmd)


# ---------------------------------------------------------------------------
# Name-map helpers (disk / diski)
# ---------------------------------------------------------------------------

def _sanitize_name(mountpoint):
    """Convert a mount-point path into a safe RRD name component."""
    name = mountpoint.replace("/", "-")
    name = name.lstrip("-")
    name = re.sub(r"-{2,}", "-", name)
    return name or "root"


def get_rrd_name(host, service_prefix, mountpoint):
    """
    Look up or create the RRD base name for a mount-point.

    The map file lives at <rrd_dir>/<service_prefix>-name-map and contains
    lines of the form  ``rrd_name:mountpoint``.

    Returns the rrd_name string.
    """
    rrd_dir = _rrd_dir(host)
    map_file = os.path.join(rrd_dir, "{}-name-map".format(service_prefix))

    entries = {}  # rrd_name -> mountpoint
    if os.path.isfile(map_file):
        try:
            with open(map_file, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        entries[parts[0]] = parts[1]
        except Exception as exc:
            log.error("Failed to read name-map %s: %s", map_file, exc)

    # Check if mount-point already mapped
    for rrd_name, mp in entries.items():
        if mp == mountpoint:
            return rrd_name

    # Not found — create a new entry
    rrd_name = _sanitize_name(mountpoint)
    # Avoid collisions
    base = rrd_name
    idx = 1
    while rrd_name in entries:
        rrd_name = "{}-{}".format(base, idx)
        idx += 1

    entries[rrd_name] = mountpoint
    try:
        with open(map_file, "w") as fh:
            for k, v in entries.items():
                fh.write("{}:{}\n".format(k, v))
        log.info("Added %s -> %s to %s", mountpoint, rrd_name, map_file)
    except Exception as exc:
        log.error("Failed to write name-map %s: %s", map_file, exc)

    return rrd_name


def _read_name_map(rrd_dir, service_prefix):
    """Return {rrd_name: mountpoint} for disk/diski name maps."""
    map_file = os.path.join(rrd_dir, "{}-name-map".format(service_prefix))
    entries = {}
    if not os.path.isfile(map_file):
        return entries
    try:
        with open(map_file, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(":", 1)
                if len(parts) == 2:
                    entries[parts[0]] = parts[1]
    except Exception as exc:
        log.error("Failed to read name-map %s: %s", map_file, exc)
    return entries


def _partition_rrds(rrd_dir, service_prefix):
    """Return [(rrd_name, mountpoint, path)] for all disk/diski partition RRDs."""
    entries = _read_name_map(rrd_dir, service_prefix)
    results = []
    for fname in sorted(os.listdir(rrd_dir)):
        if not (fname.startswith(service_prefix + "-") and fname.endswith(".rrd")):
            continue
        rrd_name = fname[len(service_prefix) + 1:-4]
        path = os.path.join(rrd_dir, fname)
        mountpoint = entries.get(rrd_name, "/" + rrd_name.replace("-", "/"))
        results.append((rrd_name, mountpoint, path))
    results.sort(key=lambda item: (item[1] != "/", item[1]))
    return results



def _graph_include_mountpoint(mountpoint):
    """Hide noisy pseudo-filesystems from combined disk graphs."""
    noisy = (
        "/dev",
        "/dev/shm",
        "/run",
        "/run/lock",
        "/run/shm",
    )
    if mountpoint in noisy:
        return False
    if "/dev/.static/dev" in mountpoint:
        return False
    if mountpoint.endswith("/lib/init/rw"):
        return False
    if mountpoint.endswith("/dev") or mountpoint.endswith("/dev/shm"):
        return False
    return True


def _graph_mountpoint_label(mountpoint):
    """Compact labels so disk legends stay readable."""
    if mountpoint == "/":
        return "root"
    if mountpoint.startswith("/extra/vz/root/"):
        rest = mountpoint[len("/extra/vz/root/"):]
        return "vz/" + rest[:18]
    if mountpoint.startswith("/extra/"):
        return mountpoint[1:20]
    return mountpoint[1:19] if mountpoint.startswith("/") else mountpoint[:18]



def _current_service_message(host, service):
    """Return the current persisted message body for a host/service."""
    svc_dir = os.path.join("/usr/local/spong/var/database", host, "services")
    for color in ("red", "yellow", "green", "purple", "clear", "blue"):
        path = os.path.join(svc_dir, "{}-{}".format(service, color))
        if not os.path.isfile(path):
            continue
        try:
            lines = open(path, "r", errors="replace").read().splitlines()
        except Exception:
            return ""
        for i, line in enumerate(lines):
            if re.match(r"^(?:timestamp\s+\d+\s+\d+|\d+\s+)", line):
                continue
            return "\n".join(lines[i:])
        return ""
    return ""


def _mountpoints_from_df_message(message):
    """Extract mountpoints from persisted df/df -i status messages."""
    mountpoints = []
    seen = set()
    pattern = re.compile(r"\s\d+%\s+(/\S*)$")
    for line in (message or "").splitlines():
        match = pattern.search(line.strip())
        if not match:
            continue
        mountpoint = match.group(1)
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        mountpoints.append(mountpoint)
    return mountpoints


# ---------------------------------------------------------------------------
# RRA definitions shared across all services
# ---------------------------------------------------------------------------

_RRA_DEFS = [
    "RRA:AVERAGE:0.5:1:1440",
    "RRA:AVERAGE:0.5:12:2160",
    "RRA:AVERAGE:0.5:288:720",
]

_RRA_DEFS_WITH_EXTREMES = _RRA_DEFS + [
    "RRA:MIN:0.5:1:1440",
    "RRA:MIN:0.5:12:2160",
    "RRA:MIN:0.5:288:720",
    "RRA:MAX:0.5:1:1440",
    "RRA:MAX:0.5:12:2160",
    "RRA:MAX:0.5:288:720",
]


# ---------------------------------------------------------------------------
# Per-service update logic
# ---------------------------------------------------------------------------

def _update_ping(rrd_dir, host, summary, timestamp):
    path = os.path.join(rrd_dir, "ping-times.rrd")

    # Parse smokeping-style fields from summary
    m_avg  = re.search(r"time=([\d.]+)ms",  summary)
    m_min  = re.search(r"min=([\d.]+)ms",   summary)
    m_max  = re.search(r"max=([\d.]+)ms",   summary)
    m_loss = re.search(r"loss=([\d.]+)%",   summary)

    if not m_avg:
        log.debug("ping: no time= found in summary for %s", host)
        return

    avg  = float(m_avg.group(1))  / 1000.0
    mn   = float(m_min.group(1))  / 1000.0 if m_min  else avg
    mx   = float(m_max.group(1))  / 1000.0 if m_max  else avg
    loss = float(m_loss.group(1))           if m_loss else 0.0

    _PING_DS = [
        "DS:mn:GAUGE:600:0:100",
        "DS:avg:GAUGE:600:0:100",
        "DS:mx:GAUGE:600:0:100",
        "DS:loss:GAUGE:600:0:100",
    ]

    if not _rrd_exists(path):
        _create_rrd(path, 300, _PING_DS, _RRA_DEFS, timestamp)

    # Migrar RRDs viejos sin el DS de pérdida (tenían 2 o 3 DS: min/avg[/max]).
    # Sólo recreamos ante un conteo POSITIVO menor a 4 (RRD viejo real). Un -1
    # (info falló) o 0 no debe disparar el borrado: perderíamos el historial.
    ds_count = _rrd_ds_count(path)
    if 0 < ds_count < 4:
        log.info("ping: deleting old %d-DS RRD for %s, will recreate", ds_count, host)
        os.remove(path)
        _create_rrd(path, 300, _PING_DS, _RRA_DEFS, timestamp)

    _update_rrd(path, timestamp, [mn, avg, mx, loss])


def _update_disk(rrd_dir, host, message, timestamp):
    """Parse df block-usage output and update disk-{name}.rrd files."""
    # df output line: device  blocks  used  avail  pct%  mountpoint
    pattern = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(/\S*)")
    for m in pattern.finditer(message):
        pct = int(m.group(4))
        used_kb = int(m.group(2))
        used_bytes = used_kb * 1024
        mountpoint = m.group(5)

        rrd_name = get_rrd_name(host, "disk", mountpoint)
        path = os.path.join(rrd_dir, "disk-{}.rrd".format(rrd_name))

        if not _rrd_exists(path):
            _create_rrd(path, 300, [
                "DS:pct:GAUGE:600:0:100",
                "DS:used:GAUGE:600:0:U",
            ], _RRA_DEFS, timestamp)

        _update_rrd(path, timestamp, [pct, used_bytes])


def _update_diski(rrd_dir, host, message, timestamp):
    """Parse df -i inode-usage output and update diski-{name}.rrd files."""
    # df -i output line: device  inodes  iused  ifree  ipct%  mountpoint
    pattern = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(/\S*)")
    for m in pattern.finditer(message):
        pct = int(m.group(4))
        used_inodes = int(m.group(2))
        mountpoint = m.group(5)

        rrd_name = get_rrd_name(host, "diski", mountpoint)
        path = os.path.join(rrd_dir, "diski-{}.rrd".format(rrd_name))

        if not _rrd_exists(path):
            _create_rrd(path, 300, [
                "DS:pct:GAUGE:600:0:100",
                "DS:used:GAUGE:600:0:U",
            ], _RRA_DEFS, timestamp)

        _update_rrd(path, timestamp, [pct, used_inodes])


def _update_cpu(rrd_dir, host, summary, timestamp):
    path = os.path.join(rrd_dir, "la.rrd")
    m = re.search(r"load\s*=\s*([\d.]+),\s*(\d+)\s+users,\s*(\d+)\s+jobs", summary)
    if not m:
        log.debug("cpu: no load= pattern found in summary for %s", host)
        return
    loadavg = float(m.group(1))
    users = int(m.group(2))
    jobs = int(m.group(3))

    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:loadavg:GAUGE:600:0:100",
            "DS:users:GAUGE:600:0:U",
            "DS:jobs:GAUGE:600:0:U",
        ], _RRA_DEFS, timestamp)

    _update_rrd(path, timestamp, [loadavg, users, jobs])


def _update_memory(rrd_dir, host, summary, message, timestamp):
    path = os.path.join(rrd_dir, "mem.rrd")

    # Try to find physical and virtual pct in summary or message
    phys_pct = None
    virt_pct = None

    combined = (summary or "") + "\n" + (message or "")

    # Common patterns: "physical: 42%", "phys 42%", "physical memory: 42 %", etc.
    m_phys = re.search(r"phy(?:s(?:ical)?)?\s*(?:mem(?:ory)?)?\s*[:\-=]?\s*([\d.]+)\s*%",
                       combined, re.IGNORECASE)
    m_virt = re.search(r"virt(?:ual)?\s*(?:mem(?:ory)?)?\s*[:\-=]?\s*([\d.]+)\s*%",
                       combined, re.IGNORECASE)

    if m_phys:
        phys_pct = float(m_phys.group(1))
    if m_virt:
        virt_pct = float(m_virt.group(1))

    # Fallback: look for two consecutive percentages (phys then virt)
    if phys_pct is None or virt_pct is None:
        pcts = re.findall(r"([\d.]+)\s*%", combined)
        if len(pcts) >= 2 and phys_pct is None:
            phys_pct = float(pcts[0])
            virt_pct = float(pcts[1])
        elif len(pcts) == 1 and phys_pct is None:
            phys_pct = float(pcts[0])

    if phys_pct is None:
        log.debug("memory: could not parse percentages for %s", host)
        return

    if virt_pct is None:
        virt_pct = phys_pct

    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:phys_pct:GAUGE:600:0:100",
            "DS:virt_pct:GAUGE:600:0:100",
        ], _RRA_DEFS, timestamp)

    _update_rrd(path, timestamp, [phys_pct, virt_pct])


def _update_processes(rrd_dir, host, summary, timestamp):
    # "all required processes running (285 total)"
    m = re.search(r"\((\d+)\s+total\)", summary)
    if not m:
        return
    total = int(m.group(1))
    path = os.path.join(rrd_dir, "processes.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:total:GAUGE:600:0:U",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [total])


def _update_rcpu(rrd_dir, summary, timestamp, filename="rcpu.rrd"):
    m = re.search(r"(\d+)%", summary)
    if not m:
        return
    pct = int(m.group(1))
    path = os.path.join(rrd_dir, filename)
    if not _rrd_exists(path):
        _create_rrd(path, 300, ["DS:cpu:GAUGE:600:0:100"], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [pct])


def _update_rtemp(rrd_dir, summary, timestamp):
    # "rtemp: board 36.0°C, cpu 60.0°C"  or  "rtemp: cpu 44.8°C"
    temps = {}
    for m in re.finditer(r"(board|cpu)\s+([\d.]+)°C", summary):
        temps[m.group(1)] = float(m.group(2))
    if not temps:
        return
    path = os.path.join(rrd_dir, "rtemp.rrd")
    if not _rrd_exists(path):
        ds_args = [f"DS:{k}:GAUGE:600:-40:150" for k in sorted(temps)]
        _create_rrd(path, 300, ds_args, _RRA_DEFS, timestamp)
    info = _run(["rrdtool", "info", path])
    if not info:
        return
    existing_ds = re.findall(rb"ds\[(\w+)\]\.index\s*=\s*(\d+)", info.stdout)
    ordered = sorted(existing_ds, key=lambda x: int(x[1]))
    values = [temps.get(n.decode(), "U") for n, _ in ordered]
    _update_rrd(path, timestamp, values)


def _update_count_rrd(rrd_dir, summary, timestamp, pattern, filename, ds_name):
    m = re.search(pattern, summary)
    if not m:
        return
    count = int(m.group(1))
    path = os.path.join(rrd_dir, filename)
    if not _rrd_exists(path):
        _create_rrd(path, 300, [f"DS:{ds_name}:GAUGE:600:0:U"], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [count])


def _extract_first_float(*texts):
    for text in texts:
        if not text:
            continue
        m = re.search(r"(-?\d+(?:[.,]\d+)?)", text)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def _update_scalar_rrd(rrd_dir, summary, message, timestamp, filename, ds_name, ds_min="U", ds_max="U", rra_args=None):
    value = _extract_first_float(summary, message)
    if value is None:
        return
    path = os.path.join(rrd_dir, filename)
    if not _rrd_exists(path):
        _create_rrd(path, 300, [f"DS:{ds_name}:GAUGE:600:{ds_min}:{ds_max}"], rra_args or _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [value])


def _update_temp(rrd_dir, summary, message, timestamp):
    _update_scalar_rrd(rrd_dir, summary, message, timestamp, "temp.rrd", "temp", -40, 150, _RRA_DEFS_WITH_EXTREMES)


def _update_hum(rrd_dir, summary, message, timestamp):
    _update_scalar_rrd(rrd_dir, summary, message, timestamp, "hum.rrd", "hum", 0, 100)


def _update_macs(rrd_dir, summary, timestamp):
    _update_count_rrd(rrd_dir, summary, timestamp, r"(\d+)\s+MACs", "macs.rrd", "macs")


def _update_wassoc(rrd_dir, summary, timestamp):
    _update_count_rrd(rrd_dir, summary, timestamp, r"wassoc:\s*(\d+)", "wassoc.rrd", "assoc")


def _update_qmailq(rrd_dir, summary, message, timestamp):
    text = "\n".join(part for part in (summary, message) if part)
    m_local = re.search(r"Local Queue:\s*(\d+)", text)
    m_remote = re.search(r"Remote Queue:\s*(\d+)", text)
    if not m_local and not m_remote:
        return
    local = int(m_local.group(1)) if m_local else "U"
    remote = int(m_remote.group(1)) if m_remote else "U"
    path = os.path.join(rrd_dir, "qmailq.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:local:GAUGE:600:0:U",
            "DS:remote:GAUGE:600:0:U",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [local, remote])


def _update_iftraffic(rrd_dir, summary, message, timestamp):
    """Update iftraffic.rrd from iftraffic plugin output."""
    m_total = re.search(
        r"Total monitorizado:\s*in\s*([\d.]+)\s*Mbps,\s*out\s*([\d.]+)\s*Mbps",
        message or "",
    )
    if not m_total:
        return

    total_in = float(m_total.group(1))
    total_out = float(m_total.group(2))

    m_util = re.search(r"iftraffic:\s+.+?\s+([\d.]+)%\s+max\b", summary or "")
    peak_util = float(m_util.group(1)) if m_util else "U"

    path = os.path.join(rrd_dir, "iftraffic.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:in:GAUGE:600:0:100000",
            "DS:out:GAUGE:600:0:100000",
            "DS:util:GAUGE:600:0:100",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [total_in, total_out, peak_util])


def _update_uptime(rrd_dir, summary, timestamp):
    """Parse 'up X days', 'up X weeks Y days', 'up X minutes', etc. → days."""
    import re as _re
    s = summary.lower()
    days = 0.0
    m = _re.search(r"(\d+)\s*week", s)
    if m:
        days += int(m.group(1)) * 7
    m = _re.search(r"(\d+)\s*day", s)
    if m:
        days += int(m.group(1))
    m = _re.search(r"(\d+):(\d+)", s)  # "HH:MM"
    if m:
        days += (int(m.group(1)) * 60 + int(m.group(2))) / 1440.0
    elif (m := _re.search(r"(\d+)\s*hour", s)):
        days += int(m.group(1)) / 24.0
    m = _re.search(r"(\d+)\s*min", s)
    if m:
        days += int(m.group(1)) / 1440.0
    if days == 0.0:
        return
    path = os.path.join(rrd_dir, "uptime.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, ["DS:days:GAUGE:600:0:U"], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [days])


_SENSOR_META = {
    # service: (ds_name, label, unit, min, max, color, area)
    "temp":    ("temp",    "Temperatura",  "°C",  -20,  60,  "#cc0000", False),
    "hum":     ("hum",     "Humedad",      "%",     0, 100,  "#0077cc", True),
    "viento":  ("viento",  "Viento",       "km/h",  0, 200,  "#009900", False),
    "presion": ("presion", "Presión",      "hPa", 800, 1100, "#8e24aa", False),
    "rafaga":  ("rafaga",  "Ráfaga",       "km/h",  0, 200,  "#ff6600", False),
}


def _update_co2(rrd_dir, summary, timestamp):
    """Update co2.rrd with eCO2 (ppm), TVOC (ppb) and AQI from summary string."""
    # summary: "eCO2: 450ppm TVOC: 120ppb AQI: 1 (Good)"
    m_eco2 = re.search(r"eCO2:\s*([\d.]+)\s*ppm", summary)
    m_tvoc = re.search(r"TVOC:\s*([\d.]+)\s*ppb", summary)
    m_aqi  = re.search(r"AQI:\s*(\d+)", summary)
    if not (m_eco2 and m_tvoc and m_aqi):
        return
    eco2 = float(m_eco2.group(1))
    tvoc = float(m_tvoc.group(1))
    aqi  = float(m_aqi.group(1))
    path = os.path.join(rrd_dir, "co2.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:eco2:GAUGE:1200:0:10000",
            "DS:tvoc:GAUGE:1200:0:60000",
            "DS:aqi:GAUGE:1200:0:6",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [eco2, tvoc, aqi])


def _update_sensor(rrd_dir, service, summary, timestamp):
    """Update a simple single-DS sensor RRD from a numeric summary."""
    meta = _SENSOR_META.get(service)
    if not meta:
        return
    ds_name, _, _, mn, mx, _, _ = meta
    try:
        value = float(summary.strip())
    except (ValueError, AttributeError):
        return
    path = os.path.join(rrd_dir, f"{service}.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300,
                    [f"DS:{ds_name}:GAUGE:1200:{mn}:{mx}"],
                    _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [value])



def _update_soil(rrd_dir, summary, timestamp):
    """Update soil.rrd with key moisture sensors.

    summary: "Lluvia:0%  Valv:16%  PastoSE:100%  PastoNE:100%  ..."
    Stores: lluvia, valv, pasto_se, pasto_ne, pasto_no, cant_sur, cant_ne, cant_no
    """
    def _val(label):
        m = re.search(rf"{re.escape(label)}:([\d.]+)%", summary)
        return float(m.group(1)) if m else "U"

    path = os.path.join(rrd_dir, "soil.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:lluvia:GAUGE:600:0:100",
            "DS:valv:GAUGE:600:0:100",
            "DS:pasto_se:GAUGE:600:0:100",
            "DS:pasto_ne:GAUGE:600:0:100",
            "DS:pasto_no:GAUGE:600:0:100",
            "DS:cant_sur:GAUGE:600:0:100",
            "DS:cant_ne:GAUGE:600:0:100",
            "DS:cant_no:GAUGE:600:0:100",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [
        _val("Lluvia"), _val("Valv"),
        _val("PastoSE"), _val("PastoNE"), _val("PastoNO"),
        _val("CantSur"), _val("CantNE"), _val("CantNO"),
    ])


def _update_presence(rrd_dir, summary, timestamp):
    """Update presence.rrd: state (0=none,1=peaceful,2=move), distance (cm), lux.

    summary: "sin presencia  686lux" | "presente  140cm (estático)  686lux" | ...
    """
    # state: encode as numeric
    if "movimiento" in summary:
        state_val = 2.0
    elif "presente" in summary:
        state_val = 1.0
    else:
        state_val = 0.0

    m_dist = re.search(r"(\d+)\s*cm", summary)
    m_lux  = re.search(r"(\d+)\s*lux", summary)
    dist = float(m_dist.group(1)) if m_dist else "U"
    lux  = float(m_lux.group(1))  if m_lux  else "U"

    path = os.path.join(rrd_dir, "presence.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 60, [
            "DS:state:GAUGE:180:0:2",
            "DS:dist:GAUGE:180:0:1000",
            "DS:lux:GAUGE:180:0:100000",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [state_val, dist, lux])


def _update_speedtest(rrd_dir, summary, timestamp):
    """Update speedtest.rrd: down (Mbps), up (Mbps), ping (ms), jitter (ms).

    summary: "↓95.3Mbps  ↑48.1Mbps  ping:4.2ms  jitter:1.3ms"
    """
    m_down   = re.search(r"↓([\d.]+)Mbps", summary)
    m_up     = re.search(r"↑([\d.]+)Mbps", summary)
    m_ping   = re.search(r"ping:([\d.]+)ms", summary)
    m_jitter = re.search(r"jitter:([\d.]+)ms", summary)

    down   = float(m_down.group(1))   if m_down   else "U"
    up     = float(m_up.group(1))     if m_up     else "U"
    ping   = float(m_ping.group(1))   if m_ping   else "U"
    jitter = float(m_jitter.group(1)) if m_jitter else "U"

    if down == "U" and up == "U":
        return

    path = os.path.join(rrd_dir, "speedtest.rrd")
    # Heartbeat = 2.5x intervalo (300s) para tolerar retrasos
    hb = 750
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:down:GAUGE:{}:0:10000".format(hb),
            "DS:up:GAUGE:{}:0:10000".format(hb),
            "DS:ping:GAUGE:{}:0:5000".format(hb),
            "DS:jitter:GAUGE:{}:0:5000".format(hb),
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [down, up, ping, jitter])


def _graph_speedtest_stacked(rrd_path, host, start, width, height):
    """Two stacked panels: Throughput (down/up Mbps) + Latency (ping ms)."""
    import subprocess, io
    from PIL import Image

    panels = [
        {
            "title": f"Speedtest Velocidad — {host}",
            "vlabel": "Mbps",
            "defs": [
                f"DEF:down={rrd_path}:down:AVERAGE",
                f"DEF:up={rrd_path}:up:AVERAGE",
            ],
            "draw": [
                "AREA:down#1565c0:Bajada ",
                r"GPRINT:down:MAX:Máx\:%6.1lf Mbps",
                r"GPRINT:down:MIN:  Mín\:%6.1lf Mbps",
                r"GPRINT:down:AVERAGE:  Prom\:%6.1lf Mbps",
                r"GPRINT:down:LAST:  Últ\:%6.1lf Mbps\l",
                "LINE2:up#2e7d32:Subida ",
                r"GPRINT:up:MAX:Máx\:%6.1lf Mbps",
                r"GPRINT:up:MIN:  Mín\:%6.1lf Mbps",
                r"GPRINT:up:AVERAGE:  Prom\:%6.1lf Mbps",
                r"GPRINT:up:LAST:  Últ\:%6.1lf Mbps\l",
            ],
        },
        {
            "title": f"Speedtest Latencia — {host}",
            "vlabel": "ms",
            "defs": [
                f"DEF:ping={rrd_path}:ping:AVERAGE",
                f"DEF:jitter={rrd_path}:jitter:AVERAGE",
                # banda: ping ± jitter
                "CDEF:smoke_hi=ping,jitter,+",
                "CDEF:smoke_lo=ping,jitter,-",
                # area superior (ping+jitter), luego area inferior sobre blanco para recortar
                "CDEF:band=smoke_hi,smoke_lo,-",
            ],
            "draw": [
                # base invisible hasta smoke_lo (piso de la banda)
                "AREA:smoke_lo#ffffff00",
                # banda de jitter encima
                "AREA:band#e6510040",
                # bordes de la banda
                "LINE1:smoke_hi#e6510080",
                "LINE1:smoke_lo#e6510080",
                # línea de ping encima
                "LINE1.5:ping#e65100:Ping    ",
                r"GPRINT:ping:MAX:Máx\:%5.1lf ms",
                r"GPRINT:ping:MIN:  Mín\:%5.1lf ms",
                r"GPRINT:ping:AVERAGE:  Prom\:%5.1lf ms",
                r"GPRINT:ping:LAST:  Últ\:%5.1lf ms\l",
                "LINE1:jitter#0000ff00:Jitter  ",
                r"GPRINT:jitter:MAX:Máx\:%5.1lf ms",
                r"GPRINT:jitter:MIN:  Mín\:%5.1lf ms",
                r"GPRINT:jitter:AVERAGE:  Prom\:%5.1lf ms",
                r"GPRINT:jitter:LAST:  Últ\:%5.1lf ms\l",
            ],
        },
    ]

    images = []
    for p in panels:
        cmd = [
            "rrdtool", "graph", "-",
            "--start", str(start), "--end", "now",
            "--width", str(width), "--height", str(max(height // 2 - 10, 120)),
            "--title", p["title"],
            "--vertical-label", p["vlabel"],
            "--color", "BACK#ffffff",
            "--color", "CANVAS#f5f5ff",
            "--slope-mode",
        ] + p["defs"] + p["draw"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            images.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))

    if not images:
        return None
    total_h = sum(im.height for im in images)
    out = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for im in images:
        out.paste(im, (0, y))
        y += im.height
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _update_ups(rrd_dir, summary, timestamp):
    """Update ups.rrd: Vin, Vout, Fin, Fout, Tbat (from ups plugin summary).

    summary: "Vin:220V  Vout:220V  Fin:50.0Hz  Fout:50.0Hz  Tbat:25°C"
    """
    import re as _re
    def _val(label, divisor=1.0):
        m = _re.search(rf"{label}:([\d.]+)", summary)
        return float(m.group(1)) / divisor if m else "U"

    path = os.path.join(rrd_dir, "ups.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, [
            "DS:vin:GAUGE:600:0:300",
            "DS:vout:GAUGE:600:0:300",
            "DS:fin:GAUGE:600:0:100",
            "DS:fout:GAUGE:600:0:100",
            "DS:tbat:GAUGE:600:0:100",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [
        _val("Vin"), _val("Vout"),
        _val("Fin"), _val("Fout"),
        _val("Tbat"),
    ])


def _graph_ups_stacked(rrd_path, host, start, width, height):
    """Two stacked panels: Voltage (Vin/Vout) + Frequency (Fin/Fout)."""
    import subprocess, tempfile
    from PIL import Image
    import io

    panels = [
        {
            "title": f"UPS Tensión — {host}",
            "vlabel": "Voltios",
            "defs": [
                f"DEF:vin={rrd_path}:vin:AVERAGE",
                f"DEF:vout={rrd_path}:vout:AVERAGE",
            ],
            "draw": [
                "LINE2:vin#1565c0:Vin",
                "LINE2:vout#2e7d32:Vout",
            ],
        },
        {
            "title": f"UPS Frecuencia — {host}",
            "vlabel": "Hz",
            "defs": [
                f"DEF:fin={rrd_path}:fin:AVERAGE",
                f"DEF:fout={rrd_path}:fout:AVERAGE",
            ],
            "draw": [
                "LINE2:fin#e65100:Fin",
                "LINE2:fout#6a1b9a:Fout",
            ],
        },
    ]

    images = []
    for p in panels:
        cmd = [
            "rrdtool", "graph", "-",
            "--start", str(start), "--end", "now",
            "--width", str(width), "--height", str(height // 2 - 10),
            "--title", p["title"],
            "--vertical-label", p["vlabel"],
            "--color", "BACK#ffffff",
            "--color", "CANVAS#f5f5ff",
            "--slope-mode",
        ] + p["defs"] + p["draw"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            images.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))

    if not images:
        return None
    total_h = sum(im.height for im in images)
    out = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for im in images:
        out.paste(im, (0, y))
        y += im.height
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _graph_iftraffic_stacked(rrd_path, host, start, width, height):
    """Two stacked panels: total throughput + peak interface utilization."""
    import subprocess, io
    from PIL import Image

    panel_h = max(height // 2 - 10, 120)
    panels = [
        {
            "title": f"Tráfico interfaces — {host}",
            "vlabel": "Mbps",
            "defs": [
                f"DEF:tin={rrd_path}:in:AVERAGE",
                f"DEF:tout={rrd_path}:out:AVERAGE",
            ],
            "draw": [
                "AREA:tin#1565c0:Entrada total ",
                r"GPRINT:tin:MAX:Máx\:%6.1lf Mbps",
                r"GPRINT:tin:MIN:  Mín\:%6.1lf Mbps",
                r"GPRINT:tin:AVERAGE:  Prom\:%6.1lf Mbps",
                r"GPRINT:tin:LAST:  Últ\:%6.1lf Mbps\l",
                "LINE2:tout#2e7d32:Salida total ",
                r"GPRINT:tout:MAX:Máx\:%6.1lf Mbps",
                r"GPRINT:tout:MIN:  Mín\:%6.1lf Mbps",
                r"GPRINT:tout:AVERAGE:  Prom\:%6.1lf Mbps",
                r"GPRINT:tout:LAST:  Últ\:%6.1lf Mbps\l",
            ],
        },
        {
            "title": f"Utilización máxima — {host}",
            "vlabel": "%",
            "defs": [
                f"DEF:util={rrd_path}:util:AVERAGE",
            ],
            "draw": [
                "--upper-limit", "100",
                "HRULE:70#f9a825:Warn 70%",
                "HRULE:90#e53935:Crit 90%",
                "AREA:util#8e24aa66:Util máx ",
                "LINE2:util#8e24aa",
                r"GPRINT:util:MAX:Máx\:%5.1lf%%",
                r"GPRINT:util:MIN:  Mín\:%5.1lf%%",
                r"GPRINT:util:AVERAGE:  Prom\:%5.1lf%%",
                r"GPRINT:util:LAST:  Últ\:%5.1lf%%\l",
            ],
        },
    ]

    images = []
    for p in panels:
        cmd = [
            "rrdtool", "graph", "-",
            "--start", str(start), "--end", "now",
            "--width", str(width), "--height", str(panel_h),
            "--title", p["title"],
            "--vertical-label", p["vlabel"],
            "--color", "BACK#ffffff",
            "--color", "CANVAS#f5f5ff",
            "--slope-mode",
        ] + p["defs"] + p["draw"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            images.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))

    if not images:
        return None
    total_h = sum(im.height for im in images)
    out = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for im in images:
        out.paste(im, (0, y))
        y += im.height
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _update_termica(rrd_dir, summary, message, timestamp):
    """Update termica.rrd with voltage, current, power, energy, leakage, temp.

    summary: "220.5V  0.487A  6.4W" (+ optional temp/leakage fields)
    message: multi-line with labeled values
    """
    import re as _re
    def _extract(pattern, text, divisor=1.0):
        m = _re.search(pattern, text)
        return float(m.group(1)) / divisor if m else None

    voltage = _extract(r"Tensión:\s*([\d.]+)", message)
    current = _extract(r"Corriente:\s*([\d.]+)", message)
    power   = _extract(r"Potencia:\s*([\d.]+)", message)
    energy  = _extract(r"Energía:\s*([\d.]+)", message)
    leakage = _extract(r"Corriente de fuga:\s*([\d.]+)", message)
    temp    = _extract(r"Temp interna:\s*([\d.]+)", message)

    if voltage is None or current is None or power is None:
        return

    path = os.path.join(rrd_dir, "termica.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 60, [
            "DS:voltage:GAUGE:700:100:300",
            "DS:current:GAUGE:700:0:100",
            "DS:power:GAUGE:700:0:30000",
            "DS:energy:GAUGE:700:0:U",
            "DS:leakage:GAUGE:700:0:1000",
            "DS:temp:GAUGE:700:-50:150",
        ], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [
        voltage,
        current,
        power,
        energy if energy is not None else "U",
        leakage if leakage is not None else 0,
        temp if temp is not None else "U",
    ])


def _update_tcp_time(rrd_dir, service, summary, timestamp):
    """Generic response-time updater: parses 'Xs' or 'Xms' from summary."""
    m = re.search(r"([\d.]+)\s*ms", summary)
    if m:
        seconds = float(m.group(1)) / 1000.0
    else:
        m = re.search(r"([\d.]+)\s*s\b", summary)
        if not m:
            return
        seconds = float(m.group(1))
    path = os.path.join(rrd_dir, f"{service}-time.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, ["DS:time:GAUGE:600:0:300"], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [seconds])


def _update_hddtemp(rrd_dir, host, summary, timestamp):
    # Accept both legacy "disk temps: sda:39°C" and current "/dev/sda: 39" summaries.
    temps = {}
    for m in re.finditer(r"(?:/dev/)?([A-Za-z0-9._-]+)\s*:\s*(\d+)(?:\s*°[CF])?", summary):
        dev = m.group(1)[:19]
        temps[dev] = int(m.group(2))
    if not temps:
        return
    sorted_devs = sorted(temps.keys())
    path = os.path.join(rrd_dir, "hddtemp.rrd")
    if not _rrd_exists(path):
        ds_args = [f"DS:{dev}:GAUGE:600:0:100" for dev in sorted_devs]
        _create_rrd(path, 300, ds_args, _RRA_DEFS, timestamp)
    info = _run(["rrdtool", "info", path])
    if not info:
        return
    existing_ds = re.findall(rb"ds\[(\w+)\]\.index\s*=\s*(\d+)", info.stdout)
    ordered = sorted(existing_ds, key=lambda x: int(x[1]))
    values = [temps.get(ds_name.decode(), "U") for ds_name, _ in ordered]
    if values:
        _update_rrd(path, timestamp, values)


def _update_sensors(rrd_dir, host, message, timestamp):
    # Parse "Package id 0:  +45.0°C" and "Core N:  +44.0°C"
    temps = {}
    for line in (message or "").splitlines():
        m = re.match(r"^(Package id \d+|Core \d+):\s+[+\-]([\d.]+)°C", line.strip())
        if m:
            label = m.group(1).lower().replace(" ", "_").replace("package_id", "pkg")
            temps[label] = float(m.group(2))
    if not temps:
        return
    # Build DS list sorted by label for consistency
    sorted_labels = sorted(temps.keys())
    path = os.path.join(rrd_dir, "sensors.rrd")
    if not _rrd_exists(path):
        ds_args = [f"DS:{label[:19]}:GAUGE:600:-100:150" for label in sorted_labels]
        _create_rrd(path, 300, ds_args, _RRA_DEFS, timestamp)
    # Get actual DS order from existing file
    info = _run(["rrdtool", "info", path])
    if not info:
        return
    existing_ds = re.findall(rb"ds\[(\w+)\]\.index\s*=\s*(\d+)", info.stdout)
    ordered = sorted(existing_ds, key=lambda x: int(x[1]))
    values = []
    for ds_name, _ in ordered:
        label = ds_name.decode()
        values.append(temps.get(label, "U"))
    if values:
        _update_rrd(path, timestamp, values)


# ---------------------------------------------------------------------------
# Public API: update_from_status
# ---------------------------------------------------------------------------

def update_from_status(host, service, summary, message, timestamp):
    """
    Extract numeric values from SPONG service status data and update RRD files.

    Parameters
    ----------
    host      : str  — hostname
    service   : str  — service name (e.g. "ping", "disk", "diski", "cpu", "memory")
    summary   : str  — one-line status summary
    message   : str  — full status message body
    timestamp : int/float — Unix timestamp of the status reading
    """
    try:
        import time as _time
        if timestamp is None:
            timestamp = _time.time()
        rrd_dir = _rrd_dir(host)
        svc = service.lower()

        if svc == "ping":
            _update_ping(rrd_dir, host, summary or "", timestamp)
        elif svc == "disk":
            _update_disk(rrd_dir, host, message or "", timestamp)
        elif svc == "diski":
            _update_diski(rrd_dir, host, message or "", timestamp)

        elif svc == "cpu":
            _update_cpu(rrd_dir, host, summary or "", timestamp)
        elif svc == "memory":
            _update_memory(rrd_dir, host, summary or "", message or "", timestamp)
        elif svc == "processes":
            _update_processes(rrd_dir, host, summary or "", timestamp)
        elif svc == "sensors":
            _update_sensors(rrd_dir, host, message or "", timestamp)
        elif svc == "hddtemp":
            _update_hddtemp(rrd_dir, host, summary or "", timestamp)
        elif svc == "rcpu":
            _update_rcpu(rrd_dir, summary or "", timestamp, "rcpu.rrd")
        elif svc == "scpu":
            _update_rcpu(rrd_dir, summary or "", timestamp, "scpu.rrd")
        elif svc == "scpu1m":
            _update_rcpu(rrd_dir, summary or "", timestamp, "scpu1m.rrd")
        elif svc == "scpu5s":
            _update_rcpu(rrd_dir, summary or "", timestamp, "scpu5s.rrd")
        elif svc == "memolt":
            _update_rcpu(rrd_dir, summary or "", timestamp, "memolt.rrd")
        elif svc == "mem":
            _update_rcpu(rrd_dir, summary or "", timestamp, "mem.rrd")
        elif svc == "rtemp":
            _update_rtemp(rrd_dir, summary or "", timestamp)
        elif svc == "temp":
            _update_temp(rrd_dir, summary or "", message or "", timestamp)
        elif svc == "hum":
            _update_hum(rrd_dir, summary or "", message or "", timestamp)
        elif svc == "macs":
            _update_macs(rrd_dir, summary or "", timestamp)
        elif svc == "wassoc":
            _update_wassoc(rrd_dir, summary or "", timestamp)
        elif svc == "qmailq":
            _update_qmailq(rrd_dir, summary or "", message or "", timestamp)
        elif svc == "iftraffic":
            _update_iftraffic(rrd_dir, summary or "", message or "", timestamp)
        elif svc == "co2":
            _update_co2(rrd_dir, summary or "", timestamp)
        elif svc == "soil":
            _update_soil(rrd_dir, summary or "", timestamp)
        elif svc == "presence":
            _update_presence(rrd_dir, summary or "", timestamp)
        elif svc == "ups":
            _update_ups(rrd_dir, summary or "", timestamp)
        elif svc == "speedtest":
            _update_speedtest(rrd_dir, summary or "", timestamp)
        elif svc == "uptime":
            _update_uptime(rrd_dir, summary or "", timestamp)
        elif svc in _TCP_TIME_GRAPH_META:
            _update_tcp_time(rrd_dir, svc, summary or "", timestamp)
        else:
            log.debug("update_from_status: unrecognised service '%s' for host %s", service, host)
    except Exception as exc:
        log.error("update_from_status(%s, %s) failed: %s", host, service, exc)


# ---------------------------------------------------------------------------
# Legend helper
# ---------------------------------------------------------------------------

# Format strings by DEF variable name; fallback is "%6.2lf"
_LEGEND_FMT = {
    "avg":   "%7.4lf s",    # ping response time
    "pct":   "%5.1lf%%",    # disk / diski percentage
    "p":     "%5.1lf%%",    # memory percentage
    "la":    "%5.2lf",      # cpu load average
    "j":     "%6.0lf",      # jobs count
    "t":     "%7.4lf s",    # tcp response times (http/https/ssh/mysql/dns)
    "cpu":   "%5.1lf%%",    # rcpu / scpu
    "macs":  "%6.0lf",      # mac count
    "d":     "%5.1lf d",    # uptime days
    "total": "%6.0lf",      # processes total
    "v":     "%6.2lf",      # generic sensor
    "eco2":  "%6.0lf ppm",  # eCO2
    "tvoc":  "%6.0lf ppb",  # TVOC
    "aqi":   "%5.1lf",      # AQI index
}
# DS names whose values are temperatures (°C) — used for dynamic graphs
_TEMP_DS = {"board", "cpu", "pkg_0", "core_0", "core_1", "core_2", "core_3",
            "sda", "sdb", "sdc", "sdd"}


# DEF variable names that manage their own legends (skip in _append_legends)
_NO_AUTO_LEGEND = {"mn", "med", "mx", "loss", "spread", "q", "mid",
                   "l0", "l10", "l20", "l50", "lhi", "lossscaled"}


def _append_legends(cmd):
    """Scan cmd for DEF:var= entries and append VDEF+GPRINT legend lines."""
    for entry in list(cmd):
        m = re.match(r"DEF:(\w+)=", entry)
        if not m:
            continue
        var = m.group(1)
        if var in _NO_AUTO_LEGEND or re.match(r"p\d+$", var):
            continue
        if var in _TEMP_DS:
            fmt = "%5.1lf"
        else:
            fmt = _LEGEND_FMT.get(var, "%6.2lf")
        cmd += [
            f"VDEF:{var}max={var},MAXIMUM",
            f"VDEF:{var}min={var},MINIMUM",
            f"VDEF:{var}avg={var},AVERAGE",
            f"VDEF:{var}last={var},LAST",
            f"GPRINT:{var}max:  Max\\: {fmt}",
            f"GPRINT:{var}min:  Min\\: {fmt}",
            f"GPRINT:{var}avg:  Avg\\: {fmt}",
            f"GPRINT:{var}last:  Last\\: {fmt}\\n",
        ]


# ---------------------------------------------------------------------------
# CO2 stacked composite graph
# ---------------------------------------------------------------------------

def _graph_co2_stacked(rrd_path, host, start, width, height):
    """Generate 3 stacked sub-graphs for eCO2, TVOC and AQI using PIL."""
    from PIL import Image
    import io

    sub_h = max(50, height // 3)

    panels = [
        # (ds, label, unit, color, area, lo, hi)
        ("eco2", "eCO2",  "ppm",  "#0077cc", True,  300,  3000),
        ("tvoc", "TVOC",  "ppb",  "#009900", False,    0,  1000),
        ("aqi",  "AQI",   "",     "#ff6600", True,     0,     5),
    ]

    images = []
    for ds, label, unit, color, area, lo, hi in panels:
        vtitle = f"{unit}" if unit else label
        cmd = [
            "rrdtool", "graph", "-",
            "--start", start, "--end", "now",
            "--width", str(width), "--height", str(sub_h),
            "--vertical-label", vtitle,
            "--title", f"{label}  {host}",
            "--lower-limit", str(lo),
            "--upper-limit", str(hi),
            "--rigid",
            f"DEF:v={rrd_path}:{ds}:AVERAGE",
        ]
        cmd += GRAPH_COLORS
        if area:
            cmd += [f"AREA:v{color}:{label}"]
        else:
            cmd += [f"LINE2:v{color}:{label}"]
        fmt = "%6.0lf" if ds in ("eco2", "tvoc") else "%5.1lf"
        cmd += [
            f"VDEF:vmax=v,MAXIMUM",
            f"VDEF:vmin=v,MINIMUM",
            f"VDEF:vavg=v,AVERAGE",
            f"VDEF:vlast=v,LAST",
            f"GPRINT:vmax:  Max\\: {fmt}",
            f"GPRINT:vmin:  Min\\: {fmt}",
            f"GPRINT:vavg:  Avg\\: {fmt}",
            f"GPRINT:vlast:  Last\\: {fmt}\\n",
        ]
        result = _run(cmd)
        if result is None or result.returncode != 0:
            return None
        images.append(Image.open(io.BytesIO(result.stdout)))

    total_h = sum(img.height for img in images)
    combined = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for img in images:
        combined.paste(img, (0, y))
        y += img.height

    buf = io.BytesIO()
    combined.save(buf, format="PNG")
    return buf.getvalue()



def _graph_presence_stacked(rrd_path, host, start, width, height):
    """2 stacked panels: Iluminación (lux) y Distancia/Presencia (cm)."""
    from PIL import Image
    import io

    sub_h = max(60, height // 2)

    panels = [
        # (ds, label, unit, color, area, lo, hi)
        ("lux",  "Iluminación", "lux", "#f9a825", True,  0, None),
        ("dist", "Distancia",   "cm",  "#0077cc", False, 0, None),
    ]
    images = []
    for ds, label, unit, color, area, lo, hi in panels:
        cmd = [
            "rrdtool", "graph", "-",
            "--start", start, "--end", "now",
            "--width", str(width), "--height", str(sub_h),
            "--vertical-label", unit,
            "--title", f"{label}  {host}",
            "--lower-limit", str(lo),
        ]
        if hi is not None:
            cmd += ["--upper-limit", str(hi), "--rigid"]
        cmd += GRAPH_COLORS
        cmd += [f"DEF:v={rrd_path}:{ds}:AVERAGE"]
        cmd += [f"AREA:v{color}:{label}"] if area else [f"LINE2:v{color}:{label}"]
        cmd += [
            "VDEF:vmax=v,MAXIMUM", "VDEF:vmin=v,MINIMUM",
            "VDEF:vavg=v,AVERAGE", "VDEF:vlast=v,LAST",
            "GPRINT:vmax:  Max\\: %6.0lf", "GPRINT:vmin:  Min\\: %6.0lf",
            "GPRINT:vavg:  Avg\\: %6.0lf", "GPRINT:vlast:  Last\\: %6.0lf\\n",
        ]
        result = _run(cmd)
        if result is None or result.returncode != 0:
            return None
        images.append(Image.open(io.BytesIO(result.stdout)))

    total_h = sum(img.height for img in images)
    combined = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for img in images:
        combined.paste(img, (0, y))
        y += img.height
    buf = io.BytesIO()
    combined.save(buf, format="PNG")
    return buf.getvalue()


def _graph_soil(rrd_path, host, start, width, height):
    """Single panel with all soil moisture sensors as lines."""
    colors = ["#0077cc", "#009900", "#cc0000", "#ff8800",
              "#8e24aa", "#00aaaa", "#aa7700", "#555555"]
    ds_labels = [
        ("pasto_se", "PastoSE"), ("pasto_ne", "PastoNE"), ("pasto_no", "PastoNO"),
        ("cant_sur", "CantSur"), ("cant_ne",  "CantNE"),  ("cant_no",  "CantNO"),
        ("valv",     "Válvulas"),("lluvia",    "Lluvia"),
    ]
    cmd = [
        "rrdtool", "graph", "-",
        "--start", start, "--end", "now",
        "--width", str(width), "--height", str(height),
        "--vertical-label", "%",
        "--title", f"Suelo  {host}",
        "--lower-limit", "0", "--upper-limit", "100",
    ]
    cmd += GRAPH_COLORS
    defs, lines = [], []
    for i, (ds, label) in enumerate(ds_labels):
        color = colors[i % len(colors)]
        cmd += [f"DEF:{ds}={rrd_path}:{ds}:AVERAGE"]
        cmd += [f"LINE1:{ds}{color}:{label:<10}"]
        cmd += [
            f"VDEF:{ds}last={ds},LAST",
            f"GPRINT:{ds}last: %5.1lf%%\\n",
        ]
    result = _run(cmd)
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _graph_termica_stacked(rrd_path, host, start, width, height):
    """3 stacked panels: Potencia (W), Corriente (A), Tensión (V)."""
    from PIL import Image
    import io

    sub_h = max(50, height // 3)

    panels = [
        # (ds, label, unit, color, area, lo, hi)
        ("power",   "Potencia",  "W",   "#cc0000", True,  0,   None),
        ("current", "Corriente", "A",   "#0077cc", False, 0,   None),
        ("voltage", "Tensión",   "V",   "#009900", False, 180, 250),
    ]

    images = []
    for ds, label, unit, color, area, lo, hi in panels:
        cmd = [
            "rrdtool", "graph", "-",
            "--start", start, "--end", "now",
            "--width", str(width), "--height", str(sub_h),
            "--vertical-label", unit,
            "--title", f"{label}  {host}",
            "--lower-limit", str(lo),
        ]
        if hi is not None:
            cmd += ["--upper-limit", str(hi), "--rigid"]
        cmd += GRAPH_COLORS
        cmd += [f"DEF:v={rrd_path}:{ds}:AVERAGE"]
        if area:
            cmd += [f"AREA:v{color}:{label}"]
        else:
            cmd += [f"LINE2:v{color}:{label}"]
        fmt = "%6.1lf" if ds in ("power", "voltage") else "%7.3lf"
        cmd += [
            "VDEF:vmax=v,MAXIMUM",
            "VDEF:vmin=v,MINIMUM",
            "VDEF:vavg=v,AVERAGE",
            "VDEF:vlast=v,LAST",
            f"GPRINT:vmax:  Max\\: {fmt}",
            f"GPRINT:vmin:  Min\\: {fmt}",
            f"GPRINT:vavg:  Avg\\: {fmt}",
            f"GPRINT:vlast:  Last\\: {fmt}\\n",
        ]
        result = _run(cmd)
        if result is None or result.returncode != 0:
            return None
        images.append(Image.open(io.BytesIO(result.stdout)))

    total_h = sum(img.height for img in images)
    combined = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for img in images:
        combined.paste(img, (0, y))
        y += img.height

    buf = io.BytesIO()
    combined.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helper: response-time graph args with P50/P90/P95 percentiles via VDEF
# ---------------------------------------------------------------------------

def _tcp_time_graph_args(rrd_path, title, color):
    """Return rrdtool graph args for a TCP response-time RRD with percentile HRULEs."""
    return [
        "--vertical-label", "segundos",
        "--title", title,
        "DEF:t={}:time:AVERAGE".format(rrd_path),
        "VDEF:p50=t,50,PERCENTNAN",
        "VDEF:p90=t,90,PERCENTNAN",
        "VDEF:p95=t,95,PERCENTNAN",
        "LINE2:t{}:Resp    ".format(color),
        r"GPRINT:t:MAX:Máx\:%7.4lf s",
        r"GPRINT:t:MIN:  Mín\:%7.4lf s",
        r"GPRINT:t:AVERAGE:  Prom\:%7.4lf s",
        r"GPRINT:t:LAST:  Últ\:%7.4lf s\l",
        "HRULE:p50#00aa44:P50   ",
        r"GPRINT:p50:%7.4lf s\l",
        "HRULE:p90#ff9900:P90   ",
        r"GPRINT:p90:%7.4lf s\l",
        "HRULE:p95#cc0000:P95   ",
        r"GPRINT:p95:%7.4lf s\l",
    ]


# ---------------------------------------------------------------------------
# Speedtest: comparison graph (current week vs previous week vs prev month)
# ---------------------------------------------------------------------------

def _graph_speedtest_compare(rrd_path, host, width, height):
    """Three stacked panels (down/up/ping) overlaying current week,
    previous week (shifted +7d) and previous month (shifted +28d)."""
    import subprocess, io
    from PIL import Image

    W7  = 7  * 24 * 3600   # 7 days in seconds
    W28 = 28 * 24 * 3600

    panels = [
        {
            "title": f"Speedtest Bajada — {host}  (semana actual vs anterior vs hace 1 mes)",
            "vlabel": "Mbps",
            "ds": "down",
            "color_now":  "#1565c0",
            "color_prev": "#90caf9",
            "color_old":  "#cce0ff",
        },
        {
            "title": f"Speedtest Subida — {host}",
            "vlabel": "Mbps",
            "ds": "up",
            "color_now":  "#2e7d32",
            "color_prev": "#a5d6a7",
            "color_old":  "#cceecc",
        },
        {
            "title": f"Speedtest Ping — {host}",
            "vlabel": "ms",
            "ds": "ping",
            "color_now":  "#e65100",
            "color_prev": "#ffcc80",
            "color_old":  "#ffe0cc",
        },
    ]

    images = []
    panel_h = max(height // 3 - 10, 100)
    for p in panels:
        ds = p["ds"]
        cmd = [
            "rrdtool", "graph", "-",
            "--start", "now-7d", "--end", "now",
            "--width", str(width), "--height", str(panel_h),
            "--title", p["title"],
            "--vertical-label", p["vlabel"],
            "--color", "BACK#ffffff",
            "--color", "CANVAS#f5f5ff",
            "--slope-mode",
            # hace 1 mes (shifted +28d para alinear con eje)
            f"DEF:old={rrd_path}:{ds}:AVERAGE:start=now-35d:end=now-28d",
            "SHIFT:old:{}".format(W28),
            # semana anterior (shifted +7d)
            f"DEF:prev={rrd_path}:{ds}:AVERAGE:start=now-14d:end=now-7d",
            "SHIFT:prev:{}".format(W7),
            # semana actual
            f"DEF:now={rrd_path}:{ds}:AVERAGE",
            # dibujar de fondo a frente
            "LINE1:old{}80:Hace 1 mes  ".format(p["color_old"]),
            r"GPRINT:old:AVERAGE:Prom\:%6.1lf\l",
            "LINE1:prev{}:Sem. anterior  ".format(p["color_prev"]),
            r"GPRINT:prev:AVERAGE:Prom\:%6.1lf\l",
            "LINE2:now{}:Esta semana  ".format(p["color_now"]),
            r"GPRINT:now:AVERAGE:Prom\:%6.1lf\l",
        ]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            images.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))

    if not images:
        return None
    total_h = sum(im.height for im in images)
    out = Image.new("RGB", (images[0].width, total_h), (255, 255, 255))
    y = 0
    for im in images:
        out.paste(im, (0, y))
        y += im.height
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


_TEMP_RANGE_STEPS = {
    "24h": 3600,       # max/min de cada hora
    "7d": 86400,       # max/min de cada día
    "30d": 86400,      # max/min de cada día
    "1y": 2592000,     # max/min de cada mes aproximado de 30 días
}

_TEMP_RANGE_PERIOD_SECONDS = {
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
    "1y": 365 * 24 * 3600,
}

_TEMP_RANGE_FETCH_RESOLUTION = {
    "24h": 300,
    "7d": 3600,
    "30d": 3600,
    "1y": 86400,
}


def _rrd_has_rra_cf(rrd_path, cf):
    info = _run(["rrdtool", "info", rrd_path])
    if info is None or info.returncode != 0:
        return False
    pattern = rb'rra\[\d+\]\.cf\s*=\s*"' + cf.encode() + rb'"'
    return re.search(pattern, info.stdout) is not None


def _fetch_rrd_values(rrd_path, ds_name, start_ts, end_ts, resolution):
    result = _run([
        "rrdtool", "fetch", rrd_path, "AVERAGE",
        "--start", str(int(start_ts)),
        "--end", str(int(end_ts)),
        "--resolution", str(int(resolution)),
    ])
    if result is None or result.returncode != 0:
        return []

    rows = result.stdout.decode(errors="replace").splitlines()
    if not rows:
        return []

    ds_index = 0
    header_seen = False
    values = []
    for line in rows:
        line = line.strip()
        if not line:
            continue
        if not header_seen and not line[0].isdigit():
            columns = line.split()
            if ds_name in columns:
                ds_index = columns.index(ds_name)
            header_seen = True
            continue
        if ":" not in line:
            continue
        ts_text, value_text = line.split(":", 1)
        try:
            ts = int(ts_text)
        except ValueError:
            continue
        parts = value_text.split()
        if ds_index >= len(parts):
            continue
        try:
            value = float(parts[ds_index])
        except ValueError:
            continue
        if math.isfinite(value):
            values.append((ts, value))
    return values


def _bucket_start(ts, period, start_ts):
    ts = int(ts)
    if period == "24h":
        return ts - (ts % 3600)
    if period in ("7d", "30d"):
        return ts - (ts % 86400)
    step = _TEMP_RANGE_STEPS[period]
    return int(start_ts) + (((ts - int(start_ts)) // step) * step)


def _graph_temperature_range_from_fetch(rrd_path, ds_name, host, period, width, height):
    step = _TEMP_RANGE_STEPS.get(period)
    period_seconds = _TEMP_RANGE_PERIOD_SECONDS.get(period)
    resolution = _TEMP_RANGE_FETCH_RESOLUTION.get(period)
    if not step or not period_seconds or not resolution:
        return None

    end_ts = int(time.time())
    start_ts = end_ts - period_seconds
    buckets = {}
    for ts, value in _fetch_rrd_values(rrd_path, ds_name, start_ts, end_ts, resolution):
        bucket = _bucket_start(ts - 1, period, start_ts)
        current = buckets.get(bucket)
        if current is None:
            buckets[bucket] = [value, value]
        else:
            current[0] = min(current[0], value)
            current[1] = max(current[1], value)

    if not buckets:
        return None

    fd, tmp_rrd = tempfile.mkstemp(prefix="spong-temp-range-", suffix=".rrd")
    os.close(fd)
    try:
        bucket_rows = sorted(buckets.items())
        first_update = bucket_rows[0][0] + step
        create = [
            "rrdtool", "create", tmp_rrd,
            "--step", str(step),
            "--start", str(first_update - step - 1),
            "DS:mn:GAUGE:{}:-100:200".format(step * 2),
            "DS:mx:GAUGE:{}:-100:200".format(step * 2),
            "RRA:AVERAGE:0.5:1:{}".format(len(bucket_rows) + 2),
        ]
        created = _run(create)
        if created is None or created.returncode != 0:
            return None

        for bucket, (mn, mx) in bucket_rows:
            updated = _run([
                "rrdtool", "update", tmp_rrd,
                "{}:{:.6f}:{:.6f}".format(bucket + step, mn, mx),
            ])
            if updated is None or updated.returncode != 0:
                return None

        cmd = [
            "rrdtool", "graph", "-",
            "--start", str(start_ts), "--end", str(end_ts),
            "--width", str(width), "--height", str(height),
            "--vertical-label", "°C",
            "--title", "Temperatura {}".format(host),
        ]
        cmd += GRAPH_COLORS
        cmd += [
            "DEF:mn={}:mn:AVERAGE".format(tmp_rrd),
            "DEF:mx={}:mx:AVERAGE".format(tmp_rrd),
            "CDEF:range=mx,mn,-",
            "AREA:mn#ffffff00",
            "AREA:range#ff8c3340::STACK",
            "LINE2:mx#43a047:Máximo ",
            "VDEF:mxmax=mx,MAXIMUM",
            "VDEF:mxlast=mx,LAST",
            "GPRINT:mxmax:%5.1lf °C",
            "GPRINT:mxlast:  Últ\\:%5.1lf °C\\n",
            "LINE2:mn#ff8c33:Mínimo ",
            "VDEF:mnmin=mn,MINIMUM",
            "VDEF:mnlast=mn,LAST",
            "GPRINT:mnmin:%5.1lf °C",
            "GPRINT:mnlast:  Últ\\:%5.1lf °C\\n",
        ]
        result = _run(cmd)
        if result is None or result.returncode != 0:
            return None
        return result.stdout
    finally:
        try:
            os.unlink(tmp_rrd)
        except OSError:
            pass


def _temperature_range_graph_args(rrd_path, ds_name, host, period):
    step = _TEMP_RANGE_STEPS.get(period)
    if not step:
        return None

    has_extremes = _rrd_has_rra_cf(rrd_path, "MIN") and _rrd_has_rra_cf(rrd_path, "MAX")
    max_cf = "MAX" if has_extremes else "AVERAGE"
    min_cf = "MIN" if has_extremes else "AVERAGE"

    return [
        "--vertical-label", "°C",
        "--title", "Temperatura {}".format(host),
        "DEF:mx={}:{}:{}:step={}:reduce=MAX".format(rrd_path, ds_name, max_cf, step),
        "DEF:mn={}:{}:{}:step={}:reduce=MIN".format(rrd_path, ds_name, min_cf, step),
        "CDEF:range=mx,mn,-",
        "AREA:mn#ffffff00",
        "AREA:range#ff8c3340::STACK",
        "LINE2:mx#43a047:Máximo ",
        "VDEF:mxmax=mx,MAXIMUM",
        "VDEF:mxlast=mx,LAST",
        "GPRINT:mxmax:%5.1lf °C",
        "GPRINT:mxlast:  Últ\\:%5.1lf °C\\n",
        "LINE2:mn#ff8c33:Mínimo ",
        "VDEF:mnmin=mn,MINIMUM",
        "VDEF:mnlast=mn,LAST",
        "GPRINT:mnmin:%5.1lf °C",
        "GPRINT:mnlast:  Últ\\:%5.1lf °C\\n",
    ]


# ---------------------------------------------------------------------------
# Public API: graph_png
# ---------------------------------------------------------------------------

def graph_png(host, service, period="24h", width=500, height=150, mounts="filtered"):
    """
    Generate a PNG graph for a service and return it as bytes.

    Parameters
    ----------
    host    : str
    service : str  — "ping", "disk", "disk-{name}", "cpu"/"la", "memory"
    period  : str  — one of "1h", "24h", "7d", "30d", "1y"
    width   : int
    height  : int

    Returns
    -------
    bytes or None
    """
    try:
        rrd_dir = _rrd_dir(host)
        start = PERIOD_MAP.get(period, "-1d")
        svc = service.lower()

        cmd = ["rrdtool", "graph", "-",
               "--start", start, "--end", "now",
               "--width", str(width), "--height", str(height)]
        cmd += GRAPH_COLORS

        if svc == "ping":
            rrd_path = os.path.join(rrd_dir, "ping-times.rrd")
            if not _rrd_exists(rrd_path):
                log.debug("graph_png: %s does not exist", rrd_path)
                return None
            # Check if RRD has the new 4-DS schema (mn/avg/mx/loss)
            ds_count = _rrd_ds_count(rrd_path)
            cmd += [
                "--vertical-label", "segundos",
                "--title", "Ping  {}".format(host),
                "--lower-limit", "0",
                "--alt-autoscale-max",
            ]
            if ds_count >= 4:
                # Smokeping-style: shaded spread + median line + loss
                cmd += [
                    f"DEF:mn={rrd_path}:mn:AVERAGE",
                    f"DEF:med={rrd_path}:avg:AVERAGE",
                    f"DEF:mx={rrd_path}:mx:AVERAGE",
                    f"DEF:loss={rrd_path}:loss:AVERAGE",
                    # Smokeping-style graduated smoke bands (3 layers)
                    f"CDEF:spread=mx,mn,-",
                    f"CDEF:q=spread,4,/",
                    f"CDEF:mid=spread,2,/",
                    f"AREA:mn#ffffff:",
                    f"AREA:q#a8a8a8::STACK",
                    f"AREA:mid#606060::STACK",
                    f"AREA:q#a8a8a8::STACK",
                    # Median line colored by loss level (smokeping gradient)
                    # loss=0%: green  1-10%: yellow  11-20%: orange  21-50%: red-orange  >50%: red
                    f"CDEF:l0=loss,0,EQ,med,UNKN,IF",
                    f"CDEF:l10=loss,0,GT,loss,10,LE,*,med,UNKN,IF",
                    f"CDEF:l20=loss,10,GT,loss,20,LE,*,med,UNKN,IF",
                    f"CDEF:l50=loss,20,GT,loss,50,LE,*,med,UNKN,IF",
                    f"CDEF:lhi=loss,50,GT,med,UNKN,IF",
                    f"LINE2:l0#00cc00:",
                    f"LINE2:l10#ffcc00:",
                    f"LINE2:l20#ff8800:",
                    f"LINE2:l50#cc4400:",
                    f"LINE2:lhi#cc0000:",
                    # Stats legend
                    f"VDEF:vmed=med,AVERAGE",
                    f"VDEF:vmn=mn,MINIMUM",
                    f"VDEF:vmx=mx,MAXIMUM",
                    f"VDEF:vloss=loss,AVERAGE",
                    f"VDEF:vlastloss=loss,LAST",
                    f"GPRINT:vmed:Mediana\\: %7.4lf s",
                    f"GPRINT:vmn:  Min\\: %7.4lf s",
                    f"GPRINT:vmx:  Max\\: %7.4lf s\\n",
                    f"GPRINT:vloss:Pérdida\\: %4.1lf%%",
                    f"GPRINT:vlastloss:  Last\\: %4.1lf%%\\n",
                    # Loss color key (colored squares via zero-height lines)
                    f"COMMENT:Colores pérdida\\:",
                    f"LINE1:0#00cc00:  0%",
                    f"LINE1:0#ffcc00:  1-10%",
                    f"LINE1:0#ff8800:  11-20%",
                    f"LINE1:0#cc4400:  21-50%",
                    f"LINE1:0#cc0000:  >50%\\n",
                ]
            else:
                # Fallback for old 2-DS RRDs
                cmd += [
                    f"DEF:avg={rrd_path}:avg:AVERAGE",
                    f"LINE2:avg#00cc00:ms",
                ]

        elif svc.startswith("diski-"):
            name = svc[6:]
            rrd_path = os.path.join(rrd_dir, "diski-{}.rrd".format(name))
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "% inodos",
                "--upper-limit", "100",
                "--title", "Inodos {} {}".format(name, host),
                "DEF:pct={}:pct:AVERAGE".format(rrd_path),
                "AREA:pct#aa55ff:% inodos",
            ]

        elif svc == "diski":
            partitions = _partition_rrds(rrd_dir, "diski")
            current_mounts = set(_mountpoints_from_df_message(_current_service_message(host, "diski")))
            if current_mounts:
                partitions = [p for p in partitions if p[1] in current_mounts]
            if mounts != "full":
                partitions = [p for p in partitions if _graph_include_mountpoint(p[1])]
            if not partitions:
                return None
            colors = ["#7e57c2", "#3949ab", "#1e88e5", "#00897b", "#43a047", "#fdd835", "#fb8c00", "#e53935", "#8e24aa", "#6d4c41"]
            label_width = 12 if width < 900 else 18
            cmd += [
                "--vertical-label", "% inodos",
                "--upper-limit", "100",
                "--title", "Inodos por particion {}".format(host),
            ]
            for idx, (_rrd_name, mountpoint, rrd_path) in enumerate(partitions):
                ds = "p{}".format(idx)
                color = colors[idx % len(colors)]
                label = _graph_mountpoint_label(mountpoint)[:label_width]
                cmd += [
                    "DEF:{}={}:pct:AVERAGE".format(ds, rrd_path),
                    "LINE2:{}{}:{}".format(ds, color, label),
                ]

        elif svc.startswith("disk-"):
            name = svc[5:]  # strip "disk-"
            rrd_path = os.path.join(rrd_dir, "disk-{}.rrd".format(name))
            if not _rrd_exists(rrd_path):
                log.debug("graph_png: %s does not exist", rrd_path)
                return None
            cmd += [
                "--vertical-label", "%",
                "--upper-limit", "100",
                "--title", "Disco {} {}".format(name, host),
                "DEF:pct={}:pct:AVERAGE".format(rrd_path),
                "AREA:pct#ffaa00:% usado",
            ]

        elif svc == "disk":
            partitions = _partition_rrds(rrd_dir, "disk")
            current_mounts = set(_mountpoints_from_df_message(_current_service_message(host, "disk")))
            if current_mounts:
                partitions = [p for p in partitions if p[1] in current_mounts]
            if mounts != "full":
                partitions = [p for p in partitions if _graph_include_mountpoint(p[1])]
            if not partitions:
                log.debug("graph_png: no disk RRDs found for %s", host)
                return None
            colors = ["#ff9800", "#43a047", "#1e88e5", "#8e24aa", "#e53935", "#00897b", "#6d4c41", "#3949ab", "#fb8c00", "#7cb342"]
            label_width = 12 if width < 900 else 18
            cmd += [
                "--vertical-label", "% usado",
                "--upper-limit", "100",
                "--title", "Disco por particion {}".format(host),
            ]
            for idx, (_rrd_name, mountpoint, rrd_path) in enumerate(partitions):
                ds = "p{}".format(idx)
                color = colors[idx % len(colors)]
                label = _graph_mountpoint_label(mountpoint)[:label_width]
                cmd += [
                    "DEF:{}={}:pct:AVERAGE".format(ds, rrd_path),
                    "LINE2:{}{}:{}".format(ds, color, label),
                ]

        elif svc == "memory":
            rrd_path = os.path.join(rrd_dir, "mem.rrd")
            if not _rrd_exists(rrd_path):
                log.debug("graph_png: %s does not exist", rrd_path)
                return None
            # Detect DS name (existing files may use physpctused instead of phys_pct)
            info = _run(["rrdtool", "info", rrd_path])
            ds_name = "phys_pct"
            if info and b"physpctused" in info.stdout:
                ds_name = "physpctused"
            cmd += [
                "--vertical-label", "%",
                "--upper-limit", "100",
                "--title", "Memoria {}".format(host),
                "DEF:p={}:{}:AVERAGE".format(rrd_path, ds_name),
                "LINE2:p#cc0000:física %",
            ]

        elif svc in ("cpu", "la", "jobs"):
            rrd_path = os.path.join(rrd_dir, "la.rrd")
            if not _rrd_exists(rrd_path):
                return None
            if svc == "jobs":
                cmd += [
                    "--vertical-label", "procesos",
                    "--title", "Jobs {}".format(host),
                    "DEF:j={}:jobs:AVERAGE".format(rrd_path),
                    "LINE2:j#cc6600:jobs",
                ]
            elif svc == "la":
                cmd += [
                    "--vertical-label", "load",
                    "--title", "Load {}".format(host),
                    "DEF:la={}:loadavg:AVERAGE".format(rrd_path),
                    "LINE2:la#0000cc:load avg",
                ]
            else:
                cmd += [
                    "--vertical-label", "load",
                    "--title", "CPU {}".format(host),
                    "DEF:la={}:loadavg:AVERAGE".format(rrd_path),
                    "DEF:j={}:jobs:AVERAGE".format(rrd_path),
                    "LINE2:la#0000cc:load avg",
                    "LINE1:j#cc6600:jobs",
                ]

        elif svc == "processes":
            rrd_path = os.path.join(rrd_dir, "processes.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "procesos",
                "--title", "Procesos {}".format(host),
                "DEF:t={}:total:AVERAGE".format(rrd_path),
                "LINE2:t#009900:total",
            ]

        elif svc == "sensors":
            rrd_path = os.path.join(rrd_dir, "sensors.rrd")
            if not _rrd_exists(rrd_path):
                return None
            info = _run(["rrdtool", "info", rrd_path])
            if not info:
                return None
            ds_names = re.findall(rb"ds\[(\w+)\]\.index\s*=\s*(\d+)", info.stdout)
            ds_names = [n.decode() for n, _ in sorted(ds_names, key=lambda x: int(x[1]))]
            colors = ["#cc0000", "#0000cc", "#009900", "#cc6600", "#990099", "#00aaaa"]
            cmd += ["--vertical-label", "°C", "--title", "Sensores {}".format(host)]
            for i, ds in enumerate(ds_names):
                color = colors[i % len(colors)]
                cmd += ["DEF:{0}={1}:{0}:AVERAGE".format(ds, rrd_path),
                        "LINE2:{0}{1}:{0}".format(ds, color)]

        elif svc in _TCP_TIME_GRAPH_META:
            rrd_path = os.path.join(rrd_dir, "{}-time.rrd".format(svc))
            if not _rrd_exists(rrd_path):
                return None
            label, color = _TCP_TIME_GRAPH_META[svc]
            cmd += _tcp_time_graph_args(rrd_path, "{} {}".format(label, host), color)

        elif svc == "hddtemp":
            rrd_path = os.path.join(rrd_dir, "hddtemp.rrd")
            if not _rrd_exists(rrd_path):
                return None
            info = _run(["rrdtool", "info", rrd_path])
            if not info:
                return None
            ds_names = re.findall(rb"ds\[(\w+)\]\.index\s*=\s*(\d+)", info.stdout)
            ds_names = [n.decode() for n, _ in sorted(ds_names, key=lambda x: int(x[1]))]
            colors = ["#cc0000", "#0000cc", "#009900", "#cc6600"]
            cmd += ["--vertical-label", "°C", "--title", "Temp. Disco {}".format(host)]
            for i, ds in enumerate(ds_names):
                color = colors[i % len(colors)]
                cmd += ["DEF:{0}={1}:{0}:AVERAGE".format(ds, rrd_path),
                        "LINE2:{0}{1}:{0}".format(ds, color)]

        elif svc in ("rcpu", "scpu", "scpu1m", "scpu5s", "memolt", "mem"):
            rrd_path = os.path.join(rrd_dir, "{}.rrd".format(svc))
            if not _rrd_exists(rrd_path):
                return None
            label = {"rcpu": "CPU router", "scpu": "CPU switch (5s)", "scpu1m": "CPU switch (1m)",
                     "scpu5s": "CPU switch (5s)", "memolt": "Memoria SNMP", "mem": "Memoria SNMP"}.get(svc, svc)
            color = {"rcpu": "#0077cc", "scpu": "#0077cc", "scpu1m": "#0077cc",
                     "scpu5s": "#0077cc", "memolt": "#6a1b9a", "mem": "#6a1b9a"}.get(svc, "#0077cc")
            cmd += [
                "--vertical-label", "%",
                "--upper-limit", "100",
                "--title", "{} {}".format(label, host),
                "DEF:cpu={}:cpu:AVERAGE".format(rrd_path),
                "AREA:cpu{}:% ".format(color),
            ]

        elif svc == "rtemp":
            rrd_path = os.path.join(rrd_dir, "rtemp.rrd")
            if not _rrd_exists(rrd_path):
                return None
            info = _run(["rrdtool", "info", rrd_path])
            if not info:
                return None
            ds_names = re.findall(rb"ds\[(\w+)\]\.index\s*=\s*(\d+)", info.stdout)
            ds_names = [n.decode() for n, _ in sorted(ds_names, key=lambda x: int(x[1]))]
            colors = {"board": "#ff6600", "cpu": "#cc0000"}
            cmd += ["--vertical-label", "°C", "--title", "Temp remota {}".format(host)]
            for ds in ds_names:
                color = colors.get(ds, "#009900")
                cmd += ["DEF:{0}={1}:{0}:AVERAGE".format(ds, rrd_path),
                        "LINE2:{0}{1}:{0}".format(ds, color)]

        elif svc == "temp":
            rrd_path = os.path.join(rrd_dir, "temp.rrd")
            if not _rrd_exists(rrd_path):
                return None
            if period in _TEMP_RANGE_STEPS:
                range_png = _graph_temperature_range_from_fetch(rrd_path, "temp", host, period, width, height)
                if range_png:
                    return range_png
            range_args = _temperature_range_graph_args(rrd_path, "temp", host, period)
            if range_args:
                cmd += range_args
            else:
                cmd += [
                    "--vertical-label", "°C",
                    "--title", "Temperatura {}".format(host),
                    "DEF:temp={}:temp:AVERAGE".format(rrd_path),
                    "LINE2:temp#ff6600:Temperatura",
                ]

        elif svc == "hum":
            rrd_path = os.path.join(rrd_dir, "hum.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "%",
                "--lower-limit", "0",
                "--upper-limit", "100",
                "--title", "Humedad {}".format(host),
                "DEF:hum={}:hum:AVERAGE".format(rrd_path),
                "AREA:hum#4fc3f7:Humedad",
            ]

        elif svc == "macs":
            rrd_path = os.path.join(rrd_dir, "macs.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "MACs",
                "--title", "MACs aprendidas {}".format(host),
                "DEF:macs={}:macs:AVERAGE".format(rrd_path),
                "AREA:macs#43a047:MACs",
            ]

        elif svc == "wassoc":
            rrd_path = os.path.join(rrd_dir, "wassoc.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "Clientes",
                "--title", "WiFi asociados {}".format(host),
                "DEF:assoc={}:assoc:AVERAGE".format(rrd_path),
                "AREA:assoc#0288d1:Asociados",
            ]

        elif svc == "qmailq":
            rrd_path = os.path.join(rrd_dir, "qmailq.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "mensajes",
                "--lower-limit", "0",
                "--title", "Qmail Queue {}".format(host),
                "DEF:local={}:local:AVERAGE".format(rrd_path),
                "DEF:remote={}:remote:AVERAGE".format(rrd_path),
                "LINE2:local#ef6c00:Local",
                "LINE2:remote#1565c0:Remote",
            ]

        elif svc == "iftraffic":
            rrd_path = os.path.join(rrd_dir, "iftraffic.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_iftraffic_stacked(rrd_path, host, start, width, height)

        elif svc in ("uptime", "ruptime"):
            rrd_path = os.path.join(rrd_dir, "uptime.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "días",
                "--title", "Uptime {}".format(host),
                "DEF:d={}:days:AVERAGE".format(rrd_path),
                "AREA:d#43a047:días",
            ]

        elif svc == "co2":
            rrd_path = os.path.join(rrd_dir, "co2.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_co2_stacked(rrd_path, host, start, width, height)

        elif svc == "soil":
            rrd_path = os.path.join(rrd_dir, "soil.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_soil(rrd_path, host, start, width, height)

        elif svc == "presence":
            rrd_path = os.path.join(rrd_dir, "presence.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_presence_stacked(rrd_path, host, start, width, height)

        elif svc == "speedtest":
            rrd_path = os.path.join(rrd_dir, "speedtest.rrd")
            if not _rrd_exists(rrd_path):
                return None
            if period == "compare":
                return _graph_speedtest_compare(rrd_path, host, width, height)
            return _graph_speedtest_stacked(rrd_path, host, start, width, height)

        elif svc == "ups":
            rrd_path = os.path.join(rrd_dir, "ups.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_ups_stacked(rrd_path, host, start, width, height)

        elif svc == "termica":
            rrd_path = os.path.join(rrd_dir, "termica.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_termica_stacked(rrd_path, host, start, width, height)

        elif svc in _SENSOR_META:
            ds_name, label, unit, mn, mx, color, area = _SENSOR_META[svc]
            rrd_path = os.path.join(rrd_dir, f"{svc}.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", unit,
                "--title", f"{label} {host}",
                f"DEF:v={rrd_path}:{ds_name}:AVERAGE",
            ]
            if area:
                cmd += [f"AREA:v{color}:{unit}"]
            else:
                cmd += [f"LINE2:v{color}:{unit}"]

        else:
            log.debug("graph_png: unrecognised service '%s' for host %s", service, host)
            return None

        _append_legends(cmd)
        result = _run(cmd)
        if result is None or result.returncode != 0:
            return None
        return result.stdout

    except Exception as exc:
        log.error("graph_png(%s, %s) failed: %s", host, service, exc)
        return None
