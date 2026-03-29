"""Network check: Sensor de presencia humana Tuya (mmWave radar).

Lee el estado de presencia via tinytuya (protocolo local, sin cloud).
Configuración en /usr/local/spong/etc/sensors.yaml (gitignoreado).

DPS relevantes del RTCZ-05 y compatibles:
  DPS 1:   estado — 'none' | 'peaceful' (estático) | 'move' (movimiento)
  DPS 101: distancia al objetivo en cm
"""

import os
import threading
import time

_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "../../../etc/sensors.yaml")
)

_config_cache: dict | None = None
_config_mtime: float | None = None
_config_lock = threading.Lock()

_device_cache: dict = {}   # hostname → (timestamp, result)
_device_lock = threading.Lock()
_CACHE_TTL = 55


def _load_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        return {}
    with _config_lock:
        if _config_cache is not None and mtime == _config_mtime:
            return _config_cache
        import yaml
        with open(_CONFIG_PATH) as f:
            data = yaml.safe_load(f)
        _config_cache = (data or {}).get("presence", {})
        _config_mtime = mtime
        return _config_cache


def _read_device(cfg: dict) -> tuple[str, str, str]:
    import tinytuya
    d = tinytuya.Device(cfg["id"], cfg["ip"], cfg["local_key"])
    d.set_version(float(cfg.get("version", 3.5)))
    d.set_socketTimeout(6)
    status = d.status()

    if not status or "dps" not in status:
        err = status.get("Error", "sin respuesta") if status else "sin respuesta"
        return "red", "presence: sin datos", f"tinytuya: {err}"

    dps = status["dps"]
    state = str(dps.get("1", "")).lower()
    dist_cm = dps.get("101")

    dist_str = f"  {dist_cm}cm" if dist_cm else ""

    _STATE = {
        "none":       ("clear",  "sin presencia",          "No se detecta presencia"),
        "peaceful":   ("green",  "presente (estático)",    "Presencia estática"),
        "move":       ("yellow", "movimiento",             "Movimiento detectado"),
        "large_move": ("yellow", "movimiento grande",      "Movimiento grande detectado"),
        "small_move": ("yellow", "movimiento pequeño",     "Movimiento pequeño detectado"),
    }
    color, label, msg = _STATE.get(state, ("green", f"presente ({state})", f"Estado: {state}"))
    if state != "none" and dist_str:
        label += dist_str
        msg   += dist_str
    return color, label, msg


def check_presence(hostname: str) -> tuple[str, str, str]:
    devices = _load_config()
    if not devices:
        return "red", "presence: sin config", f"Falta {_CONFIG_PATH}"

    cfg = devices.get(hostname)
    if not cfg:
        return "clear", "presence: host no configurado", ""

    now = time.time()
    with _device_lock:
        if hostname in _device_cache:
            ts, cached = _device_cache[hostname]
            if now - ts < _CACHE_TTL:
                return cached

    result = _read_device(cfg)

    with _device_lock:
        _device_cache[hostname] = (now, result)

    return result
