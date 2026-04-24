"""Network check: cámara  via HTTP."""
from ._camara import check_camara

def check_camara(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, )
