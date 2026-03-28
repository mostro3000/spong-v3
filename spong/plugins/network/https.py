"""Network check: HTTPS (con soporte para TLS legacy)."""

import re
import socket
import ssl
import time
from ... import config


def _make_ctx(legacy: bool) -> ssl.SSLContext:
    if legacy:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except AttributeError:
            pass
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _https_fetch(host: str, port: int, path: str, timeout: int = 10) -> str:
    """Intenta HTTPS moderno → legacy → solo TCP (para certs con clave débil)."""
    request = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}\r\nConnection: close\r\n\r\n")

    ssl_timeout = min(timeout, 3)   # timeout corto para el handshake SSL
    # Intentos SSL: moderno y luego legacy
    for legacy in (False, True):
        try:
            ctx = _make_ctx(legacy)
            with socket.create_connection((host, port), timeout=ssl_timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as sock:
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
        except ssl.SSLError:
            if not legacy:
                continue  # reintenta con legacy
            # SSL falla por clave débil u otro motivo de handshake
            # Cae a verificación de puerto TCP
            break
        except socket.timeout:
            if not legacy:
                continue
            break
        except Exception as e:
            return f"[error: {e}]"

    # Fallback: verificar que el puerto esté abierto (cert demasiado débil para Python)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "[tcp-ok]"
    except Exception as e:
        return f"[error: {e}]"


def check_https(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    start = time.time()
    response = _https_fetch(host, 443, "/", timeout=5)
    elapsed = f"{time.time() - start:.3f}"

    if response == "[tcp-ok]":
        return "green", f"https port open - {elapsed}s", response
    code_m = re.search(r"HTTP/\S+\s+(\d{3})", response)
    if code_m:
        code = int(code_m.group(1))
        if code >= 500:
            return "red", f"https error - {code}", response
        if code >= 400 and code != 401:
            return "yellow", f"https warning - {code}", response
        return "green", f"https ok - {code} - {elapsed}s", response
    if "[error" in response:
        return "red", "https connection failed", response
    return "yellow", "can't determine https status", response
