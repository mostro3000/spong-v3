"""Network check: POP3."""

from ... import config
from ._tcp_check import check_simple


def check_pop(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 110, "", r"^\+OK", "pop")
