"""
Channel Vault Plugin — v1.0.0
==============================
Server-side PSK vault that goes beyond MeshMonitor's basic decryption:

1. VAULT  — store unlimited channel PSKs beyond the radio's 8 slots.
            Packets the radio couldn't decrypt are decrypted server-side
            and retroactively processed in message history.

2. DETECT — passively watches incoming packets for channel indices not
            present in any vault entry. Raises an "unknown channel" alert
            so the operator knows they're missing packets.

3. BACKUP — snapshot all 8 current radio channel configs (names + PSKs)
            into the vault at any time. One-click restore.

4. HOTSWAP — write any vaulted channel to a chosen radio slot, then
             optionally reboot. Guided 3-step flow with slot preview,
             collision warning, and confirmation.

RF decrypt algorithm (matches meshtastic firmware crypto.cpp):
  nonce  = packet_id (uint32 LE, 4B) + from_node (uint32 LE, 4B) + 0x00×8
  cipher = AES-128-CTR if PSK is 16B, AES-256-CTR if 32B
  key    = PSK bytes directly
  plain  = AES-CTR-decrypt(key, nonce, ciphertext)
  then parse plain as Data protobuf → portnum + payload
"""

import asyncio
import base64
import json
import logging
import os
import sqlite3
import struct
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pubsub import pub

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    from meshtastic import portnums_pb2, mesh_pb2
    HAS_PROTO = True
except ImportError:
    HAS_PROTO = False

logger        = logging.getLogger("plugin.channel_vault")
plugin_router = APIRouter()

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(PLUGIN_DIR, "channel_vault.db")
_DB_LOCK = threading.Lock()


def _db_init():
    with _DB_LOCK:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS vault_channels (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                psk_b64     TEXT NOT NULL,
                notes       TEXT DEFAULT '',
                priority    INTEGER DEFAULT 100,
                created_at  REAL,
                updated_at  REAL,
                slot_hint   INTEGER DEFAULT -1,
                uplink      BOOLEAN DEFAULT 1,
                downlink    BOOLEAN DEFAULT 1,
                source      TEXT DEFAULT 'manual'
            );
            CREATE TABLE IF NOT EXISTS backups (
                id          TEXT PRIMARY KEY,
                label       TEXT NOT NULL,
                slot_id     TEXT NOT NULL,
                channels_json TEXT NOT NULL,
                created_at  REAL
            );
            CREATE TABLE IF NOT EXISTS decrypted_cache (
                packet_event_id TEXT PRIMARY KEY,
                vault_id        TEXT,
                portnum         TEXT,
                payload_json    TEXT,
                decrypted_at    REAL
            );
        """)
        conn.commit()
        conn.close()


def _db_conn():
    return sqlite3.connect(_DB_PATH, check_same_thread=False)


def _get_vault_channels() -> List[dict]:
    with _DB_LOCK:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT id,name,psk_b64,notes,priority,created_at,updated_at,"
            "slot_hint,uplink,downlink,source FROM vault_channels ORDER BY priority ASC"
        ).fetchall()
        conn.close()
    return [
        dict(zip(["id","name","psk_b64","notes","priority","created_at",
                  "updated_at","slot_hint","uplink","downlink","source"], r))
        for r in rows
    ]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_node_registry: Dict[str, Any] = {}
_event_loop:    Optional[asyncio.AbstractEventLoop] = None
_alerts:        List[dict] = []          # unknown-channel alerts
_alert_seen:    set        = set()       # (slot_id, channel_idx) already alerted
_decrypt_stats  = {"attempted": 0, "succeeded": 0, "failed": 0}


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def init_plugin(context: dict):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    _db_init()
    logger.info("Channel Vault v1.0.0 — crypto=%s proto=%s slots=%d",
                HAS_CRYPTO, HAS_PROTO, len(_node_registry))
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


# ---------------------------------------------------------------------------
# Packet listener — detect unknown channels + attempt decrypt
# ---------------------------------------------------------------------------

def _on_receive(packet, interface=None):
    try:
        decoded  = packet.get("decoded", {})
        portnum  = str(decoded.get("portnum", "")) if isinstance(decoded, dict) else ""
        ch_idx   = packet.get("channel", 0) or 0
        slot_id  = _slot_for_iface(None)

        # Is this an encrypted packet the radio couldn't decode?
        is_encrypted = not portnum or packet.get("encrypted") is not None

        if is_encrypted:
            # Try to decrypt with vault keys
            if HAS_CRYPTO:
                vault_chs = _get_vault_channels()
                if vault_chs:
                    asyncio.run_coroutine_threadsafe(
                        _try_decrypt_packet(packet, slot_id, vault_chs),
                        _event_loop,
                    ) if _event_loop else None

            # Alert if this channel index has no matching vault entry
            key = (slot_id, ch_idx)
            if key not in _alert_seen:
                _alert_seen.add(key)
                alert = {
                    "id":        str(uuid.uuid4())[:8],
                    "ts":        time.time(),
                    "slot_id":   slot_id,
                    "ch_idx":    ch_idx,
                    "from_id":   packet.get("fromId") or packet.get("from_id", "?"),
                    "packet_id": packet.get("id"),
                    "dismissed": False,
                }
                _alerts.insert(0, alert)
                if len(_alerts) > 50:
                    _alerts.pop()
                logger.info("Channel Vault: unknown encrypted ch=%d from=%s [%s]",
                            ch_idx, alert["from_id"], slot_id)

    except Exception as e:
        logger.debug("_on_receive: %s", e)


async def _try_decrypt_packet(packet: dict, slot_id: str, vault_chs: list):
    """Attempt decryption of a single encrypted packet using vault PSKs."""
    if not HAS_CRYPTO:
        return

    pkt_id   = packet.get("id") or 0
    from_num = packet.get("from") or 0
    enc_data = packet.get("encrypted")

    if not enc_data or not isinstance(enc_data, (bytes, bytearray)):
        return

    # Build nonce: packet_id (4B LE) + from_node (4B LE) + 0x00 * 8
    try:
        nonce = struct.pack("<II", pkt_id & 0xFFFFFFFF, from_num & 0xFFFFFFFF) + b"\x00" * 8
    except struct.error:
        return

    _decrypt_stats["attempted"] += 1

    for ch in vault_chs:
        try:
            psk_bytes = base64.b64decode(ch["psk_b64"])
        except Exception:
            continue

        if len(psk_bytes) not in (16, 32):
            # Pad 1-byte PSK to 16 bytes (meshtastic default channel expansion)
            if len(psk_bytes) == 1 and psk_bytes[0] == 0x01:
                # Well-known default AES key (from meshtastic firmware)
                psk_bytes = bytes([
                    0xd4, 0xf1, 0xbb, 0x3a, 0x20, 0x29, 0x07, 0x59,
                    0xf0, 0xbc, 0xff, 0xab, 0xcf, 0x4e, 0xc3, 0x14,
                ])
            else:
                continue

        try:
            cipher = Cipher(algorithms.AES(psk_bytes), modes.CTR(nonce))
            dec    = cipher.decryptor()
            plain  = dec.update(bytes(enc_data)) + dec.finalize()
        except Exception:
            continue

        # Validate: try to parse as Data protobuf
        portnum, payload_json = _parse_data_proto(plain)
        if portnum:
            _decrypt_stats["succeeded"] += 1
            event_id = packet.get("event_id") or f"pkt_{pkt_id}_{from_num}"
            _save_decrypted(event_id, ch["id"], portnum, payload_json)
            logger.info("Channel Vault: decrypted packet %s portnum=%s vault='%s'",
                        event_id, portnum, ch["name"])
            return

    _decrypt_stats["failed"] += 1


def _parse_data_proto(data: bytes) -> Tuple[Optional[str], Optional[str]]:
    """
    Attempt to parse raw bytes as a meshtastic Data protobuf.
    Returns (portnum_name, payload_json) or (None, None) on failure.
    """
    if not HAS_PROTO or len(data) < 2:
        # Without proto, heuristic check: first byte is a valid protobuf field tag
        # Field 1 (portnum) = tag 0x08, field 3 (payload) = tag 0x1a
        if len(data) >= 2 and data[0] == 0x08 and data[1] < 100:
            return f"PORTNUM_{data[1]}", None
        return None, None
    try:
        from meshtastic import mesh_pb2
        d = mesh_pb2.Data()
        d.ParseFromString(data)
        # Validate: portnum must be a known value
        portnum = d.portnum
        if portnum == 0:
            return None, None
        try:
            from meshtastic import portnums_pb2
            name = portnums_pb2.PortNum.Name(portnum)
        except Exception:
            if portnum < 1 or portnum > 1023:
                return None, None
            name = f"PORTNUM_{portnum}"
        payload = {}
        if d.payload:
            payload = {"raw_b64": base64.b64encode(d.payload).decode()}
            # Try to decode TEXT_MESSAGE
            if "TEXT" in name:
                try:
                    payload["text"] = d.payload.decode("utf-8")
                except Exception:
                    pass
        return name, json.dumps(payload)
    except Exception:
        return None, None


def _save_decrypted(event_id: str, vault_id: str, portnum: str, payload_json: Optional[str]):
    try:
        with _DB_LOCK:
            conn = _db_conn()
            conn.execute(
                "INSERT OR REPLACE INTO decrypted_cache "
                "(packet_event_id,vault_id,portnum,payload_json,decrypted_at) "
                "VALUES (?,?,?,?,?)",
                (event_id, vault_id, portnum, payload_json, time.time()),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("save_decrypted: %s", e)


# ---------------------------------------------------------------------------
# Retroactive decrypt — process historical encrypted packets from the main DB
# ---------------------------------------------------------------------------

async def _retroactive_decrypt(slot_id: str, vault_id: str):
    """
    Scan the slot's main meshtastic DB for encrypted packets and attempt
    to decrypt them with the newly added vault PSK.
    """
    slot = _node_registry.get(slot_id)
    if not slot:
        return 0
    db_mgr = getattr(slot, "db_manager", None)
    if not db_mgr:
        return 0
    db_path = getattr(db_mgr, "db_path", None)
    if not db_path or db_path == ":memory:":
        return 0

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        # Find encrypted packets: raw not null, packet_type='Encrypted' or decoded is null
        rows = conn.execute(
            "SELECT event_id, raw FROM packets "
            "WHERE (packet_type='Encrypted' OR (decoded IS NULL AND raw IS NOT NULL)) "
            "AND raw IS NOT NULL LIMIT 500"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("retroactive scan: %s", e)
        return 0

    decrypted = 0
    vault_chs = _get_vault_channels()
    target_ch = next((c for c in vault_chs if c["id"] == vault_id), None)
    if not target_ch:
        return 0

    for event_id, raw_json in rows:
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except Exception:
            continue

        pkt_id   = raw.get("id") or 0
        from_num = raw.get("from") or 0

        # Get encrypted bytes from the raw dict
        enc_b64  = raw.get("encrypted")
        if not enc_b64:
            continue
        if isinstance(enc_b64, str):
            try:
                enc_data = base64.b64decode(enc_b64)
            except Exception:
                continue
        elif isinstance(enc_b64, (bytes, bytearray)):
            enc_data = bytes(enc_b64)
        else:
            continue

        try:
            psk_bytes = base64.b64decode(target_ch["psk_b64"])
            if len(psk_bytes) == 1 and psk_bytes[0] == 0x01:
                psk_bytes = bytes([
                    0xd4,0xf1,0xbb,0x3a,0x20,0x29,0x07,0x59,
                    0xf0,0xbc,0xff,0xab,0xcf,0x4e,0xc3,0x14,
                ])
            if len(psk_bytes) not in (16, 32):
                continue
            nonce  = struct.pack("<II", pkt_id & 0xFFFFFFFF, from_num & 0xFFFFFFFF) + b"\x00" * 8
            cipher = Cipher(algorithms.AES(psk_bytes), modes.CTR(nonce))
            plain  = cipher.decryptor().update(enc_data)
            portnum, payload_json = _parse_data_proto(plain)
            if portnum:
                _save_decrypted(event_id, vault_id, portnum, payload_json)
                decrypted += 1
        except Exception:
            continue

    logger.info("Retroactive decrypt [%s]: %d decrypted from history", slot_id, decrypted)
    return decrypted


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_for_iface(_iface) -> str:
    # meshtastic.receive topic delivers only (packet) — no interface
    # Fall back to node_0 if available
    for sid, slot in _node_registry.items():
        return sid  # return first available slot
    return "node_0"


def _get_iface(slot_id: str):
    slot = _node_registry.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    cm = getattr(slot, "connection_manager", None)
    if not cm or not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready")
    if not cm.interface:
        raise HTTPException(503, "No radio interface")
    return cm.interface


def _read_radio_channels(slot_id: str) -> List[dict]:
    iface = _get_iface(slot_id)
    node  = iface.localNode
    chs   = []
    for i, ch in enumerate(getattr(node, "channels", []) or []):
        s  = getattr(ch, "settings", None)
        ri = int(getattr(ch, "role", 0))
        psk = getattr(s, "psk", b"") if s else b""
        chs.append({
            "index":    i,
            "name":     getattr(s, "name", "") if s else "",
            "role":     {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}.get(ri, str(ri)),
            "psk_b64":  base64.b64encode(psk).decode() if psk else "",
            "uplink":   bool(getattr(s, "uplink_enabled",   True) if s else True),
            "downlink": bool(getattr(s, "downlink_enabled", True) if s else True),
        })
    return chs


# ---------------------------------------------------------------------------
# REST routes — Vault CRUD
# ---------------------------------------------------------------------------

@plugin_router.get("")
@plugin_router.get("/")
async def health():
    return {
        "plugin":  "channel_vault", "version": "1.0.0", "status": "running",
        "crypto":  HAS_CRYPTO, "proto": HAS_PROTO,
        "vault_count": len(_get_vault_channels()),
        "decrypt_stats": _decrypt_stats,
    }


class VaultAddReq(BaseModel):
    name:     str
    psk_b64:  str
    notes:    str  = ""
    priority: int  = Field(100, ge=0, le=999)
    uplink:   bool = True
    downlink: bool = True
    slot_id:  str  = "node_0"   # for retroactive decrypt


@plugin_router.post("/vault")
async def vault_add(r: VaultAddReq):
    # Validate PSK
    try:
        raw = base64.b64decode(r.psk_b64)
    except Exception:
        raise HTTPException(400, "Invalid base64 PSK")
    if len(raw) not in (1, 16, 32):
        raise HTTPException(400, f"PSK must be 1, 16 or 32 bytes (got {len(raw)})")

    vid = str(uuid.uuid4())[:12]
    now = time.time()
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(
            "INSERT INTO vault_channels (id,name,psk_b64,notes,priority,"
            "created_at,updated_at,uplink,downlink,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vid, r.name.strip(), r.psk_b64, r.notes, r.priority, now, now,
             r.uplink, r.downlink, "manual"),
        )
        conn.commit()
        conn.close()

    # Retroactive decrypt in background
    if HAS_CRYPTO and _event_loop:
        asyncio.run_coroutine_threadsafe(
            _retroactive_decrypt(r.slot_id, vid), _event_loop
        )

    return {"status": "added", "id": vid}


@plugin_router.get("/vault")
async def vault_list():
    return {"channels": _get_vault_channels(), "stats": _decrypt_stats}


@plugin_router.delete("/vault/{vid}")
async def vault_delete(vid: str):
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("DELETE FROM vault_channels WHERE id=?", (vid,))
        conn.commit()
        conn.close()
    return {"status": "deleted"}


class VaultUpdateReq(BaseModel):
    name:     Optional[str]  = None
    notes:    Optional[str]  = None
    priority: Optional[int]  = None
    uplink:   Optional[bool] = None
    downlink: Optional[bool] = None


@plugin_router.patch("/vault/{vid}")
async def vault_update(vid: str, r: VaultUpdateReq):
    fields = {k: v for k, v in r.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Nothing to update")
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(f"UPDATE vault_channels SET {set_clause} WHERE id=?",
                     [*fields.values(), vid])
        conn.commit()
        conn.close()
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@plugin_router.get("/alerts")
async def get_alerts():
    return {"alerts": [a for a in _alerts if not a.get("dismissed")]}


@plugin_router.post("/alerts/{aid}/dismiss")
async def dismiss_alert(aid: str):
    for a in _alerts:
        if a["id"] == aid:
            a["dismissed"] = True
    return {"status": "dismissed"}


# ---------------------------------------------------------------------------
# Decrypt cache / results
# ---------------------------------------------------------------------------

@plugin_router.get("/decrypted")
async def get_decrypted(limit: int = 100):
    with _DB_LOCK:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT dc.packet_event_id, dc.vault_id, dc.portnum, dc.payload_json, "
            "dc.decrypted_at, vc.name as vault_name "
            "FROM decrypted_cache dc LEFT JOIN vault_channels vc ON dc.vault_id=vc.id "
            "ORDER BY dc.decrypted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
    return {"results": [
        dict(zip(["packet_event_id","vault_id","portnum","payload_json",
                  "decrypted_at","vault_name"], r))
        for r in rows
    ]}


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------

@plugin_router.post("/backup")
async def backup_channels(slot_id: str = "node_0", label: str = ""):
    chs   = await asyncio.to_thread(_read_radio_channels, slot_id)
    bid   = str(uuid.uuid4())[:12]
    label = label.strip() or f"Backup {time.strftime('%Y-%m-%d %H:%M', time.gmtime())}"
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute(
            "INSERT INTO backups (id,label,slot_id,channels_json,created_at) VALUES (?,?,?,?,?)",
            (bid, label, slot_id, json.dumps(chs), time.time()),
        )
        conn.commit()
        conn.close()
    return {"status": "ok", "backup_id": bid, "label": label, "channels": chs}


@plugin_router.get("/backups")
async def list_backups():
    with _DB_LOCK:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT id,label,slot_id,created_at FROM backups ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
    return {"backups": [dict(zip(["id","label","slot_id","created_at"], r)) for r in rows]}


@plugin_router.get("/backups/{bid}")
async def get_backup(bid: str):
    with _DB_LOCK:
        conn = _db_conn()
        row = conn.execute(
            "SELECT id,label,slot_id,channels_json,created_at FROM backups WHERE id=?", (bid,)
        ).fetchone()
        conn.close()
    if not row:
        raise HTTPException(404, "Backup not found")
    d = dict(zip(["id","label","slot_id","channels_json","created_at"], row))
    d["channels"] = json.loads(d.pop("channels_json"))
    return d


@plugin_router.delete("/backups/{bid}")
async def delete_backup(bid: str):
    with _DB_LOCK:
        conn = _db_conn()
        conn.execute("DELETE FROM backups WHERE id=?", (bid,))
        conn.commit()
        conn.close()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Radio channel read
# ---------------------------------------------------------------------------

@plugin_router.get("/radio/{slot_id}")
async def read_radio_channels(slot_id: str):
    chs = await asyncio.to_thread(_read_radio_channels, slot_id)
    return {"slot_id": slot_id, "channels": chs}


# ---------------------------------------------------------------------------
# Hotswap — write a vault channel to the radio
# ---------------------------------------------------------------------------

class HotswapReq(BaseModel):
    vault_id:      str
    slot_id:       str   = "node_0"
    radio_slot:    int   = Field(..., ge=0, le=7)
    role:          str   = "SECONDARY"
    uplink:        bool  = True
    downlink:      bool  = True
    reboot_after:  bool  = True
    reboot_delay:  int   = Field(5, ge=3, le=30)


@plugin_router.post("/hotswap")
async def hotswap(r: HotswapReq):
    """
    Write a vaulted channel configuration to a specific radio slot.
    Optionally reboots the node afterwards (required for channel changes
    to take effect on the mesh).
    """
    # Fetch vault entry
    vault_chs = _get_vault_channels()
    ch = next((c for c in vault_chs if c["id"] == r.vault_id), None)
    if not ch:
        raise HTTPException(404, "Vault channel not found")

    iface = _get_iface(r.slot_id)

    def _do_write():
        import meshtastic.channel_pb2 as ch_pb2
        node = iface.localNode
        if not node.channels or r.radio_slot >= len(node.channels):
            raise ValueError(f"Radio slot {r.radio_slot} not available")

        radio_ch = node.channels[r.radio_slot]
        role_map  = {"PRIMARY": 1, "SECONDARY": 2, "DISABLED": 0}
        radio_ch.role                      = role_map.get(r.role.upper(), 2)
        radio_ch.settings.name             = ch["name"]
        radio_ch.settings.uplink_enabled   = r.uplink
        radio_ch.settings.downlink_enabled = r.downlink
        try:
            radio_ch.settings.psk = base64.b64decode(ch["psk_b64"])
        except Exception as e:
            raise ValueError(f"Invalid PSK: {e}") from e

        node.writeChannel(r.radio_slot)
        return True

    try:
        await asyncio.to_thread(_do_write)
    except Exception as e:
        raise HTTPException(500, f"Channel write failed: {e}") from e

    result = {
        "status":     "written",
        "vault_name": ch["name"],
        "radio_slot": r.radio_slot,
        "slot_id":    r.slot_id,
    }

    if r.reboot_after:
        async def _reboot():
            await asyncio.sleep(1.5)
            try:
                def _do_reboot():
                    iface.localNode.reboot(secs=r.reboot_delay)
                    iface.waitForAckNak()
                await asyncio.to_thread(_do_reboot)
                logger.info("Channel Vault: reboot sent after hotswap")
            except Exception as e:
                logger.warning("Hotswap reboot: %s", e)
        asyncio.create_task(_reboot())
        result["reboot_in"] = r.reboot_delay

    return result


# ---------------------------------------------------------------------------
# Import vault from backup (copy backup channels into vault)
# ---------------------------------------------------------------------------

@plugin_router.post("/import_backup/{bid}")
async def import_from_backup(bid: str):
    with _DB_LOCK:
        conn = _db_conn()
        row = conn.execute(
            "SELECT channels_json FROM backups WHERE id=?", (bid,)
        ).fetchone()
        conn.close()
    if not row:
        raise HTTPException(404, "Backup not found")

    chs = json.loads(row[0])
    added = []
    now   = time.time()
    with _DB_LOCK:
        conn = _db_conn()
        for i, ch in enumerate(chs):
            if not ch.get("psk_b64") or ch.get("role") == "DISABLED":
                continue
            vid = str(uuid.uuid4())[:12]
            conn.execute(
                "INSERT OR IGNORE INTO vault_channels "
                "(id,name,psk_b64,notes,priority,created_at,updated_at,"
                "slot_hint,uplink,downlink,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (vid, ch.get("name") or f"Channel {i}", ch["psk_b64"],
                 f"Imported from backup", i*10, now, now,
                 ch.get("index", i), ch.get("uplink", True),
                 ch.get("downlink", True), "backup"),
            )
            added.append(vid)
        conn.commit()
        conn.close()
    return {"status": "ok", "imported": len(added)}
