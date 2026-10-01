# MeshDash R3.1.10 (unreleased)

## Security (all panels should update)
- **Authentication fix.** An authentication bypass let some protected API routes run for requests that were not logged in. All earlier versions are affected: please update. Logged-out requests are now always rejected (redirect to `/login` for pages, 401 for API calls).
- **Admin-only system actions.** Configuration, updates, restarts, plugin management and similar system actions now require a logged-in operator or admin. Public-mode viewers and spectator accounts can't use them.
- **Secrets stay on the server.** The settings API no longer returns secret values; saving the form with them blank keeps the stored ones.

## Updates can no longer leave a panel stuck
- **Restart in place.** Update and Restart now use `execv` instead of exiting cleanly and relying on a supervisor. Under systemd `Restart=on-failure` (or with no supervisor) the old clean exit left the panel down.
- **Verified before anything changes.** The update is streamed to a `.part` file, checked for zip integrity, sha256 (when the server publishes one), required files, and the version inside matching the version offered. Disk space and write access are checked first. A rejected download changes nothing.
- **Transactional install.** The release is staged and every `.py` compile-checked. Dependencies are resolved with `pip --dry-run` and installed only if `requirements.txt` changed. Every file about to be replaced or removed is backed up to `data/update_backups/`, then the swap is done file by file with atomic replaces. A failure undoes it immediately, and a power cut mid-install is undone on the next boot. Files a release removes are now deleted (tracked by `data/.release_manifest.json`).
- **Trial boot with automatic rollback.** The new version must start, stay up and serve the update/rollback/status routes. Otherwise the previous version is restored: on an unhandled startup crash, after 3 failed starts, if it isn't healthy within 5 minutes, or if critical routes are missing. A rolled-back version isn't installed again unless forced.
- **Paths no longer depend on the working directory.** The updater resolves the install from its own location. Previously, a panel started from another directory never applied its update, or applied it in the wrong place.
- `GET /api/system/update-status` and `POST /api/system/update-rollback` (manual rollback). The UI reports the outcome of the last update in the system log, and its restart screen waits for the real result instead of reloading blindly after 60s.
- The "Installing…" screen now actually appears (the broadcast used `g.main_event_loop` on a dict and silently failed). The radio is released properly before a restart (the coroutine was never awaited).

## Maps
- **OpenStreetMap is now the default basemap everywhere** (main map, overview, node detail, Geo Fence, Proximity Prune, ISS, Weather, Traceroute, Share Map). No API key needed. CARTO now requires a key, so **C2 Dark** uses CARTO only when `CARTO_BASEMAP_API_KEY` is set and shows OpenStreetMap otherwise; a map is never blank. All maps take their tile URL and attribution from `/api/map/carto-config.js` (`core/carto_basemap.browser_config`).
- CSP `img-src` now allows `tile.openstreetmap.org` (only `*.tile.openstreetmap.org` was allowed) and `server.arcgisonline.com`; Satellite tiles were being blocked.

## Reliability
- **Config writes are atomic** (temp file, fsync, replace, `.bak` kept) everywhere: settings, Web Setup, key removal. A crash mid-write used to leave an empty config, which re-ran setup and lost the community API key.
- **Web Setup keeps existing secrets** if the form leaves them empty.
- **`AUTH_SECRET_KEY` is saved once when missing.** It used to be regenerated on every start, which logged everyone out on every restart and update.
- **Fresh installs no longer run the R2→R3 self-heal.** A clean clone used to delete README.md, reinstall every dependency and crash on its first start.
- **Community heartbeat:** it uses the port the app actually listens on (`--port`), not a hard-coded 8000. It takes the node ID from the running app when the local API call fails, and logs its health on change instead of failing silently.
- **Docker runner (3.1.5):** the data restore no longer deletes `data/` when the copy fails.
- `scripts/meshdash.service`: a reference systemd unit for manual installs (`Restart=always`).
- `tests/test_update.py`: 33 tests covering every update failure path.

# MeshDash R3.1.3

## Bug Fixes
- **Version check without node**: The heartbeat/version check no longer requires a connected node (local_node_id) to work. Any running dashboard will always check for updates and report its version to the C2 server, even if no radio is connected. Previously, MQTT/WebSerial setups with invalid or missing node IDs would silently report "current" and never check for updates.
- **MQTT_NODE_ID hex parsing**: Non-hex MQTT node IDs (like `!md3989820`) no longer silently discard the node identity. The raw ID string is preserved as a fallback so `local_node_id` always gets set when MQTT_NODE_ID is configured.
- **Dashboard heartbeat ping**: The version check now sends `dashboard_version` and `X-Dashboard-Version` headers to the server, so the C2 backend knows which dashboards are online and what version they're running — even without a node connected.

# MeshDash R3.1.2

## Bug Fixes
- **Self-heal restart loop**: Fixed infinite Docker restart loop when self-heal migration fails with PermissionError or FileExistsError. The bootstrap marker is now written *before* any risky file operations, breaking the loop immediately. Stale backup dirs from failed attempts are cleaned up on entry. All I/O errors (not just PermissionError) are caught gracefully with `sys.exit(0)` instead of `sys.exit(1)`.
- **`shutil.copytree` FileExistsError**: Backup copy now uses `dirs_exist_ok=True` to handle partial backup dirs from previous failed attempts.
- **web_telemetry plugin crash**: Added missing `import asyncio` that caused `NameError` on startup.
- **macOS venv Python version**: Self-heal now prefers `sys.executable` over `/usr/bin/python3` when running Python 3.10+, fixing the `anyio==4.13.0` incompatibility on macOS.
- **Weather plugin daily/weekly schedules**: User-entered hour/minute was treated as UTC instead of being converted from local time. One-time schedules worked because `datetime-local` inputs go through `Date` (which handles timezone), but daily/weekly just passed raw numbers. Fixed: frontend now converts local time → UTC before sending to backend, and display converts UTC → local with a "(UTC HH:MM)" note when different. Users with existing daily schedules should recreate them.

# MeshDash R3.1.1

## Maintenance & Polish
- Updated Docker labels and version references to R3.1.1
- Cleaned up documentation and installation references
- CI workflow now properly reports lint and syntax failures

# MeshDash R3.0

## Architecture: Complete Core Rebuild
Modular file structure with 15 route modules, 3 connection handlers, 119+ API endpoints across 17k lines of Python. Every subsystem split into a dedicated module for easier maintenance and contribution.

### Setup
API key creation and endpoint configuration handled from the dashboard UI. Create, rotate, and manage access without re-running the installer.

### Install Migration
Smart migration detects existing mesh-dash installations, creates timestamped backups of databases and plugins, and preserves all user data on upgrade. R2.x installations are detected and migrated automatically.

### Startup Detection
Cloud-installed systems authenticate and log straight in. Manual installs redirect to /setup for first-time configuration.

### Multi-Radio Slots
Connect up to 16 Meshtastic radios simultaneously. Each slot gets its own isolated SQLite database, dedicated SSE stream, and independent connection config. Mix Serial, TCP, BLE, MQTT, and MeshCore on one dashboard.

### MQTT Connection
Connect to mqtt.meshtastic.org as an observer without owning a physical radio. Filter by region and channel. Stable for receiving; under active development for transmitting.

### MeshCore Connection
Alternative protocol via the meshcore Python library. Connects to MeshCore nodes over Serial, TCP, or BLE. Beta status.

### WebSerial
Configuration moved from the setup wizard to dashboard settings. Connect and disconnect browser-USB sessions from inside the app.

### Auth Hardening
JWT tokens stored in HttpOnly, SameSite cookies. Bcrypt password hashing with automatic salt generation. CSRF double-submit cookie protection on all state-changing requests. Optional TOTP two-factor authentication via pyotp.

### Packet Source Detection
Received-from-source attribution (RF/MQTT/LOCAL) with confidence scoring. Heuristic-based classification. Beta status.

### Self-Healing Bootstrap
R3.0 detects stale R2.x installations and repairs them automatically. Creates backups before any migration.

### Docker
Official `rusjpmd/meshdash-runner` Docker image with standalone mode (built-in `/setup` wizard) and C2 cloud setup. Auto-downloads the latest version on boot, auto-updates on restart, and migrates V2.0 data volumes automatically.

### Plugin System
Drop-in folder architecture with FastAPI router, static file server, sidebar nav, and lifecycle management. Zero core modifications needed.

### Remote Access
Five access tiers (off, heartbeat, monitor, read, operator, full) with HMAC-signed outbound-only polling. No port forwarding required.

## Known Issues


- WebSerial is configured from the dashboard settings page, not during initial setup.
- Custom plugins from R2.x may need path updates for the new file structure. Plugins from the official store are compatible as-is.
- Packet source attribution (RF/MQTT/LOCAL) is in beta. May misclassify in mixed RF/MQTT environments.
- ALL RADIOS mode: Node Config is intentionally disabled. Select a specific radio from the topbar switcher to configure it.
- ALL RADIOS mode: Channel configuration reads from the primary radio only.
