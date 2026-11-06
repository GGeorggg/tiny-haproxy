FROM haproxy:2.8-alpine
USER root

# Install minimal build tools + openssl + mDNS resolver dependencies (avahi & nss-mdns)
RUN apk add --no-cache curl ca-certificates jq openssl avahi nss-mdns

# Configure mDNS / local hostname resolution
RUN echo "hosts: files mdns4_minimal [NOTFOUND=return] dns mdns4" > /etc/nsswitch.conf

# Fetch GeoIP CIDRs (AT and surrounding: CH, DE, CZ, SK, HU, SI, IT, HR)
RUN mkdir -p /etc/haproxy/whitelists && \
    for country in at ch de cz sk hu si it hr; do \
      curl -sS "https://www.ipdeny.com/ipblocks/data/countries/${country}.zone" >> /etc/haproxy/whitelists/geoip.lst; \
    done

# Fetch ASN Subnets (AS25255 Drei, AS8412 Magenta, AS8447 A1) via RIPE Stat API
RUN for asn in AS25255 AS8412 AS8447; do \
      curl -sS "https://stat.ripe.net/data/announced-prefixes/data.json?resource=${asn}" | \
      jq -r '.data.prefixes[].prefix' >> /etc/haproxy/whitelists/asns.lst; \
    done

# Clean up build tools
RUN apk del curl jq

# Create directories for dynamic backend files, rules, and SPOE configs
RUN mkdir -p /etc/haproxy/rules.d /etc/haproxy/backends.d /etc/haproxy/spoe.d /etc/haproxy/certs

# Copy base template, SPOE configs, and default rule/backend modules
COPY haproxy.cfg.template /usr/local/etc/haproxy/haproxy.cfg.template
COPY spoe-engine.cnf /etc/haproxy/spoe.d/spoe-engine.cnf
COPY rules.d/ /etc/haproxy/rules.d/
COPY backends.d/ /etc/haproxy/backends.d/

# Entrypoint script:
# 1. Starts Avahi mDNS daemon.
# 2. Bootstraps self-signed cert if missing.
# 3. Assembles runtime haproxy.cfg from template + rules.d/*.cfg + backends.d/*.cfg.
# 4. Launches background 24-hour acme.sh renewal loop.
# 5. Executes HAProxy as main process.
ENTRYPOINT ["sh", "-c", "\
  avahi-daemon --daemonize && \
  mkdir -p /etc/haproxy/certs && \
  if [ ! -f /etc/haproxy/certs/site.pem ]; then \
    echo 'Generating temporary self-signed certificate...'; \
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
      -keyout /etc/haproxy/certs/site.pem \
      -out /etc/haproxy/certs/site.pem \
      -subj '/CN=localhost'; \
  fi && \
  echo '# Auto-generated HAProxy configuration' > /usr/local/etc/haproxy/haproxy.cfg && \
  cat /usr/local/etc/haproxy/haproxy.cfg.template >> /usr/local/etc/haproxy/haproxy.cfg && \
  echo '' >> /usr/local/etc/haproxy/haproxy.cfg && \
  echo '# =========================================================================' >> /usr/local/etc/haproxy/haproxy.cfg && \
  echo '# DYNAMIC FRONTEND RULES INJECTED FROM /etc/haproxy/rules.d/' >> /usr/local/etc/haproxy/haproxy.cfg && \
  echo '# =========================================================================' >> /usr/local/etc/haproxy/haproxy.cfg && \
  for r in /etc/haproxy/rules.d/*.cfg; do \
    if [ -f \"$r\" ]; then \
      echo \"# --- Loaded Rule: $r ---\" >> /usr/local/etc/haproxy/haproxy.cfg; \
      cat \"$r\" >> /usr/local/etc/haproxy/haproxy.cfg; \
      echo '' >> /usr/local/etc/haproxy/haproxy.cfg; \
    fi; \
  done && \
  echo '# =========================================================================' >> /usr/local/etc/haproxy/haproxy.cfg && \
  echo '# DYNAMIC BACKENDS INJECTED FROM /etc/haproxy/backends.d/' >> /usr/local/etc/haproxy/haproxy.cfg && \
  echo '# =========================================================================' >> /usr/local/etc/haproxy/haproxy.cfg && \
  for f in /etc/haproxy/backends.d/*.cfg; do \
    if [ -f \"$f\" ]; then \
      echo \"# --- Loaded Backend: $f ---\" >> /usr/local/etc/haproxy/haproxy.cfg; \
      cat \"$f\" >> /usr/local/etc/haproxy/haproxy.cfg; \
      echo '' >> /usr/local/etc/haproxy/haproxy.cfg; \
    fi; \
  done && \
  (while true; do sleep 86400; if command -v acme.sh >/dev/null 2>&1; then acme.sh --cron --home /data/acme; fi; done) & \
  exec haproxy -f /usr/local/etc/haproxy/haproxy.cfg \
"]