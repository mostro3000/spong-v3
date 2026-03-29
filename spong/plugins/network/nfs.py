"""Network check: disponibilidad NFS via rpcinfo.

Verifica que el servidor NFS tenga nfs y mountd registrados en el portmapper.
Usa `rpcinfo -p HOST` y busca los programas nfs (100003) y mountd (100005).
"""

import subprocess
from ... import config

_TIMEOUT = 15


def check_nfs(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname

    try:
        result = subprocess.run(
            ["rpcinfo", "-p", host],
            capture_output=True, text=True,
            timeout=_TIMEOUT,
        )
        output = result.stdout + result.stderr
    except FileNotFoundError:
        return "red", "nfs: rpcinfo no encontrado", "Instalar: apt install rpcbind"
    except subprocess.TimeoutExpired:
        return "red", f"nfs: timeout ({_TIMEOUT}s)", f"rpcinfo -p {host} no respondió"
    except Exception as e:
        return "red", f"nfs: error: {e}", ""

    has_nfs    = "nfs"    in output.lower()
    has_mountd = "mountd" in output.lower()

    if has_nfs and has_mountd:
        return "green", "nfs ok  (nfs + mountd)", output.strip()
    elif has_nfs:
        return "yellow", "nfs: mountd no responde", output.strip()
    elif has_mountd:
        return "yellow", "nfs: nfsd no responde", output.strip()
    else:
        return "red", "nfs down  (sin nfs ni mountd)", output.strip() or f"rpcinfo -p {host} sin respuesta"
