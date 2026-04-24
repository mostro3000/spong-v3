"""Helper compartido para checks de DVR (lee /tmp/<host> generado externamente)."""

import re
import os
from ... import config


def check_dvrcam(hostname: str, n: int) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    tmp_file = f"/tmp/{host}"

    if not os.path.exists(tmp_file):
        return "red", f"dvrcam{n}: archivo {tmp_file} no encontrado", ""

    try:
        # errors='ignore' para descartar bytes binarios del dump de telnet
        content = open(tmp_file, errors='ignore').read()
    except Exception as e:
        return "red", f"dvrcam{n}: error leyendo {tmp_file}: {e}", ""

    # Busca línea "chan N:" (sin subchan) y extrae campo de estado
    # Formato: chan N: bps X, fpsVi X, fps X, lost X, res XxX, signal true/false
    # parts: [chan, N:, bps, val, fpsVi, val, fps, val, lost, val, res, val, signal, true/false]
    #         0     1   2   3    4      5    6   7    8   9    10  11   12   13
    for line in content.splitlines():
        if re.search(rf"\bchan\s+{n}:", line) and "subchan" not in line:
            parts = line.split()
            # Busca el campo 'signal' explícitamente
            try:
                sig_idx = parts.index("signal")
                signal_val = parts[sig_idx + 1] if sig_idx + 1 < len(parts) else ""
            except ValueError:
                signal_val = parts[13] if len(parts) > 13 else ""
            message = line.strip()
            if "false" in signal_val.lower():
                return "red", f"dvrcam{n}: camara desconectada", message
            else:
                return "green", f"dvrcam{n}: camara conectada ({signal_val})", message

    return "yellow", f"dvrcam{n}: canal {n} no encontrado en {tmp_file}", content[:200]
