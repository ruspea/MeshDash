"""
Weather Plugin for MeshDash
============================
• Fetch weather from Open-Meteo and send to mesh (broadcast or DM)
• Self-contained schedule engine: daily / weekly / one-time
• Non-blocking: asyncio background task fires jobs at the right time
• SQLite-backed schedule storage (weather_schedules.db)
• Full REST CRUD: list / create / update / delete / pause schedules
• Watchdog heartbeat: pings MeshDash core every 30s (manifest: "watchdog": true)
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Body, HTTPException, Query

# ---------------------------------------------------------------------------
# Plugin registry
# ---------------------------------------------------------------------------
core_context: dict = {}
plugin_router = APIRouter()

# ---------------------------------------------------------------------------
# Database path – stored alongside the script so the plugin is self-contained
# ---------------------------------------------------------------------------
_DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_schedules.db")
_db_conn: Optional[sqlite3.Connection] = None
_scheduler_task: Optional[asyncio.Task] = None
_watchdog_task:  Optional[asyncio.Task] = None

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

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
            dest_type   TEXT NOT NULL DEFAULT 'broadcast',   -- 'broadcast' | 'direct'
            channel     INTEGER NOT NULL DEFAULT 0,
            node_id     TEXT NOT NULL DEFAULT '',
            sched_type  TEXT NOT NULL,   -- 'daily' | 'weekly' | 'once'
            hour        INTEGER NOT NULL,
            minute      INTEGER NOT NULL,
            days        TEXT NOT NULL DEFAULT '[]',  -- JSON list of 0-6 (Mon-Sun) for weekly
            run_at      REAL,            -- unix ts for one-time jobs
            units       TEXT NOT NULL DEFAULT 'metric',  -- 'metric' | 'imperial'
            enabled     INTEGER NOT NULL DEFAULT 1,
            last_run    REAL,
            next_run    REAL,
            run_count   INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        )
    """)
    # Migration: add units column if missing
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
        if "units" not in cols:
            conn.execute("ALTER TABLE schedules ADD COLUMN units TEXT NOT NULL DEFAULT 'metric'")
            conn.commit()
    except Exception:
        pass

    conn.commit()


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["days"] = json.loads(d.get("days") or "[]")
    except Exception:
        d["days"] = []
    return d


def _calc_next_run(sched: dict, after: float = None) -> Optional[float]:
    """Return the next UTC unix timestamp this schedule should fire, or None."""
    if after is None:
        after = time.time()
    stype = sched.get("sched_type")
    h = int(sched.get("hour", 0))
    m = int(sched.get("minute", 0))

    if stype == "once":
        run_at = sched.get("run_at")
        return float(run_at) if run_at and float(run_at) > after else None

    now_dt = datetime.fromtimestamp(after, tz=timezone.utc)
    candidate = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)

    if stype == "daily":
        if candidate.timestamp() <= after:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    if stype == "weekly":
        days = sched.get("days") or []
        if not days:
            return None
        days_set = set(int(d) for d in days)
        for delta in range(0, 8):
            check = candidate + timedelta(days=delta)
            if check.weekday() in days_set:
                if check.timestamp() > after:
                    return check.timestamp()
        return None

    return None


def _all_schedules() -> list:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM schedules ORDER BY next_run ASC NULLS LAST").fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_schedule(sid: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    return _row_to_dict(row) if row else None


def _upsert_schedule(s: dict):
    conn = _get_db()
    conn.execute("""
        INSERT INTO schedules
            (id,label,lat,lon,dest_type,channel,node_id,sched_type,hour,minute,days,
             run_at,units,enabled,last_run,next_run,run_count,created_at,updated_at)
        VALUES
            (:id,:label,:lat,:lon,:dest_type,:channel,:node_id,:sched_type,:hour,:minute,:days,
             :run_at,:units,:enabled,:last_run,:next_run,:run_count,:created_at,:updated_at)
        ON CONFLICT(id) DO UPDATE SET
            label=excluded.label, lat=excluded.lat, lon=excluded.lon,
            dest_type=excluded.dest_type, channel=excluded.channel, node_id=excluded.node_id,
            sched_type=excluded.sched_type, hour=excluded.hour, minute=excluded.minute,
            days=excluded.days, run_at=excluded.run_at, units=excluded.units,
            enabled=excluded.enabled,
            last_run=excluded.last_run, next_run=excluded.next_run,
            run_count=excluded.run_count, updated_at=excluded.updated_at
    """, {**s, "days": json.dumps(s.get("days") or [])})
    conn.commit()


def _delete_schedule(sid: str):
    conn = _get_db()
    conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
    conn.commit()

# ---------------------------------------------------------------------------
# Core weather fetch (shared between on-demand and scheduler)
# ---------------------------------------------------------------------------

async def _do_weather_send(lat: float, lon: float, dest_type: str,
                            channel: int, node_id: str,
                            units: str = "metric") -> str:
    """Fetch weather and send to mesh. Returns the sent message string."""
    cm = core_context.get("connection_manager")
    if not cm or not cm.is_ready.is_set():
        raise RuntimeError("Radio not ready")

    # Ask Open-Meteo to return values in the requested units
    temp_unit = "fahrenheit" if units == "imperial" else "celsius"
    wind_unit = "mph" if units == "imperial" else "kmh"

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        f"&temperature_unit={temp_unit}&wind_speed_unit={wind_unit}"
        "&timezone=auto"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    current = data.get("current") or {}
    if not current:
        raise RuntimeError("No current weather data returned")

    temp     = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    wind     = current.get("wind_speed_10m")
    code     = current.get("weather_code", 0)
    ts       = current.get("time", "")
    cond     = _wmo_description(code)

    if units == "imperial":
        temp_str = f"{temp}°F" if temp is not None else "N/A"
        wind_str = f"{wind} mph" if wind is not None else "N/A"
    else:
        temp_str = f"{temp}°C" if temp is not None else "N/A"
        wind_str = f"{wind} km/h" if wind is not None else "N/A"

    msg = (
        f"🌤️ Weather at {ts}: {temp_str}, {cond}, "
        f"humidity {humidity}%, wind {wind_str}"
    )

    # Meshtastic limit: 230 bytes
    msg_bytes = msg.encode('utf-8')
    if len(msg_bytes) > 230:
        msg = msg_bytes[:227].decode('utf-8', errors='ignore') + "..."

    if dest_type == "direct" and node_id:
        await cm.sendText(msg, destinationId=node_id.strip(), channelIndex=0)
    else:
        await cm.sendText(msg, destinationId="^all", channelIndex=channel)

    return msg


def _wmo_description(code: int) -> str:
    codes = {
        0:"clear sky",1:"mainly clear",2:"partly cloudy",3:"overcast",
        45:"fog",48:"rime fog",51:"light drizzle",53:"moderate drizzle",
        55:"dense drizzle",56:"freezing drizzle",57:"dense freezing drizzle",
        61:"slight rain",63:"moderate rain",65:"heavy rain",
        66:"freezing rain",67:"heavy freezing rain",
        71:"slight snow",73:"moderate snow",75:"heavy snow",77:"snow grains",
        80:"slight rain showers",81:"moderate rain showers",82:"violent rain showers",
        85:"slight snow showers",86:"heavy snow showers",
        95:"thunderstorm",96:"thunderstorm with hail",99:"heavy thunderstorm with hail",
    }
    return codes.get(code, "unknown")

# ---------------------------------------------------------------------------
# Watchdog heartbeat
# ---------------------------------------------------------------------------

async def _watchdog_heartbeat():
    """
    Pings the MeshDash core watchdog every 30s.
    Required because manifest.json has "watchdog": true.
    Without this the core will mark the plugin as 'hung' after 120s of silence.
    """
    logger = core_context.get("logger") or logging.getLogger("weather_plugin")
    while True:
        try:
            await asyncio.sleep(30)
            wd  = core_context.get("plugin_watchdog")
            pid = core_context.get("plugin_id")
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("🛑 Weather watchdog heartbeat stopped")
            return
        except Exception as e:
            logger.warning(f"⚠️ Watchdog heartbeat error: {e}")

# ---------------------------------------------------------------------------
# Scheduler worker
# ---------------------------------------------------------------------------

async def _scheduler_worker():
    """
    Runs forever as an asyncio Task.
    Wakes every 30s, checks for due schedules, fires them without blocking.
    """
    logger = core_context.get("logger") or logging.getLogger("weather_plugin")
    logger.info("🕐 Weather scheduler worker started")

    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            schedules = await asyncio.to_thread(_all_schedules)

            for s in schedules:
                if not s.get("enabled"):
                    continue
                next_run = s.get("next_run")
                if next_run is None or next_run > now:
                    continue

                logger.info(f"⏰ Weather schedule '{s['label']}' firing (id={s['id']})")
                try:
                    msg = await _do_weather_send(
                        lat       = s["lat"],
                        lon       = s["lon"],
                        dest_type = s["dest_type"],
                        channel   = s["channel"],
                        node_id   = s["node_id"],
                        units     = s.get("units", "metric"),
                    )
                    logger.info(f"✅ Scheduled weather sent: {msg[:60]}…")
                except Exception as e:
                    logger.error(f"❌ Scheduled weather send failed: {e}")

                s["last_run"]   = now
                s["run_count"]  = int(s.get("run_count") or 0) + 1
                s["updated_at"] = now

                if s["sched_type"] == "once":
                    s["enabled"]  = 0
                    s["next_run"] = None
                else:
                    s["next_run"] = _calc_next_run(s, after=now)

                await asyncio.to_thread(_upsert_schedule, s)

        except asyncio.CancelledError:
            logger.info("🛑 Weather scheduler worker stopped")
            return
        except Exception as e:
            logger.error(f"❌ Scheduler worker error: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Plugin lifecycle — single init_plugin
# ---------------------------------------------------------------------------

def init_plugin(context: dict):
    global _scheduler_task, _watchdog_task
    core_context.update(context)
    logger = core_context.get("logger") or logging.getLogger("weather_plugin")
    logger.info("✅ Weather plugin initialising…")

    # Ensure DB is ready
    _get_db()

    # Refresh next_run for all enabled schedules that have no next_run set
    try:
        for s in _all_schedules():
            if s.get("enabled") and not s.get("next_run") and s.get("sched_type") != "once":
                s["next_run"]   = _calc_next_run(s)
                s["updated_at"] = time.time()
                _upsert_schedule(s)
    except Exception as e:
        logger.warning(f"Could not refresh next_run on boot: {e}")

    # init_plugin is called from a threading.Thread by MeshDash core.
    # asyncio.get_event_loop() from a thread returns a different/closed loop.
    # The correct approach is run_coroutine_threadsafe() with the loop that
    # was passed in via context["event_loop"] (set in md.py lifespan).
    loop = core_context.get("event_loop")
    if loop is None:
        logger.warning("⚠️  event_loop not in context — scheduler and watchdog will not start")
        return

    try:
        fut = asyncio.run_coroutine_threadsafe(_scheduler_worker(), loop)
        logger.info("✅ Weather scheduler task started")
    except Exception as e:
        logger.error(f"❌ Could not start scheduler task: {e}")

    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(), loop)
        logger.info("🐕 Weather watchdog heartbeat started")
    except Exception as e:
        logger.error(f"❌ Could not start watchdog heartbeat: {e}")

# ---------------------------------------------------------------------------
# API — on-demand send
# ---------------------------------------------------------------------------

@plugin_router.get("/fetch")
async def fetch_weather(
    lat:         float         = Query(...),
    lon:         float         = Query(...),
    channel:     int           = Query(0),
    destination: Optional[str] = Query(None),
    units:       str           = Query("metric", regex="^(metric|imperial)$"),
):
    cm = core_context.get("connection_manager")
    if not cm or not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready")
    try:
        dest_type = "direct" if (destination and destination.strip()) else "broadcast"
        msg = await _do_weather_send(
            lat=lat, lon=lon,
            dest_type=dest_type,
            channel=channel,
            node_id=destination or "",
            units=units,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Weather API error: {e}")
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
        "units":       units,
    }

# ---------------------------------------------------------------------------
# API — schedule CRUD
# ---------------------------------------------------------------------------

@plugin_router.get("/schedules")
async def list_schedules():
    schedules = await asyncio.to_thread(_all_schedules)
    return {"schedules": schedules}


@plugin_router.post("/schedules")
async def create_schedule(body: dict = Body(...)):
    """
    Required fields:
        lat, lon, sched_type ('daily'|'weekly'|'once'), hour, minute
    Optional:
        label, dest_type ('broadcast'|'direct'), channel, node_id,
        days ([0-6] for weekly), run_at (unix ts for once),
        units ('metric'|'imperial', default 'metric')
    """
    required = ["lat", "lon", "sched_type", "hour", "minute"]
    for f in required:
        if f not in body:
            raise HTTPException(400, f"Missing field: {f}")

    stype = body["sched_type"]
    if stype not in ("daily", "weekly", "once"):
        raise HTTPException(400, "sched_type must be 'daily', 'weekly', or 'once'")
    if stype == "weekly" and not body.get("days"):
        raise HTTPException(400, "days[] required for weekly schedules")
    if stype == "once" and not body.get("run_at"):
        raise HTTPException(400, "run_at (unix timestamp) required for one-time schedules")

    now = time.time()
    sid = str(uuid.uuid4())
    s = {
        "id":         sid,
        "label":      str(body.get("label") or "")[:80],
        "lat":        float(body["lat"]),
        "lon":        float(body["lon"]),
        "dest_type":  body.get("dest_type", "broadcast"),
        "channel":    int(body.get("channel", 0)),
        "node_id":    str(body.get("node_id") or ""),
        "sched_type": stype,
        "hour":       int(body["hour"]),
        "minute":     int(body["minute"]),
        "days":       body.get("days") or [],
        "run_at":     float(body["run_at"]) if body.get("run_at") else None,
        "units":      body.get("units", "metric"),
        "enabled":    1,
        "last_run":   None,
        "run_count":  0,
        "created_at": now,
        "updated_at": now,
    }
    s["next_run"] = _calc_next_run(s)

    await asyncio.to_thread(_upsert_schedule, s)
    return {"status": "created", "schedule": s}


@plugin_router.patch("/schedules/{sid}")
async def update_schedule(sid: str, body: dict = Body(...)):
    s = await asyncio.to_thread(_get_schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")

    allowed = ["label","lat","lon","dest_type","channel","node_id",
               "sched_type","hour","minute","days","run_at","units","enabled"]
    for k in allowed:
        if k in body:
            s[k] = body[k]

    s["updated_at"] = time.time()
    if any(k in body for k in ["sched_type","hour","minute","days","run_at","enabled"]):
        s["next_run"] = _calc_next_run(s) if s.get("enabled") else None

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
    s["next_run"]   = _calc_next_run(s) if s["enabled"] else None
    await asyncio.to_thread(_upsert_schedule, s)
    return {"status": "toggled", "enabled": bool(s["enabled"]), "schedule": s}


@plugin_router.post("/schedules/{sid}/run-now")
async def run_schedule_now(sid: str):
    """Trigger a schedule immediately (test / manual fire)."""
    s = await asyncio.to_thread(_get_schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    cm = core_context.get("connection_manager")
    if not cm or not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready")
    try:
        msg = await _do_weather_send(
            lat=s["lat"], lon=s["lon"],
            dest_type=s["dest_type"],
            channel=s["channel"],
            node_id=s["node_id"],
            units=s.get("units", "metric"),
        )
    except Exception as e:
        raise HTTPException(500, f"Send failed: {e}")
    return {"status": "sent", "message": msg}