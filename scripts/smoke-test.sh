#!/usr/bin/env bash
# Behavioural smoke test of the built image (needs Docker, no real certs/backends).
# Usage: scripts/smoke-test.sh [image]   (default: tiny-haproxy:dev)
# All containers share ONE network namespace so 127.0.0.1:8080 (backend) works as configured.
set -euo pipefail

IMAGE="${1:-tiny-haproxy:dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CERTS="$(mktemp -d)"
WL="$(mktemp -d)"
# mktemp -d is mode 0700; curlimages/curl runs as a non-root user and must be
# able to read the bind-mounted certs (otherwise curl exits 58 / 000).
chmod 755 "$CERTS" "$WL"
LOGDIR="${PWD}/smoke-logs"
PREFIX="tinyhap-smoke-$$"
CURL_IMG="curlimages/curl:8.11.1"
PY_IMG="python:3.13-alpine"
fail=0

mkdir -p "$LOGDIR"

# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  # Capture logs BEFORE removing containers. Cap each to 200 lines so the
  # CI step summary stays under GitHub's 1 MB limit even when HAProxy is
  # spamming the SPOE retry loop.
  for name in proxy spoa backend; do
    cname="$PREFIX-$name"
    if docker inspect "$cname" >/dev/null 2>&1; then
      docker logs --tail 200 "$cname" > "$LOGDIR/$name.log" 2>&1 || true
    fi
  done
  docker rm -f "$PREFIX-proxy" "$PREFIX-spoa" "$PREFIX-backend" >/dev/null 2>&1 || true
  rm -rf "$CERTS" "$WL"
}
trap cleanup EXIT

"$HERE/gen-test-certs.sh" "$CERTS" >/dev/null
echo "127.0.0.0/8" > "$WL/geoip.lst"; : > "$WL/asns.lst"

docker run -d --name "$PREFIX-backend" "$PY_IMG" \
  python -m http.server 8080 --bind 127.0.0.1 >/dev/null

docker run -d --name "$PREFIX-spoa" --network "container:$PREFIX-backend" \
  -v "$HERE/dummy-spoa.py:/dummy-spoa.py:ro" \
  "$PY_IMG" python /dummy-spoa.py >/dev/null

# Wait until the SPOE agent is accepting TCP connections.
# Note: the agent speaks raw SPOE, not HTTP, so use a plain TCP connect.
wait_port() { # host port
  for _ in $(seq 1 30); do
    docker run --rm --network "container:$PREFIX-backend" "$PY_IMG" \
      python -c "import socket,sys; s=socket.create_connection(('$1', $2), 1); s.close()" \
      >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}

wait_port 127.0.0.1 9000 || { echo "SPOE agent did not become ready" >&2; exit 1; }

# ---- SPOE handshake probe -------------------------------------------------
# Sends a minimal HAPROXY-HELLO and expects an AGENT-HELLO back. This catches
# wire-format regressions before HAProxy is ever started.
echo "== SPOE handshake probe"
if docker run --rm --network "container:$PREFIX-backend" \
    -v "$HERE/spoe-probe.py:/spoe-probe.py:ro" "$PY_IMG" python /spoe-probe.py
then
  :
else
  fail=1
fi

# ---- proxy checks ---------------------------------------------------------
start_proxy() { # extra docker args
  docker rm -f "$PREFIX-proxy" >/dev/null 2>&1 || true
  docker run -d --name "$PREFIX-proxy" --network "container:$PREFIX-backend" \
    -v "$CERTS/site.pem:/etc/haproxy/certs/site.pem:ro" \
    -v "$CERTS/ca.crt:/etc/haproxy/certs/ca.crt:ro" "$@" "$IMAGE" >/dev/null
  for _ in $(seq 1 20); do
    docker run --rm --network "container:$PREFIX-backend" "$CURL_IMG" -fs \
      http://127.0.0.1:8404/healthz >/dev/null 2>&1 && return 0
    sleep 1
  done
  docker logs "$PREFIX-proxy" >&2; echo "proxy did not become healthy" >&2; exit 1
}

c() { # curl inside the shared namespace; stdout = http code
  docker run --rm --network "container:$PREFIX-backend" \
    -v "$CERTS:/certs:ro" "$CURL_IMG" -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true
}
MTLS=(--http1.1 --cacert /certs/ca.crt --cert /certs/client.crt --key /certs/client.key \
      --connect-to localhost:443:127.0.0.1:443)

check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS  $1 ($3)"; else echo "FAIL  $1 (expected $2, got $3)"; fail=1; fi
}

echo "== Phase 1: whitelist contains 127.0.0.0/8"
start_proxy -v "$WL/geoip.lst:/etc/haproxy/whitelists/geoip.lst:ro" \
            -v "$WL/asns.lst:/etc/haproxy/whitelists/asns.lst:ro"
for _ in $(seq 1 20); do [ "$(c "${MTLS[@]}" https://localhost/)" = 200 ] && break; sleep 1; done # backend UP
if [ "$(c "${MTLS[@]}" https://localhost/)" != 200 ]; then
  echo "---- DIAG: default backend never became healthy" >&2
  echo "-- direct GET http://127.0.0.1:8080/ from the shared netns:" >&2
  docker run --rm --network "container:$PREFIX-backend" "$CURL_IMG" -s -o /dev/null \
    -w 'direct backend: %{http_code}\n' --max-time 5 http://127.0.0.1:8080/ >&2 || true
  echo "-- mTLS request headers/body from proxy:" >&2
  docker run --rm --network "container:$PREFIX-backend" -v "$CERTS:/certs:ro" "$CURL_IMG" -si \
    --max-time 10 "${MTLS[@]}" https://localhost/ 2>&1 | head -20 >&2 || true
  echo "-- proxy log (tail):" >&2;   docker logs --tail 60 "$PREFIX-proxy" >&2 || true
  echo "-- backend log (tail):" >&2; docker logs --tail 20 "$PREFIX-backend" >&2 || true
  echo "-- spoa log (tail):" >&2;    docker logs --tail 30 "$PREFIX-spoa" >&2 || true
  echo "---- END DIAG" >&2
fi
check "http -> https redirect"        301 "$(c http://127.0.0.1/foo)"
check "ACME reaches acme backend"     503 "$(c http://127.0.0.1/.well-known/acme-challenge/x)"
check "https with client cert"        200 "$(c "${MTLS[@]}" https://localhost/)"
check "https without client cert"     000 "$(c --http1.1 --cacert /certs/ca.crt --connect-to localhost:443:127.0.0.1:443 https://localhost/)"
check "TLS 1.2 refused"               000 "$(c "${MTLS[@]}" --tls-max 1.2 https://localhost/)"
check "oversized Content-Length"      413 "$(c "${MTLS[@]}" -X POST -H 'Content-Length: 9000000' -H 'Expect:' -d x https://localhost/)"
hdrs="$(docker run --rm --network "container:$PREFIX-backend" -v "$CERTS:/certs:ro" "$CURL_IMG" \
        -sI --max-time 10 "${MTLS[@]}" https://localhost/ | tr -d '\r' || true)"
if grep -qi '^strict-transport-security:' <<<"$hdrs"; then echo "PASS  HSTS header set"; else echo "FAIL  HSTS header missing"; fail=1; fi
if grep -qi '^server:' <<<"$hdrs"; then echo "FAIL  Server header leaked"; fail=1; else echo "PASS  Server header removed"; fi

echo "== Phase 2: image whitelist (loopback is not in it)"
start_proxy
check "non-whitelisted source denied" 403 "$(c "${MTLS[@]}" https://localhost/)"
check "ACME still allowed"            503 "$(c http://127.0.0.1/.well-known/acme-challenge/x)"

exit "$fail"