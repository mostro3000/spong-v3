"""Network check: UPS APC -  via SNMP."""
from ._ups_snmp import check_ups_metric

def check_(hostname: str) -> tuple[str, str, str]:
    return check_ups_metric(hostname, '')
