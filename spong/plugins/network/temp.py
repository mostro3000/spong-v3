"""Network check: Temperature sensor — lee JSON locales en /var/www/html/."""

import json
import os


def _read_value(path: str, key: str) -> float | None:
    """Lee un valor numérico de un JSON con coma decimal (ej: '28,2' → 28.2)."""
    try:
        with open(path) as f:
            data = json.load(f)
        raw = str(data[key]).replace(',', '.')
        return float(raw)
    except Exception:
        return None


# Mapa host → (archivo JSON, clave dentro del JSON)
_HOST_MAP: dict[str, tuple[str, str]] = {
    "exterior":    ("/var/www/html/tiempo.json",      "temperatura"),
    "comedor":     ("/var/www/html/tcomedor.json",    "value"),
    "garaje":      ("/var/www/html/tgarage.json",     "value"),
    "pieza-ninias":("/var/www/html/tpieza1piso.json", "value"),
    "oficina":     ("/var/www/html/toficina.json",    "value"),
}

# Umbrales por host: (warn_lo, warn_hi, crit_lo, crit_hi)
# green  si warn_lo < temp < warn_hi
# yellow si (warn_hi <= temp < crit_hi) o (crit_lo < temp <= warn_lo)
# red    en otro caso
_THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    "exterior":     (10, 27, 5,  35),
    "comedor":      (15, 27, 10, 39),
    "garaje":       (10, 32, 5,  38),
    "pieza-ninias": (10, 32, 5,  38),
    "oficina":      (10, 32, 5,  38),
}


def check_temp(hostname: str) -> tuple[str, str, str]:
    if hostname not in _HOST_MAP:
        return "clear", "temp: host no configurado", ""

    path, key = _HOST_MAP[hostname]
    val = _read_value(path, key)

    if val is None:
        return "red", "temp: sin datos", f"No se pudo leer {path}"

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
