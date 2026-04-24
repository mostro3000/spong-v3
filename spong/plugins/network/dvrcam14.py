"""Network check: DVR canal 14."""
from ._dvrcam import check_dvrcam

def check_dvrcam14(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 14)
