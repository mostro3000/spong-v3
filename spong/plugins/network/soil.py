"""Network check: Sensores de suelo — riego-patio (riegopi SSH JSON).

Lee los sensores de humedad de suelo, lluvia y válvulas desde
/dev/shm/riepopi.json via SSH.

Valores: 0 = mojado (sensor en cortocircuito), 100 = seco (circuito abierto).
SensorLluvia: 0 = sin lluvia, >0 = lluvia detectada.
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

# Umbrales de "seco" (el sensor devuelve valores altos cuando está seco)
_DRY_WARN = 80   # % → yellow: suelo seco
_DRY_CRIT = 95   # % → red: muy seco


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

    for key, label in _LABELS.items():
        val = soil.get(key)
        if val is None:
            continue
        val = float(val)
        parts.append(f"{label}:{val:.0f}%")
        message_lines.append(f"{label}: {val:.1f}%")
        if key != "SensorLluvia" and key != "SensorHumedadValvulas":
            if val > max_dry:
                max_dry = val

    if not parts:
        return "red", "soil: sin sensores", str(soil)

    summary = "  ".join(parts)
    message = "\n".join(message_lines)

    if lluvia > 0:
        color = "yellow"   # lluvia activa
    elif max_dry >= _DRY_CRIT:
        color = "red"
    elif max_dry >= _DRY_WARN:
        color = "yellow"
    else:
        color = "green"

    return color, summary, message
