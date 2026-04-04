"""Network check: SpamAssassin spamd daemon (port 783).

Sends a malformed SPAMD request; a live daemon responds with
'SPAMD/1.0 76 Bad header line:' indicating it is up.
"""

from ... import config
from ._tcp_check import check_simple


def check_spamd(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    return check_simple(host, 783, "OK\r\n\r\n", r"SPAMD/\S+ \d+ ", "spamd")
