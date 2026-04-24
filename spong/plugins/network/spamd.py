"""Network check: SpamAssassin spamd daemon (port 783).

Uses a valid SPAMC PING request; a live daemon should reply with
"SPAMD/<ver> 0 PONG".
"""

from ... import config
from ._tcp_check import check_simple

_REQUEST = "PING SPAMC/1.5\r\nUser: spong\r\n\r\n"


def check_spamd(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 783, _REQUEST, r"SPAMD/\S+ 0 PONG", "spamd")
