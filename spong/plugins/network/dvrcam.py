"""Network check: DVR canal ."""
from ._dvrcam import check_dvrcam

def check_dvrcam(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, )
