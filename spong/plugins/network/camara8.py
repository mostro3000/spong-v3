"""Network check: camara 8 via HTTP."""
from ._camara import check_camara

def check_camara8(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 8)
