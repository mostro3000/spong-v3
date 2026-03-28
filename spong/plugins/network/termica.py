"""Network check: Llaves térmicas Tuya — lee caché JSON de termicas_live.py."""

import json
import os
import time

_CACHE_PATH = "/root/hack/termicas/.termicas_cache.json"
_MAX_AGE = 300  # segundos; termicas_live.py actualiza cada 60s

# hostname SPONG → device_id Tuya
_HOST_MAP: dict[str, str] = {
    "termica1":        "eb7b8fc3c8c382de81yrng",
    "termica2":        "eb70617be0205230b4mdzp",
    "termica3":        "eb675e5534729258ccozov",
    "termica-central": "eb17413663ef0c07d0mtns",
}

# Umbrales por host: (warn_A, crit_A, leak_warn_mA, leak_crit_mA)
_THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    "termica1":        (16.0, 20.0, 5.0, 30.0),
    "termica2":        (16.0, 20.0, 5.0, 30.0),
    "termica3":        (16.0, 20.0, 5.0, 30.0),
    "termica-central": (25.0, 32.0, 5.0, 30.0),
}


def check_termica(hostname: str) -> tuple[str, str, str]:
    device_id = _HOST_MAP.get(hostname)
    if not device_id:
        return "clear", "termica: host no configurado", ""

    try:
        with open(_CACHE_PATH) as f:
            cache = json.load(f)
    except Exception as e:
        return "red", "termica: sin caché", f"No se pudo leer {_CACHE_PATH}: {e}"

    entry = cache.get(device_id)
    if not entry:
        return "red", "termica: sin datos", f"Device {device_id} no encontrado en caché"

    age = time.time() - entry.get("timestamp", 0)
    if age > _MAX_AGE:
        return "red", f"termica: datos viejos ({int(age)}s)", "termicas_live.py no está corriendo?"

    d = entry.get("data", {})
    voltage = d.get("voltage_V")
    current = d.get("current_A")
    power   = d.get("power_W")
    leakage = d.get("leakage_current", 0)
    switch  = d.get("switch", True)
    temp    = d.get("temp_internal")

    if not switch:
        return "red", "termica: CORTADA", f"La llave térmica {hostname} está abierta (switch=OFF)"

    if voltage is None or current is None or power is None:
        return "red", "termica: datos incompletos", str(d)

    warn_A, crit_A, leak_warn, leak_crit = _THRESHOLDS[hostname]

    parts = [f"{voltage:.1f}V", f"{current:.3f}A", f"{power:.1f}W"]
    if temp is not None:
        parts.append(f"{temp}°C")
    if leakage:
        parts.append(f"fuga:{leakage}mA")
    summary = "  ".join(parts)
    message = (
        f"Tensión: {voltage:.1f} V\n"
        f"Corriente: {current:.3f} A\n"
        f"Potencia: {power:.1f} W\n"
        f"Energía: {d.get('energy_kWh', '?')} kWh\n"
        f"Corriente de fuga: {leakage} mA"
    )
    if temp is not None:
        message += f"\nTemp interna: {temp} °C"

    if current >= crit_A or leakage >= leak_crit:
        color = "red"
    elif current >= warn_A or leakage >= leak_warn:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
