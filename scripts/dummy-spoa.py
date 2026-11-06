#!/usr/bin/env python3
"""Hand-written minimal SPOE agent: completes HELLO, ACKs NOTIFYs with empty actions."""
import socket
import struct
import sys

# ---- SPOE frame types ----
FRAME_HAPROXY_HELLO = 1
FRAME_NOTIFY = 2
FRAME_AGENT_HELLO = 101
FRAME_AGENT_ACK = 102

# ---- Varint (unsigned, little-endian base-128) ----
def read_varint(data: bytes, off: int):
    """Read a varint, return (value, new_offset)."""
    shift = 0
    value = 0
    while True:
        b = data[off]
        off += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, off
        shift += 7

def encode_varint(value: int) -> bytes:
    """Encode an unsigned int as varint."""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

# ---- Typed data helpers (just enough for HELLO reply) ----
SPOE_DATA_T_STRING = 4
SPOE_DATA_T_UINT32 = 5

def encode_string(s: str) -> bytes:
    raw = s.encode()
    return bytes([SPOE_DATA_T_STRING, len(raw)]) + raw

def encode_uint32(v: int) -> bytes:
    return bytes([SPOE_DATA_T_UINT32]) + struct.pack(">I", v)

# ---- Frame construction ----
def build_frame(frame_type: int, stream_id: int, frame_id: int, payload: bytes) -> bytes:
    """Build a complete SPOE frame with length prefix."""
    flags = 0  # no fragmentation, FIN not needed for single-frame payloads
    meta = struct.pack(">I", flags) + encode_varint(stream_id) + encode_varint(frame_id)
    body = bytes([frame_type]) + meta + payload
    return struct.pack(">I", len(body)) + body

def build_agent_hello(max_frame_size: int) -> bytes:
    """AGENT-HELLO with version 1.0, same max frame size, no capabilities."""
    payload = b""
    payload += encode_string("version") + encode_string("1.0")
    payload += encode_string("max-frame-size") + encode_uint32(max_frame_size)
    payload += encode_string("capabilities") + encode_string("")
    return build_frame(FRAME_AGENT_HELLO, 0, 0, payload)

def build_agent_ack(stream_id: int, frame_id: int) -> bytes:
    """Empty ACK: no actions, HAProxy resumes the stream."""
    return build_frame(FRAME_AGENT_ACK, stream_id, frame_id, b"")

# ---- Frame reader ----
def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf

def recv_frame(sock: socket.socket):
    """Read one frame; return (frame_type, stream_id, frame_id, payload)."""
    len_bytes = recv_exact(sock, 4)
    (length,) = struct.unpack(">I", len_bytes)
    body = recv_exact(sock, length)
    off = 0
    frame_type = body[off]
    off += 1
    # flags: 4 bytes
    off += 4
    stream_id, off = read_varint(body, off)
    frame_id, off = read_varint(body, off)
    return frame_type, stream_id, frame_id, body[off:]

# ---- Connection handler ----
def handle_conn(conn: socket.socket):
    """Process one SPOE connection until close."""
    try:
        while True:
            frame_type, sid, fid, payload = recv_frame(conn)

            if frame_type == FRAME_HAPROXY_HELLO:
                # Extract max-frame-size from the KV-LIST payload.
                # We need to skip through the KV-LIST; for a minimal agent,
                # we can just parse names until we hit "max-frame-size".
                max_size = _extract_max_frame_size(payload)
                conn.sendall(build_agent_hello(max_size))

            elif frame_type == FRAME_NOTIFY:
                # Reply with an empty ACK; no actions taken.
                conn.sendall(build_agent_ack(sid, fid))

            else:
                # Ignore other frame types (e.g. AGENT-DISCONNECT from HAProxy).
                pass

    except (ConnectionError, struct.error, IndexError):
        pass
    finally:
        conn.close()

def _extract_max_frame_size(payload: bytes) -> int:
    """Walk the KV-LIST just enough to find max-frame-size."""
    off = 0
    while off < len(payload):
        # Read KV-NAME (typed string)
        dtype = payload[off]
        off += 1
        if dtype != SPOE_DATA_T_STRING:
            # Unexpected; bail with a safe default
            return 16384
        name_len = payload[off]
        off += 1
        name = payload[off:off + name_len].decode()
        off += name_len

        # Read KV-VALUE
        vtype = payload[off]
        off += 1
        if vtype == SPOE_DATA_T_UINT32:
            (val,) = struct.unpack(">I", payload[off:off + 4])
            off += 4
            if name == "max-frame-size":
                return val
        elif vtype == SPOE_DATA_T_STRING:
            slen = payload[off]
            off += 1
            off += slen
        else:
            # Unknown type; stop parsing
            break
    return 16384  # safe default

def main():
    host = "0.0.0.0"
    port = 9000

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    print(f"dummy-spoa listening on {host}:{port}", file=sys.stderr)

    while True:
        conn, _ = srv.accept()
        handle_conn(conn)

if __name__ == "__main__":
    main()