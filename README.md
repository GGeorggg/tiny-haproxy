# Micro HAProxy Container for RouterOS & OpenWrt

Ultra-lightweight HAProxy container published on **GitHub Container Registry (GHCR)** for
**MikroTik RouterOS** (CRS310, ARM/ARM64) and **OpenWrt** (WRT1900ACS Shelby).

## Layout

| Path | Purpose |
|---|---|
| `haproxy.cfg.template` | Global/defaults, `resolvers mdns`, and the HTTPS frontend |
| `rules.d/*.cfg` | Frontend routing rules, appended to the frontend |
| `backends.d/*.cfg` | Backend definitions |
| `spoe-engine.cnf` | SPOE agent config |
| `avahi-daemon.conf` | Resolver-only avahi config (no publishing) |
| `scripts/entrypoint.sh` | Starts mDNS, creates a temp server cert and a placeholder client CA if missing, assembles `haproxy.cfg`, runs HAProxy |

At start-up the entrypoint concatenates the template, `rules.d/*.cfg` and `backends.d/*.cfg`
into `/usr/local/etc/haproxy/haproxy.cfg`. Mount your own files over those directories to
customise routing and backends (see [Persistent data and mount points](#persistent-data-and-mount-points)).

## Persistent data and mount points

Everything written inside the container is lost when the container is recreated. Mount
these paths from the host (RouterOS: from `/usb1/...`, not internal flash):

| Container path | Mode | What lives there | Mount it to... |
|---|---|---|---|
| `/data` | read-write | acme.sh home (`/data/acme`) | keep issued certificates and the ACME account across recreation |
| `/etc/haproxy/certs` | read-write | `site.pem` (server cert + key), `ca.crt` (client CA for mTLS) | persist the generated self-signed cert and supply your real `ca.crt` |
| `/etc/haproxy/rules.d` | read-only is fine | `*.cfg` frontend routing rules | change routing without rebuilding the image |
| `/etc/haproxy/backends.d` | read-only is fine | `*.cfg` backend definitions | change backends without rebuilding the image |

`/etc/haproxy/certs` must be writable: the entrypoint writes `site.pem` there when it is
missing (self-signed, 365 days) and a placeholder `ca.crt`. Without that mount both are
regenerated on every container start. Put your real client CA at `ca.crt` in the mounted
directory. If it is missing, HAProxy still starts, but with a throw-away CA that accepts no
client certificates (mTLS fails closed), and the entrypoint logs a warning. Replace the file
and restart to let clients in. `/data` is where acme.sh keeps its state (`--home /data/acme`);
the image does not install acme.sh yet, so today it only reserves the location.

### Backend configuration and rules

`rules.d/` and `backends.d/` are read **only at container start**; restart the container after
a change. A mount **replaces the whole directory**, including the defaults baked into the
image, so the mounted directories must be complete. These pieces are required:

- `backends.d` must define `default_backend`, `acme_backend` and `spoe_agent_backend` (see
  `backends.d/00-default.cfg` and `99-spoe-agent.cfg` in this repo). The template and
  `spoe-engine.cnf` reference them.
- `rules.d` must end with the `default_backend default_backend` line (see
  `rules.d/01-routing.cfg`). The rules are appended to the last frontend (`https_in`).

Start by copying this repo's `rules.d/` and `backends.d/` to the host, then edit. Files are
concatenated in alphabetical order, so keep the numeric prefixes. Check a change before
restarting the real container:

```sh
docker run --rm --entrypoint sh \
  -v "$PWD/certs:/etc/haproxy/certs" \
  -v "$PWD/rules.d:/etc/haproxy/rules.d:ro" \
  -v "$PWD/backends.d:/etc/haproxy/backends.d:ro" \
  <image> -c 'assemble-config.sh && haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg'
```

### MikroTik RouterOS example

Create the directories on the USB disk first (`/file print` should list them), then mount
them. Parameter names differ between RouterOS 7.x versions, so check them with `?`
(`/container/mounts/add ?`, `/container/set ?`):

```routeros
/container/mounts
add list=tiny-haproxy src=/usb1/tiny-proxy/data        dst=/data
add list=tiny-haproxy src=/usb1/tiny-proxy/certs       dst=/etc/haproxy/certs
add list=tiny-haproxy src=/usb1/tiny-proxy/rules.d     dst=/etc/haproxy/rules.d
add list=tiny-haproxy src=/usb1/tiny-proxy/backends.d  dst=/etc/haproxy/backends.d
/container set [find name=tiny-haproxy] mountlists=tiny-haproxy
```

Older 7.x releases use `/container/mounts add name=... src=... dst=...` plus
`mounts=<names>` on the container instead. Keep `root-dir`, `layer-dir` and the mount sources
on the same USB disk to avoid wearing out internal flash.

## mDNS (`*.local`) support

Backends can use `.local` names, for example:

```haproxy
server api1 service-api.local:3000 check resolvers mdns resolve-prefer ipv4 init-addr none maxconn 5
```

### Why not `nss-mdns`?

Alpine uses musl, which has no NSS support, so `nss-mdns` and `/etc/nsswitch.conf` do nothing
here. mDNS is therefore done with a small chain instead:

```
HAProxy `resolvers mdns` --DNS--> avahi2dns (127.0.0.1:5354) --D-Bus--> avahi-daemon --mDNS--> LAN
```

- **avahi-daemon** (part of the Alpine `avahi` package, plus **dbus**) does the actual mDNS lookups and publishes nothing.
- **avahi2dns** exposes avahi as an ordinary DNS server on `127.0.0.1:5354`.
- **HAProxy** has no per-domain forwarding. It does not need it: only `server` lines that say
  `resolvers mdns` use that resolver, so no extra forwarding resolver (dnsmasq/unbound) is needed.
- `init-addr none` lets HAProxy start even if the name is not resolvable yet. It is
  resolved at runtime and re-checked according to the `hold` settings.

Do not point `nameserver` at `224.0.0.251:5353` directly. HAProxy uses a connected UDP socket,
so unicast replies from the responder are most likely dropped.

### MikroTik RouterOS setup

mDNS is link-local multicast and does not cross interfaces by itself. Use RouterOS's mDNS
repeater to forward queries and answers between the LAN and the container's veth:

```routeros
/ip dns set mdns-repeat-ifaces=<lan-bridge>,<container-veth>
```

This option is available in recent RouterOS 7.x releases. Verify the exact name and minimum
version for your build. The container's interface must also be the one named in
`allow-interfaces=` in `avahi-daemon.conf` (default `eth0`, the interface name *inside* the
container).

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MDNS_ENABLE` | `1` | Set to `0` to skip dbus/avahi/avahi2dns entirely |
| `AVAHI2DNS_ARGS` | `-a 127.0.0.1 -p 5354 -d local` | Flags for `avahi2dns` (`-a` address, `-p` port, `-d` domains, `-v` debug). If you change address or port, update `nameserver` in `haproxy.cfg.template` too |

If the mDNS stack fails to start, the entrypoint logs a warning and HAProxy starts anyway.

### Verifying

Inside the container:

```sh
avahi-resolve -n service-api.local      # works -> avahi + repeater are fine
```

- Works here but HAProxy shows no address: check the `resolvers mdns` section and the
  `avahi2dns` listen address/port.
- Fails here: check the RouterOS repeater interfaces and `allow-interfaces` in `avahi-daemon.conf`.

### Caveats

- avahi-daemon and dbus-daemon need root, so the image stays root (`USER 0`) and HAProxy drops
  to the `haproxy` user itself after binding the ports (`user`/`group` in `global`). The
  whitelists are built in a separate stage by `scripts/fetch-whitelists.sh`, so curl and jq
  never reach the final image.
- `avahi2dns` is only packaged in Alpine **edge** (community), and the upstream `amd64` release
  binary is glibc-linked and does not run on Alpine. The Dockerfile therefore copies the binary
  out of an `alpine:edge` build stage. That makes the version float with edge; pin it with
  `apk add avahi2dns=<version>` in that stage if you need reproducible builds.
- Keep `scripts/entrypoint.sh` and the Dockerfile on LF line endings. CRLF breaks the
  shebang.

## Security scanning and accepted findings

CI scans the image with Trivy (`ci.yml`, and again as a gate in `build-and-push.yml` before
anything is pushed). HIGH/CRITICAL findings that have a fix fail the build.

To accept a finding, add an entry to `.trivyignore.yaml`, which both workflows pass to
Trivy via `trivyignores`:

```yaml
vulnerabilities:
  - id: CVE-2026-12345
    paths:
      - "usr/local/bin/some-binary"   # relative to the image root, as Trivy prints it
    statement: Why this is acceptable here
    expired_at: 2027-01-06            # the build fails again after this date
```

Always scope an entry to a `paths` or `purls` match and set `expired_at`, so that accepted
risks resurface for review instead of being forgotten. Remove the entry once a fixed
version is in the image. To try it locally:
`trivy image --ignorefile .trivyignore.yaml --severity HIGH,CRITICAL --ignore-unfixed <image>`.
