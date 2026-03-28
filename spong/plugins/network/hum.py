"""Network check: Humidity sensor."""

from .temp import check_temp


def check_hum(hostname: str) -> tuple[str, str, str]:
    return check_temp(hostname)
