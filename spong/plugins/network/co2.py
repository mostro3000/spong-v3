"""Network check: Air quality (eCO2, TVOC, AQI) via SSH JSON."""

from ._ssh_json import ssh_read_json

_SSH_MAP: dict[str, tuple[str, str]] = {
    "riegopi":    ("192.168.0.78", "/dev/shm/riepopi.json"),
    "riego-patio": ("192.168.0.78", "/dev/shm/riepopi.json"),
}

# Umbrales eCO2 (ppm): normal indoor ~400-1000
_CO2_WARN = 1000
_CO2_CRIT = 2000


def check_co2(hostname: str) -> tuple[str, str, str]:
    if hostname not in _SSH_MAP:
        return "clear", "co2: host no configurado", ""

    ssh_host, path = _SSH_MAP[hostname]
    data = ssh_read_json(ssh_host, path)

    try:
        air  = data["air"]
        co2  = float(air["eCO2_ppm"])
        tvoc = float(air["TVOC_ppb"])
        aqi  = int(air["AQI"])
        aqi_cat = air.get("AQI_category", "")
    except Exception:
        return "red", "co2: sin datos (SSH)", f"No se pudo leer {path} en {ssh_host}"

    summary = f"eCO2: {co2:.0f}ppm TVOC: {tvoc:.0f}ppb AQI: {aqi} ({aqi_cat})"
    message = summary

    if co2 >= _CO2_CRIT or aqi >= 4:
        color = "red"
    elif co2 >= _CO2_WARN or aqi >= 3:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
