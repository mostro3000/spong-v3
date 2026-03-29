"""Network check: disponibilidad de cámara via RTSP o protocolo Tapo.

Prueba puerto 554 con RTSP OPTIONS estándar; si falla, prueba puerto 2020
(protocolo propietario Tapo C-series). Verde si alguno responde.
"""

import socket
import time
from ... import config

_RTSP_REQUEST = b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: SPONG\r\n\r\n"
_TIMEOUT = 5


def _try_rtsp(ip: str) -> tuple[bool, str]:
    try:
        start = time.time()
        with socket.create_connection((ip, 554), timeout=_TIMEOUT) as s:
            s.sendall(_RTSP_REQUEST)
            s.settimeout(2.0)
            try:
                banner = s.recv(256).decode(errors="replace")
            except socket.timeout:
                banner = ""
        elapsed = time.time() - start
        if "RTSP/1.0" in banner:
            return True, f"RTSP/554  {elapsed:.2f}s"
        # Puerto abierto pero sin respuesta RTSP válida — igual cuenta como up
        return True, f"TCP/554 ok  {elapsed:.2f}s"
    except ConnectionRefusedError:
        return False, "RTSP/554 rehusado"
    except (socket.timeout, OSError):
        return False, "RTSP/554 sin respuesta"


def _try_tapo(ip: str) -> tuple[bool, str]:
    try:
        start = time.time()
        with socket.create_connection((ip, 2020), timeout=_TIMEOUT):
            pass
        elapsed = time.time() - start
        return True, f"Tapo/2020  {elapsed:.2f}s"
    except ConnectionRefusedError:
        return False, "Tapo/2020 rehusado"
    except (socket.timeout, OSError):
        return False, "Tapo/2020 sin respuesta"


def check_rtsp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    ip = ips[0] if ips else hostname

    ok, info = _try_rtsp(ip)
    if not ok:
        ok2, info2 = _try_tapo(ip)
        if ok2:
            ok, info = True, info2
        else:
            return "red", "rtsp: sin stream", f"{info} / {info2}"

    return "green", info, f"Cámara {hostname} ({ip}): {info}"
