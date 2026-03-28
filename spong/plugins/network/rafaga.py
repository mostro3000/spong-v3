"""Network check: Wind gust sensor."""

from .temp import check_temp


def check_rafaga(hostname: str) -> tuple[str, str, str]:
    return check_temp(hostname)
