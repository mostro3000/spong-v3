"""Network check: Switch CPU load (5-second average) via SNMP."""

from ... import config
from .snmp import snmp_get_int, snmp_get_str

# Cisco SG500/SG550
_OID_CISCO_SG_CPU_5S = [1, 3, 6, 1, 4, 1, 9, 6, 1, 101, 1, 7, 0]
# TP-Link JetStream
_OID_TPLINK_CPU      = [1, 3, 6, 1, 4, 1, 11863, 6, 4, 1, 1, 1, 1, 2, 1]
_OID_SYSDESCR        = [1, 3, 6, 1, 2, 1, 1, 1, 0]

_WARN = 70
_CRIT = 90


def check_scpu5s(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    cpu = snmp_get_int(host, community, _OID_CISCO_SG_CPU_5S)
    if cpu is None:
        cpu = snmp_get_int(host, community, _OID_TPLINK_CPU)

    if cpu is None:
        descr = snmp_get_str(host, community, _OID_SYSDESCR) or ""
        if "SwOS" in descr:
            return "clear", "scpu5s: N/A (SwOS no soporta CPU via SNMP)", descr
        return "red", f"scpu5s: no response from {hostname}", ""

    if cpu >= _CRIT:
        color = "red"
    elif cpu >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, f"scpu5s: {cpu}% cpu (5s)", f"Switch CPU load (5s): {cpu}%"
