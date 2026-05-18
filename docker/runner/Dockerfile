# MeshDash R3.0 Runner — Self-updating Docker Container
#
# This is the official Docker runner for MeshDash R3.0+.
# Unlike the app Dockerfile (which bundles the source), this runner
# is a thin bootstrap image that downloads and installs MeshDash
# from meshdash.co.uk on first boot, then auto-updates on restarts.
#
# Usage:
#   docker run -d \
#     --name meshdash \
#     --restart always \
#     --network host \
#     --privileged \
#     -v /dev:/dev \
#     -v meshdash_data:/app/data \
#     -e MD_SETUP_KEY="MD-YOUR-KEY-HERE" \
#     -e MD_SETUP_URL="https://meshdash.co.uk/user_setup_core.php" \
#     rusjpmd/meshdash-runner:3.0

FROM python:3.12-slim

LABEL maintainer="info@meshdash.co.uk"
LABEL description="Official MeshDash Runner R3.0 — Self-updating Meshtastic Dashboard"
LABEL version="3.0"
LABEL org.opencontainers.image.title="MeshDash Runner"
LABEL org.opencontainers.image.description="Self-updating Docker container for MeshDash R3.0+"
LABEL org.opencontainers.image.version="3.0"
LABEL org.opencontainers.image.source="https://github.com/ruspea/MeshDash"

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_CACHE_DIR=/app/data/.cache
ENV PIP_NO_CACHE_DIR=1

# Install system dependencies required for:
# - Serial/USB access (usbutils)
# - Package downloads (wget, curl)
# - Bluetooth (bluez for BLE)
# - Building native Python extensions (gcc, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    curl \
    usbutils \
    bluez \
    gcc \
    libffi-dev \
    libssl-dev \
    libbluetooth-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Allow the app to write to /app (needed for zip extraction, self-heal backups)
RUN chmod 777 /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/api/status || exit 1

ENTRYPOINT ["/entrypoint.sh"]