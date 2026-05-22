"""Network check: Switch uptime via SNMP sysUpTime.

Reads sysUpTime (1.3.6.1.2.1.1.3.0) — value in hundredths of seconds (TimeTicks).
Returns yellow if the switch has been up less than 1 day (recently rebooted).
"""

from ... import config
from .snmp import snmp_get_int

# sysUpTime — hundredths of a second since last reboot
_OID_SYSUPTIME = [1, 3, 6, 1, 2, 1, 1, 3, 0]

_WARN_DAYS = 1  # yellow if up less than this


def check_suptime(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    ticks = snmp_get_int(host, community, _OID_SYSUPTIME)
    if ticks is None:
        return "purple", f"suptime: no SNMP response from {hostname}", ""

    total_seconds = ticks / 100
    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)

    if days > 0:
        up_str = f"{days}d {hours:02d}:{minutes:02d}"
    else:
        up_str = f"{hours:02d}:{minutes:02d}"

    summary = f"up {up_str}"
    message = f"sysUpTime: {ticks} ticks = {summary}"

    if days < _WARN_DAYS:
        return "yellow", f"suptime: {summary} (reinicio reciente)", message

    return "green", f"suptime: {summary}", message
