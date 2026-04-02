"""Network check: UPS APC - freq_out via SNMP."""
from ._ups_snmp import check_ups_metric

def check_freq_out(hostname: str) -> tuple[str, str, str]:
    return check_ups_metric(hostname, "freq_out")
