"""Network check: camara 6 via HTTP."""
from ._camara import check_camara

def check_camara6(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 6)
