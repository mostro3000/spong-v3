"""Helper compartido para checks de cámaras via HTTP (endpoint /cN.txt)."""

import re
import socket
from ... import config


def check_camara(hostname: str, n: int) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname

    try:
        with socket.create_connection((host, 80), timeout=10) as sock:
            sock.sendall(
                f"GET /c{n}.txt HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        response = b"".join(chunks).decode(errors="replace")
        body = response.split("\r\n\r\n", 1)[1].strip() if "\r\n\r\n" in response else response.strip()
    except Exception as e:
        return "red", f"camara{n}: {e}", ""

    m = re.search(r"(\d+)", body)
    count = int(m.group(1)) if m else 0
    message = f"camara{n}: {count} grabaciones\n{body}"

    if count >= 1:
        return "green", f"camara{n}: {count} grabaciones", message
    else:
        return "red", f"camara{n}: sin grabaciones", message
