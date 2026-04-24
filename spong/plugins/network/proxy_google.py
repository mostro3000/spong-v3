"""Network check: HTTP proxy reachability to Google generate_204."""

from ... import config
from ._tcp_check import check_simple

_REQUEST = (
    "GET http://www.google.com/generate_204 HTTP/1.1\r\n"
    "Host: www.google.com\r\n"
    "User-Agent: spong-proxy-google/1.0\r\n"
    "Connection: close\r\n\r\n"
)


def check_proxy_google(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 3128, _REQUEST, r"^HTTP/1\.[01] 204\b", "proxy_google")
