"""
Mesh Ping Plugin — v1.1.0
==========================
Active RTT ping sessions via UI launch or DM trigger command.

Each ping sends a readable DM: "PING 1/5 (every 5s) — awaiting ACK"
The target node sees exactly what is happening if they read the messages.

DM trigger (sent as DM to this node):
  ping 5          → 5 pings, 5s apart
  ping 10 3       → 10 pings, 3s apart

Sends summary DM on completion:
  ✓ PING 5/5 | RTT avg=1.8s min=1.2s max=3.1s
  ✗ PING 3/5 | RTT avg=2.4s min=1.8s max=3.9s | loss=40%

Config (persisted to SQLite):
  dm_trigger_enabled, dm_trigger_word, default_count, default_interval,
  max_count, max_interval, summary_dm_enabled, channel_index
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pubsub import pub

# Lazy import — meshtastic_dashboard may not be fully loaded at import time
_broadcast_data = None
def _get_broadcast():
    global _broadcast_data
    if _broadcast_data is None:
        try:
            from meshtastic_dashboard import broadcast_data as _bd
            _broadcast_data = _bd
        except Exception:
            pass
    return _broadcast_data

logger        = logging.getLogger("plugin.mesh_ping")
plugin_router = APIRouter()

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(PLUGIN_DIR, "mesh_ping.db")
_DB_LOCK = threading.Lock()

_DEFAULTS = {
    "dm_trigger_enabled":  True,
    "dm_trigger_word":     "ping",
    "default_count":       5,
    "default_interval":    5.0,      # ← 5 seconds default
    "max_count":           20,
    "max_interval":        30.0,
    "summary_dm_enabled":  True,
    "channel_index":       0,
}

def _load_config() -> dict:
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            row = conn.execute("SELECT value FROM config WHERE key='settings'").fetchone()
            conn.close()
        if row:
            saved = json.loads(row[0])
            cfg   = dict(_DEFAULTS)
            cfg.update(saved)
            return cfg
    except Exception as e:
        logger.warning("Config load failed: %s", e)
    return dict(_DEFAULTS)

def _save_config(cfg: dict):
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT OR REPLACE INTO config (key,value) VALUES ('settings',?)",
                         (json.dumps(cfg),))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Config save failed: %s", e)

_cfg: dict = {}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_node_registry: Dict[str, Any] = {}
_event_loop:    Optional[asyncio.AbstractEventLoop] = None
_sessions:      Dict[str, dict] = {}
_history:       List[dict]      = []
_sessions_lock  = threading.Lock()
_MAX_HISTORY    = 100

# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def init_plugin(context: dict):
    global _node_registry, _event_loop, _cfg
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    _cfg           = _load_config()
    logger.info("Mesh Ping v1.1.0 — dm_trigger=%s word='%s' default_interval=%.1fs",
                _cfg["dm_trigger_enabled"], _cfg["dm_trigger_word"],
                _cfg["default_interval"])
    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
    try:
        pub.subscribe(_on_receive, "meshtastic.receive")
    except Exception as e:
        logger.error("pub.subscribe: %s", e)
    if _event_loop:
        try:
            asyncio.run_coroutine_threadsafe(_watchdog(context), _event_loop)
        except Exception as e:
            logger.error("Watchdog: %s", e)


async def _watchdog(context):
    wd, pid = context.get("plugin_watchdog"), context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Ping message builder
# ---------------------------------------------------------------------------

def _ping_text(index: int, count: int, interval: float) -> str:
    """
    Build a human-readable ping DM that explains itself if the target reads it.
    Example: "PING 2/5 (5s interval) — awaiting ACK #2"
    Kept under 80 chars to fit mesh DM limits comfortably.
    """
    iv_str = f"{interval:.0f}s" if interval == int(interval) else f"{interval:.1f}s"
    return f"PING {index}/{count} (every {iv_str}) \u2014 pls ignore, RTT test"

# ---------------------------------------------------------------------------
# Incoming packet handler
# ---------------------------------------------------------------------------

def _on_receive(packet, interface=None):
    try:
        decoded = packet.get("decoded", {})
        portnum = str(decoded.get("portnum", ""))
        from_id = packet.get("fromId") or packet.get("from_id") or ""
        to_id   = packet.get("toId")   or packet.get("to_id")   or ""

        # ── DM trigger ────────────────────────────────────────────────────────
        if _cfg.get("dm_trigger_enabled") and "TEXT_MESSAGE" in portnum:
            text = decoded.get("text", "").strip()
            word = _cfg.get("dm_trigger_word", "ping")
            pat  = re.compile(
                r'^\s*' + re.escape(word) + r'\s+(\d+)(?:\s+(\d+(?:\.\d+)?))?\s*$',
                re.IGNORECASE,
            )
            m = pat.match(text)
            if m:
                local_id = _local_id_for_iface(interface)
                if local_id and (to_id == local_id or to_id == "^all"):
                    count    = min(int(m.group(1)), int(_cfg.get("max_count", 20)))
                    interval = float(m.group(2)) if m.group(2) else float(_cfg.get("default_interval", 5.0))
                    interval = max(1.0, min(float(_cfg.get("max_interval", 30.0)), interval))
                    count    = max(1, count)
                    slot_id  = _slot_for_iface(interface)
                    if _event_loop:
                        asyncio.run_coroutine_threadsafe(
                            _start_session(from_id, count, interval, slot_id, "dm", from_id),
                            _event_loop,
                        )

        # ── ACK / response matching ───────────────────────────────────────────
        if "ROUTING" in portnum or "NODEINFO" in portnum:
            decoded2 = packet.get("decoded", {})
            req_id   = decoded2.get("requestId") or decoded2.get("request_id")
            routing  = decoded2.get("routing", {})
            error    = (routing.get("errorReason", "NONE") if routing else "NONE") or "NONE"

            with _sessions_lock:
                for sess in _sessions.values():
                    if sess.get("status") != "running":
                        continue
                    for att in sess.get("attempts", []):
                        if att.get("status") != "pending":
                            continue
                        matched = (
                            (req_id and att.get("packet_id") == req_id)
                            or ("NODEINFO" in portnum and from_id == sess.get("target_id"))
                        )
                        if matched:
                            rtt            = time.time() - att["sent_at"]
                            att["status"]  = "nak" if error not in ("NONE", "") else "ok"
                            att["rtt"]     = round(rtt, 3)
                            att["error"]   = error if error not in ("NONE", "") else None
                            att["recv_at"] = time.time()
                            if _event_loop:
                                asyncio.run_coroutine_threadsafe(
                                    _broadcast(sess["id"]), _event_loop
                                )
                            break

    except Exception as e:
        logger.debug("_on_receive: %s", e)


def _local_id_for_iface(_iface) -> Optional[str]:
    # meshtastic.receive topic delivers only (packet) — no interface
    # Fall back to first available node's local ID
    for slot in _node_registry.values():
        lid = getattr(slot.meshtastic_data, "local_node_id", None)
        if lid:
            return lid
    return None

def _slot_for_iface(_iface) -> str:
    # meshtastic.receive topic delivers only (packet) — no interface
    # Fall back to first available slot
    for sid in _node_registry:
        return sid
    return "node_0"

# ---------------------------------------------------------------------------
# Session engine
# ---------------------------------------------------------------------------

async def _start_session(target_id, count, interval, slot_id,
                          triggered_by="ui", requester_id=None) -> str:
    sid = str(uuid.uuid4())[:8]
    sess = {
        "id":           sid,
        "target_id":    target_id,
        "slot_id":      slot_id,
        "count":        count,
        "interval":     interval,
        "triggered_by": triggered_by,
        "requester_id": requester_id,
        "status":       "running",
        "started_at":   time.time(),
        "ended_at":     None,
        "current":      0,
        "attempts":     [],
    }
    with _sessions_lock:
        _sessions[sid] = sess
    await _broadcast(sid)
    asyncio.create_task(_run_session(sid))
    return sid


async def _run_session(sid: str):
    with _sessions_lock:
        sess = _sessions.get(sid)
        if not sess:
            return
        target   = sess["target_id"]
        slot_id  = sess["slot_id"]
        count    = sess["count"]
        interval = sess["interval"]

    # ACK timeout: wait up to min(interval, 15s) for each ping
    ack_timeout = min(interval, 15.0)
    # Gap between sending next ping = interval (full gap, independent of ACK time)
    # We track when each ping was sent and sleep until interval has elapsed.

    for i in range(count):
        with _sessions_lock:
            if _sessions.get(sid, {}).get("status") == "stopped":
                break

        ping_text = _ping_text(i + 1, count, interval)
        att = {
            "index":      i + 1,
            "status":     "pending",
            "sent_at":    time.time(),
            "rtt":        None,
            "packet_id":  None,
            "error":      None,
            "sent_text":  ping_text,
        }
        with _sessions_lock:
            _sessions[sid]["attempts"].append(att)
            _sessions[sid]["current"] = i + 1

        # ── Send the ping DM ──────────────────────────────────────────────
        send_time = time.time()
        pkt_id    = None
        send_ok   = False
        try:
            slot = _node_registry.get(slot_id)
            cm   = getattr(slot, "connection_manager", None)
            if cm and cm.interface and cm.is_ready.is_set():
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        cm.interface.sendText,
                        ping_text,
                        destinationId=target,
                        wantAck=True,
                        channelIndex=int(_cfg.get("channel_index", 0)),
                    ),
                    timeout=8.0,
                )
                if result:
                    pkt_id  = getattr(result, "id", None) or \
                              (isinstance(result, dict) and result.get("id"))
                    send_ok = True
            else:
                logger.warning("Ping %d/%d: radio not ready (slot=%s)", i+1, count, slot_id)
        except asyncio.TimeoutError:
            logger.warning("Ping %d/%d: sendText timed out", i+1, count)
        except Exception as e:
            logger.warning("Ping %d/%d failed: %r", i+1, count, e)

        # Update attempt with packet_id (and mark send_failed if radio was down)
        with _sessions_lock:
            a = _sessions[sid]["attempts"][-1]
            a["packet_id"] = pkt_id
            if not send_ok:
                a["status"] = "nak"
                a["error"]  = "send_failed"

        await _broadcast(sid)

        # ── Wait for ACK ─────────────────────────────────────────────────────
        if send_ok:
            deadline = time.time() + ack_timeout
            while time.time() < deadline:
                with _sessions_lock:
                    status = _sessions[sid]["attempts"][-1]["status"]
                if status != "pending":
                    break
                await asyncio.sleep(0.1)

            # Timeout if no ACK arrived
            with _sessions_lock:
                if _sessions[sid]["attempts"][-1]["status"] == "pending":
                    _sessions[sid]["attempts"][-1]["status"] = "timeout"

            await _broadcast(sid)

        # ── Wait for the rest of the interval before sending next ping ────────
        # Always wait a full `interval` seconds from send_time before next ping.
        # This means the total session time = count × interval (predictable).
        if i < count - 1:
            elapsed   = time.time() - send_time
            remaining = interval - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

    # ── Finalise ─────────────────────────────────────────────────────────────
    with _sessions_lock:
        s = _sessions.get(sid, {})
        if s.get("status") == "running":
            s["status"] = "done"
        s["ended_at"] = time.time()

    await _broadcast(sid)

    # Summary DM
    with _sessions_lock:
        s = dict(_sessions.get(sid, {}))
    if _cfg.get("summary_dm_enabled") and s.get("triggered_by") == "dm" \
            and s.get("requester_id"):
        asyncio.create_task(_send_summary(s))

    # Move to history
    with _sessions_lock:
        if sid in _sessions:
            done = _sessions.pop(sid)
            _history.insert(0, done)
            if len(_history) > _MAX_HISTORY:
                _history.pop()

    # Final broadcast from history
    await _broadcast(sid)


async def _send_summary(sess: dict):
    atts     = sess.get("attempts", [])
    ok       = [a for a in atts if a["status"] == "ok"]
    rtts     = [a["rtt"] for a in ok if a["rtt"] is not None]
    total    = len(atts)
    ok_n     = len(ok)
    loss_pct = round((1 - ok_n / max(total, 1)) * 100)

    parts = [f"PING {ok_n}/{total}"]
    if rtts:
        avg_r = sum(rtts) / len(rtts)
        parts.append(f"RTT avg={avg_r:.1f}s min={min(rtts):.1f}s max={max(rtts):.1f}s")
    if loss_pct > 0:
        parts.append(f"loss={loss_pct}%")
    nak_n = len([a for a in atts if a["status"] == "nak"])
    if nak_n:
        parts.append(f"NAK={nak_n}")

    icon = "\u2713" if loss_pct == 0 else "\u2717" if loss_pct == 100 else "\u26a0"
    text = f"{icon} {' | '.join(parts)}"

    try:
        slot = _node_registry.get(sess.get("slot_id", "node_0"))
        cm   = getattr(slot, "connection_manager", None)
        if cm:
            await cm.sendText(
                text,
                destinationId=sess["requester_id"],
                channelIndex=int(_cfg.get("channel_index", 0)),
                wantAck=False,
            )
            logger.info("Summary DM → %s: %s", sess["requester_id"], text)
    except Exception as e:
        logger.error("Summary DM: %s", e)


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------

async def _broadcast(sid: str):
    try:
        bd = _get_broadcast()
        if bd is None:
            logger.warning("broadcast_data not available yet for sid=%s", sid)
            return
        with _sessions_lock:
            sess = _sessions.get(sid) or \
                   next((s for s in _history if s["id"] == sid), None)
        if sess:
            await bd(
                {"event": "mesh_ping_update", "data": _safe(sess)},
                slot_id=sess.get("slot_id", "node_0"),
            )
    except Exception as e:
        logger.error("_broadcast error: %s", e, exc_info=True)


def _safe(sess: dict) -> dict:
    return {
        "id":           sess.get("id"),
        "target_id":    sess.get("target_id"),
        "slot_id":      sess.get("slot_id"),
        "count":        sess.get("count"),
        "interval":     sess.get("interval"),
        "status":       sess.get("status"),
        "started_at":   sess.get("started_at"),
        "ended_at":     sess.get("ended_at"),
        "triggered_by": sess.get("triggered_by"),
        "requester_id": sess.get("requester_id"),
        "current":      sess.get("current", 0),
        "attempts": [
            {
                "index":     a.get("index"),
                "status":    a.get("status"),
                "rtt":       a.get("rtt"),
                "error":     a.get("error"),
                "sent_at":   a.get("sent_at"),
                "sent_text": a.get("sent_text", ""),
            }
            for a in sess.get("attempts", [])
        ],
    }

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@plugin_router.get("")
@plugin_router.get("/")
async def health():
    with _sessions_lock:
        active = len(_sessions)
        hist   = len(_history)
    return {"plugin": "mesh_ping", "version": "1.1.0", "status": "running",
            "active_sessions": active, "history_count": hist}


class StartReq(BaseModel):
    target_id: str
    slot_id:   str   = "node_0"
    count:     int   = Field(5,   ge=1, le=20)
    interval:  float = Field(5.0, ge=1, le=30)   # ← default 5s


@plugin_router.post("/start")
async def start_ping(r: StartReq):
    sid = await _start_session(
        r.target_id, r.count, r.interval, r.slot_id, "ui"
    )
    return {"session_id": sid, "status": "started"}


@plugin_router.post("/stop/{session_id}")
async def stop_ping(session_id: str):
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            raise HTTPException(404, "Session not found or already complete")
        sess["status"] = "stopped"
    return {"session_id": session_id, "status": "stopped"}


@plugin_router.get("/sessions")
async def get_sessions():
    with _sessions_lock:
        active = [_safe(s) for s in _sessions.values()]
        hist   = [_safe(s) for s in _history]
    return {"active": active, "history": hist}


@plugin_router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    with _sessions_lock:
        sess = _sessions.get(session_id) or \
               next((s for s in _history if s["id"] == session_id), None)
    if not sess:
        raise HTTPException(404, "Session not found")
    return _safe(sess)


@plugin_router.delete("/history")
async def clear_history():
    with _sessions_lock:
        _history.clear()
    return {"status": "cleared"}


# ── Config ────────────────────────────────────────────────────────────────────

@plugin_router.get("/config")
async def get_config():
    return dict(_cfg)


class ConfigBody(BaseModel):
    dm_trigger_enabled: Optional[bool]  = None
    dm_trigger_word:    Optional[str]   = None
    default_count:      Optional[int]   = Field(None, ge=1, le=20)
    default_interval:   Optional[float] = Field(None, ge=1, le=30)
    max_count:          Optional[int]   = Field(None, ge=1, le=20)
    max_interval:       Optional[float] = Field(None, ge=1, le=30)
    summary_dm_enabled: Optional[bool]  = None
    channel_index:      Optional[int]   = Field(None, ge=0, le=7)


@plugin_router.post("/config")
async def set_config(body: ConfigBody):
    global _cfg
    data = body.model_dump(exclude_none=True)
    if "dm_trigger_word" in data:
        w = data["dm_trigger_word"].strip().lower()
        if not w or not w.isalpha():
            raise HTTPException(400, "Trigger word must be letters only")
        data["dm_trigger_word"] = w
    _cfg.update(data)
    _save_config(_cfg)
    return {"status": "saved", "config": _cfg}


# ── Nodes ─────────────────────────────────────────────────────────────────────

@plugin_router.get("/nodes/{slot_id}")
async def list_nodes(slot_id: str):
    slot = _node_registry.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    local_id = getattr(slot.meshtastic_data, "local_node_id", None)
    nodes = []
    for nid, nd in slot.meshtastic_data.nodes.items():
        if nid == local_id:
            continue
        u = nd.get("user") or {}
        nodes.append({
            "node_id":    nid,
            "long_name":  u.get("longName")  or nd.get("long_name")  or nid,
            "short_name": u.get("shortName") or nd.get("short_name") or nid[-4:],
            "last_heard": nd.get("lastHeard") or nd.get("last_heard") or 0,
            "snr":        nd.get("snr"),
        })
    nodes.sort(key=lambda n: -(n["last_heard"] or 0))
    return {"slot_id": slot_id, "nodes": nodes}