# Micro HAProxy Container for RouterOS & OpenWrt

Ultra-lightweight HAProxy container (~15MB image, <10MB RAM) published directly on **GitHub Container Registry (GHCR)** via HTTPS for **MikroTik RouterOS** (CRS310, ARM/ARM64) and **OpenWrt** (WRT1900ACS Shelby).

## Supported Architectures
- `linux/arm/v7` (MikroTik CRS310, Linksys WRT1900ACS Shelby)
- `linux/arm64` (MikroTik ARM64 hardware)
- `linux/amd64` (x86_64 routers/VMs)

## Container Image URL (GHCR via HTTPS)
`ghcr.io/ggeorggg/tiny-haproxy:latest`

> **Note:** To pull publicly from GHCR on MikroTik/OpenWrt without credentials, ensure your package visibility is set to **Public** in GitHub under **Packages > tiny-haproxy > Package Settings**.

---

## Required mounts (certificates are NOT in the image)

| Path in container | Content |
|---|---|
| `/etc/haproxy/certs/site.pem` | server certificate chain + private key |
| `/etc/haproxy/certs/ca.crt` | CA used to verify client certificates (mTLS is **mandatory** on 443) |

RouterOS example (copy the files to `usb1/certs` first):

```routeros
/container/mounts/add name=proxy-certs src=usb1/certs dst=/etc/haproxy/certs
/container/add remote-image="ggeorggg/tiny-haproxy:latest" interface=veth1 root-dir=usb1/proxy mounts=proxy-certs comment="proxy" memory-limit=16M
```

Docker example: add `-v /path/to/certs:/etc/haproxy/certs:ro` to the `docker run` command below.

Behaviour notes: TLS 1.3 only, clients without a valid certificate are rejected,
sources outside the GeoIP/ASN whitelist get `403`, plain HTTP is redirected to HTTPS
except `/.well-known/acme-challenge/` (reachable from anywhere for Let's Encrypt).

---

## RouterOS Setup (MikroTik CRS310)

```routeros
{
# Install Required Packages might require reboot
:local ver [/system/resource/get version];
:local arch [/system/resource/get architecture-name];
:local baseUrl "https://download.mikrotik.com/routeros/$ver";

/tool/fetch url="$baseUrl/container-$ver-$arch.npk" dst-path="container-$ver-$arch.npk";
/tool/fetch url="$baseUrl/rose-storage-$ver-$arch.npk" dst-path="rose-storage-$ver-$arch.npk";

:put "Packages downloaded successfully. Rebooting router to apply packages...";
/system/reboot;
}

# Enable container might require (manual) reboot
/system/device-mode/update container=yes

# Set registry URL to GHCR
/container/config/set registry-url=https://ghcr.io

# Create interface
/interface/veth/add name=veth-tiny-haproxy address=10.98.13.2 gateway=10.98.13.254

# Attach USB Disk - format once with btrfs

# Create BTRFS Subvolume
/disk/btrfs/subvolume/add fs=Mikrotik name=tiny-proxy mount=yes mountpoint=tiny-proxy

# Pull and start container from GitHub
/container/add remote-image="ggeorggg/tiny-haproxy:latest" interface=veth-tiny-haproxy root-dir=usb1/proxy comment="proxy" memory-limit=16M
```

### RouterOS Automated Update Script

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
    /container/add remote-image="ggeorggg/tiny-haproxy:latest" interface=veth1 root-dir=usb1/proxy comment="proxy" memory-limit=16M

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

## OpenWrt (WRT1900ACS Shelby) Setup

```bash
docker pull ghcr.io/ggeorggg/tiny-haproxy:latest

docker run -d \
  --name tiny-haproxy \
  --restart unless-stopped \
  -p 80:80 -p 443:443 \
  --memory=16m \
  ghcr.io/ggeorggg/tiny-haproxy:latest
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
| `ci.yml` | hadolint, actionlint, zizmor, shellcheck, `haproxy -c`, smoke test, Trivy (repo + image, SARIF to code scanning) on every PR/push |
| `codeql.yml` | CodeQL analysis of the GitHub Actions workflows |
| `build-and-push.yml` | Trivy gate, multi-arch build, push to GHCR, build provenance attestation, SBOM artifact; weekly rebuild refreshes whitelists and base image |
| `dependabot.yml` | weekly updates for GitHub Actions, Docker base images, devcontainer features |

Whitelist downloads in the Dockerfile fail the build on HTTP errors, non-CIDR content or implausibly small lists.
