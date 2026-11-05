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

## RouterOS Setup (MikroTik CRS310)

```routeros
# Set registry URL to GHCR
/container/config/set registry-url=https://ghcr.io

# Pull and start container from GitHub
/container/add remote-image="ggeorggg/tiny-haproxy:latest" interface=veth1 root-dir=usb1/proxy comment="proxy" memory-limit=16M
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