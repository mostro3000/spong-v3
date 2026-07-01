"""Helpers compartidos para checks individuales de UPS APC via SNMP."""

from .snmp import snmp_get_int
from ... import config

_BASE = [1, 3, 6, 1, 4, 1, 318, 1, 1, 1]

OIDS = {
    "volt_in":   _BASE + [3, 2, 1, 0],
    "volt_out":  _BASE + [4, 2, 1, 0],
    "freq_in":   _BASE + [3, 2, 4, 0],
    "freq_out":  _BASE + [4, 2, 2, 0],
    "temp_bat":  _BASE + [2, 2, 2, 0],
    "temp_ext":  [1, 3, 6, 1, 4, 1, 318, 1, 1, 10, 2, 3, 2, 1, 4, 1],
}

UNITS = {
    "volt_in": "V", "volt_out": "V",
    "freq_in": "Hz", "freq_out": "Hz",
    "temp_bat": "°C", "temp_ext": "°C",
}


def _normalize(metric: str, raw: int) -> float:
    """Normaliza la lectura SNMP a la unidad final.

    La MIB APC PowerNet reporta freq y temp_ext en décimas (0.1 Hz / 0.1 °C).
    Debe dividirse siempre por 10, igual que hace ups.py; el heurístico previo
    (raw > 1000) nunca disparaba para temperaturas reales — una sonda a 25 °C
    (raw 250) quedaba como 250 °C = rojo falso permanente.
    """
    if metric in ("freq_in", "freq_out", "temp_ext"):
        return round(raw / 10.0, 1)
    return float(raw)


def _color(val: float, metric: str) -> str:
    if metric in ("volt_in", "volt_out"):
        if val < 195 or val > 240: return "red"
        if val < 205 or val > 230: return "yellow"
    elif metric in ("freq_in", "freq_out"):
        if val < 46 or val > 54: return "red"
        if val < 48 or val > 52: return "yellow"
    elif metric in ("temp_bat", "temp_ext"):
        if val > 40: return "red"
        if val > 30: return "yellow"
    return "green"


def check_ups_metric(hostname: str, metric: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")

    raw = snmp_get_int(host, community, OIDS[metric])
    if raw is None:
        return "purple", f"{metric}: sin respuesta SNMP", ""

    val = _normalize(metric, raw)
    unit = UNITS[metric]
    color = _color(val, metric)
    summary = f"{metric}: {val}{unit}"
    return color, summary, f"{metric}: {val}{unit}"
