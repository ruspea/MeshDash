"""
Auto-Responder Plugin for MeshDash
==================================
• Listens for incoming Direct Messages (DMs).
• Replies with a configured response.
• Uses an async queue to prevent blocking the radio thread.
• Implements a 5-minute cooldown per node to prevent infinite ping-pong loops.
"""

import asyncio
import logging
import os
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from pubsub import pub

core_context: dict = {}
plugin_router = APIRouter()

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "responder_config.db")
_db_conn: Optional[sqlite3.Connection] = None

_dm_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None

_cooldowns: dict = {}
COOLDOWN_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Database Helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                message TEXT    NOT NULL DEFAULT 'Auto-reply: I am currently away from my radio.'
            )
        """)
        _db_conn.commit()
        row = _db_conn.execute("SELECT * FROM config WHERE id=1").fetchone()
        if not row:
            _db_conn.execute(
                "INSERT INTO config (id, enabled, message) "
                "VALUES (1, 0, 'Auto-reply: I am currently away from my radio.')"
            )
            _db_conn.commit()
    return _db_conn


def _get_config() -> dict:
    row = _get_db().execute("SELECT * FROM config WHERE id=1").fetchone()
    return dict(row) if row else {"enabled": 0, "message": ""}


def _update_config(enabled: int, message: str):
    conn = _get_db()
    conn.execute("UPDATE config SET enabled=?, message=? WHERE id=1", (enabled, message))
    conn.commit()


# ---------------------------------------------------------------------------
# Pubsub Callback  (sync — runs on the meshtastic radio thread)
# ---------------------------------------------------------------------------

def _on_receive(packet, interface=None):
    logger = core_context.get("logger") or logging.getLogger("auto_responder")
    try:
        # ── Log every packet so we can see what fields are actually present ──
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum") if isinstance(decoded, dict) else None
        logger.debug(
            f"auto_responder: rx packet keys={list(packet.keys())} "
            f"portnum={portnum!r} to={packet.get('to')!r} toId={packet.get('toId')!r} "
            f"from={packet.get('from')!r} fromId={packet.get('fromId')!r}"
        )

        if not isinstance(decoded, dict):
            return

        # portnum may be the string "TEXT_MESSAGE_APP" (post-processed packet)
        # or the integer 1 (raw packet) — accept both
        if portnum not in ("TEXT_MESSAGE_APP", 1):
            return

        # ── Resolve from_id ──────────────────────────────────────────────────
        from_id = packet.get("fromId")
        if not from_id:
            raw_from = packet.get("from")
            if isinstance(raw_from, int):
                from_id = f"!{raw_from:08x}"
        if not from_id:
            logger.debug("auto_responder: could not resolve from_id, dropping")
            return

        # ── Resolve to_id ────────────────────────────────────────────────────
        to_id = packet.get("toId")
        if not to_id:
            raw_to = packet.get("to")
            if isinstance(raw_to, int):
                to_id = "^all" if raw_to == 0xFFFFFFFF else f"!{raw_to:08x}"
        if not to_id:
            logger.debug("auto_responder: could not resolve to_id, dropping")
            return

        logger.debug(f"auto_responder: text packet from={from_id} to={to_id}")

        # ── Must not be a broadcast ──────────────────────────────────────────
        if to_id == "^all":
            logger.debug("auto_responder: broadcast, ignoring")
            return

        # ── Must be addressed to our local node ─────────────────────────────
        meshtastic_data = core_context.get("meshtastic_data")
        local_node_id = getattr(meshtastic_data, "local_node_id", None) if meshtastic_data else None

        if not local_node_id:
            logger.warning("auto_responder: local_node_id not known yet, cannot check DM target")
            return

        logger.debug(f"auto_responder: local_node_id={local_node_id} to_id={to_id}")

        if to_id != local_node_id:
            logger.debug(f"auto_responder: packet not for us ({to_id} != {local_node_id}), ignoring")
            return

        # ── Don't reply to ourselves ─────────────────────────────────────────
        if from_id == local_node_id:
            logger.debug("auto_responder: packet from ourselves, ignoring")
            return

        logger.info(f"auto_responder: DM detected from {from_id}, queueing reply")

        # ── Hand off to async worker ─────────────────────────────────────────
        loop  = core_context.get("event_loop")
        queue = _dm_queue
        if loop is None or queue is None:
            logger.warning("auto_responder: event_loop or queue not ready yet")
            return

        if loop.is_running():
            try:
                loop.call_soon_threadsafe(queue.put_nowait, from_id)
            except asyncio.QueueFull:
                logger.warning("auto_responder: DM queue full, dropping reply request")
        else:
            logger.warning("auto_responder: event loop not running")

    except Exception as exc:
        logger.error(f"auto_responder _on_receive error: {exc}", exc_info=True)


# ---------------------------------------------------------------------------
# Async Worker
# ---------------------------------------------------------------------------

async def _responder_worker():
    logger = core_context.get("logger") or logging.getLogger("auto_responder")
    logger.info("auto_responder: worker started")

    while True:
        sender_id = None
        try:
            sender_id = await _dm_queue.get()
            logger.info(f"auto_responder: worker processing reply to {sender_id}")

            config = await asyncio.to_thread(_get_config)
            if not config.get("enabled"):
                logger.info(f"auto_responder: disabled, skipping reply to {sender_id}")
                continue

            now = time.time()
            if (now - _cooldowns.get(sender_id, 0)) < COOLDOWN_SECONDS:
                logger.info(f"auto_responder: {sender_id} in cooldown, skipping")
                continue

            cm = core_context.get("connection_manager")
            if cm is None:
                logger.warning("auto_responder: connection_manager not in context")
                continue

            is_ready = getattr(cm, "is_ready", None)
            if is_ready is not None and not is_ready.is_set():
                logger.warning("auto_responder: radio not ready, cannot send reply")
                continue

            reply_text = config.get("message") or "Auto-reply: Unavailable."
            logger.info(f"auto_responder: sending reply to {sender_id}: {reply_text!r}")

            await cm.sendText(
                reply_text,
                destinationId=sender_id,
                channelIndex=0,
                wantAck=True,  # Request ACK so the message is relayed over RF via mesh
            )
            _cooldowns[sender_id] = now
            logger.info(f"auto_responder: reply sent successfully to {sender_id}")

        except asyncio.CancelledError:
            logger.info("auto_responder: worker stopped")
            break
        except Exception as exc:
            logger.error(f"auto_responder: worker error: {exc}", exc_info=True)
        finally:
            if sender_id is not None:
                try:
                    _dm_queue.task_done()
                except Exception:
                    pass


async def _watchdog_heartbeat() -> None:
    """
    Pings the MeshDash core watchdog every 30 s.
    Required because manifest.json has "watchdog": true.
    Without this the core marks the plugin as 'hung' after 120 s of silence.
    """
    logger = core_context.get("logger") or logging.getLogger("auto_responder")
    wd  = core_context.get("plugin_watchdog")
    pid = core_context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("auto_responder: watchdog heartbeat stopped")
            return
        except Exception as e:
            logger.warning("auto_responder: watchdog error: %s", e)


# ---------------------------------------------------------------------------
# Plugin Lifecycle
# ---------------------------------------------------------------------------

async def _setup():
    global _dm_queue, _worker_task
    logger = core_context.get("logger") or logging.getLogger("auto_responder")
    _dm_queue = asyncio.Queue(maxsize=100)
    _worker_task = asyncio.create_task(_responder_worker())
    _worker_task.set_name("auto_responder_worker")
    # Watchdog heartbeat — started here so it runs on the same event loop
    wd_task = asyncio.create_task(_watchdog_heartbeat())
    wd_task.set_name("auto_responder_watchdog")
    logger.info("auto_responder: queue, worker task and watchdog heartbeat created")


def init_plugin(context: dict):
    core_context.update(context)
    logger = core_context.get("logger") or logging.getLogger("auto_responder")

    _get_db()

    loop = core_context.get("event_loop")
    if loop is None:
        logger.error("auto_responder: no event_loop in context — plugin cannot start")
        return

    # Fire-and-forget — do NOT block with .result() from this thread
    asyncio.run_coroutine_threadsafe(_setup(), loop)

    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
    pub.subscribe(_on_receive, "meshtastic.receive")
    logger.info("auto_responder: subscribed to meshtastic.receive, init complete")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@plugin_router.get("/config")
async def get_config():
    return await asyncio.to_thread(_get_config)


@plugin_router.post("/config")
async def save_config(body: dict = Body(...)):
    enabled = int(body.get("enabled", 0))
    message = str(body.get("message", "")).strip()

    if not message:
        raise HTTPException(400, "Message cannot be empty.")

    await asyncio.to_thread(_update_config, enabled, message)
    return {"status": "success", "enabled": enabled, "message": message}