"""Network check: HTTP proxy (Squid/similar) on port 3128."""

from ... import config
from ._tcp_check import check_simple


def check_proxy(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 3128, "GET / HTTP/1.1\r\n\r\n", r"HTTP/1\.[01] [0-9]", "proxy")
