MeshDash R3.0 — Official Docker Runner
Self-updating Meshtastic Command & Control Dashboard

MeshDash is a powerful, persistent web dashboard for your Meshtastic nodes. It logs packet history, visualizes telemetry, manages messages, and provides a sleek interface for controlling your mesh hardware.

🔗 Generate your install command at: https://meshdash.co.uk/
📦 Source code (GPL-3.0): https://github.com/ruspea/MeshDash

🚀 Quick Start

Method 1: Cloud Setup (Recommended)
Generate a configuration profile on our website and the container auto-configures on first boot:

```
docker run -d \
  --name meshdash \
  --restart always \
  --network host \
  --privileged \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /dev:/dev \
  -v meshdash_data:/app/data \
  -e MD_SETUP_KEY="MD-YOUR-KEY-HERE" \
  -e MD_SETUP_URL="https://meshdash.co.uk/user_setup_core.php" \
  rusjpmd/meshdash-runner:latest
```

Method 2: Standalone (No Account Needed)
Just run the container and configure everything through the built-in web setup wizard:

```
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

Then open http://localhost:8000/setup in your browser to configure your radio connection and create an admin account.

🌟 Key Features
• Automatic Updates: With MD_SETUP_KEY, the container checks for and installs the latest MeshDash version on boot
• Built-in Setup Wizard: No account needed — configure everything through the web UI at /setup
• Persistent Database: SQLite stores node lists, message history, and telemetry logs across restarts
• Interactive Map: Visualizes node positions, traceroutes, and waypoints with Leaflet.js
• Telemetry & Graphs: Tracks battery levels, SNR, and environmental metrics over time
• Full Messaging: Send and receive private DMs and channel broadcasts with chat-log history
• Hardware Control: GPIO toggling, remote reboot, shutdown, and node management
• 32 Built-in Plugins: ISS tracker, weather alerts, auto-responder, task scheduler, mesh bulletin board, and more
• Automation: Built-in Auto-Reply system and Task Scheduler
• C2 Bridge: Optional Command & Control bridging for remote management over the internet

⚙️ Configuration Methods
Cloud Auto-Config (MD_SETUP_KEY)
When the container starts with MD_SETUP_KEY, it securely connects to the MeshDash server, downloads your settings, and applies them. Deploy to headless Raspberry Pis in seconds.

Standalone Mode (No Key)
Run the container without MD_SETUP_KEY and configure everything through the web setup wizard at http://localhost:8000/setup. Create an admin account, set your radio connection type (Serial/TCP/BLE), and you're running.

📂 Required Flags
• --network host: Access host networking for local Meshtastic device discovery
• --privileged: Required for Serial/USB access to the radio
• -v /dev:/dev: Maps host device tree so the container can see serial hardware
• -v meshdash_data:/app/data: Persists database, logs, and settings across container updates
• --log-opt max-size=10m --log-opt max-file=3: Prevents log disk exhaustion

📡 Hardware Support
• Serial/USB: Direct connection via USB cable (recommended)
• TCP/WiFi: Connection to a node on the local network via IP address
• BLE/Bluetooth: Connection via Bluetooth Low Energy

🛠 Troubleshooting
• Radio not connecting: docker logs -f meshdash — ensure --privileged and -v /dev:/dev are set
• Where is the dashboard? Port 8000 by default. Access via http://<host-ip>:8000
• Config not downloading: Verify MD_SETUP_KEY and MD_SETUP_URL are correct
• Fresh install with no key: Open http://localhost:8000/setup to run the setup wizard
