"""Network check: camara 4 via HTTP."""
from ._camara import check_camara

def check_camara4(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 4)
