#!/bin/sh
# Container entrypoint:
# 1. (optional) start dbus -> avahi-daemon -> avahi2dns for *.local resolution.
# 2. Bootstrap a self-signed server cert and a placeholder client CA if missing.
# 3. Assemble haproxy.cfg (assemble-config.sh) from template + rules.d + backends.d.
# 4. Start the 24h acme.sh renewal loop.
# 5. exec HAProxy.
set -eu

CFG=/usr/local/etc/haproxy/haproxy.cfg
MDNS_ENABLE="${MDNS_ENABLE:-1}"
# avahi2dns must listen on 127.0.0.1:5354, the address in the `resolvers mdns` section
# of haproxy.cfg.template. Flags: -a bind address, -p port, -d domains, -v debug.
AVAHI2DNS_ARGS="${AVAHI2DNS_ARGS:--a 127.0.0.1 -p 5354 -d local}"

start_mdns() {
  mkdir -p /run/dbus
  rm -f /run/dbus/pid
  dbus-uuidgen --ensure
  dbus-daemon --system --fork
  avahi-daemon --daemonize --no-rlimits
  i=0
  while [ ! -S /run/avahi-daemon/socket ] && [ "$i" -lt 10 ]; do
    i=$((i + 1)); sleep 1
  done
  # shellcheck disable=SC2086  # intentional word splitting of extra args
  avahi2dns $AVAHI2DNS_ARGS &
}

if [ "$MDNS_ENABLE" = "1" ]; then
  # mDNS is optional: a failure must not stop HAProxy from starting.
  start_mdns || echo "WARN: mDNS stack failed to start; *.local names will not resolve" >&2
fi

mkdir -p /etc/haproxy/certs
if [ ! -f /etc/haproxy/certs/site.pem ]; then
  echo 'Generating temporary self-signed certificate...'
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout /etc/haproxy/certs/site.pem \
    -out /etc/haproxy/certs/site.pem \
    -subj '/CN=localhost'
fi

# The 443 bind uses `ca-file ... verify required`, so HAProxy refuses to start without a
# CA file. If none is mounted, install a throw-away self-signed CA whose private key is
# discarded immediately: HAProxy starts, but no client certificate can ever validate
# against it, so every mTLS handshake is rejected (fail closed). Mount a real
# /etc/haproxy/certs/ca.crt to allow clients.
if [ ! -f /etc/haproxy/certs/ca.crt ]; then
  echo 'WARN: /etc/haproxy/certs/ca.crt missing; installed a placeholder CA, no client certificate will be accepted (mTLS fails closed)' >&2
  CA_KEY="$(mktemp)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "$CA_KEY" \
    -out /etc/haproxy/certs/ca.crt \
    -subj '/CN=placeholder-ca-no-client-certs-accepted'
  rm -f "$CA_KEY"
fi

assemble-config.sh

(
  while true; do
    sleep 86400
    if command -v acme.sh >/dev/null 2>&1; then acme.sh --cron --home /data/acme; fi
  done
) &

exec haproxy -f "$CFG"
