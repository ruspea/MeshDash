#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════════
# MeshDash R3.1.1 Docker Runner — Entrypoint
#
# Two boot modes:
#
#   Standalone Mode (no MD_SETUP_KEY):
#     1. Query meshdash.co.uk for the latest version
#     2. Download, extract, install deps
#     3. Start MeshDash → user sees /setup wizard
#
#   C2 Mode (with MD_SETUP_KEY):
#     1. Validate MD_SETUP_KEY and MD_SETUP_URL
#     2. Query MeshDash server for target version + zip URL + config
#     3. Download, extract, install deps, fetch config
#     4. Create c2_installed.flag → skip setup wizard
#     5. Start MeshDash → preconfigured, user logs in
#
# Both modes seed data/.r3_bootstrap_done to skip self-heal.
# Both modes build /opt/venv from requirements.txt if missing.
# Both modes default to port 8000.
# ═══════════════════════════════════════════════════════════════════

echo "══════════════════════════════════════════════"
echo "  MeshDash R3.1.1 Docker Runner"
echo "══════════════════════════════════════════════"

APP_PORT="${WEBSERVER_PORT:-8000}"
C2_MODE=false

# ── Mode Detection ───────────────────────────────────────────────
if [ -n "$MD_SETUP_KEY" ] && [ -n "$MD_SETUP_URL" ]; then
    C2_MODE=true
    echo "[INFO] MD_SETUP_KEY detected — C2 cloud setup mode"
else
    echo "[INFO] No MD_SETUP_KEY — standalone mode"
    echo "[INFO] Will download latest version from meshdash.co.uk"
fi

# ── Get Version Info ──────────────────────────────────────────────
if [ "$C2_MODE" = true ]; then
    echo "[INFO] Contacting MeshDash server for install info..."
    HTTP_CODE=$(curl -s -o /tmp/md_response.json -w "%{http_code}" "${MD_SETUP_URL}?action=get_install_info&key=${MD_SETUP_KEY}" 2>/dev/null)
    RESPONSE=$(cat /tmp/md_response.json 2>/dev/null)

    if [ "$HTTP_CODE" != "200" ] || [ -z "$RESPONSE" ]; then
        if [ "$HTTP_CODE" = "403" ]; then
            echo "[ERROR] Invalid API key. Generate a new one at https://meshdash.co.uk/"
        elif [ "$HTTP_CODE" = "404" ]; then
            echo "[ERROR] Configuration not found. Create a setup at https://meshdash.co.uk/"
        elif [ -z "$HTTP_CODE" ] || [ "$HTTP_CODE" = "000" ]; then
            echo "[ERROR] Cannot reach MeshDash server. Check your network connection."
        else
            echo "[ERROR] Server returned HTTP $HTTP_CODE"
        fi
        sleep 300
        exit 1
    fi

    TARGET_VERSION=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['install_info']['version'])" 2>/dev/null)
    ZIP_URL=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin)['install_info']['zip_url'])" 2>/dev/null)
else
    # Standalone: query the public versions API
    echo "[INFO] Fetching latest version from meshdash.co.uk..."
    VERSIONS_RESPONSE=$(curl -sf -X POST "https://meshdash.co.uk/user_setup_core.php" \
        -H "Content-Type: application/json" \
        -d '{"action":"list_versions"}' 2>/dev/null)

    if [ -z "$VERSIONS_RESPONSE" ]; then
        echo "[ERROR] Cannot reach meshdash.co.uk. Check your network connection."
        sleep 300
        exit 1
    fi

    # First version in the list is the latest (sorted descending)
    TARGET_VERSION=$(echo "$VERSIONS_RESPONSE" | python3 -c "import sys, json; v=json.load(sys.stdin)['versions']; print(v[0]['version'])" 2>/dev/null)
    ZIP_URL=$(echo "$VERSIONS_RESPONSE" | python3 -c "import sys, json; v=json.load(sys.stdin)['versions']; print(v[0]['download_url'])" 2>/dev/null)

    if [ -z "$TARGET_VERSION" ] || [ -z "$ZIP_URL" ]; then
        echo "[ERROR] Could not determine latest version from server."
        sleep 300
        exit 1
    fi
fi

echo "[INFO] Target version: $TARGET_VERSION"

# ── Check Local Version ──────────────────────────────────────────
CURRENT_VERSION="none"
if [ -f "version.tag" ]; then
    CURRENT_VERSION=$(cat version.tag)
fi

# ── Update / Install Logic ────────────────────────────────────────
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

    # Seed the R3 bootstrap marker so self-heal is skipped
    mkdir -p data
    touch data/.r3_bootstrap_done

    # C2 mode: mark as C2-installed so setup wizard is skipped
    if [ "$C2_MODE" = true ]; then
        touch data/c2_installed.flag
    fi

    # Standalone mode: write minimal config with PUBLIC_MODE=False
    # so the setup wizard shows instead of public-mode free-for-all
    if [ "$C2_MODE" = false ] && [ ! -f ".mesh-dash_config" ]; then
        cat > .mesh-dash_config << 'CONFIG'
# MeshDash — Docker Standalone Mode
# Configure your radio at http://localhost:8000/setup
PUBLIC_MODE=False
WEBSERVER_PORT=8000
MESHTASTIC_CONNECTION_TYPE=SERIAL
CONFIG
    fi

    # Mark version
    echo "$TARGET_VERSION" > version.tag
    echo "[INFO] Update complete — now at $TARGET_VERSION"
else
    echo "[INFO] Version $CURRENT_VERSION is up to date."
fi

# ── Ensure /opt/venv exists ───────────────────────────────────────
if [ ! -d "/opt/venv" ] || [ ! -f "/opt/venv/bin/python3" ]; then
    if [ -f "requirements.txt" ]; then
        echo "[INFO] /opt/venv missing — rebuilding..."
        python3 -m venv /opt/venv
        /opt/venv/bin/pip install --upgrade pip -q
        /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
    fi
fi

# ── Fetch Config (C2 mode only) ──────────────────────────────────
if [ "$C2_MODE" = true ]; then
    echo "[INFO] Refreshing configuration..."
    curl -sf "${MD_SETUP_URL}?action=download_config&key=${MD_SETUP_KEY}" -o .mesh-dash_config 2>/dev/null || {
        echo "[WARN] Could not download config — using existing config if present"
    }

    if [ ! -s ".mesh-dash_config" ] && [ -f "/tmp/config_backup" ]; then
        cp /tmp/config_backup .mesh-dash_config
        rm -f /tmp/config_backup
        echo "[WARN] Using preserved config (server download failed)"
    fi
fi

# ── Start MeshDash ────────────────────────────────────────────────
if [ "$C2_MODE" = true ]; then
    echo "══════════════════════════════════════════════"
    echo "  🚀 Starting MeshDash on port $APP_PORT (C2 configured)"
    echo "══════════════════════════════════════════════"
else
    echo "══════════════════════════════════════════════"
    echo "  🚀 Starting MeshDash on port $APP_PORT"
    echo "  📋 Open http://localhost:$APP_PORT/setup to configure"
    echo "══════════════════════════════════════════════"
fi

export PATH="/opt/venv/bin:$PATH"
exec /opt/venv/bin/python3 meshtastic_dashboard.py --host 0.0.0.0 --port "$APP_PORT"
