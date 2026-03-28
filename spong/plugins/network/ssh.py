"""Network check: SSH."""

from ... import config
from ._tcp_check import check_simple


def check_ssh(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 22, "", r"SSH", "ssh")
