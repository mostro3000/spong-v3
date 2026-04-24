"""Network check: DVR canal 9."""
from ._dvrcam import check_dvrcam

def check_dvrcam9(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 9)
