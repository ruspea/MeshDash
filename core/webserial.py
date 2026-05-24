"""
webserial_api.py — Web Serial Bridge Endpoints for MeshDash
─────────────────────────────────────────────────────────────────────────────
POST /api/webserial/packet  — Raw FromRadio protobuf binary → decoded + injected
POST /api/webserial/send    — {text, destinationId, channelIndex} → raw frame bytes
POST /api/webserial/status  — {"status": "connected"|"disconnected"}
GET  /api/webserial/status  — Current session state
─────────────────────────────────────────────────────────────────────────────
Slot-aware: all globals replaced with per-slot state injected via
configure_webserial(slot_registry, get_current_active_user_fn).
Falls back to legacy single-slot module-scan for backwards compat.
"""

WEB_SERIAL_ENABLED = True

from core.routes.schemas import User
from core.auth import get_current_active_user
import asyncio
import json as _json
import logging
import random
import struct
import time
import traceback
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.responses import JSONResponse, Response as FastAPIResponse
from pydantic import BaseModel

logger = logging.getLogger("webserial_api")

web_serial_router = APIRouter()

MAX_PACKET_BYTES = 512
_SERIAL_MAGIC1 = 0x94
_SERIAL_MAGIC2 = 0xC3

# Slot registry injection
# main.py calls configure_webserial() during lifespan startup, injecting
# the NODE_REGISTRY dict and the auth dependency function.
# Until that call is made the module falls back to the legacy module-scan
# so existing single-slot deployments keep working with zero changes.

_slot_registry: Optional[Dict] = None
_auth_fn: Optional[Callable] = None


def configure_webserial(slot_registry: Dict, auth_fn: Callable) -> None:
    global _slot_registry, _auth_fn
    _slot_registry = slot_registry
    _auth_fn = auth_fn
    logger.info("Web Serial: slot registry injected (%d slot(s))", len(slot_registry))


# Per-slot session state

def _make_session() -> Dict[str, Any]:
    return {
        "connected":       False,
        "connect_time":    None,
        "packets_rx":      0,
        "packets_tx":      0,
        "errors":          0,
        "last_seen":       None,
        "config_complete": False,
    }


# slot_id → session dict
_ws_sessions: Dict[str, Dict[str, Any]] = {}


def _get_session(slot_id: str) -> Dict[str, Any]:
    if slot_id not in _ws_sessions:
        _ws_sessions[slot_id] = _make_session()
    return _ws_sessions[slot_id]


# Slot resolution helpers

def _resolve_slot(slot_id: str):
    """Return (meshtastic_data, packet_queue) for the given slot_id.

    If the slot registry has been injected (multi-slot mode) use it.
    Otherwise fall back to the legacy module-scan so a plain single-slot
    deployment needs no changes at all.
    """
    if _slot_registry is not None:
        slot = _slot_registry.get(slot_id)
        if slot is None:
            raise HTTPException(404, f"Slot '{slot_id}' not found")
        return slot.meshtastic_data, slot.packet_queue

    # Legacy fallback — scan loaded modules for the globals
    mod = _legacy_get_app_module()
    if mod is None:
        raise HTTPException(503, "Web Serial: app module not available")
    md = getattr(mod, "meshtastic_data", None)
    q = getattr(mod, "packet_queue", None)
    return md, q


_legacy_module_cache = None


def _legacy_get_app_module():
    global _legacy_module_cache
    if _legacy_module_cache is not None:
        return _legacy_module_cache
    import sys
    import importlib
    for name, mod in sys.modules.items():
        if hasattr(mod, "meshtastic_data") and hasattr(mod, "packet_queue"):
            _legacy_module_cache = mod
            logger.info("Web Serial: legacy module resolved as '%s'", name)
            return mod
    for candidate in ("main", "meshtastic_dashboard", "app"):
        try:
            mod = importlib.import_module(candidate)
            if hasattr(mod, "meshtastic_data") and hasattr(mod, "packet_queue"):
                _legacy_module_cache = mod
                return mod
        except ImportError:
            continue
    logger.error("Web Serial: could not resolve app module")
    return None


# Auth helper

async def _check_auth(request: Request) -> None:
    if _auth_fn is not None:
        try:
            await _auth_fn(request)
        except Exception as exc:
            if hasattr(exc, "status_code") and exc.status_code == 302:
                raise HTTPException(401, "Authentication required")
            raise
        return

    # Legacy path — pull auth fn from module
    mod = _legacy_get_app_module()
    if mod is None:
        return
    fn = getattr(mod, "get_current_active_user", None)
    if fn is None:
        return
    try:
        await fn(request)
    except Exception as exc:
        if hasattr(exc, "status_code") and exc.status_code == 302:
            raise HTTPException(401, "Authentication required")
        raise


def _disabled() -> None:
    if not WEB_SERIAL_ENABLED:
        raise HTTPException(503, "Web Serial disabled")


# Serial framing

def _wrap_frame(payload: bytes) -> bytes:
    return struct.pack(">BBH", _SERIAL_MAGIC1, _SERIAL_MAGIC2, len(payload)) + payload


# Pydantic models

class SendTextRequest(BaseModel):
    text:          str
    destinationId: str = "^all"
    channelIndex:  int = 0


class StatusRequest(BaseModel):
    status: str


# Routes

@web_serial_router.post("/packet")
async def ingest_packet(
    request: Request,
    slot_id: str = Query(default="node_0"),
):
    _disabled()
    await _check_auth(request)

    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty body")
    if len(body) > MAX_PACKET_BYTES:
        raise HTTPException(413, f"Packet too large ({len(body)} bytes, max {MAX_PACKET_BYTES})")

    session = _get_session(slot_id)
    session["last_seen"]   = time.time()
    session["packets_rx"] += 1

    try:
        from meshtastic import mesh_pb2
        from_radio = mesh_pb2.FromRadio()
        from_radio.ParseFromString(bytes(body))
    except Exception as e:
        session["errors"] += 1
        logger.debug("Web Serial [%s]: protobuf decode error (skipping): %s", slot_id, e)
        return JSONResponse({"event": "ignored", "type": "decode_error"})

    variant = from_radio.WhichOneof("payload_variant")

    if variant == "my_info":
        try:
            md, _ = _resolve_slot(slot_id)
            if md:
                node_num = from_radio.my_info.my_node_num
                nid = f"!{node_num:08x}"
                if md.local_node_id != nid or not md.local_node_info:
                    md.local_node_id = nid
                    md.local_node_info = md.local_node_info or {}
                    md.local_node_info.update({"node_id": nid, "node_num": node_num, "node_id_hex": nid})
                    if nid not in md.nodes:
                        md.nodes[nid] = {}
                    md.nodes[nid]["isLocal"] = True
                    md.nodes[nid]["node_id"] = nid
                    logger.info("Web Serial [%s]: local node = %s", slot_id, nid)
        except HTTPException:
            raise
        except Exception as e:
            logger.debug("Web Serial [%s]: my_info error: %s", slot_id, e)
        return JSONResponse({"event": "local_node_info", "data": {
            "node_id":  f"!{from_radio.my_info.my_node_num:08x}",
            "node_num":  from_radio.my_info.my_node_num,
        }})

    if variant == "config_complete_id":
        if not session.get("config_complete"):
            session["config_complete"] = True
            logger.info("Web Serial [%s]: config burst complete — stream initialised", slot_id)
        return JSONResponse({"event": "connection_status", "data": "Connected"})

    if variant == "node_info":
        n = from_radio.node_info
        return JSONResponse({"event": "node_update", "data": {
            "node_id":    f"!{n.num:08x}",
            "node_num":   n.num,
            "snr":        n.snr,
            "last_heard": n.last_heard,
        }})

    if variant != "packet":
        return JSONResponse({"event": "ignored", "type": variant or "unknown"})

    try:
        packet_dict = _decode_mesh_packet(from_radio.packet)
    except Exception as e:
        session["errors"] += 1
        logger.error("Web Serial [%s]: packet decode error: %s\n%s", slot_id, e, traceback.format_exc())
        return JSONResponse({"event": "ignored", "type": "conversion_error"})

    packet_dict["_from_webserial"]   = True
    packet_dict["source"]            = "WEBSERIAL"
    packet_dict["source_confidence"] = 1.0

    try:
        _, q = _resolve_slot(slot_id)
        if q:
            try:
                q.put_nowait(packet_dict)
            except asyncio.QueueFull:
                logger.warning("Web Serial [%s]: packet queue full — dropping", slot_id)
                return JSONResponse({"event": "dropped", "reason": "queue_full"})
    except HTTPException:
        raise

    return JSONResponse({"event": "packet", "data": packet_dict})


@web_serial_router.post("/send")
async def encode_and_send(
    req: SendTextRequest,
    request: Request,
    slot_id: str = Query(default="node_0"),
):
    _disabled()
    await _check_auth(request)

    if not req.text or len(req.text) > 228:
        raise HTTPException(400, "Message empty or too long (max 228 chars)")

    try:
        raw = _encode_text(req.text, req.destinationId, req.channelIndex)
    except Exception as e:
        logger.error("Web Serial encode error: %s", e)
        raise HTTPException(500, f"Encode error: {e}")

    session = _get_session(slot_id)
    session["packets_tx"] += 1
    session["last_seen"]   = time.time()

    try:
        md, q = _resolve_slot(slot_id)
        if md and q:
            try:
                q.put_nowait({
                    "fromId":              md.local_node_id or "unknown",
                    "toId":                req.destinationId,
                    "channel":             req.channelIndex,
                    "decoded":             {"payload": req.text},
                    "app_packet_type":     "Message",
                    "rxTime":              int(time.time()),
                    "_synthetic_outbound": True,
                })
            except asyncio.QueueFull:
                pass
    except HTTPException:
        raise

    return FastAPIResponse(content=raw, media_type="application/octet-stream")


@web_serial_router.post("/status")
async def set_status(
    request: Request,
    slot_id: str = Query(default="node_0"),
):
    _disabled()
    await _check_auth(request)

    try:
        body   = await request.body()
        try:
            data   = _json.loads(body)
            status = str(data.get("status", "")).lower().strip()
        except Exception:
            status = body.decode("utf-8", errors="replace").strip().lower()
    except Exception:
        status = ""

    if not status:
        return JSONResponse({"ok": False, "error": "no status provided"})

    session = _get_session(slot_id)

    if status == "connected":
        session.update({
            "connected":       True,
            "connect_time":    time.time(),
            "packets_rx":      0,
            "packets_tx":      0,
            "errors":          0,
            "config_complete": False,
        })
        logger.info("Web Serial [%s]: browser connected a serial port", slot_id)
        try:
            md, _ = _resolve_slot(slot_id)
            if md:
                md.set_connection_status("Web Serial (Browser)")
        except HTTPException:
            pass

    elif status == "disconnected":
        was = session["connected"]
        session["connected"] = False
        logger.info("Web Serial [%s]: browser disconnected serial port", slot_id)
        if was:
            try:
                md, _ = _resolve_slot(slot_id)
                if md:
                    md.set_connection_status("Disconnected")
                    # In PUBLIC_MODE wipe all in-memory session state so the
                    # next browser session starts completely clean
                    _pm = False
                    if _slot_registry is not None:
                        slot = _slot_registry.get(slot_id)
                        if slot is not None:
                            import sys
                            _main = None
                            for _mn in ("meshtastic_dashboard", "__main__", "main"):
                                _main = sys.modules.get(_mn)
                                if _main and hasattr(_main, "PUBLIC_MODE"):
                                    break
                            _pm = bool(getattr(_main, "PUBLIC_MODE", False)) if _main else False
                    if _pm:
                        md.nodes.clear()
                        md.local_node_id = None
                        md.local_node_info = None
                        md.packets.clear()
                        md.stats["packets_received_session"] = 0
                        md.stats["text_messages_session"] = 0
                        md.stats["position_updates_session"] = 0
                        md.stats["telemetry_reports_session"] = 0
                        md.stats["nodes_seen_session"] = set()
                        md.stats["channels_seen_session"] = set()
                        # Reset the session dict so next connect starts fresh
                        _ws_sessions.pop(slot_id, None)
                        logger.info("Web Serial [%s]: PUBLIC_MODE — session state wiped on disconnect", slot_id)
            except HTTPException:
                pass

    return JSONResponse({"ok": True, "connected": session["connected"]})


@web_serial_router.get("/status")
async def get_status(
    request: Request,
    slot_id: str = Query(default="node_0"),
):
    _disabled()
    await _check_auth(request)
    session = _get_session(slot_id)
    return JSONResponse({
        "enabled":      WEB_SERIAL_ENABLED,
        "slot_id":      slot_id,
        "connected":    session["connected"],
        "connect_time": session["connect_time"],
        "packets_rx":   session["packets_rx"],
        "packets_tx":   session["packets_tx"],
        "errors":       session["errors"],
        "last_seen":    session["last_seen"],
    })


@web_serial_router.get("/sessions")
async def get_all_sessions(request: Request):
    """Returns session state for every active slot — useful for multi-node dashboards."""
    _disabled()
    await _check_auth(request)
    return JSONResponse({
        "sessions": {sid: dict(sess) for sid, sess in _ws_sessions.items()}
    })


@web_serial_router.get("/wakeup")
async def get_wakeup_frame(
    request: Request,
    slot_id: str = Query(default="node_0"),
):
    """Generates the ToRadio(want_config_id) frame.
    Sending this to the ESP32 triggers it to dump MyInfo, NodeDB, and Channels.
    """
    _disabled()
    await _check_auth(request)
    try:
        from meshtastic import mesh_pb2
        to_radio = mesh_pb2.ToRadio()
        to_radio.want_config_id = random.randint(10000, 999999)
        raw = _wrap_frame(to_radio.SerializeToString())
        return FastAPIResponse(content=raw, media_type="application/octet-stream")
    except Exception as e:
        logger.error("Web Serial wakeup encode error: %s", e)
        raise HTTPException(500, f"Wakeup error: {e}")


# Protobuf helpers

_PORTNUM_TYPE = {
    1:   "Message",
    3:   "Position",
    67:  "Telemetry",
    4:   "NodeInfo",
    5:   "Routing",
    70:  "RemoteHardware",
    71:  "MapReport",
    72:  "StoreAndForward",
    73:  "RangeTest",
    257: "Admin",
    256: "Reply",
}


def _decode_mesh_packet(mp) -> Dict:
    from_num = getattr(mp, "from", 0)
    to_num   = mp.to

    packet: Dict = {
        "id":       mp.id,
        "from":     from_num,
        "to":       to_num,
        "channel":  mp.channel,
        "hopLimit": mp.hop_limit,
        "hopStart": mp.hop_start,
        "wantAck":  mp.want_ack,
        "rxSnr":    mp.rx_snr,
        "rxRssi":   mp.rx_rssi,
        "rxTime":   mp.rx_time or int(time.time()),
        "viaMqtt":  mp.via_mqtt,
        "priority": mp.priority,
    }

    if from_num:
        packet["fromId"] = f"!{from_num:08x}"
    if to_num:
        packet["toId"] = "^all" if to_num == 0xffffffff else f"!{to_num:08x}"

    if mp.HasField("decoded"):
        d = mp.decoded
        decoded: Dict = {"portnum": d.portnum}

        try:
            from meshtastic import portnums_pb2 as _pn, mesh_pb2, telemetry_pb2

            pnum = d.portnum
            packet["app_packet_type"] = _PORTNUM_TYPE.get(pnum, f"port_{pnum}")

            if pnum == _pn.PortNum.TEXT_MESSAGE_APP:
                text = d.payload.decode("utf-8", errors="replace")
                decoded["text"]    = text
                decoded["payload"] = text

            elif pnum == _pn.PortNum.POSITION_APP:
                pos = mesh_pb2.Position()
                pos.ParseFromString(d.payload)
                pd = {}
                if pos.latitude_i:   pd["latitude"]   = pos.latitude_i  * 1e-7; pd["latitudeI"]  = pos.latitude_i
                if pos.longitude_i:  pd["longitude"]  = pos.longitude_i * 1e-7; pd["longitudeI"] = pos.longitude_i
                if pos.altitude:     pd["altitude"]   = pos.altitude
                if pos.sats_in_view: pd["satsInView"] = pos.sats_in_view
                if pos.time:         pd["time"]       = pos.time
                decoded["position"] = pd

            elif pnum == _pn.PortNum.TELEMETRY_APP:
                tel = telemetry_pb2.Telemetry()
                tel.ParseFromString(d.payload)
                td = {}
                if tel.HasField("device_metrics"):
                    dm = tel.device_metrics
                    td["deviceMetrics"] = {
                        "batteryLevel":       dm.battery_level,
                        "voltage":            round(dm.voltage, 3),
                        "channelUtilization": round(dm.channel_utilization, 2),
                        "airUtilTx":          round(dm.air_util_tx, 2),
                        "uptimeSeconds":      dm.uptime_seconds,
                    }
                if tel.HasField("environment_metrics"):
                    em = tel.environment_metrics
                    td["environmentMetrics"] = {
                        "temperature":        round(em.temperature, 2),
                        "relativeHumidity":   round(em.relative_humidity, 2),
                        "barometricPressure": round(em.barometric_pressure, 2),
                        "gasResistance":      round(em.gas_resistance, 2),
                        "iaq":                em.iaq,
                    }
                decoded["telemetry"] = td

            elif pnum == _pn.PortNum.NODEINFO_APP:
                user = mesh_pb2.User()
                user.ParseFromString(d.payload)
                decoded["user"] = {
                    "id":        user.id,
                    "longName":  user.long_name,
                    "shortName": user.short_name,
                    "macaddr":   user.macaddr.hex() if user.macaddr else None,
                    "hwModel":   str(user.hw_model),
                    "role":      str(user.role),
                }

            elif pnum == _pn.PortNum.ROUTING_APP:
                r = mesh_pb2.Routing()
                r.ParseFromString(d.payload)
                decoded["routing"]     = {"errorReason": str(r.error_reason), "requestId": r.request_id}
                decoded["requestId"]   = r.request_id
                decoded["errorReason"] = str(r.error_reason)

        except ImportError:
            import base64
            decoded["raw_payload_b64"] = base64.b64encode(d.payload).decode()
        except Exception:
            pass

        packet["decoded"]   = decoded
        packet["encrypted"] = False
    else:
        packet["encrypted"]        = True
        packet["decoded"]          = {}
        packet["app_packet_type"]  = "Encrypted"

    return packet


def _encode_text(text: str, destination_id: str, channel_index: int) -> bytes:
    from meshtastic import mesh_pb2, portnums_pb2

    data = mesh_pb2.Data()
    data.portnum       = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    data.payload       = text.encode("utf-8")
    data.want_response = False

    mp = mesh_pb2.MeshPacket()
    mp.decoded.CopyFrom(data)
    mp.channel   = channel_index
    mp.want_ack  = destination_id != "^all"
    mp.hop_limit = 3

    if destination_id == "^all":
        mp.to = 0xffffffff
    elif destination_id.startswith("!"):
        try:
            mp.to = int(destination_id[1:], 16)
        except ValueError:
            mp.to = 0xffffffff
    else:
        try:
            mp.to = int(destination_id, 16)
        except ValueError:
            mp.to = 0xffffffff

    to_radio = mesh_pb2.ToRadio()
    to_radio.packet.CopyFrom(mp)
    return _wrap_frame(to_radio.SerializeToString())