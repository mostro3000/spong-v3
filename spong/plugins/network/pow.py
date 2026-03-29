"""Network check: Sonoff POW R2 / Tasmota — consumo eléctrico via HTTP.

Lee tensión, corriente, potencia y energía de cualquier dispositivo
con firmware Tasmota que exponga la API /cm?cmnd=Status%208.

La IP se obtiene de ip_addr en hosts.yaml.
"""

import json
import urllib.request
import urllib.error
from ... import config


def _fetch_energy(ip: str, timeout: int = 8) -> dict | None:
    """GET /cm?cmnd=Status%208 → dict ENERGY, o None en error."""
    try:
        url = f"http://{ip}/cm?cmnd=Status%208"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data.get("StatusSNS", {}).get("ENERGY")
    except Exception:
        return None


def check_pow(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    ip = ips[0] if ips else hostname

    e = _fetch_energy(ip)
    if not e:
        return "red", "pow: sin respuesta", f"No se pudo conectar a {ip} (Tasmota /cm?cmnd=Status 8)"

    power   = float(e.get("Power",   0))
    voltage = float(e.get("Voltage", 0))
    current = float(e.get("Current", 0))
    today   = float(e.get("Today",   0))
    total   = float(e.get("Total",   0))
    factor  = float(e.get("Factor",  1.0))

    summary = f"{power:.0f}W  {current:.3f}A  {voltage:.0f}V  {today:.3f}kWh"
    message = (
        f"Potencia: {power:.1f} W\n"
        f"Corriente: {current:.3f} A\n"
        f"Tensión: {voltage:.0f} V\n"
        f"Factor de potencia: {factor:.2f}\n"
        f"Energía hoy: {today:.3f} kWh\n"
        f"Energía total: {total:.3f} kWh"
    )

    if power >= 2000:
        color = "red"
    elif power >= 1500:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
