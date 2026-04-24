"""Network check: DVR canal 7."""
from ._dvrcam import check_dvrcam

def check_dvrcam7(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 7)
