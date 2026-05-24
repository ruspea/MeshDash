"""
Geo Fence Plugin — v1.0.0
==========================
Advanced geofencing engine. Monitors live node positions and evaluates
every defined zone + trigger combination on each position update.

Zone types:
  circle    — centre lat/lon + radius metres
  polygon   — list of [lat,lon] vertices (GeoJSON ring)
  corridor  — polyline + buffer width metres
  node_rel  — follows a reference node; effective centre moves with it

Trigger types:
  enter     — first position inside zone after being outside
  exit      — first position outside zone after being inside
  dwell     — inside zone continuously for >= dwell_seconds
  absent    — zone has no nodes inside for >= absent_seconds
  proximity — two specific nodes within distance_m of each other
  speed     — node ground speed exceeds threshold_kmh
  heading   — node bearing changes by >= degrees within heading_window_secs

Actions (multiple per trigger, each independently configured):
  alert     — SSE broadcast (always)
  dm        — mesh DM to target_node_id with message template
  broadcast — mesh broadcast on channel_index with message template
  webhook   — HTTP POST to url with JSON payload

Message template tokens:
  {node}      long name of triggering node
  {node_id}   node_id
  {zone}      zone name
  {trigger}   trigger type
  {lat}       latitude (6dp)
  {lon}       longitude (6dp)
  {speed}     ground speed km/h
  {dist}      distance from zone boundary / between nodes (metres)
  {ts}        ISO timestamp
"""

import asyncio
import base64
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pubsub import pub

logger        = logging.getLogger("plugin.geo_fence")
plugin_router = APIRouter()

_DB_PATH = os.path.join(PLUGIN_DIR, "geo_fence.db")
_DB_LOCK = threading.Lock()

_node_registry: Dict[str, Any] = {}
_event_loop:    Optional[asyncio.AbstractEventLoop] = None

# Runtime state — rebuilt from DB on startup
_zones:    Dict[str, dict] = {}   # zone_id → zone dict
_triggers: Dict[str, dict] = {}   # trigger_id → trigger dict

# Per-(zone,node) state machine
_node_zone_state: Dict[str, dict] = {}   # "zone_id:node_id" → state dict
# Per-(node_a,node_b) proximity state
_prox_state: Dict[str, dict] = {}        # "a:b" → state dict
# Per-node motion state (for speed + heading)
_motion_state: Dict[str, dict] = {}      # node_id → {last_lat, last_lon, last_ts, last_heading}

_state_lock = threading.Lock()

_last_fired: Dict[str, float] = {}  # "trigger_id:node_id" → unix ts

# Haversine geometry

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two lat/lon points."""
    R = 6_371_000.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a  = math.sin(Δφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(Δλ/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 → point 2 in degrees (0–360)."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δλ     = math.radians(lon2 - lon1)
    x = math.sin(Δλ) * math.cos(φ2)
    y = math.cos(φ1)*math.sin(φ2) - math.sin(φ1)*math.cos(φ2)*math.cos(Δλ)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _point_in_polygon(lat: float, lon: float, ring: List[List[float]]) -> bool:
    """Ray-casting point-in-polygon. ring = [[lat,lon], ...]."""
    n   = len(ring)
    ins = False
    j   = n - 1
    for i in range(n):
        xi, yi = ring[i][1], ring[i][0]   # lon, lat
        xj, yj = ring[j][1], ring[j][0]
        if ((yi > lat) != (yj > lat)) and (lon < (xj-xi)*(lat-yi)/(yj-yi+1e-12) + xi):
            ins = not ins
        j = i
    return ins


def _dist_to_segment(plat: float, plon: float,
                     alat: float, alon: float,
                     blat: float, blon: float) -> float:
    """Approximate distance from point P to segment AB (metres, flat-earth)."""
    # Project onto a local Cartesian plane centred on A
    R    = 6_371_000.0
    cosA = math.cos(math.radians(alat))
    ax, ay = 0.0, 0.0
    bx = (blon - alon) * cosA * math.pi/180 * R
    by = (blat - alat) * math.pi/180 * R
    px = (plon - alon) * cosA * math.pi/180 * R
    py = (plat - alat) * math.pi/180 * R
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, (px*dx + py*dy) / (dx*dx + dy*dy + 1e-9)))
    cx, cy = ax + t*dx, ay + t*dy
    return math.hypot(px - cx, py - cy)


def _point_in_corridor(lat: float, lon: float,
                       path: List[List[float]], width_m: float) -> bool:
    """True if point is within width_m/2 of any segment of path."""
    half = width_m / 2.0
    for i in range(len(path) - 1):
        a, b = path[i], path[i+1]
        if _dist_to_segment(lat, lon, a[0], a[1], b[0], b[1]) <= half:
            return True
    return False


def _point_in_zone(lat: float, lon: float, zone: dict,
                   ref_nodes: Dict[str, dict]) -> Tuple[bool, float]:
    """
    Returns (inside, dist_to_boundary_or_centre).
    dist is positive when inside (metres from boundary) or
    negative when outside (metres beyond boundary).
    """
    ztype = zone["zone_type"]

    if ztype == "circle":
        clat = zone["centre_lat"]
        clon = zone["centre_lon"]
        r    = zone["radius_m"]
        dist = _haversine(lat, lon, clat, clon)
        return dist <= r, r - dist

    if ztype == "polygon":
        ring  = zone["geometry"]          # [[lat,lon], ...]
        inside = _point_in_polygon(lat, lon, ring)
        # Dist = min distance to any edge (approximate)
        min_d = min(
            _dist_to_segment(lat, lon, ring[i][0], ring[i][1],
                             ring[(i+1)%len(ring)][0], ring[(i+1)%len(ring)][1])
            for i in range(len(ring))
        )
        return inside, min_d if inside else -min_d

    if ztype == "corridor":
        path  = zone["geometry"]
        width = zone["radius_m"]
        inside = _point_in_corridor(lat, lon, path, width)
        min_d  = min(
            _dist_to_segment(lat, lon, path[i][0], path[i][1],
                             path[i+1][0], path[i+1][1])
            for i in range(len(path)-1)
        )
        return inside, (width/2 - min_d) if inside else (min_d - width/2)

    if ztype == "node_rel":
        ref_id   = zone.get("ref_node_id", "")
        ref_node = ref_nodes.get(ref_id, {})
        rlat     = ref_node.get("latitude")
        rlon     = ref_node.get("longitude")
        if rlat is None or rlon is None:
            return False, 0.0
        r    = zone["radius_m"]
        dist = _haversine(lat, lon, rlat, rlon)
        return dist <= r, r - dist

    return False, 0.0


# DB

def _db_init():
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS zones (
            id          TEXT PRIMARY KEY,
            slot_id     TEXT NOT NULL DEFAULT 'node_0',
            name        TEXT NOT NULL,
            zone_type   TEXT NOT NULL,
            centre_lat  REAL,
            centre_lon  REAL,
            radius_m    REAL DEFAULT 100,
            geometry    TEXT,
            ref_node_id TEXT,
            colour      TEXT DEFAULT '#00c8f5',
            active      BOOLEAN DEFAULT 1,
            created_at  REAL,
            updated_at  REAL,
            notes       TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS triggers (
            id              TEXT PRIMARY KEY,
            zone_id         TEXT,
            slot_id         TEXT NOT NULL DEFAULT 'node_0',
            name            TEXT NOT NULL,
            trigger_type    TEXT NOT NULL,
            target_node_ids TEXT DEFAULT '[]',
            ref_node_id     TEXT,
            dwell_seconds   REAL DEFAULT 60,
            absent_seconds  REAL DEFAULT 300,
            distance_m      REAL DEFAULT 200,
            threshold_kmh   REAL DEFAULT 50,
            heading_degrees REAL DEFAULT 90,
            heading_window  REAL DEFAULT 30,
            cooldown_secs   REAL DEFAULT 60,
            active          BOOLEAN DEFAULT 1,
            actions         TEXT DEFAULT '[]',
            created_at      REAL
        );
        CREATE TABLE IF NOT EXISTS events (
            id              TEXT PRIMARY KEY,
            ts              REAL,
            trigger_id      TEXT,
            zone_id         TEXT,
            node_id         TEXT,
            trigger_type    TEXT,
            details         TEXT,
            acked           BOOLEAN DEFAULT 0
        );
        """)
        conn.commit()
        conn.close()


def _db_conn():
    return sqlite3.connect(_DB_PATH, check_same_thread=False)


def _load_zones_triggers():
    global _zones, _triggers
    with _DB_LOCK:
        conn = _db_conn()
        cur  = conn.execute("SELECT * FROM zones WHERE active=1")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.close()
    if not cols:
        return
    _zones = {}
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("geometry"):
            try: d["geometry"] = json.loads(d["geometry"])
            except Exception as e:
                logger.error(f"Failed to parse zone geometry JSON: {e}")
                d["geometry"] = []
        _zones[d["id"]] = d

    with _DB_LOCK:
        conn  = _db_conn()
        tcur  = conn.execute("SELECT * FROM triggers WHERE active=1")
        trows = tcur.fetchall()
        tcols = [d[0] for d in tcur.description] if tcur.description else []
        conn.close()
    _triggers = {}
    for r in trows:
        d = dict(zip(tcols, r))
        for jf in ("target_node_ids", "actions"):
            try: d[jf] = json.loads(d.get(jf) or "[]")
            except Exception as e:
                logger.error(f"Failed to parse trigger field {jf} JSON: {e}")
                d[jf] = []
        _triggers[d["id"]] = d


def _save_event(trigger_id, zone_id, node_id, ttype, details):
    eid = str(uuid.uuid4())[:12]
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(
            "INSERT INTO events (id,ts,trigger_id,zone_id,node_id,trigger_type,details) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, time.time(), trigger_id, zone_id, node_id, ttype, json.dumps(details))
        )
        conn.commit()
        conn.close()
    return eid


# Plugin lifecycle

def init_plugin(context: dict):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    _db_init()
    _load_zones_triggers()
    logger.info("Geo Fence v1.0.0 — %d zone(s), %d trigger(s)",
                len(_zones), len(_triggers))
    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
    try:
        pub.subscribe(_on_receive, "meshtastic.receive")
    except Exception as e:
        logger.error("pub.subscribe: %s", e)
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


# Packet listener

def _on_receive(packet, interface=None):
    try:
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return
        portnum = str(decoded.get("portnum", ""))
        if "POSITION" not in portnum and "position" not in decoded:
            return

        pos     = decoded.get("position", {})
        lat     = pos.get("latitude")
        lon     = pos.get("longitude")
        if lat is None and pos.get("latitudeI"):
            lat = pos["latitudeI"] * 1e-7
        if lon is None and pos.get("longitudeI"):
            lon = pos["longitudeI"] * 1e-7
        if lat is None or lon is None:
            return

        from_id = packet.get("fromId") or packet.get("from_id") or ""
        slot_id = _slot_for_iface(None)
        speed   = pos.get("groundSpeed")   # m/s or None
        track   = pos.get("groundTrack")   # degrees or None

        if _event_loop:
            asyncio.run_coroutine_threadsafe(
                _evaluate(from_id, lat, lon, speed, track, slot_id),
                _event_loop,
            )
    except Exception as e:
        logger.debug("_on_receive: %s", e)


def _slot_for_iface(_iface) -> str:
    # meshtastic.receive topic delivers only (packet) — no interface
    # Return first available slot (no interface-based matching possible)
    for sid in _node_registry:
        return sid
    return "node_0"


def _get_all_node_positions(slot_id: str) -> Dict[str, dict]:
    slot = _node_registry.get(slot_id)
    if not slot:
        return {}
    result = {}
    for nid, nd in slot.meshtastic_data.nodes.items():
        lat = nd.get("latitude")
        lon = nd.get("longitude")
        if lat and lon:
            result[nid] = {"latitude": lat, "longitude": lon,
                           "long_name": nd.get("long_name") or nd.get("user", {}).get("longName") or nid}
    return result


# Evaluation engine

async def _evaluate(node_id: str, lat: float, lon: float,
                    speed_ms, track_deg, slot_id: str):
    """Called on every position packet. Evaluates all active zones + triggers."""
    ref_nodes = _get_all_node_positions(slot_id)
    ref_nodes[node_id] = {"latitude": lat, "longitude": lon}
    now = time.time()

    for zid, zone in list(_zones.items()):
        if zone.get("slot_id") != slot_id:
            continue
        inside, dist = _point_in_zone(lat, lon, zone, ref_nodes)
        state_key = f"{zid}:{node_id}"
        with _state_lock:
            state = _node_zone_state.get(state_key) or {
                "inside": None, "entered_at": None, "dwell_fired": False
            }

        prev = state.get("inside")

        if inside and prev is False:
            # ENTER
            state = {"inside": True, "entered_at": now, "dwell_fired": False}
            await _fire_triggers(zid, node_id, "enter", lat, lon, dist, slot_id, ref_nodes)

        elif not inside and prev is True:
            # EXIT
            state = {"inside": False, "entered_at": None, "dwell_fired": False}
            await _fire_triggers(zid, node_id, "exit", lat, lon, dist, slot_id, ref_nodes)

        elif inside and prev is True:
            # Still inside — check dwell
            if not state.get("dwell_fired"):
                entered = state.get("entered_at") or now
                for tid, trig in list(_triggers.items()):
                    if trig.get("zone_id") != zid:
                        continue
                    if trig.get("trigger_type") != "dwell":
                        continue
                    if _target_matches(trig, node_id) and (now - entered) >= trig.get("dwell_seconds", 60):
                        state["dwell_fired"] = True
                        await _fire_trigger(tid, trig, zid, node_id, "dwell", lat, lon, dist, slot_id, ref_nodes)

        elif prev is None:
            state = {"inside": inside, "entered_at": now if inside else None, "dwell_fired": False}

        with _state_lock:
            _node_zone_state[state_key] = state

    # Handled in _absent_scanner background task

    if speed_ms is not None:
        speed_kmh = speed_ms * 3.6
        for tid, trig in list(_triggers.items()):
            if trig.get("trigger_type") != "speed":
                continue
            if trig.get("slot_id") != slot_id:
                continue
            if not _target_matches(trig, node_id):
                continue
            if speed_kmh >= trig.get("threshold_kmh", 50):
                details = {"speed_kmh": round(speed_kmh, 1)}
                await _fire_trigger(tid, trig, None, node_id, "speed", lat, lon, 0.0, slot_id, ref_nodes, details)

    if track_deg is not None:
        with _state_lock:
            ms = _motion_state.get(node_id) or {}
        prev_heading = ms.get("last_heading")
        if prev_heading is not None:
            diff = abs(track_deg - prev_heading)
            if diff > 180: diff = 360 - diff
            for tid, trig in list(_triggers.items()):
                if trig.get("trigger_type") != "heading":
                    continue
                if trig.get("slot_id") != slot_id:
                    continue
                if not _target_matches(trig, node_id):
                    continue
                if diff >= trig.get("heading_degrees", 90):
                    details = {"heading_change_deg": round(diff, 1),
                               "from_heading": round(prev_heading, 1),
                               "to_heading": round(track_deg, 1)}
                    await _fire_trigger(tid, trig, None, node_id, "heading", lat, lon, 0.0, slot_id, ref_nodes, details)
        with _state_lock:
            _motion_state[node_id] = {**ms, "last_heading": track_deg,
                                       "last_lat": lat, "last_lon": lon, "last_ts": now}

    for tid, trig in list(_triggers.items()):
        if trig.get("trigger_type") != "proximity":
            continue
        if trig.get("slot_id") != slot_id:
            continue
        node_a_ids = trig.get("target_node_ids") or []
        ref_b      = trig.get("ref_node_id", "")
        if not ref_b:
            continue
        # Trigger fires when node_id (which just moved) is in node_a_ids
        # and is within distance_m of ref_b
        if node_a_ids and node_id not in node_a_ids:
            continue
        ref_b_data = ref_nodes.get(ref_b)
        if not ref_b_data:
            continue
        dist = _haversine(lat, lon,
                          ref_b_data["latitude"], ref_b_data["longitude"])
        threshold = trig.get("distance_m", 200)
        pk = f"{node_id}:{ref_b}"
        with _state_lock:
            ps = _prox_state.get(pk, {"near": None})
        near = dist <= threshold
        if near and not ps.get("near"):
            _prox_state[pk] = {"near": True, "ts": now}
            details = {"dist_m": round(dist), "ref_node": ref_b}
            await _fire_trigger(tid, trig, None, node_id, "proximity", lat, lon, dist, slot_id, ref_nodes, details)
        elif not near and ps.get("near"):
            with _state_lock:
                _prox_state[pk] = {"near": False, "ts": now}


def _target_matches(trig: dict, node_id: str) -> bool:
    targets = trig.get("target_node_ids") or []
    return not targets or node_id in targets


async def _fire_triggers(zone_id, node_id, ttype, lat, lon, dist, slot_id, ref_nodes):
    for tid, trig in list(_triggers.items()):
        if trig.get("zone_id") != zone_id: continue
        if trig.get("trigger_type") != ttype: continue
        if trig.get("slot_id") != slot_id: continue
        if not _target_matches(trig, node_id): continue
        await _fire_trigger(tid, trig, zone_id, node_id, ttype, lat, lon, dist, slot_id, ref_nodes)


async def _fire_trigger(trigger_id, trig, zone_id, node_id, ttype,
                        lat, lon, dist, slot_id, ref_nodes, extra=None):
    """Evaluate cooldown, save event, execute all actions."""
    ck = f"{trigger_id}:{node_id}"
    now = time.time()
    cooldown = trig.get("cooldown_secs", 60)
    if now - _last_fired.get(ck, 0) < cooldown:
        return
    _last_fired[ck] = now

    zone = _zones.get(zone_id or "")
    slot = _node_registry.get(slot_id)
    node_data = (slot.meshtastic_data.nodes.get(node_id) if slot else None) or {}
    u = node_data.get("user") or {}
    node_name = u.get("longName") or node_data.get("long_name") or node_id

    details = {
        "node_id": node_id, "node_name": node_name,
        "zone_id": zone_id, "zone_name": zone.get("name", "") if zone else "",
        "trigger_type": ttype,
        "lat": round(lat, 6), "lon": round(lon, 6),
        "dist_m": round(dist) if dist else 0,
        "ts": now,
    }
    if extra:
        details.update(extra)

    eid = _save_event(trigger_id, zone_id, node_id, ttype, details)

    # Build template context
    ctx = {
        "node":    node_name,
        "node_id": node_id,
        "zone":    zone.get("name", "") if zone else "",
        "trigger": ttype,
        "lat":     str(round(lat, 6)),
        "lon":     str(round(lon, 6)),
        "speed":   str(details.get("speed_kmh", "")),
        "dist":    str(details.get("dist_m", "")),
        "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }

    # SSE alert always
    try:
        from meshtastic_dashboard import broadcast_data
        await broadcast_data({
            "event": "geo_fence_event",
            "data":  {"event_id": eid, **details},
        }, slot_id=slot_id)
    except Exception as e:
        logger.debug("broadcast: %s", e)

    logger.info("GeoFence [%s] %s %s %s", slot_id, ttype.upper(), node_id,
                zone.get("name", "") if zone else "")

    # Execute configured actions
    for action in (trig.get("actions") or []):
        atype = action.get("type", "")
        try:
            if atype == "dm":
                msg = _render(action.get("message", "GeoFence: {trigger} {node} @ {zone}"), ctx)
                target = action.get("target_node_id", "")
                if target and slot:
                    cm = slot.connection_manager
                    await cm.sendText(msg, destinationId=target,
                                     channelIndex=action.get("channel_index", 0),
                                     wantAck=False)

            elif atype == "broadcast":
                msg = _render(action.get("message", "GeoFence: {trigger} {node} @ {zone}"), ctx)
                if slot:
                    cm = slot.connection_manager
                    await cm.sendText(msg, destinationId="^all",
                                     channelIndex=action.get("channel_index", 0),
                                     wantAck=False)

            elif atype == "webhook":
                url  = action.get("url", "")
                body = _render(action.get("body", "{}"), ctx)
                if url:
                    asyncio.create_task(_post_webhook(url, body, action.get("headers", {})))

        except Exception as e:
            logger.warning("Action %s failed: %s", atype, e)


def _render(template: str, ctx: dict) -> str:
    for k, v in ctx.items():
        template = template.replace("{" + k + "}", str(v))
    return template


async def _post_webhook(url: str, body: str, headers: dict):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, content=body, headers={
                "Content-Type": "application/json",
                **headers,
            })
    except Exception as e:
        logger.warning("Webhook %s: %s", url, e)


# Routes

@plugin_router.get("")
@plugin_router.get("/")
async def health():
    return {"plugin": "geo_fence", "version": "1.0.0", "status": "running",
            "zones": len(_zones), "triggers": len(_triggers)}



class ZoneReq(BaseModel):
    slot_id:     str   = "node_0"
    name:        str
    zone_type:   str   = "circle"    # circle|polygon|corridor|node_rel
    centre_lat:  Optional[float] = None
    centre_lon:  Optional[float] = None
    radius_m:    float = 200.0
    geometry:    Optional[list]  = None   # [[lat,lon],...] for polygon/corridor
    ref_node_id: Optional[str]   = None   # for node_rel zone
    colour:      str   = "#00c8f5"
    notes:       str   = ""


@plugin_router.get("/zones")
async def list_zones(slot_id: str = ""):
    with _DB_LOCK:
        conn = _db_conn()
        q    = "SELECT * FROM zones" + (" WHERE slot_id=?" if slot_id else "") + " ORDER BY created_at DESC"
        cur  = conn.execute(q, (slot_id,) if slot_id else ())
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        conn.close()
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("geometry"):
            try: d["geometry"] = json.loads(d["geometry"])
            except Exception as e:
                logger.error(f"Failed to parse zone geometry JSON: {e}")
                d["geometry"] = []
        result.append(d)
    return {"zones": result}


@plugin_router.post("/zones")
async def create_zone(r: ZoneReq):
    zid = str(uuid.uuid4())[:12]
    now = time.time()
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(
            "INSERT INTO zones (id,slot_id,name,zone_type,centre_lat,centre_lon,"
            "radius_m,geometry,ref_node_id,colour,active,created_at,updated_at,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (zid, r.slot_id, r.name, r.zone_type, r.centre_lat, r.centre_lon,
             r.radius_m, json.dumps(r.geometry) if r.geometry else None,
             r.ref_node_id, r.colour, now, now, r.notes)
        )
        conn.commit()
        conn.close()
    _load_zones_triggers()
    return {"status": "created", "id": zid}


@plugin_router.put("/zones/{zid}")
async def update_zone(zid: str, r: ZoneReq):
    now = time.time()
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(
            "UPDATE zones SET name=?,zone_type=?,centre_lat=?,centre_lon=?,radius_m=?,"
            "geometry=?,ref_node_id=?,colour=?,notes=?,updated_at=? WHERE id=?",
            (r.name, r.zone_type, r.centre_lat, r.centre_lon, r.radius_m,
             json.dumps(r.geometry) if r.geometry else None,
             r.ref_node_id, r.colour, r.notes, now, zid)
        )
        conn.commit()
        conn.close()
    _load_zones_triggers()
    return {"status": "updated"}


@plugin_router.delete("/zones/{zid}")
async def delete_zone(zid: str):
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("DELETE FROM zones WHERE id=?", (zid,))
        conn.execute("DELETE FROM triggers WHERE zone_id=?", (zid,))
        conn.commit()
        conn.close()
    _load_zones_triggers()
    return {"status": "deleted"}



class TriggerReq(BaseModel):
    slot_id:         str       = "node_0"
    zone_id:         Optional[str]   = None
    name:            str
    trigger_type:    str       = "enter"
    target_node_ids: List[str] = []
    ref_node_id:     Optional[str]   = None
    dwell_seconds:   float     = 60.0
    absent_seconds:  float     = 300.0
    distance_m:      float     = 200.0
    threshold_kmh:   float     = 50.0
    heading_degrees: float     = 90.0
    heading_window:  float     = 30.0
    cooldown_secs:   float     = 60.0
    actions:         list      = []


@plugin_router.get("/triggers")
async def list_triggers(slot_id: str = ""):
    with _DB_LOCK:
        conn = _db_conn()
        q    = "SELECT * FROM triggers" + (" WHERE slot_id=?" if slot_id else "") + " ORDER BY created_at DESC"
        cur  = conn.execute(q, (slot_id,) if slot_id else ())
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        conn.close()
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        for jf in ("target_node_ids","actions"):
            try: d[jf] = json.loads(d.get(jf) or "[]")
            except Exception as e:
                logger.error(f"Failed to parse trigger field {jf} JSON: {e}")
                d[jf] = []
        result.append(d)
    return {"triggers": result}


@plugin_router.post("/triggers")
async def create_trigger(r: TriggerReq):
    tid = str(uuid.uuid4())[:12]
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(
            "INSERT INTO triggers (id,zone_id,slot_id,name,trigger_type,target_node_ids,"
            "ref_node_id,dwell_seconds,absent_seconds,distance_m,threshold_kmh,"
            "heading_degrees,heading_window,cooldown_secs,active,actions,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (tid, r.zone_id, r.slot_id, r.name, r.trigger_type,
             json.dumps(r.target_node_ids), r.ref_node_id,
             r.dwell_seconds, r.absent_seconds, r.distance_m, r.threshold_kmh,
             r.heading_degrees, r.heading_window, r.cooldown_secs,
             json.dumps(r.actions), time.time())
        )
        conn.commit()
        conn.close()
    _load_zones_triggers()
    return {"status": "created", "id": tid}


@plugin_router.delete("/triggers/{tid}")
async def delete_trigger(tid: str):
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("DELETE FROM triggers WHERE id=?", (tid,))
        conn.commit()
        conn.close()
    _load_zones_triggers()
    return {"status": "deleted"}



@plugin_router.get("/events")
async def list_events(limit: int = 200, unacked_only: bool = False):
    with _DB_LOCK:
        conn = _db_conn()
        q    = "SELECT * FROM events"
        if unacked_only: q += " WHERE acked=0"
        q   += " ORDER BY ts DESC LIMIT ?"
        cur  = conn.execute(q, (limit,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        conn.close()
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("details"):
            try: d["details"] = json.loads(d["details"])
            except Exception as e:
                logger.error(f"Failed to parse event details JSON: {e}")
        result.append(d)
    return {"events": result}


@plugin_router.post("/events/{eid}/ack")
async def ack_event(eid: str):
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("UPDATE events SET acked=1 WHERE id=?", (eid,))
        conn.commit()
        conn.close()
    return {"status": "acked"}


@plugin_router.post("/events/ack_all")
async def ack_all_events():
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("UPDATE events SET acked=1")
        conn.commit()
        conn.close()
    return {"status": "ok"}


@plugin_router.delete("/events")
async def clear_events():
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("DELETE FROM events")
        conn.commit()
        conn.close()
    return {"status": "cleared"}



@plugin_router.get("/nodes/{slot_id}")
async def live_nodes(slot_id: str):
    nodes = _get_all_node_positions(slot_id)
    slot  = _node_registry.get(slot_id)
    result = []
    for nid, pos in nodes.items():
        nd = (slot.meshtastic_data.nodes.get(nid) if slot else None) or {}
        u  = nd.get("user") or {}
        result.append({
            "node_id":   nid,
            "long_name": u.get("longName") or nd.get("long_name") or nid,
            "lat":       pos["latitude"],
            "lon":       pos["longitude"],
            "last_heard":nd.get("lastHeard") or 0,
            "is_local":  nd.get("isLocal", False),
        })
    return {"slot_id": slot_id, "nodes": result}



@plugin_router.get("/state/{slot_id}")
async def zone_state(slot_id: str):
    """Which nodes are currently inside which zones."""
    nodes   = _get_all_node_positions(slot_id)
    result  = {}
    for zid, zone in _zones.items():
        if zone.get("slot_id") != slot_id: continue
        inside = []
        for nid, pos in nodes.items():
            try:
                inn, _ = _point_in_zone(pos["latitude"], pos["longitude"], zone, nodes)
                if inn: inside.append(nid)
            except Exception: pass
        result[zid] = {"zone_name": zone["name"], "nodes_inside": inside}
    return {"slot_id": slot_id, "zones": result}