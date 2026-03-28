"""Network check: Wind sensor."""

from .temp import check_temp


def check_viento(hostname: str) -> tuple[str, str, str]:
    return check_temp(hostname)
