"""Network check: Humidity sensor — lee JSON locales en /var/www/html/."""

from .temp import _read_value

_HOST_MAP: dict[str, tuple[str, str]] = {
    "exterior":     ("/var/www/html/tiempo.json",      "humedad"),
    "pieza-ninias": ("/var/www/html/hpieza1piso.json", "value"),
}

def check_hum(hostname: str) -> tuple[str, str, str]:
    if hostname in _HOST_MAP:
        path, key = _HOST_MAP[hostname]
        val = _read_value(path, key)
        if val is None:
            return "red", "hum: sin datos", f"No se pudo leer {path}"
    else:
        return "clear", "hum: host no configurado", ""

    message = f"Humedad: {val}"
    summary = str(val)

    # green: 15 < hum < 80 | yellow: (5<=hum<=15) o (80<=hum<=90) | red: resto
    if 15 < val < 80:
        color = "green"
    elif (5 <= val <= 15) or (80 <= val <= 90):
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
