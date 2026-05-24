"""
Push Notifications Plugin — Backend v2.1

Architecture:
  - bridge.html hooks PluginBridge.onPacket() (SSE stream, real-time)
  - Bridge evaluates all notification rules in the browser
  - Bridge POSTs to /notify when a push should fire
  - Backend applies quiet-hours gate, VAPID-signs and dispatches
  - Bridge also POSTs to /log so the plugin page shows a live packet log
"""

import os
import time
import json
import logging
import asyncio
import sqlite3
import base64
import threading
import contextlib
from collections import deque
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("plugin.push_notifications")
plugin_router = APIRouter()

try:
    from pywebpush import webpush, WebPushException
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    HAS_WEBPUSH = True
except ImportError:
    HAS_WEBPUSH = False
    logger.warning("pywebpush/cryptography missing. Run: pip install pywebpush cryptography --break-system-packages")

DB_PATH   = os.path.join(os.path.dirname(__file__), "push_config.db")
_db_lock  = threading.Lock()
_node_registry: Dict[str, Any] = {}

_live_log: deque = deque(maxlen=200)
_live_log_lock   = threading.Lock()

_config: Dict[str, Any] = {
    # Master switch
    "enabled": True,

    # VAPID contact — MUST be your real server URL or real email.
    # Apple APNs strictly validates this. Use https://yourdomain.com or mailto:you@example.com
    "vapid_contact": "https://example.com",

    # Message types
    "notify_dms":          True,   # direct messages to our node
    "notify_broadcasts":   False,  # every channel broadcast (spam mode)
    "notify_channels":     [],     # specific channel indices; [] = all when broadcasts on
    "notify_on_keyword":   True,   # keyword match in broadcasts
    "keywords":            ["sos", "help", "admin", "emergency", "mayday"],

    # Non-message packet types
    "notify_position":     False,  # any position update from a node
    "notify_telemetry":    False,  # telemetry packets (battery, metrics)
    "notify_node_online":  False,  # node comes online (heard for first time this session)
    "notify_traceroute":   False,  # traceroute results
    "notify_paxcounter":   False,  # pax counter detections
    "notify_detection":    False,  # detection sensor events
    "notify_waypoint":     False,  # new waypoints received

    # Threshold filters (only notify if value crosses these)
    "telemetry_battery_threshold": 20,   # alert if battery below this %
    "telemetry_any":               False, # notify every telemetry (ignores threshold)

    # Quiet hours
    "quiet_hours_enabled": False,
    "quiet_start":         "22:00",
    "quiet_end":           "08:00",
    "dm_override_quiet":   True,   # DMs still fire during quiet hours
}
_config_lock = threading.Lock()
_last_push_result: dict = {}


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _db():
    """Context manager: get a DB connection, auto-close on exit or exception."""
    conn = _get_db()
    try:
        yield conn
    finally:
        conn.close()


def _init_db() -> None:
    with _db_lock:
        with _db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS vapid_keys
                (id INTEGER PRIMARY KEY, public_key TEXT, private_key TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS subscriptions
                (endpoint TEXT PRIMARY KEY, keys_json TEXT, device_name TEXT, created_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS config_store
                (key TEXT PRIMARY KEY, value TEXT)""")
            conn.commit()

            row = conn.execute("SELECT value FROM config_store WHERE key='settings'").fetchone()
            if row:
                try:
                    with _config_lock:
                        _config.update(json.loads(row["value"]))
                except Exception as e:
                    logger.warning("Push: config load error: %s", e)

            if HAS_WEBPUSH:
                if not conn.execute("SELECT id FROM vapid_keys WHERE id=1").fetchone():
                    priv, pub = _gen_vapid()
                    conn.execute("INSERT INTO vapid_keys (id, public_key, private_key) VALUES (1,?,?)", (pub, priv))
                    conn.commit()
                    logger.info("Push: VAPID keys generated.")


def _gen_vapid():
    pk  = ec.generate_private_key(ec.SECP256R1())
    pub = pk.public_key()
    pb  = pk.private_numbers().private_value.to_bytes(32, "big")
    ub  = pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return (base64.urlsafe_b64encode(pb).decode().rstrip("="),
            base64.urlsafe_b64encode(ub).decode().rstrip("="))


def _vapid_pair():
    with _db_lock:
        with _db() as conn:
            row = conn.execute("SELECT public_key, private_key FROM vapid_keys WHERE id=1").fetchone()
        return (row["public_key"], row["private_key"]) if row else (None, None)


def _is_quiet_hours() -> bool:
    with _config_lock:
        cfg = dict(_config)
    if not cfg.get("quiet_hours_enabled"):
        return False
    try:
        now = time.localtime()
        cur = now.tm_hour * 60 + now.tm_min
        sh, sm = map(int, cfg["quiet_start"].split(":"))
        eh, em = map(int, cfg["quiet_end"].split(":"))
        s, e = sh*60+sm, eh*60+em
        return (cur >= s or cur <= e) if s > e else (s <= cur <= e)
    except Exception:
        return False


def _dispatch(payload: dict) -> int:
    if not HAS_WEBPUSH:
        return 0
    pub, priv = _vapid_pair()
    if not priv:
        return 0

    sent, dead = 0, []
    with _db_lock:
        with _db() as conn:
            subs = conn.execute("SELECT * FROM subscriptions").fetchall()

    with _config_lock:
        contact = _config.get("vapid_contact", "https://example.com").strip() or "https://example.com"
    if not contact.startswith(("mailto:", "https://", "http://")):
        contact = "https://" + contact

    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": json.loads(sub["keys_json"])},
                data=json.dumps(payload),
                vapid_private_key=priv,
                vapid_claims={"sub": contact, "ttl": 86400},
            )
            sent += 1
        except WebPushException as ex:
            code = getattr(ex.response, "status_code", None) if ex.response else None
            body = ""
            try:
                body = ex.response.text if ex.response else str(ex)
            except Exception:
                body = str(ex)
            if code in (404, 410):
                logger.info("Push: expired subscription removed (HTTP %s)", code)
                dead.append(sub["endpoint"])
            else:
                logger.error("Push FAILED %s… HTTP %s: %s",
                             sub["endpoint"][:50], code, body[:300])
        except Exception as ex:
            logger.error("Push dispatch error: %s", ex)

    if dead:
        with _db_lock:
            with _db() as conn:
                conn.executemany("DELETE FROM subscriptions WHERE endpoint=?", [(ep,) for ep in dead])
                conn.commit()

    global _last_push_result
    _last_push_result = {
        "ts": time.time(),
        "sent": sent,
        "total": len(subs),
        "contact": contact,
        "dead_removed": len(dead),
    }
    return sent


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
            logger.info("Push Notifications watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("Push Notifications watchdog error: %s", e)


def init_plugin(context: dict) -> None:
    global _node_registry
    _node_registry = context.get("node_registry", {})
    _init_db()
    logger.info("Push Notifications v2.1 ready. webpush=%s", "OK" if HAS_WEBPUSH else "MISSING")

    # Launch watchdog heartbeat on the main event loop.
    # init_plugin runs inside a threading.Thread, so run_coroutine_threadsafe
    # is the only safe way to schedule a coroutine onto the running loop.
    loop = context.get("event_loop")
    if loop is None:
        logger.warning("Push Notifications: event_loop not in context — watchdog will not start.")
        return
    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("Push Notifications watchdog heartbeat started.")
    except Exception as e:
        logger.error("Push Notifications: could not start watchdog heartbeat: %s", e)


class ConfigModel(BaseModel):
    enabled:              bool       = True
    notify_dms:           bool       = True
    notify_broadcasts:    bool       = False
    notify_channels:      List[int]  = []
    notify_on_keyword:    bool       = True
    keywords:             List[str]  = ["sos", "help", "admin", "emergency", "mayday"]
    notify_position:      bool       = False
    notify_telemetry:     bool       = False
    notify_node_online:   bool       = False
    notify_traceroute:    bool       = False
    notify_paxcounter:    bool       = False
    notify_detection:     bool       = False
    notify_waypoint:      bool       = False
    telemetry_battery_threshold: int = Field(20, ge=0, le=100)
    telemetry_any:        bool       = False
    quiet_hours_enabled:  bool       = False
    quiet_start:          str        = "22:00"
    quiet_end:            str        = "08:00"
    dm_override_quiet:    bool       = True
    vapid_contact:        str        = "https://example.com"


class SubModel(BaseModel):
    endpoint:    str
    keys:        dict
    device_name: str = "Unknown"


class NotifyRequest(BaseModel):
    title:       str
    body:        str
    url:         str  = "/"
    tag:         str  = "meshdash"
    icon:        str  = "/static/icons/favicon.ico"
    is_dm:       bool = False
    packet_type: str  = ""
    from_id:     str  = ""
    channel:     int  = 0


class LogEntry(BaseModel):
    ts:          float
    packet_type: str
    from_id:     str  = ""
    to_id:       str  = ""
    channel:     int  = 0
    summary:     str  = ""
    notified:    bool = False
    reason:      str  = ""   # why notified or why skipped


@plugin_router.get("/status")
async def get_status():
    pub, _ = _vapid_pair()
    with _config_lock:
        en = _config.get("enabled", True)
    with _db_lock:
        with _db() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    return {
        "active":          HAS_WEBPUSH,
        "enabled":         en,
        "vapid_public":    pub,
        "subscriptions":   cnt,
        "quiet_hours_now": _is_quiet_hours(),
    }


@plugin_router.get("/config")
async def get_config():
    with _config_lock:
        return dict(_config)


@plugin_router.post("/config")
async def set_config(body: ConfigModel):
    with _config_lock:
        _config.update(body.dict())
    with _db_lock:
        with _db() as conn:
            conn.execute("REPLACE INTO config_store (key, value) VALUES ('settings',?)", (json.dumps(dict(_config)),))
            conn.commit()
    return {"status": "ok", **_config}


@plugin_router.get("/subscriptions")
async def list_subs():
    with _db_lock:
        with _db() as conn:
            rows = conn.execute("SELECT endpoint, device_name, created_at FROM subscriptions ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@plugin_router.post("/subscribe")
async def subscribe(sub: SubModel):
    with _db_lock:
        with _db() as conn:
            conn.execute("REPLACE INTO subscriptions (endpoint, keys_json, device_name, created_at) VALUES (?,?,?,?)",
                         (sub.endpoint, json.dumps(sub.keys), sub.device_name, time.time()))
            conn.commit()
    logger.info("Push: subscribed — %s", sub.device_name)
    return {"status": "ok"}


@plugin_router.delete("/unsubscribe")
async def unsubscribe(request: Request):
    data = await request.json()
    ep   = data.get("endpoint")
    if not ep:
        raise HTTPException(400, "endpoint required")
    with _db_lock:
        with _db() as conn:
            conn.execute("DELETE FROM subscriptions WHERE endpoint=?", (ep,))
            conn.commit()
    return {"status": "ok"}


@plugin_router.post("/notify")
async def notify(req: NotifyRequest):
    """Called by bridge.html when a packet matches notification rules."""
    with _config_lock:
        cfg = dict(_config)
    if not cfg.get("enabled"):
        return {"status": "skipped", "reason": "disabled"}
    if _is_quiet_hours() and not (req.is_dm and cfg.get("dm_override_quiet")):
        return {"status": "skipped", "reason": "quiet_hours"}

    payload = {
        "title": req.title,
        "body":  req.body,
        "url":   req.url,
        "tag":   req.tag,
        "icon":  req.icon,
        "badge": "/static/icons/favicon.ico",
        "packet_type": req.packet_type,
    }
    sent = await asyncio.to_thread(_dispatch, payload)
    logger.info("Push → %d subscriber(s): %s", sent, req.title)
    return {"status": "ok", "sent": sent}


@plugin_router.post("/log")
async def log_packet(entry: LogEntry):
    """Called by bridge.html for every evaluated packet — populates the live log."""
    with _live_log_lock:
        _live_log.appendleft(entry.dict())
    return {"status": "ok"}


@plugin_router.get("/log")
async def get_log(limit: int = 100):
    """Returns the last N log entries for the plugin page live feed."""
    with _live_log_lock:
        entries = list(_live_log)[:min(limit, 200)]
    return {"entries": entries, "total": len(_live_log)}


@plugin_router.delete("/log")
async def clear_log():
    with _live_log_lock:
        _live_log.clear()
    return {"status": "ok"}


@plugin_router.get("/debug")
async def push_debug():
    """Returns diagnostic info: VAPID contact in use, last push result, subscription count."""
    pub, _ = _vapid_pair()
    with _config_lock:
        contact = _config.get("vapid_contact", "https://example.com")
    with _db_lock:
        with _db() as conn:
            subs = conn.execute("SELECT endpoint, device_name FROM subscriptions").fetchall()
    return {
        "has_webpush":     HAS_WEBPUSH,
        "vapid_public":    pub,
        "vapid_contact":   contact,
        "subscriptions":   [{"device": s["device_name"], "endpoint_prefix": s["endpoint"][:60]} for s in subs],
        "last_push":       _last_push_result,
        "quiet_hours_now": _is_quiet_hours(),
    }


@plugin_router.post("/test")
async def test_push():
    sent = await asyncio.to_thread(_dispatch, {
        "title": "MeshDash ✓ Push Test",
        "body":  "Web push routing is operational.",
        "url":   "/",
        "tag":   "meshdash-test",
        "icon":  "/static/icons/favicon.ico",
        "badge": "/static/icons/favicon.ico",
    })
    return {"status": "ok", "sent": sent}