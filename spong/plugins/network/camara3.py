"""Network check: camara 3 via HTTP."""
from ._camara import check_camara

def check_camara3(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 3)
