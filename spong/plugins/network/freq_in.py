"""Network check: UPS APC - freq_in via SNMP."""
from ._ups_snmp import check_ups_metric

def check_freq_in(hostname: str) -> tuple[str, str, str]:
    return check_ups_metric(hostname, "freq_in")
