"""Network check: DNS."""

import socket
import time
from ... import config
from ...safe_exec import safe_exec


def check_dns(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname

    # Try Python DNS resolution first (no external tool needed)
    try:
        t0 = time.time()
        result = socket.getaddrinfo(hostname, None)
        elapsed = f"{time.time() - t0:.3f}"
        if result:
            resolved = result[0][4][0]
            return "green", f"dns ok - {hostname} -> {resolved} - {elapsed}s", ""
    except socket.gaierror:
        pass

    # Fallback to nslookup
    dns_cmd = config.get_command("dns", "/usr/bin/nslookup")
    output = "".join(safe_exec(f"{dns_cmd} {hostname}", timeout=15))
    if "Address:" in output and "NXDOMAIN" not in output:
        return "green", f"dns ok - {hostname}", output
    return "red", f"dns failed for {hostname}", output
