"""Shared TCP check helper for network plugins."""

import socket
import time


def check_tcp(
    host: str, port: int, data: str = "", timeout: int = 10, maxlen: int = 4096
) -> tuple[str, str]:
    """Connect to host:port, send data, return (error_code, response).

    error_code is "" on success, or an error description.
    """
    try:
        start = time.time()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            if data:
                sock.sendall(data.encode())
            chunks = []
            received = 0
            sock.settimeout(1)
            while received < maxlen:
                try:
                    chunk = sock.recv(min(1024, maxlen - received))
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if not data:
                    break  # banner-only check: first chunk es suficiente
        return "", b"".join(chunks).decode(errors="replace")
    except socket.timeout:
        return "connection timed out", ""
    except ConnectionRefusedError:
        return "connection refused", ""
    except OSError as e:
        return str(e), ""


def check_simple(
    host: str, port: int, send: str, check_pattern: str,
    service: str = "", timeout: int = 10,
) -> tuple[str, str, str]:
    """Try connecting up to 3 times with increasing timeouts."""
    import re

    for attempt, to in enumerate([3, 5, 12], 1):
        start = time.time()
        err, message = check_tcp(host, port, send, timeout=to)
        elapsed = time.time() - start

        if not err and re.search(check_pattern, message):
            elapsed_str = f"{elapsed:.3f}"
            summary = f"{service} ok - {elapsed_str}s response time"
            if attempt > 1:
                summary += f", attempt {attempt}"
            return "green", summary, message

    summary = f"{service} is down" + (f", {err}" if err else "")
    return "red", summary, message
