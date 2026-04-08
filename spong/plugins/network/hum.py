"""Network check: Humidity sensor — lee JSON locales, via SSH o via HTTP."""

from .temp import _read_value, _http_read

_HTTP_MAP: dict[str, tuple] = {
    "living": ("http://sensor-temp-living/json", "humidity_pct"),
    "pieza-chica": ("http://sensor-temp-pieza-chica/json", "humidity_pct"),
    "pieza-ninias": ("http://sensor-temp-pieza-ninias/json", "humidity_pct"),
}

_HOST_MAP: dict[str, tuple[str, str]] = {
    "exterior":     ("/var/www/html/tiempo.json",      "humedad"),
}

# Hosts remotos via SSH: host → (ip, path, [clave1, clave2, ...])
_SSH_MAP: dict[str, tuple[str, str, list[str]]] = {
    "riego-patio": ("192.168.0.78", "/dev/shm/riepopi.json", ["air", "humidity_%"]),
}


def check_hum(hostname: str) -> tuple[str, str, str]:
    if hostname in _HTTP_MAP:
        url, *keys = _HTTP_MAP[hostname]
        val = _http_read(url, *keys)
        if val is None:
            return "red", "hum: sin datos (HTTP)", f"No se pudo leer {url}"
    elif hostname in _SSH_MAP:
        from ._ssh_json import ssh_read_json
        ssh_host, path, keys = _SSH_MAP[hostname]
        data = ssh_read_json(ssh_host, path)
        try:
            val = float(data[keys[0]][keys[1]])
        except Exception:
            return "red", "hum: sin datos (SSH)", f"No se pudo leer {path} en {ssh_host}"
    elif hostname in _HOST_MAP:
        path, key = _HOST_MAP[hostname]
        val = _read_value(path, key)
        if val is None:
            return "red", "hum: sin datos", f"No se pudo leer {path}"
    else:
        return "clear", "hum: host no configurado", ""

    message = f"Humedad: {val}%"
    summary = str(val)

    # green: 15 < hum < 80 | yellow: (5<=hum<=15) o (80<=hum<=90) | red: resto
    if 15 < val < 80:
        color = "green"
    elif (5 <= val <= 15) or (80 <= val <= 90):
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
