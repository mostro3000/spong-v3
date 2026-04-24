"""Network check: DVR canal 6."""
from ._dvrcam import check_dvrcam

def check_dvrcam6(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 6)
