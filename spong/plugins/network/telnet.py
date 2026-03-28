"""Network check: Telnet."""

from ... import config
from ._tcp_check import check_tcp
import time


def check_telnet(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    start = time.time()
    err, message = check_tcp(host, 23, "", timeout=10)
    elapsed = f"{time.time()-start:.3f}"
    if err:
        return "red", f"telnet is down, {err}", message
    return "green", f"telnet ok - {elapsed}s", message
