"""Network check: Sensores de suelo — riego-patio (riegopi SSH JSON).

Lee los sensores de humedad de suelo, lluvia y válvulas desde
/dev/shm/riepopi.json via SSH.

Sensores de suelo (pasto/cantero): 0 = mojado, 100 = seco (resistivo).
SensorLluvia: 0 = sin lluvia, >0 = lluvia detectada.
SensorHumedadValvulas: detecta agua en un lugar donde NO debería haber.
  Lógica invertida: valor bajo = agua presente = ALARMA.
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

# Umbrales sensores de suelo (valor alto = seco)
_DRY_WARN = 80   # % → yellow: suelo seco
_DRY_CRIT = 95   # % → red: muy seco

# Umbrales válvulas — lógica INVERTIDA (valor bajo = agua presente = alarma)
_VALV_CRIT = 30  # % → red: agua detectada donde no debe haber
_VALV_WARN = 50  # % → yellow: posible humedad en zona de válvulas


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
    max_dry = 0.0
    lluvia = float(soil.get("SensorLluvia", 0))
    valv = float(soil.get("SensorHumedadValvulas", 100))

    for key, label in _LABELS.items():
        val = soil.get(key)
        if val is None:
            continue
        val = float(val)
        parts.append(f"{label}:{val:.0f}%")
        message_lines.append(f"{label}: {val:.1f}%")
        if key not in ("SensorLluvia", "SensorHumedadValvulas"):
            if val > max_dry:
                max_dry = val

    if not parts:
        return "red", "soil: sin sensores", str(soil)

    summary = "  ".join(parts)
    message = "\n".join(message_lines)

    # Válvulas: lógica invertida — valor bajo = agua donde no debe haber
    if valv <= _VALV_CRIT:
        color = "red"
    elif valv <= _VALV_WARN:
        color = "yellow"
    elif lluvia > 0:
        color = "yellow"
    elif max_dry >= _DRY_CRIT:
        color = "red"
    elif max_dry >= _DRY_WARN:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
