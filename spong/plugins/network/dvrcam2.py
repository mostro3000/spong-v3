"""Network check: DVR canal 2."""
from ._dvrcam import check_dvrcam

def check_dvrcam2(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 2)
