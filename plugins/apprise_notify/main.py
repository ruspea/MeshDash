"""
Apprise Notify Plugin — v1.0.0
================================
Sends mesh events to 130+ notification services via the Apprise library.

Event types watched
  message      — any channel message (all channels unless filtered)
  dm           — direct messages to/from local node
  node_online  — node seen for first time or back after offline threshold
  node_offline — node not heard for offline_threshold_min minutes
  node_seen    — ANY packet from a specific node (heartbeat)
  packet       — raw packet received (can filter by port_num)

Rule filters (all optional, stacked as AND logic)
  slot_id             — which radio slot to watch
  node_ids            — whitelist of node IDs (empty = all)
  channel_index       — specific channel index (None = all)
  direction           — "incoming" | "outgoing" | "both"
  keywords_include    — message must contain at least one of these words
  keywords_exclude    — message must NOT contain any of these words
  min_hops / max_hops — hop count range filter

DnD (Do Not Disturb) per rule
  dnd_days    — bitmask Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64
                0 = no DnD day filter (always allowed)
  dnd_start   — "HH:MM" start of quiet window (e.g. "22:00")
  dnd_end     — "HH:MM" end of quiet window   (e.g. "07:00")
                windows that cross midnight are supported

Rate limiting per rule
  rate_limit_n    — max fires in the rate_limit_window_min window
  rate_limit_window_min — rolling window in minutes (default 60)

Message templates
  {node}     long name of source node
  {node_id}  raw node ID
  {short}    short name
  {msg}      message text (empty for non-message events)
  {channel}  channel index
  {hops}     hop count
  {snr}      SNR dB
  {bat}      battery %
  {event}    event type string
  {ts}       HH:MM:SS timestamp
  {slot}     slot_id
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import apprise as _apprise_lib
    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pubsub import pub

import os

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

logger        = logging.getLogger("plugin.apprise_notify")
plugin_router = APIRouter()

_DB_PATH = os.path.join(PLUGIN_DIR, "apprise_notify.db")
_DB_LOCK = threading.Lock()

_node_registry: Dict[str, Any] = {}
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# Track last-heard times for online/offline detection
_last_heard: Dict[str, float] = {}   # node_id → unix ts
_online_state: Dict[str, bool] = {}  # node_id → True/False

# Rate-limit tracking: rule_id → deque of fire timestamps
_rate_fires: Dict[str, list] = {}

# Rules cache — avoids SQLite read on every packet
_rules_cache: List[dict] = []
_rules_cache_ts: float = 0.0
_RULES_CACHE_TTL = 10.0


def _get_rules_cached() -> List[dict]:
    global _rules_cache, _rules_cache_ts
    now = time.time()
    if now - _rules_cache_ts > _RULES_CACHE_TTL:
        _rules_cache = _load_all("rules")
        _rules_cache_ts = now
    return _rules_cache


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _db_init():
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS destinations (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            tags        TEXT DEFAULT '',
            enabled     BOOLEAN DEFAULT 1,
            created_at  REAL
        );

        CREATE TABLE IF NOT EXISTS rules (
            id                    TEXT PRIMARY KEY,
            name                  TEXT NOT NULL,
            enabled               BOOLEAN DEFAULT 1,
            slot_id               TEXT DEFAULT 'node_0',
            event_type            TEXT NOT NULL,
            dest_ids              TEXT DEFAULT '[]',

            -- Source filters
            node_ids              TEXT DEFAULT '[]',
            channel_index         INTEGER DEFAULT -1,
            direction             TEXT DEFAULT 'both',
            keywords_include      TEXT DEFAULT '[]',
            keywords_exclude      TEXT DEFAULT '[]',
            min_hops              INTEGER DEFAULT -1,
            max_hops              INTEGER DEFAULT -1,

            -- DnD
            dnd_enabled           BOOLEAN DEFAULT 0,
            dnd_days              INTEGER DEFAULT 0,
            dnd_start             TEXT DEFAULT '22:00',
            dnd_end               TEXT DEFAULT '07:00',

            -- Rate limit
            rate_limit_n          INTEGER DEFAULT 0,
            rate_limit_window_min INTEGER DEFAULT 60,

            -- Apprise send options
            priority              TEXT DEFAULT 'info',
            title_tmpl            TEXT DEFAULT 'Mesh Alert — {event}',
            body_tmpl             TEXT DEFAULT '[{ts}] {node}: {msg}',

            -- Offline detection (node_offline event only)
            offline_threshold_min INTEGER DEFAULT 60,

            created_at            REAL,
            last_fired            REAL DEFAULT 0,
            fire_count            INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS delivery_log (
            id          TEXT PRIMARY KEY,
            ts          REAL,
            rule_id     TEXT,
            rule_name   TEXT,
            dest_id     TEXT,
            dest_name   TEXT,
            event_type  TEXT,
            node_id     TEXT,
            title       TEXT,
            body        TEXT,
            success     BOOLEAN,
            error       TEXT DEFAULT ''
        );
        """)
        conn.commit()
        conn.close()


def _db():
    return sqlite3.connect(_DB_PATH, check_same_thread=False)


def _row_to_dict(cur, row):
    return {d[0]: v for d, v in zip(cur.description, row)}


def _load_all(table):
    with _DB_LOCK:
        conn = _db()
        cur  = conn.execute(f"SELECT * FROM {table} ORDER BY created_at DESC")
        rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
        conn.close()
    for r in rows:
        for key in ("dest_ids", "node_ids", "keywords_include", "keywords_exclude"):
            if key in r and isinstance(r[key], str):
                try:
                    r[key] = json.loads(r[key])
                except Exception:
                    r[key] = []
    return rows


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def init_plugin(context: dict):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")

    if not APPRISE_AVAILABLE:
        logger.warning("apprise library not installed — notifications disabled. Run: pip install apprise")

    _db_init()
    logger.info("Apprise Notify v1.0.0 — %s",
                "ready" if APPRISE_AVAILABLE else "APPRISE NOT INSTALLED")

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
    asyncio.create_task(_offline_scanner())
    while True:
        try:
            await asyncio.sleep(30)
            if wd and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception:
            pass


async def _offline_scanner():
    """Every 2 minutes, check nodes that haven't been heard past their rule threshold."""
    while True:
        try:
            await asyncio.sleep(120)
            now = time.time()
            rules = [r for r in _load_all("rules")
                     if r["enabled"] and r["event_type"] == "node_offline"]
            for rule in rules:
                threshold_s = (rule.get("offline_threshold_min") or 60) * 60
                slot = _node_registry.get(rule["slot_id"])
                if not slot:
                    continue
                for nid, nd in slot.meshtastic_data.nodes.items():
                    lh = nd.get("lastHeard") or nd.get("last_heard") or 0
                    if not lh:
                        continue
                    age = now - lh
                    was_online = _online_state.get(nid, True)
                    if age >= threshold_s and was_online:
                        _online_state[nid] = False
                        ctx = _build_ctx(nid, nd, rule["slot_id"],
                                         event_type="node_offline")
                        asyncio.create_task(_maybe_fire(rule, ctx))
                    elif age < threshold_s:
                        _online_state[nid] = True
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("offline_scanner: %s", e)


# ---------------------------------------------------------------------------
# Packet listener
# ---------------------------------------------------------------------------

def _on_receive(packet, interface=None):
    """Called by pubsub for every received packet. Runs in the interface thread."""
    if not _event_loop:
        return
    try:
        asyncio.run_coroutine_threadsafe(_handle_packet(packet, interface), _event_loop)
    except Exception as e:
        logger.debug("_on_receive dispatch: %s", e)


async def _handle_packet(packet: dict, interface=None):
    if not packet:
        return

    # Identify slot
    slot_id = "node_0"
    if interface:
        for sid, slot in _node_registry.items():
            iface = getattr(slot.meshtastic_data, "_interface", None) or \
                    getattr(slot.meshtastic_data, "interface", None)
            if iface is interface:
                slot_id = sid
                break

    decoded    = packet.get("decoded") or {}
    portnum    = decoded.get("portnum", "")
    from_id    = packet.get("fromId") or packet.get("from_id") or ""
    to_id      = packet.get("toId")   or packet.get("to_id")   or ""
    channel    = packet.get("channel") or packet.get("channelIndex") or 0
    hop_start  = packet.get("hopStart")  or packet.get("hop_start")  or 0
    hop_limit  = packet.get("hopLimit")  or packet.get("hop_limit")  or 0
    snr        = packet.get("rxSnr")     or packet.get("rx_snr")
    hops_used  = max(0, hop_start - hop_limit) if hop_start else 0

    slot = _node_registry.get(slot_id)
    local_id = getattr(slot.meshtastic_data, "local_node_id", "") if slot else ""
    node = (slot.meshtastic_data.nodes.get(from_id) or {}) if slot else {}

    # Update last_heard + online state
    _last_heard[from_id] = time.time()
    was_online = _online_state.get(from_id, False)
    if not was_online:
        _online_state[from_id] = True

    # Build context dict
    msg_text = ""
    if portnum == "TEXT_MESSAGE_APP":
        txt = decoded.get("text") or ""
        if not txt:
            payload = decoded.get("payload")
            if isinstance(payload, bytes):
                try:
                    txt = payload.decode("utf-8", errors="replace")
                except Exception:
                    txt = ""
        msg_text = txt

    ctx = _build_ctx(from_id, node, slot_id, event_type="packet",
                     msg=msg_text, channel=channel,
                     hops=hops_used, snr=snr,
                     to_id=to_id, local_id=local_id,
                     portnum=portnum)

    rules = _get_rules_cached()
    for rule in rules:
        if not rule["enabled"]:
            continue
        if rule["slot_id"] != slot_id:
            continue
        etype = rule["event_type"]

        if etype == "packet":
            asyncio.create_task(_maybe_fire(rule, ctx))

        elif etype == "message" and portnum == "TEXT_MESSAGE_APP":
            if to_id != local_id or to_id == "^all" or to_id == "":
                asyncio.create_task(_maybe_fire(rule, {**ctx, "event_type": "message"}))

        elif etype == "dm" and portnum == "TEXT_MESSAGE_APP":
            if to_id == local_id or from_id == local_id:
                asyncio.create_task(_maybe_fire(rule, {**ctx, "event_type": "dm"}))

        elif etype == "node_seen":
            asyncio.create_task(_maybe_fire(rule, {**ctx, "event_type": "node_seen"}))

        elif etype == "node_online" and not was_online:
            asyncio.create_task(_maybe_fire(rule, {**ctx, "event_type": "node_online"}))


def _build_ctx(node_id, node_dict, slot_id, event_type="packet",
               msg="", channel=0, hops=0, snr=None,
               to_id="", local_id="", portnum=""):
    u    = node_dict.get("user") or {}
    dm   = node_dict.get("deviceMetrics") or {}
    bat  = dm.get("batteryLevel") or node_dict.get("battery_level")
    lname = u.get("longName")  or node_dict.get("long_name")  or node_id
    sname = u.get("shortName") or node_dict.get("short_name") or node_id[-4:]
    now  = datetime.now()
    return {
        "node_id":    node_id,
        "long_name":  lname,
        "short_name": sname,
        "msg":        msg,
        "channel":    channel,
        "hops":       hops,
        "snr":        snr,
        "battery":    bat,
        "event_type": event_type,
        "slot_id":    slot_id,
        "to_id":      to_id,
        "local_id":   local_id,
        "portnum":    portnum,
        "ts":         now.strftime("%H:%M:%S"),
        "ts_unix":    time.time(),
    }


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

async def _maybe_fire(rule: dict, ctx: dict):
    """Check all filters + DnD + rate limit, then fire."""
    try:
        if not _passes_filters(rule, ctx):
            return
        if _in_dnd(rule):
            return
        if not _passes_rate_limit(rule):
            return

        asyncio.create_task(_fire_rule(rule, ctx))
    except Exception as e:
        logger.debug("_maybe_fire: %s", e)


def _passes_filters(rule: dict, ctx: dict) -> bool:
    # Node whitelist
    node_ids = rule.get("node_ids") or []
    if node_ids and ctx["node_id"] not in node_ids:
        return False

    # Channel filter (-1 = any)
    ch = rule.get("channel_index", -1)
    if ch >= 0 and ctx.get("channel") != ch:
        return False

    # Direction (dm/message events only)
    direction = rule.get("direction", "both")
    if direction != "both":
        if direction == "incoming" and ctx.get("node_id") == ctx.get("local_id"):
            return False
        if direction == "outgoing" and ctx.get("node_id") != ctx.get("local_id"):
            return False

    # Keyword include (any match = pass)
    ki = rule.get("keywords_include") or []
    if ki:
        text = (ctx.get("msg") or "").lower()
        if not any(kw.lower() in text for kw in ki if kw):
            return False

    # Keyword exclude (any match = fail)
    ke = rule.get("keywords_exclude") or []
    if ke:
        text = (ctx.get("msg") or "").lower()
        if any(kw.lower() in text for kw in ke if kw):
            return False

    # Hops range
    min_h = rule.get("min_hops", -1)
    max_h = rule.get("max_hops", -1)
    hops  = ctx.get("hops") or 0
    if min_h >= 0 and hops < min_h:
        return False
    if max_h >= 0 and hops > max_h:
        return False

    return True


def _in_dnd(rule: dict) -> bool:
    """Return True if we are currently in the DnD window for this rule."""
    if not rule.get("dnd_enabled"):
        return False

    now   = datetime.now()
    day_bit = 1 << now.weekday()  # Mon=0→1, Tue=1→2, ...

    dnd_days = rule.get("dnd_days", 0)
    if dnd_days and not (dnd_days & day_bit):
        return False  # today is not a DnD day

    start_str = rule.get("dnd_start", "22:00")
    end_str   = rule.get("dnd_end",   "07:00")
    try:
        sh, sm = [int(x) for x in start_str.split(":")]
        eh, em = [int(x) for x in end_str.split(":")]
    except Exception:
        return False

    now_min = now.hour * 60 + now.minute
    start_m = sh * 60 + sm
    end_m   = eh * 60 + em

    if start_m <= end_m:
        # same-day window e.g. 09:00–17:00
        return start_m <= now_min <= end_m
    else:
        # crosses midnight e.g. 22:00–07:00
        return now_min >= start_m or now_min <= end_m


def _passes_rate_limit(rule: dict) -> bool:
    n      = rule.get("rate_limit_n", 0)
    window = rule.get("rate_limit_window_min", 60) * 60
    if n <= 0:
        return True

    rid   = rule["id"]
    now   = time.time()
    fires = _rate_fires.setdefault(rid, [])
    # Prune old entries
    _rate_fires[rid] = [t for t in fires if now - t < window]
    if len(_rate_fires[rid]) >= n:
        return False
    _rate_fires[rid].append(now)
    return True


# ---------------------------------------------------------------------------
# Apprise dispatch
# ---------------------------------------------------------------------------

async def _fire_rule(rule: dict, ctx: dict):
    if not APPRISE_AVAILABLE:
        logger.warning("Apprise not installed — cannot send notification")
        return

    dest_ids = rule.get("dest_ids") or []
    if not dest_ids:
        return

    dests = _load_all("destinations")
    dest_map = {d["id"]: d for d in dests if d["enabled"]}

    title = _render_tmpl(rule.get("title_tmpl", "Mesh Alert — {event}"), ctx)
    body  = _render_tmpl(rule.get("body_tmpl",  "[{ts}] {node}: {msg}"), ctx)
    ptype = _apprise_type(rule.get("priority", "info"))

    # Update fire stats
    with _DB_LOCK:
        conn = _db()
        conn.execute("UPDATE rules SET last_fired=?, fire_count=fire_count+1 WHERE id=?",
                     (time.time(), rule["id"]))
        conn.commit()
        conn.close()

    for did in dest_ids:
        dest = dest_map.get(did)
        if not dest:
            continue
        success, error = await _send_apprise(dest["url"], title, body, ptype)
        lid = str(uuid.uuid4())[:12]
        with _DB_LOCK:
            conn = _db()
            conn.execute(
                "INSERT INTO delivery_log (id,ts,rule_id,rule_name,dest_id,dest_name,"
                "event_type,node_id,title,body,success,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid, time.time(), rule["id"], rule["name"],
                 did, dest["name"], ctx["event_type"], ctx["node_id"],
                 title, body, success, error)
            )
            conn.commit()
            conn.close()
        if not success:
            logger.warning("Apprise send failed [%s → %s]: %s", rule["name"], dest["name"], error)


async def _send_apprise(url: str, title: str, body: str, notify_type) -> tuple:
    """Fire a single Apprise notification. Returns (success, error_str)."""
    try:
        ap = _apprise_lib.Apprise()
        ap.add(url)
        result = await asyncio.wait_for(
            ap.async_notify(body=body, title=title, notify_type=notify_type),
            timeout=15.0
        )
        return bool(result), ""
    except asyncio.TimeoutError:
        return False, "Apprise send timed out after 15s"
    except Exception as e:
        return False, str(e)


def _apprise_type(priority: str):
    if not APPRISE_AVAILABLE:
        return None
    m = {
        "info":    _apprise_lib.NotifyType.INFO,
        "success": _apprise_lib.NotifyType.SUCCESS,
        "warning": _apprise_lib.NotifyType.WARNING,
        "failure": _apprise_lib.NotifyType.FAILURE,
    }
    return m.get(priority, _apprise_lib.NotifyType.INFO)


def _render_tmpl(tmpl: str, ctx: dict) -> str:
    snr_str = f"{ctx['snr']:.1f}" if ctx.get("snr") is not None else "—"
    bat_str = f"{ctx['battery']}%" if ctx.get("battery") is not None else "—"
    return (tmpl
            .replace("{node}",    ctx.get("long_name", ctx.get("node_id", "")))
            .replace("{node_id}", ctx.get("node_id", ""))
            .replace("{short}",   ctx.get("short_name", ""))
            .replace("{msg}",     ctx.get("msg", ""))
            .replace("{channel}", str(ctx.get("channel", 0)))
            .replace("{hops}",    str(ctx.get("hops", 0)))
            .replace("{snr}",     snr_str)
            .replace("{bat}",     bat_str)
            .replace("{event}",   ctx.get("event_type", ""))
            .replace("{ts}",      ctx.get("ts", ""))
            .replace("{slot}",    ctx.get("slot_id", "")))


# ---------------------------------------------------------------------------
# Management API
# ---------------------------------------------------------------------------

# ── Destinations ─────────────────────────────────────────────────────────────

class DestReq(BaseModel):
    name:    str
    url:     str
    tags:    str  = ""
    enabled: bool = True


@plugin_router.get("/destinations")
async def list_destinations():
    return {"destinations": _load_all("destinations")}


@plugin_router.post("/destinations")
async def create_destination(r: DestReq):
    did = str(uuid.uuid4())[:12]
    with _DB_LOCK:
        conn = _db()
        conn.execute("INSERT INTO destinations (id,name,url,tags,enabled,created_at) VALUES (?,?,?,?,?,?)",
                     (did, r.name, r.url, r.tags, r.enabled, time.time()))
        conn.commit()
        conn.close()
    return {"id": did, "status": "created"}


@plugin_router.patch("/destinations/{did}")
async def update_destination(did: str, r: DestReq):
    with _DB_LOCK:
        conn = _db()
        conn.execute("UPDATE destinations SET name=?,url=?,tags=?,enabled=? WHERE id=?",
                     (r.name, r.url, r.tags, r.enabled, did))
        conn.commit()
        conn.close()
    return {"status": "updated"}


@plugin_router.delete("/destinations/{did}")
async def delete_destination(did: str):
    with _DB_LOCK:
        conn = _db()
        conn.execute("DELETE FROM destinations WHERE id=?", (did,))
        conn.commit()
        conn.close()
    return {"status": "deleted"}


@plugin_router.post("/destinations/{did}/test")
async def test_destination(did: str):
    """Send a test notification immediately."""
    if not APPRISE_AVAILABLE:
        raise HTTPException(503, "Apprise library not installed. Run: pip install apprise")
    dests = _load_all("destinations")
    dest  = next((d for d in dests if d["id"] == did), None)
    if not dest:
        raise HTTPException(404, "Destination not found")
    success, error = await _send_apprise(
        dest["url"],
        "✅ MeshDash — Test Notification",
        "This is a test from the Apprise Notify plugin. If you see this, the destination is working correctly.",
        _apprise_type("success")
    )
    return {"success": success, "error": error}


# ── Rules ─────────────────────────────────────────────────────────────────────

class RuleReq(BaseModel):
    name:                 str
    enabled:              bool  = True
    slot_id:              str   = "node_0"
    event_type:           str   = "message"
    dest_ids:             list  = []
    node_ids:             list  = []
    channel_index:        int   = -1
    direction:            str   = "both"
    keywords_include:     list  = []
    keywords_exclude:     list  = []
    min_hops:             int   = -1
    max_hops:             int   = -1
    dnd_enabled:          bool  = False
    dnd_days:             int   = 0
    dnd_start:            str   = "22:00"
    dnd_end:              str   = "07:00"
    rate_limit_n:         int   = 0
    rate_limit_window_min: int  = 60
    priority:             str   = "info"
    title_tmpl:           str   = "Mesh Alert — {event}"
    body_tmpl:            str   = "[{ts}] {node}: {msg}"
    offline_threshold_min: int  = 60


@plugin_router.get("/rules")
async def list_rules():
    return {"rules": _load_all("rules")}


@plugin_router.post("/rules")
async def create_rule(r: RuleReq):
    rid = str(uuid.uuid4())[:12]
    with _DB_LOCK:
        conn = _db()
        conn.execute("""
            INSERT INTO rules
            (id,name,enabled,slot_id,event_type,dest_ids,node_ids,channel_index,
             direction,keywords_include,keywords_exclude,min_hops,max_hops,
             dnd_enabled,dnd_days,dnd_start,dnd_end,rate_limit_n,rate_limit_window_min,
             priority,title_tmpl,body_tmpl,offline_threshold_min,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (rid, r.name, r.enabled, r.slot_id, r.event_type,
              json.dumps(r.dest_ids), json.dumps(r.node_ids),
              r.channel_index, r.direction,
              json.dumps(r.keywords_include), json.dumps(r.keywords_exclude),
              r.min_hops, r.max_hops,
              r.dnd_enabled, r.dnd_days, r.dnd_start, r.dnd_end,
              r.rate_limit_n, r.rate_limit_window_min,
              r.priority, r.title_tmpl, r.body_tmpl,
              r.offline_threshold_min, time.time()))
        conn.commit()
        conn.close()
    return {"id": rid, "status": "created"}


@plugin_router.patch("/rules/{rid}")
async def update_rule(rid: str, r: RuleReq):
    with _DB_LOCK:
        conn = _db()
        conn.execute("""
            UPDATE rules SET
            name=?,enabled=?,slot_id=?,event_type=?,dest_ids=?,node_ids=?,
            channel_index=?,direction=?,keywords_include=?,keywords_exclude=?,
            min_hops=?,max_hops=?,dnd_enabled=?,dnd_days=?,dnd_start=?,dnd_end=?,
            rate_limit_n=?,rate_limit_window_min=?,priority=?,title_tmpl=?,body_tmpl=?,
            offline_threshold_min=?
            WHERE id=?
        """, (r.name, r.enabled, r.slot_id, r.event_type,
              json.dumps(r.dest_ids), json.dumps(r.node_ids),
              r.channel_index, r.direction,
              json.dumps(r.keywords_include), json.dumps(r.keywords_exclude),
              r.min_hops, r.max_hops,
              r.dnd_enabled, r.dnd_days, r.dnd_start, r.dnd_end,
              r.rate_limit_n, r.rate_limit_window_min,
              r.priority, r.title_tmpl, r.body_tmpl,
              r.offline_threshold_min, rid))
        conn.commit()
        conn.close()
    return {"status": "updated"}


@plugin_router.delete("/rules/{rid}")
async def delete_rule(rid: str):
    with _DB_LOCK:
        conn = _db()
        conn.execute("DELETE FROM rules WHERE id=?", (rid,))
        conn.commit()
        conn.close()
    return {"status": "deleted"}


@plugin_router.post("/rules/{rid}/test")
async def test_rule(rid: str):
    """Fire the rule immediately with dummy context — tests destinations work."""
    if not APPRISE_AVAILABLE:
        raise HTTPException(503, "Apprise library not installed")
    rules = _load_all("rules")
    rule  = next((r for r in rules if r["id"] == rid), None)
    if not rule:
        raise HTTPException(404, "Rule not found")
    ctx = _build_ctx("TEST_NODE", {
        "user": {"longName": "Test Node", "shortName": "TEST"},
    }, rule["slot_id"], event_type=rule["event_type"], msg="This is a test message from MeshDash")
    await _fire_rule(rule, ctx)
    return {"status": "fired"}


# ── Delivery log ──────────────────────────────────────────────────────────────

@plugin_router.get("/log")
async def delivery_log(limit: int = 100):
    with _DB_LOCK:
        conn = _db()
        cur  = conn.execute("SELECT * FROM delivery_log ORDER BY ts DESC LIMIT ?", (limit,))
        rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
        conn.close()
    return {"log": rows, "count": len(rows)}


@plugin_router.delete("/log")
async def clear_log():
    with _DB_LOCK:
        conn = _db()
        conn.execute("DELETE FROM delivery_log")
        conn.commit()
        conn.close()
    return {"status": "cleared"}


# ── Status ────────────────────────────────────────────────────────────────────

@plugin_router.get("/status")
async def status():
    rules_all = _load_all("rules")
    dests_all = _load_all("destinations")
    with _DB_LOCK:
        conn = _db()
        total_fires  = conn.execute("SELECT COUNT(*) FROM delivery_log").fetchone()[0]
        total_failed = conn.execute("SELECT COUNT(*) FROM delivery_log WHERE success=0").fetchone()[0]
        conn.close()
    return {
        "apprise_installed": APPRISE_AVAILABLE,
        "rules_total":   len(rules_all),
        "rules_active":  sum(1 for r in rules_all if r["enabled"]),
        "dests_total":   len(dests_all),
        "dests_active":  sum(1 for d in dests_all if d["enabled"]),
        "total_fires":   total_fires,
        "total_failed":  total_failed,
    }


# ── Nodes (for picker) ────────────────────────────────────────────────────────

@plugin_router.get("/nodes/{slot_id}")
async def nodes_for_picker(slot_id: str):
    slot = _node_registry.get(slot_id)
    if not slot:
        return {"nodes": []}
    result = []
    for nid, nd in slot.meshtastic_data.nodes.items():
        u = nd.get("user") or {}
        result.append({
            "node_id":   nid,
            "long_name": u.get("longName") or nd.get("long_name") or nid,
            "last_heard": nd.get("lastHeard") or 0,
        })
    result.sort(key=lambda n: -(n["last_heard"] or 0))
    return {"nodes": result}