#!/bin/sh
# Builds the GeoIP / ASN whitelists. Fails the build on any download problem
# instead of silently producing empty or broken lists.
set -eu

OUT="${1:-/out}"
GEO_COUNTRIES="${GEO_COUNTRIES:-at ch de cz sk hu si it hr}"
ASNS="${ASNS:-AS25255 AS8412 AS8447}"
CIDR_RE='^[0-9a-fA-F:.]+/[0-9]+$'

mkdir -p "$OUT"
: > "$OUT/geoip.lst"
: > "$OUT/asns.lst"

for country in $GEO_COUNTRIES; do
  curl -fsS --retry 3 --retry-delay 2 --max-time 60 \
    "https://www.ipdeny.com/ipblocks/data/countries/${country}.zone" >> "$OUT/geoip.lst"
done

for asn in $ASNS; do
  curl -fsS --retry 3 --retry-delay 2 --max-time 60 \
    "https://stat.ripe.net/data/announced-prefixes/data.json?resource=${asn}" |
    jq -er '.data.prefixes[].prefix' >> "$OUT/asns.lst"
done

# Sanity checks: only CIDRs, plausible sizes
for f in geoip.lst asns.lst; do
  if grep -Evq "$CIDR_RE" "$OUT/$f"; then
    echo "ERROR: $f contains non-CIDR lines" >&2
    grep -Ev "$CIDR_RE" "$OUT/$f" | head -5 >&2
    exit 1
  fi
done
[ "$(wc -l < "$OUT/geoip.lst")" -gt 1000 ] || { echo "ERROR: geoip.lst suspiciously small" >&2; exit 1; }
[ "$(wc -l < "$OUT/asns.lst")" -gt 20 ]   || { echo "ERROR: asns.lst suspiciously small" >&2; exit 1; }

echo "geoip.lst: $(wc -l < "$OUT/geoip.lst") entries, asns.lst: $(wc -l < "$OUT/asns.lst") entries"
