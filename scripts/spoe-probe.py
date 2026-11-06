#!/usr/bin/env python3
"""Standalone SPOP handshake probe (independent of dummy-spoa.py's code).

Plays HAProxy against an agent on 127.0.0.1:9000:
  1. HAPROXY-HELLO  -> expects AGENT-HELLO (101) with FIN, valid version,
                       max-frame-size, capabilities
  2. NOTIFY         -> expects ACK (103) with the same stream/frame id
Exits non-zero on any deviation from the spec.
"""
import socket
import struct
import sys

HAPROXY_HELLO, NOTIFY, AGENT_HELLO, ACK = 1, 3, 101, 103
FIN = 1


def ev(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def rv(d, o):
    v = s = 0
    while True:
        b = d[o]; o += 1
        v |= (b & 0x7F) << s
        if not b & 0x80:
            return v, o
        s += 7


def name(s):                       # KV key: varint len + bytes, no type byte
    b = s.encode()
    return ev(len(b)) + b


def sval(s):                       # typed STRING
    b = s.encode()
    return bytes([8]) + ev(len(b)) + b


def uval(v):                       # typed UINT32 (varint body)
    return bytes([3]) + ev(v)


def frame(ftype, sid, fid, payload, flags=FIN):
    body = bytes([ftype]) + struct.pack(">I", flags) + ev(sid) + ev(fid) + payload
    return struct.pack(">I", len(body)) + body


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("peer closed connection")
        buf += c
    return buf


def recv_frame(s):
    (n,) = struct.unpack(">I", recv_exact(s, 4))
    body = recv_exact(s, n)
    ftype = body[0]
    (flags,) = struct.unpack(">I", body[1:5])
    sid, o = rv(body, 5)
    fid, o = rv(body, o)
    return ftype, flags, sid, fid, body[o:]


def parse_kv(p):
    out, o = {}, 0
    while o < len(p):
        l, o = rv(p, o)
        k = p[o:o + l].decode(); o += l
        t = p[o] & 0x0F; o += 1
        if t == 8:
            l, o = rv(p, o)
            out[k] = p[o:o + l].decode(); o += l
        elif t in (2, 3, 4, 5):
            out[k], o = rv(p, o)
        else:
            raise ValueError(f"unexpected type {t} for {k}")
    return out


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


s = socket.create_connection(("127.0.0.1", 9000), 2)
s.settimeout(3)
try:
    hello = (name("supported-versions") + sval("2.0")
             + name("max-frame-size") + uval(16384)
             + name("capabilities") + sval("pipelining,async")
             + name("engine-id") + sval("probe"))
    s.sendall(frame(HAPROXY_HELLO, 0, 0, hello))

    ftype, flags, _, _, payload = recv_frame(s)
    if ftype != AGENT_HELLO:
        fail(f"expected AGENT-HELLO ({AGENT_HELLO}), got {ftype}")
    if not flags & FIN:
        fail("AGENT-HELLO missing FIN flag")
    kv = parse_kv(payload)
    for k in ("version", "max-frame-size", "capabilities"):
        if k not in kv:
            fail(f"AGENT-HELLO missing '{k}': {kv}")
    if kv["version"] != "2.0":
        fail(f"unexpected version {kv['version']!r}")
    print(f"AGENT-HELLO ok: {kv}")

    s.sendall(frame(NOTIFY, 7, 9, b""))
    ftype, flags, sid, fid, _ = recv_frame(s)
    if ftype != ACK or (sid, fid) != (7, 9) or not flags & FIN:
        fail(f"bad ACK: type={ftype} flags={flags} sid={sid} fid={fid}")
    print("ACK ok")
except (OSError, ValueError, IndexError) as exc:
    fail(str(exc))
finally:
    s.close()
