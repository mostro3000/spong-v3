"""Network check: Llaves térmicas Tuya — lectura directa via tinytuya.

Lee tensión, corriente, potencia, energía y corriente de fuga de cada
llave térmica. Incluye caché interno de 55s para no saturar los dispositivos
cuando SPONG consulta múltiples hosts en el mismo ciclo.

Configuración: /usr/local/spong/etc/termicas.yaml  (ver termicas.yaml.example)
"""

import base64
import os
import struct
import threading
import time

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../../../etc/termicas.yaml")
_CONFIG_PATH = os.path.normpath(_CONFIG_PATH)

# ---------------------------------------------------------------------------
# Carga de configuración desde YAML
# ---------------------------------------------------------------------------
_config_lock = threading.Lock()
_config_cache: dict | None = None
_config_mtime: float = 0.0


def _load_config() -> dict:
    """Lee termicas.yaml y devuelve el dict de devices. Recarga si el archivo cambia."""
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        return {}
    with _config_lock:
        if _config_cache is not None and mtime == _config_mtime:
            return _config_cache
        try:
            import yaml
            with open(_CONFIG_PATH) as f:
                data = yaml.safe_load(f)
            _config_cache = data.get("devices", {}) if data else {}
            _config_mtime = mtime
            return _config_cache
        except Exception:
            return _config_cache or {}


# ---------------------------------------------------------------------------
# Caché de lecturas por dispositivo
# ---------------------------------------------------------------------------
_CACHE: dict[str, dict] = {}
_CACHE_TTL = 55
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Decodificación de payloads binarios (idéntica a termicas_live.py)
# ---------------------------------------------------------------------------

def _decode_standard(b: bytes) -> dict | None:
    if len(b) < 8:
        return None
    return {
        "voltage_V": struct.unpack_from(">H", b, 0)[0] / 10,
        "current_A": ((b[2] << 16) | (b[3] << 8) | b[4]) / 1000,
        "power_W":   ((b[5] << 16) | (b[6] << 8) | b[7]) / 10,
    }


def _valid(d: dict) -> bool:
    v = d.get("voltage_V", 0)
    i = d.get("current_A", 0)
    p = d.get("power_W", 0)
    return (100 <= v <= 260) and (0 <= i <= 100) and (0 <= p <= 20000)


def _decode_dps17(value) -> dict | None:
    try:
        b = base64.b64decode(value)
    except Exception:
        return None
    d = _decode_standard(b)
    if d and _valid(d):
        return d
    for shift in range(1, 4):
        if len(b) >= shift + 8:
            d = _decode_standard(b[shift:])
            if d and _valid(d):
                return d
    return None


# ---------------------------------------------------------------------------
# Lectura directa del dispositivo (lógica de termicas_live.py)
# ---------------------------------------------------------------------------

def _read_device(cfg: dict) -> dict | None:
    """Conecta a la térmica y devuelve dict con los valores, o None en error."""
    try:
        import tinytuya
    except ImportError:
        return None

    try:
        dev = tinytuya.OutletDevice(
            dev_id=cfg["id"],
            address=cfg["ip"],
            local_key=cfg["local_key"],
            version=float(cfg.get("version", 3.3)),
        )
        dev.set_socketTimeout(3.0)
        dev.set_socketPersistent(True)

        raw = dev.status()
        dps = dict(raw.get("dps", {}))
        decoded = None
        version = float(cfg.get("version", 3.3))

        try:
            resp = dev.updatedps(index=[1, 6, 15, 16, 17, 18, 19, 20, 103])
            if resp and "dps" in resp:
                dps.update(resp["dps"])
            t_end = time.time() + 2.0
            while time.time() < t_end:
                try:
                    r = dev.receive()
                except Exception:
                    r = None
                if r and "dps" in r:
                    dps.update(r["dps"])
                if not decoded:
                    if "17" in dps:
                        test = _decode_dps17(dps["17"])
                        if test and _valid(test):
                            decoded = test
                    elif "6" in dps:
                        try:
                            b = base64.b64decode(dps["6"])
                            d = _decode_standard(b)
                            if d and _valid(d):
                                decoded = d
                        except Exception:
                            pass
                    if not decoded and version == 3.4:
                        tmp = {}
                        try:
                            if "20" in dps:
                                tmp["voltage_V"] = float(dps["20"]) / 10.0
                            if "18" in dps:
                                tmp["current_A"] = float(dps["18"]) / 1000.0
                            if "19" in dps:
                                tmp["power_W"]   = float(dps["19"]) / 10.0
                            if tmp:
                                decoded = tmp
                        except Exception:
                            pass
                if decoded and "1" in dps and "16" in dps:
                    break
        except Exception:
            pass

        dev.set_socketPersistent(False)

        parsed: dict = {}
        if "1"   in dps: parsed["energy_kWh"]      = round(float(dps["1"]) / 100, 2)
        if "16"  in dps: parsed["switch"]           = dps["16"]
        if "15"  in dps: parsed["leakage_current"]  = dps["15"]
        if "103" in dps: parsed["temp_internal"]    = dps["103"]

        if not decoded and "17" in dps:
            decoded = _decode_dps17(dps["17"])
        if not decoded and "6" in dps:
            try:
                b = base64.b64decode(dps["6"])
                d = _decode_standard(b)
                if d and _valid(d):
                    decoded = d
            except Exception:
                pass
        if not decoded and version == 3.4:
            tmp = {}
            try:
                if "20" in dps: tmp["voltage_V"] = float(dps["20"]) / 10.0
                if "18" in dps: tmp["current_A"] = float(dps["18"]) / 1000.0
                if "19" in dps: tmp["power_W"]   = float(dps["19"]) / 10.0
                if tmp: decoded = tmp
            except Exception:
                pass

        if decoded:
            parsed.update(decoded)

        return parsed if parsed else None

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Función principal del plugin
# ---------------------------------------------------------------------------

def check_termica(hostname: str) -> tuple[str, str, str]:
    devices = _load_config()
    if not devices:
        return "red", "termica: sin config", f"Falta {_CONFIG_PATH} — copiar desde termicas.yaml.example"

    cfg = devices.get(hostname)
    if not cfg:
        return "clear", "termica: host no configurado", f"{hostname} no está en termicas.yaml"

    # Intentar caché primero
    now = time.time()
    with _cache_lock:
        entry = _CACHE.get(hostname)
        if entry and (now - entry["timestamp"]) < _CACHE_TTL:
            d = entry["data"]
        else:
            d = None

    if d is None:
        d = _read_device(cfg)
        if d:
            with _cache_lock:
                _CACHE[hostname] = {"timestamp": now, "data": d}

    if not d:
        return "red", "termica: sin respuesta", f"No se pudo conectar a {cfg['ip']}"

    voltage = d.get("voltage_V")
    current = d.get("current_A")
    power   = d.get("power_W")
    leakage = d.get("leakage_current", 0)
    switch  = d.get("switch", True)
    temp    = d.get("temp_internal")

    if not switch:
        return "red", "termica: CORTADA", f"La llave térmica {hostname} está abierta (switch=OFF)"

    if voltage is None or current is None or power is None:
        return "red", "termica: datos incompletos", str(d)

    warn_A    = float(cfg.get("warn_A",      16.0))
    crit_A    = float(cfg.get("crit_A",      20.0))
    leak_warn = float(cfg.get("leak_warn_mA", 5.0))
    leak_crit = float(cfg.get("leak_crit_mA", 30.0))

    parts = [f"{voltage:.1f}V", f"{current:.3f}A", f"{power:.1f}W"]
    if temp is not None:
        parts.append(f"{temp}°C")
    if leakage:
        parts.append(f"fuga:{leakage}mA")
    summary = "  ".join(parts)

    message = (
        f"Tensión: {voltage:.1f} V\n"
        f"Corriente: {current:.3f} A\n"
        f"Potencia: {power:.1f} W\n"
        f"Energía: {d.get('energy_kWh', '?')} kWh\n"
        f"Corriente de fuga: {leakage} mA"
    )
    if temp is not None:
        message += f"\nTemp interna: {temp} °C"

    if current >= crit_A or leakage >= leak_crit:
        color = "red"
    elif current >= warn_A or leakage >= leak_warn:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
