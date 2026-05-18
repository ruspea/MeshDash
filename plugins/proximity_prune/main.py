"""
Proximity Pruner Plugin - Backend API v1.2
Per-slot distance calculation, database pruning, and Manual GPS Fallback.
"""
import os
import json
import math
import sqlite3
import time
import logging
import asyncio
import threading
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
logger = logging.getLogger("plugin.proximity_prune")
plugin_router = APIRouter()
_config_lock = threading.Lock()
_config: Dict[str, Any] = {"slots": {}}
_node_registry: Dict[str, Any] = {}
_DB_PATH = os.path.join(os.path.dirname(__file__), "proximity_prune_config.db")
_DB_LOCK = threading.Lock()


def _db_load() -> dict:
    """Load persisted config from SQLite. Returns {} on first run."""
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            row = conn.execute("SELECT value FROM config WHERE key='settings'").fetchone()
            conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.warning("Proximity Pruner: failed to load config: %s", e)
    return {}


def _db_save(cfg: dict) -> None:
    """Persist config dict to SQLite."""
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES ('settings', ?)",
                (json.dumps(cfg),),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Proximity Pruner: failed to persist config: %s", e)


class SlotConfig(BaseModel):
    enabled: bool = False
    radius_km: float = Field(10.0, ge=0.1, le=1000.0)
    prune_no_gps: bool = False
    manual_lat: Optional[float] = None
    manual_lon: Optional[float] = None
class ConfigModel(BaseModel):
    slots: Dict[str, SlotConfig] = Field(default_factory=dict)
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
def do_prune_sweep(force_slot: str = None):
    slots_to_check = [force_slot] if force_slot else list(_node_registry.keys())
    for slot_id in slots_to_check:
        slot = _node_registry.get(slot_id)
        if not slot: continue
        with _config_lock:
            slot_cfg = _config.get("slots", {}).get(slot_id, {})
            if not force_slot and not slot_cfg.get("enabled"):
                continue
            radius_km = slot_cfg.get("radius_km", 10.0)
            prune_no_gps = slot_cfg.get("prune_no_gps", False)
            man_lat = slot_cfg.get("manual_lat")
            man_lon = slot_cfg.get("manual_lon")
        md = slot.meshtastic_data
        db = slot.db_manager
        local_id = md.local_node_id
        if not local_id or local_id not in md.nodes:
            continue
        local_node = md.nodes[local_id]
        l_lat = local_node.get("position", {}).get("latitude") or local_node.get("latitude")
        l_lon = local_node.get("position", {}).get("longitude") or local_node.get("longitude")
        if l_lat is None or l_lon is None or (l_lat == 0 and l_lon == 0):
            l_lat = man_lat
            l_lon = man_lon
        if l_lat is None or l_lon is None:
            continue
        to_delete = []
        for nid, node in list(md.nodes.items()):
            if nid == local_id or node.get("isLocal") or node.get("is_local"):
                continue
            n_lat = node.get("position", {}).get("latitude") or node.get("latitude")
            n_lon = node.get("position", {}).get("longitude") or node.get("longitude")
            if n_lat is None or n_lon is None or (n_lat == 0 and n_lon == 0):
                if prune_no_gps:
                    to_delete.append(nid)
                continue
            dist = haversine(l_lat, l_lon, n_lat, n_lon)
            if dist > radius_km:
                to_delete.append(nid)
        if to_delete:
            deleted_count = 0
            try:
                conn = db._get_connection()
                for nid in to_delete:
                    md.nodes.pop(nid, None)
                    conn.execute("DELETE FROM nodes WHERE node_id = ?", (nid,))
                    deleted_count += 1
                conn.commit()
                logger.info(f"Pruned {deleted_count} nodes from {slot_id} (>{radius_km}km)")
            except Exception as e:
                logger.error(f"DB Error pruning on {slot_id}: {e}")
async def background_pruner():
    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(do_prune_sweep)
        except Exception as e:
            logger.error(f"Pruner daemon error: {e}")


async def _watchdog_heartbeat(context: dict) -> None:
    """
    Pings the MeshDash core watchdog every 30 s.
    Required because manifest.json has "watchdog": true.
    Without this the core marks the plugin as 'hung' after 120 s of silence.
    """
    wd  = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("Proximity Pruner watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("Proximity Pruner watchdog error: %s", e)


def init_plugin(context: dict) -> None:
    global _node_registry
    _node_registry = context.get("node_registry", {})

    # Restore persisted config so per-slot settings survive restarts
    saved = _db_load()
    if saved:
        with _config_lock:
            _config.update({k: v for k, v in saved.items() if k in _config})
        logger.info(
            "Proximity Pruner: loaded persisted config — %d slot(s)",
            len(_config.get("slots", {})),
        )

    loop = context.get("event_loop")
    if loop:
        asyncio.run_coroutine_threadsafe(background_pruner(), loop)
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("Proximity Pruner watchdog heartbeat started.")
    else:
        logger.warning("Proximity Pruner: event_loop not in context — watchdog will not start.")
    logger.info("Proximity Pruner v1.3 initialised. Background daemon running.")
@plugin_router.get("/config")
async def get_config():
    with _config_lock:
        return dict(_config)
@plugin_router.post("/config")
async def set_config(body: ConfigModel):
    with _config_lock:
        _config["slots"] = {k: v.dict() for k, v in body.slots.items()}
        snapshot = dict(_config)
    await asyncio.to_thread(_db_save, snapshot)
    logger.info(
        "Proximity Pruner config persisted — %d slot(s)",
        len(snapshot.get("slots", {})),
    )
    return {"status": "ok", **snapshot}
@plugin_router.get("/slots")
async def list_slots():
    """Return registered radio slots for the settings page slot selector."""
    slots = {}
    for slot_id, slot in _node_registry.items():
        try:
            slots[slot_id] = {
                "label":      getattr(slot, "label", slot_id),
                "node_count": len(slot.meshtastic_data.nodes),
                "is_ready":   slot.connection_manager.is_ready.is_set()
                              if slot.connection_manager else False,
            }
        except Exception:
            slots[slot_id] = {"label": slot_id, "node_count": 0, "is_ready": False}
    return slots


@plugin_router.post("/prune_now/{slot_id}")
async def prune_now(slot_id: str):
    if slot_id not in _node_registry:
        raise HTTPException(404, "Slot not found")
    try:
        await asyncio.to_thread(do_prune_sweep, force_slot=slot_id)
        return {"status": "ok", "message": f"Prune sweep completed for {slot_id}."}
    except Exception as e:
        raise HTTPException(500, f"Prune failed: {e}")