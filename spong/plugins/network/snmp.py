"""Network check: SNMP."""

import socket
import struct
from ... import config


def _build_snmp_request(community: str, oid: list[int], pdu_type: int = 0xA0) -> bytes:
    """Build a minimal SNMPv1 request PDU. pdu_type: 0xA0=GET, 0xA1=GETNEXT."""
    def encode_oid(oid_list):
        encoded = bytes([40 * oid_list[0] + oid_list[1]])
        for val in oid_list[2:]:
            if val < 128:
                encoded += bytes([val])
            else:
                parts = []
                while val:
                    parts.append(val & 0x7F)
                    val >>= 7
                parts.reverse()
                for i, p in enumerate(parts):
                    encoded += bytes([p | (0x80 if i < len(parts)-1 else 0)])
        return encoded

    def tlv(tag, value):
        length = len(value)
        if length < 128:
            return bytes([tag, length]) + value
        elif length < 256:
            return bytes([tag, 0x81, length]) + value
        else:
            return bytes([tag, 0x82, length >> 8, length & 0xFF]) + value

    oid_tlv = tlv(0x06, encode_oid(oid))
    varbind = tlv(0x30, oid_tlv + b"\x05\x00")
    pdu = tlv(pdu_type, b"\x02\x01\x01" + b"\x02\x01\x00" + b"\x02\x01\x00" + tlv(0x30, varbind))
    return tlv(0x30, b"\x02\x01\x00" + tlv(0x04, community.encode()) + pdu)


def _build_snmp_get_request(community: str, oid: list[int]) -> bytes:
    """Build a minimal SNMPv1 GetRequest PDU."""
    def encode_oid(oid_list: list[int]) -> bytes:
        encoded = bytes([40 * oid_list[0] + oid_list[1]])
        for val in oid_list[2:]:
            if val < 128:
                encoded += bytes([val])
            else:
                parts = []
                while val:
                    parts.append(val & 0x7F)
                    val >>= 7
                parts.reverse()
                for i, p in enumerate(parts):
                    encoded += bytes([p | (0x80 if i < len(parts)-1 else 0)])
        return encoded

    def tlv(tag: int, value: bytes) -> bytes:
        length = len(value)
        if length < 128:
            return bytes([tag, length]) + value
        elif length < 256:
            return bytes([tag, 0x81, length]) + value
        else:
            return bytes([tag, 0x82, length >> 8, length & 0xFF]) + value

    oid_encoded = encode_oid(oid)
    oid_tlv = tlv(0x06, oid_encoded)
    null_tlv = b"\x05\x00"
    varbind = tlv(0x30, oid_tlv + null_tlv)
    varbind_list = tlv(0x30, varbind)

    request_id = b"\x02\x01\x01"  # integer 1
    error_status = b"\x02\x01\x00"
    error_index = b"\x02\x01\x00"
    pdu = tlv(0xA0, request_id + error_status + error_index + varbind_list)

    comm = community.encode()
    community_tlv = tlv(0x04, comm)
    version = b"\x02\x01\x00"  # v1
    return tlv(0x30, version + community_tlv + pdu)


def _parse_snmp_value(response: bytes) -> tuple[int, bytes] | None:
    """Return (tag, raw_value_bytes) of the first VarBind value in an SNMP response."""
    def read_tlv(data, pos):
        tag = data[pos]; pos += 1
        length = data[pos]; pos += 1
        if length & 0x80:
            n = length & 0x7F
            length = int.from_bytes(data[pos:pos+n], 'big'); pos += n
        return tag, data[pos:pos+length], pos+length

    try:
        _, msg, _ = read_tlv(response, 0)
        pos = 0
        _, _, pos = read_tlv(msg, pos)   # version
        _, _, pos = read_tlv(msg, pos)   # community
        _, pdu, _ = read_tlv(msg, pos)   # GetResponse PDU
        pos = 0
        _, _, pos = read_tlv(pdu, pos)   # request-id
        _, _, pos = read_tlv(pdu, pos)   # error-status
        _, _, pos = read_tlv(pdu, pos)   # error-index
        _, vbl, _ = read_tlv(pdu, pos)   # VarBindList
        _, vb, _  = read_tlv(vbl, 0)    # VarBind
        pos = 0
        _, _, pos = read_tlv(vb, pos)    # OID
        tag, val, _ = read_tlv(vb, pos)  # value
        return tag, val
    except Exception:
        return None


def _parse_snmp_int(response: bytes) -> int | None:
    """Extract the first integer value from an SNMP GetResponse."""
    result = _parse_snmp_value(response)
    if result is None:
        return None
    tag, val = result
    if tag in (0x02, 0x41, 0x42, 0x43):  # INTEGER, Counter32, Gauge32, Counter64
        return int.from_bytes(val, 'big')
    return None


def _parse_snmp_response_oid(response: bytes) -> list[int] | None:
    """Extract the OID from the first VarBind in an SNMP response."""
    def read_tlv(data, pos):
        tag = data[pos]; pos += 1
        length = data[pos]; pos += 1
        if length & 0x80:
            n = length & 0x7F
            length = int.from_bytes(data[pos:pos+n], 'big'); pos += n
        return tag, data[pos:pos+length], pos+length

    try:
        _, msg, _ = read_tlv(response, 0)
        pos = 0
        _, _, pos = read_tlv(msg, pos)   # version
        _, _, pos = read_tlv(msg, pos)   # community
        _, pdu, _ = read_tlv(msg, pos)   # PDU
        pos = 0
        _, _, pos = read_tlv(pdu, pos)   # request-id
        _, _, pos = read_tlv(pdu, pos)   # error-status
        _, _, pos = read_tlv(pdu, pos)   # error-index
        _, vbl, _ = read_tlv(pdu, pos)   # VarBindList
        _, vb, _  = read_tlv(vbl, 0)    # VarBind
        _, oid_bytes, _ = read_tlv(vb, 0)
        # Decode OID bytes
        result = []
        first = oid_bytes[0]
        result.append(first // 40)
        result.append(first % 40)
        i = 1
        while i < len(oid_bytes):
            val = 0
            while oid_bytes[i] & 0x80:
                val = (val << 7) | (oid_bytes[i] & 0x7F)
                i += 1
            val = (val << 7) | oid_bytes[i]
            result.append(val)
            i += 1
        return result
    except Exception:
        return None


def snmp_walk_count(host: str, community: str, base_oid: list[int],
                    timeout: int = 10, max_entries: int = 2000) -> int | None:
    """Walk the SNMP subtree rooted at base_oid and return the number of entries."""
    current_oid = base_oid[:]
    count = 0
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        for _ in range(max_entries):
            packet = _build_snmp_request(community, current_oid, pdu_type=0xA1)
            sock.sendto(packet, (host, 161))
            response, _ = sock.recvfrom(4096)
            next_oid = _parse_snmp_response_oid(response)
            if next_oid is None:
                break
            # Stop if the returned OID is outside the base subtree
            if next_oid[:len(base_oid)] != base_oid:
                break
            count += 1
            current_oid = next_oid
        sock.close()
        return count
    except socket.timeout:
        return count if count > 0 else None
    except Exception:
        return None


def snmp_get_int(host: str, community: str, oid: list[int], timeout: int = 5) -> int | None:
    """Send SNMPv1 GET for the given OID and return the integer value, or None."""
    try:
        packet = _build_snmp_get_request(community, oid)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(packet, (host, 161))
        response, _ = sock.recvfrom(4096)
        sock.close()
        return _parse_snmp_int(response)
    except Exception:
        return None


def check_snmp(hostname: str) -> tuple[str, str, str]:
    ips = config.host_ips(hostname)
    host_cfg = config.get_host(hostname) or {}
    community = host_cfg.get("snmp_community", "public")
    host = ips[0] if ips else hostname

    # sysDescr OID: 1.3.6.1.2.1.1.1.0
    oid = [1, 3, 6, 1, 2, 1, 1, 1, 0]
    try:
        packet = _build_snmp_get_request(community, oid)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.sendto(packet, (host, 161))
        response, _ = sock.recvfrom(4096)
        sock.close()
        if not response:
            return "red", "snmp: empty response", ""
        result = _parse_snmp_value(response)
        if result is not None:
            tag, val = result
            if tag == 0x04:  # OCTET STRING
                descr = val.decode(errors="replace").strip()
            elif tag in (0x02, 0x41, 0x42, 0x43):
                descr = str(int.from_bytes(val, 'big'))
            else:
                descr = val.hex()
        else:
            descr = "ok"
        return "green", f"snmp ok - {descr}", descr
    except socket.timeout:
        return "red", f"snmp timeout for {hostname}", ""
    except Exception as e:
        return "red", f"snmp error: {e}", ""
