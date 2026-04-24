"""Network check: DVR canal 3."""
from ._dvrcam import check_dvrcam

def check_dvrcam3(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 3)
