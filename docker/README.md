# Docker

MeshDash is available as a self-updating Docker image: **`rusjpmd/meshdash-runner`**

The runner is a thin bootstrap container that downloads the latest MeshDash from meshdash.co.uk on first boot and auto-updates on every restart. No manual image builds needed.

## Quick Start

### Cloud Setup (with MD_SETUP_KEY)

Generate your install command at [meshdash.co.uk](https://meshdash.co.uk/) and run it:

```bash
docker run -d \
  --name meshdash \
  --restart always \
  --network host \
  --privileged \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /dev:/dev \
  -v meshdash_data:/app/data \
  -e MD_SETUP_KEY="MD-YOUR-KEY-HERE" \
  -e MD_SETUP_URL="https://meshdash.co.uk/setup" \
  rusjpmd/meshdash-runner:latest
```

The container auto-downloads the latest MeshDash version, installs dependencies, fetches your config, and starts the dashboard. Updates happen automatically on restart.

### Standalone (no account needed)

No setup key? No problem. Just run the container and configure everything through the built-in web wizard:

```bash
docker run -d \
  --name meshdash \
  --restart always \
  --network host \
  --privileged \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /dev:/dev \
  -v meshdash_data:/app/data \
  rusjpmd/meshdash-runner:latest
```

Then open **http://localhost:8000/setup** in your browser to configure your radio connection and create an admin account.

## Required Flags

| Flag | Purpose |
|------|---------|
| `--network host` | Access host networking for local Meshtastic device discovery |
| `--privileged` | Required for Serial/USB access to the radio |
| `-v /dev:/dev` | Maps host device tree so the container can see serial hardware |
| `-v meshdash_data:/app/data` | Persists database, logs, and settings across updates |

## Ports

The runner defaults to port **8000** for backward compatibility with V2 users. C2-provisioned installs use **8181** (set during setup wizard). Override with `WEBSERVER_PORT` env var.

## Updating

The runner checks meshdash.co.uk for the latest version on every restart. If a newer version is available, it downloads and installs it automatically. Your data in `/app/data` is preserved across updates.

## Building from Source

If you want to build your own image (for offline/air-gapped deployments), use the app Dockerfile in the repo root:

```bash
docker build -t meshdash:r3.0 .
docker run -d --name meshdash --network host meshdash:r3.0
```

Note: self-built images don't auto-update. Use the runner for automatic updates.