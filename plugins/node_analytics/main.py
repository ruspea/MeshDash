"""
Node Analytics Plugin - main.py
Provides status endpoint; all data fetched from main dashboard APIs.
"""
import os
import sys
import time
import threading

from fastapi import APIRouter

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

plugin_router = APIRouter()

# Watchdog globals
_watchdog_dict = None
_plugin_id = None

def _watchdog_loop():
    """Heartbeat for the plugin watchdog system."""
    while True:
        try:
            if _watchdog_dict is not None and _plugin_id is not None:
                _watchdog_dict[_plugin_id] = time.time()
        except Exception:
            pass
        time.sleep(30)

_md = None
_db = None
_reg = None


def init_plugin(context: dict):
    """Called by plugin loader with dashboard context."""
    global _md, _db, _reg, _watchdog_dict, _plugin_id
    _watchdog_dict = context.get("plugin_watchdog")
    _plugin_id = context.get("plugin_id")
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()
    _md = context.get("meshtastic_data")
    _db = context.get("db_manager")
    _reg = context.get("node_registry")


@plugin_router.get("/status")
async def get_status():
    """Health check endpoint."""
    return {"state": "ready", "ready": True, "plugin": "node_analytics", "version": "1.0.0"}
