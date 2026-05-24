import core.globals as g
from core.c2 import send_system_message
from core.broadcast import broadcast_data, broadcast_stats
from core.auth import ensure_serializable
# Auto-extracted from meshtastic_dashboard.py
import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from core.routes.schemas import User, TracerouteRequest
from core.auth import verify_csrf, get_current_active_user
from pubsub import pub

try:
    from meshtastic.protobuf import mesh_pb2 as _mesh_pb2
    from meshtastic.protobuf import portnums_pb2
except ImportError:
    from meshtastic import mesh_pb2 as _mesh_pb2
    from meshtastic import portnums_pb2

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/nodes")
async def api_nodes(slot_id: str = "node_0"):
    if slot_id == "all":
        # Merge nodes from all slots; stamp heard_by_slot; later entries win on conflict
        merged: Dict[str, Any] = {}
        for sid, s in g.NODE_REGISTRY.items():
            for nid, ndata in s.g.meshtastic_data.nodes.items():
                node = dict(ndata)
                node["heard_by_slot"] = sid
                merged[nid] = node
        return merged
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _md = _slot.g.meshtastic_data if _slot else g.meshtastic_data
    if g.PUBLIC_MODE and not _md.local_node_id:
        return {}
    return _md.nodes


@router.get("/api/nodes/{node_id}")
async def api_node(node_id: str):
    if node_id in g.meshtastic_data.nodes:
        return g.meshtastic_data.nodes[node_id]
    raise HTTPException(404, "Node not found")


@router.get("/api/neighbors")
async def api_neighbors(slot_id: str = "node_0"):
    if slot_id == "all":
        seen = set()
        results = []
        for s in g.NODE_REGISTRY.values():
            rows = await asyncio.to_thread(s.g.db_manager.get_neighbors)
            for r in rows:
                key = (r.get("node_id"), r.get("neighbor_id"))
                if key not in seen:
                    seen.add(key)
                    results.append(r)
        return results
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _db = _slot.g.db_manager if _slot else g.db_manager
    return await asyncio.to_thread(_db.get_neighbors)


@router.get("/api/traceroutes")
async def api_traceroutes(limit: int = 50, slot_id: str = "node_0"):
    if slot_id == "all":
        all_tr = []
        for s in g.NODE_REGISTRY.values():
            rows = await asyncio.to_thread(s.g.db_manager.get_traceroutes, limit)
            all_tr.extend(rows)
        all_tr.sort(key=lambda r: r.get("timestamp", 0) or 0, reverse=True)
        return all_tr[:limit]
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _db = _slot.g.db_manager if _slot else g.db_manager
    return await asyncio.to_thread(_db.get_traceroutes, limit)


@router.post("/api/traceroute/run")
async def run_traceroute(req: TracerouteRequest, user: User = Depends(verify_csrf)):
    _slot = g.NODE_REGISTRY.get(req.slot_id) or g.NODE_REGISTRY.get("node_0")
    if not _slot or not _slot.g.connection_manager.is_ready.is_set():
        _md_status = _slot.g.meshtastic_data.connection_status if _slot else g.meshtastic_data.connection_status
        raise HTTPException(503, f"Radio not ready ({_md_status})")
    _cm = _slot.g.connection_manager
    _md = _slot.g.meshtastic_data
    _db = _slot.g.db_manager

    # Guard: interface must exist and be callable before we attempt to send
    if not _cm.interface or not hasattr(_cm.interface, "sendData"):
        raise HTTPException(503, "Radio interface not available  reconnecting, please retry.")

    target = req.node_id.strip()
    if not target.startswith("!"):
        raise HTTPException(400, "node_id must be in !xxxxxxxx format")

    result_holder: Dict[str, Any] = {}
    done = threading.Event()
    _sent_request_id = None
    _send_error: List[str] = []  # mutable container so inner closure can write to it

    def _on_traceroute(packet, interface=None):
        decoded = packet.get("decoded", {})
        if str(decoded.get("portnum")) != "TRACEROUTE_APP":
            return
        tr = decoded.get("traceroute", {})
        if not isinstance(tr, dict):
            return

        req_id          = decoded.get("requestId")
        actual_from     = packet.get("fromId", "")
        route_back_ints = tr.get("routeBack", [])
        route_to_ints   = tr.get("route", [])

        if _sent_request_id and req_id and req_id != _sent_request_id:
            return

        is_our_response = (
            actual_from == target
            or bool(route_back_ints)
            or (route_to_ints and isinstance(route_to_ints[0], int)
                and f"!{route_to_ints[0]:08x}" == target)
        )
        if not is_our_response:
            return

        local_hex       = _md.local_node_id or "?"
        snr_towards_raw = tr.get("snrTowards", [])
        snr_back_raw    = tr.get("snrBack", [])

        def hex_id(n):
            return f"!{n:08x}" if isinstance(n, int) else str(n)

        def snr_db(raw):
            return round(raw / 4.0, 2)

        nodes_to   = [local_hex] + [hex_id(n) for n in route_to_ints]   + [target]
        nodes_back = [target]    + [hex_id(n) for n in route_back_ints] + [local_hex]

        hop_start          = packet.get("hopStart", 0)
        hop_limit_pkt      = packet.get("hopLimit", 0)
        hops_used          = max(0, hop_start - hop_limit_pkt)
        total_intermediate = len(route_to_ints) + len(route_back_ints)

        result_holder.update({
            "target":      target,
            "origin":      local_hex,
            "slot_id":     req.slot_id,
            "rssi":        packet.get("rxRssi"),
            "snr":         packet.get("rxSnr"),
            "hop_start":   hop_start,
            "hop_limit":   hop_limit_pkt,
            "hops_used":   hops_used,
            "direct_link": total_intermediate == 0,
            "path_to": [
                {
                    "from": nodes_to[i],
                    "to":   nodes_to[i + 1],
                    "snr":  snr_db(snr_towards_raw[i]) if i < len(snr_towards_raw) else None,
                }
                for i in range(len(nodes_to) - 1)
            ],
            "path_back": [
                {
                    "from": nodes_back[i],
                    "to":   nodes_back[i + 1],
                    "snr":  snr_db(snr_back_raw[i]) if i < len(snr_back_raw) else None,
                }
                for i in range(len(nodes_back) - 1)
            ],
            "timestamp": time.time(),
        })

        _db.save_traceroute(
            from_id=local_hex,
            to_id=target,
            route_list=route_to_ints,
            timestamp=result_holder["timestamp"],
            route_back=route_back_ints,
            snr_towards=snr_towards_raw,
            snr_back=snr_back_raw,
            rssi=result_holder["rssi"],
            snr=result_holder["snr"],
            hops_used=hops_used,
        )
        done.set()

    pub.subscribe(_on_traceroute, "meshtastic.receive")
    try:
        def _send_and_capture():
            nonlocal _sent_request_id
            try:
                r = _mesh_pb2.RouteDiscovery()
                pkt = _cm.interface.sendData(
                    r,
                    destinationId=target,
                    portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                    wantResponse=True,
                    channelIndex=0,
                    hopLimit=req.hop_limit,
                )
                if pkt and hasattr(pkt, "id"):
                    _sent_request_id = pkt.id
            except Exception as _send_exc:
                _send_error.append(str(_send_exc))
                logger.error("sendData (traceroute) failed for %s: %s", target, _send_exc)
        await asyncio.to_thread(_send_and_capture)

        # If sending itself failed, raise immediately rather than waiting 60s
        if _send_error:
            raise HTTPException(500, f"Failed to send traceroute packet: {_send_error[0]}")

        await asyncio.to_thread(done.wait, 60)
    finally:
        try:
            pub.unsubscribe(_on_traceroute, "meshtastic.receive")
        except Exception:
            pass

    if not done.is_set():
        raise HTTPException(504, f"No traceroute response from {target} within 60s  node may be offline or out of range.")

    await broadcast_data({"event": "traceroute_result", "data": result_holder}, slot_id=req.slot_id)
    return result_holder


@router.get("/api/nodes/{node_id}/count/{item_type}")
async def api_node_count(
    node_id: str,
    item_type: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    slot_id: str = "node_0",
):
    if item_type not in ["messages_sent", "positions", "telemetry"]:
        raise HTTPException(400, "Invalid item_type")
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _db = _slot.g.db_manager if _slot else g.db_manager
    count = await asyncio.to_thread(_db.count_node_items, node_id, item_type, start, end)
    if count == -1:
        raise HTTPException(400, "Invalid item type")
    return {"node_id": node_id, "item_type": item_type, "count": count}


@router.get("/api/nodes/{node_id}/history/{table_name}")
async def api_node_history_endpoint(
    node_id: str,
    table_name: str,
    limit: int = 1000,
    start: Optional[float] = None,
    end: Optional[float] = None,
    slot_id: str = "node_0",
):
    valid_tables = ["positions", "telemetry", "packets"]
    if table_name not in valid_tables:
        raise HTTPException(400, f"Invalid table. Must be one of: {valid_tables}")

    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _db = _slot.g.db_manager if _slot else g.db_manager

    if table_name == "packets":
        try:
            q = "SELECT * FROM packets WHERE from_id = ?"
            params: list = [node_id]
            if start:
                q += " AND timestamp >= ?"
                params.append(start)
            if end:
                q += " AND timestamp <= ?"
                params.append(end)
            q += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            def fetch_raw():
                conn = _db._get_connection()
                rows = conn.execute(q, params).fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    if d.get("decoded"):
                        try:
                            d["decoded"] = json.loads(d["decoded"])
                        except Exception:
                            pass
                    if d.get("raw"):
                        try:
                            d["raw"] = json.loads(d["raw"])
                        except Exception:
                            pass
                    results.append(d)
                return results

            return await asyncio.to_thread(fetch_raw)
        except Exception as e:
            logger.error(f"Error fetching packet history: {e}")
            raise HTTPException(500, "Database error fetching packets")

    return await asyncio.to_thread(_db.get_node_history, node_id, table_name, start, end, limit)


