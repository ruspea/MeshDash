"""
Node Admin Plugin — Backend API v1.4
=====================================

Based on official meshtastic Python CLI source (__main__.py) patterns:

OFFICIAL PATTERN FOR REMOTE CONFIG WRITES:
  node = iface.getNode(dest, False)   # False = no channel sync
  node.localConfig.X.field = value
  node.writeConfig("section")         # fire and forget — no waitForAckNak
  # The node sends an implicit ACK via onAckNak callback if it receives it
  # But we do NOT block waiting — just report "sent"

OFFICIAL PATTERN FOR REBOOT/SHUTDOWN (commands that need ACK):
  node = iface.getNode(dest, False)
  node.reboot()
  iface.waitForAckNak()               # blocks up to 20s for explicit ACK
  # Then read iface._acknowledgment

KEY INSIGHT: setOwner, writeConfig do NOT use waitForAckNak in the CLI.
Only reboot, shutdown, and a few others do.

NO TRANSACTIONS for individual commands — transactions are only used when
batching multiple --set commands together in the CLI.

VERIFIED CORRECT METHODS (from node.py source):
  node.writeConfig("device"|"lora"|"position"|"display"|"power"|"bluetooth"|
                   "network"|"security"|"mqtt"|"serial"|"telemetry"|
                   "canned_message"|"ambient_lighting"|"paxcounter")
  node.writeChannel(index)
  node.setOwner(long_name=, short_name=)
  node.setFixedPosition(lat, lon, alt)
  node.removeFixedPosition()
  node.reboot(secs=N)           <- uses waitForAckNak
  node.shutdown(secs=N)         <- uses waitForAckNak
  node.factoryReset(full=False) <- uses waitForAckNak
  node.resetNodeDb()
  node.setFavorite(nodeId)
  node.removeFavorite(nodeId)
  node.setIgnored(nodeId)
  node.removeIgnored(nodeId)
  node.setTime(timeSec=0)
"""

import asyncio
import base64
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger        = logging.getLogger("plugin.node_admin")
plugin_router = APIRouter()

_node_registry: Dict[str, Any] = {}
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_op_log: List[Dict] = []
_op_lock = threading.Lock()
_MAX_LOG = 150


def _log(slot_id, target, op, status, detail="", nak_reason=""):
    e = {"ts": time.time(), "slot_id": slot_id, "target": target,
         "op": op, "status": status, "detail": detail}
    if nak_reason:
        e["nak_reason"] = nak_reason
    with _op_lock:
        _op_log.append(e)
        if len(_op_log) > _MAX_LOG:
            _op_log.pop(0)
    logger.info("NODE_ADMIN [%s] %s \u2192 %s [%s] %s", slot_id, target, op, status, detail)


def init_plugin(context):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    logger.info("Node Admin v1.4 \u2014 %d slot(s)", len(_node_registry))
    if _event_loop:
        try:
            asyncio.run_coroutine_threadsafe(_watchdog(context), _event_loop)
        except Exception as e:
            logger.error("Watchdog start: %s", e)


async def _watchdog(context):
    wd, pid = context.get("plugin_watchdog"), context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Watchdog: %s", e)


# ---------------------------------------------------------------------------
# Slot / interface helpers
# ---------------------------------------------------------------------------

def _get_slot(slot_id):
    s = _node_registry.get(slot_id)
    if s is None:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")
    return s


def _get_iface(slot_id):
    slot = _get_slot(slot_id)
    cm   = slot.connection_manager
    if cm is None:
        raise HTTPException(503, f"Slot '{slot_id}' has no connection manager.")
    try:
        ct = (cm.config.get("MESHTASTIC_CONNECTION_TYPE") or "").upper()
    except Exception:
        ct = ""
    if ct in ("MQTT", "MESHCORE"):
        raise HTTPException(501,
            f"Admin commands require a direct radio connection (TCP/Serial/BLE). "
            f"This slot uses {ct}.")
    if not cm.is_ready.is_set():
        raise HTTPException(503, "Radio not ready.")
    iface = getattr(cm, "interface", None) or getattr(cm, "_interface", None)
    if iface is None:
        raise HTTPException(503, "Interface not available.")
    return iface


def _local_id(slot_id):
    try:
        return _get_slot(slot_id).meshtastic_data.local_node_id
    except Exception:
        return None


def _is_local(node_id, slot_id):
    return node_id == _local_id(slot_id)


def _get_node(iface, node_id, slot_id):
    """Get node object. For remote nodes uses getNode(dest, False) — no channel sync."""
    try:
        if _is_local(node_id, slot_id):
            return iface.localNode
        node = iface.getNode(node_id, False)   # False = requestChannels=False
        if node is None:
            raise ValueError(f"getNode returned None for {node_id}")
        return node
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Cannot get node {node_id}: {e}") from e


def _pub_key(slot_id):
    try:
        iface = _get_iface(slot_id)
        pk = None
        if hasattr(iface, "myInfo") and iface.myInfo:
            pk = iface.myInfo.get("publicKey") or iface.myInfo.get("public_key")
        if not pk and hasattr(iface, "localNode"):
            try:
                raw = iface.localNode.localConfig.security.public_key
                if raw:
                    pk = base64.b64encode(raw).decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            except Exception:
                pass
        if pk:
            if isinstance(pk, (bytes, bytearray)):
                pk = base64.b64encode(pk).decode()
            return str(pk)
    except Exception as e:
        logger.debug("pub_key %s: %s", slot_id, e)
    return None


# ---------------------------------------------------------------------------
# ACK helpers
#
# OFFICIAL PATTERN (from __main__.py):
#   - Config writes (setOwner, writeConfig): NO waitForAckNak — fire and forget
#     The node's onAckNak callback fires asynchronously if/when it receives it
#   - Control commands (reboot, shutdown): waitForAckNak = True
#
# We report "sent" for config writes (that's all the CLI does too).
# For control commands we wait and report the actual ACK/NAK.
# ---------------------------------------------------------------------------

_NAK_REASONS = {
    "PKI_UNKNOWN_PUBKEY": (
        "Admin key not recognised \u2014 this radio's public key is not registered "
        "in the remote node's Security Config > Admin Key. See Setup Guide tab."
    ),
    "NO_CHANNEL": (
        "NO_CHANNEL \u2014 admin packet routing failed. "
        "Try: pip install --upgrade meshtastic"
    ),
    "NOT_AUTHORIZED": "Not authorised \u2014 check admin key is registered correctly.",
    "PKI_FAILED_VERIFICATION": "PKI verification failed \u2014 admin key mismatch.",
}


def _wait_ack(iface) -> dict:
    """
    Block until ACK/NAK received or timeout (official pattern for reboot/shutdown).
    Returns ack dict: {"ack": "ok"|"implicit"|"nak"|"timeout", "msg": str}
    """
    try:
        iface.waitForAckNak()
    except Exception as e:
        err = str(e).lower()
        if "timed out" in err or "timeout" in err:
            return {"ack": "timeout", "msg": "No response within timeout. Node may be out of range or offline."}
        logger.debug("waitForAckNak: %s", e)

    ack = getattr(iface, "_acknowledgment", None)
    if ack is None:
        return {"ack": "timeout", "msg": "No acknowledgment object."}

    if getattr(ack, "receivedNak", False):
        reason = _last_nak_reason.get("reason", "")
        msg = _NAK_REASONS.get(reason) or f"NAK \u2014 {reason or 'rejected by remote node'}"
        return {"ack": "nak", "msg": msg, "reason": reason}

    if getattr(ack, "receivedAck", False):
        return {"ack": "ok", "msg": "ACK \u2014 remote node confirmed receipt."}

    if getattr(ack, "receivedImplAck", False):
        return {"ack": "implicit", "msg": "Implicit ACK \u2014 packet transmitted."}

    return {"ack": "timeout", "msg": "No response within timeout."}


def _reset_ack(iface):
    _last_nak_reason["reason"] = ""
    ack = getattr(iface, "_acknowledgment", None)
    if ack and hasattr(ack, "reset"):
        try:
            ack.reset()
        except Exception:
            pass


_last_nak_reason: dict = {"reason": ""}


def _patch_onak(node):
    """Capture the specific NAK error reason from routing packets."""
    original = getattr(node, "_original_onak", None) or node.onAckNak
    def patched(p):
        try:
            reason = p.get("decoded", {}).get("routing", {}).get("errorReason", "")
            _last_nak_reason["reason"] = reason if reason and reason != "NONE" else ""
        except Exception:
            pass
        return original(p)
    node._original_onak = original
    node.onAckNak = patched


# ---------------------------------------------------------------------------
# Execution wrapper
# ---------------------------------------------------------------------------

async def _run(slot_id, node_id, op, fn):
    """fn() returns (detail_str, ack_dict)."""
    _log(slot_id, node_id, op, "pending")
    try:
        detail, ack = await asyncio.to_thread(fn)
        status     = ack.get("ack", "ok")
        nak_reason = ack.get("reason", "") if status == "nak" else ""
        log_status = "nak" if status == "nak" else "timeout" if status == "timeout" else "ok"
        _log(slot_id, node_id, op, log_status, f"{detail} [{status}]", nak_reason)
        return {"status": log_status, "operation": op, "node_id": node_id,
                "detail": detail, "ack": ack}
    except HTTPException:
        raise
    except Exception as e:
        _log(slot_id, node_id, op, "error", str(e))
        raise HTTPException(500, f"{op} failed: {e}") from e


# ---------------------------------------------------------------------------
# Enum maps
# ---------------------------------------------------------------------------

ROLE_MAP = {
    "CLIENT": 0, "CLIENT_MUTE": 1, "ROUTER": 2, "ROUTER_CLIENT": 3,
    "REPEATER": 4, "TRACKER": 5, "SENSOR": 6, "TAK": 7,
    "CLIENT_HIDDEN": 8, "LOST_AND_FOUND": 9, "TAK_TRACKER": 10,
}
PRESET_MAP = {
    "LONG_FAST": 0, "LONG_SLOW": 1, "VERY_LONG_SLOW": 2, "MEDIUM_SLOW": 3,
    "MEDIUM_FAST": 4, "SHORT_SLOW": 5, "SHORT_FAST": 6, "LONG_MODERATE": 7,
}
REGION_MAP = {
    "US": 1, "EU_433": 2, "EU_868": 3, "CN": 4, "JP": 5, "ANZ": 6,
    "KR": 7, "TW": 8, "RU": 9, "IN": 10, "NZ_865": 11, "TH": 12,
    "LORA_24": 13, "UA_433": 14, "UA_868": 15, "MY_433": 16,
    "MY_919": 17, "SG_923": 18,
}
CH_ROLE_MAP = {"PRIMARY": 1, "SECONDARY": 2, "DISABLED": 0}


# ---------------------------------------------------------------------------
# ACK responses for different command types
# ---------------------------------------------------------------------------

def _ack_sent(is_remote):
    """Config write — fire and forget, just report sent."""
    if not is_remote:
        return {"ack": "local", "msg": "Local node \u2014 written synchronously."}
    return {"ack": "sent", "msg": "Command sent over mesh. Reboot node to apply."}


def _ack_wait(iface, node, is_remote):
    """Control command — wait for ACK/NAK."""
    if not is_remote:
        return {"ack": "local", "msg": "Local node \u2014 command sent."}
    return _wait_ack(iface)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class _Base(BaseModel):
    slot_id: str
    node_id: str


class OwnerReq(_Base):
    long_name:  str = Field(..., min_length=1, max_length=39)
    short_name: str = Field(..., min_length=1, max_length=4)


class DeviceReq(_Base):
    role: str = "CLIENT"
    node_info_broadcast_secs: int = Field(3600, ge=3600, le=86400)


class LoraReq(_Base):
    use_preset:    bool = True
    modem_preset:  str  = "LONG_FAST"
    bandwidth:     Optional[int] = None
    spread_factor: Optional[int] = None
    coding_rate:   Optional[int] = None
    region:        str  = "EU_868"
    hop_limit:     int  = Field(3, ge=1, le=7)
    tx_power:      Optional[int] = None


class PosReq(_Base):
    position_broadcast_secs: int = Field(900, ge=32, le=86400)
    smart_position:  bool = True
    fixed_position:  bool = False
    fixed_lat:       Optional[float] = None
    fixed_lon:       Optional[float] = None
    fixed_alt:       Optional[int]   = None


class MqttReq(_Base):
    enabled:                 bool = False
    address:                 str  = ""
    username:                str  = ""
    password:                str  = ""
    encryption_enabled:      bool = True
    json_enabled:            bool = False
    root:                    str  = ""
    proxy_to_client_enabled: bool = False
    map_reporting_enabled:   bool = False


class TelemetryReq(_Base):
    device_update_interval:          int  = Field(3600, ge=0, le=86400)
    environment_update_interval:     int  = Field(0,    ge=0, le=86400)
    environment_measurement_enabled: bool = False
    air_quality_enabled:             bool = False
    air_quality_interval:            int  = Field(0, ge=0, le=86400)


class DisplayReq(_Base):
    screen_on_secs: int  = Field(0, ge=0, le=86400)
    units:          int  = Field(0, ge=0, le=1)
    flip_screen:    bool = False
    oled:           int  = Field(0, ge=0, le=3)
    gps_format:     int  = Field(0, ge=0, le=5)


class PowerReq(_Base):
    min_wake_secs:       int = Field(10,  ge=0, le=3600)
    sds_secs:            int = Field(0,   ge=0)
    ls_secs:             int = Field(300, ge=0, le=86400)
    wait_bluetooth_secs: int = Field(60,  ge=0, le=3600)


class BluetoothReq(_Base):
    enabled:   bool = True
    mode:      int  = Field(0, ge=0, le=2)
    fixed_pin: int  = Field(123456, ge=0, le=999999)


class RebootReq(_Base):
    delay_seconds: int = Field(5, ge=0, le=60)


class ShutdownReq(_Base):
    delay_seconds: int = Field(5, ge=0, le=60)


class SetTimeReq(_Base):
    unix_timestamp: int = 0


class FactoryReq(_Base):
    confirm:           str
    full_device_reset: bool = False


class PurgeDbReq(_Base):
    confirm: str


class ChReadReq(_Base):
    pass


class ChSetReq(_Base):
    channel_index: int  = Field(..., ge=0, le=7)
    name:          str  = ""
    psk_b64:       str  = ""
    role:          str  = "SECONDARY"
    uplink:        bool = True
    downlink:      bool = True


class NodeRefReq(_Base):
    target_node_id: str


# ---------------------------------------------------------------------------
# Routes — health / metadata
# ---------------------------------------------------------------------------

@plugin_router.get("")
@plugin_router.get("/")
async def health():
    return {"plugin": "node_admin", "version": "1.4.0", "status": "running",
            "slots": len(_node_registry)}


@plugin_router.get("/slots")
async def list_slots():
    out = []
    for sid, slot in _node_registry.items():
        try:
            cm = slot.connection_manager
            try:
                ct = (cm.config.get("MESHTASTIC_CONNECTION_TYPE") or "").upper()
            except Exception:
                ct = "UNKNOWN"
            ok    = ct not in ("MQTT", "MESHCORE")
            ready = cm.is_ready.is_set() if cm else False
            pk    = _pub_key(sid) if (ok and ready) else None
            out.append({
                "slot_id": sid, "label": getattr(slot, "label", sid),
                "local_node_id": slot.meshtastic_data.local_node_id,
                "is_ready": ready, "connection_type": ct,
                "admin_supported": ok,
                "node_count": len(slot.meshtastic_data.nodes),
                "local_public_key": pk,
            })
        except Exception:
            out.append({"slot_id": sid, "label": sid, "local_node_id": None,
                        "is_ready": False, "connection_type": "UNKNOWN",
                        "admin_supported": False, "node_count": 0, "local_public_key": None})
    return {"slots": out}


@plugin_router.get("/nodes/{slot_id}")
async def list_nodes(slot_id: str):
    slot = _get_slot(slot_id)
    lid  = _local_id(slot_id)
    ns   = []
    for nid, nd in slot.meshtastic_data.nodes.items():
        u = nd.get("user") or {}
        ns.append({
            "node_id":    nid,
            "long_name":  u.get("longName")  or nd.get("long_name")  or nid,
            "short_name": u.get("shortName") or nd.get("short_name") or "?",
            "hw_model":   u.get("hwModel")   or nd.get("hw_model")   or "",
            "is_local":   nid == lid,
            "last_heard": nd.get("lastHeard") or nd.get("last_heard") or 0,
            "snr":        nd.get("snr"),
            "hops_away":  nd.get("hopsAway") or nd.get("hops_away"),
        })
    ns.sort(key=lambda n: (not n["is_local"], -(n["last_heard"] or 0)))
    return {"slot_id": slot_id, "nodes": ns}


@plugin_router.get("/public_key/{slot_id}")
async def get_public_key(slot_id: str):
    pk  = _pub_key(slot_id)
    lid = _local_id(slot_id)
    return {"slot_id": slot_id, "local_node_id": lid, "public_key": pk,
            "note": "" if pk else "Not available — check firmware \u22652.5 and radio connected"}


# ---------------------------------------------------------------------------
# Config write routes — FIRE AND FORGET (no waitForAckNak)
# Official CLI pattern: just call the method and move on
# ---------------------------------------------------------------------------

@plugin_router.post("/set_owner")
async def set_owner(r: OwnerReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.setOwner(long_name=r.long_name, short_name=r.short_name)
        return f"'{r.long_name}' / '{r.short_name}'", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_owner", _do)


@plugin_router.post("/set_device_config")
async def set_device_config(r: DeviceReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.localConfig.device.role = ROLE_MAP.get(r.role.upper(), 0)
        node.localConfig.device.node_info_broadcast_secs = r.node_info_broadcast_secs
        node.writeConfig("device")
        return f"role={r.role} interval={r.node_info_broadcast_secs}s", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_device_config", _do)


@plugin_router.post("/set_lora_config")
async def set_lora_config(r: LoraReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        lc   = node.localConfig.lora
        lc.region    = REGION_MAP.get(r.region.upper(), 3)
        lc.hop_limit = r.hop_limit
        if r.tx_power is not None:
            lc.tx_power = r.tx_power
        if r.use_preset:
            lc.use_preset   = True
            lc.modem_preset = PRESET_MAP.get(r.modem_preset.upper(), 0)
            detail = f"region={r.region} hops={r.hop_limit} preset={r.modem_preset}"
        else:
            lc.use_preset = False
            if r.bandwidth     is not None: lc.bandwidth     = r.bandwidth
            if r.spread_factor is not None: lc.spread_factor = r.spread_factor
            if r.coding_rate   is not None: lc.coding_rate   = r.coding_rate
            detail = f"region={r.region} hops={r.hop_limit} manual"
        node.writeConfig("lora")
        return detail, _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_lora_config", _do)


@plugin_router.post("/set_position_config")
async def set_position_config(r: PosReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        if r.fixed_position and r.fixed_lat is not None:
            node.setFixedPosition(r.fixed_lat, r.fixed_lon or 0.0, r.fixed_alt or 0)
        else:
            pc = node.localConfig.position
            pc.position_broadcast_secs = r.position_broadcast_secs
            pc.gps_mode       = 1 if r.smart_position else 0
            pc.fixed_position = False
            node.writeConfig("position")
        return f"interval={r.position_broadcast_secs}s smart={r.smart_position}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_position_config", _do)


@plugin_router.post("/set_mqtt_config")
async def set_mqtt_config(r: MqttReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        mc = node.moduleConfig.mqtt
        mc.enabled                 = r.enabled
        mc.address                 = r.address
        mc.username                = r.username
        mc.password                = r.password
        mc.encryption_enabled      = r.encryption_enabled
        mc.json_enabled            = r.json_enabled
        mc.root                    = r.root
        mc.proxy_to_client_enabled = r.proxy_to_client_enabled
        mc.map_reporting_enabled   = r.map_reporting_enabled
        node.writeConfig("mqtt")
        return f"enabled={r.enabled} address={r.address or '(default)'}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_mqtt_config", _do)


@plugin_router.post("/set_telemetry_config")
async def set_telemetry_config(r: TelemetryReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        tc = node.moduleConfig.telemetry
        tc.device_update_interval          = r.device_update_interval
        tc.environment_update_interval     = r.environment_update_interval
        tc.environment_measurement_enabled = r.environment_measurement_enabled
        tc.air_quality_enabled             = r.air_quality_enabled
        tc.air_quality_interval            = r.air_quality_interval
        node.writeConfig("telemetry")
        return f"device={r.device_update_interval}s env={r.environment_update_interval}s", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_telemetry_config", _do)


@plugin_router.post("/set_display_config")
async def set_display_config(r: DisplayReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        dc = node.localConfig.display
        dc.screen_on_secs = r.screen_on_secs
        dc.units          = r.units
        dc.flip_screen    = r.flip_screen
        dc.oled           = r.oled
        dc.gps_format     = r.gps_format
        node.writeConfig("display")
        return f"screen_on={r.screen_on_secs}s units={'metric' if r.units==0 else 'imperial'}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_display_config", _do)


@plugin_router.post("/set_power_config")
async def set_power_config(r: PowerReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        pc = node.localConfig.power
        pc.min_wake_secs       = r.min_wake_secs
        pc.sds_secs            = r.sds_secs
        pc.ls_secs             = r.ls_secs
        pc.wait_bluetooth_secs = r.wait_bluetooth_secs
        node.writeConfig("power")
        return f"ls={r.ls_secs}s sds={r.sds_secs}s bt_wait={r.wait_bluetooth_secs}s", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_power_config", _do)


@plugin_router.post("/set_bluetooth_config")
async def set_bluetooth_config(r: BluetoothReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        bc = node.localConfig.bluetooth
        bc.enabled   = r.enabled
        bc.mode      = r.mode
        bc.fixed_pin = r.fixed_pin
        node.writeConfig("bluetooth")
        mode_label = {0: "random PIN", 1: "fixed PIN", 2: "no PIN"}.get(r.mode, str(r.mode))
        return f"enabled={r.enabled} mode={mode_label}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_bluetooth_config", _do)


# ---------------------------------------------------------------------------
# Control command routes — WAIT FOR ACK (official pattern: reboot, shutdown)
# ---------------------------------------------------------------------------

@plugin_router.post("/reboot")
async def reboot_node(r: RebootReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        if rem:
            _patch_onak(node)
            _reset_ack(iface)
        node.reboot(secs=r.delay_seconds)
        return f"Reboot in {r.delay_seconds}s", _ack_wait(iface, node, rem)
    return await _run(r.slot_id, r.node_id, "reboot", _do)


@plugin_router.post("/shutdown")
async def shutdown_node(r: ShutdownReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        if rem:
            _patch_onak(node)
            _reset_ack(iface)
        node.shutdown(secs=r.delay_seconds)
        return f"Shutdown in {r.delay_seconds}s", _ack_wait(iface, node, rem)
    return await _run(r.slot_id, r.node_id, "shutdown", _do)


@plugin_router.post("/set_time")
async def set_time(r: SetTimeReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        ts = r.unix_timestamp or int(time.time())
        node.setTime(ts)
        return f"Time set to {ts}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_time", _do)


@plugin_router.post("/factory_reset")
async def factory_reset(r: FactoryReq):
    if r.confirm != "FACTORY RESET":
        raise HTTPException(400, "Confirm text must be exactly: FACTORY RESET")
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        if rem:
            _patch_onak(node)
            _reset_ack(iface)
        node.factoryReset(full=r.full_device_reset)
        kind = "full wipe" if r.full_device_reset else "config reset"
        return f"Factory reset ({kind}) sent", _ack_wait(iface, node, rem)
    return await _run(r.slot_id, r.node_id, "factory_reset", _do)


@plugin_router.post("/purge_node_db")
async def purge_node_db(r: PurgeDbReq):
    if r.confirm != "PURGE DATABASE":
        raise HTTPException(400, "Confirm text must be exactly: PURGE DATABASE")
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.resetNodeDb()
        return "Node DB reset sent", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "purge_node_db", _do)


@plugin_router.post("/set_favorite")
async def set_favorite(r: NodeRefReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.setFavorite(r.target_node_id)
        return f"Favourited: {r.target_node_id}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_favorite", _do)


@plugin_router.post("/remove_favorite")
async def remove_favorite(r: NodeRefReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.removeFavorite(r.target_node_id)
        return f"Un-favourited: {r.target_node_id}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "remove_favorite", _do)


@plugin_router.post("/set_ignored")
async def set_ignored(r: NodeRefReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.setIgnored(r.target_node_id)
        return f"Ignored: {r.target_node_id}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_ignored", _do)


@plugin_router.post("/remove_ignored")
async def remove_ignored(r: NodeRefReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        node.removeIgnored(r.target_node_id)
        return f"Un-ignored: {r.target_node_id}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "remove_ignored", _do)


# ---------------------------------------------------------------------------
# Channel routes
# ---------------------------------------------------------------------------

@plugin_router.post("/read_channels")
async def read_channels(r: ChReadReq):
    iface = _get_iface(r.slot_id)
    def _do():
        if _is_local(r.node_id, r.slot_id):
            node = iface.localNode
        else:
            # For remote channels we need requestChannels=True
            node = iface.getNode(r.node_id, True)
        chs = []
        for i, ch in enumerate(getattr(node, "channels", None) or []):
            try:
                s   = getattr(ch, "settings", None)
                ri  = int(getattr(ch, "role", 0))
                psk = getattr(s, "psk", None) if s else None
                chs.append({
                    "index":    i,
                    "name":     getattr(s, "name", "") if s else "",
                    "role":     {0:"DISABLED",1:"PRIMARY",2:"SECONDARY"}.get(ri, str(ri)),
                    "psk_set":  bool(psk and len(psk) > 0),
                    "uplink":   bool(getattr(s, "uplink_enabled",   True) if s else True),
                    "downlink": bool(getattr(s, "downlink_enabled", True) if s else True),
                })
            except Exception as e:
                chs.append({"index": i, "error": str(e), "name": "", "role": "UNKNOWN",
                            "psk_set": False, "uplink": True, "downlink": True})
        return chs
    try:
        chs = await asyncio.to_thread(_do)
        return {
            "status": "ok", "node_id": r.node_id, "channels": chs,
            "note": ("" if _is_local(r.node_id, r.slot_id)
                     else "Remote channel read can be slow \u2014 click Load again if empty."),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Channel read failed: {e}") from e


@plugin_router.post("/set_channel")
async def set_channel(r: ChSetReq):
    iface = _get_iface(r.slot_id)
    rem   = not _is_local(r.node_id, r.slot_id)
    def _do():
        node = _get_node(iface, r.node_id, r.slot_id)
        if not node.channels or r.channel_index >= len(node.channels):
            raise ValueError(f"Channel {r.channel_index} not loaded \u2014 load channels first.")
        ch = node.channels[r.channel_index]
        ch.role                      = CH_ROLE_MAP.get(r.role.upper(), 2)
        ch.settings.name             = r.name
        ch.settings.uplink_enabled   = r.uplink
        ch.settings.downlink_enabled = r.downlink
        if r.psk_b64:
            try:
                ch.settings.psk = base64.b64decode(r.psk_b64)
            except Exception as e:
                raise ValueError(f"Invalid PSK base64: {e}") from e
        node.writeChannel(r.channel_index)
        return f"Channel {r.channel_index} '{r.name}' \u2192 {r.role}", _ack_sent(rem)
    return await _run(r.slot_id, r.node_id, "set_channel", _do)


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

@plugin_router.get("/log")
async def get_log(limit: int = 60):
    with _op_lock:
        return {"log": list(reversed(_op_log))[:limit]}