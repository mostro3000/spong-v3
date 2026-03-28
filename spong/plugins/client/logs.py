"""Client check: log file monitoring."""

from __future__ import annotations
import re
import time
from pathlib import Path
from ... import config
from ...status_sender import send_status

# State: track (inode, offset) per log file
_log_state: dict[str, dict] = {}


def check_logs(hostname: str) -> None:
    log_checks = config.get_log_checks()
    if not log_checks:
        send_status(hostname, "logs", "green", "no log checks configured", "")
        return

    color = "green"
    issues = []
    details = []
    now = time.time()

    for check_cfg in log_checks:
        logfile = check_cfg.get("logfile", "")
        checks = check_cfg.get("checks", [])
        log_path = Path(logfile)
        if not log_path.exists():
            continue

        try:
            stat = log_path.stat()
            inode = stat.st_ino
            size = stat.st_size
            state = _log_state.get(logfile, {})
            prev_inode = state.get("inode", inode)
            prev_offset = state.get("offset", size)

            # Detect log rotation
            if inode != prev_inode or size < prev_offset:
                prev_offset = 0

            with open(log_path, "r", errors="replace") as f:
                f.seek(prev_offset)
                new_content = f.read()
                new_offset = f.tell()

            _log_state[logfile] = {"inode": inode, "offset": new_offset}

            for chk in checks:
                pattern = chk.get("pattern", "")
                status = chk.get("status", "yellow")
                duration_min = chk.get("duration", 10)
                text_template = chk.get("text", pattern)

                for line in new_content.splitlines():
                    m = re.search(pattern, line)
                    if m:
                        # Substitute capture groups
                        text = text_template
                        for i, grp in enumerate(m.groups(), 1):
                            text = text.replace(f"{{{i}}}", grp or "")
                            text = text.replace(f"${i}", grp or "")
                        issues.append(text)
                        details.append(f"{logfile}: {line}")
                        if status == "red":
                            color = "red"
                        elif status == "yellow" and color != "red":
                            color = "yellow"

        except OSError:
            pass

    if issues:
        summary = "; ".join(issues[:3])
        if len(issues) > 3:
            summary += f" (+{len(issues)-3} more)"
        message = "\n".join(details)
    else:
        summary = "no log issues detected"
        message = ""

    send_status(hostname, "logs", color, summary, message)
