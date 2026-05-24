"""
ISS Tracker Plugin for MeshDash
===============================
• Fetch ISS location and "people in space" from open-notify.org
• Self-contained schedule engine: alerts when ISS is within X distance
• Trigger types: recurring / once
• Non-blocking: asyncio background task polls and fires jobs dynamically
• SQLite-backed schedule storage (iss_schedules.db)
• Full REST CRUD: list / create / update / delete / pause schedules
• Endpoint for frontend mapping: /iss-location
• Watchdog heartbeat: pings MeshDash core every 30s (manifest: "watchdog": true)
"""

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Body, HTTPException, Query

# Plugin registry
core_context: dict = {}
plugin_router = APIRouter()

# Database path – self-contained
_DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iss_schedules.db")
_db_conn: Optional[sqlite3.Connection] = None
_scheduler_task: Optional[asyncio.Task] = None
_watchdog_task:  Optional[asyncio.Task] = None

# Cache to respect Open-Notify's 5-second polling limits
_iss_cache = {"data": None, "time": 0.0}

# Math & Distance Logic

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates the great-circle distance between two points in km."""
    R = 6371.0  # Earth radius in kilometers
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

# DB helpers

def _get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10.0)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL;")
        _db_conn.execute("PRAGMA synchronous=NORMAL;")
        _init_schema(_db_conn)
    return _db_conn


def _init_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          TEXT PRIMARY KEY,
            label       TEXT NOT NULL DEFAULT '',
            lat         REAL NOT NULL,
            lon         REAL NOT NULL,
            distance_km REAL NOT NULL DEFAULT 1000.0,
            dest_type   TEXT NOT NULL DEFAULT 'broadcast',
            channel     INTEGER NOT NULL DEFAULT 0,
            node_id     TEXT NOT NULL DEFAULT '',
            sched_type  TEXT NOT NULL,   -- 'recurring' | 'once'
            enabled     INTEGER NOT NULL DEFAULT 1,
            last_run    REAL,
            run_count   INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        )
    """)
    conn.commit()


def _row_to_dict(row) -> dict:
    return dict(row)


def _all_schedules() -> list:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_schedule(sid: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    return _row_to_dict(row) if row else None


def _upsert_schedule(s: dict):
    conn = _get_db()
    conn.execute("""
        INSERT INTO schedules
            (id,label,lat,lon,distance_km,dest_type,channel,node_id,sched_type,
             enabled,last_run,run_count,created_at,updated_at)
        VALUES
            (:id,:label,:lat,:lon,:distance_km,:dest_type,:channel,:node_id,:sched_type,
             :enabled,:last_run,:run_count,:created_at,:updated_at)
        ON CONFLICT(id) DO UPDATE SET
            label=excluded.label, lat=excluded.lat, lon=excluded.lon,
            distance_km=excluded.distance_km, dest_type=excluded.dest_type,
            channel=excluded.channel, node_id=excluded.node_id,
            sched_type=excluded.sched_type, enabled=excluded.enabled,
            last_run=excluded.last_run, run_count=excluded.run_count,
            updated_at=excluded.updated_at
    """, s)
    conn.commit()


def _delete_schedule(sid: str):
    conn = _get_db()
    conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
    conn.commit()

# Core ISS API Fetch

async def fetch_iss_data() -> dict:
    """Fetches location and crew count. Caches for 5s to avoid API limits."""
    now = time.time()
    if _iss_cache["data"] and (now - _iss_cache["time"] < 5.0):
        return _iss_cache["data"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        r_pos = await client.get("http://api.open-notify.org/iss-now.json")
        r_pos.raise_for_status()
        pos_data = r_pos.json()

        r_ast = await client.get("http://api.open-notify.org/astros.json")
        r_ast.raise_for_status()
        ast_data = r_ast.json()

    people = ast_data.get("people", [])
    count = ast_data.get("number", 0)

    data = {
        "lat": float(pos_data["iss_position"]["latitude"]),
        "lon": float(pos_data["iss_position"]["longitude"]),
        "timestamp": pos_data["timestamp"],
        "people_count": count,
        "people": people
    }
    
    _iss_cache["data"] = data
    _iss_cache["time"] = now
    return data


async def _do_iss_send(user_lat: float, user_lon: float, dest_type: str,
                       channel: int, node_id: str) -> str:
    """Builds and dispatches the message to MeshDash core."""
    cm = core_context.get("connection_manager")
    if not cm or not cm.is_ready.is_set():
        raise RuntimeError("Radio not ready")

    iss_data = await fetch_iss_data()
    dist = haversine(user_lat, user_lon, iss_data["lat"], iss_data["lon"])
    
    names = ", ".join([p["name"] for p in iss_data["people"]])
    count = iss_data["people_count"]

    msg = (
        f"🚀 ISS Tracker: The ISS is currently {dist:.1f} km away! "
        f"There are {count} people in space right now: {names}."
    )

    # Fallback to shorter text if node restrictions are aggressive
    # Use byte-length check for Meshtastic's 230-byte limit (emoji are multi-byte)
    if len(msg.encode('utf-8')) > 220:
        msg = f"🚀 ISS is {dist:.1f} km away! {count} people currently in space."

    if dest_type == "direct" and node_id:
        await cm.sendText(msg, destinationId=node_id.strip(), channelIndex=0)
    else:
        await cm.sendText(msg, destinationId="^all", channelIndex=channel)

    return msg

# Watchdog heartbeat

async def _watchdog_heartbeat():
    logger = core_context.get("logger") or logging.getLogger("iss_plugin")
    while True:
        try:
            await asyncio.sleep(30)
            wd  = core_context.get("plugin_watchdog")
            pid = core_context.get("plugin_id")
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("🛑 ISS watchdog heartbeat stopped")
            return
        except Exception as e:
            logger.warning(f"⚠️ Watchdog heartbeat error: {e}")

# Scheduler worker (Distance Threshold Poller)

async def _scheduler_worker():
    """
    Runs forever. Wakes every 30s, checks ISS distance against all
    active schedules. Fires if the ISS enters the distance radius.
    """
    logger = core_context.get("logger") or logging.getLogger("iss_plugin")
    logger.info("🕐 ISS distance scheduler worker started")

    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            schedules = await asyncio.to_thread(_all_schedules)

            if not any(s.get("enabled") for s in schedules):
                continue

            try:
                iss_data = await fetch_iss_data()
            except Exception as e:
                logger.warning(f"⚠️ Failed to fetch ISS data for schedules: {e}")
                continue

            iss_lat = iss_data["lat"]
            iss_lon = iss_data["lon"]

            for s in schedules:
                if not s.get("enabled"):
                    continue

                dist = haversine(s["lat"], s["lon"], iss_lat, iss_lon)
                threshold = s.get("distance_km", 1000.0)

                if dist <= threshold:
                    # Orbit cooldown: Prevent firing every 30s while overhead.
                    # The ISS orbits every ~90 mins, so a 1-hour cooldown is safe.
                    if s["sched_type"] == "recurring" and s.get("last_run"):
                        if now - s["last_run"] < 3600:
                            continue

                    logger.info(f"⏰ ISS entered '{s['label']}' radius (dist={dist:.1f}km)")
                    try:
                        msg = await _do_iss_send(
                            user_lat  = s["lat"],
                            user_lon  = s["lon"],
                            dest_type = s["dest_type"],
                            channel   = s["channel"],
                            node_id   = s["node_id"],
                        )
                        logger.info(f"✅ ISS alert sent: {msg[:60]}…")
                    except Exception as e:
                        logger.error(f"❌ Scheduled ISS send failed: {e}")

                    s["last_run"]   = now
                    s["run_count"]  = int(s.get("run_count") or 0) + 1
                    s["updated_at"] = now

                    if s["sched_type"] == "once":
                        s["enabled"] = 0

                    await asyncio.to_thread(_upsert_schedule, s)

        except asyncio.CancelledError:
            logger.info("🛑 ISS scheduler worker stopped")
            return
        except Exception as e:
            logger.error(f"❌ Scheduler worker error: {e}", exc_info=True)

# Plugin lifecycle

def init_plugin(context: dict):
    global _scheduler_task, _watchdog_task
    core_context.update(context)
    logger = core_context.get("logger") or logging.getLogger("iss_plugin")
    logger.info("✅ ISS Tracker plugin initialising…")

    _get_db()

    loop = core_context.get("event_loop")
    if loop is None:
        logger.warning("⚠️ event_loop not in context — tasks will not start")
        return

    try:
        _scheduler_task = asyncio.run_coroutine_threadsafe(_scheduler_worker(), loop)
        logger.info("✅ ISS scheduler task started")
    except Exception as e:
        logger.error(f"❌ Could not start scheduler task: {e}")

    try:
        _watchdog_task = asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(), loop)
        logger.info("🐕 ISS watchdog heartbeat started")
    except Exception as e:
        logger.error(f"❌ Could not start watchdog heartbeat: {e}")

# API Endpoints

@plugin_router.get("/iss-location")
async def get_iss_location():
    """Provides current ISS location for frontend map plotting."""
    try:
        data = await fetch_iss_data()
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch ISS location: {e}")


@plugin_router.get("/fetch")
async def fetch_iss_now(
    lat:         float         = Query(...),
    lon:         float         = Query(...),
    channel:     int           = Query(0),
    destination: Optional[str] = Query(None),
):
    """Triggers an on-demand send of the current ISS status."""
    cm = core_context.get("connection_manager")
    if not cm or not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready")
    try:
        dest_type = "direct" if (destination and destination.strip()) else "broadcast"
        msg = await _do_iss_send(
            user_lat=lat, user_lon=lon,
            dest_type=dest_type,
            channel=channel,
            node_id=destination or "",
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ISS API error: {e}")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"Send failed: {e}")

    return {
        "status":      "sent",
        "message":     msg,
        "channel":     channel,
        "destination": destination,
        "mode":        "direct" if destination else "broadcast",
    }


@plugin_router.get("/schedules")
async def list_schedules():
    schedules = await asyncio.to_thread(_all_schedules)
    return {"schedules": schedules}


@plugin_router.post("/schedules")
async def create_schedule(body: dict = Body(...)):
    required = ["lat", "lon", "sched_type"]
    for f in required:
        if f not in body:
            raise HTTPException(400, f"Missing field: {f}")

    # Coerce any legacy schedule types from old frontend to 'recurring'
    stype = body["sched_type"]
    if stype in ("daily", "weekly", "recurring"):
        stype = "recurring"
    elif stype == "once":
        stype = "once"
    else:
        raise HTTPException(400, "sched_type must be 'recurring' or 'once'")

    dist_km = body.get("distance_km")
    if dist_km is None:
        dist_km = 1000.0

    now = time.time()
    sid = str(uuid.uuid4())
    s = {
        "id":          sid,
        "label":       str(body.get("label") or "")[:80],
        "lat":         float(body["lat"]),
        "lon":         float(body["lon"]),
        "distance_km": float(dist_km),
        "dest_type":   body.get("dest_type", "broadcast"),
        "channel":     int(body.get("channel", 0)),
        "node_id":     str(body.get("node_id") or ""),
        "sched_type":  stype,
        "enabled":     1,
        "last_run":    None,
        "run_count":   0,
        "created_at":  now,
        "updated_at":  now,
    }

    await asyncio.to_thread(_upsert_schedule, s)
    return {"status": "created", "schedule": s}


@plugin_router.patch("/schedules/{sid}")
async def update_schedule(sid: str, body: dict = Body(...)):
    s = await asyncio.to_thread(_get_schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")

    allowed = ["label","lat","lon","distance_km","dest_type","channel","node_id",
               "sched_type","enabled"]
    for k in allowed:
        if k in body:
            s[k] = body[k]

    if s["sched_type"] in ("daily", "weekly"):
        s["sched_type"] = "recurring"

    s["updated_at"] = time.time()
    await asyncio.to_thread(_upsert_schedule, s)
    return {"status": "updated", "schedule": s}


@plugin_router.delete("/schedules/{sid}")
async def delete_schedule(sid: str):
    s = await asyncio.to_thread(_get_schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    await asyncio.to_thread(_delete_schedule, sid)
    return {"status": "deleted", "id": sid}


@plugin_router.post("/schedules/{sid}/toggle")
async def toggle_schedule(sid: str):
    s = await asyncio.to_thread(_get_schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s["enabled"]    = 0 if s["enabled"] else 1
    s["updated_at"] = time.time()
    await asyncio.to_thread(_upsert_schedule, s)
    return {"status": "toggled", "enabled": bool(s["enabled"]), "schedule": s}


@plugin_router.post("/schedules/{sid}/run-now")
async def run_schedule_now(sid: str):
    """Trigger an alert immediately (test / manual fire)."""
    s = await asyncio.to_thread(_get_schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    cm = core_context.get("connection_manager")
    if not cm or not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready")
    try:
        msg = await _do_iss_send(
            user_lat  = s["lat"],
            user_lon  = s["lon"],
            dest_type = s["dest_type"],
            channel   = s["channel"],
            node_id   = s["node_id"],
        )
    except Exception as e:
        raise HTTPException(500, f"Send failed: {e}")
    return {"status": "sent", "message": msg}