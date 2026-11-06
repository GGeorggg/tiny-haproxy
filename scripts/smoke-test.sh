#!/usr/bin/env bash
# Behavioural smoke test of the built image (needs Docker, no real certs/backends).
# Usage: scripts/smoke-test.sh [image]   (default: tiny-haproxy:dev)
# All containers share ONE network namespace so 127.0.0.1:8080 (backend) works as configured.
set -euo pipefail

IMAGE="${1:-tiny-haproxy:dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CERTS="$(mktemp -d)"
WL="$(mktemp -d)"
PREFIX="tinyhap-smoke-$$"
CURL_IMG="curlimages/curl:8.11.1"
fail=0

# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  docker rm -f "$PREFIX-proxy" "$PREFIX-backend" "$PREFIX-spoa" >/dev/null 2>&1 || true
  rm -rf "$CERTS" "$WL"
}
trap cleanup EXIT

"$HERE/gen-test-certs.sh" "$CERTS" >/dev/null
echo "127.0.0.0/8" > "$WL/geoip.lst"; : > "$WL/asns.lst"

docker run -d --name "$PREFIX-backend" python:3.13-alpine \
  python -m http.server 8080 --bind 127.0.0.1 >/dev/null

docker run -d --name "$PREFIX-spoa" --network "container:$PREFIX-backend" \
  -v "$HERE/dummy-spoa.py:/dummy-spoa.py:ro" \
  python:3.13-alpine python /dummy-spoa.py >/dev/null

start_proxy() { # extra docker args
  # Wait until the SPOE agent is listening (plain TCP, not HTTP).
  for _ in $(seq 1 15); do
    docker run --rm --network "container:$PREFIX-backend" alpine:3.20 \
      sh -c 'nc -z 127.0.0.1 9000' >/dev/null 2>&1 && break
    sleep 1
    echo ... waiting for SPOA agent on 127.0.0.1 9000/tcp
  done

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
check "http -> https redirect"        301 "$(c http://127.0.0.1/foo)"
check "ACME reaches acme backend"     503 "$(c http://127.0.0.1/.well-known/acme-challenge/x)"
check "https with client cert"        200 "$(c "${MTLS[@]}" https://localhost/)"
check "https without client cert"     000 "$(c --http1.1 --cacert /certs/ca.crt --connect-to localhost:443:127.0.0.1:443 https://localhost/)"
check "TLS 1.2 refused"               000 "$(c "${MTLS[@]}" --tls-max 1.2 https://localhost/)"
check "oversized Content-Length"      413 "$(c "${MTLS[@]}" -X POST -H 'Content-Length: 9000000' -H 'Expect:' -d x https://localhost/)"
hdrs="$(docker run --rm --network "container:$PREFIX-backend" -v "$CERTS:/certs:ro" "$CURL_IMG" \
        -sI --max-time 10 "${MTLS[@]}" https://localhost/ | tr -d '\r')"
if grep -qi '^strict-transport-security:' <<<"$hdrs"; then echo "PASS  HSTS header set"; else echo "FAIL  HSTS header missing"; fail=1; fi
if grep -qi '^server:' <<<"$hdrs"; then echo "FAIL  Server header leaked"; fail=1; else echo "PASS  Server header removed"; fi

echo "== Phase 2: image whitelist (loopback is not in it)"
start_proxy
check "non-whitelisted source denied" 403 "$(c "${MTLS[@]}" https://localhost/)"
check "ACME still allowed"            503 "$(c http://127.0.0.1/.well-known/acme-challenge/x)"

exit "$fail"
