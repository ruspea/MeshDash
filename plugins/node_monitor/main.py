"""
Node Monitor Plugin - main.py
Telemetry-based alerting system with background monitoring worker.
"""
import os
import sys
import sqlite3
import time
import asyncio
import logging
import threading
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

logger = logging.getLogger("node_monitor_plugin")
plugin_router = APIRouter()

# Globals - set by init_plugin
_db_path = None
_local = threading.local()
_m_data = None
_c_mgr = None
_db_mgr = None
_node_registry: Dict[str, Any] = {}
_worker_task = None
_watchdog_dict = None
_plugin_id = None


def init_plugin(context: dict):
    """Called by plugin loader with dashboard context."""
    global _m_data, _c_mgr, _db_mgr, _node_registry, _db_path, _watchdog_dict, _plugin_id
    _watchdog_dict = context.get("plugin_watchdog")
    _plugin_id = context.get("plugin_id")
    _m_data = context.get("meshtastic_data")
    _c_mgr = context.get("connection_manager")
    _db_mgr = context.get("db_manager")
    _node_registry = context.get("node_registry", {})
    
    # Use plugin's own database file in the plugin directory
    _db_path = os.path.join(PLUGIN_DIR, "monitor_rules.db")
    _init_db()
    
    # Register the background worker via the primary path (plugin_coros or direct).
    # Use plugin_coros (list-based) when available; fall back to run_coroutine_threadsafe.
    event_loop = context.get("event_loop")
    plugin_coros = context.get("plugin_coros")
    
    registered = False
    if plugin_coros is not None and isinstance(plugin_coros, list):
        plugin_coros.append(_monitor_worker)
        registered = True
        logger.info("\u2705 Node Monitor: background worker registered via plugin_coros")
    elif event_loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_monitor_worker(), event_loop)
            registered = True
            logger.info("\u2705 Node Monitor: monitor_worker launched via run_coroutine_threadsafe")
        except Exception as e:
            logger.error("Failed to launch monitor_worker: %s", e)
    
    if not registered:
        logger.error("event_loop not in context and plugin_coros unavailable \u2014 monitor_worker cannot start")


# Database Management

def _get_conn():
    """Thread-local SQLite connection."""
    if getattr(_local, "conn", None) is None:
        _local.conn = sqlite3.connect(_db_path, timeout=15.0)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def _init_db():
    """Initialize the monitor rules database."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monitor_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            target_node TEXT,
            metric TEXT,
            condition TEXT,
            threshold REAL,
            check_interval_minutes INTEGER,
            dest_node TEXT,
            dest_channel INTEGER,
            slot_id TEXT DEFAULT 'node_0',
            last_checked REAL DEFAULT 0,
            last_alerted REAL DEFAULT 0,
            is_active BOOLEAN DEFAULT 1
        )
    """)
    conn.commit()
    logger.info("✅ Node Monitor: database initialized at %s", _db_path)


# Pydantic Models

class MonitorRuleBase(BaseModel):
    name: str
    target_node: str
    metric: str
    condition: str  # 'below', 'above', 'equals'
    threshold: float
    check_interval_minutes: int
    dest_node: str
    dest_channel: int = 0
    slot_id: str = "node_0"
    is_active: bool = True


class MonitorRuleCreate(MonitorRuleBase):
    pass


class MonitorRuleUpdate(MonitorRuleBase):
    pass


class MonitorRuleResponse(MonitorRuleBase):
    id: int
    last_checked: float
    last_alerted: float


# Database CRUD

def _db_get_rules() -> List[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM monitor_rules ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def _db_add_rule(rule: MonitorRuleCreate) -> int:
    conn = _get_conn()
    cursor = conn.execute("""
        INSERT INTO monitor_rules 
        (name, target_node, metric, condition, threshold, check_interval_minutes, 
         dest_node, dest_channel, slot_id, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        rule.name, rule.target_node, rule.metric, rule.condition,
        rule.threshold, rule.check_interval_minutes, rule.dest_node,
        rule.dest_channel, rule.slot_id or 'node_0', rule.is_active
    ))
    conn.commit()
    return cursor.lastrowid


def _db_update_rule(rule_id: int, rule: MonitorRuleUpdate):
    conn = _get_conn()
    conn.execute("""
        UPDATE monitor_rules SET 
            name=?, target_node=?, metric=?, condition=?, threshold=?, 
            check_interval_minutes=?, dest_node=?, dest_channel=?, slot_id=?, is_active=?
        WHERE id=?
    """, (
        rule.name, rule.target_node, rule.metric, rule.condition,
        rule.threshold, rule.check_interval_minutes, rule.dest_node,
        rule.dest_channel, rule.slot_id or 'node_0', rule.is_active, rule_id
    ))
    conn.commit()


def _db_delete_rule(rule_id: int):
    conn = _get_conn()
    conn.execute("DELETE FROM monitor_rules WHERE id=?", (rule_id,))
    conn.commit()


def _db_update_timestamps(rule_id: int, checked: float, alerted: float):
    conn = _get_conn()
    conn.execute("""
        UPDATE monitor_rules SET last_checked=?, last_alerted=? WHERE id=?
    """, (checked, alerted, rule_id))
    conn.commit()


# Helpers

def _get_slot_cm(slot_id: str):
    """Return the connection_manager for a given slot_id, falling back to node_0."""
    slot = _node_registry.get(slot_id) or _node_registry.get('node_0')
    if slot:
        return slot.connection_manager
    return _c_mgr


def _get_slot_md(slot_id: str):
    """Return the meshtastic_data for a given slot_id."""
    slot = _node_registry.get(slot_id) or _node_registry.get('node_0')
    if slot:
        return slot.meshtastic_data
    return _m_data


def _extract_metric(node_info: dict, metric: str) -> Optional[float]:
    """Safely extracts the nested telemetry metric from the node dictionary."""
    metric = metric.lower()
    try:
        if metric == "battery_level":
            return float(node_info.get("deviceMetrics", {}).get("batteryLevel"))
        elif metric == "voltage":
            return float(node_info.get("deviceMetrics", {}).get("voltage"))
        elif metric == "channel_utilization":
            return float(node_info.get("deviceMetrics", {}).get("channelUtilization"))
        elif metric == "air_util_tx":
            return float(node_info.get("deviceMetrics", {}).get("airUtilTx"))
        elif metric == "snr":
            return float(node_info.get("snr"))
        elif metric == "rssi":
            return float(node_info.get("rssi"))
        elif metric == "temperature":
            return float(node_info.get("environmentMetrics", {}).get("temperature"))
        elif metric == "humidity":
            return float(node_info.get("environmentMetrics", {}).get("relativeHumidity"))
        elif metric == "pressure":
            return float(node_info.get("environmentMetrics", {}).get("barometricPressure"))
    except (TypeError, ValueError):
        return None
    return None


# Background Worker

async def _monitor_worker():
    """Non-blocking background loop that evaluates rules and sends alerts."""
    logger.info("⚙️ Node Monitor: Alert Engine Started")
    
    # Heartbeat immediately on start so watchdog sees activity right away
    try:
        if _watchdog_dict is not None and _plugin_id is not None:
            _watchdog_dict[_plugin_id] = time.time()
    except Exception:
        pass
    
    # Wait for system to stabilize
    await asyncio.sleep(5)
    
    while True:
        # Heartbeat every 30s so watchdog (120s timeout) never fires
        try:
            if _watchdog_dict is not None and _plugin_id is not None:
                _watchdog_dict[_plugin_id] = time.time()
        except Exception:
            pass
        
        await asyncio.sleep(30)
        
        try:
            if not _m_data and not _node_registry:
                continue

            rules = await asyncio.to_thread(_db_get_rules)
            now = time.time()

            for rule in rules:
                if not rule["is_active"]:
                    continue

                if (now - rule["last_checked"]) < (rule["check_interval_minutes"] * 60):
                    continue

                target = rule["target_node"]
                slot_id = rule.get("slot_id") or "node_0"
                cm = _get_slot_cm(slot_id)

                # Skip if the chosen slot's radio isn't ready
                if not cm or not getattr(cm, 'is_ready', None) or not cm.is_ready.is_set():
                    logger.debug("Monitor: slot %s not ready, skipping rule %s", slot_id, rule["id"])
                    continue

                # Use the slot's own meshtastic_data for node lookup
                md = _get_slot_md(slot_id)
                node_info = md.nodes.get(target) if md else None
                new_last_alerted = rule["last_alerted"]

                if node_info:
                    val = _extract_metric(node_info, rule["metric"])
                    
                    if val is not None:
                        triggered = False
                        cond = rule["condition"].lower()
                        thresh = float(rule["threshold"])

                        if cond == "below" and val < thresh:
                            triggered = True
                        elif cond == "above" and val > thresh:
                            triggered = True
                        elif cond == "equals" and val == thresh:
                            triggered = True

                        if triggered and (now - rule["last_alerted"]) >= (rule["check_interval_minutes"] * 60):
                            dest = rule["dest_node"]
                            chan = rule["dest_channel"]
                            target_name = node_info.get("user", {}).get("shortName", target)
                            msg = f"🚨 MONITOR ALERT: {target_name} {rule['metric'].upper()} is {val} ({cond} {thresh})"
                            logger.info("Triggering Alert [slot=%s]: %s -> Dest: %s", slot_id, msg, dest)
                            await cm.sendText(msg, destinationId=dest, channelIndex=chan)
                            new_last_alerted = now

                await asyncio.to_thread(_db_update_timestamps, rule["id"], now, new_last_alerted)

        except asyncio.CancelledError:
            logger.info("Node Monitor: worker cancelled")
            break
        except Exception as e:
            logger.error(f"❌ Node Monitor worker error: {e}", exc_info=True)


# API Endpoints

@plugin_router.get("/status")
async def get_status():
    """Health check endpoint."""
    rules = await asyncio.to_thread(_db_get_rules)
    active_count = sum(1 for r in rules if r.get("is_active"))
    return {
        "state": "ready",
        "ready": True,
        "plugin": "node_monitor",
        "version": "1.0.0",
        "total_rules": len(rules),
        "active_rules": active_count
    }


@plugin_router.get("/rules", response_model=List[MonitorRuleResponse])
async def get_rules():
    rules = await asyncio.to_thread(_db_get_rules)
    return rules


@plugin_router.post("/rules", response_model=MonitorRuleResponse)
async def create_rule(rule: MonitorRuleCreate):
    rule_id = await asyncio.to_thread(_db_add_rule, rule)
    
    if hasattr(rule, "model_dump"):
        rule_data = rule.model_dump()
    else:
        rule_data = rule.dict()
        
    return {
        **rule_data,
        "id": rule_id,
        "last_checked": 0,
        "last_alerted": 0,
    }


@plugin_router.put("/rules/{rule_id}")
async def update_rule(rule_id: int, rule: MonitorRuleUpdate):
    await asyncio.to_thread(_db_update_rule, rule_id, rule)
    return {"status": "success", "id": rule_id}


@plugin_router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    await asyncio.to_thread(_db_delete_rule, rule_id)
    return {"status": "deleted", "id": rule_id}


@plugin_router.post("/rules/{rule_id}/test")
async def test_rule_alert(rule_id: int):
    """Send a test alert for a specific rule."""
    rules = await asyncio.to_thread(_db_get_rules)
    rule = next((r for r in rules if r["id"] == rule_id), None)
    
    if not rule:
        raise HTTPException(404, "Rule not found")

    slot_id = rule.get("slot_id") or "node_0"
    cm = _get_slot_cm(slot_id)
    
    if not cm or not getattr(cm, 'is_ready', None) or not cm.is_ready.is_set():
        raise HTTPException(503, f"Slot '{slot_id}' radio not connected")

    md = _get_slot_md(slot_id)
    node_info = (md.nodes.get(rule["target_node"], {}) if md else {})
    target_name = node_info.get("user", {}).get("shortName", rule["target_node"])
    
    test_msg = f"🧪 TEST ALERT for {rule['name']}: {target_name} monitor is active."
    
    await cm.sendText(test_msg, destinationId=rule["dest_node"], channelIndex=rule["dest_channel"])
    return {"status": "test_sent", "message": test_msg, "slot_id": slot_id}


@plugin_router.post("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int):
    """Toggle a rule's active state."""
    rules = await asyncio.to_thread(_db_get_rules)
    rule = next((r for r in rules if r["id"] == rule_id), None)
    
    if not rule:
        raise HTTPException(404, "Rule not found")
    
    new_state = not rule["is_active"]
    
    def _toggle():
        conn = _get_conn()
        conn.execute("UPDATE monitor_rules SET is_active=? WHERE id=?", (new_state, rule_id))
        conn.commit()
    
    await asyncio.to_thread(_toggle)
    
    return {"status": "toggled", "id": rule_id, "is_active": new_state}
