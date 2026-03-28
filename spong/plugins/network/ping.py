"""Network check: ICMP ping."""

import re
from ... import config
from ...safe_exec import safe_exec

_PING_COUNT = 10  # pings per interval (smokeping-style spread)


def check_ping(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    ping_tmpl = config.get_command("ping", f"/bin/ping -c {_PING_COUNT} {{host}}")

    color = "green"
    down = []
    message = ""
    detected_ip = ""
    rtt_min = rtt_avg = rtt_max = loss_pct = None

    for ip in ips:
        cmd = ping_tmpl.replace("{host}", ip)
        output = "".join(safe_exec(cmd, timeout=30))
        message = output

        # Extract IP from PING header
        m = re.search(r"^PING\s+(\d{1,3}(?:\.\d{1,3}){3})", output, re.MULTILINE)
        if m:
            detected_ip = m.group(1)

        ping_ok = ("bytes from" in output or "is alive" in output or
                   "octets from" in output)
        if not ping_ok:
            color = "red"
            down.append(ip)
        else:
            # Parse rtt min/avg/max/mdev line
            m = re.search(r"rtt min/avg/max/\w+ = ([\d.]+)/([\d.]+)/([\d.]+)", output)
            if m:
                rtt_min = float(m.group(1))
                rtt_avg = float(m.group(2))
                rtt_max = float(m.group(3))
            # Parse packet loss
            m = re.search(r"(\d+)% packet loss", output)
            if m:
                loss_pct = float(m.group(1))
            break   # success

    if color == "red":
        summary = "ping failed for " + ", ".join(down)
    else:
        parts = [f"ping: {detected_ip or ips[0]}"]
        if rtt_avg is not None:
            parts.append(f"time={rtt_avg:.3f}ms")
        if rtt_min is not None:
            parts.append(f"min={rtt_min:.3f}ms")
        if rtt_max is not None:
            parts.append(f"max={rtt_max:.3f}ms")
        if loss_pct is not None:
            parts.append(f"loss={loss_pct:.0f}%")
        summary = " ".join(parts)

    return color, summary, message
