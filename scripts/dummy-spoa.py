#!/usr/bin/env python3
"""Minimal hand-written SPOE agent for CI smoke tests.

Diagnostic build: dumps the raw HAPROXY-HELLO payload as hex and walks the
KV-LIST while printing every name/value encountered. Always replies with a
valid AGENT-HELLO (max-frame-size 16384) so HAProxy accepts the handshake
even if our KV walker diverges, which keeps the proxy unblocked while we
debug the framing.
"""
import binascii
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

ADVERTISED_MAX_FRAME = 16384  # always reply with this; safe for HAProxy 2.8

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

# ---------------- frame construction ----------------

def build_frame(frame_type: int, stream_id: int, frame_id: int, payload: bytes) -> bytes:
    flags = 0
    meta = struct.pack(">I", flags) + encode_varint(stream_id) + encode_varint(frame_id)
    body = bytes([frame_type]) + meta + payload
    return struct.pack(">I", len(body)) + body


def build_agent_hello(max_frame_size: int) -> bytes:
    payload = b""
    payload += encode_string("version")        + encode_string("1.0")
    payload += encode_string("max-frame-size") + encode_uint32(max_frame_size)
    payload += encode_string("capabilities")   + encode_string("pipelining async")
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

# ---------------- diagnostic HELLO walker ----------------

def dump_hello(payload: bytes, peer):
    print(f"[dummy-spoa] HELLO payload ({len(payload)} bytes) from {peer}:",
          file=sys.stderr)
    print("  " + binascii.hexlify(payload).decode(), file=sys.stderr)
    print("  " + repr(payload), file=sys.stderr)


def walk_hello(payload: bytes):
    """Walk the HELLO payload, returning (max_frame_size, saw_capabilities).

    The HAPROXY-HELLO payload begins with a KV-LIST whose leading varint is
    the number of bytes that follow. Inside, each pair is
    <typed-string-name><typed-value>. Values are typed exactly like the
    SPOP data-type table.
    """
    list_len, off = read_varint(payload, 0)
    end = off + list_len
    print(f"[dummy-spoa] HELLO KV-LIST length={list_len} (payload ends at {end}, "
          f"actual={len(payload)})", file=sys.stderr)
    max_fs = None
    saw_caps = None
    while off < end:
        pair_start = off
        # name
        if payload[off] != SPOE_DATA_T_STRING:
            print(f"[dummy-spoa] HELLO: at offset {off} expected STRING (0x08), "
                  f"got 0x{payload[off]:02x}", file=sys.stderr)
            break
        off += 1
        if off >= end:
            print(f"[dummy-spoa] HELLO: name length byte at {off} runs past end",
                  file=sys.stderr)
            break
        name_len = payload[off]
        off += 1
        if off + name_len > end:
            print(f"[dummy-spoa] HELLO: name bytes {name_len} run past end",
                  file=sys.stderr)
            break
        name = payload[off:off + name_len].decode(errors="replace")
        off += name_len
        # value
        if off >= end:
            print(f"[dummy-spoa] HELLO: value type byte missing after {name!r}",
                  file=sys.stderr)
            break
        vtype = payload[off]
        off += 1
        if vtype == SPOE_DATA_T_STRING:
            if off >= end:
                print(f"[dummy-spoa] HELLO: {name} value length missing",
                      file=sys.stderr)
                break
            vlen = payload[off]
            off += 1
            if off + vlen > end:
                print(f"[dummy-spoa] HELLO: {name} value bytes {vlen} run past end",
                      file=sys.stderr)
                break
            value = payload[off:off + vlen].decode(errors="replace")
            off += vlen
            print(f"[dummy-spoa] HELLO pair {name!r} = STRING({value!r}) "
                  f"[{off - pair_start} bytes]", file=sys.stderr)
            if name == "capabilities":
                saw_caps = value
        elif vtype == SPOE_DATA_T_UINT32:
            if off + 4 > end:
                print(f"[dummy-spoa] HELLO: {name} UINT32 truncated",
                      file=sys.stderr)
                break
            (value,) = struct.unpack(">I", payload[off:off + 4])
            off += 4
            print(f"[dummy-spoa] HELLO pair {name!r} = UINT32({value}) "
                  f"[{off - pair_start} bytes]", file=sys.stderr)
            if name == "max-frame-size":
                max_fs = value
        elif vtype in _FIXED_LEN:
            n = _FIXED_LEN[vtype]
            if off + n > end:
                print(f"[dummy-spoa] HELLO: {name} fixed value truncated",
                      file=sys.stderr)
                break
            off += n
            print(f"[dummy-spoa] HELLO pair {name!r} = type {vtype} "
                  f"[{off - pair_start} bytes]", file=sys.stderr)
        else:
            print(f"[dummy-spoa] HELLO: {name} has unknown type {vtype} "
                  f"at offset {off - 1}", file=sys.stderr)
            break
    if off != end:
        print(f"[dummy-spoa] HELLO walker stopped at {off}, expected {end}",
              file=sys.stderr)
    return max_fs, saw_caps

# ---------------- connection handler ----------------

def handle_conn(conn: socket.socket, peer):
    print(f"[dummy-spoa] accepted {peer}", file=sys.stderr)
    first_hello = True
    try:
        while True:
            frame_type, sid, fid, payload = recv_frame(conn)
            print(
                f"[dummy-spoa] frame type={frame_type} sid={sid} fid={fid} "
                f"payload_len={len(payload)}",
                file=sys.stderr,
            )

            if frame_type == FRAME_HAPROXY_HELLO:
                if first_hello:
                    dump_hello(payload, peer)
                    first_hello = False
                try:
                    max_fs, caps = walk_hello(payload)
                    print(f"[dummy-spoa] HELLO walk result: max-frame-size="
                          f"{max_fs}, capabilities={caps!r}", file=sys.stderr)
                except Exception as exc:
                    print(f"[dummy-spoa] HELLO walker raised: {exc}", file=sys.stderr)
                # Always reply with a valid AGENT-HELLO so the handshake
                # succeeds and HAProxy stops resetting.
                hello = build_agent_hello(ADVERTISED_MAX_FRAME)
                print(f"[dummy-spoa] sending AGENT-HELLO max-frame-size="
                      f"{ADVERTISED_MAX_FRAME} ({len(hello)} bytes)",
                      file=sys.stderr)
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