"""Network check: DNS.

Verifica que el servidor DNS del host monitoreado responde, enviando una
consulta UDP directa a host:53 (no al resolver local del servidor spong).
"""

import socket
import struct
import time
from ... import config

_QTYPE_A = 1
_QCLASS_IN = 1
_TID = 0x5060           # transaction id fijo; el puerto de origen lo aleatoriza el SO


def _build_query(qname: str) -> bytes:
    header = struct.pack(">HHHHHH", _TID, 0x0100, 1, 0, 0, 0)  # RD=1, 1 pregunta
    q = b""
    for label in qname.split("."):
        if not label:
            continue
        try:
            enc = label.encode("idna")          # IDN → punycode
        except UnicodeError:
            enc = label.encode("ascii", "ignore")
        enc = enc[:63]                          # límite de etiqueta DNS
        q += bytes([len(enc)]) + enc
    q += b"\x00" + struct.pack(">HH", _QTYPE_A, _QCLASS_IN)
    return header + q


def _dns_query(server_ip: str, qname: str, timeout: int = 5):
    """Envía una query A a server_ip:53. Devuelve (rcode, ancount) o None."""
    packet = _build_query(qname)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(packet, (server_ip, 53))
        data, _ = s.recvfrom(4096)
    if len(data) < 12:
        return None
    r_tid, r_flags = struct.unpack(">HH", data[:4])
    if r_tid != _TID or not (r_flags & 0x8000):   # QR debe estar en 1 (respuesta)
        return None
    ancount = struct.unpack(">H", data[6:8])[0]
    rcode = r_flags & 0x0F
    return rcode, ancount


def check_dns(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host = ips[0] if ips else hostname
    # Nombre a consultar contra el servidor. Configurable; por defecto el propio
    # host (si sirve su zona resolverá, y si no, un NXDOMAIN igual prueba que vive).
    qname = config.get("dns.query_name", hostname)

    t0 = time.time()
    try:
        result = _dns_query(host, qname, timeout=5)
    except socket.timeout:
        return "red", f"dns sin respuesta - {host}", f"timeout consultando {host}:53"
    except OSError as e:
        return "red", f"dns error - {host}", f"{host}:53 -> {e}"
    elapsed = f"{time.time() - t0:.3f}"

    if result is None:
        return "red", f"dns respuesta inválida - {host}", f"respuesta malformada de {host}:53"

    rcode, ancount = result
    if rcode == 0 and ancount > 0:
        return "green", f"dns ok - {qname} vía {host} ({ancount} reg) - {elapsed}s", ""
    if rcode in (0, 3):   # NOERROR sin registros o NXDOMAIN: el servidor está vivo y respondió
        return "green", f"dns responde - {qname} vía {host} (rcode {rcode}) - {elapsed}s", ""
    return "yellow", f"dns rcode {rcode} - {host}", f"{host}:53 respondió rcode {rcode}"
