"""Network check: DVR canal 15."""
from ._dvrcam import check_dvrcam

def check_dvrcam15(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 15)
