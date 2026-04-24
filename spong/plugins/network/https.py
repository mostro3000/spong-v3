"""Network check: HTTPS (con soporte para TLS legacy)."""

import re
import socket
import ssl
import time
from ... import config

_CERT_RED_SECS = 3 * 86400
_CERT_YELLOW_SECS = 6 * 86400


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


def _cert_expiry_ts(cert: dict) -> float | None:
    not_after = cert.get("notAfter") if cert else None
    if not not_after:
        return None
    try:
        return float(ssl.cert_time_to_seconds(not_after))
    except Exception:
        return None


def _cert_expiry_label(expiry_ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    remaining = int(expiry_ts - now)
    if remaining <= 0:
        expired = abs(remaining)
        if expired >= 86400:
            return f"expired {expired // 86400}d ago"
        if expired >= 3600:
            return f"expired {expired // 3600}h ago"
        return "expired"
    if remaining >= 86400:
        return f"expires in {remaining // 86400}d"
    if remaining >= 3600:
        return f"expires in {remaining // 3600}h"
    return f"expires in {remaining // 60}m"


def _cert_expiry_color(expiry_ts: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    remaining = expiry_ts - now
    if remaining < _CERT_RED_SECS:
        return "red"
    if remaining < _CERT_YELLOW_SECS:
        return "yellow"
    return ""


def _https_fetch(host: str, port: int, path: str, timeout: int = 10) -> tuple[str, float | None]:
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
                    cert_expiry_ts = _cert_expiry_ts(sock.getpeercert())
                    sock.sendall(request.encode())
                    chunks = []
                    while True:
                        data = sock.recv(4096)
                        if not data:
                            break
                        chunks.append(data)
                        if len(b"".join(chunks)) > 65536:
                            break
            return b"".join(chunks).decode(errors="replace"), cert_expiry_ts
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
            return f"[error: {e}]", None

    # Fallback: verificar que el puerto esté abierto (cert demasiado débil para Python)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "[tcp-ok]", None
    except Exception as e:
        return f"[error: {e}]", None


def check_https(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    start = time.time()
    response, cert_expiry_ts = _https_fetch(host, 443, "/", timeout=5)
    elapsed = f"{time.time() - start:.3f}"
    cert_now = time.time()
    cert_detail = ""
    cert_color = ""
    if cert_expiry_ts is not None:
        cert_detail = _cert_expiry_label(cert_expiry_ts, cert_now)
        cert_color = _cert_expiry_color(cert_expiry_ts, cert_now)
        expiry_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cert_expiry_ts))
        response = f"certificate notAfter: {expiry_str} ({cert_detail})\n\n{response}"

    if response == "[tcp-ok]":
        return "green", f"https port open - {elapsed}s", response
    code_m = re.search(r"HTTP/\S+\s+(\d{3})", response)
    if code_m:
        code = int(code_m.group(1))
        if code >= 500:
            summary = f"https error - {code}"
            if cert_color:
                summary += f" - {cert_detail}"
            return "red", summary, response
        if code >= 400 and code != 401:
            color = "red" if cert_color == "red" else "yellow"
            summary = f"https warning - {code}"
            if cert_color:
                summary += f" - {cert_detail}"
            return color, summary, response
        if cert_color == "red":
            return "red", f"https cert warning - {cert_detail}", response
        if cert_color == "yellow":
            return "yellow", f"https cert warning - {cert_detail}", response
        return "green", f"https ok - {code} - {elapsed}s", response
    if "[error" in response:
        return "red", "https connection failed", response
    if cert_color == "red":
        return "red", f"https cert warning - {cert_detail}", response
    if cert_color == "yellow":
        return "yellow", f"https cert warning - {cert_detail}", response
    return "yellow", "can't determine https status", response
