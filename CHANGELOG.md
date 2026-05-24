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
