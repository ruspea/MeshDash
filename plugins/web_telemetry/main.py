"""
Web Telemetry Plugin for MeshDash
=================================

Web-to-RF bridge for extracting data from websites and broadcasting
over the Meshtastic mesh network.

This plugin uses existing dashboard endpoints:
- /extract - Parse DOM and extract text blocks
- /api/monitor - Transmit message over mesh
- /api/tasks - Schedule recurring tasks
"""

import logging
import os
import time
import threading
from typing import Any, Dict

from fastapi import APIRouter

logger = logging.getLogger("plugins.web_telemetry")

plugin_router = APIRouter()
router = plugin_router  # Alias

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

@plugin_router.get("/status")
async def get_status():
    """Plugin status endpoint."""
    return {
        "plugin": "web_telemetry",
        "status": "ready",
        "description": "Web-to-RF bridge for IoT data ingestion",
        "uses_endpoints": ["/extract", "/api/monitor", "/api/tasks"],
    }


@plugin_router.get("/help")
async def get_help():
    """Return usage documentation."""
    return {
        "plugin": "web_telemetry",
        "workflow": [
            "1. Enter URL and click PARSE DOM",
            "2. Select a text block to monitor",
            "3. Choose destination node",
            "4. Transmit immediately or schedule task",
        ],
        "task_type": "website_monitor",
    }


async def _watchdog_heartbeat():
    logger = core_context.get("logger") or logging.getLogger("web_telemetry")
    while True:
        try:
            await asyncio.sleep(30)
            wd  = core_context.get("plugin_watchdog")
            pid = core_context.get("plugin_id")
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("Web Telemetry watchdog heartbeat stopped")
            return
        except Exception as e:
            logger.warning(f"Web Telemetry watchdog error: {e}")


def init_plugin(context: Dict[str, Any]) -> None:
    """Called by MeshDash plugin loader on startup."""
    global _watchdog_dict, _plugin_id
    _watchdog_dict = context.get("plugin_watchdog")
    _plugin_id = context.get("plugin_id")

    loop = context.get("event_loop")
    if loop is not None:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(), loop)
        logger.info("WEB_TELEMETRY PLUGIN: Watchdog heartbeat started")
    else:
        logger.warning("WEB_TELEMETRY PLUGIN: No event_loop — watchdog will not start")
    logger.info("WEB_TELEMETRY PLUGIN: Initialized")
