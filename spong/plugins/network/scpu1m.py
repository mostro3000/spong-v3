"""Network check: Switch CPU load (1-minute average) via SNMP."""

from ... import config
from .snmp import snmp_get_int, snmp_get_str

# Cisco SG500/SG550
_OID_CISCO_SG_CPU_1M = [1, 3, 6, 1, 4, 1, 9, 6, 1, 101, 1, 8, 0]
# TP-Link JetStream (same OID, different interval not available)
_OID_TPLINK_CPU      = [1, 3, 6, 1, 4, 1, 11863, 6, 4, 1, 1, 1, 1, 2, 1]
_OID_SYSDESCR        = [1, 3, 6, 1, 2, 1, 1, 1, 0]

_WARN = 70
_CRIT = 90


def check_scpu1m(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    cpu = snmp_get_int(host, community, _OID_CISCO_SG_CPU_1M)
    if cpu is None:
        cpu = snmp_get_int(host, community, _OID_TPLINK_CPU)

    if cpu is None:
        descr = snmp_get_str(host, community, _OID_SYSDESCR) or ""
        if "SwOS" in descr:
            return "clear", "scpu1m: N/A (SwOS no soporta CPU via SNMP)", descr
        return "red", f"scpu1m: no response from {hostname}", ""

    if cpu >= _CRIT:
        color = "red"
    elif cpu >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, f"scpu1m: {cpu}% cpu (1m)", f"Switch CPU load (1min): {cpu}%"
