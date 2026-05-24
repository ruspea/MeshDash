"""
from __future__ import annotations
Cold Nodes Plugin — Backend API v2.2

Node dict shape (post meshtastic_dashboard.py fix):
  All nodes in meshtastic_data.nodes use camelCase regardless of whether
  they were loaded from the DB at startup or received live via SSE.
  get_all_nodes() now normalises last_heard→lastHeard and is_local→isLocal.

  Key fields this plugin reads:
    node["lastHeard"]                     — unix timestamp (0 or absent = never)
    node["isLocal"]                        — bool
    node["user"]["shortName"]              — display name
    node["user"]["longName"]               — full name
    node["user"]["hwModel"]                — hardware model string
    node["deviceMetrics"]["batteryLevel"]  — int %
    node["snr"]                            — float dB
    node["rssi"]                           — int dBm
    node["position"]["latitude"]           — float
    node["position"]["longitude"]          — float
    node["heard_by_slot"]                  — str (injected by plugin, not core)

Slot awareness:
  NODE_REGISTRY contains one NodeSlot per connected radio.
  Each slot has its own meshtastic_data.nodes (separate DB per slot).
  When slot_id="all" we merge all slots; most-recently-heard copy wins.
  Delete always operates on ALL slots' memory and DBs regardless of
  which slot the node was last heard on.
"""

import os
import json
import sqlite3
import time
import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

# User type — lazy import to avoid circular import when plugin loads at startup
def _get_user_type():
    """Lazily resolve the User type from meshtastic_dashboard when needed."""
    try:
        from meshtastic_dashboard import User
        return User
    except (ImportError, AttributeError):
        from typing import TypedDict
        class User(TypedDict):
            username: str
            role: int
            disabled: bool
        return User

logger = logging.getLogger("plugin.cold_nodes")
plugin_router = APIRouter()

# Persistence — SQLite single-row config store, lives next to main.py
_DB_PATH = os.path.join(os.path.dirname(__file__), "cold_nodes_config.db")
_DB_LOCK = threading.Lock()


def _db_load_config() -> dict:
    """Load persisted config from SQLite. Returns defaults if not yet written."""
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.commit()
            row = conn.execute(
                "SELECT value FROM config WHERE key='settings'"
            ).fetchone()
            conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.warning("Cold Nodes: failed to load persisted config: %s", e)
    return {}


def _db_save_config(cfg: dict) -> None:
    """Persist config dict to SQLite."""
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES ('settings', ?)",
                (json.dumps(cfg),),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Cold Nodes: failed to persist config: %s", e)

# Globals injected by init_plugin
_node_registry: Dict[str, Any] = {}
_meshtastic_data = None
_db_manager = None

# In-process config — shared between HTTP handlers (thread-safe via lock)
_config: Dict[str, Any] = {
    "enabled":         False,
    "threshold_hours": 24.0,
}
_config_lock = threading.Lock()


def init_plugin(context: dict) -> None:
    global _node_registry, _meshtastic_data, _db_manager
    _node_registry   = context.get("node_registry") or {}
    _meshtastic_data = context.get("meshtastic_data")
    _db_manager      = context.get("db_manager")

    # Restore persisted config so enabled/threshold survive restarts
    saved = _db_load_config()
    if saved:
        with _config_lock:
            _config.update({k: v for k, v in saved.items() if k in _config})
        logger.info(
            "Cold Nodes: loaded persisted config — enabled=%s threshold=%.1fh",
            _config["enabled"], _config["threshold_hours"],
        )

    logger.info("Cold Nodes plugin v2.2 initialised (%d slot(s))", len(_node_registry))

    # Launch watchdog heartbeat on the main event loop.
    # init_plugin runs inside a threading.Thread, so run_coroutine_threadsafe
    # is the only safe way to schedule a coroutine onto the running loop.
    loop = context.get("event_loop")
    if loop is None:
        logger.warning("Cold Nodes: event_loop not in context — watchdog will not start.")
        return
    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("Cold Nodes watchdog heartbeat started.")
    except Exception as e:
        logger.error("Cold Nodes: could not start watchdog heartbeat: %s", e)


# Watchdog heartbeat

async def _watchdog_heartbeat(context: dict) -> None:
    """
    Pings the MeshDash core watchdog every 30 s.
    Required because manifest.json has "watchdog": true.
    Without this the core marks the plugin as 'hung' after 120 s of silence.
    """
    wd  = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("Cold Nodes watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("Cold Nodes watchdog error: %s", e)


# Field accessors — single source of truth for reading node dicts
# After the meshtastic_dashboard.py fix, all in-memory nodes use camelCase.
# We still include snake_case fallbacks here as defensive safety nets in case
# the plugin is deployed against an older unpatched dashboard version.

def _last_heard(node: dict) -> float:
    """
    Return lastHeard as a float unix timestamp.
    Returns 0.0 if never heard.
    Checks camelCase first (normalised), snake_case as fallback (unpatched).
    """
    v = node.get("lastHeard") or node.get("last_heard")
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_local(node: dict) -> bool:
    """True if this node is one of our own radios."""
    return bool(node.get("isLocal") or node.get("is_local"))


def _short_name(node: dict) -> str:
    user = node.get("user") or {}
    return (user.get("shortName") or node.get("short_name") or "?").strip() or "?"


def _long_name(node: dict) -> str:
    user = node.get("user") or {}
    return (user.get("longName") or node.get("long_name") or "Unknown").strip() or "Unknown"


def _hw_model(node: dict) -> str:
    """
    Hardware model in two possible locations:
      - node["user"]["hwModel"]  — from live NodeInfo packet / background sync
      - node["hw_model"]         — top-level DB column (still snake_case, not renamed)
    Strip enum prefix: "HardwareModel.TBEAM" → "TBEAM", "UNSET" → "".
    """
    user = node.get("user") or {}
    raw = (user.get("hwModel") or node.get("hw_model") or "").strip()
    if "." in raw:
        raw = raw.split(".")[-1]
    if raw.upper() in ("UNSET", "UNKNOWN", "0", ""):
        return ""
    return raw


def _battery(node: dict) -> Optional[int]:
    """Battery % — in deviceMetrics for live nodes, battery_level column for DB."""
    dm = node.get("deviceMetrics") or {}
    bat = dm.get("batteryLevel")
    if bat is not None:
        try:
            return int(bat)
        except (TypeError, ValueError):
            pass
    bat = node.get("battery_level")
    if bat is not None:
        try:
            return int(bat)
        except (TypeError, ValueError):
            pass
    return None


def _snr(node: dict) -> Optional[float]:
    v = node.get("snr")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _rssi(node: dict) -> Optional[int]:
    v = node.get("rssi")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _lat_lon(node: dict) -> tuple:
    """Return (lat, lon) floats or (None, None)."""
    pos = node.get("position") or {}
    lat = pos.get("latitude") or node.get("latitude")
    lon = pos.get("longitude") or node.get("longitude")
    try:
        lat = float(lat) if lat else None
        lon = float(lon) if lon else None
    except (TypeError, ValueError):
        lat = lon = None
    return lat, lon


def _age_hours(lh: float) -> Optional[float]:
    if not lh:
        return None
    return round((time.time() - lh) / 3600, 2)


# Slot / node collection helpers

def _get_local_node_ids() -> set:
    """Collect all local radio node IDs across all slots."""
    ids: set = set()
    for slot in _node_registry.values():
        try:
            lid = slot.meshtastic_data.local_node_id
            if lid:
                ids.add(lid)
        except Exception:
            pass
    return ids


def _collect_nodes(slot_id: str) -> Dict[str, Dict]:
    """
    Return a merged dict of all nodes visible from the requested slot(s).

    slot_id="all" → merge every slot in NODE_REGISTRY.
    slot_id="node_0" etc → only that slot's meshtastic_data.nodes.

    When the same node_id appears in multiple slots the copy with the
    most recent lastHeard wins, so we always show the freshest data.
    The "heard_by_slot" key records which slot last heard the node.
    """
    if slot_id == "all":
        target_slots = list(_node_registry.items())
    else:
        slot = _node_registry.get(slot_id)
        target_slots = [(slot_id, slot)] if slot else []

    if not target_slots:
        logger.warning("Cold Nodes: slot '%s' not found in NODE_REGISTRY", slot_id)

    merged: Dict[str, Dict] = {}
    for sid, slot in target_slots:
        if slot is None:
            continue
        try:
            nodes = slot.meshtastic_data.nodes
        except Exception as e:
            logger.warning("Cold Nodes: cannot read nodes from slot '%s': %s", sid, e)
            continue

        for nid, ndata in nodes.items():
            node = dict(ndata)  # shallow copy — don't mutate the live dict
            node["heard_by_slot"] = sid
            existing = merged.get(nid)
            if existing is None:
                merged[nid] = node
            else:
                # Keep the copy with the more recent lastHeard
                if _last_heard(node) > _last_heard(existing):
                    merged[nid] = node

    return merged


# Pydantic models

class ConfigModel(BaseModel):
    enabled:         bool  = False
    threshold_hours: float = Field(24.0, gt=0, le=8760)   # 0.5h – 1 year


class DeleteRequest(BaseModel):
    node_ids: List[str]
    slot_id:  str = "node_0"   # informational — delete always hits all slots


# API routes

@plugin_router.get("/config")
async def get_config():
    with _config_lock:
        return dict(_config)


@plugin_router.post("/config")
async def set_config(body: ConfigModel):
    with _config_lock:
        _config["enabled"]         = body.enabled
        _config["threshold_hours"] = body.threshold_hours
        cfg_snapshot = dict(_config)
    # Persist to disk so the setting survives restarts
    await asyncio.to_thread(_db_save_config, cfg_snapshot)
    logger.info(
        "Cold Nodes config updated and persisted: enabled=%s threshold=%.1fh",
        body.enabled, body.threshold_hours,
    )
    return {"status": "ok", **cfg_snapshot}


@plugin_router.get("/nodes")
async def get_cold_nodes(
    threshold_hours: float = 24.0,
    slot_id:         str   = "all",
):
    """
    Return all nodes not heard within threshold_hours.
    slot_id controls which slot(s) to scan:
      "all"    → all connected radios (default, recommended)
      "node_0" → primary radio only
      etc.

    "Never heard" nodes (lastHeard = 0) are always included and sorted last
    so actionable stale nodes appear first.
    """
    if threshold_hours <= 0:
        raise HTTPException(status_code=400, detail="threshold_hours must be > 0")

    now        = time.time()
    cutoff     = now - (threshold_hours * 3600)
    nodes      = _collect_nodes(slot_id)
    local_ids  = _get_local_node_ids()
    cold: List[Dict] = []

    for nid, ndata in nodes.items():
        # Skip our own radios
        if nid in local_ids:
            continue
        if _is_local(ndata):
            continue

        lh = _last_heard(ndata)

        # Include if: never heard OR heard before the cutoff window
        if lh != 0.0 and lh >= cutoff:
            continue

        lat, lon = _lat_lon(ndata)
        cold.append({
            "node_id":       nid,
            "short_name":    _short_name(ndata),
            "long_name":     _long_name(ndata),
            "hw_model":      _hw_model(ndata),
            "last_heard":    lh,           # 0.0 = never heard
            "age_hours":     _age_hours(lh),
            "heard_by_slot": ndata.get("heard_by_slot") or "node_0",
            "snr":           _snr(ndata),
            "rssi":          _rssi(ndata),
            "battery_level": _battery(ndata),
            "latitude":      lat,
            "longitude":     lon,
        })

    # Sort: known-stale oldest first, never-heard last
    # last_heard=0 → sort key = infinity so they go to the end
    cold.sort(key=lambda n: n["last_heard"] if n["last_heard"] > 0 else float("inf"))

    total_known = len(nodes)
    return {
        "threshold_hours": threshold_hours,
        "slot_id":         slot_id,
        "total_nodes":     total_known,
        "count":           len(cold),
        "nodes":           cold,
    }


def _require_admin_or_operator(user: "User"):
    """Guard: raise 403 if user is not admin (0) or operator (1)."""
    if isinstance(user, RedirectResponse):
        return user
    if user.role not in (0, 1):
        raise HTTPException(403, "Admin or Operator access required.")


@plugin_router.post("/delete")
async def delete_nodes(
    req: DeleteRequest,
    user: "User" = Depends(_require_admin_or_operator),
):
    """
    Permanently delete nodes from ALL slots' memory and databases.
    The slot_id parameter is accepted for API compatibility but deletion
    always runs across every registered slot — a node should not persist
    in one slot's DB after being deleted from another's.
    """
    if not req.node_ids:
        raise HTTPException(status_code=400, detail="node_ids list is empty")

    # Safety: never delete local radio nodes
    local_ids = _get_local_node_ids()
    blocked   = [nid for nid in req.node_ids if nid in local_ids]
    if blocked:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete local node(s): {blocked}",
        )

    results: List[Dict] = []

    for nid in req.node_ids:
        result: Dict = {
            "node_id":              nid,
            "status":               "ok",
            "error":                None,
            "removed_from_memory":  False,
            "removed_from_db":      False,
        }
        try:
            for sid, slot in _node_registry.items():
                try:
                    if nid in slot.meshtastic_data.nodes:
                        del slot.meshtastic_data.nodes[nid]
                        result["removed_from_memory"] = True
                        logger.info(
                            "Cold Nodes: removed %s from memory [slot=%s]", nid, sid
                        )
                except Exception as e:
                    logger.warning(
                        "Cold Nodes: memory remove error for %s [slot=%s]: %s", nid, sid, e
                    )

            for sid, slot in _node_registry.items():
                try:
                    db = slot.db_manager

                    def _db_del(d=db, n=nid):
                        conn = d._get_connection()
                        cur  = conn.execute("DELETE FROM nodes WHERE node_id = ?", (n,))
                        conn.commit()
                        return cur.rowcount

                    rows = await asyncio.to_thread(_db_del)
                    if rows:
                        result["removed_from_db"] = True
                        logger.info(
                            "Cold Nodes: deleted %s from DB [slot=%s] (%d row)", nid, sid, rows
                        )
                except Exception as e:
                    logger.warning(
                        "Cold Nodes: DB delete error for %s [slot=%s]: %s", nid, sid, e
                    )

            if not result["removed_from_memory"] and not result["removed_from_db"]:
                result["status"] = "not_found"

        except Exception as e:
            logger.error("Cold Nodes: unexpected error deleting %s: %s", nid, e, exc_info=True)
            result["status"] = "error"
            result["error"]  = str(e)

        results.append(result)

    deleted = sum(1 for r in results if r["status"] == "ok")
    return {
        "deleted": deleted,
        "total":   len(req.node_ids),
        "results": results,
    }


@plugin_router.get("/slots")
async def list_slots():
    """Return metadata for all registered radio slots."""
    slots = []
    for slot_id, slot in _node_registry.items():
        try:
            slots.append({
                "slot_id":    slot_id,
                "label":      getattr(slot, "label", slot_id),
                "node_count": len(slot.meshtastic_data.nodes),
                "is_ready":   slot.connection_manager.is_ready.is_set()
                              if slot.connection_manager else False,
            })
        except Exception:
            slots.append({
                "slot_id":    slot_id,
                "label":      slot_id,
                "node_count": 0,
                "is_ready":   False,
            })
    return {"slots": slots}