#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════════
# MeshDash R3.0 Docker Runner — Entrypoint
#
# Two boot modes:
#
#   C2 Mode (with MD_SETUP_KEY):
#     1. Validate MD_SETUP_KEY and MD_SETUP_URL
#     2. Query MeshDash server for target version + zip URL
#     3. Download, extract, install deps, fetch config
#     4. Start MeshDash
#
#   Standalone Mode (no MD_SETUP_KEY):
#     1. Start MeshDash with no config
#     2. App detects no users → shows /setup wizard
#     3. User configures everything through the web UI
#
# Both modes seed data/.r3_bootstrap_done to skip self-heal.
# Both modes build /opt/venv from requirements.txt if missing.
# ═══════════════════════════════════════════════════════════════════

echo "══════════════════════════════════════════════"
echo "  🦀 MeshDash R3.0 Docker Runner"
echo "══════════════════════════════════════════════"

# Default to 8000 for backward compatibility with V2.0 Docker commands.
# Users can override with WEBSERVER_PORT env var if they want 8181.
APP_PORT="${WEBSERVER_PORT:-8000}"

# ── Mode detection ────────────────────────────────────────────────
if [ -z "$MD_SETUP_KEY" ] || [ -z "$MD_SETUP_URL" ]; then
    # ── Standalone Mode ──────────────────────────────────────────
    echo "[INFO] No MD_SETUP_KEY set — starting in standalone mode"
    echo "[INFO] Setup wizard will be available at http://localhost:$APP_PORT/setup"
    echo ""

    # Ensure /opt/venv exists
    if [ ! -d "/opt/venv" ] || [ ! -f "/opt/venv/bin/python3" ]; then
        if [ -f "requirements.txt" ]; then
            echo "[INFO] Building Python virtual environment (/opt/venv)..."
            python3 -m venv /opt/venv
            /opt/venv/bin/pip install --upgrade pip -q
            echo "[INFO] Installing dependencies from requirements.txt..."
            /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
        fi
    fi

    # Seed the R3 bootstrap marker
    mkdir -p data
    touch data/.r3_bootstrap_done

    # Write a minimal config so PUBLIC_MODE=False (setup wizard shows)
    # Without this, config.py defaults PUBLIC_MODE=True (no auth, no setup)
    if [ ! -f ".mesh-dash_config" ]; then
        cat > .mesh-dash_config << 'CONFIG'
# MeshDash — Docker Standalone Mode
# Configure your radio at http://localhost:8000/setup
PUBLIC_MODE=False
WEBSERVER_PORT=8000
MESHTASTIC_CONNECTION_TYPE=SERIAL
CONFIG
    fi

    echo "══════════════════════════════════════════════"
    echo "  🚀 Starting MeshDash on port $APP_PORT"
    echo "  📋 Open http://localhost:$APP_PORT/setup to configure"
    echo "══════════════════════════════════════════════"
    export PATH="/opt/venv/bin:$PATH"
    exec /opt/venv/bin/python3 meshtastic_dashboard.py --host 0.0.0.0 --port "$APP_PORT"
fi

# ── C2 Mode ──────────────────────────────────────────────────────
echo "[INFO] MD_SETUP_KEY detected — C2 cloud setup mode"

# ── 2. Get Version Info from Server ──────────────────────────────
echo "[INFO] Contacting MeshDash server for install info..."
HTTP_CODE=$(curl -s -o /tmp/md_response.json -w "%{http_code}" "${MD_SETUP_URL}?action=get_install_info&key=${MD_SETUP_KEY}" 2>/dev/null)
RESPONSE=$(cat /tmp/md_response.json 2>/dev/null)

if [ "$HTTP_CODE" != "200" ] || [ -z "$RESPONSE" ]; then
    if [ "$HTTP_CODE" = "403" ]; then
        echo "[ERROR] Invalid API key. Generate a new one at https://meshdash.co.uk/"
    elif [ "$HTTP_CODE" = "404" ]; then
        echo "[ERROR] Configuration not found. Please create a setup first at https://meshdash.co.uk/"
    elif [ -z "$HTTP_CODE" ] || [ "$HTTP_CODE" = "000" ]; then
        echo "[ERROR] Cannot reach MeshDash server. Check your network connection."
    else
        echo "[ERROR] Server returned HTTP $HTTP_CODE"
    fi
    sleep 300
    exit 1
fi

# Parse the JSON response
TARGET_VERSION=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['install_info']['version'])" 2>/dev/null)
ZIP_URL=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['install_info']['zip_url'])" 2>/dev/null)
CONFIG_URL=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['install_info']['config_url'])" 2>/dev/null)

if [ -z "$TARGET_VERSION" ] || [ -z "$ZIP_URL" ]; then
    echo "[ERROR] Invalid response from server — missing version or zip URL."
    sleep 300
    exit 1
fi

echo "[INFO] Server target version: $TARGET_VERSION"

# ── 3. Check Local Version ──────────────────────────────────────
CURRENT_VERSION="none"
if [ -f "version.tag" ]; then
    CURRENT_VERSION=$(cat version.tag)
fi

# ── 4. Update / Install Logic ────────────────────────────────────
if [ "$CURRENT_VERSION" != "$TARGET_VERSION" ] || [ ! -f "meshtastic_dashboard.py" ]; then
    echo "[INFO] Update required (Current: $CURRENT_VERSION → Target: $TARGET_VERSION)"

    # Preserve data directory and config before cleaning
    echo "[INFO] Preserving data/ and .mesh-dash_config..."
    if [ -d "data" ]; then
        mv data /tmp/data_backup 2>/dev/null || true
    fi
    if [ -f ".mesh-dash_config" ]; then
        cp .mesh-dash_config /tmp/config_backup 2>/dev/null || true
    fi

    # Clean old app files (preserve data and config)
    echo "[INFO] Cleaning old files..."
    find . -maxdepth 1 -type f -not -name '.mesh-dash_config' -not -name 'version.tag' -delete
    find . -maxdepth 1 -type d -not -name '.' -not -name 'data' -exec rm -rf {} +

    # Download and extract
    echo "[INFO] Downloading MeshDash $TARGET_VERSION..."
    wget -q "$ZIP_URL" -O app.zip

    echo "[INFO] Extracting..."
    unzip -o -q app.zip
    rm app.zip

    # Restore data directory
    if [ -d "/tmp/data_backup" ]; then
        cp -rn /tmp/data_backup/* data/ 2>/dev/null || true
        rm -rf /tmp/data_backup
    fi

    # Build /opt/venv from the shipped requirements.txt
    if [ -f "requirements.txt" ]; then
        echo "[INFO] Building Python virtual environment (/opt/venv)..."
        python3 -m venv /opt/venv
        /opt/venv/bin/pip install --upgrade pip -q
        echo "[INFO] Installing dependencies from requirements.txt..."
        /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
    else
        echo "[WARN] No requirements.txt found — skipping dependency install"
    fi

    # Seed the R3 bootstrap marker
    mkdir -p data
    touch data/.r3_bootstrap_done

    # Mark C2-installed so setup wizard is skipped
    touch data/c2_installed.flag

    # Mark version
    echo "$TARGET_VERSION" > version.tag
    echo "[INFO] Update complete — now at $TARGET_VERSION"
else
    echo "[INFO] Version $CURRENT_VERSION is up to date."
fi

# ── 5. Ensure /opt/venv exists ───────────────────────────────────
if [ ! -d "/opt/venv" ] || [ ! -f "/opt/venv/bin/python3" ]; then
    if [ -f "requirements.txt" ]; then
        echo "[INFO] /opt/venv missing — rebuilding..."
        python3 -m venv /opt/venv
        /opt/venv/bin/pip install --upgrade pip -q
        /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
    fi
fi

# ── 6. Fetch Latest Config ───────────────────────────────────────
echo "[INFO] Refreshing configuration..."
curl -sf "${MD_SETUP_URL}?action=download_config&key=${MD_SETUP_KEY}" -o .mesh-dash_config 2>/dev/null || {
    echo "[WARN] Could not download config — using existing config if present"
}

if [ ! -s ".mesh-dash_config" ] && [ -f "/tmp/config_backup" ]; then
    cp /tmp/config_backup .mesh-dash_config
    rm -f /tmp/config_backup
    echo "[WARN] Using preserved config (server download failed)"
fi

# ── 7. Start MeshDash ────────────────────────────────────────────
echo "══════════════════════════════════════════════"
echo "  🚀 Starting MeshDash on port $APP_PORT"
echo "══════════════════════════════════════════════"
export PATH="/opt/venv/bin:$PATH"
exec /opt/venv/bin/python3 meshtastic_dashboard.py --host 0.0.0.0 --port "$APP_PORT"