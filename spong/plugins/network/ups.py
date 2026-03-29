"""Network check: UPS APC via SNMP (PowerNet MIB).

Lee tensión entrada/salida, frecuencia entrada/salida, temperatura batería
y temperatura exterior (sonda opcional) via SNMPv1.

OIDs APC PowerNet MIB (1.3.6.1.4.1.318.1.1.1.*):
  Tensión entrada:      3.2.1.0   (V)
  Tensión salida:       4.2.1.0   (V)
  Frecuencia entrada:   3.2.4.0   (0.1 Hz)
  Frecuencia salida:    4.2.2.0   (0.1 Hz)
  Temperatura batería:  2.2.2.0   (°C)
  Temperatura exterior: 1.3.6.1.4.1.318.1.1.10.2.3.2.1.4.1  (0.1 °C)

Umbrales (Argentina, 220V / 50Hz):
  Tensión:    warn <205 o >230 V  |  crit <195 o >240 V
  Frecuencia: warn <48 o >52 Hz   |  crit <46 o >54 Hz
  Temp bat:   warn >30 °C         |  crit >40 °C
  Temp ext:   warn >30 °C         |  crit >40 °C
"""

from .snmp import snmp_get_int
from ... import config

# APC PowerNet MIB base: 1.3.6.1.4.1.318.1.1.1
_BASE = [1, 3, 6, 1, 4, 1, 318, 1, 1, 1]

_OID_VOLT_IN   = _BASE + [3, 2, 1, 0]
_OID_VOLT_OUT  = _BASE + [4, 2, 1, 0]
_OID_FREQ_IN   = _BASE + [3, 2, 4, 0]   # unidades: 0.1 Hz
_OID_FREQ_OUT  = _BASE + [4, 2, 2, 0]   # unidades: 0.1 Hz
_OID_TEMP_BAT  = _BASE + [2, 2, 2, 0]   # °C
_OID_TEMP_EXT  = [1, 3, 6, 1, 4, 1, 318, 1, 1, 10, 2, 3, 2, 1, 4, 1]  # 0.1 °C

# Umbrales
_VOLT_WARN_LO,  _VOLT_WARN_HI  = 205, 230
_VOLT_CRIT_LO,  _VOLT_CRIT_HI  = 195, 240
_FREQ_WARN_LO,  _FREQ_WARN_HI  = 48,  52
_FREQ_CRIT_LO,  _FREQ_CRIT_HI  = 46,  54
_TEMP_BAT_WARN, _TEMP_BAT_CRIT = 30,  40
_TEMP_EXT_WARN, _TEMP_EXT_CRIT = 30,  40


def _severity(val, warn_lo=None, warn_hi=None, crit_lo=None, crit_hi=None,
              warn_hi_only=None, crit_hi_only=None):
    """Devuelve 'red'/'yellow'/'green' según umbrales."""
    if crit_lo is not None and val < crit_lo:   return "red"
    if crit_hi is not None and val > crit_hi:   return "red"
    if crit_hi_only is not None and val > crit_hi_only: return "red"
    if warn_lo is not None and val < warn_lo:   return "yellow"
    if warn_hi is not None and val > warn_hi:   return "yellow"
    if warn_hi_only is not None and val > warn_hi_only: return "yellow"
    return "green"


def check_ups(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")

    results = {}
    colors = []
    problems = []

    # --- Tensión entrada ---
    v = snmp_get_int(host, community, _OID_VOLT_IN)
    if v is not None:
        results["Vin"] = v
        c = _severity(v, _VOLT_WARN_LO, _VOLT_WARN_HI, _VOLT_CRIT_LO, _VOLT_CRIT_HI)
        colors.append(c)
        if c != "green":
            problems.append(f"Vin={v}V")

    # --- Tensión salida ---
    v = snmp_get_int(host, community, _OID_VOLT_OUT)
    if v is not None:
        results["Vout"] = v
        c = _severity(v, _VOLT_WARN_LO, _VOLT_WARN_HI, _VOLT_CRIT_LO, _VOLT_CRIT_HI)
        colors.append(c)
        if c != "green":
            problems.append(f"Vout={v}V")

    # --- Frecuencia entrada (unidad: 0.1 Hz) ---
    v = snmp_get_int(host, community, _OID_FREQ_IN)
    if v is not None:
        freq = round(v / 10, 1)
        results["Fin"] = freq
        c = _severity(freq, _FREQ_WARN_LO, _FREQ_WARN_HI, _FREQ_CRIT_LO, _FREQ_CRIT_HI)
        colors.append(c)
        if c != "green":
            problems.append(f"Fin={freq}Hz")

    # --- Frecuencia salida (unidad: 0.1 Hz) ---
    v = snmp_get_int(host, community, _OID_FREQ_OUT)
    if v is not None:
        freq = round(v / 10, 1)
        results["Fout"] = freq
        c = _severity(freq, _FREQ_WARN_LO, _FREQ_WARN_HI, _FREQ_CRIT_LO, _FREQ_CRIT_HI)
        colors.append(c)
        if c != "green":
            problems.append(f"Fout={freq}Hz")

    # --- Temperatura batería ---
    v = snmp_get_int(host, community, _OID_TEMP_BAT)
    if v is not None:
        results["Tbat"] = v
        c = _severity(v, warn_hi_only=_TEMP_BAT_WARN, crit_hi_only=_TEMP_BAT_CRIT)
        colors.append(c)
        if c != "green":
            problems.append(f"Tbat={v}°C")

    # --- Temperatura exterior (sonda opcional) ---
    v = snmp_get_int(host, community, _OID_TEMP_EXT)
    if v is not None and v > 0:
        temp = round(v / 10, 1)
        results["Text"] = temp
        c = _severity(temp, warn_hi_only=_TEMP_EXT_WARN, crit_hi_only=_TEMP_EXT_CRIT)
        colors.append(c)
        if c != "green":
            problems.append(f"Text={temp}°C")

    if not results:
        return "red", "ups: sin respuesta SNMP", f"No se pudo contactar {host} via SNMP"

    # Color global: el peor
    if "red" in colors:
        color = "red"
    elif "yellow" in colors:
        color = "yellow"
    else:
        color = "green"

    # Summary
    parts = []
    if "Vin"   in results: parts.append(f"Vin:{results['Vin']}V")
    if "Vout"  in results: parts.append(f"Vout:{results['Vout']}V")
    if "Fin"   in results: parts.append(f"Fin:{results['Fin']}Hz")
    if "Fout"  in results: parts.append(f"Fout:{results['Fout']}Hz")
    if "Tbat"  in results: parts.append(f"Tbat:{results['Tbat']}°C")
    if "Text"  in results: parts.append(f"Text:{results['Text']}°C")

    summary = "  ".join(parts)
    if problems:
        summary += "  ⚠ " + ", ".join(problems)

    message = "\n".join([
        f"Tensión entrada:    {results.get('Vin',  'N/A')} V",
        f"Tensión salida:     {results.get('Vout', 'N/A')} V",
        f"Frecuencia entrada: {results.get('Fin',  'N/A')} Hz",
        f"Frecuencia salida:  {results.get('Fout', 'N/A')} Hz",
        f"Temp. batería:      {results.get('Tbat', 'N/A')} °C",
    ] + ([f"Temp. exterior:     {results['Text']} °C"] if "Text" in results else []))

    return color, summary, message
