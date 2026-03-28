"""Network check: Temperature sensor (generic HTTP/MQTT)."""

from ... import config
from ._tcp_check import check_tcp


def check_temp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    # Generic: try to connect on port 80
    err, message = check_tcp(host, 80, "GET /temp HTTP/1.0\r\n\r\n", timeout=5)
    if err:
        return "green", f"temp: no data (sensor may be offline)", ""
    return "green", f"temp: ok", message[:200]
