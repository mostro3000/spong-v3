"""Network check: Temperature sensor — lee JSON locales o via SSH."""

import json


def _read_value(path: str, key: str) -> float | None:
    """Lee un valor numérico de un JSON local con coma decimal (ej: '28,2' → 28.2)."""
    try:
        with open(path) as f:
            data = json.load(f)
        raw = str(data[key]).replace(',', '.')
        return float(raw)
    except Exception:
        return None


# Mapa host → (archivo JSON local, clave dentro del JSON)
_HOST_MAP: dict[str, tuple[str, str]] = {
    "exterior":    ("/var/www/html/tiempo.json",      "temperatura"),
    "comedor":     ("/var/www/html/tcomedor.json",    "value"),
    "garaje":      ("/var/www/html/tgarage.json",     "value"),
    "pieza-ninias":("/var/www/html/tpieza1piso.json", "value"),
    "oficina":     ("/var/www/html/toficina.json",    "value"),
}

# Hosts remotos (SSH): host → (ip/hostname ssh, path, json_keys anidadas)
_SSH_MAP: dict[str, tuple[str, str, list[str]]] = {
    "riegopi": ("192.168.0.78", "/dev/shm/riepopi.json", ["air", "temperature_C"]),
}

# Umbrales por host: (warn_lo, warn_hi, crit_lo, crit_hi)
_THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    "exterior":     (10, 27, 5,  35),
    "comedor":      (15, 27, 10, 39),
    "garaje":       (10, 32, 5,  38),
    "pieza-ninias": (10, 32, 5,  38),
    "oficina":      (10, 32, 5,  38),
    "riegopi":      (10, 32, 5,  38),
}


def check_temp(hostname: str) -> tuple[str, str, str]:
    # Hosts remotos via SSH
    if hostname in _SSH_MAP:
        from ._ssh_json import ssh_read_json
        ssh_host, path, keys = _SSH_MAP[hostname]
        data = ssh_read_json(ssh_host, path)
        try:
            val = float(data[keys[0]][keys[1]])
        except Exception:
            return "red", "temp: sin datos (SSH)", f"No se pudo leer {path} en {ssh_host}"
    elif hostname in _HOST_MAP:
        path, key = _HOST_MAP[hostname]
        val = _read_value(path, key)
        if val is None:
            return "red", "temp: sin datos", f"No se pudo leer {path}"
    else:
        return "clear", "temp: host no configurado", ""

    warn_lo, warn_hi, crit_lo, crit_hi = _THRESHOLDS[hostname]
    message = f"Temperatura: {val}"
    summary = str(val)

    if warn_lo < val < warn_hi:
        color = "green"
    elif (val >= warn_hi and val < crit_hi) or (val <= warn_lo and val > crit_lo):
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
