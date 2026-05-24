"""
Mesh Visualizer Plugin for MeshDash
====================================
3D mesh network visualiser — renders nodes and links in an interactive
Three.js scene. The frontend (static/index.html) is self-contained and
drives the 3D view via the core MeshDash API and SSE.

This module provides the plugin lifecycle (watchdog heartbeat) and a
health endpoint. All visualisation work happens client-side.
"""

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter

# Plugin registry
plugin_router = APIRouter()

core_context: dict = {}
_watchdog_task: Optional[asyncio.Task] = None


# Watchdog heartbeat
async def _watchdog_heartbeat():
    """Ping MeshDash core every 30s so it doesn't mark us as hung."""
    logger = core_context.get("logger") or logging.getLogger("mesh_visualizer")
    while True:
        try:
            await asyncio.sleep(30)
            wd = core_context.get("plugin_watchdog")
            pid = core_context.get("plugin_id")
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("🛑 Visualizer watchdog heartbeat stopped")
            return
        except Exception as e:
            logger.warning(f"⚠️ Visualizer watchdog heartbeat error: {e}")


# Endpoints
@plugin_router.get("/status")
async def get_status():
    """Health / readiness endpoint."""
    cm = core_context.get("connection_manager")
    radio_ready = bool(cm and getattr(cm, "is_ready", None) and cm.is_ready.is_set())
    return {
        "state": "ready" if radio_ready else "waiting",
        "ready": radio_ready,
        "radio_ready": radio_ready,
    }


# Plugin lifecycle
def init_plugin(context: dict):
    """Called by MeshDash core when the plugin is loaded."""
    global core_context
    core_context = context

    logger = context.get("logger") or logging.getLogger("mesh_visualizer")
    loop = context.get("event_loop")
    if loop:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(), loop)
        logger.info("🎮 Mesh Visualizer v1.1.1 initialised")
    else:
        logger.warning("🎮 Mesh Visualizer: no event_loop — watchdog not started")
