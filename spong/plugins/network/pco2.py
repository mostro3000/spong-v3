"""Network check: Calidad de aire del patio (riegopi) — eCO2, TVOC, AQI via SSH JSON."""

from ._ssh_json import ssh_read_json

_SSH_HOST = "192.168.0.78"
_JSON_PATH = "/dev/shm/riepopi.json"

_CO2_WARN = 1000  # ppm
_CO2_CRIT = 2000


def check_pco2(hostname: str) -> tuple[str, str, str]:
    data = ssh_read_json(_SSH_HOST, _JSON_PATH)
    try:
        air     = data["air"]
        co2     = float(air["eCO2_ppm"])
        tvoc    = float(air["TVOC_ppb"])
        aqi     = int(air["AQI"])
        aqi_cat = air.get("AQI_category", "")
    except Exception:
        return "red", "pco2: sin datos", f"No se pudo leer {_JSON_PATH} en {_SSH_HOST}"

    summary = f"eCO2: {co2:.0f}ppm TVOC: {tvoc:.0f}ppb AQI: {aqi} ({aqi_cat})"
    message = summary

    if co2 >= _CO2_CRIT or aqi >= 4:
        color = "red"
    elif co2 >= _CO2_WARN or aqi >= 3:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
