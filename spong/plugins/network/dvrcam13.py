"""Network check: DVR canal 13."""
from ._dvrcam import check_dvrcam

def check_dvrcam13(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 13)
