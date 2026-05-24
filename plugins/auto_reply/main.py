"""
Auto-Reply Plugin for MeshDash v2.0
===================================

Self-contained hierarchical auto-reply system with:
- Folder/action tree structure for interactive menus
- Pattern matching: contains, exact, regex
- Per-sender session state with TTL
- Per-slot cooldown tracking
- 21+ response macro tokens
- Runtime enable/disable
- Deploy Demo Menu feature
- Debug panel for monitoring

v2.0 Enhancements:
- Global listening configuration (slots, channels, DMs)
- Per-rule scope overrides (slot_ids, channel_ids, listen_dm, listen_channel)
- Smart reply routing: DM→DM, Channel→Channel with @!nodeId prefix
- Hardened input validation and edge case handling
"""

import sqlite3
import time
import re
import os
import json
import threading
import asyncio
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import time

from fastapi import APIRouter, Body, HTTPException, Path as PathParam, Query, status

# pydantic is always available in this environment — use it directly
from pydantic import BaseModel, Field

# Plugin directory and database
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PLUGIN_DIR, "auto_reply.db")
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")
AUTO_REPLY_TABLE = "auto_reply_rules"

# Module-level context references (set by init_plugin)
_logger = None
_db_manager = None
_meshtastic_data = None
_connection_manager = None
_event_loop = None
_plugin_id = None

# Task handle so we can cancel cleanly on shutdown
_watchdog_task: Optional[asyncio.Task] = None


# WATCHDOG HEARTBEAT
# Because this plugin sets "watchdog": true in manifest.json, the MeshDash
# core will track it in _plugin_watchdog and expect a heartbeat every 30 s.
# If no heartbeat arrives within 120 s, the core marks the plugin as "hung".


async def _watchdog_heartbeat():
    """
    Pings the MeshDash core watchdog every 30 s.
    Writes:  watchdog_dict[plugin_id] = time.time()
    """
    while True:
        try:
            await asyncio.sleep(30)
            wd  = _meshtastic_data.get("plugin_watchdog") if isinstance(_meshtastic_data, dict) else None
            if wd is not None and _plugin_id:
                wd[_plugin_id] = time.time()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# Debug log buffer (circular, last 100 events)
_debug_log: deque = deque(maxlen=100)
_debug_lock = threading.Lock()


def _log_debug(event_type: str, data: Dict[str, Any]) -> None:
    """Add an event to the debug log."""
    with _debug_lock:
        _debug_log.append({
            "ts": datetime.now().isoformat(),
            "type": event_type,
            **data
        })


def get_debug_log() -> List[Dict]:
    """Return the debug log as a list (newest first)."""
    with _debug_lock:
        return list(reversed(_debug_log))


def clear_debug_log() -> int:
    """Clear the debug log, return count cleared."""
    with _debug_lock:
        count = len(_debug_log)
        _debug_log.clear()
        return count


# Global Configuration
_DEFAULT_CONFIG = {
    "enabled": True,
    "listen_slots": [],
    "listen_channels": [],
    "listen_dm": True,
    "listen_channel": True,
    "channel_reply_prefix": True,
    "session_ttl_seconds": 300,
}

_config: Dict[str, Any] = {}
_config_lock = threading.Lock()


def _load_config() -> Dict[str, Any]:
    """Load configuration from disk, falling back to defaults."""
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
                _logger.error("CONFIG_LOAD: %s", e)
            _config = _DEFAULT_CONFIG.copy()
        return _config.copy()


def _save_config(cfg: Dict[str, Any]) -> bool:
    """Save configuration to disk."""
    global _config
    with _config_lock:
        try:
            validated = {**_DEFAULT_CONFIG, **cfg}
            validated["listen_slots"] = list(validated.get("listen_slots") or [])
            validated["listen_channels"] = list(validated.get("listen_channels") or [])
            validated["enabled"] = bool(validated.get("enabled", True))
            validated["listen_dm"] = bool(validated.get("listen_dm", True))
            validated["listen_channel"] = bool(validated.get("listen_channel", True))
            validated["channel_reply_prefix"] = bool(validated.get("channel_reply_prefix", True))
            validated["session_ttl_seconds"] = int(validated.get("session_ttl_seconds", 300))
            
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(validated, f, indent=2)
            _config = validated
            if _logger:
                _logger.info("CONFIG_SAVE: Saved configuration")
            return True
        except Exception as e:
            if _logger:
                _logger.error("CONFIG_SAVE: %s", e)
            return False


def get_config() -> Dict[str, Any]:
    """Get current configuration."""
    with _config_lock:
        if not _config:
            return _load_config()
        return _config.copy()


def set_config(cfg: Dict[str, Any]) -> bool:
    """Update configuration."""
    return _save_config(cfg)


def is_enabled() -> bool:
    """Check if auto-reply engine is enabled."""
    return get_config().get("enabled", True)


def set_enabled(value: bool) -> None:
    """Enable or disable auto-reply engine."""
    cfg = get_config()
    cfg["enabled"] = bool(value)
    _save_config(cfg)
    if _logger:
        _logger.info("AUTO_REPLY: engine %s", "ENABLED" if value else "DISABLED")


# Cooldown tracking: keyed by (slot_id, rule_id, sender_id)
_cooldowns: Dict[str, Dict[int, Dict[str, float]]] = {}
_cooldown_lock = threading.Lock()


def _check_cooldown(slot_id: str, rule_id: int, sender_id: str, cooldown_sec: int) -> bool:
    """Returns True if sender is still in cooldown for this rule."""
    if cooldown_sec <= 0:
        return False
    key = f"{slot_id}:{sender_id}"
    now = time.time()
    with _cooldown_lock:
        slot_cd = _cooldowns.setdefault(slot_id, {})
        rule_cd = slot_cd.setdefault(rule_id, {})
        last_time = rule_cd.get(key, 0)
        return (now - last_time) < cooldown_sec


def _set_cooldown(slot_id: str, rule_id: int, sender_id: str) -> None:
    """Mark cooldown start time."""
    key = f"{slot_id}:{sender_id}"
    with _cooldown_lock:
        slot_cd = _cooldowns.setdefault(slot_id, {})
        rule_cd = slot_cd.setdefault(rule_id, {})
        rule_cd[key] = time.time()


def clear_rule_cooldowns(rule_id: int) -> None:
    """Clear all cooldowns for a deleted rule."""
    with _cooldown_lock:
        for slot_cd in _cooldowns.values():
            slot_cd.pop(rule_id, None)


# Session tracking: per-sender menu navigation state
_sessions: Dict[str, Dict[str, Any]] = {}
_session_lock = threading.Lock()


def _get_session_parent(slot_id: str, sender_id: str) -> Optional[int]:
    """Get the current menu context (parent_id) for a sender."""
    ttl = get_config().get("session_ttl_seconds", 300)
    key = f"{slot_id}:{sender_id}"
    now = time.time()
    with _session_lock:
        sess = _sessions.get(key)
        if sess and (now - sess.get("ts", 0)) < ttl:
            return sess.get("parent_id")
        _sessions.pop(key, None)
        return None


def _set_session_parent(slot_id: str, sender_id: str, parent_id: int) -> None:
    """Set the current menu context for a sender."""
    key = f"{slot_id}:{sender_id}"
    with _session_lock:
        _sessions[key] = {"parent_id": parent_id, "ts": time.time()}


def _clear_session(slot_id: str, sender_id: str) -> None:
    """Clear menu context for a sender."""
    key = f"{slot_id}:{sender_id}"
    with _session_lock:
        _sessions.pop(key, None)


def get_all_sessions() -> Dict[str, Dict]:
    """Get all active sessions (for debug)."""
    ttl = get_config().get("session_ttl_seconds", 300)
    now = time.time()
    with _session_lock:
        return {
            k: v for k, v in _sessions.items()
            if (now - v.get("ts", 0)) < ttl
        }


# Database

def _get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Get a database connection with WAL mode and proper settings."""
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_auto_reply_db(db_path: str = DB_PATH) -> None:
    """Initialize or migrate the auto-reply database."""
    conn = None
    try:
        conn = _get_db_connection(db_path)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {AUTO_REPLY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_phrase TEXT NOT NULL,
                match_type TEXT DEFAULT 'contains',
                response_message TEXT NOT NULL,
                cooldown_seconds INTEGER DEFAULT 60,
                is_enabled INTEGER DEFAULT 1,
                parent_id INTEGER,
                node_type TEXT DEFAULT 'action',
                label TEXT DEFAULT '',
                scope_slots TEXT DEFAULT NULL,
                scope_channels TEXT DEFAULT NULL,
                scope_listen_dm INTEGER DEFAULT NULL,
                scope_listen_channel INTEGER DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Migration: add v2.0 columns if missing
        cursor = conn.execute(f"PRAGMA table_info({AUTO_REPLY_TABLE})")
        columns = {row[1] for row in cursor.fetchall()}
        
        migrations = [
            ("scope_slots", "TEXT DEFAULT NULL"),
            ("scope_channels", "TEXT DEFAULT NULL"),
            ("scope_listen_dm", "INTEGER DEFAULT NULL"),
            ("scope_listen_channel", "INTEGER DEFAULT NULL"),
        ]
        
        for col_name, col_def in migrations:
            if col_name not in columns:
                conn.execute(f"ALTER TABLE {AUTO_REPLY_TABLE} ADD COLUMN {col_name} {col_def}")
                if _logger:
                    _logger.info("DB_MIGRATE: Added column %s", col_name)
        
        conn.commit()
        if _logger:
            _logger.info("AUTO_REPLY DB: Initialized at %s", db_path)
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_INIT: %s", e)
    finally:
        if conn:
            conn.close()
    
    _load_config()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a database row to a dictionary, parsing JSON fields."""
    d = dict(row)
    d["is_enabled"] = bool(d.get("is_enabled"))
    
    for field in ["scope_slots", "scope_channels"]:
        val = d.get(field)
        if val is not None:
            try:
                d[field] = json.loads(val)
            except Exception:
                d[field] = None
    
    for field in ["scope_listen_dm", "scope_listen_channel"]:
        val = d.get(field)
        if val is None:
            d[field] = None
        else:
            d[field] = bool(val)
    
    return d


def db_get_auto_reply_rules(
    only_enabled: bool = False,
    db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    """Retrieve all auto-reply rules."""
    conn = None
    try:
        conn = _get_db_connection(db_path)
        query = f"SELECT * FROM {AUTO_REPLY_TABLE}"
        if only_enabled:
            query += " WHERE is_enabled = 1"
        query += " ORDER BY id"
        rows = conn.execute(query).fetchall()
        return [_row_to_dict(r) for r in rows]
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_GET_RULES: %s", e)
        return []
    finally:
        if conn:
            conn.close()


def db_get_auto_reply_rule_by_id(
    rule_id: int,
    db_path: str = DB_PATH
) -> Optional[Dict[str, Any]]:
    """Retrieve a single rule by ID."""
    conn = None
    try:
        conn = _get_db_connection(db_path)
        row = conn.execute(
            f"SELECT * FROM {AUTO_REPLY_TABLE} WHERE id = ?", (rule_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_GET_RULE: %s", e)
        return None
    finally:
        if conn:
            conn.close()


def db_add_auto_reply_rule(
    trigger_phrase: str,
    match_type: str,
    response_message: str,
    cooldown_seconds: int = 60,
    is_enabled: bool = True,
    parent_id: Optional[int] = None,
    node_type: str = "action",
    label: str = "",
    scope_slots: Optional[List[str]] = None,
    scope_channels: Optional[List[int]] = None,
    scope_listen_dm: Optional[bool] = None,
    scope_listen_channel: Optional[bool] = None,
    db_path: str = DB_PATH
) -> Optional[int]:
    """Add a new auto-reply rule."""
    if not trigger_phrase or not trigger_phrase.strip():
        raise ValueError("Trigger phrase cannot be empty.")
    if not response_message or not response_message.strip():
        raise ValueError("Response message cannot be empty.")
    if match_type not in ("contains", "exact", "regex"):
        raise ValueError(f"Invalid match_type: {match_type}")
    if match_type == "regex":
        try:
            re.compile(trigger_phrase)
        except re.error as e:
            raise ValueError(f"Invalid regex '{trigger_phrase}': {e}") from e
    
    scope_slots_json = json.dumps(scope_slots) if scope_slots is not None else None
    scope_channels_json = json.dumps(scope_channels) if scope_channels is not None else None
    scope_dm_int = None if scope_listen_dm is None else (1 if scope_listen_dm else 0)
    scope_ch_int = None if scope_listen_channel is None else (1 if scope_listen_channel else 0)
    
    conn = None
    try:
        conn = _get_db_connection(db_path)
        cur = conn.execute(
            f"""INSERT INTO {AUTO_REPLY_TABLE}
                (trigger_phrase, match_type, response_message, cooldown_seconds,
                 is_enabled, parent_id, node_type, label,
                 scope_slots, scope_channels, scope_listen_dm, scope_listen_channel)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trigger_phrase.strip(), match_type, response_message,
             max(0, int(cooldown_seconds)), 1 if is_enabled else 0,
             parent_id, node_type if node_type in ("folder", "action") else "action",
             label or "",
             scope_slots_json, scope_channels_json, scope_dm_int, scope_ch_int)
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_ADD: %s", e)
        return None
    finally:
        if conn:
            conn.close()


def db_update_auto_reply_rule(
    rule_id: int,
    trigger_phrase: str,
    match_type: str,
    response_message: str,
    cooldown_seconds: int = 60,
    is_enabled: bool = True,
    parent_id: Optional[int] = None,
    node_type: str = "action",
    label: str = "",
    scope_slots: Optional[List[str]] = None,
    scope_channels: Optional[List[int]] = None,
    scope_listen_dm: Optional[bool] = None,
    scope_listen_channel: Optional[bool] = None,
    db_path: str = DB_PATH
) -> Optional[Dict[str, Any]]:
    """Update an existing auto-reply rule."""
    if not trigger_phrase or not trigger_phrase.strip():
        raise ValueError("Trigger phrase cannot be empty.")
    if not response_message or not response_message.strip():
        raise ValueError("Response message cannot be empty.")
    if match_type not in ("contains", "exact", "regex"):
        raise ValueError(f"Invalid match_type: {match_type}")
    if match_type == "regex":
        try:
            re.compile(trigger_phrase)
        except re.error as e:
            raise ValueError(f"Invalid regex '{trigger_phrase}': {e}") from e
    
    scope_slots_json = json.dumps(scope_slots) if scope_slots is not None else None
    scope_channels_json = json.dumps(scope_channels) if scope_channels is not None else None
    scope_dm_int = None if scope_listen_dm is None else (1 if scope_listen_dm else 0)
    scope_ch_int = None if scope_listen_channel is None else (1 if scope_listen_channel else 0)
    
    conn = None
    try:
        conn = _get_db_connection(db_path)
        conn.execute(
            f"""UPDATE {AUTO_REPLY_TABLE} SET
                trigger_phrase=?, match_type=?, response_message=?, cooldown_seconds=?,
                is_enabled=?, parent_id=?, node_type=?, label=?,
                scope_slots=?, scope_channels=?, scope_listen_dm=?, scope_listen_channel=?
                WHERE id = ?""",
            (trigger_phrase.strip(), match_type, response_message,
             max(0, int(cooldown_seconds)), 1 if is_enabled else 0,
             parent_id, node_type if node_type in ("folder", "action") else "action",
             label or "",
             scope_slots_json, scope_channels_json, scope_dm_int, scope_ch_int,
             rule_id)
        )
        conn.commit()
        return db_get_auto_reply_rule_by_id(rule_id, db_path)
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_UPDATE: %s", e)
        return None
    finally:
        if conn:
            conn.close()


def db_delete_auto_reply_rule(rule_id: int, db_path: str = DB_PATH) -> bool:
    """Delete an auto-reply rule."""
    conn = None
    try:
        conn = _get_db_connection(db_path)
        cur = conn.execute(f"DELETE FROM {AUTO_REPLY_TABLE} WHERE id = ?", (rule_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            clear_rule_cooldowns(rule_id)
        return deleted
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_DELETE: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def db_get_rules_tree(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Get rules as a nested tree structure."""
    flat = db_get_auto_reply_rules(db_path=db_path)
    by_id = {r["id"]: {**r, "children": []} for r in flat}
    roots = []
    for node in by_id.values():
        pid = node.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(node)
        else:
            roots.append(node)
    return roots


def db_clear_all_rules(db_path: str = DB_PATH) -> int:
    """Delete all rules. Returns count deleted."""
    conn = None
    try:
        conn = _get_db_connection(db_path)
        cur = conn.execute(f"DELETE FROM {AUTO_REPLY_TABLE}")
        conn.commit()
        return cur.rowcount
    except sqlite3.Error as e:
        if _logger:
            _logger.exception("DB_CLEAR_ALL: %s", e)
        return 0
    finally:
        if conn:
            conn.close()


def db_deploy_demo_menu(db_path: str = DB_PATH) -> int:
    """Deploy a demonstration menu tree. Returns count of rules created."""
    conn = None
    try:
        conn = _get_db_connection(db_path)
        conn.execute(f"DELETE FROM {AUTO_REPLY_TABLE}")
        conn.execute(f"DELETE FROM sqlite_sequence WHERE name='{AUTO_REPLY_TABLE}'")
        conn.commit()
    except sqlite3.Error as e:
        if _logger:
            _logger.error("DEMO_MENU: failed to clear table: %s", e)
        return 0
    finally:
        if conn:
            conn.close()
    
    ids = {}
    
    def add_rule(name, trigger, match, response, cooldown, parent_name, node_type, label):
        parent_id = ids.get(parent_name) if parent_name else None
        new_id = db_add_auto_reply_rule(
            trigger_phrase=trigger,
            match_type=match,
            response_message=response,
            cooldown_seconds=cooldown,
            is_enabled=True,
            parent_id=parent_id,
            node_type=node_type,
            label=label,
            db_path=db_path
        )
        if new_id:
            ids[name] = new_id
        return new_id is not None
    
    count = 0
    
    # Root menu
    if add_rule("main", "!md", "exact",
        "📡 MESHDASH MENU\n──────────────\n1️⃣ info - Node Information\n2️⃣ net - Network Status\n3️⃣ help - Command Help\n\nReply with option name.",
        5, None, "folder", "Main Menu"):
        count += 1
    
    # Info submenu
    if add_rule("info", "info", "exact",
        "ℹ️ NODE INFO\n──────────────\n• id - Show Node ID\n• bat - Battery Status\n• loc - Location\n• back - Return to Main",
        5, "main", "folder", "Info Menu"):
        count += 1
    
    # Info actions
    if add_rule("info_id", "id", "exact",
        "📟 NODE: {name}\nID: {node_id}\nShort: {short_name}\nModel: {hw_model}",
        30, "info", "action", "Show ID"):
        count += 1
    
    if add_rule("info_bat", "bat", "exact",
        "🔋 BATTERY STATUS\nLevel: {battery_level}%\nVoltage: {voltage}V",
        30, "info", "action", "Battery"):
        count += 1
    
    if add_rule("info_loc", "loc", "exact",
        "📍 LOCATION\nCoords: {location}\nAltitude: {altitude}m\nSats: {sats_in_view}",
        30, "info", "action", "Location"):
        count += 1
    
    if add_rule("info_back", "back", "exact",
        "📡 MESHDASH MENU\n──────────────\n1️⃣ info - Node Information\n2️⃣ net - Network Status\n3️⃣ help - Command Help\n\nReply with option name.",
        2, "info", "folder", "Back to Main"):
        count += 1
    
    # Net submenu
    if add_rule("net", "net", "exact",
        "📶 NETWORK STATUS\n──────────────\n• sig - Signal Quality\n• util - Channel Utilization\n• back - Return to Main",
        5, "main", "folder", "Network Menu"):
        count += 1
    
    # Net actions
    if add_rule("net_sig", "sig", "exact",
        "📶 SIGNAL QUALITY\nSNR: {snr} dB\nRSSI: {rssi} dBm",
        30, "net", "action", "Signal"):
        count += 1
    
    if add_rule("net_util", "util", "exact",
        "📊 CHANNEL UTILIZATION\nChannel: {channel_utilization}%\nTX Air: {air_util_tx}%",
        30, "net", "action", "Utilization"):
        count += 1
    
    if add_rule("net_back", "back", "exact",
        "📡 MESHDASH MENU\n──────────────\n1️⃣ info - Node Information\n2️⃣ net - Network Status\n3️⃣ help - Command Help\n\nReply with option name.",
        2, "net", "folder", "Back to Main"):
        count += 1
    
    # Help action
    if add_rule("help", "help", "exact",
        "❓ COMMAND HELP\n\nSend '!md' to open the main menu.\nNavigate by replying with option names.\nMenus timeout after 5 minutes.\n\nTokens: {name}, {battery_level}, {location}, etc.",
        30, "main", "action", "Help"):
        count += 1
    
    # Standalone commands
    if add_rule("env", "!env", "exact",
        "🌡️ ENVIRONMENT\nTemp: {temperature}°C\nHumidity: {humidity}%\nPressure: {pressure} hPa",
        60, None, "action", "Environment"):
        count += 1
    
    if add_rule("ping", "!ping", "exact",
        "🏓 PONG from {name}!\nLast heard: {last_heard}",
        10, None, "action", "Ping"):
        count += 1
    
    if _logger:
        _logger.info("DEMO_MENU: deployed %d rules", count)
    return count


# Placeholder replacement (21+ tokens)

def replace_placeholders(message: str, node_info: Dict[str, Any]) -> str:
    """Replace {tokens} in message with node data."""
    if not message or not node_info:
        return message or ""
    
    def _dig(*keys):
        """Dig into nested dicts."""
        obj = node_info
        for k in keys:
            if isinstance(obj, dict):
                obj = obj.get(k)
            else:
                return None
        return obj
    
    def _flat(*keys):
        """Try multiple flat keys."""
        for k in keys:
            v = node_info.get(k)
            if v is not None:
                return v
        return None
    
    # Last heard formatting
    last_heard_raw = _flat("last_heard", "lastHeard")
    if last_heard_raw:
        try:
            if isinstance(last_heard_raw, (int, float)):
                last_heard_str = datetime.fromtimestamp(last_heard_raw).strftime("%Y-%m-%d %H:%M")
            else:
                last_heard_str = str(last_heard_raw)
        except Exception:
            last_heard_str = "N/A"
    else:
        last_heard_str = "N/A"
    
    # Location
    lat = _dig("position", "latitude") or _flat("latitude")
    lon = _dig("position", "longitude") or _flat("longitude")
    if lat is not None and lon is not None:
        try:
            location_str = f"{float(lat):.5f}, {float(lon):.5f}"
        except Exception:
            location_str = "N/A"
    else:
        location_str = "N/A"
    
    altitude = _dig("position", "altitude") or _flat("altitude")
    sats = _dig("position", "satsInView") or _flat("sats_in_view", "satsInView")
    role_raw = _dig("user", "role") or _flat("role")
    
    token_map = {
        "name": _dig("user", "longName") or _flat("long_name", "longName") or "N/A",
        "node_id": _flat("node_id", "nodeId", "nodeNum", "num") or "N/A",
        "short_name": _dig("user", "shortName") or _flat("short_name", "shortName") or "N/A",
        "battery_level": _dig("deviceMetrics", "batteryLevel") or _flat("battery_level", "batteryLevel") or "N/A",
        "voltage": _dig("deviceMetrics", "voltage") or _flat("voltage") or "N/A",
        "snr": _flat("snr", "rxSnr") or "N/A",
        "rssi": _flat("rssi", "rxRssi") or "N/A",
        "location": location_str,
        "latitude": f"{float(lat):.5f}" if lat is not None else "N/A",
        "longitude": f"{float(lon):.5f}" if lon is not None else "N/A",
        "altitude": str(altitude) if altitude is not None else "N/A",
        "temperature": _dig("environmentMetrics", "temperature") or _flat("temperature") or "N/A",
        "humidity": _dig("environmentMetrics", "relativeHumidity") or _flat("humidity") or "N/A",
        "pressure": _dig("environmentMetrics", "barometricPressure") or _flat("pressure") or "N/A",
        "channel_utilization": _dig("deviceMetrics", "channelUtilization") or _flat("channel_utilization") or "N/A",
        "air_util_tx": _dig("deviceMetrics", "airUtilTx") or _flat("air_util_tx") or "N/A",
        "firmware_version": _dig("deviceMetrics", "firmwareVersion") or _flat("firmware_version") or "N/A",
        "hw_model": _dig("user", "hwModel") or _flat("hw_model") or "N/A",
        "role": str(role_raw) if role_raw is not None else "N/A",
        "last_heard": last_heard_str,
        "sats_in_view": str(sats) if sats is not None else "N/A",
    }
    
    def _fmt(v):
        if v is None or v == "N/A":
            return "N/A"
        if isinstance(v, float):
            return f"{v:.0f}" if v == int(v) else f"{v:.2f}"
        return str(v)
    
    for ph in re.findall(r"\{([^}]+)\}", message):
        if ph in token_map:
            message = message.replace(f"{{{ph}}}", _fmt(token_map[ph]))
    
    return message


# Message matching engine with scope filtering

def _rule_applies_to_context(
    rule: Dict[str, Any],
    slot_id: str,
    channel_index: Optional[int],
    is_dm: bool,
    global_config: Dict[str, Any]
) -> bool:
    """
    Check if a rule applies to the current message context.
    Per-rule scope overrides global config; NULL = inherit global.
    """
    # Slot filtering
    rule_slots = rule.get("scope_slots")
    if rule_slots is not None:
        if slot_id not in rule_slots:
            return False
    else:
        global_slots = global_config.get("listen_slots", [])
        if global_slots and slot_id not in global_slots:
            return False
    
    # DM vs Channel type filtering
    rule_listen_dm = rule.get("scope_listen_dm")
    rule_listen_ch = rule.get("scope_listen_channel")
    
    if is_dm:
        listen_dm = rule_listen_dm if rule_listen_dm is not None else global_config.get("listen_dm", True)
        if not listen_dm:
            return False
    else:
        listen_ch = rule_listen_ch if rule_listen_ch is not None else global_config.get("listen_channel", True)
        if not listen_ch:
            return False
        
        rule_channels = rule.get("scope_channels")
        if rule_channels is not None:
            if channel_index not in rule_channels:
                return False
        else:
            global_channels = global_config.get("listen_channels", [])
            if global_channels and channel_index not in global_channels:
                return False
    
    return True


def _matches_rule(incoming_lower: str, incoming_original: str, rule: Dict[str, Any]) -> bool:
    """Check if message matches rule's trigger pattern."""
    trigger = rule.get("trigger_phrase", "")
    match_type = rule.get("match_type", "contains")
    try:
        if match_type == "exact":
            return trigger.strip().lower() == incoming_lower.strip()
        if match_type == "contains":
            return trigger.lower() in incoming_lower
        if match_type == "regex":
            return bool(re.search(trigger, incoming_original, re.IGNORECASE))
    except re.error:
        pass
    return False


def _children_of(parent_id: Optional[int], rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Get enabled children of a parent node."""
    return [r for r in rules if r.get("is_enabled") and (r.get("parent_id") or None) == (parent_id or None)]


def check_message_for_auto_reply(
    incoming_message: str,
    sender_node_id: str,
    channel_index: Optional[int],
    rules: List[Dict[str, Any]],
    *,
    slot_id: str = "node_0",
    local_node_ids: Optional[Set[str]] = None,
    is_direct_message: bool = False,
) -> List[Dict[str, Any]]:
    """
    Evaluate message against rule tree, return list of reply dicts.
    """
    config = get_config()
    
    _log_debug("incoming", {
        "message": incoming_message[:100] if incoming_message else "",
        "sender": sender_node_id,
        "channel": channel_index,
        "slot": slot_id,
        "is_dm": is_direct_message,
        "rules_count": len(rules),
        "engine_enabled": config.get("enabled", True),
    })
    
    if not config.get("enabled", True):
        _log_debug("skip", {"reason": "engine_disabled"})
        return []
    
    if not incoming_message or not incoming_message.strip():
        _log_debug("skip", {"reason": "empty_message"})
        return []
    
    if not sender_node_id:
        _log_debug("skip", {"reason": "no_sender"})
        return []
    
    if local_node_ids and sender_node_id in local_node_ids:
        _log_debug("skip", {"reason": "self_message", "local_ids": list(local_node_ids or [])})
        return []
    
    if channel_index is None:
        channel_index = 0
    
    incoming_lower = incoming_message.lower().strip()
    incoming_original = incoming_message.strip()
    
    current_parent = _get_session_parent(slot_id, sender_node_id)
    
    applicable_rules = [
        r for r in rules
        if r.get("is_enabled") and _rule_applies_to_context(r, slot_id, channel_index, is_direct_message, config)
    ]
    
    _log_debug("filtering", {
        "total_rules": len(rules),
        "applicable_rules": len(applicable_rules),
        "slot": slot_id,
        "channel": channel_index,
        "is_dm": is_direct_message,
    })
    
    candidates = _children_of(current_parent, applicable_rules)
    
    _log_debug("matching", {
        "current_parent": current_parent,
        "candidates_count": len(candidates),
        "incoming_lower": incoming_lower[:50],
    })
    
    if not candidates and current_parent is not None:
        _clear_session(slot_id, sender_node_id)
        current_parent = None
        candidates = _children_of(None, applicable_rules)
        _log_debug("session_reset", {"new_candidates": len(candidates)})
    
    if not candidates:
        _log_debug("no_match", {"reason": "no_candidates"})
        return []
    
    for rule in candidates:
        rule_id = rule["id"]
        cooldown_sec = rule.get("cooldown_seconds", 60)
        node_type = rule.get("node_type", "action")
        trigger = rule.get("trigger_phrase", "")
        
        if _check_cooldown(slot_id, rule_id, sender_node_id, cooldown_sec):
            _log_debug("cooldown", {"rule_id": rule_id, "trigger": trigger})
            continue
        
        if not _matches_rule(incoming_lower, incoming_original, rule):
            continue
        
        _set_cooldown(slot_id, rule_id, sender_node_id)
        
        if is_direct_message:
            reply_channel = 0
            is_dm_reply = True
            reply_prefix = ""
        else:
            reply_channel = channel_index
            is_dm_reply = False
            if config.get("channel_reply_prefix", True):
                reply_prefix = f"@{sender_node_id} "
            else:
                reply_prefix = ""
        
        reply = {
            "destination": sender_node_id,
            "message": rule["response_message"],
            "channel": reply_channel,
            "rule_id": rule_id,
            "node_type": node_type,
            "is_dm_reply": is_dm_reply,
            "reply_prefix": reply_prefix,
        }
        
        _log_debug("match", {
            "rule_id": rule_id,
            "trigger": trigger,
            "node_type": node_type,
            "is_dm_reply": is_dm_reply,
            "reply_channel": reply_channel,
            "response_preview": rule["response_message"][:80],
        })
        
        _log_debug("outgoing", {
            "destination": sender_node_id,
            "channel": reply_channel,
            "is_dm": is_dm_reply,
            "prefix": reply_prefix,
            "message_preview": rule["response_message"][:80],
        })
        
        if node_type == "folder":
            _set_session_parent(slot_id, sender_node_id, rule_id)
        else:
            _clear_session(slot_id, sender_node_id)
        
        return [reply]
    
    _log_debug("no_match", {"reason": "no_rule_matched", "tried": len(candidates)})
    return []


# FastAPI Router

plugin_router = APIRouter()


async def _in_thread(fn, *args, **kwargs):
    """Run a blocking function in a thread pool."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except sqlite3.Error as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {e}") from e


# --- Status & Config ---

@plugin_router.get("/status")
async def get_status():
    """Plugin status and configuration."""
    config = get_config()
    rules = await _in_thread(db_get_auto_reply_rules)
    enabled_count = sum(1 for r in rules if r.get("is_enabled"))
    return {
        "plugin": "auto_reply",
        "version": "2.0",
        "enabled": config.get("enabled", True),
        "config": config,
        "rules_total": len(rules),
        "rules_enabled": enabled_count,
    }


@plugin_router.get("/config")
async def get_config_endpoint():
    """Get current configuration."""
    return get_config()


@plugin_router.put("/config")
async def update_config(config: Dict[str, Any] = Body(...)):
    """Update configuration."""
    if _save_config(config):
        return {"detail": "Configuration updated.", "config": get_config()}
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save configuration.")


@plugin_router.get("/enabled")
async def get_engine_enabled():
    """Check if engine is enabled."""
    return {"auto_reply_enabled": is_enabled()}


@plugin_router.put("/enabled")
async def set_engine_enabled(value: bool = Query(...)):
    """Enable or disable engine."""
    set_enabled(value)
    return {"auto_reply_enabled": is_enabled()}


# --- Rules CRUD ---

@plugin_router.get("/rules")
async def list_rules(only_enabled: bool = Query(False)):
    """List all rules (flat)."""
    return await _in_thread(db_get_auto_reply_rules, only_enabled=only_enabled)


@plugin_router.get("/rules/tree")
async def get_rules_tree():
    """Get rules as nested tree."""
    return await _in_thread(db_get_rules_tree)


@plugin_router.get("/rules/{rule_id}")
async def get_rule(rule_id: int = PathParam(...)):
    """Get a single rule."""
    rule = await _in_thread(db_get_auto_reply_rule_by_id, rule_id)
    if not rule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Rule {rule_id} not found.")
    return rule


class RuleCreate(BaseModel):
    trigger_phrase: str
    match_type: str = "exact"
    response_message: str
    cooldown_seconds: int = 60
    is_enabled: bool = True
    parent_id: Optional[int] = None
    node_type: str = "action"
    label: str = ""
    scope_slots: Optional[List[str]] = None
    scope_channels: Optional[List[int]] = None
    scope_listen_dm: Optional[bool] = None
    scope_listen_channel: Optional[bool] = None


@plugin_router.post("/rules")
async def create_rule(rule: RuleCreate):
    """Create a new rule."""
    new_id = await _in_thread(
        db_add_auto_reply_rule,
        trigger_phrase=rule.trigger_phrase,
        match_type=rule.match_type,
        response_message=rule.response_message,
        cooldown_seconds=rule.cooldown_seconds,
        is_enabled=rule.is_enabled,
        parent_id=rule.parent_id,
        node_type=rule.node_type,
        label=rule.label,
        scope_slots=rule.scope_slots,
        scope_channels=rule.scope_channels,
        scope_listen_dm=rule.scope_listen_dm,
        scope_listen_channel=rule.scope_listen_channel,
    )
    if new_id is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create rule.")
    return await _in_thread(db_get_auto_reply_rule_by_id, new_id)


@plugin_router.put("/rules/{rule_id}")
async def update_rule(rule_id: int, rule: RuleCreate):
    """Update a rule."""
    updated = await _in_thread(
        db_update_auto_reply_rule,
        rule_id=rule_id,
        trigger_phrase=rule.trigger_phrase,
        match_type=rule.match_type,
        response_message=rule.response_message,
        cooldown_seconds=rule.cooldown_seconds,
        is_enabled=rule.is_enabled,
        parent_id=rule.parent_id,
        node_type=rule.node_type,
        label=rule.label,
        scope_slots=rule.scope_slots,
        scope_channels=rule.scope_channels,
        scope_listen_dm=rule.scope_listen_dm,
        scope_listen_channel=rule.scope_listen_channel,
    )
    if not updated:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to update rule {rule_id}.")
    return updated


@plugin_router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int = PathParam(...)):
    """Delete a rule."""
    deleted = await _in_thread(db_delete_auto_reply_rule, rule_id)
    if not deleted:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete rule {rule_id}.")
    return {"detail": f"Rule {rule_id} deleted."}


@plugin_router.put("/rules/bulk/enable")
async def bulk_enable(enable: bool = Query(...)):
    """Enable or disable all rules."""
    def _do_bulk():
        conn = _get_db_connection(DB_PATH)
        try:
            cur = conn.execute(f"UPDATE {AUTO_REPLY_TABLE} SET is_enabled = ?", (1 if enable else 0,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    
    rows = await _in_thread(_do_bulk)
    return {"detail": f"Set is_enabled={enable} on {rows} rules.", "rows_affected": rows}


@plugin_router.post("/deploy-demo")
async def deploy_demo_menu():
    """Clear all rules and deploy a demonstration menu tree."""
    count = await _in_thread(db_deploy_demo_menu)
    return {"detail": f"Deployed {count} demo rules.", "rules_created": count}


@plugin_router.delete("/rules")
async def clear_all_rules():
    """Delete all rules."""
    count = await _in_thread(db_clear_all_rules)
    return {"detail": f"Deleted {count} rules.", "rules_deleted": count}


# --- Debug endpoints ---

@plugin_router.get("/debug/log")
async def get_debug_log_endpoint():
    """Get the debug event log (last 100 events, newest first)."""
    return {"events": get_debug_log()}


@plugin_router.delete("/debug/log")
async def clear_debug_log_endpoint():
    """Clear the debug event log."""
    count = clear_debug_log()
    return {"detail": f"Cleared {count} debug events."}


@plugin_router.get("/debug/sessions")
async def get_sessions_endpoint():
    """Get current session state for all senders."""
    return {"sessions": get_all_sessions()}


@plugin_router.get("/debug/cooldowns")
async def get_cooldowns_endpoint():
    """Get current cooldown state."""
    with _cooldown_lock:
        return {"cooldowns": {k: dict(v) for k, v in _cooldowns.items()}}


@plugin_router.post("/debug/test")
async def test_message(
    message: str = Query(..., description="Message to test"),
    sender: str = Query("!test1234", description="Simulated sender node ID"),
    channel: int = Query(0, description="Channel index"),
    slot_id: str = Query("node_0", description="Slot ID"),
    is_dm: bool = Query(True, description="Is direct message"),
):
    """
    Test auto-reply matching without actually sending.
    Simulates receiving a message and returns what would be replied.
    """
    rules = db_get_auto_reply_rules(only_enabled=True)
    replies = check_message_for_auto_reply(
        incoming_message=message,
        sender_node_id=sender,
        channel_index=channel,
        rules=rules,
        slot_id=slot_id,
        local_node_ids=set(),
        is_direct_message=is_dm,
    )
    
    return {
        "input": {
            "message": message,
            "sender": sender,
            "channel": channel,
            "slot_id": slot_id,
            "is_dm": is_dm,
        },
        "config": get_config(),
        "rules_checked": len(rules),
        "replies": replies,
        "would_reply": len(replies) > 0,
    }


# Plugin lifecycle

def init_plugin(context: Dict[str, Any]) -> None:
    """Called by MeshDash plugin loader on startup."""
    global _logger, _db_manager, _meshtastic_data, _connection_manager, _event_loop, _plugin_id
    
    # Store full context for watchdog access
    _meshtastic_data = context
    _logger = context["logger"]
    _db_manager = context["db_manager"]
    _connection_manager = context["connection_manager"]
    _event_loop = context["event_loop"]
    _plugin_id = context["plugin_id"]
    
    # Initialize database
    init_auto_reply_db()
    
    # Expose functions for dashboard integration
    context["auto_reply_plugin"] = {
        "check_message": check_message_for_auto_reply,
        "get_rules": db_get_auto_reply_rules,
        "replace_placeholders": replace_placeholders,
        "is_enabled": is_enabled,
        "set_enabled": set_enabled,
        "get_config": get_config,
        "set_config": set_config,
    }
    
    # Start watchdog heartbeat
    try:
        loop = _event_loop if _event_loop else asyncio.get_event_loop()
        _watchdog_task = loop.create_task(_watchdog_heartbeat())
        _logger.info("AUTO_REPLY PLUGIN v2.0: Watchdog heartbeat started")
    except Exception as e:
        _logger.warning("AUTO_REPLY: could not start watchdog heartbeat: %s", e)
    
    _logger.info("AUTO_REPLY PLUGIN v2.0: Initialized with DB at %s", DB_PATH)
