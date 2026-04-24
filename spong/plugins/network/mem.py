"""Network check: memoria % via SNMP.

Alias corto de `memolt`. Soporta TP-Link y, por fallback estándar `hrStorage`,
también equipos MikroTik/RouterOS.
"""

from .memolt import _check_mem


def check_mem(hostname: str) -> tuple[str, str, str]:
    return _check_mem(hostname, "mem")
