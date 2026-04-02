"""Network check: HTTP."""

import re
import socket
import time
from ... import config


def _tcp_fetch(connect_host: str, port: int, request: str, timeout: int = 10) -> str:
    try:
        start = time.time()
        with socket.create_connection((connect_host, port), timeout=timeout) as sock:
            sock.sendall(request.encode())
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if len(b"".join(chunks)) > 65536:
                    break
        return b"".join(chunks).decode(errors="replace")
    except Exception as e:
        return f"[connection error: {e}]"


def _resolve(hname: str, hostname: str) -> str:
    """Return IP to connect to: use spong's configured IP when hname matches
    the spong hostname (avoids DNS failures for unresolvable device names)."""
    if hname == hostname:
        ips = config.host_ips(hostname)
        if ips:
            return ips[0]
    return hname


def check_http(hostname: str) -> tuple[str, str, str]:
    urls = config.http_urls_for(hostname)
    color = "green"
    summary = ""
    full_message = ""

    for url in urls:
        # Parse URL
        m = re.match(r"^(?:HEAD |GET )?(?:https?://)?([^/]+)(/.*)$", url)
        if not m:
            m = re.match(r"^(?:https?://)?([^/]+)(.*)$", url)
        if not m:
            continue
        hostpart = m.group(1)
        path = m.group(2) or "/"

        hname, _, port_str = hostpart.partition(":")
        port = int(port_str) if port_str else 80
        if not hname or hname == "_HOST_":
            hname = hostname

        connect_host = _resolve(hname, hostname)

        method = "GET"
        request = (f"{method} {path} HTTP/1.1\r\n"
                   f"Host: {hname}:{port}\r\n"
                   f"Connection: close\r\n\r\n")

        t0 = time.time()
        response = _tcp_fetch(connect_host, port, request)
        elapsed = f"{time.time() - t0:.3f}"
        full_message += f"->{method} {path} HTTP/1.1\nHost: {hname}:{port}\n{response}\n\n"

        code_m = re.search(r"HTTP/\S+\s+(\d{3})", response)
        if code_m:
            code = int(code_m.group(1))
            if code >= 500:
                color = "red"
                summary = f"http error - {code} - {url}"
            elif code >= 400 and code != 401:
                if color != "red":
                    color = "yellow"
                    summary = f"http warning - {code} - {url}"
            else:
                if color not in ("red", "yellow"):
                    color = "green"
                    summary = f"http ok - {code} - {elapsed}s"
        elif "HTTP" not in response:
            color = "red"
            summary = "no response from http server"
        else:
            if color != "red":
                color = "yellow"
                summary = "can't determine status code"

    return color, summary or "http ok", full_message
