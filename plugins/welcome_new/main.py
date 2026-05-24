"""
Welcome New Nodes Plugin for MeshDash
=====================================
Automatically sends a welcome DM to newly discovered mesh nodes.

Features:
- Configurable welcome message with template variables
- Per-node cooldown (don't re-welcome within X hours)
- Delivery mode: DM or broadcast on a channel
- Include telemetry data in welcome message
- Watchdog heartbeat for plugin health monitoring
- Full REST API + dashboard UI
- Persistent welcome history (SQLite)
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

# Plugin directory and database
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PLUGIN_DIR, "welcome_history.db")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")

# Module-level context references (set by init_plugin)
_logger = None
_m_data = None
_c_mgr = None
_event_loop = None
_plugin_id = None
_node_registry: Dict[str, Any] = {}
_watchdog_dict = None

_watchdog_task: Optional[asyncio.Task] = None
_worker_task: Optional[asyncio.Task] = None

# Default configuration
_DEFAULT_CONFIG = {
    "enabled": True,
    "message": (
        "Welcome to the mesh!\n"
        "This node runs MeshDash — here's what's available:\n"
        "• Send 'ping' for a response\n"
        "• Send 'weather' for current conditions\n"
        "• Send 'help' for more commands\n"
        "Happy meshing!"
    ),
    "dest_type": "direct",        # "direct" = DM, "broadcast" = channel
    "channel": 0,                 # Channel index for broadcast mode
    "cooldown_hours": 24,         # Don't re-welcome same node within this many hours
    "include_telemetry": False,   # Include node telemetry in welcome message
    "include_position": False,    # Include position info in welcome message
    "welcome_self": False,        # Whether to welcome the local node
    "ignore_local": True,         # Don't welcome nodes with isLocal=True
}

_config: Dict[str, Any] = {}
_config_lock = threading.Lock()


def _load_config() -> Dict[str, Any]:
    global _config
    with _config_lock:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    _config = {**_DEFAULT_CONFIG, **loaded}
            else:
                _config = _DEFAULT_CONFIG.copy()
        except Exception as e:
            if _logger:
                _logger.error("WELCOME: config load error: %s", e)
            _config = _DEFAULT_CONFIG.copy()
        return _config.copy()


def _save_config(cfg: Dict[str, Any]) -> bool:
    global _config
    with _config_lock:
        try:
            merged = {**_DEFAULT_CONFIG, **cfg}
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2)
            _config = merged
            if _logger:
                _logger.info("WELCOME: config saved")
            return True
        except Exception as e:
            if _logger:
                _logger.error("WELCOME: config save error: %s", e)
            return False


def get_config() -> Dict[str, Any]:
    with _config_lock:
        if not _config:
            return _load_config()
        return _config.copy()


# Database — welcome history
_db_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        _db_local.conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
        _db_local.conn.row_factory = sqlite3.Row
        _db_local.conn.execute("PRAGMA journal_mode=WAL")
        _db_local.conn.execute("PRAGMA synchronous=NORMAL")
    return _db_local.conn


def _init_db():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS welcome_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id     TEXT NOT NULL,
            short_name  TEXT DEFAULT '',
            long_name   TEXT DEFAULT '',
            sent_at     REAL NOT NULL,
            message     TEXT DEFAULT '',
            dest_type   TEXT DEFAULT 'direct',
            channel     INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'sent'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_welcome_node ON welcome_history(node_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_welcome_sent ON welcome_history(sent_at)
    """)
    conn.commit()
    if _logger:
        _logger.info("WELCOME: database initialized at %s", DB_PATH)


def _db_add_welcome(node_id: str, short_name: str, long_name: str,
                    message: str, dest_type: str, channel: int, status: str = "sent"):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO welcome_history (node_id, short_name, long_name, sent_at, message, dest_type, channel, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (node_id, short_name, long_name, time.time(), message, dest_type, channel, status))
    conn.commit()


def _db_get_history(limit: int = 100) -> List[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM welcome_history ORDER BY sent_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def _db_get_last_welcome(node_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM welcome_history WHERE node_id = ? ORDER BY sent_at DESC LIMIT 1",
        (node_id,)
    ).fetchone()
    return dict(row) if row else None


def _db_clear_history():
    conn = _get_conn()
    conn.execute("DELETE FROM welcome_history")
    conn.commit()


# Message template rendering
_TEMPLATE_VARS = {
    "{node_id}": "The node's unique ID (e.g. !a1b2c3d4)",
    "{short_name}": "Node's short name (e.g. CLAW)",
    "{long_name}": "Node's long display name",
    "{hops}": "Hops away (if known)",
    "{snr}": "Signal-to-noise ratio (if known)",
    "{battery}": "Battery level % (if telemetry available)",
    "{voltage}": "Battery voltage (if telemetry available)",
    "{position}": "Last known position (if available)",
}


def _render_message(template: str, node_info: dict) -> str:
    """Replace template variables with actual node data."""
    msg = template
    user_info = node_info.get("user", {}) or {}
    device_metrics = node_info.get("deviceMetrics", {}) or {}

    msg = msg.replace("{node_id}", node_info.get("node_id", "unknown") or "unknown")
    msg = msg.replace("{short_name}", user_info.get("shortName", "???") or "???")
    msg = msg.replace("{long_name}", user_info.get("longName", "Unknown") or "Unknown")
    msg = msg.replace("{hops}", str(node_info.get("hopLimit", "?") or "?"))
    msg = msg.replace("{snr}", str(round(node_info.get("snr", 0), 1) if node_info.get("snr") is not None else "?"))
    msg = msg.replace("{battery}", str(int(device_metrics.get("batteryLevel", 0))) if device_metrics.get("batteryLevel") is not None else "?")
    msg = msg.replace("{voltage}", str(round(device_metrics.get("voltage", 0), 2)) if device_metrics.get("voltage") is not None else "?")

    # Position
    lat = node_info.get("latitude") or (node_info.get("position", {}) or {}).get("latitude")
    lon = node_info.get("longitude") or (node_info.get("position", {}) or {}).get("longitude")
    if lat is not None and lon is not None:
        msg = msg.replace("{position}", f"{lat:.4f}, {lon:.4f}")
    else:
        msg = msg.replace("{position}", "unknown")

    return msg


# Core logic — detect new nodes and send welcome
_MAX_NODES = 500  # Cap in-memory dicts to prevent unbounded growth
_known_nodes: Dict[str, float] = {}  # node_id -> first_seen timestamp (in-memory fast check)
_welcomed_nodes: Dict[str, float] = {}  # node_id -> last_welcomed timestamp (in-memory)
_lock = threading.Lock()


def _prune_dicts():
    """Remove oldest entries if dicts exceed _MAX_NODES."""
    if len(_known_nodes) > _MAX_NODES:
        sorted_items = sorted(_known_nodes.items(), key=lambda x: x[1])
        _known_nodes.clear()
        _known_nodes.update(sorted_items[-_MAX_NODES:])
    if len(_welcomed_nodes) > _MAX_NODES:
        sorted_items = sorted(_welcomed_nodes.items(), key=lambda x: x[1])
        _welcomed_nodes.clear()
        _welcomed_nodes.update(sorted_items[-_MAX_NODES:])


def _build_extra_message(node_info: dict, config: dict) -> str:
    """Build telemetry/position appendix if enabled."""
    parts = []
    if config.get("include_telemetry"):
        dm = node_info.get("deviceMetrics", {}) or {}
        if dm:
            lines = ["📊 Telemetry:"]
            if "batteryLevel" in dm:
                lines.append(f"  Battery: {dm['batteryLevel']}%")
            if "voltage" in dm:
                lines.append(f"  Voltage: {dm['voltage']:.2f}V")
            if "channelUtilization" in dm:
                lines.append(f"  Ch Util: {dm['channelUtilization']:.1f}%")
            if "airUtilTx" in dm:
                lines.append(f"  Air TX: {dm['airUtilTx']:.1f}%")
            if len(lines) > 1:
                parts.append("\n".join(lines))

    if config.get("include_position"):
        lat = node_info.get("latitude") or (node_info.get("position", {}) or {}).get("latitude")
        lon = node_info.get("longitude") or (node_info.get("position", {}) or {}).get("longitude")
        if lat is not None and lon is not None:
            parts.append(f"📍 Position: {lat:.4f}, {lon:.4f}")

    return "\n".join(parts) if parts else ""


async def _send_welcome(node_id: str, node_info: dict, slot_id: str = "node_0"):
    """Send a welcome message to a newly discovered node."""
    config = get_config()
    if not config.get("enabled", True):
        return

    # Skip local node
    if config.get("ignore_local", True) and node_info.get("isLocal"):
        if _logger:
            _logger.debug("WELCOME: skipping local node %s", node_id)
        return

    # Cooldown check
    cooldown_hours = config.get("cooldown_hours", 24)
    now = time.time()
    with _lock:
        last = _welcomed_nodes.get(node_id, 0)
        if (now - last) < (cooldown_hours * 3600):
            if _logger:
                _logger.debug("WELCOME: node %s on cooldown (%.1fh remaining)", node_id,
                              cooldown_hours - (now - last) / 3600)
            return
        _welcomed_nodes[node_id] = now
    _prune_dicts()

    # Get connection manager for the slot
    cm = _c_mgr
    if _node_registry and slot_id in _node_registry:
        slot_cm = getattr(_node_registry[slot_id], "connection_manager", None)
        if slot_cm:
            cm = slot_cm

    if not cm or not getattr(cm, "is_ready", None) or not cm.is_ready.is_set():
        if _logger:
            _logger.warning("WELCOME: radio not ready, cannot welcome %s", node_id)
        return

    # Build message
    template = config.get("message", _DEFAULT_CONFIG["message"])
    message = _render_message(template, node_info)
    extra = _build_extra_message(node_info, config)
    if extra:
        message = message + "\n" + extra

    # Truncate to Meshtastic max (230 bytes)
    msg_bytes = message.encode('utf-8')
    if len(msg_bytes) > 230:
        message = msg_bytes[:227].decode('utf-8', errors='ignore') + "..."

    dest_type = config.get("dest_type", "direct")
    channel = config.get("channel", 0)
    user_info = node_info.get("user", {}) or {}
    short_name = user_info.get("shortName", "")
    long_name = user_info.get("longName", "")

    try:
        if dest_type == "direct" and node_id:
            await cm.sendText(message, destinationId=node_id, channelIndex=0)
        else:
            await cm.sendText(message, destinationId="^all", channelIndex=channel)

        _db_add_welcome(node_id, short_name, long_name, message, dest_type, channel, "sent")
        if _logger:
            _logger.info("✅ WELCOME: sent to %s (%s)", short_name or node_id, dest_type)
    except Exception as e:
        _db_add_welcome(node_id, short_name, long_name, message, dest_type, channel, "failed")
        if _logger:
            _logger.error("❌ WELCOME: failed to send to %s: %s", node_id, e)


# Background worker — scans for new nodes
async def _welcome_worker():
    """Periodically scan meshtastic_data.nodes for newly discovered nodes."""
    if _logger:
        _logger.info("👋 Welcome worker started — scanning for new nodes every 10s")

    # Initial: mark all currently known nodes as already seen
    if _m_data and hasattr(_m_data, "nodes"):
        for nid in list(_m_data.nodes.keys()):
            with _lock:
                _known_nodes[nid] = time.time()
        _prune_dicts()

    while True:
        try:
            await asyncio.sleep(10)

            if not _m_data or not hasattr(_m_data, "nodes"):
                continue

            config = get_config()
            if not config.get("enabled", True):
                continue

            current_nodes = dict(_m_data.nodes)  # snapshot
            now = time.time()

            for nid, node_info in current_nodes.items():
                with _lock:
                    if nid in _known_nodes:
                        continue
                    _known_nodes[nid] = now

                # Skip our own node
                if node_info.get("isLocal"):
                    continue
                # Skip nodes without a user record (not fully discovered yet)
                if not node_info.get("user"):
                    continue

                if _logger:
                    sn = (node_info.get("user") or {}).get("shortName", nid)
                    _logger.info("👋 WELCOME: new node discovered — %s (%s)", sn, nid)

                await _send_welcome(nid, node_info)

        except asyncio.CancelledError:
            if _logger:
                _logger.info("👋 Welcome worker stopped")
            return
        except Exception as e:
            if _logger:
                _logger.error("WELCOME worker error: %s", e, exc_info=True)


# Watchdog heartbeat
async def _watchdog_heartbeat():
    while True:
        try:
            await asyncio.sleep(30)
            if _watchdog_dict is not None and _plugin_id:
                _watchdog_dict[_plugin_id] = time.time()
        except asyncio.CancelledError:
            return
        except Exception:
            pass


# Pydantic models
class ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    message: Optional[str] = None
    dest_type: Optional[str] = None
    channel: Optional[int] = None
    cooldown_hours: Optional[float] = None
    include_telemetry: Optional[bool] = None
    include_position: Optional[bool] = None
    welcome_self: Optional[bool] = None
    ignore_local: Optional[bool] = None


class ManualWelcome(BaseModel):
    node_id: str
    slot_id: str = "node_0"


# Plugin router
plugin_router = APIRouter()


@plugin_router.get("/config")
async def api_get_config():
    return get_config()


@plugin_router.patch("/config")
async def api_update_config(update: ConfigUpdate):
    cfg = get_config()
    for key, val in update.model_dump(exclude_none=True).items():
        if val is not None:
            cfg[key] = val
    _save_config(cfg)
    return get_config()


@plugin_router.post("/config/reset")
async def api_reset_config():
    _save_config(_DEFAULT_CONFIG.copy())
    return get_config()


@plugin_router.get("/template_vars")
async def api_template_vars():
    """Return available template variables for the message editor."""
    return {"variables": {k: v for k, v in _TEMPLATE_VARS.items()}}


@plugin_router.get("/history")
async def api_get_history(limit: int = Query(100, ge=1, le=1000)):
    rows = await asyncio.to_thread(_db_get_history, limit)
    # Format timestamps
    for r in rows:
        r["sent_at_iso"] = datetime.fromtimestamp(r["sent_at"], tz=timezone.utc).isoformat()
    return {"history": rows, "count": len(rows)}


@plugin_router.delete("/history")
async def api_clear_history():
    await asyncio.to_thread(_db_clear_history)
    return {"status": "cleared"}


@plugin_router.get("/known_nodes")
async def api_known_nodes():
    """Return the list of nodes we've already seen (won't be re-welcomed)."""
    with _lock:
        return {
            "known_count": len(_known_nodes),
            "welcomed_count": len(_welcomed_nodes),
            "nodes": [
                {"node_id": nid, "first_seen": ts}
                for nid, ts in sorted(_known_nodes.items(), key=lambda x: x[1])
            ]
        }


@plugin_router.post("/send")
async def api_manual_send(body: ManualWelcome):
    """Manually send a welcome message to a specific node."""
    cm = _c_mgr
    nr = _node_registry or {}
    if body.slot_id in nr:
        slot_cm = getattr(nr[body.slot_id], "connection_manager", None)
        if slot_cm:
            cm = slot_cm

    if not cm or not getattr(cm, "is_ready", None) or not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready")

    md = _m_data
    if body.slot_id in nr:
        slot_md = getattr(nr[body.slot_id], "meshtastic_data", None)
        if slot_md:
            md = slot_md

    node_info = md.nodes.get(body.node_id) if md and hasattr(md, "nodes") else None
    if not node_info:
        raise HTTPException(404, f"Node {body.node_id} not found")

    config = get_config()
    template = config.get("message", _DEFAULT_CONFIG["message"])
    message = _render_message(template, node_info)
    extra = _build_extra_message(node_info, config)
    if extra:
        message = message + "\n" + extra
    msg_bytes = message.encode('utf-8')
    if len(msg_bytes) > 230:
        message = msg_bytes[:227].decode('utf-8', errors='ignore') + "..."

    dest_type = config.get("dest_type", "direct")
    channel = config.get("channel", 0)

    try:
        if dest_type == "direct":
            await cm.sendText(message, destinationId=body.node_id, channelIndex=0)
        else:
            await cm.sendText(message, destinationId="^all", channelIndex=channel)

        user_info = node_info.get("user", {}) or {}
        await asyncio.to_thread(
            _db_add_welcome,
            body.node_id,
            user_info.get("shortName", ""),
            user_info.get("longName", ""),
            message,
            dest_type,
            channel,
            "sent"
        )
        return {"status": "sent", "node_id": body.node_id, "message": message}
    except Exception as e:
        await asyncio.to_thread(
            _db_add_welcome,
            body.node_id, "", "", message, dest_type, channel, "failed"
        )
        raise HTTPException(500, f"Send failed: {e}")


@plugin_router.get("/nodes")
async def api_list_nodes():
    """Return current known mesh nodes for the UI picker."""
    md = _m_data
    if not md or not hasattr(md, "nodes"):
        return {"nodes": []}
    nodes = []
    for nid, info in md.nodes.items():
        user = info.get("user", {}) or {}
        nodes.append({
            "node_id": nid,
            "short_name": user.get("shortName", ""),
            "long_name": user.get("longName", ""),
            "is_local": info.get("isLocal", False),
            "hw_model": user.get("hwModel", ""),
        })
    return {"nodes": nodes}


@plugin_router.get("/status")
async def api_status():
    config = get_config()
    with _lock:
        known = len(_known_nodes)
        welcomed = len(_welcomed_nodes)
    rows = await asyncio.to_thread(_db_get_history, 1000)
    return {
        "state": "ready",
        "ready": True,
        "plugin": "welcome_new",
        "version": "1.0.0",
        "enabled": config.get("enabled", True),
        "known_nodes": known,
        "welcomed_nodes": welcomed,
        "total_welcomes_sent": len(rows),
    }


# Plugin lifecycle
def init_plugin(context: dict):
    global _logger, _m_data, _c_mgr, _event_loop, _plugin_id
    global _node_registry, _watchdog_dict

    _m_data = context.get("meshtastic_data")
    _c_mgr = context.get("connection_manager")
    _event_loop = context.get("event_loop")
    _plugin_id = context.get("plugin_id", "welcome_new")
    _logger = context.get("logger") or logging.getLogger("welcome_new")
    _node_registry = context.get("node_registry", {})
    _watchdog_dict = context.get("plugin_watchdog")

    _load_config()
    _init_db()

    # Hydrate cooldown from DB so restarts don't re-welcome recently-seen nodes
    try:
        recent = _db_get_history(limit=500)
        cooldown_h = get_config().get("cooldown_hours", 24)
        cutoff = time.time() - (cooldown_h * 3600)
        for row in recent:
            nid = row.get("node_id")
            sent_at = row.get("sent_at", 0)
            if nid and sent_at and sent_at > cutoff:
                _welcomed_nodes[nid] = sent_at
        if _welcomed_nodes:
            _logger.info("WELCOME: hydrated %d cooldown entries from DB", len(_welcomed_nodes))
    except Exception as e:
        _logger.warning("WELCOME: cooldown hydration failed: %s", e)

    loop = _event_loop
    if loop is None:
        _logger.warning("WELCOME: no event_loop — worker cannot start")
        return

    asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(), loop)
    asyncio.run_coroutine_threadsafe(_welcome_worker(), loop)
    _logger.info("👋 Welcome New Nodes plugin v1.0 initialised")