# syntax=docker/dockerfile:1

# ---- Stage 1: download whitelists (build tools never reach the final image) ----
FROM alpine:3.22 AS whitelists
# hadolint ignore=DL3018
RUN apk add --no-cache curl ca-certificates jq
COPY --chmod=755 scripts/fetch-whitelists.sh /usr/local/bin/fetch-whitelists.sh
RUN /usr/local/bin/fetch-whitelists.sh /out

# ---- Stage 2: avahi2dns ----
# avahi2dns is not in the stable Alpine branch that haproxy:2.8-alpine is built on
# (only edge/community), so take the binary from a throw-away edge stage instead of
# mixing edge repositories into the final image.
FROM alpine:edge AS avahi2dns
# hadolint ignore=DL3018
RUN apk add --no-cache avahi2dns && cp "$(command -v avahi2dns)" /avahi2dns

# ---- Stage 3: runtime ----
FROM haproxy:3.1-alpine

# Stays root on purpose: dbus-daemon and avahi-daemon need it, and binding ports
# 80/443 needs it on RouterOS, where the unprivileged-port sysctl cannot be set.
# haproxy.cfg drops to the haproxy user right after startup (global: user/group).
# hadolint ignore=DL3002
USER 0

# mDNS on Alpine: musl has no NSS, so nss-mdns cannot work here.
# Instead: avahi-daemon (package `avahi`, needs dbus) answers mDNS, avahi2dns exposes
# it as a plain DNS server on 127.0.0.1:5354, and the HAProxy `resolvers mdns` section
# (haproxy.cfg.template) queries that. avahi-tools provides avahi-resolve for debugging.
# See README.md. Alpine package pins go stale with every security rebuild, so they
# are intentionally not pinned.
# hadolint ignore=DL3018
RUN apk add --no-cache ca-certificates openssl dbus avahi avahi-tools
COPY --from=avahi2dns /avahi2dns /usr/local/bin/avahi2dns
COPY --from=whitelists /out/ /etc/haproxy/whitelists/

COPY haproxy.cfg.template /usr/local/etc/haproxy/haproxy.cfg.template
COPY spoe-engine.cnf /etc/haproxy/spoe.d/spoe-engine.cnf
COPY rules.d/ /etc/haproxy/rules.d/
COPY backends.d/ /etc/haproxy/backends.d/
COPY avahi-daemon.conf /etc/avahi/avahi-daemon.conf
COPY --chmod=755 scripts/entrypoint.sh scripts/assemble-config.sh /usr/local/bin/

# Certificates are NOT part of the image. Mount /etc/haproxy/certs read-write to persist:
#   /etc/haproxy/certs/site.pem  (server cert + key; a temporary self-signed one is
#                                 generated at start-up if missing)
#   /etc/haproxy/certs/ca.crt    (client CA for mTLS; a placeholder that accepts no
#                                 clients is generated if missing)
# Also mountable: /data (acme.sh home), /etc/haproxy/rules.d, /etc/haproxy/backends.d
# (a mount replaces the image defaults). See README.md, "Persistent data and mount points".

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
