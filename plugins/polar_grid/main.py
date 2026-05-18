"""
Polar Grid Plugin — Backend API v1.2
Config storage only. All rendering is done in the browser bridge.
"""
import os
import json
import sqlite3
import asyncio
import threading
import logging
import time
from typing import Any, Dict, List, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("plugin.polar_grid")
plugin_router = APIRouter()

_node_registry: Dict[str, Any] = {}

_DB_PATH   = os.path.join(os.path.dirname(__file__), "polar_grid_config.db")
_DB_LOCK   = threading.Lock()
_config_lock = threading.Lock()

_config: Dict[str, Any] = {
    "enabled":         False,
    "unit":            "km",
    "rings":           [1, 2, 5, 10, 25, 50],
    "azimuths":        12,
    "ring_color":      "#00c8f5",
    "azimuth_color":   "#ffa826",
    "ring_opacity":    0.45,
    "azimuth_opacity": 0.35,
    "ring_weight":     1.5,
    "azimuth_weight":  1.0,
    "label_rings":     True,
    "fill_bands":      True,
    "show_cardinal":   True,
    "manual_lat":      None,
    "manual_lon":      None,
    "minimise_nodes":  False,
}


def _db_load() -> dict:
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
        logger.warning("Polar Grid: failed to load config: %s", e)
    return {}


def _db_save(cfg: dict) -> None:
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
        logger.warning("Polar Grid: failed to persist config: %s", e)


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
            logger.info("Polar Grid watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("Polar Grid watchdog error: %s", e)


def init_plugin(context: dict) -> None:
    global _node_registry
    _node_registry = context.get("node_registry") or {}

    saved = _db_load()
    if saved:
        with _config_lock:
            _config.update({k: v for k, v in saved.items() if k in _config})
        logger.info(
            "Polar Grid: loaded persisted config — enabled=%s unit=%s",
            _config["enabled"], _config["unit"],
        )

    logger.info("Polar Grid plugin v1.2 initialised.")

    loop = context.get("event_loop")
    if loop is None:
        logger.warning("Polar Grid: event_loop not in context — watchdog will not start.")
        return
    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("Polar Grid watchdog heartbeat started.")
    except Exception as e:
        logger.error("Polar Grid: could not start watchdog heartbeat: %s", e)


class ConfigModel(BaseModel):
    enabled:          bool          = False
    unit:             str           = Field("km", pattern="^(km|mi|nm)$")
    rings:            List[float]   = Field(default=[1, 2, 5, 10, 25, 50], min_length=1, max_length=20)
    azimuths:         int           = Field(12, ge=0, le=36)
    ring_color:       str           = Field("#00c8f5", pattern="^#[0-9a-fA-F]{6}$")
    azimuth_color:    str           = Field("#ffa826", pattern="^#[0-9a-fA-F]{6}$")
    ring_opacity:     float         = Field(0.45, ge=0.0, le=1.0)
    azimuth_opacity:  float         = Field(0.35, ge=0.0, le=1.0)
    ring_weight:      float         = Field(1.5,  ge=0.5, le=8.0)
    azimuth_weight:   float         = Field(1.0,  ge=0.5, le=8.0)
    label_rings:      bool          = True
    fill_bands:       bool          = True
    show_cardinal:    bool          = True
    manual_lat:       Optional[float] = None
    manual_lon:       Optional[float] = None
    minimise_nodes:   bool          = False


@plugin_router.get("/config")
async def get_config():
    with _config_lock:
        return dict(_config)


@plugin_router.post("/config")
async def set_config(body: ConfigModel):
    with _config_lock:
        _config.update(body.dict())
        snapshot = dict(_config)
    await asyncio.to_thread(_db_save, snapshot)
    logger.info(
        "Polar Grid config updated and persisted: enabled=%s unit=%s rings=%s minimise=%s",
        body.enabled, body.unit, body.rings, body.minimise_nodes,
    )
    return {"status": "ok", **snapshot}