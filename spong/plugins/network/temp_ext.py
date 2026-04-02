"""Network check: UPS APC - temp_ext via SNMP."""
from ._ups_snmp import check_ups_metric

def check_temp_ext(hostname: str) -> tuple[str, str, str]:
    return check_ups_metric(hostname, "temp_ext")
