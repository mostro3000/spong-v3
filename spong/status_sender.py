"""Send status updates to the SPONG server."""

from __future__ import annotations
import socket
import time
import logging
from . import config

log = logging.getLogger(__name__)


def _send(server: str, port: int, data: bytes, timeout: int = 30) -> str | None:
    """Send raw data to server:port. Returns error string or None on success."""
    try:
        with socket.create_connection((server, port), timeout=timeout) as sock:
            sock.sendall(data)
        return None
    except OSError as e:
        return str(e)


def _send_to_servers(data: bytes) -> None:
    """Send data to all configured servers."""
    server = config.server_host()
    port = config.update_port()
    for srv in server.split():
        host, _, p = srv.partition(":")
        p = int(p) if p else port
        err = _send(host, p, data, timeout=30)
        if err:
            log.error("status_sender: %s:%d - %s", host, p, err)


def send_status(
    host: str, service: str, color: str, summary: str,
    message: str = "", ttl: int = 0,
) -> None:
    from .protocol import format_status_update
    data = format_status_update(host, service, color, summary, message, ttl)
    _send_to_servers(data)


def send_event(
    host: str, service: str, color: str, summary: str, message: str = "",
) -> None:
    ts = int(time.time())
    msg = f"event {host} {service} {color} {ts} {summary}\n{message}\n"
    _send_to_servers(msg.encode())


def send_page(
    host: str, service: str, color: str, summary: str, message: str = "",
) -> None:
    ts = int(time.time())
    msg = f"page {host} {service} {color} {ts} {summary}\n{message}\n"
    _send_to_servers(msg.encode())


def send_ack(
    host: str, services: str, end_time: float, contact: str, message: str = "",
) -> None:
    from .protocol import format_ack_update
    data = format_ack_update(host, services, time.time(), end_time, contact, message)
    _send_to_servers(data)


def send_ack_del(ack_id: str) -> None:
    from .protocol import format_ack_del
    _send_to_servers(format_ack_del(ack_id))


def query_server(command: str, hosts: str = "all",
                 fmt: str = "text", view: str = "standard",
                 extra: str = "") -> str:
    """Send a query and return the response."""
    server = config.server_host()
    port = config.query_port()
    request = f"{command} [{hosts}] {fmt} {view}"
    if extra:
        request += f" {extra}"
    request += "\n"
    try:
        with socket.create_connection((server, port), timeout=30) as sock:
            sock.sendall(request.encode())
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")
    except OSError as e:
        return f"ERROR: {e}"
