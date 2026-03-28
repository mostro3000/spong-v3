"""Network check: Wind gust sensor — lee /var/www/html/tiempo.json."""

from .temp import _read_value

_JSON = "/var/www/html/tiempo.json"


def check_rafaga(hostname: str) -> tuple[str, str, str]:
    if hostname != "exterior":
        return "clear", "rafaga: host no configurado", ""

    val = _read_value(_JSON, "rafaga")
    if val is None:
        return "red", "rafaga: sin datos", f"No se pudo leer {_JSON}"

    message = f"Rafaga: {val}"
    summary = str(val)

    # green: <10 | yellow: 10-20 | red: >=20
    if val < 10:
        color = "green"
    elif val < 20:
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
