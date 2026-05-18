"""
Mesh BB — Bulletin Board System v1.0.0
=========================================================
Complete rewrite with:
  - Correct DM vs channel routing with channel reply (@mention prefix)
  - Per-channel permissions (active, reply_in_channel, announce_new_posts)
  - Per-node block list (block from DM, channel, or all)
  - Proper chunked send with ACK tracking and retry
  - Paginated list (5 posts/page, bb.room list 2)
  - Full conversation trace table (inbound + outbound, grouped by conv_id)
  - Public share URL (read-only, no auth)
  - Auto-announce scheduler (daily/weekly/interval cron)
  - Popularity tracking (access_count on rooms, read_count on posts)
  - bb popular / bb.room popular commands
"""

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from pubsub import pub

logger = logging.getLogger("plugin.mesh_bb")
plugin_router = APIRouter()

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh_bb.db")
_DB_LOCK = threading.Lock()
_context: Dict[str, Any] = {}

# Meshtastic hard limits
_MESH_MAX_BYTES   = 215   # conservative (228 theoretical - framing overhead)
_CHUNK_DELAY_S    = 2.8   # seconds between chunks
_ACK_TIMEOUT_S    = 12.0  # wait this long for ACK before retry
_ACK_MAX_RETRIES  = 2     # retries after first send = 3 total attempts
_POSTS_PER_PAGE   = 5     # bb.room list pagination

# ACK tracking: msg_id -> asyncio.Event
_ack_pending: Dict[str, asyncio.Event] = {}
_ack_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────
# Database init
# ─────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db():
    with _DB_LOCK:
        conn = _get_db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            description     TEXT DEFAULT '',
            channel_index   INTEGER DEFAULT 0,
            broadcast_new   INTEGER DEFAULT 1,
            ttl_hours       INTEGER DEFAULT 0,
            max_posts       INTEGER DEFAULT 100,
            locked          INTEGER DEFAULT 0,
            created_at      REAL NOT NULL,
            created_by      TEXT DEFAULT 'admin',
            post_count      INTEGER DEFAULT 0,
            access_count    INTEGER DEFAULT 0,
            last_accessed   REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            seq         INTEGER NOT NULL,
            author_id   TEXT NOT NULL,
            author_name TEXT DEFAULT 'Unknown',
            body        TEXT NOT NULL,
            created_at  REAL NOT NULL,
            expires_at  REAL DEFAULT NULL,
            pinned      INTEGER DEFAULT 0,
            deleted     INTEGER DEFAULT 0,
            read_count  INTEGER DEFAULT 0,
            UNIQUE(room_id, seq)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            node_id     TEXT NOT NULL,
            room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            created_at  REAL NOT NULL,
            PRIMARY KEY (node_id, room_id)
        );

        -- Per-channel permission config (one row per channel 0-7)
        CREATE TABLE IF NOT EXISTS channel_config (
            channel_index       INTEGER PRIMARY KEY,
            active              INTEGER DEFAULT 1,
            reply_in_channel    INTEGER DEFAULT 0,
            announce_new_posts  INTEGER DEFAULT 1,
            label               TEXT DEFAULT ''
        );

        -- Node block list
        CREATE TABLE IF NOT EXISTS node_blocks (
            node_id     TEXT PRIMARY KEY,
            block_type  TEXT NOT NULL DEFAULT 'all',
            reason      TEXT DEFAULT '',
            created_at  REAL NOT NULL
        );

        -- Full conversation trace
        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id     TEXT NOT NULL,
            ts          REAL NOT NULL,
            direction   TEXT NOT NULL,
            node_id     TEXT NOT NULL,
            node_name   TEXT DEFAULT '',
            channel_index INTEGER DEFAULT -1,
            is_dm       INTEGER DEFAULT 1,
            room_id     TEXT DEFAULT '',
            raw_text    TEXT NOT NULL,
            chunk_num   INTEGER DEFAULT 1,
            total_chunks INTEGER DEFAULT 1,
            ack_received INTEGER DEFAULT -1,
            success     INTEGER DEFAULT 1,
            conv_type   TEXT DEFAULT 'command'
        );

        -- Schedules (auto-announce)
        CREATE TABLE IF NOT EXISTS schedules (
            id              TEXT PRIMARY KEY,
            label           TEXT NOT NULL,
            room_id         TEXT NOT NULL,
            channel_index   INTEGER NOT NULL,
            message_tmpl    TEXT NOT NULL,
            schedule_type   TEXT NOT NULL DEFAULT 'daily',
            schedule_value  TEXT NOT NULL DEFAULT '12:00',
            enabled         INTEGER DEFAULT 1,
            created_at      REAL NOT NULL,
            last_fired      REAL DEFAULT 0,
            fire_count      INTEGER DEFAULT 0,
            next_fire       REAL DEFAULT 0
        );

        -- Plugin config KV store
        CREATE TABLE IF NOT EXISTS plugin_config (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_posts_room ON posts(room_id, seq);
        CREATE INDEX IF NOT EXISTS idx_subs_room  ON subscriptions(room_id);
        CREATE INDEX IF NOT EXISTS idx_subs_node  ON subscriptions(node_id);
        CREATE INDEX IF NOT EXISTS idx_conv_node  ON conversations(node_id);
        CREATE INDEX IF NOT EXISTS idx_conv_id    ON conversations(conv_id);
        """)
        # Seed default channel config rows 0-7
        for ch in range(8):
            conn.execute(
                "INSERT OR IGNORE INTO channel_config (channel_index, active, reply_in_channel, announce_new_posts) VALUES (?,1,0,1)",
                (ch,)
            )
        # Seed default plugin config
        for key, val in [
            ("public_share_enabled", "0"),
            ("public_share_token",   secrets.token_urlsafe(12)),
        ]:
            conn.execute("INSERT OR IGNORE INTO plugin_config (key,value) VALUES (?,?)", (key, val))
        conn.commit()
        conn.close()
    logger.info("MeshBB DB ready: %s", _DB_PATH)


# ─────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────

def _cfg_get(key: str, default: str = "") -> str:
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT value FROM plugin_config WHERE key=?", (key,)).fetchone()
        conn.close()
    return row["value"] if row else default


def _cfg_set(key: str, value: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("INSERT OR REPLACE INTO plugin_config (key,value) VALUES (?,?)", (key, value))
        conn.commit()
        conn.close()


def _get_channel_config(channel_index: int) -> Dict:
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM channel_config WHERE channel_index=?", (channel_index,)).fetchone()
        conn.close()
    if row:
        return dict(row)
    return {"channel_index": channel_index, "active": 1, "reply_in_channel": 0, "announce_new_posts": 1, "label": ""}


def _get_all_channel_configs() -> List[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM channel_config ORDER BY channel_index").fetchall()]
        conn.close()
    return rows


def _is_node_blocked(node_id: str, context: str = "all") -> bool:
    """context: 'dm', 'channel', 'all' """
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT block_type FROM node_blocks WHERE node_id=?", (node_id,)).fetchone()
        conn.close()
    if not row:
        return False
    bt = row["block_type"]
    if bt == "all":
        return True
    return bt == context


# ─────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────

def _db_get_rooms(include_locked=True) -> List[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM rooms ORDER BY name ASC").fetchall()]
        conn.close()
    if not include_locked:
        rows = [r for r in rows if not r["locked"]]
    return rows


def _db_get_room(room_id: str) -> Optional[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def _db_get_posts(room_id: str, limit: int = 20, offset: int = 0, include_deleted=False) -> List[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        q = "SELECT * FROM posts WHERE room_id=?"
        params: list = [room_id]
        if not include_deleted:
            q += " AND deleted=0"
        q += " ORDER BY pinned DESC, seq DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
        conn.close()
    return rows


def _db_count_posts(room_id: str) -> int:
    with _DB_LOCK:
        conn = _get_db()
        n = conn.execute("SELECT COUNT(*) FROM posts WHERE room_id=? AND deleted=0", (room_id,)).fetchone()[0]
        conn.close()
    return n


def _db_get_post(room_id: str, seq: int) -> Optional[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM posts WHERE room_id=? AND seq=? AND deleted=0", (room_id, seq)
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def _db_create_post(room_id: str, author_id: str, author_name: str, body: str, pinned: bool = False) -> Optional[Dict]:
    room = _db_get_room(room_id)
    if not room:
        return None
    expires_at = None
    if room["ttl_hours"] and room["ttl_hours"] > 0:
        expires_at = time.time() + room["ttl_hours"] * 3600
    with _DB_LOCK:
        conn = _get_db()
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM posts WHERE room_id=?", (room_id,)
        ).fetchone()[0]
        if room["max_posts"] > 0:
            count = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE room_id=? AND deleted=0", (room_id,)
            ).fetchone()[0]
            if count >= room["max_posts"]:
                oldest = conn.execute(
                    "SELECT id FROM posts WHERE room_id=? AND deleted=0 AND pinned=0 ORDER BY seq ASC LIMIT 1",
                    (room_id,)
                ).fetchone()
                if oldest:
                    conn.execute("UPDATE posts SET deleted=1 WHERE id=?", (oldest[0],))
        conn.execute(
            "INSERT INTO posts (room_id,seq,author_id,author_name,body,created_at,expires_at,pinned) VALUES (?,?,?,?,?,?,?,?)",
            (room_id, seq, author_id, author_name, body, time.time(), expires_at, 1 if pinned else 0)
        )
        conn.execute("UPDATE rooms SET post_count=post_count+1 WHERE id=?", (room_id,))
        conn.commit()
        post = dict(conn.execute("SELECT * FROM posts WHERE room_id=? AND seq=?", (room_id, seq)).fetchone())
        conn.close()
    return post


def _db_get_subscribers(room_id: str) -> List[str]:
    with _DB_LOCK:
        conn = _get_db()
        rows = conn.execute("SELECT node_id FROM subscriptions WHERE room_id=?", (room_id,)).fetchall()
        conn.close()
    return [r[0] for r in rows]


def _db_get_subscriptions(node_id: str) -> List[str]:
    with _DB_LOCK:
        conn = _get_db()
        rows = conn.execute("SELECT room_id FROM subscriptions WHERE node_id=?", (node_id,)).fetchall()
        conn.close()
    return [r[0] for r in rows]


def _db_subscribe(node_id: str, room_id: str) -> bool:
    try:
        with _DB_LOCK:
            conn = _get_db()
            conn.execute(
                "INSERT OR IGNORE INTO subscriptions (node_id,room_id,created_at) VALUES (?,?,?)",
                (node_id, room_id, time.time())
            )
            conn.commit()
            conn.close()
        return True
    except Exception as e:
        logger.error("Subscribe error: %s", e)
        return False


def _db_unsubscribe(node_id: str, room_id: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("DELETE FROM subscriptions WHERE node_id=? AND room_id=?", (node_id, room_id))
        conn.commit()
        conn.close()


def _db_room_touch(room_id: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            "UPDATE rooms SET access_count=access_count+1, last_accessed=? WHERE id=?",
            (time.time(), room_id)
        )
        conn.commit()
        conn.close()


def _db_post_touch(room_id: str, seq: int):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            "UPDATE posts SET read_count=read_count+1 WHERE room_id=? AND seq=?",
            (room_id, seq)
        )
        conn.commit()
        conn.close()


def _db_popular_rooms(limit: int = 3) -> List[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM rooms WHERE locked=0 ORDER BY access_count DESC LIMIT ?", (limit,)
        ).fetchall()]
        conn.close()
    return rows


def _db_popular_posts(room_id: str, limit: int = 3) -> List[Dict]:
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE room_id=? AND deleted=0 ORDER BY read_count DESC LIMIT ?",
            (room_id, limit)
        ).fetchall()]
        conn.close()
    return rows


def _log_conversation(conv_id: str, direction: str, node_id: str, node_name: str,
                      channel_index: int, is_dm: bool, room_id: str, text: str,
                      chunk_num: int = 1, total_chunks: int = 1,
                      ack_received: int = -1, success: bool = True, conv_type: str = "command"):
    try:
        with _DB_LOCK:
            conn = _get_db()
            conn.execute("""
                INSERT INTO conversations
                (conv_id,ts,direction,node_id,node_name,channel_index,is_dm,room_id,
                 raw_text,chunk_num,total_chunks,ack_received,success,conv_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (conv_id, time.time(), direction, node_id, node_name,
                 channel_index, 1 if is_dm else 0, room_id,
                 text[:2000], chunk_num, total_chunks, ack_received, 1 if success else 0, conv_type)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.debug("MeshBB conv log error: %s", e)


# ─────────────────────────────────────────────────────────────
# Node name resolver
# ─────────────────────────────────────────────────────────────

def _resolve_node_name(node_id: str) -> str:
    try:
        registry = _context.get("node_registry") or {}
        for slot in registry.values():
            nodes = getattr(slot.meshtastic_data, "nodes", {})
            node = nodes.get(node_id)
            if node:
                u = node.get("user") or {}
                return u.get("longName") or u.get("shortName") or node_id
    except Exception:
        pass
    return node_id


# ─────────────────────────────────────────────────────────────
# Chunking & send engine
# ─────────────────────────────────────────────────────────────

def _split_to_chunks(text: str, max_bytes: int) -> List[str]:
    """Split text into chunks fitting within max_bytes (UTF-8 safe)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]
    chunks = []
    while encoded:
        chunk_b = encoded[:max_bytes]
        # Walk back to valid UTF-8 boundary
        while chunk_b:
            try:
                chunk_b.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk_b = chunk_b[:-1]
        chunks.append(chunk_b.decode("utf-8"))
        encoded = encoded[len(chunk_b):]
    return chunks


async def _send_raw(text: str, dest_id: str, channel_index: int, want_ack: bool = False) -> Optional[str]:
    """Send a single mesh packet. Returns packet id string or None."""
    cm = _context.get("connection_manager")
    if not cm:
        return None
    try:
        # For direct node DMs, do NOT pass channelIndex — meshtastic-python handles
        # PKI/direct routing automatically. Passing channelIndex=0 breaks PKI DMs
        # by forcing channel broadcast behaviour.
        if dest_id == "^all" or channel_index > 0:
            result = await cm.sendText(text, destinationId=dest_id,
                                       channelIndex=channel_index, wantAck=want_ack)
        else:
            result = await cm.sendText(text, destinationId=dest_id, wantAck=want_ack)
        # meshtastic sendText returns a MeshPacket protobuf — id is an int field
        if result is not None:
            pid = getattr(result, "id", None)
            if pid:
                return str(pid)
        # No trackable id — caller treats this as untracked
        return "sent"
    except Exception as e:
        logger.error("MeshBB _send_raw error: %s", e)
        return None


async def _wait_for_ack(msg_id: str, timeout: float) -> bool:
    """Wait for ACK event for msg_id. Returns True if ACK received."""
    async with _ack_lock:
        ev = asyncio.Event()
        _ack_pending[msg_id] = ev
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        async with _ack_lock:
            _ack_pending.pop(msg_id, None)


async def _send_message(
    text: str,
    dest_id: str,
    channel_index: int,
    is_dm: bool,
    conv_id: str,
    node_id: str,
    node_name: str,
    room_id: str = "",
):
    """
    Core send function. Handles:
    - Single vs multi-chunk
    - ACK tracking with retry for multi-chunk
    - Correct dest_id/channel_index for DM vs channel reply
    - Conversation logging for every chunk
    """
    # For channel replies: prepend @NodeName: so recipients know who the reply is for
    if not is_dm:
        short_name = node_name if node_name != node_id else node_id[-4:]
        prefix = f"@{short_name}: "
    else:
        prefix = ""

    # Reserve bytes for prefix on every chunk
    effective_max = _MESH_MAX_BYTES - len(prefix.encode("utf-8"))

    # Generate chunks from the raw text
    raw_chunks = _split_to_chunks(text, effective_max)
    n = len(raw_chunks)

    if n == 1:
        full_text = prefix + raw_chunks[0]
        packet_id = await _send_raw(full_text, dest_id, channel_index, want_ack=False)
        _log_conversation(
            conv_id, "out", node_id, node_name, channel_index, is_dm, room_id,
            full_text, 1, 1, -1, packet_id is not None
        )
        return

    # Multi-chunk: reserve space for prefix + worst-case annotation "[99/99] "
    annotation_overhead = 8  # "[XX/XX] " worst case
    effective_max2 = _MESH_MAX_BYTES - len(prefix.encode("utf-8")) - annotation_overhead
    effective_max2 = max(60, effective_max2)  # never go below 60 bytes of content
    raw_chunks = _split_to_chunks(text, effective_max2)
    n = len(raw_chunks)

    logger.info("MeshBB: sending %d chunks to %s (is_dm=%s)", n, node_id, is_dm)

    for i, chunk in enumerate(raw_chunks):
        chunk_num = i + 1
        if i == 0:
            annotated = f"{prefix}[1/{n} — {n-1} part(s) follow]\n{chunk}"
        elif i == n - 1:
            annotated = f"{prefix}[{chunk_num}/{n}] {chunk}\n[END — {n} parts delivered]"
        else:
            annotated = f"{prefix}[{chunk_num}/{n}] {chunk}"

        success = False
        for attempt in range(_ACK_MAX_RETRIES + 1):
            if attempt > 0:
                logger.info("MeshBB: chunk %d/%d retry %d for %s", chunk_num, n, attempt, node_id)
                await asyncio.sleep(1.5)

            use_ack = True
            packet_id = await _send_raw(annotated, dest_id, channel_index, want_ack=use_ack)

            if packet_id and packet_id != "sent":
                got_ack = await _wait_for_ack(packet_id, _ACK_TIMEOUT_S)
            else:
                # Can't track ACK without id — assume success after delay
                await asyncio.sleep(1.5)
                got_ack = True

            _log_conversation(
                conv_id, "out", node_id, node_name, channel_index, is_dm, room_id,
                annotated, chunk_num, n, 1 if got_ack else 0, packet_id is not None
            )

            if got_ack:
                success = True
                break

        if not success:
            logger.warning("MeshBB: chunk %d/%d failed after retries for %s", chunk_num, n, node_id)

        # Delay between chunks (except after last)
        if i < n - 1:
            await asyncio.sleep(_CHUNK_DELAY_S)


async def _send_dm(node_id: str, text: str, conv_id: str, node_name: str = "", room_id: str = ""):
    """Send a DM reply. channelIndex=0 required — meshtastic uses destinationId for DM routing."""
    nn = node_name or _resolve_node_name(node_id)
    await _send_message(text, node_id, 0, True, conv_id, node_id, nn, room_id)


async def _send_channel_reply(
    from_id: str, channel_index: int, text: str, conv_id: str, node_name: str = "", room_id: str = ""
):
    """Send a reply to a channel message (broadcasts to same channel, @mentions sender)."""
    nn = node_name or _resolve_node_name(from_id)
    await _send_message(text, "^all", channel_index, False, conv_id, from_id, nn, room_id)


async def _broadcast_to_channel(text: str, channel_index: int, conv_id: str = "", room_id: str = ""):
    """Unsolicited broadcast (no @mention prefix)."""
    cm = _context.get("connection_manager")
    if not cm:
        return
    chunks = _split_to_chunks(text, _MESH_MAX_BYTES)
    n = len(chunks)
    for i, chunk in enumerate(chunks):
        if n > 1:
            if i == 0:
                chunk = f"[1/{n}] {chunk}"
            elif i == n - 1:
                chunk = f"[{i+1}/{n}] {chunk} [END]"
            else:
                chunk = f"[{i+1}/{n}] {chunk}"
        try:
            await cm.sendText(chunk, destinationId="^all", channelIndex=channel_index, wantAck=False)
        except Exception as e:
            logger.error("MeshBB broadcast error: %s", e)
        if conv_id:
            _log_conversation(conv_id, "out", "broadcast", "broadcast", channel_index,
                              False, room_id, chunk, i+1, n, -1, True, "announce")
        if i < n - 1:
            await asyncio.sleep(_CHUNK_DELAY_S)


# ─────────────────────────────────────────────────────────────
# Reply router — determine where to send reply
# ─────────────────────────────────────────────────────────────

async def _reply(
    text: str,
    from_id: str,
    is_dm: bool,
    channel_index: int,
    conv_id: str,
    node_name: str,
    room_id: str = "",
):
    """
    Route a reply to the correct destination.
    DM → always reply as DM to from_id.
    Channel → check channel_config.reply_in_channel:
        True  → reply in channel with @mention
        False → reply via DM to from_id
    """
    if is_dm:
        await _send_dm(from_id, text, conv_id, node_name, room_id)
    else:
        ch_cfg = _get_channel_config(channel_index)
        if ch_cfg.get("reply_in_channel"):
            await _send_channel_reply(from_id, channel_index, text, conv_id, node_name, room_id)
        else:
            await _send_dm(from_id, text, conv_id, node_name, room_id)


# ─────────────────────────────────────────────────────────────
# Command parser
# ─────────────────────────────────────────────────────────────

async def _handle_command(
    from_id: str,
    text: str,
    is_dm: bool,
    channel_index: int,
):
    raw = text.strip()
    lower = raw.lower()
    node_name = _resolve_node_name(from_id)
    conv_id = str(uuid.uuid4())[:8]

    # Log incoming
    _log_conversation(conv_id, "in", from_id, node_name, channel_index, is_dm, "", raw)

    # Block check
    block_ctx = "dm" if is_dm else "channel"
    if _is_node_blocked(from_id, block_ctx):
        logger.info("MeshBB: blocked node %s tried command", from_id)
        return

    # Channel active check (only for channel messages)
    if not is_dm:
        ch_cfg = _get_channel_config(channel_index)
        if not ch_cfg.get("active"):
            logger.debug("MeshBB: channel %d not active, ignoring", channel_index)
            return

    async def reply(text: str, room_id: str = ""):
        await _reply(text, from_id, is_dm, channel_index, conv_id, node_name, room_id)

    # ── bb help ──────────────────────────────────────────────
    if lower in ("bb", "bb help", "bb ?"):
        resp = (
            "BB CMDS:\n"
            "bb list — rooms\n"
            "bb popular — top rooms\n"
            "bb status — my subs\n"
            "bb.ROOM list [pg#]\n"
            "bb.ROOM read #\n"
            "bb.ROOM post MSG\n"
            "bb.ROOM sub/unsub\n"
            "bb.ROOM popular\n"
            "\u2219 MeshDash"
        )
        await reply(resp)
        return

    # ── bb list ──────────────────────────────────────────────
    if lower == "bb list":
        rooms = _db_get_rooms(include_locked=False)
        if not rooms:
            await reply("No bulletin boards available yet.")
            return
        lines = ["BOARDS:"]
        for r in rooms:
            lines.append(f" {r['id']}: {r['name']} ({r['post_count']} posts)")
        await reply("\n".join(lines))
        return

    # ── bb status ────────────────────────────────────────────
    if lower == "bb status":
        subs = _db_get_subscriptions(from_id)
        if not subs:
            await reply("No subscriptions.\nTry: bb.ROOM sub")
        else:
            await reply("YOUR SUBS:\n" + "\n".join(f" {s}" for s in subs))
        return

    # ── bb popular ───────────────────────────────────────────
    if lower == "bb popular":
        rooms = _db_popular_rooms(3)
        if not rooms:
            await reply("No rooms yet.")
            return
        lines = ["TOP ROOMS:"]
        for r in rooms:
            lines.append(f" {r['id']}: {r['access_count']} accesses")
        await reply("\n".join(lines))
        return

    # ── bb.<room> <sub-command> ──────────────────────────────
    if lower.startswith("bb."):
        rest = raw[3:]
        parts = rest.split(" ", 2)
        room_id = parts[0].lower().strip()
        subcmd = parts[1].lower().strip() if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""

        room = _db_get_room(room_id)
        if not room:
            await reply(f"Room '{room_id}' not found.\nTry: bb list")
            return

        if room["locked"]:
            await reply(f"[{room_id}] is locked.")
            return

        _db_room_touch(room_id)

        # ── bb.ROOM list [page] ───────────────────────────────
        if subcmd == "list":
            try:
                page = max(1, int(arg)) if arg else 1
            except ValueError:
                page = 1

            total = _db_count_posts(room_id)
            if total == 0:
                await reply(f"[{room_id}] No posts yet.\nPost: bb.{room_id} post MSG")
                return

            total_pages = max(1, math.ceil(total / _POSTS_PER_PAGE))
            page = min(page, total_pages)
            offset = (page - 1) * _POSTS_PER_PAGE

            posts = _db_get_posts(room_id, limit=_POSTS_PER_PAGE, offset=offset)

            lines = [f"[{room_id}] pg {page}/{total_pages}:"]
            for p in reversed(posts):
                ts = datetime.fromtimestamp(p["created_at"], tz=timezone.utc).strftime("%m/%d %H:%M")
                pin = "* " if p["pinned"] else ""
                preview = p["body"][:35] + ("…" if len(p["body"]) > 35 else "")
                lines.append(f" #{p['seq']} {pin}{ts} {p['author_name'][:8]}: {preview}")
            if page < total_pages:
                lines.append(f"Next: bb.{room_id} list {page+1}")

            await reply("\n".join(lines), room_id)
            return

        # ── bb.ROOM read NUM ──────────────────────────────────
        if subcmd == "read":
            try:
                seq = int(arg)
            except (ValueError, TypeError):
                await reply(f"Usage: bb.{room_id} read NUMBER")
                return

            post = _db_get_post(room_id, seq)
            if not post:
                await reply(f"Post #{seq} not found in [{room_id}].")
                return

            _db_post_touch(room_id, seq)

            ts = datetime.fromtimestamp(post["created_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            pin = "[PINNED] " if post["pinned"] else ""
            reads = post.get("read_count", 0) + 1
            resp = (
                f"[{room_id}] #{post['seq']} {pin}\n"
                f"From: {post['author_name']}\n"
                f"{ts} ({reads} reads)\n"
                f"---\n"
                f"{post['body']}"
            )
            await reply(resp, room_id)
            return

        # ── bb.ROOM post MSG ──────────────────────────────────
        if subcmd == "post":
            if not arg:
                await reply(f"Usage: bb.{room_id} post YOUR MESSAGE")
                return

            post = _db_create_post(room_id, from_id, node_name, arg)
            if not post:
                await reply("Failed to create post.")
                return

            await reply(f"✓ [{room_id}] #{post['seq']} posted.", room_id)

            # Notify subscribers (excluding author)
            subs = [s for s in _db_get_subscribers(room_id) if s != from_id]
            if subs:
                notify = (
                    f"[{room_id}] NEW #{post['seq']}\n"
                    f"From: {node_name}\n"
                    f"{arg[:120]}"
                )
                asyncio.create_task(_notify_subscribers(subs, notify, room_id))

            # Broadcast to configured channels
            if room["broadcast_new"]:
                broadcast = f"[{room_id}] #{post['seq']} {node_name}: {arg[:100]}"
                asyncio.create_task(_broadcast_to_channel(
                    broadcast, room["channel_index"],
                    conv_id=conv_id, room_id=room_id
                ))
            return

        # ── bb.ROOM sub ───────────────────────────────────────
        if subcmd == "sub":
            already = from_id in _db_get_subscribers(room_id)
            if already:
                await reply(f"Already subscribed to [{room_id}].")
            else:
                _db_subscribe(from_id, room_id)
                await reply(f"✓ Subscribed to [{room_id}].\nYou'll get new posts as DMs.")
            return

        # ── bb.ROOM unsub ─────────────────────────────────────
        if subcmd == "unsub":
            _db_unsubscribe(from_id, room_id)
            await reply(f"✓ Unsubscribed from [{room_id}].")
            return

        # ── bb.ROOM popular ───────────────────────────────────
        if subcmd == "popular":
            posts = _db_popular_posts(room_id, 3)
            if not posts:
                await reply(f"[{room_id}] No posts yet.")
                return
            lines = [f"[{room_id}] TOP POSTS:"]
            for p in posts:
                preview = p["body"][:30] + ("…" if len(p["body"]) > 30 else "")
                lines.append(f" #{p['seq']} ({p['read_count']} reads): {preview}")
            await reply("\n".join(lines), room_id)
            return

        # Unknown subcommand
        await reply(
            f"Unknown. Try:\n"
            f"bb.{room_id} list\n"
            f"bb.{room_id} read #\n"
            f"bb.{room_id} post MSG\n"
            f"bb.{room_id} sub"
        )
        return


async def _notify_subscribers(subs: List[str], text: str, room_id: str):
    for i, sub_id in enumerate(subs):
        if i > 0:
            await asyncio.sleep(_CHUNK_DELAY_S * 2)
        sub_conv = str(uuid.uuid4())[:8]
        await _send_dm(sub_id, text, sub_conv, room_id=room_id)


# ─────────────────────────────────────────────────────────────
# pubsub listener
# ─────────────────────────────────────────────────────────────

def _on_receive(packet, interface=None):
    event_loop = _context.get("event_loop")
    if not event_loop:
        return
    try:
        decoded = packet.get("decoded") or {}
        portnum = decoded.get("portnum")

        # ACK handling — ROUTING_APP packets carry ACK
        # meshtastic-python encodes errorReason as int (0=NONE) OR string "NONE"
        if portnum == "ROUTING_APP":
            routing = decoded.get("routing") or {}
            error_reason = routing.get("errorReason", -1)
            # Accept ACK if errorReason is 0 (int), "NONE" (string), or absent
            is_ack = error_reason in (0, "NONE", "ack_variant") or routing.get("variant") == "ack_variant"
            if is_ack:
                # requestId field name varies by meshtastic-python version
                req_id = (packet.get("requestId") or packet.get("request_id")
                          or packet.get("decoded", {}).get("requestId"))
                if req_id:
                    rid = str(req_id)
                    asyncio.run_coroutine_threadsafe(_signal_ack(rid), event_loop)
            return

        if portnum not in ("TEXT_MESSAGE_APP", 1):
            return

        text = decoded.get("text") or ""
        if not text:
            payload = decoded.get("payload")
            if isinstance(payload, bytes):
                try:
                    text = payload.decode("utf-8", errors="replace")
                except Exception:
                    return

        if not text.lower().startswith("bb"):
            return

        from_id = packet.get("fromId") or ""
        if not from_id:
            raw_from = packet.get("from")
            if isinstance(raw_from, int):
                from_id = f"!{raw_from:08x}"
        if not from_id:
            return

        to_id = packet.get("toId") or ""
        if not to_id:
            raw_to = packet.get("to")
            if isinstance(raw_to, int):
                to_id = "^all" if raw_to == 0xFFFFFFFF else f"!{raw_to:08x}"

        # Resolve local node ID from node_registry (MeshDash stores it on slot objects)
        local_id = None
        try:
            registry = _context.get("node_registry") or {}
            for slot in registry.values():
                md = getattr(slot, "meshtastic_data", None)
                if md:
                    local_id = getattr(md, "local_node_id", None)
                    if local_id:
                        break
        except Exception:
            pass

        # Determine if this is a DM or channel message
        # DM: packet addressed directly to our node ID
        # Channel: broadcast (0xFFFFFFFF / "^all")
        is_dm = bool(local_id and to_id and to_id == local_id)
        is_channel = (to_id == "^all")

        if not is_dm and not is_channel:
            return

        channel_index = packet.get("channel") or packet.get("channelIndex") or 0

        asyncio.run_coroutine_threadsafe(
            _handle_command(from_id, text, is_dm, channel_index),
            event_loop
        )

    except Exception as e:
        logger.error("MeshBB _on_receive: %s", e)


async def _signal_ack(packet_id: str):
    async with _ack_lock:
        ev = _ack_pending.get(packet_id)
    if ev:
        ev.set()


# ─────────────────────────────────────────────────────────────
# Schedule worker
# ─────────────────────────────────────────────────────────────

def _calc_next_fire(schedule_type: str, schedule_value: str) -> float:
    """
    schedule_type / schedule_value combinations:
      daily       / HH:MM
      weekly      / DOW HH:MM   (DOW = mon/tue/wed/thu/fri/sat/sun)
      interval    / N            (every N minutes)
      custom      / CRON_EXPR   (basic: MIN HOUR DOW)
    Returns unix timestamp of next fire.
    """
    now = datetime.now()
    try:
        if schedule_type == "daily":
            import datetime as _dt
            hh, mm = [int(x) for x in schedule_value.split(":")]
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target.timestamp() <= time.time():
                target = target + _dt.timedelta(days=1)
            return target.timestamp()

        elif schedule_type == "interval":
            minutes = int(schedule_value)
            return time.time() + minutes * 60

        elif schedule_type == "weekly":
            parts = schedule_value.split()
            dow_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
            dow = dow_map.get(parts[0].lower(), 0)
            hh, mm = [int(x) for x in parts[1].split(":")]
            days_ahead = (dow - now.weekday()) % 7
            if days_ahead == 0:
                target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target.timestamp() <= time.time():
                    days_ahead = 7
                else:
                    return target.timestamp()
            import datetime as _dt
            target = (now + _dt.timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            return target.timestamp()

    except Exception as e:
        logger.warning("MeshBB schedule calc error: %s", e)

    return time.time() + 3600  # fallback 1h


def _render_schedule_template(tmpl: str, room: Dict) -> str:
    now = datetime.now()
    return (tmpl
            .replace("{room}", room.get("id", ""))
            .replace("{room_name}", room.get("name", ""))
            .replace("{post_count}", str(room.get("post_count", 0)))
            .replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{time}", now.strftime("%H:%M"))
            .replace("{day}", now.strftime("%A")))


async def _schedule_worker():
    """Check and fire schedules every 60 seconds."""
    logger.info("MeshBB: schedule worker started")
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            with _DB_LOCK:
                conn = _get_db()
                schedules = [dict(r) for r in conn.execute(
                    "SELECT * FROM schedules WHERE enabled=1 AND next_fire <= ?", (now,)
                ).fetchall()]
                conn.close()

            for sched in schedules:
                room = _db_get_room(sched["room_id"])
                if not room:
                    continue
                msg = _render_schedule_template(sched["message_tmpl"], room)
                conv_id = f"sched-{sched['id'][:6]}"
                await _broadcast_to_channel(msg, sched["channel_index"], conv_id=conv_id, room_id=sched["room_id"])

                next_fire = _calc_next_fire(sched["schedule_type"], sched["schedule_value"])
                with _DB_LOCK:
                    conn = _get_db()
                    conn.execute(
                        "UPDATE schedules SET last_fired=?, fire_count=fire_count+1, next_fire=? WHERE id=?",
                        (now, next_fire, sched["id"])
                    )
                    conn.commit()
                    conn.close()
                logger.info("MeshBB: schedule '%s' fired → ch%d", sched["label"], sched["channel_index"])

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("MeshBB schedule worker error: %s", e)


# ─────────────────────────────────────────────────────────────
# TTL worker
# ─────────────────────────────────────────────────────────────

async def _ttl_worker():
    while True:
        try:
            await asyncio.sleep(600)
            now = time.time()
            with _DB_LOCK:
                conn = _get_db()
                r = conn.execute(
                    "UPDATE posts SET deleted=1 WHERE expires_at IS NOT NULL AND expires_at < ? AND deleted=0",
                    (now,)
                )
                if r.rowcount:
                    logger.info("MeshBB TTL: pruned %d post(s)", r.rowcount)
                conn.commit()
                conn.close()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("MeshBB TTL error: %s", e)


# ─────────────────────────────────────────────────────────────
# Watchdog
# ─────────────────────────────────────────────────────────────

async def _watchdog(context: dict):
    wd = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    asyncio.create_task(_ttl_worker())
    asyncio.create_task(_schedule_worker())
    while True:
        try:
            await asyncio.sleep(30)
            if wd and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Plugin lifecycle
# ─────────────────────────────────────────────────────────────

def init_plugin(context: dict):
    global _context
    _context = context
    _init_db()

    # Set next_fire for all schedules that have never fired
    with _DB_LOCK:
        conn = _get_db()
        scheds = [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_fire=0"
        ).fetchall()]
        for s in scheds:
            nf = _calc_next_fire(s["schedule_type"], s["schedule_value"])
            conn.execute("UPDATE schedules SET next_fire=? WHERE id=?", (nf, s["id"]))
        conn.commit()
        conn.close()

    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
    try:
        pub.subscribe(_on_receive, "meshtastic.receive")
    except Exception as e:
        logger.error("MeshBB pub.subscribe: %s", e)

    loop = context.get("event_loop")
    if loop:
        asyncio.run_coroutine_threadsafe(_watchdog(context), loop)

    logger.info("Mesh BB v1.0.0 ready")


# ─────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────

class RoomCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    channel_index: int = 0
    broadcast_new: bool = True
    ttl_hours: int = 0
    max_posts: int = 100

class RoomUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    channel_index: Optional[int] = None
    broadcast_new: Optional[bool] = None
    ttl_hours: Optional[int] = None
    max_posts: Optional[int] = None
    locked: Optional[bool] = None

class PostCreate(BaseModel):
    body: str
    author_id: str = "admin"
    author_name: str = "Admin"
    pinned: bool = False

class ChannelConfigUpdate(BaseModel):
    active: Optional[bool] = None
    reply_in_channel: Optional[bool] = None
    announce_new_posts: Optional[bool] = None
    label: Optional[str] = None

class NodeBlockCreate(BaseModel):
    node_id: str
    block_type: str = "all"
    reason: str = ""

class ScheduleCreate(BaseModel):
    label: str
    room_id: str
    channel_index: int
    message_tmpl: str
    schedule_type: str = "daily"
    schedule_value: str = "12:00"
    enabled: bool = True

class BroadcastRequest(BaseModel):
    room_id: str
    post_seq: int

class ShareConfig(BaseModel):
    enabled: bool


# ─────────────────────────────────────────────────────────────
# Admin REST API — Rooms
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/rooms")
async def list_rooms():
    return {"rooms": _db_get_rooms()}


@plugin_router.post("/rooms")
async def create_room(body: RoomCreate):
    room_id = body.id.lower().strip().replace(" ", "_")
    if not room_id or not re.match(r"^[a-z0-9_]+$", room_id):
        raise HTTPException(400, "Room ID must be lowercase alphanumeric + underscores.")
    if _db_get_room(room_id):
        raise HTTPException(409, f"Room '{room_id}' already exists.")
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            "INSERT INTO rooms (id,name,description,channel_index,broadcast_new,ttl_hours,max_posts,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (room_id, body.name, body.description, body.channel_index,
             1 if body.broadcast_new else 0, body.ttl_hours, body.max_posts, time.time())
        )
        conn.commit()
        conn.close()
    return {"status": "created", "room_id": room_id}


@plugin_router.patch("/rooms/{room_id}")
async def update_room(room_id: str, body: RoomUpdate):
    if not _db_get_room(room_id):
        raise HTTPException(404)
    fields = {k: v for k, v in (body.model_dump() if hasattr(body, "model_dump") else body.dict()).items() if v is not None}
    if "broadcast_new" in fields:
        fields["broadcast_new"] = 1 if fields["broadcast_new"] else 0
    if "locked" in fields:
        fields["locked"] = 1 if fields["locked"] else 0
    if not fields:
        raise HTTPException(400, "No fields.")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(f"UPDATE rooms SET {set_clause} WHERE id=?", (*fields.values(), room_id))
        conn.commit()
        conn.close()
    return {"status": "updated"}


@plugin_router.delete("/rooms/{room_id}")
async def delete_room(room_id: str):
    if not _db_get_room(room_id):
        raise HTTPException(404)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
        conn.commit()
        conn.close()
    return {"status": "deleted"}


@plugin_router.get("/rooms/{room_id}/posts")
async def get_posts(room_id: str, limit: int = 50, offset: int = 0, include_deleted: bool = False):
    if not _db_get_room(room_id):
        raise HTTPException(404)
    posts = _db_get_posts(room_id, limit=limit, offset=offset, include_deleted=include_deleted)
    total = _db_count_posts(room_id)
    return {"room_id": room_id, "posts": posts, "total": total}


@plugin_router.post("/rooms/{room_id}/posts")
async def admin_post(room_id: str, body: PostCreate):
    if not _db_get_room(room_id):
        raise HTTPException(404)
    post = _db_create_post(room_id, body.author_id, body.author_name, body.body, body.pinned)
    if not post:
        raise HTTPException(500)
    subs = [s for s in _db_get_subscribers(room_id) if s != body.author_id]
    if subs:
        notify = f"[{room_id}] NEW #{post['seq']}\nFrom: {body.author_name}\n{body.body[:120]}"
        loop = _context.get("event_loop")
        if loop:
            asyncio.run_coroutine_threadsafe(_notify_subscribers(subs, notify, room_id), loop)
    return {"status": "created", "post": post}


@plugin_router.delete("/rooms/{room_id}/posts/{seq}")
async def delete_post(room_id: str, seq: int):
    with _DB_LOCK:
        conn = _get_db()
        r = conn.execute("UPDATE posts SET deleted=1 WHERE room_id=? AND seq=?", (room_id, seq))
        conn.commit()
        conn.close()
    if r.rowcount == 0:
        raise HTTPException(404)
    return {"status": "deleted"}


@plugin_router.patch("/rooms/{room_id}/posts/{seq}/pin")
async def toggle_pin(room_id: str, seq: int, pinned: bool = True):
    with _DB_LOCK:
        conn = _get_db()
        r = conn.execute(
            "UPDATE posts SET pinned=? WHERE room_id=? AND seq=? AND deleted=0",
            (1 if pinned else 0, room_id, seq)
        )
        conn.commit()
        conn.close()
    if r.rowcount == 0:
        raise HTTPException(404)
    return {"status": "pinned" if pinned else "unpinned"}


@plugin_router.post("/rooms/{room_id}/broadcast")
async def broadcast_post(room_id: str, body: BroadcastRequest):
    room = _db_get_room(room_id)
    if not room:
        raise HTTPException(404)
    post = _db_get_post(room_id, body.post_seq)
    if not post:
        raise HTTPException(404)
    text = f"[{room_id}] #{post['seq']} {post['author_name']}: {post['body'][:120]}"
    loop = _context.get("event_loop")
    if loop:
        asyncio.run_coroutine_threadsafe(
            _broadcast_to_channel(text, room["channel_index"], room_id=room_id), loop
        )
    return {"status": "queued"}


@plugin_router.get("/rooms/{room_id}/subscribers")
async def get_subscribers(room_id: str):
    if not _db_get_room(room_id):
        raise HTTPException(404)
    subs = _db_get_subscribers(room_id)
    return {"subscribers": [{"node_id": s, "name": _resolve_node_name(s)} for s in subs], "count": len(subs)}


@plugin_router.delete("/rooms/{room_id}/subscribers/{node_id}")
async def remove_subscriber(room_id: str, node_id: str):
    _db_unsubscribe(node_id, room_id)
    return {"status": "removed"}


# ─────────────────────────────────────────────────────────────
# Channel config API
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/channels")
async def get_channels():
    return {"channels": _get_all_channel_configs()}


@plugin_router.patch("/channels/{channel_index}")
async def update_channel(channel_index: int, body: ChannelConfigUpdate):
    if channel_index < 0 or channel_index > 7:
        raise HTTPException(400, "Channel index 0-7.")
    fields = {k: v for k, v in (body.model_dump() if hasattr(body, "model_dump") else body.dict()).items() if v is not None}
    for flag in ("active", "reply_in_channel", "announce_new_posts"):
        if flag in fields:
            fields[flag] = 1 if fields[flag] else 0
    if not fields:
        raise HTTPException(400, "No fields.")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(f"UPDATE channel_config SET {set_clause} WHERE channel_index=?",
                     (*fields.values(), channel_index))
        conn.commit()
        conn.close()
    return {"status": "updated"}


# ─────────────────────────────────────────────────────────────
# Node blocks API
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/blocks")
async def get_blocks():
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM node_blocks ORDER BY created_at DESC").fetchall()]
        conn.close()
    return {"blocks": rows}


@plugin_router.post("/blocks")
async def add_block(body: NodeBlockCreate):
    if body.block_type not in ("dm", "channel", "all"):
        raise HTTPException(400, "block_type must be dm/channel/all")
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            "INSERT OR REPLACE INTO node_blocks (node_id,block_type,reason,created_at) VALUES (?,?,?,?)",
            (body.node_id, body.block_type, body.reason, time.time())
        )
        conn.commit()
        conn.close()
    return {"status": "blocked"}


@plugin_router.delete("/blocks/{node_id}")
async def remove_block(node_id: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("DELETE FROM node_blocks WHERE node_id=?", (node_id,))
        conn.commit()
        conn.close()
    return {"status": "unblocked"}


# ─────────────────────────────────────────────────────────────
# Conversations API
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/conversations")
async def get_conversations(
    node_id: Optional[str] = None,
    room_id: Optional[str] = None,
    limit: int = 200,
    is_dm: Optional[int] = None,
    direction: Optional[str] = None,
):
    conditions = []
    params: list = []
    if node_id:
        conditions.append("node_id=?")
        params.append(node_id)
    if room_id:
        conditions.append("room_id=?")
        params.append(room_id)
    if is_dm is not None:
        conditions.append("is_dm=?")
        params.append(is_dm)
    if direction:
        conditions.append("direction=?")
        params.append(direction)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM conversations {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()]
        conn.close()
    return {"conversations": rows, "count": len(rows)}


@plugin_router.get("/conversations/nodes")
async def get_conversation_nodes():
    """Return list of unique nodes that have interacted."""
    with _DB_LOCK:
        conn = _get_db()
        rows = conn.execute(
            "SELECT node_id, node_name, COUNT(*) as cmd_count, MAX(ts) as last_seen, "
            "SUM(CASE WHEN is_dm=1 THEN 1 ELSE 0 END) as dm_count, "
            "SUM(CASE WHEN is_dm=0 THEN 1 ELSE 0 END) as channel_count "
            "FROM conversations WHERE direction='in' GROUP BY node_id ORDER BY last_seen DESC"
        ).fetchall()
        conn.close()
    return {"nodes": [dict(r) for r in rows]}


# ─────────────────────────────────────────────────────────────
# Schedules API
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/schedules")
async def get_schedules():
    with _DB_LOCK:
        conn = _get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()]
        conn.close()
    return {"schedules": rows}


@plugin_router.post("/schedules")
async def create_schedule(body: ScheduleCreate):
    if not _db_get_room(body.room_id):
        raise HTTPException(404, f"Room '{body.room_id}' not found.")
    sid = str(uuid.uuid4())[:12]
    next_fire = _calc_next_fire(body.schedule_type, body.schedule_value)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            """INSERT INTO schedules (id,label,room_id,channel_index,message_tmpl,
               schedule_type,schedule_value,enabled,created_at,next_fire)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (sid, body.label, body.room_id, body.channel_index, body.message_tmpl,
             body.schedule_type, body.schedule_value, 1 if body.enabled else 0,
             time.time(), next_fire)
        )
        conn.commit()
        conn.close()
    return {"status": "created", "id": sid, "next_fire": next_fire}


@plugin_router.patch("/schedules/{sid}")
async def update_schedule(sid: str, body: dict = Body(...)):
    allowed = {"label", "message_tmpl", "channel_index", "schedule_type", "schedule_value", "enabled"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "No valid fields.")
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    # Recalc next_fire if schedule changed
    if "schedule_type" in fields or "schedule_value" in fields:
        with _DB_LOCK:
            conn = _get_db()
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
            conn.close()
        if row:
            st = fields.get("schedule_type", row["schedule_type"])
            sv = fields.get("schedule_value", row["schedule_value"])
            fields["next_fire"] = _calc_next_fire(st, sv)
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(f"UPDATE schedules SET {set_clause} WHERE id=?", (*fields.values(), sid))
        conn.commit()
        conn.close()
    return {"status": "updated"}


@plugin_router.delete("/schedules/{sid}")
async def delete_schedule(sid: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
        conn.commit()
        conn.close()
    return {"status": "deleted"}


@plugin_router.post("/schedules/{sid}/fire")
async def fire_schedule_now(sid: str):
    """Manually trigger a schedule immediately."""
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        conn.close()
    if not row:
        raise HTTPException(404)
    sched = dict(row)
    room = _db_get_room(sched["room_id"])
    if not room:
        raise HTTPException(404, "Room not found.")
    msg = _render_schedule_template(sched["message_tmpl"], room)
    loop = _context.get("event_loop")
    if loop:
        asyncio.run_coroutine_threadsafe(
            _broadcast_to_channel(msg, sched["channel_index"], room_id=sched["room_id"]), loop
        )
    return {"status": "fired", "message": msg}


# ─────────────────────────────────────────────────────────────
# Public share API
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/share/config")
async def get_share_config():
    enabled = _cfg_get("public_share_enabled", "0") == "1"
    token = _cfg_get("public_share_token", "")
    return {
        "enabled": enabled,
        "token": token,
        "url": f"/api/plugins/mesh_bb/public/{token}" if enabled else None,
        "viewer_url": f"/static/plugins/mesh_bb/public.html?token={token}" if enabled else None,
    }


@plugin_router.post("/share/config")
async def set_share_config(body: ShareConfig):
    _cfg_set("public_share_enabled", "1" if body.enabled else "0")
    token = _cfg_get("public_share_token", secrets.token_urlsafe(12))
    enabled = body.enabled
    return {
        "enabled": enabled,
        "token": token,
        "viewer_url": f"/static/plugins/mesh_bb/public.html?token={token}" if enabled else None,
    }


@plugin_router.post("/share/regenerate")
async def regenerate_share_token():
    new_token = secrets.token_urlsafe(12)
    _cfg_set("public_share_token", new_token)
    return {"token": new_token}


@plugin_router.get("/public/{token}")
async def public_view(token: str):
    """Unauthenticated public read-only view. Returns rooms + posts as JSON."""
    stored_token = _cfg_get("public_share_token", "")
    if not stored_token or token != stored_token:
        raise HTTPException(403, "Invalid or disabled share link.")
    if _cfg_get("public_share_enabled", "0") != "1":
        raise HTTPException(403, "Public sharing is disabled.")

    rooms = _db_get_rooms(include_locked=False)
    result = []
    for room in rooms:
        posts = _db_get_posts(room["id"], limit=20)
        result.append({**room, "posts": posts})
    return {"rooms": result, "generated_at": time.time()}


# ─────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────

@plugin_router.get("/stats")
async def get_stats():
    with _DB_LOCK:
        conn = _get_db()
        total_rooms   = conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        total_posts   = conn.execute("SELECT COUNT(*) FROM posts WHERE deleted=0").fetchone()[0]
        total_subs    = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        total_cmds    = conn.execute("SELECT COUNT(*) FROM conversations WHERE direction='in'").fetchone()[0]
        cmds_24h      = conn.execute("SELECT COUNT(*) FROM conversations WHERE direction='in' AND ts>?", (time.time()-86400,)).fetchone()[0]
        active_nodes  = conn.execute("SELECT COUNT(DISTINCT node_id) FROM conversations WHERE direction='in'").fetchone()[0]
        blocked_nodes = conn.execute("SELECT COUNT(*) FROM node_blocks").fetchone()[0]
        schedules     = conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1").fetchone()[0]
        conn.close()
    return {
        "total_rooms": total_rooms, "total_posts": total_posts,
        "total_subscriptions": total_subs, "total_commands": total_cmds,
        "commands_24h": cmds_24h, "active_nodes": active_nodes,
        "blocked_nodes": blocked_nodes, "active_schedules": schedules,
    }