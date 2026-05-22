"""Network check: traffic/utilization per interface via SNMP IF-MIB.

Calcula trafico promedio entre muestras usando contadores SNMP y reporta
la interfaz con mayor utilizacion. No requiere librerias externas.

Configuracion opcional en hosts.yaml:
  iftraffic_interfaces: ["ether1", "bridge*", "sfp-sfpplus1"]
  iftraffic_ignore: ["lo", "veth*", "pppoe-*"]

Tambien reutiliza `ignore_interfaces` si existe.

Umbrales opcionales en spong.yaml:
  thresholds:
    iftraffic:
      warn: 70
      crit: 90
"""

from __future__ import annotations

import fnmatch
import json
import re
import socket
import time
from pathlib import Path

from ... import config
from .snmp import _build_snmp_request, _parse_snmp_response_oid, _parse_snmp_value

_IF_TABLE = [1, 3, 6, 1, 2, 1, 2, 2, 1]
_IFX_TABLE = [1, 3, 6, 1, 2, 1, 31, 1, 1, 1]

_OID_IF_DESCR = _IF_TABLE + [2]
_OID_IF_SPEED = _IF_TABLE + [5]
_OID_IF_ADMIN = _IF_TABLE + [7]
_OID_IF_OPER = _IF_TABLE + [8]
_OID_IF_IN32 = _IF_TABLE + [10]
_OID_IF_OUT32 = _IF_TABLE + [16]

_OID_IF_NAME = _IFX_TABLE + [1]
_OID_IF_HC_IN = _IFX_TABLE + [6]
_OID_IF_HC_OUT = _IFX_TABLE + [10]
_OID_IF_HIGH_SPEED = _IFX_TABLE + [15]

_DEFAULT_IGNORE = ["lo", "lo0", "loopback*", "null*", "veth*"]
_TIMEOUT = 8
_MAX_ENTRIES = 512
_MIN_DELTA = 30
_STALE_DELTA = 7200
_META_TTL = 6 * 3600
_STATE_VERSION = 2
_COUNTER_HC = "hc"
_COUNTER_32 = "32"


def _snmp_walk_column(host: str, community: str, base_oid: list[int],
                      timeout: int = _TIMEOUT) -> dict[int, tuple[int, bytes]]:
    """Walk una columna SNMP y devuelve {ifIndex: (tag, raw_value)}."""
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
    _tag, raw = value
    if not raw:
        return 0
    return int.from_bytes(raw, "big")


def _decode_str(value: tuple[int, bytes] | None) -> str:
    if not value:
        return ""
    _tag, raw = value
    return raw.decode(errors="replace").strip()


def _patterns(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [x for x in re.split(r"[\s,]+", value) if x]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()]


def _matches(name: str, patterns: list[str]) -> bool:
    lname = name.lower()
    return any(fnmatch.fnmatch(lname, p.lower()) for p in patterns)


def _state_path(hostname: str) -> Path:
    path = Path(config.tmp_path()) / "iftraffic"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{hostname}.json"


def _load_state(hostname: str) -> dict:
    path = _state_path(hostname)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(hostname: str, data: dict) -> None:
    path = _state_path(hostname)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True))
        tmp.replace(path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _config_signature(community: str, include: list[str], ignore: list[str]) -> str:
    return json.dumps({
        "community": community,
        "include": include,
        "ignore": ignore,
    }, sort_keys=True)


def _load_meta(state: dict, signature: str, now: float) -> dict | None:
    meta = state.get("meta")
    if not isinstance(meta, dict):
        return None
    if meta.get("version") != _STATE_VERSION:
        return None
    if meta.get("signature") != signature:
        return None
    meta_ts = float(meta.get("timestamp", 0) or 0)
    if not meta_ts or (now - meta_ts) > _META_TTL:
        return None
    interfaces = meta.get("interfaces")
    if not isinstance(interfaces, dict):
        return None
    return meta


def _select_interfaces(
    names: dict[int, tuple[int, bytes]],
    descrs: dict[int, tuple[int, bytes]],
    speeds_hi: dict[int, tuple[int, bytes]],
    speeds32: dict[int, tuple[int, bytes]],
    include: list[str],
    ignore: list[str],
) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    indexes = sorted(set(names) | set(descrs) | set(speeds_hi) | set(speeds32))
    for idx in indexes:
        name = _decode_str(names.get(idx)) or _decode_str(descrs.get(idx)) or f"if{idx}"
        lname = name.lower()
        if include and not _matches(lname, include):
            continue
        if ignore and _matches(lname, ignore):
            continue
        speed_mbps = _decode_int(speeds_hi.get(idx))
        if not speed_mbps:
            speed_bps = _decode_int(speeds32.get(idx))
            speed_mbps = round(speed_bps / 1_000_000, 1) if speed_bps else None
        selected[str(idx)] = {
            "name": name,
            "speed_mbps": speed_mbps,
        }
    return selected


def _assign_counter_modes(
    selected: dict[str, dict],
    in64: dict[int, tuple[int, bytes]],
    out64: dict[int, tuple[int, bytes]],
    in32: dict[int, tuple[int, bytes]],
    out32: dict[int, tuple[int, bytes]],
) -> dict[str, dict]:
    assigned: dict[str, dict] = {}
    for idx_str, row in selected.items():
        idx = int(idx_str)
        has_hc = _decode_int(in64.get(idx)) is not None and _decode_int(out64.get(idx)) is not None
        has_32 = _decode_int(in32.get(idx)) is not None and _decode_int(out32.get(idx)) is not None
        if has_hc:
            counter = _COUNTER_HC
        elif has_32:
            counter = _COUNTER_32
        else:
            continue
        assigned[idx_str] = {
            "name": row["name"],
            "speed_mbps": row.get("speed_mbps"),
            "counter": counter,
        }
    return assigned


def _counter_groups(selected: dict[str, dict]) -> tuple[bool, bool]:
    need_hc = False
    need_32 = False
    for row in selected.values():
        counter = row.get("counter")
        if counter == _COUNTER_HC:
            need_hc = True
        elif counter == _COUNTER_32:
            need_32 = True
    return need_hc, need_32


def _fmt_speed(mbps: float | None) -> str:
    if mbps is None:
        return "?"
    if mbps >= 1000:
        return f"{mbps / 1000:.1f}Gbps"
    return f"{mbps:.0f}Mbps"


def _fmt_util(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.1f}%"


def check_iftraffic(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")

    warn = float(config.get_threshold("iftraffic", "warn", 70))
    crit = float(config.get_threshold("iftraffic", "crit", 90))

    include = _patterns(host_cfg.get("iftraffic_interfaces"))
    ignore = _DEFAULT_IGNORE + _patterns(host_cfg.get("ignore_interfaces"))
    ignore += _patterns(host_cfg.get("iftraffic_ignore"))

    prev = _load_state(hostname)
    prev_ts = float(prev.get("timestamp", 0) or 0)
    now = time.time()
    delta_t = now - prev_ts if prev_ts else 0
    prev_ifs = prev.get("interfaces", {})
    if not isinstance(prev_ifs, dict):
        prev_ifs = {}

    signature = _config_signature(community, include, ignore)
    meta = _load_meta(prev, signature, now)
    selected_meta: dict[str, dict]
    opers: dict[int, tuple[int, bytes]]
    in64: dict[int, tuple[int, bytes]] = {}
    out64: dict[int, tuple[int, bytes]] = {}
    in32: dict[int, tuple[int, bytes]] = {}
    out32: dict[int, tuple[int, bytes]] = {}
    meta_dirty = False

    if meta is None:
        names = _snmp_walk_column(host, community, _OID_IF_NAME)
        descrs = _snmp_walk_column(host, community, _OID_IF_DESCR)
        if not names and not descrs:
            return "purple", f"iftraffic: sin respuesta SNMP de {hostname}", ""
        opers = _snmp_walk_column(host, community, _OID_IF_OPER)
        speeds32 = _snmp_walk_column(host, community, _OID_IF_SPEED)
        speeds_hi = _snmp_walk_column(host, community, _OID_IF_HIGH_SPEED)
        in64 = _snmp_walk_column(host, community, _OID_IF_HC_IN)
        out64 = _snmp_walk_column(host, community, _OID_IF_HC_OUT)
        in32 = _snmp_walk_column(host, community, _OID_IF_IN32)
        out32 = _snmp_walk_column(host, community, _OID_IF_OUT32)
        selected_meta = _select_interfaces(names, descrs, speeds_hi, speeds32, include, ignore)
        selected_meta = _assign_counter_modes(selected_meta, in64, out64, in32, out32)
        meta = {
            "version": _STATE_VERSION,
            "timestamp": now,
            "signature": signature,
            "interfaces": selected_meta,
        }
    else:
        selected_meta = meta.get("interfaces", {})
        opers = _snmp_walk_column(host, community, _OID_IF_OPER)
        need_hc, need_32 = _counter_groups(selected_meta)
        if need_hc:
            in64 = _snmp_walk_column(host, community, _OID_IF_HC_IN)
            out64 = _snmp_walk_column(host, community, _OID_IF_HC_OUT)
            if (not in64 and not out64) and not need_32:
                in32 = _snmp_walk_column(host, community, _OID_IF_IN32)
                out32 = _snmp_walk_column(host, community, _OID_IF_OUT32)
                if in32 or out32:
                    for row in selected_meta.values():
                        row["counter"] = _COUNTER_32
                    meta_dirty = True
        if need_32:
            in32 = _snmp_walk_column(host, community, _OID_IF_IN32)
            out32 = _snmp_walk_column(host, community, _OID_IF_OUT32)
            if (not in32 and not out32) and not need_hc:
                in64 = _snmp_walk_column(host, community, _OID_IF_HC_IN)
                out64 = _snmp_walk_column(host, community, _OID_IF_HC_OUT)
                if in64 or out64:
                    for row in selected_meta.values():
                        row["counter"] = _COUNTER_HC
                    meta_dirty = True

        if not opers and not in64 and not out64 and not in32 and not out32:
            return "purple", f"iftraffic: sin respuesta SNMP de {hostname}", ""

    selected = []
    snapshot_items = {}
    down_oper = []

    for idx_str, meta_row in selected_meta.items():
        idx = int(idx_str)
        name = meta_row.get("name") or f"if{idx}"
        oper = _decode_int(opers.get(idx)) or 2
        counter = meta_row.get("counter", _COUNTER_HC)

        if counter == _COUNTER_32:
            current_in = _decode_int(in32.get(idx))
            current_out = _decode_int(out32.get(idx))
        else:
            current_in = _decode_int(in64.get(idx))
            current_out = _decode_int(out64.get(idx))

        if current_in is None or current_out is None:
            continue

        snapshot_items[idx_str] = {
            "name": name,
            "in": current_in,
            "out": current_out,
        }

        if oper != 1:
            down_oper.append(name)
            continue

        prev_item = prev_ifs.get(idx_str)
        if not isinstance(prev_item, dict) or delta_t < _MIN_DELTA or delta_t > _STALE_DELTA:
            continue

        prev_in = prev_item.get("in")
        prev_out = prev_item.get("out")
        if not isinstance(prev_in, int) or not isinstance(prev_out, int):
            continue
        if current_in < prev_in or current_out < prev_out:
            continue

        speed_mbps = meta_row.get("speed_mbps")
        in_mbps = (current_in - prev_in) * 8.0 / delta_t / 1_000_000
        out_mbps = (current_out - prev_out) * 8.0 / delta_t / 1_000_000
        util_in = (in_mbps / speed_mbps * 100.0) if speed_mbps else None
        util_out = (out_mbps / speed_mbps * 100.0) if speed_mbps else None
        max_util = max(v for v in (util_in, util_out) if v is not None) if (util_in is not None or util_out is not None) else None

        selected.append({
            "name": name,
            "in_mbps": in_mbps,
            "out_mbps": out_mbps,
            "util_in": util_in,
            "util_out": util_out,
            "max_util": max_util,
            "speed_mbps": speed_mbps,
        })

    if meta_dirty:
        meta["timestamp"] = now
    meta["interfaces"] = selected_meta

    _save_state(hostname, {
        "timestamp": now,
        "interfaces": snapshot_items,
        "meta": meta,
    })

    if not snapshot_items:
        return "clear", "iftraffic: sin interfaces aplicables", "No hay interfaces SNMP seleccionadas para monitorear"

    if not selected:
        lines = [f"Primera muestra tomada para {len(snapshot_items)} interfaces monitoreadas."]
        if include:
            lines.append("Filtro include: " + ", ".join(include))
        if down_oper:
            lines.append("Oper down: " + ", ".join(sorted(down_oper)))
        lines.append("Esperando la siguiente corrida para calcular trafico promedio.")
        return "clear", "iftraffic: esperando segunda muestra", "\n".join(lines)

    selected.sort(key=lambda row: ((row["max_util"] if row["max_util"] is not None else -1), row["in_mbps"] + row["out_mbps"]), reverse=True)
    hottest = selected[0]
    hot_count = sum(1 for row in selected if row["max_util"] is not None and row["max_util"] >= warn)
    total_in = sum(row["in_mbps"] for row in selected)
    total_out = sum(row["out_mbps"] for row in selected)

    color = "green"
    max_util = hottest["max_util"]
    if max_util is not None and max_util >= crit:
        color = "red"
    elif max_util is not None and max_util >= warn:
        color = "yellow"

    if max_util is None:
        summary = (
            f"iftraffic: {hottest['name']} in {hottest['in_mbps']:.1f} out {hottest['out_mbps']:.1f} Mbps"
        )
    else:
        summary = (
            f"iftraffic: {hottest['name']} {_fmt_util(max_util)} max "
            f"(in {hottest['in_mbps']:.1f} out {hottest['out_mbps']:.1f} Mbps)"
        )
    if hot_count > 1:
        summary += f", {hot_count} altas"

    lines = [
        f"Delta: {delta_t:.1f}s",
        f"Total monitorizado: in {total_in:.1f} Mbps, out {total_out:.1f} Mbps",
        f"Umbrales: warn {warn:.0f}% crit {crit:.0f}%",
        "",
        "Interfaces:",
    ]
    for row in selected:
        lines.append(
            f"  {row['name']:<18} "
            f"in {row['in_mbps']:7.1f} Mbps  out {row['out_mbps']:7.1f} Mbps  "
            f"util { _fmt_util(row['util_in']) }/{ _fmt_util(row['util_out']) }  "
            f"speed {_fmt_speed(row['speed_mbps'])}"
        )
    if down_oper:
        lines.extend([
            "",
            "Oper down:",
            "  " + ", ".join(sorted(down_oper)),
        ])

    return color, summary, "\n".join(lines)
