"""Client check: NTP synchronisation via chrony (chronyc).

Reimplementation of the legacy Perl SPONG plugin ``check_chronyc``. Reports the
chrony system-time offset versus NTP and the state of its time sources.

Enable by adding ``chronyc`` to the host's ``checks:`` list in spong.yaml.
Thresholds are the absolute system-time offset in seconds:

    thresholds.chronyc.warn  (default 0.01)  -> yellow
    thresholds.chronyc.crit  (default 0.1)   -> red

The legacy Perl plugin flagged red at abs(offset) > 0.01; here 0.01 is the
yellow (warn) boundary and red is 0.1 by default. For strict parity set
``thresholds.chronyc.crit: 0.01``.
"""

import re

from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status

_SEV = {"green": 0, "yellow": 1, "red": 2}

# Leap-status values that mean the clock is NOT synchronised (-> red).
# "Insert second"/"Delete second" are normal pending-leap announcements while
# the clock stays synchronised, so they must NOT be treated as an error.
_BAD_LEAP = {"not synchronised", "not synchronized",
             "unsynchronised", "unsynchronized"}

# Error markers that safe_exec injects when a command fails.
_ERR_MARKERS = ("[command not found", "[timeout",
                "Cannot talk to daemon", "506")


def _worse(current: str, candidate: str) -> str:
    """Return the more severe of two colors."""
    return candidate if _SEV[candidate] > _SEV[current] else current


def _parse_tracking(lines: list[str]) -> dict:
    """Extract offset (seconds), leap status and reference id from tracking.

    chrony prints the offset magnitude unsigned and encodes direction in the
    words 'fast'/'slow of NTP time'; we make 'slow' negative so the summary
    sign reflects reality (thresholds use abs()).
    """
    info: dict = {"offset": None, "leap": None, "refid": None}
    for line in lines:
        if line.startswith("System time"):
            # "System time     : 0.000011706 seconds fast of NTP time"
            m = re.search(r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s+seconds", line)
            if m:
                val = float(m.group(1))
                if "slow of NTP" in line:
                    val = -val
                info["offset"] = val
        elif line.startswith("Leap status") and ":" in line:
            info["leap"] = line.split(":", 1)[1].strip()
        elif line.startswith("Reference ID") and ":" in line:
            info["refid"] = line.split(":", 1)[1].strip()
    return info


def _parse_sources(lines: list[str]) -> tuple[int, bool]:
    """Return (number of sources, whether one is the selected '*' source).

    Source rows begin with a mode char (``^`` server, ``=`` peer, ``#`` refclock)
    followed by one of chrony's six documented state chars
    (``*`` selected, ``+`` combined, ``-`` not combined, ``?`` unreachable,
    ``x`` falseticker, ``~`` too variable). The header row (starts with ``M``)
    and the ``=====`` separator (2nd char ``=`` is not a valid state) are thus
    skipped.
    """
    count = 0
    synced = False
    for line in lines:
        if len(line) > 2 and line[0] in "^=#" and line[1] in "*+-?x~":
            count += 1
            if line[1] == "*":
                synced = True
    return count, synced


def _evaluate(tracking_lines: list[str], sources_lines: list[str],
              warn: float, crit: float) -> tuple[str, str]:
    """Pure evaluation: turn chronyc output into (color, summary)."""
    joined = "".join(tracking_lines)
    if not tracking_lines or "[command not found" in joined:
        return "yellow", "chrony no instalado"
    if "[timeout" in joined:
        return "red", "chronyc timeout"

    trk = _parse_tracking(tracking_lines)
    offset = trk["offset"]
    if offset is None:
        if "Cannot talk to daemon" in joined or "506" in joined:
            return "red", "chronyd no responde"
        return "red", "salida de chronyc no reconocida"

    color = "green"
    issues: list[str] = []
    oks: list[str] = []

    absoff = abs(offset)
    if absoff >= crit:
        color = _worse(color, "red")
        issues.append(f"desincronizado (offset={offset:+.3g}s >= {crit}s)")
    elif absoff >= warn:
        color = _worse(color, "yellow")
        issues.append(f"offset alto ({offset:+.3g}s >= {warn}s)")
    else:
        oks.append(f"offset {offset:+.3g}s")

    leap = trk["leap"]
    if leap:
        ll = leap.lower()
        if ll in _BAD_LEAP:
            color = _worse(color, "red")
            issues.append(f"leap: {leap}")
        elif ll != "normal":
            # Insert/Delete second: pending leap announcement, clock still synced
            oks.append(f"leap: {leap}")

    # Sources: distinguish an errored `chronyc sources` call from "no sources".
    joined_src = "".join(sources_lines)
    if any(mark in joined_src for mark in _ERR_MARKERS):
        color = _worse(color, "red")
        issues.append("fuentes no disponibles")
    else:
        n_sources, synced = _parse_sources(sources_lines)
        if n_sources == 0:
            color = _worse(color, "red")
            issues.append("sin fuentes")
        elif not synced:
            color = _worse(color, "yellow")
            issues.append(f"{n_sources} fuentes, ninguna seleccionada")
        else:
            oks.append(f"{n_sources} fuentes")

    if color == "green":
        summary = "sincronizado; " + ", ".join(oks)
    else:
        summary = "; ".join(issues + oks)
    return color, summary


def check_chronyc(hostname: str) -> None:
    cmd = config.get_command("chronyc", "/usr/bin/chronyc")
    warn = float(config.get("thresholds.chronyc.warn", 0.01))
    crit = float(config.get("thresholds.chronyc.crit", 0.1))

    tracking = safe_exec(f"{cmd} tracking", timeout=30)
    sources = safe_exec(f"{cmd} -n sources", timeout=30)

    color, summary = _evaluate(tracking, sources, warn, crit)
    message = "".join(tracking) + "".join(sources)
    send_status(hostname, "chronyc", color, summary, message)
