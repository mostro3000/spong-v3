"""Network check: DVR canal 1."""
from ._dvrcam import check_dvrcam

def check_dvrcam1(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 1)
