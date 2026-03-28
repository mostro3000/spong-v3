"""Network check: Temperatura del patio (riegopi) via SSH JSON."""

from ._ssh_json import ssh_read_json

_SSH_HOST = "192.168.0.78"
_JSON_PATH = "/dev/shm/riepopi.json"

# Umbrales (warn_lo, warn_hi, crit_lo, crit_hi)
_WARN_LO, _WARN_HI = 10, 32
_CRIT_LO, _CRIT_HI =  5, 38


def check_ptemp(hostname: str) -> tuple[str, str, str]:
    data = ssh_read_json(_SSH_HOST, _JSON_PATH)
    try:
        val = float(data["air"]["temperature_C"])
    except Exception:
        return "red", "ptemp: sin datos", f"No se pudo leer {_JSON_PATH} en {_SSH_HOST}"

    message = f"Temperatura patio: {val}°C"
    summary = f"{val}°C"

    if _WARN_LO < val < _WARN_HI:
        color = "green"
    elif (val >= _WARN_HI and val < _CRIT_HI) or (val <= _WARN_LO and val > _CRIT_LO):
        color = "yellow"
    else:
        color = "red"

    return color, summary, message
