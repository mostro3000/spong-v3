"""Network check: Pressure sensor."""

from .temp import check_temp


def check_presion(hostname: str) -> tuple[str, str, str]:
    return check_temp(hostname)
