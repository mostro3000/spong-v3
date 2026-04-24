"""Network check: DVR canal 11."""
from ._dvrcam import check_dvrcam

def check_dvrcam11(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 11)
