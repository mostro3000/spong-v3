"""Network check: Temperature sensor — lee JSON locales, via SSH, via HTTP o desde SPONG legado."""

import html
import json
import re
import urllib.request


def _http_read(url: str, *keys: str) -> float | None:
    """Fetch JSON from HTTP URL and navigate key path."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
        for k in keys:
            data = data[k]
        return float(str(data).replace(',', '.'))
    except Exception:
        return None


def _read_value(path: str, key: str) -> float | None:
    """Lee un valor numerico de un JSON local con coma decimal."""
    try:
        with open(path) as f:
            data = json.load(f)
        raw = str(data[key]).replace(',', '.')
        return float(raw)
    except Exception:
        return None


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def _legacy_spong_read(url: str, label: str = "Temperatura") -> tuple[str | None, float | None, str, str]:
    """Read current status from the published legacy SPONG service page."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read().decode(errors="replace")
    except Exception:
        return None, None, "", ""

    color = None
    m = re.search(r'bgcolor="(#[0-9A-Fa-f]{6})"', body, re.IGNORECASE)
    if m:
        color = {
            "#339900": "green",
            "#ffff00": "yellow",
            "#cc0000": "red",
            "#990099": "purple",
            "#0000ff": "blue",
            "#cccccc": "clear",
        }.get(m.group(1).lower())

    summary = ""
    m = re.search(r"<b>Summary:</b>\s*(.*?)<br>", body, re.IGNORECASE | re.DOTALL)
    if m:
        summary = _strip_html(m.group(1))

    message = ""
    m = re.search(r"<pre>(.*?)</pre>", body, re.IGNORECASE | re.DOTALL)
    if m:
        message = _strip_html(m.group(1))

    value = None
    for source in (message, summary):
        if not source:
            continue
        m = re.search(rf"{re.escape(label)}:\s*([-+]?\d+(?:[.,]\d+)?)", source, re.IGNORECASE)
        if not m:
            m = re.search(r"([-+]?\d+(?:[.,]\d+)?)", source)
        if m:
            value = float(m.group(1).replace(',', '.'))
            break

    return color, value, summary, message


# Hosts via HTTP: host -> (url, key1, [key2, ...])
_HTTP_MAP: dict[str, tuple] = {
    "living": ("http://sensor-temp-living/json", "temperature_c"),
    "pieza-ninias": ("http://sensor-temp-pieza-ninias/json", "temperature_c"),
    "pieza-chica": ("http://sensor-temp-pieza-chica/json", "temperature_c"),
}

# Legacy SPONG-backed hosts. The old monitor still publishes these values.
_LEGACY_SPONG_MAP: dict[str, tuple[str, str]] = {
    "aire-norte-computos-r": ("http://s.unsl.edu.ar/cgi-bin/www-spong.cgi/service/aire-norte-computos-r/temp", "Temperatura"),
    "aire-sur-computos-r": ("http://s.unsl.edu.ar/cgi-bin/www-spong.cgi/service/aire-sur-computos-r/temp", "Temperatura"),
}

# Mapa host -> (archivo JSON local, clave dentro del JSON)
_HOST_MAP: dict[str, tuple[str, str]] = {
    "exterior": ("/var/www/html/tiempo.json", "temperatura"),
    "comedor": ("/var/www/html/tcomedor.json", "value"),
    "garaje": ("/var/www/html/tgarage.json", "value"),
    "oficina": ("/var/www/html/toficina.json", "value"),
}

# Hosts remotos via SSH: host -> (ip, path, [key1, key2, ...])
_SSH_MAP: dict[str, tuple[str, str, list[str]]] = {
    "riego-patio": ("192.168.0.78", "/dev/shm/riepopi.json", ["air", "temperature_C"]),
}

# Umbrales por host: (warn_lo, warn_hi, crit_lo, crit_hi)
_THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    "living": (15, 27, 10, 32),
    "exterior": (10, 27, 5, 35),
    "comedor": (15, 27, 10, 39),
    "garaje": (10, 32, 5, 38),
    "pieza-ninias": (10, 32, 5, 38),
    "pieza-chica": (10, 32, 5, 38),
    "oficina": (10, 32, 5, 38),
    "riego-patio": (10, 32, 5, 38),
    "aire-norte-computos-r": (10, 32, 5, 38),
    "aire-sur-computos-r": (10, 32, 5, 38),
}


def check_temp(hostname: str) -> tuple[str, str, str]:
    if hostname in _LEGACY_SPONG_MAP:
        url, label = _LEGACY_SPONG_MAP[hostname]
        color, val, summary, message = _legacy_spong_read(url, label)
        if val is None:
            return "red", "temp: sin datos (SPONG legado)", f"No se pudo extraer temperatura desde {url}"
        if color in ("green", "yellow", "red", "purple", "blue", "clear"):
            return color, summary or f"{val:.2f}", message or f"{label}: {val:.2f}"
    elif hostname in _HTTP_MAP:
        url, *keys = _HTTP_MAP[hostname]
        val = _http_read(url, *keys)
        if val is None:
            return "red", "temp: sin datos (HTTP)", f"No se pudo leer {url}"
    elif hostname in _SSH_MAP:
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

    warn_lo, warn_hi, crit_lo, crit_hi = _THRESHOLDS.get(hostname, (10, 32, 5, 38))
    message = f"Temperatura: {val:.2f}"
    summary = f"{val:.2f}"

    if warn_lo < val < warn_hi:
        color = "green"
    elif (val >= warn_hi and val < crit_hi) or (val <= warn_lo and val > crit_lo):
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
