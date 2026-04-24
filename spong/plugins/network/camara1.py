"""Network check: camara 1 via HTTP."""
from ._camara import check_camara

def check_camara1(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 1)
