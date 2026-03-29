"""Network check: uptime via SSH.

Plugin de red equivalente al check de cliente 'uptime', pero que funciona
en cualquier host accesible via SSH sin necesitar spong-client instalado.
Parsea la salida de /usr/bin/uptime y reporta días de actividad.

Caché de 55s para no abrir múltiples conexiones SSH por ciclo.
"""

import re
import subprocess
import threading
import time
from ... import config

_cache: dict[str, tuple[float, tuple]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 55


def _ssh_uptime(ip: str, timeout: int = 40) -> str | None:
    try:
        result = subprocess.run(
            ["ssh",
             "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=30",
             "-o", "StrictHostKeyChecking=no",
             f"root@{ip}", "/usr/bin/uptime"],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def check_ruptime(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    ip = ips[0] if ips else hostname

    now = time.time()
    with _cache_lock:
        if hostname in _cache:
            ts, cached = _cache[hostname]
            if now - ts < _CACHE_TTL:
                return cached

    output = _ssh_uptime(ip)
    if not output:
        result: tuple[str, str, str] = (
            "red", "uptime: sin SSH", f"No se pudo conectar a {ip}"
        )
    else:
        m = re.search(r"up\s+([^,]+)", output)
        up_str = m.group(1).strip() if m else "desconocido"
        summary = f"up {up_str}"
        color = "yellow" if "min" in up_str.lower() else "green"
        result = (color, summary, output)

    with _cache_lock:
        _cache[hostname] = (now, result)

    return result
