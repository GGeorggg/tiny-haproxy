FROM haproxy:2.8-alpine
USER root

# mDNS on Alpine: musl has no NSS, so nss-mdns cannot work here.
# Instead: avahi-daemon (needs dbus) answers mDNS, avahi2dns exposes it as a
# plain DNS server on 127.0.0.1:5354, and the HAProxy `resolvers mdns` section
# (haproxy.cfg.template) queries that. See README.md.
# NOTE: avahi2dns may only be available in newer Alpine repos (community/edge).
RUN apk add --no-cache curl ca-certificates jq openssl dbus avahi avahi-daemon avahi2dns

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

# Directories for dynamic backend files, rules, and SPOE configs
RUN mkdir -p /etc/haproxy/rules.d /etc/haproxy/backends.d /etc/haproxy/spoe.d /etc/haproxy/certs

COPY haproxy.cfg.template /usr/local/etc/haproxy/haproxy.cfg.template
COPY spoe-engine.cnf /etc/haproxy/spoe.d/spoe-engine.cnf
COPY rules.d/ /etc/haproxy/rules.d/
COPY backends.d/ /etc/haproxy/backends.d/
COPY avahi-daemon.conf /etc/avahi/avahi-daemon.conf
COPY --chmod=755 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
