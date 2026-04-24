"""Network check: camara 5 via HTTP."""
from ._camara import check_camara

def check_camara5(hostname: str) -> tuple[str, str, str]:
    return check_camara(hostname, 5)
