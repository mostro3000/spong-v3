"""Network check: NTP."""

import re
from ... import config
from ...safe_exec import safe_exec


def check_ntp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    ntpdate_cmd = config.get_command("ntpdate", "/usr/sbin/ntpdate -q")
    output = "".join(safe_exec(f"{ntpdate_cmd} {host}", timeout=15))

    # Classic ntpdate format: "stratum N, offset X"
    # Modern ntpdate format:  "+X.XXXXXX +/- Y.YYYYYY ... sN no-leap"
    if ("stratum" in output.lower() or "offset" in output.lower()
            or "+/-" in output or re.search(r"\bs\d+\b", output)):
        # Extract offset for summary if possible
        m = re.search(r"offset\s+([\d.+-]+)", output)
        if not m:
            m = re.search(r"([+-]\d+\.\d+)\s+\+/-", output)
        offset_str = f" offset {m.group(1)}s" if m else ""
        return "green", f"ntp ok{offset_str}", output
    return "red", f"ntp failed for {host}", output
