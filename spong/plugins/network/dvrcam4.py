"""Network check: DVR canal 4."""
from ._dvrcam import check_dvrcam

def check_dvrcam4(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 4)
