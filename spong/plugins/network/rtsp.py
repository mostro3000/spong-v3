"""Network check: disponibilidad de cámara Tapo via RTSP.

Secuencia de verificación:
1. DESCRIBE rtsp://<ip>/stream1  — confirma que el stream real existe
   (200 OK o 401 Unauthorized ambos indican stream activo)
2. Si falla, OPTIONS * en puerto 554  — chequeo RTSP genérico
3. Si falla, TCP en puerto 2020  — protocolo propietario Tapo C-series
"""

import socket
import time
from ... import config

_TIMEOUT = 5


def _rtsp_describe(ip: str, path: str = "/stream1") -> tuple[bool, str, str]:
    """Envía DESCRIBE al stream y devuelve (ok, resumen, detalle)."""
    url = f"rtsp://{ip}:554{path}"
    request = (
        f"DESCRIBE {url} RTSP/1.0\r\n"
        f"CSeq: 1\r\n"
        f"User-Agent: SPONG\r\n"
        f"Accept: application/sdp\r\n\r\n"
    ).encode()
    try:
        start = time.time()
        with socket.create_connection((ip, 554), timeout=_TIMEOUT) as s:
            s.sendall(request)
            s.settimeout(3.0)
            try:
                banner = s.recv(512).decode(errors="replace")
            except socket.timeout:
                banner = ""
        elapsed = time.time() - start

        if not banner:
            return False, f"DESCRIBE {path}: sin respuesta", banner

        first_line = banner.split("\r\n", 1)[0]
        # 200 OK → stream libre; 401/403 → stream existe pero requiere auth
        if "RTSP/1.0 200" in banner:
            return True, f"stream{path} ok  {elapsed:.2f}s", banner
        if any(code in banner for code in ("RTSP/1.0 401", "RTSP/1.0 403")):
            return True, f"stream{path} activo (auth)  {elapsed:.2f}s", banner
        if "RTSP/1.0" in banner:
            return False, f"DESCRIBE {path}: {first_line.strip()}", banner
        # Puerto abierto pero respuesta no RTSP
        return True, f"TCP/554 ok  {elapsed:.2f}s", banner
    except ConnectionRefusedError:
        return False, "RTSP/554 rehusado", ""
    except (socket.timeout, OSError) as e:
        return False, f"RTSP/554: {e}", ""


def _rtsp_options(ip: str) -> tuple[bool, str, str]:
    request = b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: SPONG\r\n\r\n"
    try:
        start = time.time()
        with socket.create_connection((ip, 554), timeout=_TIMEOUT) as s:
            s.sendall(request)
            s.settimeout(2.0)
            try:
                banner = s.recv(256).decode(errors="replace")
            except socket.timeout:
                banner = ""
        elapsed = time.time() - start
        if "RTSP/1.0" in banner:
            return True, f"RTSP/554 ok  {elapsed:.2f}s", banner
        return True, f"TCP/554 ok  {elapsed:.2f}s", banner
    except ConnectionRefusedError:
        return False, "RTSP/554 rehusado", ""
    except (socket.timeout, OSError) as e:
        return False, f"RTSP/554: {e}", ""


def _try_tapo_port(ip: str) -> tuple[bool, str, str]:
    try:
        start = time.time()
        with socket.create_connection((ip, 2020), timeout=_TIMEOUT):
            pass
        elapsed = time.time() - start
        return True, f"Tapo/2020  {elapsed:.2f}s", ""
    except ConnectionRefusedError:
        return False, "Tapo/2020 rehusado", ""
    except (socket.timeout, OSError) as e:
        return False, f"Tapo/2020: {e}", ""


def check_rtsp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    ip = ips[0] if ips else hostname

    # 1. DESCRIBE stream1
    ok, info, detail = _rtsp_describe(ip, "/stream1")
    if ok:
        return "green", info, f"Cámara {hostname} ({ip})\n{detail}"

    errors = [info]

    # 2. OPTIONS genérico
    ok, info, detail = _rtsp_options(ip)
    if ok:
        return "green", info, f"Cámara {hostname} ({ip})\n{detail}"

    errors.append(info)

    # 3. Puerto propietario Tapo
    ok, info, detail = _try_tapo_port(ip)
    if ok:
        return "green", info, f"Cámara {hostname} ({ip})\n{detail}"

    errors.append(info)
    return "red", "rtsp: sin stream", f"Cámara {hostname} ({ip})\n" + "\n".join(errors)
