from core.c2 import send_system_message
import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
import logging
from fastapi import APIRouter, Depends, status
from core.routes.schemas import User
from core.auth import verify_csrf, get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter()
@router.get("/api/packets")
async def api_packets(limit: int = 50, slot_id: str = "node_0"):
    if slot_id == "all":
        all_pkts = []
        for s in g.NODE_REGISTRY.values():
            pkts = s.g.meshtastic_data.get_formatted_packets_from_memory(limit)
            all_pkts.extend(pkts)
        all_pkts.sort(key=lambda p: p.get("timestamp", 0) or 0, reverse=True)
        return all_pkts[:limit]
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _md = _slot.g.meshtastic_data if _slot else g.meshtastic_data
    return _md.get_formatted_packets_from_memory(limit)


@router.get("/api/packets/history")
async def api_pkt_hist(limit: int = 100, slot_id: str = "node_0"):
    if slot_id == "all":
        all_pkts = []
        for s in g.NODE_REGISTRY.values():
            rows = await asyncio.to_thread(s.g.db_manager.get_recent_packets, limit)
            all_pkts.extend(rows)
        all_pkts.sort(key=lambda p: p.get("timestamp", 0) or 0, reverse=True)
        return all_pkts[:limit]
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _db = _slot.g.db_manager if _slot else g.db_manager
    return await asyncio.to_thread(_db.get_recent_packets, limit)


@router.get("/api/search")
async def api_global_search(q: str, limit: int = 50, slot_id: str = "node_0", user: User = Depends(get_current_active_user)):
    if not q or len(q.strip()) < 2:
        return []
    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _db = _slot.g.db_manager if _slot else g.db_manager
    return await asyncio.to_thread(_db.global_search, q.strip(), limit)


@router.post("/api/alert")
async def trigger_alert(msg: str):
    await send_system_message(f"ALERT: {msg}")
    return {"status": "Alert broadcasted"}



