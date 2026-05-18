"""
Hello Mesh — MeshDash Plugin Developer Reference
==================================================
The authoritative developer guide for building MeshDash plugins.
Demonstrates every plugin concept: manifest, lifecycle, watchdog, logging,
context injection, multi-slot awareness, SSE, bridge, and permissions.

Every proxy endpoint also demonstrates the DIRECT ACCESS pattern using
core_context — because plugins run inside the same process, they can
access db_manager, meshtastic_data, connection_manager, and NODE_REGISTRY
directly without HTTP round-trips.

Designed to be read alongside the HTML companion page.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Plugin boilerplate — every plugin MUST expose exactly these two top-level names
# ---------------------------------------------------------------------------
# 1. core_context: dict  — populated by init_plugin(), holds all injected objects
# 2. plugin_router: APIRouter  — all your API routes mount on this

core_context: dict = {}
plugin_router = APIRouter()


# ===========================================================================
# WATCHDOG HEARTBEAT
# ===========================================================================
# When manifest.json has "watchdog": true, MeshDash core tracks this plugin
# in _plugin_watchdog. The plugin must write a timestamp into the shared
# watchdog dict at least once every 120 s, or the core marks it "hung" and
# all its API routes begin returning 503.
#
# If your manifest has "watchdog": false, you do NOT need this coroutine at
# all. The core will never monitor your plugin's liveness.
# ===========================================================================

async def _watchdog_heartbeat():
    """
    Pings the MeshDash core watchdog every 30 s.

    The core passes two keys in the context dict:
        context["plugin_watchdog"]  — the shared _plugin_watchdog dict
        context["plugin_id"]        — our registered plugin ID string

    All we do is:  watchdog_dict[our_id] = time.time()
    That resets the 120-second hang timer in the core.
    """
    logger = core_context.get("logger") or logging.getLogger("hello_mesh")
    while True:
        try:
            await asyncio.sleep(30)
            wd  = core_context.get("plugin_watchdog")
            pid = core_context.get("plugin_id")
            if wd is not None and pid:
                wd[pid] = time.time()
                logger.debug(f"🐕 Watchdog ping sent for {pid}")
        except asyncio.CancelledError:
            logger.info("🛑 Hello Mesh watchdog heartbeat stopped")
            return
        except Exception as e:
            logger.warning(f"⚠️  Watchdog heartbeat error: {e}")


# ===========================================================================
# PLUGIN LIFECYCLE — init_plugin
# ===========================================================================
# The MeshDash core calls init_plugin(context) once during startup, inside
# a threading.Thread (for timeout safety). This means you CANNOT use
# asyncio.get_event_loop().create_task() here — that returns the wrong loop.
#
# The core passes context["event_loop"] which is the real running uvicorn
# loop. Always use asyncio.run_coroutine_threadsafe(coro, loop) to schedule
# background tasks from init_plugin.
# ===========================================================================

def init_plugin(context: dict):
    """
    Called once by the MeshDash core during plugin loading.

    Runs inside a daemon thread with a 15-second timeout.
    If this function hangs, the plugin is marked 'loading' and eventually
    fails to start.
    """
    core_context.update(context)
    logger = core_context.get("logger") or logging.getLogger("hello_mesh")
    logger.info("✅ Hello Mesh plugin initialising…")

    loop = core_context.get("event_loop")
    if loop is None:
        logger.warning(
            "⚠️  event_loop not in context — watchdog heartbeat will not start. "
            "Plugin may be marked hung after 120 s if watchdog:true in manifest."
        )
        return

    # Start the watchdog heartbeat.
    # Remove these three lines if your manifest has "watchdog": false.
    asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(), loop)
    logger.info("🐕 Hello Mesh watchdog heartbeat started")


# ===========================================================================
# REFERENCE ENDPOINTS
# ===========================================================================
# These endpoints are consumed by the hello_mesh HTML page to provide live,
# working demos for every MeshDash core API.
#
# IMPORTANT: These use httpx proxies so the HTML page can call them from
# the browser. But in your own plugin Python code, you should access the
# core objects DIRECTLY via core_context instead of making HTTP calls:
#
#   ✅  meshtastic_data = core_context["meshtastic_data"]
#       nodes = meshtastic_data.nodes
#
#   ❌  r = httpx.get("{_get_base_url()}/api/nodes")
#       nodes = r.json()
#
# Direct access is faster, needs no auth, works without network, and
# avoids the hard-coded port problem.
# ===========================================================================


# ---------------------------------------------------------------------------
# /info — plugin context inspection
# ---------------------------------------------------------------------------
@plugin_router.get("/info")
async def plugin_info():
    """Returns metadata about this plugin as seen by the running core."""
    wd  = core_context.get("plugin_watchdog")
    pid = core_context.get("plugin_id", "hello_mesh")
    nr  = core_context.get("node_registry") or {}
    slots = list(nr.keys()) if nr else []
    return {
        "plugin_id":            pid,
        "watchdog_enabled":     wd is not None,
        "last_watchdog_ping":   wd.get(pid) if wd else None,
        "has_connection_manager": core_context.get("connection_manager") is not None,
        "has_db_manager":       core_context.get("db_manager") is not None,
        "has_meshtastic_data":  core_context.get("meshtastic_data") is not None,
        "has_event_loop":       core_context.get("event_loop") is not None,
        "has_node_registry":    core_context.get("node_registry") is not None,
        "slot_ids":             slots,
    }


# ---------------------------------------------------------------------------
# /direct_access_demo — shows the direct-access pattern (no HTTP needed)
# ---------------------------------------------------------------------------
@plugin_router.get("/direct_access_demo")
async def direct_access_demo():
    """
    Demonstrates reading live data directly from core_context objects
    instead of making HTTP round-trips. This is the RECOMMENDED pattern
    for plugin Python code.
    """
    md = core_context.get("meshtastic_data")
    nr = core_context.get("node_registry") or {}

    if md is None:
        return {"error": "meshtastic_data not available"}

    return {
        "connection_status": md.connection_status,
        "connection_state": md._connection_state,
        "connection_transport": md._connection_transport,
        "local_node_id": md.local_node_id,
        "node_count": len(md.nodes),
        "packet_count": len(md.packets),
        "slot_count": len(nr),
        "stats": md.get_serializable_stats(),
    }


# ---------------------------------------------------------------------------
# /proxy/* — HTTP proxies for the HTML demo page

def _get_base_url() -> str:
    """Resolve the dashboard's base URL at runtime (avoid hardcoded port)."""
    try:
        from core.globals import loaded_config
        port = loaded_config.get("WEBSERVER_PORT", 8181)
        return f"http://127.0.0.1:{port}"
    except Exception:
        try:
            import os
            port = os.environ.get("WEBSERVER_PORT", "8181")
            return f"http://127.0.0.1:{port}"
        except Exception:
            return "http://127.0.0.1:8181"

# The HTML page runs in the browser and can't access core_context directly,
# so it calls these proxy endpoints which then call the real MeshDash APIs.
# In your OWN plugin Python code, use core_context directly instead!
# ---------------------------------------------------------------------------

@plugin_router.get("/proxy/status")
async def proxy_status(slot_id: str = Query("node_0")):
    """Proxies GET /api/status?slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/status?slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/stats")
async def proxy_stats(slot_id: str = Query("node_0")):
    """Proxies GET /api/stats?slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/stats?slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/nodes")
async def proxy_nodes(slot_id: str = Query("node_0")):
    """Proxies GET /api/nodes?slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/nodes?slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/packets")
async def proxy_packets(limit: int = Query(20, ge=1, le=200), slot_id: str = Query("node_0")):
    """Proxies GET /api/packets?limit=N&slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/packets?limit={limit}&slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/packets/history")
async def proxy_packets_history(limit: int = Query(50, ge=1, le=500)):
    """Proxies GET /api/packets/history?limit=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/packets/history?limit={limit}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/neighbors")
async def proxy_neighbors():
    """Proxies GET /api/neighbors"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/neighbors")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/traceroutes")
async def proxy_traceroutes(limit: int = Query(20, ge=1, le=100)):
    """Proxies GET /api/traceroutes?limit=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/traceroutes?limit={limit}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/waypoints")
async def proxy_waypoints():
    """Proxies GET /api/waypoints"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/waypoints")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/hardware_logs")
async def proxy_hardware_logs(limit: int = Query(20, ge=1, le=200)):
    """Proxies GET /api/hardware_logs?limit=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/hardware_logs?limit={limit}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/messages/history")
async def proxy_messages_history(
    channel: int = Query(0),
    limit:   int = Query(50, ge=1, le=500),
):
    """Proxies GET /api/messages/history"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(
            f"{_get_base_url()}/api/messages/history"
            f"?channel={channel}&limit={limit}"
        )
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/channels")
async def proxy_channels(slot_id: str = Query("node_0")):
    """Proxies GET /api/channels?slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/channels?slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/local_node")
async def proxy_local_node(slot_id: str = Query("node_0")):
    """Proxies GET /api/local_node/full?slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/local_node/full?slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/metrics")
async def proxy_metrics(limit: int = Query(100, ge=1, le=1000)):
    """Proxies GET /api/metrics/averages?limit=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/metrics/averages?limit={limit}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/counts")
async def proxy_counts():
    """Proxies GET /api/counts/totals"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/counts/totals")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/connection_history")
async def proxy_connection_history(limit: int = Query(60, ge=1, le=300)):
    """Proxies GET /api/system/connection_history?limit=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/system/connection_history?limit={limit}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/plugins")
async def proxy_plugins():
    """Proxies GET /api/system/plugins"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/system/plugins")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/version_status")
async def proxy_version_status():
    """Proxies GET /api/system/version-status"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/system/version-status")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/c2_status")
async def proxy_c2_status():
    """Proxies GET /api/c2/status"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/c2/status")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/slots")
async def proxy_slots():
    """Proxies GET /api/slots — the multi-radio slot registry"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/slots")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/search")
async def proxy_search(q: str = Query(""), limit: int = Query(50)):
    """Proxies GET /api/search?q=...&limit=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/search?q={q}&limit={limit}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/geocode")
async def proxy_geocode(lat: float = Query(0), lon: float = Query(0)):
    """Proxies GET /api/geocode?lat=X&lon=Y"""
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{_get_base_url()}/api/geocode?lat={lat}&lon={lon}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/connection_status")
async def proxy_connection_status():
    """Proxies GET /api/connection/status"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("{_get_base_url()}/api/connection/status")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/node/{node_id}")
async def proxy_single_node(node_id: str, slot_id: str = Query("node_0")):
    """Proxies GET /api/nodes/{node_id}?slot_id=N"""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{_get_base_url()}/api/nodes/{node_id}?slot_id={slot_id}")
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/node_count/{node_id}/{item_type}")
async def proxy_node_count(
    node_id:   str,
    item_type: str,
    start:     Optional[float] = Query(None),
    end:       Optional[float] = Query(None),
):
    """Proxies GET /api/nodes/{node_id}/count/{item_type}"""
    import httpx
    url = f"{_get_base_url()}/api/nodes/{node_id}/count/{item_type}"
    params = {}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url, params=params)
    return JSONResponse(content=r.json(), status_code=r.status_code)


@plugin_router.get("/proxy/node_history/{node_id}/{table_name}")
async def proxy_node_history(
    node_id:    str,
    table_name: str,
    limit:      int            = Query(100, ge=1, le=1000),
    start:      Optional[float] = Query(None),
    end:        Optional[float] = Query(None),
):
    """Proxies GET /api/nodes/{node_id}/history/{table_name}"""
    import httpx
    url = f"{_get_base_url()}/api/nodes/{node_id}/history/{table_name}"
    params: dict = {"limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url, params=params)
    return JSONResponse(content=r.json(), status_code=r.status_code)


# ---------------------------------------------------------------------------
# Logging reference — plugins get a namespaced logger and a ring buffer
# The core captures all output from logging.getLogger("plugin.<id>")
# and makes it available at GET /api/system/plugins/<id>/logs
# ---------------------------------------------------------------------------
@plugin_router.get("/log_test")
async def log_test():
    """
    Emits one log line at each level so you can see them in the Logs modal
    on the Plugins page (GET /api/system/plugins/hello_mesh/logs).
    The ring buffer holds up to _PLUGIN_LOG_MAX_LINES entries (default 1000).
    """
    log = core_context.get("logger") or logging.getLogger("hello_mesh")
    log.debug("🔵 DEBUG — lowest severity, filtered in most environments")
    log.info("🟢 INFO  — general operational messages")
    log.warning("🟡 WARNING — something unexpected but non-fatal")
    log.error("🔴 ERROR — something went wrong")
    log.critical("💀 CRITICAL — serious failure")
    return {"status": "ok", "message": "5 log lines emitted — check the Logs modal"}