"""Network check: Switch CPU load via SNMP (TP-Link JetStream)."""

from ... import config
from .snmp import snmp_get_int

# TP-Link JetStream CPU utilization
_OID_TPLINK_CPU = [1, 3, 6, 1, 4, 1, 11863, 6, 4, 1, 1, 1, 1, 2, 1]
# Fallback: standard hrProcessorLoad
_OID_HR_CPU     = [1, 3, 6, 1, 2, 1, 25, 3, 3, 1, 2, 1]

_WARN = 70
_CRIT = 90


def check_scpu(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    cpu = snmp_get_int(host, community, _OID_TPLINK_CPU)
    if cpu is None:
        cpu = snmp_get_int(host, community, _OID_HR_CPU)

    if cpu is None:
        return "red", f"scpu: no response from {hostname}", ""

    if cpu >= _CRIT:
        color = "red"
    elif cpu >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, f"scpu: {cpu}% cpu load", f"Switch CPU load: {cpu}%"
