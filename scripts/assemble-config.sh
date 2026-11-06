#!/bin/sh
# Assemble /usr/local/etc/haproxy/haproxy.cfg from the template plus
# rules.d/*.cfg and backends.d/*.cfg. Used by the entrypoint and by CI/Makefile
# (`haproxy -c`), so both always check exactly what the container would run.
set -eu

CFG="${HAPROXY_CFG:-/usr/local/etc/haproxy/haproxy.cfg}"

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
