"""Network check: UPS APC - volt_in via SNMP."""
from ._ups_snmp import check_ups_metric

def check_volt_in(hostname: str) -> tuple[str, str, str]:
    return check_ups_metric(hostname, "volt_in")
