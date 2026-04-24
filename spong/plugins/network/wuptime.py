"""Network check: device uptime via HTTP endpoint.

Fetches http://<host>/uptime and classifies:
- red: uptime < 1 hour
- yellow: 1 hour <= uptime < 24 hours
- green: uptime >= 24 hours
"""

import re
import socket
from ... import config

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _http_get(host: str, path: str, timeout: int = 10) -> str:
    try:
        with socket.create_connection((host, 80), timeout=timeout) as sock:
            request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            sock.sendall(request.encode())
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        response = b"".join(chunks).decode(errors="replace")
        if "\r\n\r\n" in response:
            return response.split("\r\n\r\n", 1)[1].strip()
        return response.strip()
    except Exception as e:
        return f"[error: {e}]"


def _parse_uptime_seconds(body: str) -> int | None:
    text = _ANSI_RE.sub("", body or "")
    text = " ".join(text.split())
    if not text:
        return None

    patterns = [
        (r"\bup\s+(\d+)\s+days?,\s*(\d+):(\d+)\b", lambda m: int(m.group(1)) * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60),
        (r"\bup\s+(\d+)\s+days?\b", lambda m: int(m.group(1)) * 86400),
        (r"\bup\s+(\d+):(\d+)\b", lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60),
        (r"\bup\s+(\d+)\s+hours?\b", lambda m: int(m.group(1)) * 3600),
        (r"\bup\s+(\d+)\s+hrs?\b", lambda m: int(m.group(1)) * 3600),
        (r"\bup\s+(\d+)\s+minutes?\b", lambda m: int(m.group(1)) * 60),
        (r"\bup\s+(\d+)\s+mins?\b", lambda m: int(m.group(1)) * 60),
        (r"\bup\s+(\d+)\s+min\b", lambda m: int(m.group(1)) * 60),
        (r"\bup\s+(\d+)\s+seconds?\b", lambda m: int(m.group(1))),
        (r"\bup\s+(\d+)\s+secs?\b", lambda m: int(m.group(1))),
    ]

    for pattern, convert in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return convert(match)
    return None


def check_wuptime(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname

    body = _http_get(host, "/uptime")

    if body.startswith("[error:"):
        return "red", f"wuptime: {body}", body

    message = body
    uptime_seconds = _parse_uptime_seconds(body)
    if uptime_seconds is None:
        if "day" in body.lower():
            return "green", f"wuptime ok - {body[:60]}", message
        return "red", "wuptime: uptime no interpretable", message

    if uptime_seconds < 3600:
        return "red", "wuptime: reiniciado hace menos de 1h", message
    if uptime_seconds < 86400:
        return "yellow", "wuptime: reiniciado entre 1h y 24hs", message
    return "green", f"wuptime ok - {body[:60]}", message
