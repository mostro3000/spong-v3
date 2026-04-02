"""Network check: device uptime via HTTP endpoint.

Fetches http://<host>/uptime and checks if the device has been up >= 1 day.
Designed for embedded devices that expose plain-text uptime at /uptime.
"""

import socket
from ... import config


def _http_get(host: str, path: str, timeout: int = 10) -> str:
    try:
        with socket.create_connection((host, 80), timeout=timeout) as sock:
            request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n")
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


def check_wuptime(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname

    body = _http_get(host, "/uptime")

    if body.startswith("[error:"):
        return "red", f"wuptime: {body}", body

    message = body

    if "day" in body.lower():
        return "green", f"wuptime ok - {body[:60]}", message
    else:
        return "red", "wuptime: reiniciado en menos de 24hs", message
