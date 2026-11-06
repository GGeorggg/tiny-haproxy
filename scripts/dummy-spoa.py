#!/usr/bin/env python3
"""Minimal hand-written SPOE agent for CI smoke tests.

Completes the HELLO handshake and replies to every NOTIFY with an empty ACK,
so HAProxy's `filter spoe` does not block the transaction. Uses only the
Python standard library.

Wire format (SPOP/SPOE, HAProxy 2.8+):

  frame         = <len:4B BE> <type:1B> <flags:4B>
                  <stream-id:varint> <frame-id:varint> <payload>

  HELLO payload = (<name-length:varint> <name-bytes> <typed-value>)*
                  NOTE: the key name is NOT a typed value (no 0x08 prefix).

  frame types   HAPROXY-HELLO 1, HAPROXY-DISCONNECT 2, NOTIFY 3,
                AGENT-HELLO 101, AGENT-DISCONNECT 102, ACK 103
  flags         every frame we send carries FIN (0x00000001)

  typed-value   = <type:1B> <body>
                  STRING  0x08  <len:varint> <bytes>
                  UINT32  0x03  <varint>       # NOTE: varint, not 4B BE
                  INT32   0x02  <varint>
                  INT64   0x04  <varint>
                  UINT64  0x05  <varint>
                  BOOL    0x01  <4B BE int32>
                  IPV4    0x06  <4B>
                  IPV6    0x07  <16B>

AGENT-HELLO fields we send:
  version        STRING  "2.0"
  max-frame-size UINT32  <varint>
  capabilities   STRING  "" (we are sync-only; no pipelining, no async)

HAProxy identifies engines by matching the engine-id it sent with the one the
agent stored; the agent does NOT echo engine-id in AGENT-HELLO.

Diagnostics go to stderr (visible via `docker logs`).
"""
import socket
import struct
import sys
import threading

# ---- SPOE frame types ----
FRAME_HAPROXY_HELLO  = 1
FRAME_HAPROXY_DISCON = 2
FRAME_NOTIFY         = 3
FRAME_AGENT_HELLO    = 101
FRAME_AGENT_DISCON   = 102
FRAME_AGENT_ACK      = 103

# Frame flags. HAProxy rejects any frame without FIN (no fragmentation support).
FLAG_FIN = 0x00000001

# ---- SPOE data types ----
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

_VARINT_TYPES = (
    SPOE_DATA_T_INT32, SPOE_DATA_T_UINT32,
    SPOE_DATA_T_INT64, SPOE_DATA_T_UINT64,
)

DEFAULT_MAX_FRAME_SIZE = 16384

# ---------------- varint ----------------

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

def encode_name(s: str) -> bytes:
    """KV key: <len:varint><bytes>, with no type byte."""
    raw = s.encode()
    return encode_varint(len(raw)) + raw


def encode_string(s: str) -> bytes:
    """Typed STRING value: <0x08><len:varint><bytes>."""
    raw = s.encode()
    return bytes([SPOE_DATA_T_STRING]) + encode_varint(len(raw)) + raw


def encode_uint32(v: int) -> bytes:
    # HAProxy's spoa.c reads UINT32 body as varint.
    return bytes([SPOE_DATA_T_UINT32]) + encode_varint(v)


def skip_value(data: bytes, off: int) -> int:
    """Advance past one typed value; type byte at `off`."""
    if off >= len(data):
        raise IndexError("value truncated")
    vtype = data[off]
    off += 1
    if vtype in _VARINT_TYPES:
        _, off = read_varint(data, off)
        return off
    if vtype == SPOE_DATA_T_BOOL:
        return off + 4
    if vtype == SPOE_DATA_T_IPV4:
        return off + 4
    if vtype == SPOE_DATA_T_IPV6:
        return off + 16
    if vtype == SPOE_DATA_T_NULL:
        return off
    if vtype in (SPOE_DATA_T_STRING, SPOE_DATA_T_BINARY):
        l, off = read_varint(data, off)
        if off + l > len(data):
            raise IndexError("string bytes run past end")
        return off + l
    raise ValueError(f"unknown SPOE data type {vtype}")

# ---------------- frame construction ----------------

def build_frame(frame_type, stream_id, frame_id, payload, flags=FLAG_FIN):
    meta = struct.pack(">I", flags) + encode_varint(stream_id) + encode_varint(frame_id)
    body = bytes([frame_type]) + meta + payload
    return struct.pack(">I", len(body)) + body


def build_agent_hello(version: str, max_frame_size: int) -> bytes:
    payload  = encode_name("version")        + encode_string(version)
    payload += encode_name("max-frame-size") + encode_uint32(max_frame_size)
    payload += encode_name("capabilities")   + encode_string("")
    return build_frame(FRAME_AGENT_HELLO, 0, 0, payload)


def build_agent_disconnect(status: int, message: str) -> bytes:
    payload  = encode_name("status-code") + bytes([SPOE_DATA_T_UINT32]) + encode_varint(status)
    payload += encode_name("message")     + encode_string(message)
    return build_frame(FRAME_AGENT_DISCON, 0, 0, payload)


def build_agent_ack(stream_id, frame_id):
    return build_frame(FRAME_AGENT_ACK, stream_id, frame_id, b"")

# ---------------- frame reader ----------------

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return bytes(buf)


def recv_frame(sock):
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

def _parse_kv_name(data, off):
    if off >= len(data):
        raise IndexError("name offset out of range")
    name_len, off = read_varint(data, off)
    if off + name_len > len(data):
        raise IndexError("name bytes run past end")
    name = data[off:off + name_len].decode(errors="replace")
    return name, off + name_len


def parse_hello(payload: bytes) -> dict:
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
                l, off = read_varint(payload, off)
                if off + l > len(payload):
                    break
                fields[name] = payload[off:off + l].decode(errors="replace")
                off += l
            elif vtype in _VARINT_TYPES:
                val, off = read_varint(payload, off)
                fields[name] = val
            elif vtype == SPOE_DATA_T_BOOL:
                if off + 4 > len(payload):
                    break
                (val,) = struct.unpack(">i", payload[off:off + 4])
                fields[name] = bool(val)
                off += 4
            else:
                off = skip_value(payload, off - 1)
    except (IndexError, ValueError, struct.error) as exc:
        print(f"[dummy-spoa] HELLO parse bailed: {exc}", file=sys.stderr)
    return fields


def pick_version(supported_versions: str) -> str:
    tokens = supported_versions.replace(",", " ").split()
    for preferred in ("2.0", "1.0"):
        if preferred in tokens:
            return preferred
    return "2.0"


def extract_max_frame_size(fields):
    v = fields.get("max-frame-size")
    if isinstance(v, int) and 0 < v < 0x7FFFFFFF:
        return v
    return DEFAULT_MAX_FRAME_SIZE

# ---------------- connection handler ----------------

def handle_conn(conn, peer):
    print(f"[dummy-spoa] accepted {peer}", file=sys.stderr)
    try:
        while True:
            frame_type, sid, fid, payload = recv_frame(conn)
            print(f"[dummy-spoa] frame type={frame_type} sid={sid} fid={fid} "
                  f"payload_len={len(payload)}", file=sys.stderr)

            if frame_type == FRAME_HAPROXY_HELLO:
                fields = parse_hello(payload)
                print(f"[dummy-spoa] HELLO fields: {fields}", file=sys.stderr)
                version = pick_version(fields.get("supported-versions", "2.0"))
                max_size = extract_max_frame_size(fields)
                hello = build_agent_hello(version, max_size)
                print(f"[dummy-spoa] sending AGENT-HELLO version={version} "
                      f"max-frame-size={max_size} ({len(hello)} bytes)",
                      file=sys.stderr)
                conn.sendall(hello)

            elif frame_type == FRAME_NOTIFY:
                ack = build_agent_ack(sid, fid)
                conn.sendall(ack)

            elif frame_type == FRAME_HAPROXY_DISCON:
                print(f"[dummy-spoa] HAPROXY-DISCONNECT: {parse_hello(payload)}",
                      file=sys.stderr)
                try:
                    conn.sendall(build_agent_disconnect(0, "normal"))
                except OSError:
                    pass
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
        threading.Thread(target=handle_conn, args=(conn, peer),
                         daemon=True).start()


if __name__ == "__main__":
    main()