"""Network check: DVR canal 16."""
from ._dvrcam import check_dvrcam

def check_dvrcam16(hostname: str) -> tuple[str, str, str]:
    return check_dvrcam(hostname, 16)
