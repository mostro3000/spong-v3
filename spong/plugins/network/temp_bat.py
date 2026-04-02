"""Network check: UPS APC - temp_bat via SNMP."""
from ._ups_snmp import check_ups_metric

def check_temp_bat(hostname: str) -> tuple[str, str, str]:
    return check_ups_metric(hostname, "temp_bat")
