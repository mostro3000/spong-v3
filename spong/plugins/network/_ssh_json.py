"""Helper: lee un JSON remoto via SSH con caché por ciclo de red."""

import json
import subprocess
import time
import threading

# Cache: { (host, path) -> (timestamp, data_dict | None) }
_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 60  # segundos — un ciclo de spong-network


def ssh_read_json(host: str, path: str, ssh_user: str = "root",
                  timeout: int = 15) -> dict | None:
    """
    Devuelve el JSON parseado de `path` en `host` via SSH.
    Usa caché TTL=60s para no abrir N conexiones SSH por ciclo.
    Retorna None si falla.
    """
    key = (host, path)
    now = time.time()

    with _cache_lock:
        if key in _cache:
            ts, data = _cache[key]
            if now - ts < _CACHE_TTL:
                return data

    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=no",
             f"{ssh_user}@{host}", f"cat {path}"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            data = None
        else:
            data = json.loads(result.stdout)
    except Exception:
        data = None

    with _cache_lock:
        _cache[key] = (now, data)

    return data
