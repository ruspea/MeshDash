# STUB PLUGIN — This is a placeholder with no real functionality.
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

def init_plugin(context: dict):
    global _watchdog_dict, _plugin_id
    _watchdog_dict = context.get("plugin_watchdog")
    _plugin_id = context.get("plugin_id")
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()

@plugin_router.get("/status")
async def get_status():
    return {"state": "ready", "ready": True}