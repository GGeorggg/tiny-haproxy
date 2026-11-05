#!/usr/bin/env bash
# Generates throw-away certificates for config checks and smoke tests ONLY.
# Output: ca.crt, site.pem (server cert + key), client.crt, client.key
set -euo pipefail

OUT="${1:-.test-certs}"
mkdir -p "$OUT"
cd "$OUT"

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt \
  -days 2 -subj "/CN=tiny-haproxy-test-ca" 2>/dev/null

issue() { # name, CN, extfile content
  openssl req -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=$2" 2>/dev/null
  printf '%s\n' "$3" > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out "$1.crt" -days 2 -extfile "$1.ext" 2>/dev/null
}

issue server localhost "subjectAltName=DNS:localhost,IP:127.0.0.1"
issue client test-client "extendedKeyUsage=clientAuth"

cat server.crt server.key > site.pem
rm -f ./*.csr ./*.ext ./*.srl server.key server.crt
chmod 644 ./*.pem ./*.crt ./*.key
echo "Test certificates written to $PWD"
