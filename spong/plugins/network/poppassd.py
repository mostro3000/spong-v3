"""Network check: poppassd (cambio de contraseña POP) en puerto 106."""

from ... import config
from ._tcp_check import check_simple


def check_poppassd(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 106, "QUIT\n", r"200 ", "poppassd")
