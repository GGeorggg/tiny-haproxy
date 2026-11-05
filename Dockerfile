FROM haproxy:2.8-alpine
USER root

# Install minimal build tools to scrape IP ranges
RUN apk add --no-cache curl ca-certificates jq

# 1. Fetch GeoIP CIDRs (AT and surrounding: CH, DE, CZ, SK, HU, SI, IT, HR)
RUN mkdir -p /etc/haproxy/whitelists && \
    for country in at ch de cz sk hu si it hr; do \
      curl -sS "https://www.ipdeny.com/ipblocks/data/countries/${country}.zone" >> /etc/haproxy/whitelists/geoip.lst; \
    done

# 2. Fetch ASN Subnets (AS25255 Drei, AS8412 Magenta, AS8447 A1) via RIPE Stat API
RUN for asn in AS25255 AS8412 AS8447; do \
      curl -sS "https://stat.ripe.net/data/announced-prefixes/data.json?resource=${asn}" | \
      jq -r '.data.prefixes[].prefix' >> /etc/haproxy/whitelists/asns.lst; \
    done

# Remove build tools to keep the layer minimal
RUN apk del curl jq

# Copy HAProxy configuration into image
COPY haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg