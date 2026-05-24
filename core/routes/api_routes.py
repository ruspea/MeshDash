import core.globals as g
import logging
"""
API Data Routes — extracted from meshtastic_dashboard.py
All routes in this module use lazy imports to avoid circular dependency issues.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Response, Request, Path
from fastapi.responses import JSONResponse, PlainTextResponse
from typing import Optional, Any, Dict, List
import asyncio
import time
import json
import base64

logger = logging.getLogger(__name__)

from core.auth import get_current_active_user, User, ensure_serializable, verify_csrf

router = APIRouter()


# Helper — resolve slot

def _resolve_slot(slot_id: str):
    """Return (slot, connection_manager, meshtastic_data, db_manager) for slot_id."""
    # Lazy import to avoid circular
    from meshtastic_dashboard import NODE_REGISTRY, connection_manager, meshtastic_data, db_manager, PUBLIC_MODE

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    if not _slot:
        if slot_id == "node_0":
            _slot = NODE_REGISTRY.get("node_0")
        if not _slot:
            raise HTTPException(503, f"Slot '{slot_id}' not found")

    _cm = _slot.connection_manager if _slot else connection_manager
    _md = _slot.meshtastic_data if _slot else meshtastic_data
    _db = _slot.db_manager if _slot else db_manager
    return _slot, _cm, _md, _db


def _is_connected(cm):
    """Check if a connection manager is connected (works for Serial/TCP/WebSerial and MQTT).

    Serial/TCP/WebSerial: .interface is set when connected.
    MQTT: .interface is always None but .is_ready is set on connect.
    """
    if cm is None:
        return False
    if hasattr(cm, 'is_ready') and cm.is_ready.is_set():
        return True
    return bool(getattr(cm, 'interface', None))


def _is_mqtt(cm):
    """Check if a connection manager is MQTT-based."""
    from core.connections.mqtt import MQTTConnectionManager
    return isinstance(cm, MQTTConnectionManager)


# GET /api/nodes

@router.get("/api/nodes")
async def api_nodes(slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, meshtastic_data, PUBLIC_MODE

    if slot_id == "all":
        merged: Dict[str, Any] = {}
        for sid, s in NODE_REGISTRY.items():
            for nid, ndata in s.meshtastic_data.nodes.items():
                node = dict(ndata)
                node["heard_by_slot"] = sid
                merged[nid] = node
        return merged
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _md = _slot.meshtastic_data if _slot else meshtastic_data
    if PUBLIC_MODE and not _md.local_node_id:
        return {}
    return _md.nodes


# GET /api/nodes/{node_id}

@router.get("/api/nodes/{node_id}")
async def api_node(node_id: str):
    from meshtastic_dashboard import meshtastic_data

    if node_id in meshtastic_data.nodes:
        return meshtastic_data.nodes[node_id]
    raise HTTPException(404, "Node not found")


# GET /api/node/config

@router.get("/api/node/config")
async def get_node_config(slot_id: str = "node_0", user: User = Depends(get_current_active_user)):
    _slot, _cm, _md, _db = _resolve_slot(slot_id)
    if not _is_connected(_cm):
        raise HTTPException(503, "Radio not connected")
    if not _cm.is_ready.is_set():
        raise HTTPException(503, f"Radio not ready: {_md.connection_status}")
    if _is_mqtt(_cm):
        # MQTT has no local node config — return synthetic info if available
        if _md.local_node_info:
            return JSONResponse(content=ensure_serializable(_md.local_node_info))
        raise HTTPException(503, "No local node info for MQTT slot")
    try:
        from meshtastic_dashboard import _nc_build_snapshot
        snapshot = await asyncio.to_thread(_nc_build_snapshot, _cm.interface)
        return JSONResponse(content=ensure_serializable(snapshot))
    except Exception as e:
        from meshtastic_dashboard import logger
        logger.error(f"Node config read error: {e}", exc_info=True)
        raise HTTPException(500, f"Failed to read node configuration: {e}")


# POST /api/node/config/save

class NodeConfigSaveRequest:
    """Parsed from body dict — slot_id and changes are required."""
    def __init__(self, data: dict):
        self.slot_id: str = data.get("slot_id", "node_0")
        self.changes: dict = data.get("changes", {})
        self.reboot: bool = data.get("reboot", False)


@router.post("/api/node/config/save")
async def save_node_config(
    req: dict,
    background_tasks_obj: BackgroundTasks,
    request: Request,
    user: User = Depends(verify_csrf),
):
    parsed = NodeConfigSaveRequest(req)
    _slot, _cm, _md, _db = _resolve_slot(parsed.slot_id)

    if not _is_connected(_cm):
        raise HTTPException(503, "Radio not connected")
    if not _cm.is_ready.is_set():
        raise HTTPException(503, f"Radio not ready: {_md.connection_status}")
    if _is_mqtt(_cm):
        raise HTTPException(400, "Node config changes not supported for MQTT slots")
    if not parsed.changes:
        raise HTTPException(400, "No changes provided")

    from meshtastic_dashboard import _nc_apply_changes, logger, send_system_message

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_nc_apply_changes, _cm.interface, parsed.changes),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Configuration write timed out after 30s")
    except Exception as e:
        logger.error(f"Node config save error: {e}", exc_info=True)
        raise HTTPException(500, f"Configuration apply failed: {e}")

    logger.info(f"Node config saved by {user.username} [slot={parsed.slot_id}]: written={result['written']} errors={result['errors']}")

    if parsed.reboot and result["written"]:
        async def _trigger_reboot():
            await asyncio.sleep(1.5)
            await send_system_message(f"🔄 Node [{parsed.slot_id}] rebooting after config change...")
            try:
                await asyncio.to_thread(_cm.interface.localNode.reboot)
            except Exception as rb_err:
                logger.warning(f"Reboot call error (expected if radio resets): {rb_err}")

        background_tasks_obj.add_task(_trigger_reboot)

    return JSONResponse({
        "status": "success" if not result["errors"] else "partial",
        "written": result["written"],
        "errors": result["errors"],
        "reboot_triggered": parsed.reboot and bool(result["written"]),
        "slot_id": parsed.slot_id,
    })


# GET /api/packets

@router.get("/api/packets")
async def api_packets(limit: int = 50, slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, meshtastic_data

    if slot_id == "all":
        all_pkts = []
        for s in NODE_REGISTRY.values():
            pkts = s.meshtastic_data.get_formatted_packets_from_memory(limit)
            all_pkts.extend(pkts)
        all_pkts.sort(key=lambda p: p.get("timestamp", 0) or 0, reverse=True)
        return all_pkts[:limit]
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _md = _slot.meshtastic_data if _slot else meshtastic_data
    return _md.get_formatted_packets_from_memory(limit)


# GET /api/packets/history

@router.get("/api/packets/history")
async def api_pkt_hist(limit: int = 100, slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    if slot_id == "all":
        all_pkts = []
        for s in NODE_REGISTRY.values():
            rows = await asyncio.to_thread(s.db_manager.get_recent_packets, limit)
            all_pkts.extend(rows)
        all_pkts.sort(key=lambda p: p.get("timestamp", 0) or 0, reverse=True)
        return all_pkts[:limit]
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    return await asyncio.to_thread(_db.get_recent_packets, limit)


# GET /api/neighbors

@router.get("/api/neighbors")
async def api_neighbors(slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    if slot_id == "all":
        seen = set()
        results = []
        for s in NODE_REGISTRY.values():
            rows = await asyncio.to_thread(s.db_manager.get_neighbors)
            for r in rows:
                key = (r.get("node_id"), r.get("neighbor_id"))
                if key not in seen:
                    seen.add(key)
                    results.append(r)
        return results
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    return await asyncio.to_thread(_db.get_neighbors)


# GET /api/traceroutes

@router.get("/api/traceroutes")
async def api_traceroutes(limit: int = 50, slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    if slot_id == "all":
        all_tr = []
        for s in NODE_REGISTRY.values():
            rows = await asyncio.to_thread(s.db_manager.get_traceroutes, limit)
            all_tr.extend(rows)
        all_tr.sort(key=lambda r: r.get("timestamp", 0) or 0, reverse=True)
        return all_tr[:limit]
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    return await asyncio.to_thread(_db.get_traceroutes, limit)


# GET /api/waypoints

@router.get("/api/waypoints")
async def api_waypoints():
    from meshtastic_dashboard import db_manager
    return await asyncio.to_thread(db_manager.get_waypoints)


# GET /api/hardware_logs

@router.get("/api/hardware_logs")
async def api_hw_logs(limit: int = 50):
    from meshtastic_dashboard import db_manager
    return await asyncio.to_thread(db_manager.get_hardware_logs, limit)


# GET /api/messages/history

@router.get("/api/messages/history")
async def api_msg_hist(
    from_id: str = None,
    to_id: str = None,
    channel: int = None,
    limit: int = 100,
    slot_id: str = "node_0",
):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    if to_id and to_id != "^all":
        channel = None
    elif channel is None:
        channel = 0
    return await asyncio.to_thread(_db.get_messages, from_id, to_id, channel, limit=limit)


# GET /api/metrics/averages

@router.get("/api/metrics/averages")
async def api_metrics(limit: int = 100, slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    cur, hist = await asyncio.gather(
        asyncio.to_thread(_db.get_most_recent_average_metrics),
        asyncio.to_thread(_db.get_average_metrics_history, limit),
    )
    return {"most_recent": cur, "history": hist}


# GET /api/counts/totals

@router.get("/api/counts/totals")
async def api_counts(slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    m, p, t = await asyncio.gather(
        asyncio.to_thread(_db.count_node_items, None, "messages_sent"),
        asyncio.to_thread(_db.count_node_items, None, "positions"),
        asyncio.to_thread(_db.count_node_items, None, "telemetry"),
    )
    return {"total_messages": m, "total_positions": p, "total_telemetry": t}


# GET /api/geocode

@router.get("/api/geocode")
async def geocode_reverse_route(lat: float, lon: float):
    from core.geocode import _geocode_reverse
    return await _geocode_reverse(lat, lon)


# GET /api/nodes/{node_id}/count/{item_type}

@router.get("/api/nodes/{node_id}/count/{item_type}")
async def api_node_count(
    node_id: str,
    item_type: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    slot_id: str = "node_0",
):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    if item_type not in ["messages_sent", "positions", "telemetry"]:
        raise HTTPException(400, "Invalid item_type")
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    count = await asyncio.to_thread(_db.count_node_items, node_id, item_type, start, end)
    if count == -1:
        raise HTTPException(400, "Invalid item type")
    return {"node_id": node_id, "item_type": item_type, "count": count}


# GET /api/channels

@router.get("/api/channels")
async def api_get_channels(slot_id: str = "node_0"):
    from meshtastic_dashboard import (
        NODE_REGISTRY, connection_manager, meshtastic_data, loaded_config, ensure_serializable
    )

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _cm = _slot.connection_manager if _slot else connection_manager
    _md = _slot.meshtastic_data if _slot else meshtastic_data
    _cfg = _slot.connection_manager.config if _slot else loaded_config
    ws_mode = _cfg.get("MESHTASTIC_CONNECTION_TYPE", "").upper() == "WEBSERIAL"
    mqtt_mode = _is_mqtt(_cm)

    # MQTT and WebSerial have no .interface — use local_node_info fallback
    if not _is_connected(_cm):
        if (ws_mode or mqtt_mode) and _md.local_node_info:
            try:
                chans_json = _md.local_node_info.get("channels_json")
                if chans_json:
                    chans = json.loads(chans_json)
                    return ensure_serializable([{
                        "index": c.get("index"),
                        "name": c.get("settings", {}).get("name", f"Channel {c.get('index')}"),
                        "role": c.get("role", "DISABLED"),
                        "psk": c.get("settings", {}).get("psk", ""),
                        "uplink": c.get("settings", {}).get("uplink_enabled", False),
                        "downlink": c.get("settings", {}).get("downlink_enabled", False),
                    } for c in chans])
            except Exception:
                pass
            return []
        raise HTTPException(503, "Radio not connected")

    # MQTT-specific: no local node config burst, synthesize channels from packet data
    if mqtt_mode:
        channels = []
        # Prefer channels_json from local_node_info (populated if MQTT_NODE_ID matches)
        if _md.local_node_info and "channels_json" in _md.local_node_info:
            try:
                channels = json.loads(_md.local_node_info["channels_json"])
            except Exception:
                pass
        # Fallback: build from channel_map (populated from NodeInfo packets seen on MQTT)
        # Filter to valid Meshtastic channel range (0-7)
        if not channels and _md.channel_map:
            for ch_id, ch_idx in _md.channel_map.items():
                if isinstance(ch_idx, int) and 0 <= ch_idx <= 7:
                    channels.append({"index": ch_idx, "role": "1", "settings": {"name": str(ch_id), "psk": ""}})
        # Fallback: scan recent packets for unique channel indices (Meshtastic range 0-7)
        if not channels:
            seen = {}
            for pkt in list(_md.packets)[-500:]:
                ch = pkt.get("channel")
                if ch is not None and 0 <= ch <= 7 and ch not in seen:
                    seen[ch] = True
                    channels.append({"index": ch, "role": "1", "settings": {"name": f"Channel {ch}", "psk": ""}})
        # Always include channel 0 if nothing else
        if not channels:
            channels.append({"index": 0, "role": "1", "settings": {"name": "LongFast", "psk": "AQ=="}})
        result = []
        for ch in channels:
            if not ch or not isinstance(ch, dict):
                continue
            settings = ch.get("settings", {})
            result.append({
                "index": ch.get("index"),
                "name": settings.get("name", f"Channel {ch.get('index')}"),
                "role": ch.get("role", "DISABLED"),
                "psk": settings.get("psk", ""),
                "uplink": settings.get("uplink_enabled", False),
                "downlink": settings.get("downlink_enabled", False),
            })
        return ensure_serializable(result)

    if not _md.local_node_info:
        if hasattr(_cm.interface, "myInfo"):
            _md.set_local_node_info(_cm.interface.myInfo)

    try:
        channels = []
        if _md.local_node_info and "channels_json" in _md.local_node_info:
            channels = json.loads(_md.local_node_info["channels_json"])
        elif (
            hasattr(_cm.interface, "localNode")
            and hasattr(_cm.interface.localNode, "channels")
        ):
            for c in _cm.interface.localNode.channels:
                s = getattr(c, "settings", None)
                if not s:
                    continue
                channels.append({
                    "index": getattr(c, "index", 0),
                    "role": str(getattr(c, "role", "DISABLED")),
                    "settings": {
                        "name": getattr(s, "name", ""),
                        "psk": base64.b64encode(getattr(s, "psk", b"")).decode("utf-8"),
                        "uplink_enabled": getattr(s, "uplink_enabled", False),
                        "downlink_enabled": getattr(s, "downlink_enabled", False),
                    },
                })

        result = []
        for ch in channels:
            if not ch or not isinstance(ch, dict):
                continue
            settings = ch.get("settings", {})
            result.append({
                "index": ch.get("index"),
                "name": settings.get("name", f"Channel {ch.get('index')}"),
                "role": ch.get("role", "DISABLED"),
                "psk": settings.get("psk", ""),
                "uplink": settings.get("uplink_enabled", False),
                "downlink": settings.get("downlink_enabled", False),
            })
        return ensure_serializable(result)
    except HTTPException:
        raise
    except Exception as e:
        from meshtastic_dashboard import logger
        logger.warning(f"Channel read error [{slot_id}]: {e}")
        raise HTTPException(503, "Connection unstable - please retry")


# GET /api/local_node/full

@router.get("/api/local_node/full")
async def api_local_node_full(slot_id: str = "node_0"):
    from meshtastic_dashboard import NODE_REGISTRY, connection_manager, meshtastic_data, loaded_config, ensure_serializable

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _cm = _slot.connection_manager if _slot else connection_manager
    _md = _slot.meshtastic_data if _slot else meshtastic_data
    _cfg = _slot.connection_manager.config if _slot else loaded_config
    ws_mode = _cfg.get("MESHTASTIC_CONNECTION_TYPE", "").upper() == "WEBSERIAL" if _slot else False
    mqtt_mode = _is_mqtt(_cm)

    if not _is_connected(_cm):
        if ws_mode or mqtt_mode:
            if _md.local_node_info:
                return ensure_serializable(_md.local_node_info)
            return JSONResponse({
                "node_id": _md.local_node_id or "unknown",
                "node_num": 0,
                "long_name": "",
                "short_name": "",
                "status": "connecting",
                "connection": "MQTT" if mqtt_mode else "WEBSERIAL",
            })
        if _md.local_node_info:
            return ensure_serializable(_md.local_node_info)
        raise HTTPException(503, "Radio not connected")

    # MQTT slots don't have a .interface object
    if mqtt_mode:
        if _md.local_node_info:
            return ensure_serializable(_md.local_node_info)
        return JSONResponse({
            "node_id": _md.local_node_id or "unknown",
            "node_num": 0,
            "long_name": "",
            "short_name": "",
            "status": "connected",
            "connection": "MQTT",
        })

    interface = _cm.interface
    try:
        info = getattr(interface, "myInfo", None)
        metadata = getattr(interface, "metadata", None)
        if not info:
            raise HTTPException(503, "Local node info not yet available.")

        result = {
            "node_id": f"!{info.my_node_num:08x}",
            "node_num": info.my_node_num,
            "hardware_model_string": getattr(metadata, "hw_model_str", "Unknown") if metadata else "Unknown",
            "firmware_version": getattr(metadata, "firmware_version", "Unknown") if metadata else "Unknown",
            "long_name": None, "short_name": None, "macaddr": None,
            "latitude": None, "longitude": None, "altitude": None,
            "battery_level": None, "voltage": None,
            "channel_utilization": None, "air_util_tx": None, "uptime_seconds": None,
            "lora_region": None, "lora_hop_limit": None, "lora_tx_power": None,
            "lora_tx_enabled": None, "lora_use_preset": None,
            "wifi_ssid": None, "bluetooth_enabled": None,
            "node_info_broadcast_secs": None, "position_broadcast_secs": None, "gps_mode": None,
            "role": str(getattr(info, "role", "CLIENT")),
            "region": str(getattr(info, "region", "Unknown")),
            "max_channels": getattr(info, "max_channels", 0),
            "nodedb_count": getattr(info, "nodedb_count", len(interface.nodes) if hasattr(interface, "nodes") else 0),
        }

        if hasattr(interface, "nodes") and info.my_node_num in interface.nodes:
            local_node = interface.nodes[info.my_node_num]
            user = local_node.get("user", {})
            result.update({
                "long_name": user.get("longName"),
                "short_name": user.get("shortName"),
                "macaddr": user.get("macaddr"),
            })
            position = local_node.get("position", {})
            result.update({
                "latitude": position.get("latitude"),
                "longitude": position.get("longitude"),
                "altitude": position.get("altitude"),
            })
            metrics = local_node.get("deviceMetrics", {})
            result.update({
                "battery_level": metrics.get("batteryLevel"),
                "voltage": metrics.get("voltage"),
                "channel_utilization": metrics.get("channelUtilization"),
                "air_util_tx": metrics.get("airUtilTx"),
                "uptime_seconds": metrics.get("uptimeSeconds"),
            })

        if hasattr(interface, "localNode") and interface.localNode:
            ln = interface.localNode
            lc = getattr(ln, "localConfig", None)
            if lc:
                if hasattr(lc, "lora"):
                    result.update({
                        "lora_region": str(lc.lora.region),
                        "lora_hop_limit": lc.lora.hop_limit,
                        "lora_tx_power": lc.lora.tx_power,
                        "lora_tx_enabled": lc.lora.tx_enabled,
                        "lora_use_preset": lc.lora.use_preset,
                    })
                if hasattr(lc, "network"):
                    result["wifi_ssid"] = lc.network.wifi_ssid
                if hasattr(lc, "bluetooth"):
                    result["bluetooth_enabled"] = lc.bluetooth.enabled
                if hasattr(lc, "device"):
                    result["node_info_broadcast_secs"] = lc.device.node_info_broadcast_secs
                if hasattr(lc, "position"):
                    result["position_broadcast_secs"] = lc.position.position_broadcast_secs
                    result["gps_mode"] = str(lc.position.gps_mode)

        return ensure_serializable(result)
    except HTTPException:
        raise
    except Exception as e:
        from meshtastic_dashboard import logger
        logger.warning(f"Connection reset during node read: {e}")
        raise HTTPException(503, "Connection unstable - please retry")


# POST /api/alert

@router.post("/api/alert")
async def trigger_alert(msg: str, request: Request, user: User = Depends(verify_csrf)):
    from meshtastic_dashboard import send_system_message
    await send_system_message(f"ALERT: {msg}")
    return {"status": "Alert broadcasted"}


# POST /api/console

class ConsoleRequest:
    def __init__(self, data: dict):
        self.command: str = data.get("command", "")
        self.slot_id: str = data.get("slot_id", "node_0")


@router.post("/api/console")
async def api_console(req: dict, request: Request, user: User = Depends(verify_csrf)):
    from meshtastic_dashboard import send_system_message, execute_meshtastic_command

    parsed = ConsoleRequest(req)
    await send_system_message(f"User {user.username} executed: {parsed.command}")
    try:
        output = await asyncio.wait_for(
            asyncio.to_thread(execute_meshtastic_command, parsed.command, parsed.slot_id),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        from meshtastic_dashboard import logger
        logger.error("❌ Console command timed out after 30s")
        raise HTTPException(504, "Command timed out.")
    return PlainTextResponse(output)


# POST /api/mqtt/channel_key

@router.post("/api/mqtt/channel_key")
async def mqtt_set_channel_key(
    body: dict,
    request: Request,
    user: User = Depends(verify_csrf),
):
    from meshtastic_dashboard import NODE_REGISTRY, logger, _HAS_MQTT, MQTTConnectionManager

    slot_id = body.get("slot_id", "node_0")
    channel_id = str(body.get("channel_id", ""))
    channel_idx = body.get("channel_idx", 0)
    psk_b64 = str(body.get("psk", "")).strip()

    if not psk_b64:
        raise HTTPException(400, "psk is required")

    slot = NODE_REGISTRY.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found")

    cm = slot.connection_manager
    if not _HAS_MQTT or not isinstance(cm, MQTTConnectionManager):
        raise HTTPException(400, f"Slot '{slot_id}' is not an MQTT slot")

    ok = cm.set_channel_psk(channel_id, psk_b64)
    if not ok:
        raise HTTPException(400, "Invalid PSK — could not parse base64 key")

    logger.info("MQTT [%s]: PSK stored for channel '%s' (idx %s)", slot_id, channel_id, channel_idx)
    return {"status": "ok", "slot_id": slot_id, "channel_id": channel_id}


# GET /api/search

@router.get("/api/search")
async def api_global_search(
    q: str,
    limit: int = 50,
    slot_id: str = "node_0",
    user: User = Depends(get_current_active_user),
):
    from meshtastic_dashboard import NODE_REGISTRY, db_manager

    if not q or len(q.strip()) < 2:
        return []
    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _db = _slot.db_manager if _slot else db_manager
    return await asyncio.to_thread(_db.global_search, q.strip(), limit)


# GET /api/c2/status

@router.get("/api/c2/status")
async def c2_status(user: User = Depends(get_current_active_user)):
    from meshtastic_dashboard import c2_activity
    return c2_activity.get_snapshot()


# GET /api/monitor

@router.get("/api/monitor")
async def api_monitor(
    url: str = Query(...),
    expected_pattern: Optional[str] = None,
    timeout: float = 10.0,
    slot_id: str = "node_0",
    user: User = Depends(get_current_active_user),
):
    from meshtastic_dashboard import logger

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            content = resp.text
    except Exception as e:
        return {
            "url": url,
            "reachable": False,
            "status_code": None,
            "response_time_ms": None,
            "error": str(e),
            "pattern_matched": None,
        }

    import time
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            t0 = time.monotonic()
            resp = await client.get(url)
            rt = (time.monotonic() - t0) * 1000
    except Exception:
        rt = None

    matched = None
    if expected_pattern:
        import re
        matched = bool(re.search(expected_pattern, content))

    return {
        "url": url,
        "reachable": True,
        "status_code": resp.status_code,
        "response_time_ms": round(rt, 1) if rt is not None else None,
        "pattern_matched": matched,
    }


# POST /api/monitor (website monitor with body)

class WebsiteMonitorRequest:
    def __init__(self, data: dict):
        self.url: str = data.get("url", "")
        self.expected_pattern: Optional[str] = data.get("expected_pattern")
        self.timeout: float = float(data.get("timeout", 10.0))
        self.slot_id: str = data.get("slot_id", "node_0")


@router.post("/api/monitor")
async def monitor(req: dict, request: Request, user: User = Depends(verify_csrf)):
    from meshtastic_dashboard import validate_url, logger

    parsed = WebsiteMonitorRequest(req)
    is_valid, reason = await asyncio.to_thread(validate_url, parsed.url)
    if not is_valid:
        raise HTTPException(400, f"Invalid Target: {reason}")

    import httpx

    try:
        async with httpx.AsyncClient(timeout=parsed.timeout, follow_redirects=True) as client:
            t0 = time.monotonic()
            resp = await client.get(parsed.url)
            rt = (time.monotonic() - t0) * 1000
            content = resp.text
    except Exception as e:
        return {
            "url": parsed.url,
            "reachable": False,
            "status_code": None,
            "response_time_ms": None,
            "error": str(e),
            "pattern_matched": None,
        }

    matched = None
    if parsed.expected_pattern:
        import re
        matched = bool(re.search(parsed.expected_pattern, content))

    return {
        "url": parsed.url,
        "reachable": True,
        "status_code": resp.status_code,
        "response_time_ms": round(rt, 1),
        "pattern_matched": matched,
    }


# GET /api/slots/{slot_id}/status
# NOTE: The canonical implementation is in slot_routes.py which returns
# connection_state, connection_detail, connection_transport, local_node_id,
# local_node_info, nodes, stats etc. This stub is kept only as a fallback
# if slot_routes is not mounted. If both are mounted, slot_routes wins.

# The actual endpoint is defined in slot_routes.py — do not re-register here
# to avoid route shadowing.

# POST /api/messages

class MessageRequest:
    def __init__(self, data: dict):
        self.message: str = data.get("message", "")
        self.destination: Optional[str] = data.get("destination")
        self.channel: Optional[int] = data.get("channel")
        self.slot_id: str = data.get("slot_id", "node_0")


@router.post("/api/messages")
async def send_msg(req: dict, request: Request, u: User = Depends(verify_csrf)):
    from meshtastic_dashboard import (
        NODE_REGISTRY, connection_manager, meshtastic_data, logger, send_system_message
    )

    parsed = MessageRequest(req)
    _slot = NODE_REGISTRY.get(parsed.slot_id) or NODE_REGISTRY.get("node_0")
    if _slot is None:
        raise HTTPException(503, "Connection manager not initialised.")

    _cm = _slot.connection_manager
    _md = _slot.meshtastic_data
    _db = _slot.db_manager

    if not _cm.is_ready.is_set():
        raise HTTPException(503, f"Radio not ready ({_md.connection_status}) - please wait for reconnection.")

    channel_index = parsed.channel if parsed.channel is not None else 0
    destination = parsed.destination or "^all"
    want_ack = destination != "^all"

    logger.info(f"📤 Outbound message  slot={parsed.slot_id}  dest={destination}  ch={channel_index}  msg='{parsed.message[:60]}'")

    try:
        mesh_packet = await _cm.sendText(
            parsed.message,
            destinationId=destination,
            channelIndex=channel_index,
            wantAck=want_ack,
        )
        if mesh_packet is None:
            raise HTTPException(503, "Send failed - radio did not accept the packet.")

        packet_id = getattr(mesh_packet, "id", None) if not isinstance(mesh_packet, dict) else mesh_packet.get("id")
        sender_id = _md.local_node_id
        if not sender_id:
            # Serial/TCP/BLE: try myInfo from interface
            if _cm.interface:
                info = getattr(_cm.interface, "myInfo", None)
                if info:
                    sender_id = f"!{info.my_node_num:08x}"
            # MQTT / MeshCore: try my_node_id property (duck-type)
            elif hasattr(_cm, 'my_node_id') and _cm.my_node_id:
                sender_id = f"!{_cm.my_node_id:08x}"
            # MQTT fallback: MQTT_NODE_ID from config
            elif hasattr(_cm, 'config') and _cm.config.get('MQTT_NODE_ID'):
                nid = _cm.config['MQTT_NODE_ID'].strip()
                if nid.startswith('!') and len(nid) == 9:
                    sender_id = nid

        now = int(time.time())
        initial_status = "BROADCAST" if destination == "^all" else "SENT"
        event_id = f"tx_{time.time_ns()}"

        try:
            conn = _db._get_connection()
            conn.execute(
                """INSERT INTO messages
                (packet_event_id, mesh_packet_id, from_id, to_id, channel, text, timestamp, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(packet_event_id) DO NOTHING""",
                (event_id, packet_id, sender_id, destination, channel_index, parsed.message, now, initial_status)
            )
            conn.commit()
        except Exception as e:
            logger.error(f"❌ Failed to save outbound message to DB: {e}")

        logger.info(f"✅ Message sent  slot={parsed.slot_id}  id={packet_id}  ch={channel_index} status={initial_status}")
        return {
            "status": initial_status.lower(),
            "channel": channel_index,
            "packet_id": packet_id,
            "timestamp": now,
            "slot_id": parsed.slot_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"❌ Send error: {exc}", exc_info=True)
        raise HTTPException(500, f"Transmission failed: {exc}")