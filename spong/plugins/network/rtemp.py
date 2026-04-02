"""Network check: Remote temperature via SNMP (MikroTik health MIB)."""

import time
from ... import config
from .snmp import snmp_get_int

# MikroTik RouterOS health MIB — values in tenths of °C (e.g. 370 = 37.0°C)
_OID_BOARD = [1, 3, 6, 1, 4, 1, 14988, 1, 1, 3, 10, 0]  # mtxrHlTemperature (RouterOS)
_OID_CPU   = [1, 3, 6, 1, 4, 1, 14988, 1, 1, 3, 14, 0]  # mtxrHlCpuTemperature (RouterOS)
# MikroTik SwOS (CSS series) — temperature at a different OID
_OID_SWOS  = [1, 3, 6, 1, 4, 1, 14988, 1, 1, 3, 11, 0]  # mtxrHlVoltage slot used for temp in SwOS

_WARN = 70   # yellow  (°C)
_CRIT = 85   # red     (°C)

# Some MikroTik models (e.g. RBcAPGi) cycle through multiple internal sensors
# in round-robin on OID .14, returning different values on successive queries.
# We sample the OID 3 times and take the minimum to avoid false alerts.
_SAMPLES = 3


def _snmp_get_min(host, community, oid, samples=_SAMPLES):
    """Query an SNMP OID multiple times and return the minimum value (or None)."""
    values = []
    for _ in range(samples):
        v = snmp_get_int(host, community, oid)
        if v is not None:
            values.append(v)
        time.sleep(0.1)
    return min(values) if values else None


def check_rtemp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    raw_board = snmp_get_int(host, community, _OID_BOARD)
    raw_cpu   = _snmp_get_min(host, community, _OID_CPU)

    # SwOS fallback: if neither RouterOS OID responds, try SwOS OID
    if raw_board is None and raw_cpu is None:
        raw_board = snmp_get_int(host, community, _OID_SWOS)

    if raw_board is None and raw_cpu is None:
        return "red", f"rtemp: no SNMP response from {hostname}", ""

    parts = []
    max_temp = 0
    if raw_board is not None:
        t = raw_board / 10.0
        parts.append(f"board {t:.1f}°C")
        max_temp = max(max_temp, t)
    if raw_cpu is not None:
        t = raw_cpu / 10.0
        parts.append(f"cpu {t:.1f}°C")
        max_temp = max(max_temp, t)

    summary = "rtemp: " + ", ".join(parts)
    message = summary

    if max_temp >= _CRIT:
        color = "red"
    elif max_temp >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
