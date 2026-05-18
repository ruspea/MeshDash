MeshDash R3.0 — Official Docker Runner
Self-updating Meshtastic Command & Control Dashboard

MeshDash is a powerful, persistent web dashboard for your Meshtastic nodes. It logs packet history, visualizes telemetry, manages messages, and provides a sleek interface for controlling your mesh hardware.

This runner container handles installation and automatically manages updates to the latest version of MeshDash on boot.

All source code is open source (GPL-3.0) at https://github.com/ruspea/MeshDash

🔗 Generate your install command at: https://meshdash.co.uk/

🚀 Quick Start (Recommended)
The easiest way to get running is to generate a configuration profile on our website:

1. Go to https://meshdash.co.uk/
2. Use the setup tool to define your settings (Serial Port, Node preferences, Map settings)
3. Copy your unique API Key and the generated Docker command
4. Run the command on your host (Raspberry Pi, VPS, Server)

The container will automatically fetch your configuration on the first boot, install the latest version of the dashboard, and initialize the system.

Docker Run Command
```
docker run -d \
  --name meshdash \
  --restart always \
  --network host \
  --privileged \
  -v /dev:/dev \
  -v meshdash_data:/app/data \
  -e MD_SETUP_KEY="YOUR_API_KEY_HERE" \
  -e MD_SETUP_URL="https://meshdash.co.uk/user_setup_core.php" \
  rusjpmd/meshdash-runner:latest
```

Replace YOUR_API_KEY_HERE with the key provided during the setup process on the website.

⚠️ R3.0 Breaking Change: Port changed from 8000 to 8181

🌟 Key Features
• Automatic Updates: The container checks for and installs the latest MeshDash version automatically on boot
• Persistent Database: SQLite stores node lists, message history, and telemetry logs across restarts
• Interactive Map: Visualizes node positions, traceroutes, and waypoints with Leaflet.js
• Telemetry & Graphs: Tracks battery levels, SNR, and environmental metrics over time
• Full Messaging: Send and receive private DMs and channel broadcasts with chat-log history
• Hardware Control: GPIO toggling, remote reboot, shutdown, and node management
• 32 Built-in Plugins: ISS tracker, weather alerts, auto-responder, task scheduler, mesh bulletin board, and more
• Automation: Built-in Auto-Reply system and Task Scheduler
• C2 Bridge: Optional Command & Control bridging for remote management over the internet

⚙️ Configuration Methods
Method 1: Cloud Auto-Config (Recommended)
When the container starts with the MD_SETUP_KEY environment variable, it securely connects to the MeshDash server, downloads your specific settings, and applies them immediately. Deploy to headless Raspberry Pis in seconds.

Method 2: Manual Configuration (Advanced)
If you prefer not to use the cloud setup, you can manually configure the application. Instructions for manual configuration are available on the MeshDash website.

📂 Volumes & Permissions
• --network host: Required. Allows the container to access the host's networking stack to communicate with local Meshtastic devices via TCP/HTTP or local discovery.
• --privileged: Required for Serial/USB. Allows the container to access /dev/ttyUSB* or /dev/ttyACM* devices to communicate with the radio.
• -v /dev:/dev: Maps the host device tree so the container can see the Meshtastic hardware.
• -v meshdash_data:/app/data: Persists your database (meshdash_data.db), logs, and local settings so data isn't lost on container updates.

📡 Hardware Support
• Serial/USB: (Recommended) Direct connection via USB cable to the host
• TCP/WiFi: Connection to a node on the local network via IP address
• BLE/Bluetooth: Connection via Bluetooth Low Energy

🛠 Troubleshooting
• Radio not connecting: Check the logs with docker logs -f meshdash — ensure you included --privileged and -v /dev:/dev
• Where is the dashboard? R3.0+ runs on port 8181 by default. Access via http://<host-ip>:8181
• Config not downloading: Ensure MD_SETUP_KEY and MD_SETUP_URL are set correctly