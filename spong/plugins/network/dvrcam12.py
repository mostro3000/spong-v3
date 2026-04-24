"""Network check: DVR canal 12."""
from ._dvrcam import check_dvrcam

def check_dvrcam12(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 12)
