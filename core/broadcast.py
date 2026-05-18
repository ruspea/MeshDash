import core.globals as g
from core.auth import ensure_serializable
from typing import Dict, Any, List, Optional
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# SSE queues live on the globals module — set by meshtastic_dashboard.py at startup.
# slot_routes.py also uses g.sse_queues / g.all_sse_queues for client registration.
# Using the globals ensures everyone shares the same dict objects.

async def broadcast_data(payload: Dict, slot_id: str = "node_0"):
    """Broadcast to all SSE clients subscribed to slot_id.
    Falls back to the legacy global sse_queues for slot node_0 so existing
    single-slot front-ends keep working with no changes.
    Also fans out every event to all_sse_queues so /sse/all clients receive
    events from every slot simultaneously.
    """
    try:
        json_str = json.dumps(ensure_serializable(payload["data"]))
    except Exception as e:
        logger.warning("broadcast_data serialization error: %s", e)
        return
    msg = {"event": payload["event"], "data": json_str}

    #  Stamp slot_id into the event data where applicable 
    # For events that carry node/packet data, inject heard_by_slot so the
    # frontend knows which radio produced this event.
    if slot_id and slot_id != "node_0":
        try:
            d = json.loads(json_str)
            if isinstance(d, dict) and "node_id" in d:
                d["heard_by_slot"] = slot_id
                msg = {"event": payload["event"], "data": json.dumps(d)}
            elif isinstance(d, list):
                for item in d:
                    if isinstance(item, dict):
                        item.setdefault("heard_by_slot", slot_id)
                msg = {"event": payload["event"], "data": json.dumps(d)}
        except Exception:
            pass

    #  Route to slot-specific subscribers 
    slot = g.NODE_REGISTRY.get(slot_id)
    if slot and slot_id != "node_0":
        async with slot.sse_lock:
            dead = []
            for cid, q in slot.sse_queues.items():
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    dead.append(cid)
                except Exception:
                    dead.append(cid)
            for cid in dead:
                slot.sse_queues.pop(cid, None)
    else:
        # node_0 / legacy path — use g.sse_queues (shared with slot_routes.py)
        async with g.sse_queues_lock:
            dead = []
            for cid, q in g.sse_queues.items():
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    dead.append(cid)
                except Exception:
                    dead.append(cid)
            for cid in dead:
                g.sse_queues.pop(cid, None)
                logger.debug("Removed slow/dead SSE client %s", cid)

    # Always fan out to all-mode subscribers (g.all_sse_queues shared with slot_routes.py)
    async with g.all_sse_queues_lock:
        dead = []
        for cid, q in g.all_sse_queues.items():
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(cid)
            except Exception:
                dead.append(cid)
        for cid in dead:
            g.all_sse_queues.pop(cid, None)


async def broadcast_stats():
    await broadcast_data({"event": "stats", "data": g.meshtastic_data.get_serializable_stats()}, slot_id="node_0")


async def broadcast_stats_for_slot(slot: "NodeSlot"):
    await broadcast_data(
        {"event": "stats", "data": slot.g.meshtastic_data.get_serializable_stats()},
        slot_id=slot.slot_id,
    )


def _resolve_slot_id_for_interface(interface) -> str:
    for slot in g.NODE_REGISTRY.values():
        if slot.g.connection_manager and slot.g.connection_manager.interface is interface:
            return slot.slot_id
    return "node_0"


