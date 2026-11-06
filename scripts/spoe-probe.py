#!/usr/bin/env python3
"""Standalone SPOE HELLO framing probe.

Spawns nothing: assumes an agent is listening on 127.0.0.1:9000 inside the
shared network namespace. Sends a synthetic HAPROXY-HELLO, prints the reply
frame type, and exits non-zero on failure.

Usage: python spoe-probe.py
"""
import socket
import struct
import sys


def ev(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def es(s: str) -> bytes:
    b = s.encode()
    return bytes([8, len(b)]) + b


def eu32(v: int) -> bytes:
    return bytes([3]) + struct.pack(">I", v)


payload  = es("version") + es("1.0")
payload += es("max-frame-size") + eu32(16384)
payload += es("capabilities") + es("pipelining async")
body  = b"\x01" + struct.pack(">I", 0) + ev(0) + ev(0) + payload
frame = struct.pack(">I", len(body)) + body

try:
    s = socket.create_connection(("127.0.0.1", 9000), 2)
    s.settimeout(3)
    s.sendall(frame)
    hdr = s.recv(4, socket.MSG_WAITALL)
    if len(hdr) < 4:
        print("FAIL: short length prefix", file=sys.stderr)
        sys.exit(1)
    (n,) = struct.unpack(">I", hdr)
    body = s.recv(n, socket.MSG_WAITALL)
    if not body:
        print("FAIL: empty body", file=sys.stderr)
        sys.exit(1)
    print(f"reply frame type: {body[0]} len: {n}")
    # AGENT-HELLO = 101
    if body[0] != 101:
        print(f"FAIL: expected AGENT-HELLO (101), got {body[0]}", file=sys.stderr)
        sys.exit(1)
finally:
    try:
        s.close()
    except Exception:
        pass