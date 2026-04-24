"""Network check: camara 7 via HTTP."""
from ._camara import check_camara

def check_camara7(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 7)
