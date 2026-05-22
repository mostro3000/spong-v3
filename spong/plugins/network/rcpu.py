"""Network check: Remote CPU load via SNMP (hrProcessorLoad)."""

from ... import config
from .snmp import snmp_get_int

# hrProcessorLoad.1 — standard HOST-RESOURCES-MIB, works on MikroTik and most devices
_OID_HR_CPU = [1, 3, 6, 1, 2, 1, 25, 3, 3, 1, 2, 1]
# MikroTik-specific fallback: mtxrSystemCpuLoad
_OID_MTK_CPU = [1, 3, 6, 1, 4, 1, 14988, 1, 1, 3, 14, 0]

_WARN  = 70   # yellow
_CRIT  = 90   # red


def check_rcpu(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    cpu = snmp_get_int(host, community, _OID_HR_CPU)
    if cpu is None:
        cpu = snmp_get_int(host, community, _OID_MTK_CPU)

    if cpu is None:
        return "purple", f"rcpu: no SNMP response from {hostname}", ""

    if cpu >= _CRIT:
        color = "red"
    elif cpu >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, f"rcpu: {cpu}% cpu load", f"CPU load: {cpu}%"
