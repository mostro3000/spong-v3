"""Network check: WiFi associations via HTTP endpoint.

Fetches http://<host>/assoc and parses the association count.
Designed for APs that expose a plain-text association count at /assoc.
"""

import re
import socket
from ... import config


def _http_get(host: str, path: str, timeout: int = 10) -> tuple[int, str]:
    """Returns (http_status_code, body). status=0 on connection error."""
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
        m = re.search(r"HTTP/\S+\s+(\d{3})", response)
        status = int(m.group(1)) if m else 0
        body = response.split("\r\n\r\n", 1)[1].strip() if "\r\n\r\n" in response else response.strip()
        return status, body
    except Exception as e:
        return 0, f"[error: {e}]"


def check_wassoc(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname

    status, body = _http_get(host, "/assoc")

    if status == 0:
        return "red", f"wassoc: {body}", body

    if status >= 400:
        return "clear", f"wassoc: HTTP {status} (endpoint no disponible)", body

    # Extract first integer from body
    m = re.search(r"(\d+)", body)
    if not m:
        return "red", "wassoc: respuesta inesperada", body

    count = int(m.group(1))
    message = f"{count} wireless assoc\n{body}"

    if count < 5:
        return "green", f"wassoc: {count} asociados", message
    elif count < 10:
        return "yellow", f"wassoc: {count} asociados", message
    else:
        return "red", f"wassoc: {count} asociados", message
