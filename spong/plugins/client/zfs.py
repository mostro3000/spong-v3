"""Client check: salud de pools ZFS.

Reemplazo del ``check_zfs`` Perl del cliente legacy, que solo miraba si
``zpool status`` decía ``state: ONLINE``. Acá se revisa, por cada pool:

- ``zpool list -H -o name,health,capacity,size,free`` — estado y ocupación.
- ``zpool status`` — estado de cada vdev, contadores READ/WRITE/CKSUM,
  la línea ``errors:`` y el resultado del último scrub/resilver.

Colores:

- pool con health distinto de ONLINE (DEGRADED, FAULTED, UNAVAIL, REMOVED,
  OFFLINE, SUSPENDED) -> red.
- dispositivo de datos en estado no sano, o con READ/WRITE/CKSUM > 0 -> red.
  En dispositivos auxiliares (cache, logs, spares) -> yellow: su pérdida no
  compromete los datos del pool.
- ``errors:`` distinto de "No known data errors" -> red (errores permanentes).
- scrub/resilver terminado con errores -> red; con datos reparados -> yellow;
  resilver en curso -> yellow (redundancia degradada mientras dura).
- ocupación del pool >= ``thresholds.zfs.crit`` (95 por defecto) -> red;
  >= ``thresholds.zfs.warn`` (85) -> yellow. ZFS pierde rendimiento cuando el
  pool se llena, y es independiente del uso por dataset que ve el check disk.
- sin zpool instalado, o sin pools importados -> green (igual que btrfs.py).
- timeout o error al ejecutar zpool -> yellow (síntoma de pool colgado).

La línea ``status:`` de zpool NO define el color: casi siempre es informativa
("Some supported and requested features are not enabled", "non-native block
size") y hacerla fallar pondría en amarillo a pools perfectamente sanos. Los
problemas reales ya aparecen en el health, en los contadores por dispositivo o
en ``errors:``. El texto igual se incluye en el cuerpo del mensaje.

Activar agregando ``zfs`` a la línea ``checks:`` del spong.yaml del host.
"""

from __future__ import annotations

import re

from ... import config
from ...safe_exec import safe_exec
from ...status_sender import send_status

_SEV = {"green": 0, "yellow": 1, "red": 2}

# Estados sanos de un dispositivo dentro de "config:" (AVAIL/INUSE son de spares).
_DEV_OK = {"ONLINE", "AVAIL", "INUSE"}

# Secciones auxiliares: un fallo ahí no pone en riesgo los datos del pool.
_AUX_SECTIONS = {"logs", "cache", "spares"}

_ERR_MARKERS = ("[command not found", "[timeout", "[error:")

# "  disco   ONLINE   0  0  0" con nota opcional ("too many errors").
_DEV_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(.*))?$")

# vdevs contenedores: sus contadores son la suma de los hijos, así que se
# informa su estado pero no sus contadores (si no, cada error sale dos veces).
_CONTAINER_RE = re.compile(
    r"^(mirror|raidz\d*|draid\d*|replacing|spare|indirect)-\d+$")


def _worse(a: str, b: str) -> str:
    return b if _SEV[b] > _SEV[a] else a


def _parse_list(lines: list[str]) -> list[dict]:
    """De ``zpool list -H -o name,health,capacity,size,free`` (separado por tabs)."""
    pools = []
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 2:
            continue
        cap = None
        if len(parts) > 2:
            m = re.match(r"(\d+)%?$", parts[2].strip())
            if m:
                cap = int(m.group(1))
        pools.append({
            "name": parts[0],
            "health": parts[1].upper(),
            "capacity": cap,
            "size": parts[3] if len(parts) > 3 else "",
            "free": parts[4] if len(parts) > 4 else "",
        })
    return pools


def _parse_status(lines: list[str]) -> dict:
    """De ``zpool status``: un dict por pool con estado, errores y dispositivos."""
    pools: dict = {}
    cur: dict | None = None
    in_config = False
    section = "data"

    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()

        m = re.match(r"pool:\s*(\S+)", stripped)
        if m:
            cur = {"state": "", "status": "", "scan": "", "errors": "",
                   "devices": [], "_name": m.group(1)}
            pools[m.group(1)] = cur
            in_config = False
            section = "data"
            continue
        if cur is None:
            continue

        for key in ("state", "status", "scan", "errors", "action"):
            m = re.match(rf"{key}:\s*(.*)", stripped)
            if m:
                if key in cur and not cur[key]:
                    cur[key] = m.group(1).strip()
                in_config = False
                break
        else:
            if stripped.startswith("config:"):
                in_config = True
                section = "data"
                continue
            if in_config:
                if not stripped:
                    continue
                if stripped.split()[0] == "NAME" and "STATE" in stripped:
                    continue
                low = stripped.split()[0].lower()
                if low in _AUX_SECTIONS and len(stripped.split()) == 1:
                    section = low
                    continue
                m = _DEV_RE.match(stripped)
                if m:
                    if m.group(1) == cur["_name"] and not cur["devices"]:
                        # Fila raíz del pool: sus contadores son la suma de los
                        # hijos, contarla duplicaría cada error.
                        continue
                    cur["devices"].append({
                        "name": m.group(1),
                        "state": m.group(2).upper(),
                        "read": int(m.group(3)),
                        "write": int(m.group(4)),
                        "cksum": int(m.group(5)),
                        "note": (m.group(6) or "").strip(),
                        "section": section,
                    })
                elif len(stripped.split()) == 2:
                    # p. ej. "sdc  AVAIL" dentro de spares
                    name, state = stripped.split()
                    cur["devices"].append({
                        "name": name, "state": state.upper(),
                        "read": 0, "write": 0, "cksum": 0,
                        "note": "", "section": section,
                    })
    return pools


def _scan_problems(scan: str) -> tuple[str, str]:
    """(color, detalle) del último scrub/resilver."""
    s = scan.lower()
    if not s or s.startswith("none requested"):
        return "green", ""
    if "resilver in progress" in s or s.startswith("resilver in progress"):
        return "yellow", "resilver en curso"
    m = re.search(r"with (\d+) errors", s)
    if m and int(m.group(1)) > 0:
        return "red", f"scrub con {m.group(1)} errores"
    m = re.search(r"(?:scrub repaired|resilvered)\s+(\S+)", s)
    if m:
        repaired = m.group(1)
        if not re.match(r"^0*b?$", repaired):
            return "yellow", f"reparó {repaired}"
    return "green", ""


def _evaluate(pools: list[dict], warn: int, crit: int) -> tuple[str, str]:
    """pools: lista de dicts con name, health, capacity, state, errors, scan,
    devices y probe_error. Función pura -> (color, summary)."""
    if not pools:
        return "green", "sin pools ZFS"

    color = "green"
    issues: list[str] = []
    oks: list[str] = []

    for p in pools:
        name = p["name"]
        if p.get("probe_error"):
            color = _worse(color, "yellow")
            issues.append(f"{name}: {p['probe_error']}")
            continue

        parts: list[str] = []
        health = (p.get("health") or p.get("state") or "").upper()
        if health and health != "ONLINE":
            color = _worse(color, "red")
            parts.append(health)

        for dev in p.get("devices", []):
            aux = dev["section"] in _AUX_SECTIONS
            sev = "yellow" if aux else "red"
            container = bool(_CONTAINER_RE.match(dev["name"]))
            errs = []
            if dev["state"] not in _DEV_OK:
                # El estado de un contenedor ya se deduce del health del pool;
                # solo se informa si el pool figura sano (caso inesperado).
                if not container or health == "ONLINE":
                    errs.append(dev["state"])
            if not container:
                for counter in ("read", "write", "cksum"):
                    if dev[counter] > 0:
                        errs.append(f"{counter}={dev[counter]}")
            if errs:
                color = _worse(color, sev)
                where = f"{dev['section']}/" if aux else ""
                parts.append(f"{where}{dev['name']} " + " ".join(errs))

        errors = p.get("errors", "")
        if errors and "no known data errors" not in errors.lower():
            color = _worse(color, "red")
            parts.append(errors)

        scan_color, scan_detail = _scan_problems(p.get("scan", ""))
        if scan_detail:
            color = _worse(color, scan_color)
            parts.append(scan_detail)

        cap = p.get("capacity")
        if cap is not None:
            if cap >= crit:
                color = _worse(color, "red")
                parts.append(f"{cap}% ocupado")
            elif cap >= warn:
                color = _worse(color, "yellow")
                parts.append(f"{cap}% ocupado")

        if parts:
            issues.append(f"{name}: " + ", ".join(parts))
        else:
            cap_txt = f", {cap}% ocupado" if cap is not None else ""
            oks.append(f"{name} {health or 'ONLINE'}{cap_txt}")

    return color, "zfs " + "; ".join(issues + oks)


def check_zfs(hostname: str) -> None:
    zpool = config.get_command("zpool", "/usr/sbin/zpool")

    listing = safe_exec(f"{zpool} list -H -o name,health,capacity,size,free", timeout=30)
    joined = "".join(listing)
    if "[command not found" in joined:
        # Fallback por PATH antes de dar por hecho que no hay ZFS.
        listing = safe_exec("zpool list -H -o name,health,capacity,size,free", timeout=30)
        joined = "".join(listing)
        if "[command not found" in joined:
            send_status(hostname, "zfs", "green", "zfs no instalado", "")
            return
        zpool = "zpool"
    if "[timeout" in joined or "[error:" in joined:
        send_status(hostname, "zfs", "yellow", "zfs sin respuesta (timeout/error)", joined)
        return
    if "no pools available" in joined.lower():
        send_status(hostname, "zfs", "green", "sin pools ZFS", "")
        return

    pools = _parse_list(listing)
    if not pools:
        send_status(hostname, "zfs", "green", "sin pools ZFS", "")
        return

    status_lines = safe_exec(f"{zpool} status", timeout=60)
    status_joined = "".join(status_lines)
    if any(marker in status_joined for marker in _ERR_MARKERS):
        for p in pools:
            p["probe_error"] = "zpool status sin respuesta (timeout/error)"
        detail = {}
    else:
        detail = _parse_status(status_lines)

    for p in pools:
        d = detail.get(p["name"])
        if d:
            p.update({k: d[k] for k in ("state", "status", "scan", "errors", "devices")})

    warn = int(config.get_threshold("zfs", "warn", 85))
    crit = int(config.get_threshold("zfs", "crit", 95))
    color, summary = _evaluate(pools, warn, crit)
    send_status(hostname, "zfs", color, summary, joined + "\n" + status_joined)
