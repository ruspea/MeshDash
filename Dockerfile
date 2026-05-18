# Mesh Dash — Production Dockerfile
# Multi-stage build: keeps the final image small and clean

FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev libssl-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install to a venv
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

# Create non-root user
RUN groupadd -r meshdash && useradd -r -g meshdash -d /app meshdash

WORKDIR /app

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY --chown=meshdash:meshdash . .

# Create data directory with proper permissions
RUN mkdir -p /app/data && chown meshdash:meshdash /app/data

# Security hardening
USER meshdash:meshdash

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8181/api/status')" || exit 1

EXPOSE 8181

# Use exec form for proper signal handling
ENTRYPOINT ["python", "meshtastic_dashboard.py"]
CMD ["--host", "0.0.0.0", "--port", "8181"]

# Labels
LABEL org.opencontainers.image.title="Mesh Dash"
LABEL org.opencontainers.image.description="Meshtastic Mesh Network Dashboard"
LABEL org.opencontainers.image.version="R3.0"
LABEL org.opencontainers.image.source="https://github.com/ruspea/MeshDash"
