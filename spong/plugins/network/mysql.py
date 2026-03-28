"""Network check: MySQL."""

from ... import config
from ._tcp_check import check_tcp
import time


def check_mysql(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    start = time.time()
    err, message = check_tcp(host, 3306, "", timeout=10, maxlen=256)
    elapsed = f"{time.time()-start:.3f}"
    # MySQL sends a greeting on connect
    if err:
        return "red", f"mysql is down, {err}", message
    if message and len(message) > 4:
        return "green", f"mysql ok - {elapsed}s", message
    return "yellow", "mysql: unexpected response", message
