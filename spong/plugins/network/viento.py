"""Network check: Wind speed sensor — lee /var/www/html/tiempo.json."""

from .temp import _read_value

_JSON = "/var/www/html/tiempo.json"


def check_viento(hostname: str) -> tuple[str, str, str]:
    if hostname != "exterior":
        return "clear", "viento: host no configurado", ""

    val = _read_value(_JSON, "viento")
    if val is None:
        return "red", "viento: sin datos", f"No se pudo leer {_JSON}"

    message = f"Viento: {val}"
    summary = str(val)

    # green: <10 | yellow: 10-20 | red: >=20
    if val < 10:
        color = "green"
    elif val < 20:
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
