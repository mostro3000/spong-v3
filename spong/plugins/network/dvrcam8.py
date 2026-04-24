"""Network check: DVR canal 8."""
from ._dvrcam import check_dvrcam

def check_dvrcam8(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 8)
