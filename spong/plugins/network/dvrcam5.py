"""Network check: DVR canal 5."""
from ._dvrcam import check_dvrcam

def check_dvrcam5(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 5)
