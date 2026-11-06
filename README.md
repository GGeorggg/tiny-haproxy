# Micro HAProxy Container for RouterOS & OpenWrt

Ultra-lightweight HAProxy container (~15MB image, <10MB RAM) published directly on **GitHub Container Registry (GHCR)** via HTTPS for **MikroTik RouterOS** (CRS310, ARM/ARM64) and **OpenWrt** (WRT1900ACS Shelby).

## Supported Architectures
- `linux/arm/v7` (MikroTik CRS310 98DX226S, Linksys WRT1900ACS Shelby)
- `linux/arm64` (MikroTik ARM64 hardware)
- `linux/amd64` (x86_64 routers/VMs)

## Container Image URL (GHCR via HTTPS)
`ghcr.io/ggeorggg/tiny-haproxy:latest`

> **Note:** To pull publicly from GHCR on MikroTik/OpenWrt without credentials, ensure your package visibility is set to **Public** in GitHub under **Packages > tiny-haproxy > Package Settings**.

---

## Key Features & Architecture

- **Automatic TLS Bootstrap:** Generates a temporary self-signed certificate on boot if `/etc/haproxy/certs/site.pem` does not exist, avoiding startup crashes before `acme.sh` completes execution.
- **Multicast DNS (mDNS) Support:** Includes background `avahi-daemon` and `nss-mdns` resolution (`.local` domain resolution over `224.0.0.251:5353`).
- **Dynamic Configuration Assembly:** Concatenates modular rules (`/etc/haproxy/rules.d/*.cfg`) and backends (`/etc/haproxy/backends.d/*.cfg`) into the runtime `haproxy.cfg` on startup.
- **Background ACME Loop:** Built-in background loop runs `acme.sh --cron` every 24 hours without requiring a system `cron` daemon.
- **SPOE Integration:** Stream Processing Offloading Engine support with global fail-open defaults and per-backend fail-closed overrides.

---

## Required Mounts & Storage Setup

| Container Path | Description | Recommended Mount Type |
|---|---|---|
| `/etc/haproxy/certs/` | TLS certificates (`.pem` format) and `ca.crt` | Btrfs subvolume or persistent mount |
| `/etc/haproxy/rules.d/` | Optional snippet files for dynamic frontend ACLs | Directory mount |
| `/etc/haproxy/backends.d/` | Optional snippet files for backend targets | Directory mount |

### RouterOS Setup (MikroTik CRS310)

```routeros
{
# Install Required Packages (might require reboot)
:local ver [/system/resource/get version];
:local arch [/system/resource/get architecture-name];
:local baseUrl "https://download.mikrotik.com/routeros/$ver";

/tool/fetch url="$baseUrl/container-$ver-$arch.npk" dst-path="container-$ver-$arch.npk";
/tool/fetch url="$baseUrl/rose-storage-$ver-$arch.npk" dst-path="rose-storage-$ver-$arch.npk";

:put "Packages downloaded successfully. Rebooting router to apply packages...";
/system/reboot;
}

# Enable container (might require manual cold boot)
/system/device-mode/update container=yes

# Configure VETH interface
/interface/veth/add name=veth-tiny-haproxy address=10.98.13.2 gateway=10.98.13.254

# Set registry URL to GHCR
/container/config/set registry-url=https://ghcr.io

# Create BTRFS Subvolume for persistent container storage
/disk/btrfs/subvolume/add fs=Mikrotik name=tiny-proxy mount=yes mountpoint=tiny-proxy

# Create directory mounts
/container/mounts/add name=proxy-certs src=usb1/certs dst=/etc/haproxy/certs
/container/mounts/add name=proxy-backends src=usb1/backends.d dst=/etc/haproxy/backends.d

# Pull and start container from GHCR
/container/add remote-image="ggeorggg/tiny-haproxy:latest" interface=veth-tiny-haproxy root-dir=usb1/proxy mounts=proxy-certs,proxy-backends comment="proxy" memory-limit=16M
```

---

## Configuring Frontend Rules, Backends & SPOE

### 1. Dynamic Modular Structure
On container startup, HAProxy builds the runtime configuration in the following order:
1. Base template configuration (`haproxy.cfg.template`)
2. All snippets inside `/etc/haproxy/rules.d/*.cfg` (injected into `frontend https_in`)
3. All snippets inside `/etc/haproxy/backends.d/*.cfg` (appended to the file)

### 2. Adding Frontend Rules (`/etc/haproxy/rules.d/`)

Create files ending in `.cfg` inside `/etc/haproxy/rules.d/` to route incoming hostnames or paths:

**`/etc/haproxy/rules.d/01-routing.cfg`**
```haproxy
acl is_web_app hdr(host) -i app.example.com
use_backend app_web_backend if is_web_app

acl is_api_app hdr(host) -i api.example.com
use_backend app_api_backend if is_api_app
```

---

### 3. Adding Backend Configurations (`/etc/haproxy/backends.d/`)

#### Example A: Standard Web Backend (`01-app-web.cfg`)
```haproxy
backend app_web_backend
    mode http
    balance roundrobin
    server web1 192.168.1.100:8080 check maxconn 5
```

#### Example B: mDNS Local Domain Backend (`02-app-api.cfg`)
```haproxy
backend app_api_backend
    mode http
    server api1 service-api.local:3000 check resolvers mdns resolve-prefer ipv4 maxconn 5
```

#### Example C: SPOE Agent TCP Connection (`99-spoe-agent.cfg`)
```haproxy
backend spoe_agent_backend
    mode tcp
    balance roundrobin
    timeout connect 5s
    timeout server  30s
    server spoa1 127.0.0.1:9000 check maxconn 10
```

---

### 4. SPOE Configuration & Fail-Open / Fail-Closed Behavior

The global engine (`spoe-engine.cnf`) is configured with `on-error ignore` by default. If the SPOA agent drops offline or times out, HAProxy **fails open** so client traffic is not interrupted.

#### Default Fail-Open Pattern (Recommended)
Allow traffic to pass unless the SPOA agent explicitly responded AND flagged the request as malicious:

```haproxy
backend app_web_backend
    mode http

    # Fail-Open: Only block if SPOA responded AND flagged request as malicious
    acl spoe_flagged var(txn.ip_checker.is_malicious) -m bool
    acl spoe_responded var(txn.ip_checker.is_malicious) -m found
    http-request deny status 403 if spoe_responded spoe_flagged

    server web1 192.168.1.100:8080 check maxconn 5
```

#### Fail-Closed Override (Per-Backend Security)
To enforce strict inspection on sensitive backends (rejecting requests with `503 Service Unavailable` if the SPOA engine fails to respond):

```haproxy
backend app_api_backend
    mode http

    # Fail-Closed Override: Deny if SPOA agent did NOT respond
    acl spoe_flagged var(txn.ip_checker.is_malicious) -m bool
    acl spoe_responded var(txn.ip_checker.is_malicious) -m found
    http-request deny status 503 unless spoe_responded
    http-request deny status 403 if spoe_flagged

    server api1 service-api.local:3000 check resolvers mdns resolve-prefer ipv4 maxconn 5
```

---

## OpenWrt (WRT1900ACS Shelby) Setup

```bash
docker pull ghcr.io/ggeorggg/tiny-haproxy:latest

docker run -d \
  --name tiny-haproxy \
  --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v /etc/haproxy/certs:/etc/haproxy/certs \
  -v /etc/haproxy/backends.d:/etc/haproxy/backends.d \
  --memory=16m \
  ghcr.io/ggeorggg/tiny-haproxy:latest
```

---

## RouterOS Automated Update Script

```routeros
/system script add name="auto-update-proxy" source={
    :log info "Container update: Stopping proxy..."
    /container/stop [find comment="proxy"]
    
    :local timeout 0
    :while ([/container/get [find comment="proxy"] status] != "stopped" && $timeout < 6) do={
        :delay 5s
        :set timeout ($timeout + 1)
    }

    :log info "Container update: Removing old container instance..."
    /container/remove [find comment="proxy"]
    :delay 2s

    :log info "Container update: Pulling fresh image from GHCR..."
    /container/add remote-image="ggeorggg/tiny-haproxy:latest" interface=veth-tiny-haproxy root-dir=usb1/proxy mounts=proxy-certs,proxy-backends comment="proxy" memory-limit=16M

    :log info "Container update: Waiting for download to complete..."
    :local pullTimeout 0
    :while ([/container/get [find comment="proxy"] status] != "stopped" && $pullTimeout < 30) do={
        :delay 10s
        :set pullTimeout ($pullTimeout + 1)
    }

    :if ([/container/get [find comment="proxy"] status] == "stopped") do={
        :log info "Container update: Download complete. Starting container..."
        /container/start [find comment="proxy"]
    } else={
        :log error "Container update: Download timed out or failed."
    }
}

/system scheduler add name="weekly-proxy-update" start-time=03:00:00 interval=7d script="auto-update-proxy"
```

---

## Development

Open the repository in the included **devcontainer** (VS Code / Codespaces) or install Docker, `make`, `openssl`, `jq` and `shellcheck` locally.

```bash
make help    # list targets
make lint    # hadolint, actionlint, shellcheck
make check   # build + haproxy -c with throw-away test certificates
make test    # behavioural smoke test (mTLS, ACME, geo deny, headers, body limit)
make scan    # trivy image scan (fixable HIGH/CRITICAL)
make all     # everything CI runs
```

### CI/CD

| Workflow | Purpose |
|---|---|
| `ci.yml` | hadolint, actionlint, zizmor, shellcheck, `haproxy -c`, smoke test, Trivy on every PR/push |
| `codeql.yml` | CodeQL analysis of the GitHub Actions workflows |
| `build-and-push.yml` | Trivy gate, multi-arch build (`arm/v7`, `arm64`, `amd64`), push to GHCR; weekly rebuild refreshes whitelists and base image |
| `dependabot.yml` | weekly updates for GitHub Actions, Docker base images, devcontainer features |
```
...eof

I have updated the `README.md` file with detailed instructions covering dynamic rule/backend inclusion, SPOE fail-open defaults with per-backend fail-closed overrides, self-signed bootstrapping, mDNS resolvers, and complete RouterOS/OpenWrt installation workflows.