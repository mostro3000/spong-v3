"""Network check: MAC address table size via SNMP walk (dot1dTpFdbTable)."""

from ... import config
from .snmp import snmp_walk_count

# dot1dTpFdbAddress — bridge forwarding table (standard, works on MikroTik and switches)
_OID_FDB = [1, 3, 6, 1, 2, 1, 17, 4, 3, 1, 1]


def check_macs(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    count = snmp_walk_count(host, community, _OID_FDB)

    if count is None:
        return "red", f"macs: no SNMP response from {hostname}", ""

    return "green", f"macs: {count} MACs learned", f"Bridge forwarding table: {count} entries"
