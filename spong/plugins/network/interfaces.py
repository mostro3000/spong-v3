"""Network check: interfaces de red caídas via SNMP (IF-MIB ifTable).

Detecta interfaces que están administrativamente UP (ifAdminStatus=1)
pero operativamente DOWN (ifOperStatus!=1). Ignora loopback y otras
interfaces configurables en hosts.yaml.

OIDs IF-MIB (1.3.6.1.2.1.2.2.1.*):
  ifDescr:       .2   nombre de la interfaz
  ifAdminStatus: .7   1=up, 2=down, 3=testing
  ifOperStatus:  .8   1=up, 2=down, 3=testing, ...

Configuración opcional en hosts.yaml:
  ignore_interfaces: ["lo", "lo0", "Null0"]
"""

import socket
from .snmp import _build_snmp_request, _parse_snmp_value, _parse_snmp_response_oid
from ... import config

# IF-MIB base OID 1.3.6.1.2.1.2.2.1
_IF_TABLE = [1, 3, 6, 1, 2, 1, 2, 2, 1]
_OID_IF_DESCR        = _IF_TABLE + [2]
_OID_IF_ADMIN_STATUS = _IF_TABLE + [7]
_OID_IF_OPER_STATUS  = _IF_TABLE + [8]

# Interfaces a ignorar por defecto (loopback, etc.)
_DEFAULT_IGNORE = {"lo", "lo0", "loopback", "loopback0", "null0", "null"}

_TIMEOUT = 8


def _snmp_walk_column(host: str, community: str, base_oid: list[int],
                      timeout: int = _TIMEOUT) -> dict[int, bytes]:
    """Walk una columna de la ifTable. Devuelve {ifIndex: raw_value_bytes}."""
    current = base_oid[:]
    result = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        for _ in range(512):
            pkt = _build_snmp_request(community, current, pdu_type=0xA1)
            sock.sendto(pkt, (host, 161))
            resp, _ = sock.recvfrom(4096)
            next_oid = _parse_snmp_response_oid(resp)
            if next_oid is None or next_oid[:len(base_oid)] != base_oid:
                break
            val = _parse_snmp_value(resp)
            if val is not None:
                idx = next_oid[-1]
                result[idx] = val[1]
            current = next_oid
        sock.close()
    except socket.timeout:
        pass
    except Exception:
        pass
    return result


def check_interfaces(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")

    # Interfaces a ignorar: defecto + las del host
    ignore = _DEFAULT_IGNORE.copy()
    extra = host_cfg.get("ignore_interfaces", [])
    ignore.update(i.lower() for i in extra)

    # Walk las 3 columnas
    descrs  = _snmp_walk_column(host, community, _OID_IF_DESCR)
    admins  = _snmp_walk_column(host, community, _OID_IF_ADMIN_STATUS)
    opers   = _snmp_walk_column(host, community, _OID_IF_OPER_STATUS)

    if not descrs:
        return "purple", "interfaces: sin respuesta SNMP", f"No se pudo contactar {host}"

    down = []
    lines = []
    for idx in sorted(descrs):
        name = descrs[idx].decode(errors="replace").strip()
        admin = int.from_bytes(admins.get(idx, b"\x02"), "big")  # default down
        oper  = int.from_bytes(opers.get(idx,  b"\x02"), "big")
        admin_str = {1: "up", 2: "down", 3: "testing"}.get(admin, str(admin))
        oper_str  = {1: "up", 2: "down", 3: "testing", 4: "unknown",
                     5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}.get(oper, str(oper))
        lines.append(f"  {name:<20} admin={admin_str:<8} oper={oper_str}")
        if admin == 1 and oper != 1 and name.lower() not in ignore:
            down.append(name)

    total = len(descrs)
    message = f"Interfaces ({total} total):\n" + "\n".join(lines)

    if down:
        summary = f"{len(down)} interfaz(ces) caída(s): {', '.join(down)}"
        color = "red"
    else:
        summary = f"todas las interfaces up ({total})"
        color = "green"

    return color, summary, message
