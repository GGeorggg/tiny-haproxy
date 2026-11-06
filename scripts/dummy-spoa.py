#!/usr/bin/env python3
"""Minimal hand-written SPOE agent for CI smoke tests.

Completes the HELLO handshake and replies to every NOTIFY with an empty ACK,
so HAProxy's `filter spoe` does not block the transaction. Uses only the
Python standard library.

Wire format (SPOP/SPOE, HAProxy 3.x):

  frame         = <len:4B BE> <type:1B> <flags:4B>
                  <stream-id:varint> <frame-id:varint> <payload>

  HELLO payload = (<name-length:varint> <name-bytes> <typed-value>)*
                  (names are NOT typed strings; length is a plain varint)

  typed-value   = <type:1B> <body>
                  STRING  0x08  <len:1B> <bytes>
                  UINT32  0x03  <4B BE>
                  (others per SPOP table)

  HAPROXY-HELLO fields we care about:
    supported-versions   STRING   e.g. "2.0"
    max-frame-size       UINT32
    engine-id            STRING   opaque; must be echoed in AGENT-HELLO

  AGENT-HELLO we send:
    version              STRING   one of supported-versions
    max-frame-size       UINT32   <= HAProxy's value
    capabilities         STRING
    engine-id            STRING   echoed from HELLO

Diagnostics go to stderr (visible via `docker logs`).
"""
import socket
import struct
import sys

# ---- SPOE frame types ----
FRAME_HAPROXY_HELLO  = 1
FRAME_NOTIFY         = 2
FRAME_AGENT_HELLO    = 101
FRAME_AGENT_ACK      = 102
FRAME_AGENT_DISCON   = 103

# ---- SPOE data types (SPOP spec) ----
SPOE_DATA_T_NULL    = 0
SPOE_DATA_T_BOOL    = 1
SPOE_DATA_T_INT32   = 2
SPOE_DATA_T_UINT32  = 3
SPOE_DATA_T_INT64   = 4
SPOE_DATA_T_UINT64  = 5
SPOE_DATA_T_IPV4    = 6
SPOE_DATA_T_IPV6    = 7
SPOE_DATA_T_STRING  = 8
SPOE_DATA_T_BINARY  = 9

_FIXED_LEN = {
    SPOE_DATA_T_NULL:   0,
    SPOE_DATA_T_BOOL:   4,
    SPOE_DATA_T_INT32:  4,
    SPOE_DATA_T_UINT32: 4,
    SPOE_DATA_T_INT64:  8,
    SPOE_DATA_T_UINT64: 8,
    SPOE_DATA_T_IPV4:   4,
    SPOE_DATA_T_IPV6:   16,
}

DEFAULT_MAX_FRAME_SIZE = 16384

# ---------------- varint (unsigned, 7-bit LE) ----------------

def read_varint(data: bytes, off: int):
    shift = 0
    value = 0
    while True:
        if off >= len(data):
            raise IndexError("varint truncated")
        b = data[off]
        off += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, off
        shift += 7


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

# ---------------- typed payload helpers ----------------

def encode_string(s: str) -> bytes:
    raw = s.encode()
    if len(raw) > 255:
        raise ValueError("string too long for single-byte length")
    return bytes([SPOE_DATA_T_STRING, len(raw)]) + raw


def encode_uint32(v: int) -> bytes:
    return bytes([SPOE_DATA_T_UINT32]) + struct.pack(">I", v)


def skip_value(data: bytes, off: int) -> int:
    """Advance past one typed value starting at `off` (type byte at `off`)."""
    if off >= len(data):
        raise IndexError("value truncated")
    vtype = data[off]
    off += 1
    if vtype in _FIXED_LEN:
        return off + _FIXED_LEN[vtype]
    if vtype in (SPOE_DATA_T_STRING, SPOE_DATA_T_BINARY):
        if off >= len(data):
            raise IndexError("length byte truncated")
        l = data[off]
        off += 1
        if off + l > len(data):
            raise IndexError("string bytes run past end")
        return off + l
    raise ValueError(f"unknown SPOE data type {vtype}")

# ---------------- frame construction ----------------

def build_frame(frame_type: int, stream_id: int, frame_id: int, payload: bytes) -> bytes:
    flags = 0
    meta = struct.pack(">I", flags) + encode_varint(stream_id) + encode_varint(frame_id)
    body = bytes([frame_type]) + meta + payload
    return struct.pack(">I", len(body)) + body


def build_agent_hello(version: str, max_frame_size: int, engine_id: str) -> bytes:
    payload  = encode_string("version")        + encode_string(version)
    payload += encode_string("max-frame-size") + encode_uint32(max_frame_size)
    payload += encode_string("capabilities")   + encode_string("pipelining async")
    if engine_id:
        payload += encode_string("engine-id")  + encode_string(engine_id)
    return build_frame(FRAME_AGENT_HELLO, 0, 0, payload)


def build_agent_ack(stream_id: int, frame_id: int) -> bytes:
    return build_frame(FRAME_AGENT_ACK, stream_id, frame_id, b"")

# ---------------- frame reader ----------------

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return bytes(buf)


def recv_frame(sock: socket.socket):
    (length,) = struct.unpack(">I", recv_exact(sock, 4))
    if length == 0 or length > 1024 * 1024:
        raise ValueError(f"implausible frame length {length}")
    body = recv_exact(sock, length)
    if len(body) < 1 + 4:
        raise ValueError("frame too short")
    frame_type = body[0]
    sid, off = read_varint(body, 5)
    fid, off = read_varint(body, off)
    return frame_type, sid, fid, body[off:]

# ---------------- HELLO parsing ----------------

def _parse_kv_name(data: bytes, off: int):
    """Return (name, new_offset). Name = <varint length><bytes>, no type tag."""
    if off >= len(data):
        raise IndexError("name offset out of range")
    name_len, off = read_varint(data, off)
    if off + name_len > len(data):
        raise IndexError("name bytes run past end")
    name = data[off:off + name_len].decode(errors="replace")
    return name, off + name_len


def parse_hello(payload: bytes) -> dict:
    """Parse HAPROXY-HELLO into a dict. Best-effort: logs and returns on error."""
    fields = {}
    off = 0
    try:
        while off < len(payload):
            name, off = _parse_kv_name(payload, off)
            if off >= len(payload):
                break
            vtype = payload[off]
            off += 1
            if vtype == SPOE_DATA_T_STRING:
                if off >= len(payload):
                    break
                l = payload[off]
                off += 1
                if off + l > len(payload):
                    break
                fields[name] = payload[off:off + l].decode(errors="replace")
                off += l
            elif vtype == SPOE_DATA_T_UINT32:
                if off + 4 > len(payload):
                    break
                (val,) = struct.unpack(">I", payload[off:off + 4])
                fields[name] = val
                off += 4
            else:
                off = skip_value(payload, off - 1)
    except (IndexError, ValueError, struct.error) as exc:
        print(f"[dummy-spoa] HELLO parse bailed: {exc}", file=sys.stderr)
    return fields


def pick_version(supported_versions: str) -> str:
    """Pick the highest SPOP version the agent supports from HAProxy's list."""
    tokens = supported_versions.replace(",", " ").split()
    for preferred in ("2.0", "1.0"):
        if preferred in tokens:
            return preferred
    return "1.0"


def extract_max_frame_size(fields: dict) -> int:
    v = fields.get("max-frame-size")
    if isinstance(v, int) and 0 < v < 0x7FFFFFFF:
        return v
    return DEFAULT_MAX_FRAME_SIZE

# ---------------- connection handler ----------------

def handle_conn(conn: socket.socket, peer):
    print(f"[dummy-spoa] accepted {peer}", file=sys.stderr)
    try:
        while True:
            frame_type, sid, fid, payload = recv_frame(conn)
            print(
                f"[dummy-spoa] frame type={frame_type} sid={sid} fid={fid} "
                f"payload_len={len(payload)}",
                file=sys.stderr,
            )

            if frame_type == FRAME_HAPROXY_HELLO:
                fields = parse_hello(payload)
                print(f"[dummy-spoa] HELLO fields: {fields}", file=sys.stderr)
                supported = fields.get("supported-versions", "1.0")
                version = pick_version(supported)
                max_size = extract_max_frame_size(fields)
                engine_id = fields.get("engine-id", "")
                hello = build_agent_hello(version, max_size, engine_id)
                print(
                    f"[dummy-spoa] sending AGENT-HELLO version={version} "
                    f"max-frame-size={max_size} engine-id={engine_id!r} "
                    f"({len(hello)} bytes)",
                    file=sys.stderr,
                )
                conn.sendall(hello)

            elif frame_type == FRAME_NOTIFY:
                ack = build_agent_ack(sid, fid)
                conn.sendall(ack)

            elif frame_type == FRAME_AGENT_DISCON:
                print("[dummy-spoa] peer sent AGENT-DISCONNECT; closing", file=sys.stderr)
                break

            else:
                print(f"[dummy-spoa] unknown frame type {frame_type}; closing",
                      file=sys.stderr)
                break

    except (ConnectionError, ValueError, IndexError, struct.error) as exc:
        print(f"[dummy-spoa] connection ended: {exc}", file=sys.stderr)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"[dummy-spoa] closed {peer}", file=sys.stderr)

# ---------------- main ----------------

def main():
    host = "0.0.0.0"
    port = 9000

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    print(f"[dummy-spoa] listening on {host}:{port}", file=sys.stderr)

    while True:
        conn, peer = srv.accept()
        handle_conn(conn, peer)


if __name__ == "__main__":
    main()