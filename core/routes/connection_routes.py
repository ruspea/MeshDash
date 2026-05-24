# Auto-extracted from meshtastic_dashboard.py
import core.globals as g
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Request, Response, Depends, HTTPException
from fastapi.responses import JSONResponse
from core.routes.schemas import User
from core.auth import verify_csrf, get_current_active_user, _generate_csrf_token

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/system/connection_history")
async def api_conn_hist(limit: int = 60, slot_id: str = ""):
    """Connection history. If slot_id given, use that slot's db_manager."""
    _slot = g.NODE_REGISTRY.get(slot_id) if slot_id else None
    db = _slot.g.db_manager if _slot else g.db_manager
    return await asyncio.to_thread(db.get_connection_history, limit)


@router.get("/api/connection/status")
async def api_connection_status(slot_id: str = ""):
    """Return detailed connection status and metrics for all slots (or a single slot).

    When slot_id is omitted or empty, returns ALL slots with structured state.
    When slot_id is provided, returns only that slot's data (backward compat).
    """
    from core.connections import ConnectionState

    def fmt_ts(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None

    def _build_slot_info(sid: str, slot) -> dict:
        cm = slot.g.connection_manager
        md = slot.g.meshtastic_data
        now = time.time()

        # Use structured state from MeshtasticData if available
        state_str = md._connection_state or "idle"
        detail = md._connection_detail or ""
        transport_str = md._connection_transport or ""

        # Derive transport from config if _connection_transport is empty
        if not transport_str and cm:
            conn_type = cm.config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL").upper()
            if conn_type == "MESHCORE":
                mc_transport = cm.config.get("MESHCORE_TRANSPORT", "serial").upper()
                transport_str = f"MeshCore/{mc_transport}"
            elif conn_type == "MQTT":
                transport_str = "MQTT"
            else:
                transport_str = conn_type

        # Host / device info
        host_info = ""
        if cm:
            conn_type = cm.config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL").upper()
            if conn_type == "TCP":
                host = cm.config.get("MESHTASTIC_HOST", "")
                port = cm.config.get("MESHTASTIC_PORT", "")
                host_info = f"{host}:{port}"
            elif conn_type == "SERIAL":
                host_info = cm.config.get("MESHTASTIC_SERIAL_PORT", "")
            elif conn_type == "BLE":
                host_info = cm.config.get("MESHTASTIC_BLE_MAC", "")
            elif conn_type == "MQTT":
                host_info = f"{cm.config.get('MQTT_BROKER', '')}:{cm.config.get('MQTT_PORT', '1883')}"
            elif conn_type == "MESHCORE":
                mc_transport = cm.config.get("MESHCORE_TRANSPORT", "serial").lower()
                if mc_transport == "tcp":
                    host_info = f"{cm.config.get('MESHCORE_HOST', '')}:{cm.config.get('MESHCORE_PORT', '4000')}"
                elif mc_transport == "serial":
                    host_info = cm.config.get("MESHCORE_SERIAL_PORT", "")
                elif mc_transport == "ble":
                    host_info = cm.config.get("MESHCORE_BLE_MAC", "")

        # Uptime
        current_uptime = 0.0
        if cm and hasattr(cm, '_current_connected_since') and cm._current_connected_since is not None:
            current_uptime = now - cm._current_connected_since

        # Latency
        latency_ms = 0.0
        latency_avg_ms = 0.0
        if cm and hasattr(cm, '_latency_samples') and cm._latency_samples:
            latency_ms = cm._latency_samples[-1]
            latency_avg_ms = sum(cm._latency_samples) / len(cm._latency_samples)

        # Consecutive failures
        consecutive_failures = 0
        if cm:
            if hasattr(cm, '_consecutive_reconnect_failures'):
                consecutive_failures = cm._consecutive_reconnect_failures
            elif hasattr(cm, '_failure_strikes'):
                consecutive_failures = cm._failure_strikes

        # Connection counts
        total_connections = getattr(cm, '_connect_count', 0) if cm else 0
        total_disconnections = getattr(cm, '_disconnect_count', 0) if cm else 0
        total_uptime = 0.0
        if cm and hasattr(cm, '_total_uptime'):
            total_uptime = cm._total_uptime + current_uptime

        # Health checks
        health_checks = []
        if cm and hasattr(cm, '_health_check_results') and cm._health_check_results:
            health_checks = [
                {
                    "ts": fmt_ts(hc["ts"]),
                    "alive": hc["alive"],
                    "transport": hc["transport"],
                    "latency_ms": round(hc["latency_ms"], 1)
                }
                for hc in cm._health_check_results
            ]

        # Backoff
        backoff_current = 0.0
        if cm and not (cm.is_ready.is_set() if cm else False):
            if hasattr(cm, '_calculate_backoff'):
                backoff_current = cm._calculate_backoff(
                    getattr(cm, '_consecutive_reconnect_failures', 0)
                )

        # Backward compat status string
        status_str = md.connection_status or "Unknown"

        return {
            "state": state_str,
            "detail": detail,
            "status": status_str,  # backward compat
            "transport": transport_str,
            "host": host_info,
            "connected_since": fmt_ts(getattr(cm, '_current_connected_since', None) if cm else None),
            "uptime_seconds": round(current_uptime),
            "total_connections": total_connections,
            "total_disconnections": total_disconnections,
            "total_uptime_seconds": round(total_uptime),
            "last_disconnect_reason": getattr(cm, '_last_disconnect_reason', '') if cm else "",
            "latency_ms": round(latency_ms, 1),
            "latency_avg_ms": round(latency_avg_ms, 1),
            "health_checks": health_checks,
            "consecutive_failures": consecutive_failures,
            "backoff_current": round(backoff_current, 1),
        }

    # Single slot mode (backward compat)
    if slot_id:
        _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
        if not _slot:
            raise HTTPException(404, f"Slot '{slot_id}' not found.")
        return _build_slot_info(slot_id, _slot)

    # All slots mode
    slots = {}
    active_slot_id = "node_0"
    for sid, slot in g.NODE_REGISTRY.items():
        slots[sid] = _build_slot_info(sid, slot)

    return {
        "active_slot": active_slot_id,
        "slots": slots,
    }


@router.post("/api/connection/reconnect")
async def api_connection_reconnect(slot_id: str = "node_0", user: User = Depends(verify_csrf)):
    """Force reconnect for a slot that has given up (DISCONNECTED state) or was
    manually disconnected.

    This resets the connection manager's counters and transitions to CONNECTING.
    If the connect_loop task has exited (e.g., from a previous shutdown), it
    spawns a new one.
    """
    _slot = g.NODE_REGISTRY.get(slot_id)
    if not _slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")

    cm = _slot.g.connection_manager
    if not cm:
        raise HTTPException(503, f"No connection manager for slot '{slot_id}'.")

    if not hasattr(cm, 'force_reconnect'):
        raise HTTPException(501, "Connection manager does not support force_reconnect.")

    await cm.force_reconnect()

    # Check if the connect_loop task is still running; if not, restart it
    from core.packet import _make_slot_on_connection, _make_slot_on_node_updated, _packet_processing_worker_for_slot
    live_tasks = {t for t in _slot.tasks if not t.done()}
    has_connect_loop = any("connect_loop" in t.get_name() for t in live_tasks)
    if not has_connect_loop:
        logger.info("Restarting connect_loop task for slot '%s'", slot_id)
        t = asyncio.create_task(cm.connect_loop())
        t.set_name(f"Task-{slot_id}-connect_loop")
        _slot.tasks.add(t)
        t.add_done_callback(lambda task: _slot.tasks.discard(task))

    has_packet_worker = any("_packet_processing_worker" in t.get_name() for t in live_tasks)
    if not has_packet_worker:
        logger.info("Restarting packet_processing_worker task for slot '%s'", slot_id)
        t = asyncio.create_task(_packet_processing_worker_for_slot(_slot))
        t.set_name(f"Task-{slot_id}-_packet_processing_worker_for_slot")
        _slot.tasks.add(t)
        t.add_done_callback(lambda task: _slot.tasks.discard(task))

    return {"status": "reconnecting", "slot_id": slot_id}


@router.post("/api/connection/disconnect")
async def api_connection_disconnect(slot_id: str = "node_0", user: User = Depends(verify_csrf)):
    """Disconnect a slot's radio connection. The slot remains in the registry.

    Uses disconnect() (not shutdown()) so the connect_loop stays alive and the
    user can press Reconnect to resume. The connection manager transitions to
    DISCONNECTED state and parks until force_reconnect() is called.
    """
    _slot = g.NODE_REGISTRY.get(slot_id)
    if not _slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")

    cm = _slot.g.connection_manager
    if not cm:
        raise HTTPException(503, f"No connection manager for slot '{slot_id}'.")

    # Use disconnect() instead of shutdown() to keep the connect_loop alive
    if hasattr(cm, 'disconnect'):
        try:
            await cm.disconnect()
            return {"status": "disconnected", "slot_id": slot_id}
        except Exception as e:
            logger.error("Disconnect failed for %s: %s", slot_id, e)
            raise HTTPException(500, f"Disconnect failed: {e}")
    else:
        # Fallback for managers without disconnect() (shouldn't happen)
        try:
            await cm.shutdown()
            return {"status": "disconnected", "slot_id": slot_id}
        except Exception as e:
            logger.error("Disconnect (shutdown) failed for %s: %s", slot_id, e)
            raise HTTPException(500, f"Disconnect failed: {e}")


@router.get("/api/stats")
async def api_stats(slot_id: str = "node_0"):
    if slot_id == "all":
        # Aggregate stats across all slots
        merged: Dict[str, Any] = {}
        for sid, s in g.NODE_REGISTRY.items():
            st = s.g.meshtastic_data.get_serializable_stats()
            for k, v in st.items():
                if isinstance(v, (int, float)):
                    merged[k] = merged.get(k, 0) + v
                elif k not in merged:
                    merged[k] = v
        return merged
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _md = _slot.g.meshtastic_data if _slot else g.meshtastic_data
    return _md.get_serializable_stats()


