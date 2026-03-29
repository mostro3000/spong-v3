"""Network check: memoria % en switches/routers TP-Link via SNMP.

Usa la TPLINK-SYSMONITOR-MIB (OID verificado en T2600G y modelos compatibles).

OID: 1.3.6.1.4.1.11863.6.4.1.2.1.1.2.1  (tpSysMonitorMemoryUtilization.1)
"""

from .snmp import snmp_get_int
from ... import config

_OID_TPLINK_MEM = [1, 3, 6, 1, 4, 1, 11863, 6, 4, 1, 2, 1, 1, 2, 1]

_WARN = 80   # yellow
_CRIT = 90   # red


def check_memolt(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    mem = snmp_get_int(host, community, _OID_TPLINK_MEM)

    if mem is None:
        return "red", "memolt: sin respuesta SNMP", f"No se pudo leer memoria de {host}"

    if mem >= _CRIT:
        color = "red"
    elif mem >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, f"memolt: {mem}%", f"Uso de memoria: {mem}%"
