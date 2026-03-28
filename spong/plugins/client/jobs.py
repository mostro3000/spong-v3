"""Client check: required processes."""

from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status


def check_jobs(hostname: str) -> None:
    ps_cmd = config.get_command("ps", "/bin/ps ax")
    ps_output = "".join(safe_exec(ps_cmd, timeout=30))

    procs_cfg = config.get_processes()
    crit_list = procs_cfg.get("crit", [])
    warn_list = procs_cfg.get("warn", [])

    color = "green"
    missing_crit = []
    missing_warn = []

    for proc in crit_list:
        if proc not in ps_output:
            color = "red"
            missing_crit.append(proc)

    for proc in warn_list:
        if proc not in ps_output:
            if color != "red":
                color = "yellow"
            missing_warn.append(proc)

    if missing_crit:
        summary = "missing critical: " + ", ".join(missing_crit)
        if missing_warn:
            summary += " | missing warn: " + ", ".join(missing_warn)
    elif missing_warn:
        summary = "missing warn: " + ", ".join(missing_warn)
    else:
        total = len(ps_output.splitlines())
        summary = f"all required processes running ({total} total)"

    send_status(hostname, "jobs", color, summary, ps_output[:2000])
