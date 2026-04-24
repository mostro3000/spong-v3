"""Network check: DVR canal 10."""
from ._dvrcam import check_dvrcam

def check_dvrcam10(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 10)
