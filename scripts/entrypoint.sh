#!/bin/sh
# Container entrypoint:
# 1. (optional) start dbus -> avahi-daemon -> avahi2dns for *.local resolution.
# 2. Bootstrap a self-signed cert if missing.
# 3. Assemble haproxy.cfg from template + rules.d/*.cfg + backends.d/*.cfg.
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

{
  echo '# Auto-generated HAProxy configuration'
  cat /usr/local/etc/haproxy/haproxy.cfg.template
  echo ''
  echo '# ========== DYNAMIC FRONTEND RULES (/etc/haproxy/rules.d/) =========='
  for r in /etc/haproxy/rules.d/*.cfg; do
    [ -f "$r" ] || continue
    echo "# --- Loaded Rule: $r ---"
    cat "$r"
    echo ''
  done
  echo '# ========== DYNAMIC BACKENDS (/etc/haproxy/backends.d/) =========='
  for f in /etc/haproxy/backends.d/*.cfg; do
    [ -f "$f" ] || continue
    echo "# --- Loaded Backend: $f ---"
    cat "$f"
    echo ''
  done
} > "$CFG"

(
  while true; do
    sleep 86400
    if command -v acme.sh >/dev/null 2>&1; then acme.sh --cron --home /data/acme; fi
  done
) &

exec haproxy -f "$CFG"
