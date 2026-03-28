"""Network check: Humedad del patio (riegopi) via SSH JSON."""

from ._ssh_json import ssh_read_json

_SSH_HOST = "192.168.0.78"
_JSON_PATH = "/dev/shm/riepopi.json"


def check_phum(hostname: str) -> tuple[str, str, str]:
    data = ssh_read_json(_SSH_HOST, _JSON_PATH)
    try:
        val = float(data["air"]["humidity_%"])
    except Exception:
        return "red", "phum: sin datos", f"No se pudo leer {_JSON_PATH} en {_SSH_HOST}"

    message = f"Humedad patio: {val}%"
    summary = f"{val}%"

    # green: 15 < hum < 80 | yellow: (5<=hum<=15) o (80<=hum<=90) | red: resto
    if 15 < val < 80:
        color = "green"
    elif (5 <= val <= 15) or (80 <= val <= 90):
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
