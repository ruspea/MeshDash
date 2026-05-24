"""
Node Ignore Plugin — v1.0.0
=============================
Persistent per-node ignore list. Ignored nodes are hidden from all UI views
(node grid, map, DMs, analytics) without deleting their data.

Mechanism:
  • Stores {node_id, slot_id} in plugin SQLite DB — survives restarts.
  • On init and on every ignore/unignore, calls slot.meshtastic_data.update_node()
    with {"_ni_ignored": True/False, "broadcast": True}.
  • This stamps the flag into the in-memory node dict AND broadcasts a
    node_update SSE event so every connected client updates immediately.
  • The bridge.html intercepts meshState.nodes reads and SSE node_update
    events to filter ignored nodes from ALL frontend rendering.

The node continues to receive packets and relay on the mesh — this is
purely a server-side UI visibility preference.

Optionally can also push the ignore to the radio via node.setIgnored()
(limited to 8 entries on the radio; the server-side list is unlimited).
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger        = logging.getLogger("plugin.node_ignore")
plugin_router = APIRouter()

import os
_DB_PATH = os.path.join(os.path.dirname(__file__), "node_ignore.db")
_DB_LOCK = threading.Lock()

_node_registry: Dict[str, Any] = {}
_event_loop:    Optional[asyncio.AbstractEventLoop] = None

# DB

def _db_init():
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ignored_nodes (
                node_id    TEXT NOT NULL,
                slot_id    TEXT NOT NULL DEFAULT 'node_0',
                notes      TEXT DEFAULT '',
                ignored_at REAL,
                PRIMARY KEY (node_id, slot_id)
            );
        """)
        conn.commit()
        conn.close()


def _db_get(slot_id: str) -> List[dict]:
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        rows = conn.execute(
            "SELECT node_id, slot_id, notes, ignored_at FROM ignored_nodes WHERE slot_id=? ORDER BY ignored_at DESC",
            (slot_id,)
        ).fetchall()
        conn.close()
    return [dict(zip(["node_id", "slot_id", "notes", "ignored_at"], r)) for r in rows]


def _db_get_all() -> List[dict]:
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        rows = conn.execute(
            "SELECT node_id, slot_id, notes, ignored_at FROM ignored_nodes ORDER BY ignored_at DESC"
        ).fetchall()
        conn.close()
    return [dict(zip(["node_id", "slot_id", "notes", "ignored_at"], r)) for r in rows]


def _db_set(node_id: str, slot_id: str, notes: str = ""):
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute(
            "INSERT OR REPLACE INTO ignored_nodes (node_id, slot_id, notes, ignored_at) VALUES (?,?,?,?)",
            (node_id, slot_id, notes, time.time())
        )
        conn.commit()
        conn.close()


def _db_remove(node_id: str, slot_id: str):
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute("DELETE FROM ignored_nodes WHERE node_id=? AND slot_id=?", (node_id, slot_id))
        conn.commit()
        conn.close()


def _db_is_ignored(node_id: str, slot_id: str) -> bool:
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        row = conn.execute(
            "SELECT 1 FROM ignored_nodes WHERE node_id=? AND slot_id=?", (node_id, slot_id)
        ).fetchone()
        conn.close()
    return row is not None


# Core: stamp _ni_ignored flag into live node data

def _stamp_node(node_id: str, slot_id: str, ignored: bool):
    """
    Merge _ni_ignored flag into the live in-memory node dict and broadcast
    a node_update SSE event. This propagates to all connected clients
    immediately without any frontend polling.
    """
    slot = _node_registry.get(slot_id)
    if not slot:
        return
    md = getattr(slot, "meshtastic_data", None)
    if not md or not hasattr(md, "update_node"):
        return
    try:
        md.update_node(node_id, {"_ni_ignored": ignored}, broadcast=True)
        logger.debug("Stamped _ni_ignored=%s on %s [%s]", ignored, node_id, slot_id)
    except Exception as e:
        logger.warning("stamp_node %s: %s", node_id, e)


def _stamp_all(slot_id: str, ignored_ids: List[str]):
    """Stamp all currently-ignored nodes at startup (no broadcast needed — clients
    aren't connected yet). Called from init_plugin."""
    slot = _node_registry.get(slot_id)
    if not slot:
        return
    md = getattr(slot, "meshtastic_data", None)
    if not md or not hasattr(md, "update_node"):
        return
    for nid in ignored_ids:
        try:
            # broadcast=False at startup — SSE clients aren't connected yet
            md.update_node(nid, {"_ni_ignored": True}, broadcast=False)
        except Exception as e:
            logger.debug("stamp_all %s: %s", nid, e)
    if ignored_ids:
        logger.info("Node Ignore: stamped %d ignored node(s) in slot '%s'", len(ignored_ids), slot_id)


# Plugin lifecycle

def init_plugin(context: dict):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    _db_init()

    # Stamp all ignored nodes into live in-memory data at startup
    all_ignored = _db_get_all()
    by_slot: Dict[str, List[str]] = {}
    for row in all_ignored:
        by_slot.setdefault(row["slot_id"], []).append(row["node_id"])
    for sid, nids in by_slot.items():
        _stamp_all(sid, nids)

    logger.info("Node Ignore v1.0.0 — %d ignored node(s) across %d slot(s)",
                len(all_ignored), len(by_slot))

    if _event_loop:
        asyncio.run_coroutine_threadsafe(_watchdog(context), _event_loop)


async def _watchdog(context):
    wd, pid = context.get("plugin_watchdog"), context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception:
            pass


# Routes

@plugin_router.get("")
@plugin_router.get("/")
async def health():
    total = len(_db_get_all())
    return {"plugin": "node_ignore", "version": "1.0.0", "status": "running",
            "total_ignored": total}



@plugin_router.get("/list")
async def list_ignored(slot_id: str = "node_0"):
    rows = _db_get(slot_id)
    # Enrich with live node data (name etc)
    slot = _node_registry.get(slot_id)
    md   = getattr(slot, "meshtastic_data", None) if slot else None
    for row in rows:
        node_data = (md.nodes.get(row["node_id"]) if md else None) or {}
        u = node_data.get("user") or {}
        row["long_name"]  = u.get("longName")  or node_data.get("long_name")  or row["node_id"]
        row["short_name"] = u.get("shortName") or node_data.get("short_name") or row["node_id"][-4:]
        row["hw_model"]   = node_data.get("hw_model") or ""
        row["last_heard"] = node_data.get("lastHeard") or 0
    return {"ignored": rows, "count": len(rows)}


@plugin_router.get("/list/all")
async def list_all_ignored():
    rows = _db_get_all()
    return {"ignored": rows, "count": len(rows)}



@plugin_router.get("/check")
async def check_ignored(node_id: str, slot_id: str = "node_0"):
    return {"node_id": node_id, "slot_id": slot_id,
            "ignored": _db_is_ignored(node_id, slot_id)}



class IgnoreReq(BaseModel):
    node_id: str
    slot_id: str = "node_0"
    notes:   str = ""


@plugin_router.post("/ignore")
async def ignore_node(r: IgnoreReq):
    if not r.node_id:
        raise HTTPException(400, "node_id required")

    # Prevent ignoring local node
    slot = _node_registry.get(r.slot_id)
    if slot:
        local_id = getattr(slot.meshtastic_data, "local_node_id", None)
        if local_id and r.node_id == local_id:
            raise HTTPException(400, "Cannot ignore the local node")

    _db_set(r.node_id, r.slot_id, r.notes)

    # Stamp into live data and broadcast SSE immediately
    if _event_loop:
        asyncio.run_coroutine_threadsafe(
            _async_stamp(r.node_id, r.slot_id, True), _event_loop
        )
    else:
        _stamp_node(r.node_id, r.slot_id, True)

    logger.info("Node Ignore: ignored %s [%s] — '%s'", r.node_id, r.slot_id, r.notes)
    return {"status": "ignored", "node_id": r.node_id, "slot_id": r.slot_id}


async def _async_stamp(node_id: str, slot_id: str, ignored: bool):
    _stamp_node(node_id, slot_id, ignored)


@plugin_router.post("/unignore")
async def unignore_node(r: IgnoreReq):
    if not r.node_id:
        raise HTTPException(400, "node_id required")

    _db_remove(r.node_id, r.slot_id)

    if _event_loop:
        asyncio.run_coroutine_threadsafe(
            _async_stamp(r.node_id, r.slot_id, False), _event_loop
        )
    else:
        _stamp_node(r.node_id, r.slot_id, False)

    logger.info("Node Ignore: unignored %s [%s]", r.node_id, r.slot_id)
    return {"status": "unignored", "node_id": r.node_id, "slot_id": r.slot_id}



class BulkReq(BaseModel):
    node_ids: List[str]
    slot_id:  str = "node_0"
    notes:    str = ""


@plugin_router.post("/ignore/bulk")
async def bulk_ignore(r: BulkReq):
    slot = _node_registry.get(r.slot_id)
    local_id = getattr(getattr(slot, "meshtastic_data", None), "local_node_id", None) if slot else None

    stamped = []
    for nid in r.node_ids:
        if local_id and nid == local_id:
            continue
        _db_set(nid, r.slot_id, r.notes)
        _stamp_node(nid, r.slot_id, True)
        stamped.append(nid)

    return {"status": "ok", "ignored": len(stamped)}


@plugin_router.post("/unignore/all")
async def unignore_all(slot_id: str = "node_0"):
    rows = _db_get(slot_id)
    for row in rows:
        _db_remove(row["node_id"], slot_id)
        _stamp_node(row["node_id"], slot_id, False)
    return {"status": "ok", "unignored": len(rows)}



@plugin_router.get("/nodes/{slot_id}")
async def list_nodes(slot_id: str):
    slot = _node_registry.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    local_id = getattr(slot.meshtastic_data, "local_node_id", None)
    nodes = []
    for nid, nd in slot.meshtastic_data.nodes.items():
        u = nd.get("user") or {}
        nodes.append({
            "node_id":    nid,
            "long_name":  u.get("longName")  or nd.get("long_name")  or nid,
            "short_name": u.get("shortName") or nd.get("short_name") or nid[-4:],
            "last_heard": nd.get("lastHeard") or nd.get("last_heard") or 0,
            "is_local":   nid == local_id,
            "ignored":    nd.get("_ni_ignored", False),
        })
    nodes.sort(key=lambda n: -(n["last_heard"] or 0))
    return {"slot_id": slot_id, "nodes": nodes}
