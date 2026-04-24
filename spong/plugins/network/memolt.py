"""Network check: memoria % via SNMP.

Prueba primero el OID TP-Link de TPLINK-SYSMONITOR-MIB y, si no responde,
cae al storage estándar HOST-RESOURCES-MIB (`hrStorageRam` / `main memory`).

Esto permite usar el mismo servicio `mem`/`memolt` tanto en switches TP-Link
como en routers MikroTik RouterOS sin duplicar plugins.
"""

import socket

from ... import config
from .snmp import (
    _build_snmp_request,
    _parse_snmp_response_oid,
    _parse_snmp_value,
    snmp_get_int,
    snmp_get_str,
)

_OID_TPLINK_MEM = [1, 3, 6, 1, 4, 1, 11863, 6, 4, 1, 2, 1, 1, 2, 1]
_OID_SYSDESCR = [1, 3, 6, 1, 2, 1, 1, 1, 0]

_HR_STORAGE = [1, 3, 6, 1, 2, 1, 25, 2, 3, 1]
_OID_HR_TYPE = _HR_STORAGE + [2]
_OID_HR_DESCR = _HR_STORAGE + [3]
_OID_HR_ALLOC = _HR_STORAGE + [4]
_OID_HR_SIZE = _HR_STORAGE + [5]
_OID_HR_USED = _HR_STORAGE + [6]
_OID_HR_RAM = (1, 3, 6, 1, 2, 1, 25, 2, 1, 2)

_WARN = 80   # yellow
_CRIT = 90   # red
_TIMEOUT = 8
_MAX_ENTRIES = 256


def _snmp_walk_column(host: str, community: str, base_oid: list[int],
                      timeout: int = _TIMEOUT) -> dict[int, tuple[int, bytes]]:
    """Walk de una columna SNMP y devuelve {index: (tag, raw_value)}."""
    current = base_oid[:]
    result: dict[int, tuple[int, bytes]] = {}
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        for _ in range(_MAX_ENTRIES):
            pkt = _build_snmp_request(community, current, pdu_type=0xA1)
            sock.sendto(pkt, (host, 161))
            resp, _ = sock.recvfrom(4096)
            next_oid = _parse_snmp_response_oid(resp)
            if next_oid is None or next_oid[:len(base_oid)] != base_oid:
                break
            value = _parse_snmp_value(resp)
            if value is not None:
                result[next_oid[-1]] = value
            current = next_oid
    except Exception:
        return result
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return result


def _decode_int(value: tuple[int, bytes] | None) -> int | None:
    if not value:
        return None
    tag, raw = value
    if tag not in (0x02, 0x41, 0x42, 0x43):
        return None
    return int.from_bytes(raw, "big")


def _decode_str(value: tuple[int, bytes] | None) -> str:
    if not value:
        return ""
    tag, raw = value
    if tag != 0x04:
        return ""
    return raw.decode(errors="replace").strip()


def _decode_oid(value: tuple[int, bytes] | None) -> tuple[int, ...] | None:
    if not value:
        return None
    tag, raw = value
    if tag != 0x06 or not raw:
        return None

    decoded = [raw[0] // 40, raw[0] % 40]
    i = 1
    while i < len(raw):
        part = 0
        while i < len(raw) and raw[i] & 0x80:
            part = (part << 7) | (raw[i] & 0x7F)
            i += 1
        if i >= len(raw):
            return None
        part = (part << 7) | raw[i]
        decoded.append(part)
        i += 1
    return tuple(decoded)


def _fmt_bytes(byte_count: int) -> str:
    gib = 1024 ** 3
    mib = 1024 ** 2
    if byte_count >= gib:
        return f"{byte_count / gib:.1f} GiB"
    return f"{byte_count / mib:.1f} MiB"


def _read_tplink_mem(host: str, community: str) -> tuple[int, str] | None:
    mem = snmp_get_int(host, community, _OID_TPLINK_MEM)
    if mem is None:
        return None
    return mem, "via TP-Link sysMonitor"


def _read_hrstorage_mem(host: str, community: str) -> tuple[int, str] | None:
    types = _snmp_walk_column(host, community, _OID_HR_TYPE)
    descrs = _snmp_walk_column(host, community, _OID_HR_DESCR)
    allocs = _snmp_walk_column(host, community, _OID_HR_ALLOC)
    sizes = _snmp_walk_column(host, community, _OID_HR_SIZE)
    useds = _snmp_walk_column(host, community, _OID_HR_USED)

    candidates = []
    for idx in sorted(set(types) | set(descrs) | set(sizes) | set(useds)):
        size = _decode_int(sizes.get(idx))
        used = _decode_int(useds.get(idx))
        if size is None or used is None or size <= 0:
            continue

        descr = _decode_str(descrs.get(idx))
        ldescr = descr.lower()
        type_oid = _decode_oid(types.get(idx))

        score = 0
        if type_oid == _OID_HR_RAM:
            score += 100
        if "main memory" in ldescr:
            score += 80
        elif "memory" in ldescr or "ram" in ldescr:
            score += 40
        if score == 0:
            continue

        alloc = _decode_int(allocs.get(idx)) or 1
        candidates.append((score, size, idx, descr or f"storage {idx}", alloc, used))

    if not candidates:
        return None

    _score, size, _idx, descr, alloc, used = max(candidates, key=lambda item: (item[0], item[1]))
    pct = int(round(used * 100.0 / size))
    pct = max(0, min(100, pct))
    used_bytes = used * alloc
    size_bytes = size * alloc
    detail = f"{descr} {_fmt_bytes(used_bytes)}/{_fmt_bytes(size_bytes)} via hrStorage"
    return pct, detail


def _check_mem(hostname: str, service_name: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    result = _read_tplink_mem(host, community)
    if result is None:
        result = _read_hrstorage_mem(host, community)

    if result is None:
        descr = snmp_get_str(host, community, _OID_SYSDESCR)
        if descr:
            return "clear", f"{service_name}: N/A (sin memoria SNMP)", descr
        return "red", f"{service_name}: sin respuesta SNMP", f"No se pudo leer memoria de {host}"

    mem, detail = result

    if mem >= _CRIT:
        color = "red"
    elif mem >= _WARN:
        color = "yellow"
    else:
        color = "green"

    return color, f"{service_name}: {mem}%", f"Uso de memoria: {mem}% ({detail})"


def check_memolt(hostname: str) -> tuple[str, str, str]:
    return _check_mem(hostname, "memolt")
