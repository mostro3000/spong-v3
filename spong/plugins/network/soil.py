"""Network check: Sensores de suelo — riego-patio (riegopi SSH JSON).

Lee los sensores de humedad de suelo, lluvia y válvulas desde
/dev/shm/riepopi.json via SSH.

Sensores de suelo (pasto/cantero): % de humedad. 100% = muy húmedo, 0% = seco.
SensorLluvia: 0 = sin lluvia, >0 = lluvia detectada.
SensorHumedadValvulas: detecta agua en un lugar donde NO debería haber.
  Valor alto = agua presente = ALARMA. <30% verde, 30-50% amarillo, >50% rojo.
"""

from ._ssh_json import ssh_read_json

_SSH_MAP: dict[str, tuple[str, str]] = {
    "riego-patio": ("192.168.0.78", "/dev/shm/riepopi.json"),
}

# Etiquetas cortas para el summary (max ~8 chars c/u)
_LABELS = {
    "SensorLluvia":                   "Lluvia",
    "SensorHumedadValvulas":          "Valv",
    "SensorHumedadPastoSurEste":      "PastoSE",
    "SensorHumedadPastoNorteEste":    "PastoNE",
    "SensorHumedadPastoNorteOeste":   "PastoNO",
    "SensorHumedadCanteroSur":        "CantSur",
    "SensorHumedadCanteroNorteEste":  "CantNE",
    "SensorHumedadCanteroNorteOeste": "CantNO",
}

# Umbrales sensores de suelo (% humedad: valor bajo = seco = alarma)
_DRY_WARN = 20   # % → yellow: suelo seco
_DRY_CRIT = 10   # % → red: suelo muy seco

# Umbrales válvulas — valor alto = agua presente = alarma
_VALV_WARN = 30  # % → yellow: posible humedad en zona de válvulas
_VALV_CRIT = 50  # % → red: agua detectada donde no debe haber


def check_soil(hostname: str) -> tuple[str, str, str]:
    if hostname not in _SSH_MAP:
        return "clear", "soil: host no configurado", ""

    ssh_host, path = _SSH_MAP[hostname]
    data = ssh_read_json(ssh_host, path)

    try:
        soil = data["soil"]
    except Exception:
        return "red", "soil: sin datos (SSH)", f"No se pudo leer {path} en {ssh_host}"

    parts = []
    message_lines = []
    min_hum = 100.0
    lluvia = float(soil.get("SensorLluvia", 0))
    valv = float(soil.get("SensorHumedadValvulas", 0))

    for key, label in _LABELS.items():
        val = soil.get(key)
        if val is None:
            continue
        val = float(val)
        parts.append(f"{label}:{val:.0f}%")
        message_lines.append(f"{label}: {val:.1f}%")
        if key not in ("SensorLluvia", "SensorHumedadValvulas"):
            if val < min_hum:
                min_hum = val

    if not parts:
        return "red", "soil: sin sensores", str(soil)

    summary = "  ".join(parts)
    message = "\n".join(message_lines)

    # Válvulas: valor alto = agua donde no debe haber
    if valv >= _VALV_CRIT:
        color = "red"
    elif valv >= _VALV_WARN:
        color = "yellow"
    # Suelo: valor bajo = seco = alarma
    elif min_hum <= _DRY_CRIT:
        color = "red"
    elif min_hum <= _DRY_WARN:
        color = "yellow"
    elif lluvia > 0:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
