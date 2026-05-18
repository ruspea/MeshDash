<p align="center">
  <img src="https://meshdash.co.uk/static/icons/meshdash-logo.png" alt="MeshDash" width="120" />
</p>

<h1 align="center">MeshDash</h1>

<p align="center">
  Open-source self-hosted Meshtastic Command & Control dashboard<br>
  Manage, monitor, and automate your mesh network locally
</p>

<p align="center">
  <a href="https://meshdash.co.uk">Website</a> ·
  <a href="https://meshdash.co.uk/docs/">Documentation</a> ·
  <a href="https://meshdash.co.uk/c2_setup.php">Setup Wizard</a> ·
  <a href="https://meshdash.co.uk/docs/?page=api-core">REST API</a> ·
  <a href="https://meshdash.co.uk/docs/?page=plugin-development">Plugins</a>
</p>

<p align="center">
  <a href="https://www.wikidata.org/wiki/Q139844395"><img src="https://img.shields.io/badge/Wikidata-Q139844395-blue" alt="Wikidata" /></a>
  <a href="https://meshdash.co.uk"><img src="https://img.shields.io/badge/Website-meshdash.co.uk-green" alt="Website" /></a>
  <img src="https://img.shields.io/badge/version-R3.0-orange" alt="Version" />
  <img src="https://img.shields.io/badge/license-GPL--3.0--only-blue" alt="License" />
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Raspberry%20Pi%20%7C%20WSL2-lightgrey" alt="Platform" />
</p>

---

## What is MeshDash?

MeshDash is a self-hosted command and control platform for [Meshtastic](https://meshtastic.org) mesh radio networks. It runs entirely on your hardware — Raspberry Pi, Linux server, or WSL2 — and connects to your Meshtastic radios via Serial, TCP, BLE, MQTT, MeshCore, or WebSerial. No cloud dependency. No port forwarding required for remote access. Your data stays on your network unless you choose otherwise.

It provides a real-time web dashboard with instant Server-Sent Events (SSE) updates — no polling lag — and a comprehensive REST API with 100+ endpoints for integrations, automation, and scripting.

## Installation

### Option 1 — C2 Setup Wizard (recommended)

The fastest way to get MeshDash running. The setup wizard generates a personalised one-liner installer configured for your radio connection, network, and preferences.

1. Go to **[https://meshdash.co.uk/c2_setup.php](https://meshdash.co.uk/c2_setup.php)**
2. Enter your radio connection details and preferences
3. Copy the generated install command
4. Run it on your machine

```bash
# Example — your command will be customised by the wizard
curl -sL https://meshdash.co.uk/versions/R3.0/install.sh | bash
```

The wizard handles:
- Radio connection type selection (Serial, TCP, BLE, MQTT, MeshCore, WebSerial)
- Network configuration and web server port
- Automatic Python venv setup and dependency installation
- systemd service creation for auto-start on boot
- Migration from existing R2.x installations with timestamped backups
- C2-provisioned flag for seamless first login

### Option 2 — Manual Installation

Clone the repository and set up MeshDash yourself. Full control, no wizard dependencies.

```bash
# Clone the repository
git clone https://github.com/ruspea/MeshDash.git
cd MeshDash

# Create a Python virtual environment
python3 -m venv mesh-dash_venv
source mesh-dash_venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the initial setup
python3 meshtastic_dashboard.py
```

Then open `http://localhost:8282/setup` in your browser to complete the first-time configuration.

**Requirements:** Python 3.9+, a Meshtastic radio (or MQTT observer mode), Linux / Raspberry Pi / WSL2.

> **Docker support** is temporarily unavailable in R3.0 and will return in R3.1. Use the native installers above.

## Core Features

### Live Node C2

Real-time node monitoring updated instantly via Server-Sent Events — no polling lag.

- Battery level, SNR, RSSI, GPS coordinates, and signal quality bars
- Source detection (RF / MQTT / LOCAL) with confidence scoring
- Node cards with online/offline status and last-seen timestamps
- Link quality scoring and fleet overview

### Interactive Mapping

Leaflet-based GPS map with rich visualisation options.

- Node positions with live GPS tracking
- Trajectory paths and movement history
- RF neighbour link overlays showing mesh topology
- Multiple tile styles (street, satellite, topographic)
- Polar grid overlay with azimuth lines and range rings (via plugin)
- Geofencing with unlimited zones — circle, polygon, corridor, node-relative triggers (via plugin)
- ISS tracker overlay (via plugin)

### Messaging

Full mesh messaging from the dashboard.

- Direct P2P messages to any node
- Channel broadcasting to all listeners
- Live ACK delivery tracking and unread counters
- 230-character message limit enforcement
- Emoji picker integration (via plugin)

### MeshShark Analyser

Wireshark-style packet capture and analysis — built directly into the dashboard.

- BPF syntax filtering for targeted packet inspection
- Three-pane detail view: packet list, decoded metadata, raw JSON
- Packet type filtering (all types, or filter by protocol)
- Source evidence scoring (RF / MQTT / LOCAL attribution)
- Live capture mode with real-time packet ingestion

### Network Diagnostics

Live traceroute and mesh health tools.

- Hop-by-hop bidirectional SNR measurement
- Traceroute results plotted on the map
- Historical traceroute log
- Path statistics and analysis

### Analytics

Comprehensive telemetry charting across your entire mesh.

- **9 telemetry metrics:** Battery %, SNR dB, Channel Utilisation %, Voltage, Position, Routing, Neighbour count, Power, Temperature
- Time ranges from 1 hour to 30 days
- Network-wide averages
- Side-by-side comparison for up to 4 nodes with linked Y-axes

### Automation & Rules

Event-driven automation engine — no scripting required.

- **Task Scheduler:** Cron-based message broadcasts with 3-retry logic and web sensor ingress
- **Auto-Reply Engine:** Regex-powered matching with 20+ dynamic placeholders, per-sender cooldowns, and hierarchical menu trees
- **Telemetry Threshold Alerts:** Battery, voltage, SNR, temperature, and uptime triggers with configurable thresholds
- **Web Telemetry Ingress:** Extract data from websites and broadcast over mesh

### Radio Management

Direct radio configuration from the dashboard — no CLI needed.

- Full protobuf config read/write to radio flash memory
- Up to 16 simultaneous radio slots with isolated databases
- Each slot has its own SQLite database, SSE stream, and connection config
- Mix Serial, TCP, BLE, MQTT, and MeshCore on one dashboard
- Radio slot switching from the topbar
- Automatic reboot after config write

### Extensibility — Plugin System

Drop-in plugin architecture. Add features without touching core code.

- FastAPI router integration for custom API endpoints
- Static file server for plugin UI assets
- Sidebar navigation hooks
- Plugin lifecycle management (install, enable, disable, remove)
- 30+ community plugins available in the built-in store

## Connectivity

MeshDash supports every way to talk to a Meshtastic radio:

| Method | Description |
|---|---|
| **Serial** | USB serial connection to a local radio |
| **TCP** | Network connection over TCP (direct to node) |
| **BLE** | Bluetooth Low Energy connection |
| **MQTT** | Connect to mqtt.meshtastic.org as an observer — no physical radio required. Filter by region and channel |
| **MeshCore** | Alternative protocol via the meshcore Python library over Serial, TCP, or BLE |
| **WebSerial** | Browser-based USB serial — configure and disconnect from the dashboard settings page |

## Remote Access

Access your MeshDash dashboard from any network — **no port forwarding, dynamic DNS, or VPN required.** Your server polls meshdash.co.uk for queued commands using HMAC-signed outbound-only connections.

Five configurable access tiers (default: off):

| Tier | Name | Permissions |
|---|---|---|
| 0 | Off | No remote access |
| 1 | Heartbeat Only | Server appears on the community map at meshdash.co.uk. No inbound commands |
| 2 | Read-Only Telemetry | Node list, packet stats, telemetry query. No write access |
| 3 | Messaging | All Tier 2 + send messages to any node or channel |
| 4 | Operator | Full dashboard access — all API endpoints, task management, auto-reply, config read-write |
| 5 | Full C2 | All Tier 4 + system restart, config file write, slot management, plugin control |

## Security

MeshDash implements a layered security model:

- **JWT** tokens stored in HttpOnly, SameSite cookies — not accessible to JavaScript
- **Bcrypt** password hashing with automatic salt generation
- **CSRF** double-submit cookie protection on all state-changing requests
- **TOTP** two-factor authentication (optional, via pyotp)
- **HMAC-signed** remote access with rate limiting
- **API key** authentication for programmatic access with per-key node-ID locking
- **Automatic IP blocking** after repeated authentication failures
- **Rate limiting** with configurable windows and block durations
- **Request body size limits** (2MB default)
- **Prepared statement caching** to prevent SQL injection

Data never leaves the local network unless you explicitly enable remote access or community features.

## REST API

100+ endpoints across 8 API groups:

| API Group | Coverage |
|---|---|
| **Core** | Nodes, messages, heartbeats, mesh data, node activity |
| **Authentication** | API key CRUD, auth logs, session management |
| **Monitor** | Node monitoring, telemetry thresholds, alerts |
| **Tasks** | Cron scheduler, outbox management, retry logic |
| **Auto-Reply** | Regex rules, cooldowns, menu trees |
| **Plugins** | Install, enable, disable, configure, store browse |
| **System** | Server stats, config, diagnostics, queue management |
| **WebSerial** | Browser serial connection management |

Full documentation: [meshdash.co.uk/docs/?page=api-core](https://meshdash.co.uk/docs/?page=api-core)

```bash
# Example — get all nodes
curl -H "X-Api-Key: YOUR_API_KEY" \
  https://your-meshdash-host/api/nodes

# Example — broadcast a message
curl -X POST -H "X-Api-Key: YOUR_API_KEY" \
  -d '{"message": "Hello mesh"}' \
  https://your-meshdash-host/api/broadcast
```

## Plugin Ecosystem

### Core Plugins (included)

| Plugin | Description |
|---|---|
| **Apprise Notifications** | Send mesh events to 130+ services — Discord, Telegram, Slack, Email, and more |
| **Auto-Reply** | Hierarchical auto-reply with slot/channel/DM filtering, smart reply routing, per-rule cooldowns |
| **Auto-Responder** | Automatic DM replies with anti-spam cooldown |
| **Channel Vault** | Server-side PSK vault for unlimited channel configurations beyond the radio's 8 slots |
| **Cold Nodes** | Detects and badges stale nodes not heard within a configurable period |
| **Emoji Picker** | Adds emoji picker to all message input fields |
| **Geo Fence** | Advanced geofencing — unlimited zones (circle, polygon, corridor, node-relative) with triggers |
| **Google Translate** | Site-wide Google Translate widget integration |
| **Hello Mesh** | Interactive API explorer & plugin development tutorial |
| **ISS Tracker** | Real-time International Space Station position on the map with crew details |
| **Medi AI** | Medical advice via local offline models or external hosted LLMs |
| **Mesh Analytics** | Comprehensive mesh analytics — node scoring, RF analysis, topology mapping |
| **Mesh BB** | Fully-featured mesh-native bulletin board system |
| **Mesh Chat AI** | AI chatbot with dynamically selectable personality modes |
| **Mesh Ping** | Active round-trip ping sessions with RTT measurement, launch from UI or trigger via DM |
| **Mesh Traceroute** | Dedicated traceroute plugin (placeholder for expanded implementation) |
| **Mesh Visualizer** | 3D mesh network visualiser — interactive rendering of nodes and links |
| **Network Intelligence** | Per-node Link Quality Scores, Smart Hops, and network Entropy computation |
| **Node Admin** | Complete remote node management — identity, role, LoRa radio, position, channels |
| **Node Analytics** | Per-node telemetry analysis with charts for power, RF, network metrics |
| **Node Comparison** | Side-by-side node comparison (placeholder) |
| **Node Ignore** | Persistent per-node ignore list — ignored nodes hidden from all dashboard views |
| **Node Monitor** | Telemetry threshold alerting — battery, voltage, SNR, temperature, uptime |
| **PKI Alerts** | Trust On First Use (TOFU) security with RF anomaly profiling and spoofing detection |
| **Polar Grid** | Azimuth lines and range rings centred on your node, interactive distance measurement |
| **Proximity Pruner** | Automatically purge nodes beyond a configurable radius from the database |
| **Push Notifications** | Real-time Web Push alerts via SSE bridge — DMs, channels, keyword triggers |
| **Share Map** | Create public embeddable widgets — live maps, node stat cards, network stats |
| **TCP Proxy** | Virtual Meshtastic TCP server — connect official mobile apps, Python CLI, or other clients |
| **Theme Editor** | Live CSS variable theming — pick accent colours, backgrounds, and styles |
| **Weather** | Fetch current weather from Open-Meteo and broadcast to the mesh |
| **Web Telemetry** | Extract data from websites and broadcast over mesh — one-time or scheduled |
| **Welcome New Nodes** | Automatically DM newly discovered nodes with a configurable welcome message |

### Build Your Own

Plugins are self-contained Python packages dropped into the `plugins_core/` directory. Each plugin gets:

- A FastAPI router for custom API endpoints
- A static file server for UI assets
- Sidebar navigation entries
- Lifecycle management (install, enable, disable, remove)

[Plugin Development Guide →](https://meshdash.co.uk/docs/?page=plugin-development)

## C2 Admin Dashboard

MeshDash includes a full admin dashboard for managing your C2 infrastructure:

- **Node registry** — active nodes, path analysis, activity logs
- **API key management** — create, rotate, toggle, and delete API keys from the UI
- **Command queue** — add, cancel, and monitor pending commands and outbox
- **Auth logging** — all authentication attempts with IP blocking and rate limit stats
- **Security overview** — real-time security posture dashboard
- **Heartbeat monitoring** — trend analysis and reset controls
- **Data dumps** — request and result management with volume analytics
- **Server stats** — live system metrics

## Technology Stack

| Layer | Technologies |
|---|---|
| **Backend** | Python, FastAPI, SQLite, asyncio, meshtastic Python library |
| **Frontend** | HTML/JS, Server-Sent Events (SSE), Web Serial API, Leaflet |
| **Security** | JWT, bcrypt, HttpOnly cookies, CSRF double-submit, TOTP 2FA, HMAC |
| **Connectivity** | Serial, TCP, BLE, MQTT, MeshCore, WebSerial |
| **Multi-Radio** | Up to 16 simultaneous radio slots with isolated databases |
| **Database** | SQLite (per-slot isolation), prepared statement caching |

## Documentation

| Topic | Link |
|---|---|
| Installation Guide | [meshdash.co.uk/?p=install](https://meshdash.co.uk/?p=install) |
| C2 Setup Wizard | [meshdash.co.uk/c2_setup.php](https://meshdash.co.uk/c2_setup.php) |
| Security & Authentication | [meshdash.co.uk/?p=security](https://meshdash.co.uk/?p=security) |
| Multi-Radio Slots | [meshdash.co.uk/?p=multiradio](https://meshdash.co.uk/?p=multiradio) |
| Remote Access | [meshdash.co.uk/?p=remote](https://meshdash.co.uk/?p=remote) |
| Hardware Guide | [meshdash.co.uk/?p=hardware](https://meshdash.co.uk/?p=hardware) |
| REST API Reference | [meshdash.co.uk/docs/?page=api-core](https://meshdash.co.uk/docs/?page=api-core) |
| Plugin Development | [meshdash.co.uk/docs/?page=plugin-development](https://meshdash.co.uk/docs/?page=plugin-development) |
| Database Schema | [meshdash.co.uk/docs/?page=database-schema](https://meshdash.co.uk/docs/?page=database-schema) |
| C2 Terminal | [meshdash.co.uk/docs/?page=c2-terminal](https://meshdash.co.uk/docs/?page=c2-terminal) |
| Connection Manager | [meshdash.co.uk/docs/?page=connection-manager](https://meshdash.co.uk/docs/?page=connection-manager) |
| Troubleshooting | [meshdash.co.uk/docs/?page=troubleshooting](https://meshdash.co.uk/docs/?page=troubleshooting) |
| Changelog | [meshdash.co.uk/?p=changelog](https://meshdash.co.uk/?p=changelog) |

## Requirements

- **Python** 3.9+
- **OS** — Linux (Debian/Ubuntu), Raspberry Pi OS, or WSL2
- **Radio** — Any Meshtastic-compatible radio, or MQTT observer mode (no physical radio needed)
- **Network** — Local network access to the radio

## License

MeshDash is licensed under [GPL-3.0-only](LICENSE). It is free to use, modify, and distribute under the terms of the GNU General Public License v3.

---

<p align="center">
  Built for operators who run their own infrastructure.
</p>