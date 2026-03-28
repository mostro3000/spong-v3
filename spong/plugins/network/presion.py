"""Network check: Atmospheric pressure sensor — lee /var/www/html/tiempo.json."""

from .temp import _read_value

_JSON = "/var/www/html/tiempo.json"


def check_presion(hostname: str) -> tuple[str, str, str]:
    if hostname != "exterior":
        return "clear", "presion: host no configurado", ""

    val = _read_value(_JSON, "presion")
    if val is None:
        return "red", "presion: sin datos", f"No se pudo leer {_JSON}"

    message = f"Presion: {val}"
    summary = str(val)

    # green: 910 < val < 950 | yellow: 900 < val <= 960 (fuera de green) | red: resto
    if 910 < val < 950:
        color = "green"
    elif 900 < val <= 960:
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
