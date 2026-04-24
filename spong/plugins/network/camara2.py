"""Network check: camara 2 via HTTP."""
from ._camara import check_camara

def check_camara2(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 2)
