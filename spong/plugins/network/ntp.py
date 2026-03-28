"""Network check: NTP."""

from ... import config
from ...safe_exec import safe_exec


def check_ntp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    ntpdate_cmd = config.get_command("ntpdate", "/usr/sbin/ntpdate -q")
    output = "".join(safe_exec(f"{ntpdate_cmd} {host}", timeout=15))
    if "stratum" in output.lower() or "offset" in output.lower():
        return "green", f"ntp ok - {host}", output
    return "red", f"ntp failed for {host}", output
