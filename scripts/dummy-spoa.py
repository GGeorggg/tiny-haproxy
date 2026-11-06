#!/usr/bin/env python3
"""Minimal hand-written SPOE agent for CI smoke tests.

Completes the HELLO handshake and replies to every NOTIFY with an empty ACK,
so HAProxy's `filter spoe` does not block the transaction. Uses only the
Python standard library.

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

# Map fixed-size type -> byte length, for skipping unknown values safely.
_FIXED_LEN = {
    SPOE_DATA_T_NULL:   0,
    SPOE_DATA_T_BOOL:   4,   # sent as int32 on the wire
    SPOE_DATA_T_INT32:  4,
    SPOE_DATA_T_UINT32: 4,
    SPOE_DATA_T_INT64:  8,
    SPOE_DATA_T_UINT64: 8,
    SPOE_DATA_T_IPV4:   4,
    SPOE_DATA_T_IPV6:   16,
}

DEFAULT_MAX_FRAME_SIZE = 16384

# ---------------- varint (unsigned, 7-bit little endian) ----------------

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


def skip_value(data: bytes, off: int):
    """Given a typed value at `off`, return the offset just after it."""
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
        return off + l
    raise ValueError(f"unknown SPOE data type {vtype}")

# ---------------- frame construction ----------------

def build_frame(frame_type: int, stream_id: int, frame_id: int, payload: bytes) -> bytes:
    flags = 0
    meta = struct.pack(">I", flags) + encode_varint(stream_id) + encode_varint(frame_id)
    body = bytes([frame_type]) + meta + payload
    return struct.pack(">I", len(body)) + body


def build_agent_hello(max_frame_size: int) -> bytes:
    # Must not exceed what HAProxy offered.
    payload = b""
    payload += encode_string("version")       + encode_string("1.0")
    payload += encode_string("max-frame-size") + encode_uint32(max_frame_size)
    payload += encode_string("capabilities")   + encode_string("")
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
    # body[1:5] = flags
    sid, off = read_varint(body, 5)
    fid, off = read_varint(body, off)
    return frame_type, sid, fid, body[off:]

# ---------------- HELLO parsing ----------------

def extract_max_frame_size(payload: bytes) -> int:
    """Walk the KV-LIST at the start of HAPROXY-HELLO and find max-frame-size."""
    off = 0
    try:
        while off < len(payload):
            name, off = _parse_kv_name(payload, off)
            if name == "max-frame-size":
                # value follows
                if off >= len(payload):
                    return DEFAULT_MAX_FRAME_SIZE
                vtype = payload[off]
                off += 1
                if vtype == SPOE_DATA_T_UINT32:
                    if off + 4 > len(payload):
                        return DEFAULT_MAX_FRAME_SIZE
                    (val,) = struct.unpack(">I", payload[off:off + 4])
                    return val
                # wrong type; skip and keep looking
            off = skip_value(payload, off)
    except (IndexError, ValueError) as exc:
        print(f"[dummy-spoa] HELLO parse failed: {exc}", file=sys.stderr)
    return DEFAULT_MAX_FRAME_SIZE


def _parse_kv_name(data: bytes, off: int):
    """Return (name, new_offset) for a typed string at `off`."""
    if off >= len(data):
        raise IndexError("name offset out of range")
    vtype = data[off]
    off += 1
    if vtype != SPOE_DATA_T_STRING:
        raise ValueError(f"expected string key, got type {vtype}")
    if off >= len(data):
        raise IndexError("name length out of range")
    l = data[off]
    off += 1
    name = data[off:off + l].decode(errors="replace")
    return name, off + l

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
                max_size = extract_max_frame_size(payload)
                # Clamp to a sane value; must be <= HAProxy's own max-frame-size,
                # which we cannot know beyond what we parsed.
                if max_size < 256:
                    max_size = DEFAULT_MAX_FRAME_SIZE
                hello = build_agent_hello(max_size)
                print(
                    f"[dummy-spoa] sending AGENT-HELLO max-frame-size={max_size} "
                    f"({len(hello)} bytes)",
                    file=sys.stderr,
                )
                conn.sendall(hello)

            elif frame_type == FRAME_NOTIFY:
                ack = build_agent_ack(sid, fid)
                print(f"[dummy-spoa] sending AGENT-ACK ({len(ack)} bytes)", file=sys.stderr)
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
        # Single-threaded on purpose: HAProxy opens at most one long-lived
        # connection per agent, plus health checks that close immediately.
        handle_conn(conn, peer)


if __name__ == "__main__":
    main()