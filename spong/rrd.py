"""
rrd.py — Update existing RRD files from SPONG service status data and generate graph images.
"""

import logging
import os
import re
import subprocess

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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rrd_dir(host):
    """Return (and create if necessary) the RRD directory for a host."""
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
    """Return the number of DS entries in an existing RRD file, or 0 on error."""
    result = _run(["rrdtool", "info", path])
    if result is None or result.returncode != 0:
        return 0
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


# ---------------------------------------------------------------------------
# RRA definitions shared across all services
# ---------------------------------------------------------------------------

_RRA_DEFS = [
    "RRA:AVERAGE:0.5:1:1440",
    "RRA:AVERAGE:0.5:12:2160",
    "RRA:AVERAGE:0.5:288:720",
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

    # Migrate old RRDs that lack the loss DS (had 2 or 3 DS: min/avg[/max])
    ds_count = _rrd_ds_count(path)
    if ds_count < 4:
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


def _update_macs(rrd_dir, summary, timestamp):
    m = re.search(r"(\d+)\s+MACs", summary)
    if not m:
        return
    count = int(m.group(1))
    path = os.path.join(rrd_dir, "macs.rrd")
    if not _rrd_exists(path):
        _create_rrd(path, 300, ["DS:macs:GAUGE:600:0:U"], _RRA_DEFS, timestamp)
    _update_rrd(path, timestamp, [count])


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
    # summary: "disk temps: sdb:39°C, sdc:42°C"
    temps = {}
    for m in re.finditer(r"(\w+):(\d+)°C", summary):
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
        elif svc in ("cpu", "la"):
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
        elif svc == "rtemp":
            _update_rtemp(rrd_dir, summary or "", timestamp)
        elif svc == "macs":
            _update_macs(rrd_dir, summary or "", timestamp)
        elif svc == "co2":
            _update_co2(rrd_dir, summary or "", timestamp)
        elif svc in _SENSOR_META:
            _update_sensor(rrd_dir, svc, summary or "", timestamp)
        elif svc == "uptime":
            _update_uptime(rrd_dir, summary or "", timestamp)
        elif svc == "dns":
            _update_tcp_time(rrd_dir, "dns", summary or "", timestamp)
        elif svc == "mysql":
            _update_tcp_time(rrd_dir, "mysql", summary or "", timestamp)
        elif svc == "https":
            _update_tcp_time(rrd_dir, "https", summary or "", timestamp)
        elif svc == "http":
            _update_tcp_time(rrd_dir, "http", summary or "", timestamp)
        elif svc == "ssh":
            _update_tcp_time(rrd_dir, "ssh", summary or "", timestamp)
        elif svc in ("telnet", "ftp", "smtp", "imap", "ntp"):
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
        if var in _NO_AUTO_LEGEND:
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


# ---------------------------------------------------------------------------
# Public API: graph_png
# ---------------------------------------------------------------------------

def graph_png(host, service, period="24h", width=500, height=150):
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
            rrd_path = os.path.join(rrd_dir, "diski-root.rrd")
            if not _rrd_exists(rrd_path):
                candidates = [f for f in os.listdir(rrd_dir)
                              if f.startswith("diski-") and f.endswith(".rrd")]
                if not candidates:
                    return None
                rrd_path = os.path.join(rrd_dir, sorted(candidates)[0])
            name = os.path.basename(rrd_path)[6:-4]
            cmd += [
                "--vertical-label", "% inodos",
                "--upper-limit", "100",
                "--title", "Inodos {} {}".format(name, host),
                "DEF:pct={}:pct:AVERAGE".format(rrd_path),
                "AREA:pct#aa55ff:% inodos",
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
            # Generic disk — graph root if available, else first disk found
            rrd_path = os.path.join(rrd_dir, "disk-root.rrd")
            if not _rrd_exists(rrd_path):
                # Try to find any disk rrd
                candidates = [
                    f for f in os.listdir(rrd_dir)
                    if f.startswith("disk-") and f.endswith(".rrd")
                ]
                if not candidates:
                    log.debug("graph_png: no disk RRDs found for %s", host)
                    return None
                rrd_path = os.path.join(rrd_dir, sorted(candidates)[0])
            name = os.path.basename(rrd_path)[5:-4]  # strip "disk-" and ".rrd"
            cmd += [
                "--vertical-label", "%",
                "--upper-limit", "100",
                "--title", "Disco {} {}".format(name, host),
                "DEF:pct={}:pct:AVERAGE".format(rrd_path),
                "AREA:pct#ffaa00:% usado",
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

        elif svc == "ssh":
            rrd_path = os.path.join(rrd_dir, "ssh-time.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "segundos",
                "--title", "SSH {}".format(host),
                "DEF:t={}:time:AVERAGE".format(rrd_path),
                "LINE2:t#8e24aa:resp time",
            ]

        elif svc in ("telnet", "ftp", "smtp", "imap", "ntp"):
            rrd_path = os.path.join(rrd_dir, "{}-time.rrd".format(svc))
            if not _rrd_exists(rrd_path):
                return None
            _SVC_COLORS = {"telnet": "#e65100", "ftp": "#0277bd", "smtp": "#2e7d32",
                           "imap": "#6a1b9a", "ntp": "#00695c"}
            color = _SVC_COLORS.get(svc, "#555555")
            cmd += [
                "--vertical-label", "segundos",
                "--title", "{} {}".format(svc.upper(), host),
                "DEF:t={}:time:AVERAGE".format(rrd_path),
                "LINE2:t{}:resp time".format(color),
            ]

        elif svc == "http":
            rrd_path = os.path.join(rrd_dir, "http-time.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "segundos",
                "--title", "HTTP {}".format(host),
                "DEF:t={}:time:AVERAGE".format(rrd_path),
                "LINE2:t#0077cc:resp time",
            ]

        elif svc == "https":
            rrd_path = os.path.join(rrd_dir, "https-time.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "segundos",
                "--title", "HTTPS {}".format(host),
                "DEF:t={}:time:AVERAGE".format(rrd_path),
                "LINE2:t#00aa44:resp time",
            ]

        elif svc == "mysql":
            rrd_path = os.path.join(rrd_dir, "mysql-time.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "segundos",
                "--title", "MySQL {}".format(host),
                "DEF:t={}:time:AVERAGE".format(rrd_path),
                "LINE2:t#0077cc:resp time",
            ]

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

        elif svc in ("rcpu", "scpu"):
            rrd_path = os.path.join(rrd_dir, "{}.rrd".format(svc))
            if not _rrd_exists(rrd_path):
                return None
            label = "CPU router" if svc == "rcpu" else "CPU switch"
            cmd += [
                "--vertical-label", "%",
                "--upper-limit", "100",
                "--title", "{} {}".format(label, host),
                "DEF:cpu={}:cpu:AVERAGE".format(rrd_path),
                "AREA:cpu#0077cc:cpu %",
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

        elif svc == "uptime":
            rrd_path = os.path.join(rrd_dir, "uptime.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "días",
                "--title", "Uptime {}".format(host),
                "DEF:d={}:days:AVERAGE".format(rrd_path),
                "AREA:d#43a047:días",
            ]

        elif svc == "dns":
            rrd_path = os.path.join(rrd_dir, "dns-time.rrd")
            if not _rrd_exists(rrd_path):
                return None
            cmd += [
                "--vertical-label", "segundos",
                "--title", "DNS {}".format(host),
                "DEF:t={}:time:AVERAGE".format(rrd_path),
                "LINE2:t#ff6600:resp time",
            ]

        elif svc == "co2":
            rrd_path = os.path.join(rrd_dir, "co2.rrd")
            if not _rrd_exists(rrd_path):
                return None
            return _graph_co2_stacked(rrd_path, host, start, width, height)

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
