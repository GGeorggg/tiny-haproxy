# syntax=docker/dockerfile:1

# ---- Stage 1: download whitelists (build tools never reach the final image) ----
FROM alpine:3.22 AS whitelists
RUN apk add --no-cache curl ca-certificates jq
COPY scripts/fetch-whitelists.sh /usr/local/bin/fetch-whitelists.sh
RUN /usr/local/bin/fetch-whitelists.sh /out

# ---- Stage 2: runtime ----
FROM haproxy:2.8-alpine

# Stays root on purpose: binding ports 80/443 needs it on RouterOS, where the
# unprivileged-port sysctl cannot be set. haproxy.cfg drops to the haproxy user
# right after startup (global: user/group).
USER root

COPY --from=whitelists /out/ /etc/haproxy/whitelists/
COPY haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg

# Certificates are NOT part of the image, mount them:
#   /etc/haproxy/certs/site.pem  (server cert + key)
#   /etc/haproxy/certs/ca.crt    (client CA for mTLS)

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8404/healthz || exit 1
