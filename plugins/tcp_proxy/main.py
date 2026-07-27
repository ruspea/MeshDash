import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pubsub import pub

try:
    from meshtastic.protobuf import (
        mesh_pb2, config_pb2, channel_pb2, portnums_pb2
    )
except ImportError:
    from meshtastic import (
        mesh_pb2, config_pb2, channel_pb2, portnums_pb2
    )

module_config_pb2 = None
try:
    from meshtastic.protobuf import module_config_pb2
except ImportError:
    try:
        from meshtastic import module_config_pb2
    except ImportError:
        pass  # Handled at build time — module config sections sent as empty frames

# Detect which field name FromRadio uses for module config.
# Older meshtastic-python (pre-2.3) used camelCase "moduleConfig";
# newer versions use snake_case "module_config" per protobuf3 Python conventions.
_FROMRADIO_MODULE_CONFIG_FIELD: str = "module_config"
try:
    _fr_test = mesh_pb2.FromRadio()
    if not hasattr(_fr_test, "module_config") and hasattr(_fr_test, "moduleConfig"):
        _FROMRADIO_MODULE_CONFIG_FIELD = "moduleConfig"
except Exception:
    pass

PLUGIN_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PLUGIN_DIR, "config.json")

START1     = 0x94
START2     = 0xC3
MAX_PACKET = 512 * 1024

plugin_router = APIRouter()
logger        = logging.getLogger("plugin.tcp_proxy")

DEFAULTS = {
    "enabled":           True,
    "port":              4403,
    "bind_host":         "0.0.0.0",
    "slot_id":           "node_0",
    "max_clients":       8,
    "session_ttl_secs":  7200,  # how long to keep offline sessions (2h)
    "session_queue_max": 100,   # max queued messages per session
}

_node_registry:   Dict[str, Any] = {}
_event_loop:      Optional[asyncio.AbstractEventLoop] = None
_plugin_watchdog: dict = {}
_plugin_id:       str  = "tcp_proxy"

_server:        Optional[asyncio.Server] = None
_server_task:   Any = None
_clients_lock:  Optional[asyncio.Lock] = None
_sessions_lock: Optional[asyncio.Lock] = None

_debug_log: deque = deque(maxlen=500)

_stats = {
    "pkts_to_clients":   0,
    "pkts_from_clients": 0,
    "total_connections": 0,
    "peak_clients":      0,
    "msgs_replayed":     0,
}

# Track pending admin requests: packet_id -> client_cid
# Used to route admin responses back to the specific client that requested them
_admin_pending: Dict[int, str] = {}
# Timestamps for admin_pending entries so we can age them out
_admin_pending_ts: Dict[int, float] = {}

# Track packet IDs recently forwarded FROM clients TO the radio.
# Used to suppress echo-back: meshtastic-python pubsub delivers back packets
# we sent, which we must NOT re-broadcast to clients or they will see their
# own outbound packets reflected and may disconnect.
# Format: {packet_id: timestamp_sent}
_forwarded_packet_ids: Dict[int, float] = {}
_ECHO_SUPPRESS_TTL: float = 30.0  # seconds to keep IDs in the echo filter


# ══════════════════════════════════════════════════════════════════════════════
# Session — persists across reconnects, keyed by IP
# ══════════════════════════════════════════════════════════════════════════════

class Session:
    """
    Represents a named app instance that may connect and disconnect.
    Identity: IP address (primary), confirmed by from_num on first packet.
    """
    __slots__ = (
        "session_id", "ip", "from_node_id", "from_node_name",
        "first_seen", "last_seen", "last_connected", "last_disconnected",
        "total_connections", "total_pkts_rx", "total_pkts_tx",
        "msg_queue",     # deque of FromRadio proto bytes to replay on reconnect
        "msg_queue_max",
        "online",        # currently connected?
        "client_cid",    # cid of current ProxyClient if online
        "msgs_sent",     # list of outbound text messages (for display)
        "app_hint",      # guessed app type
    )

    def __init__(self, ip: str, queue_max: int = 100):
        self.session_id        = str(uuid.uuid4())[:12]
        self.ip                = ip
        self.from_node_id      = None  # "!aabbccdd" — set on first outbound packet
        self.from_node_name    = None
        self.first_seen        = time.time()
        self.last_seen         = time.time()
        self.last_connected    = time.time()
        self.last_disconnected: Optional[float] = None
        self.total_connections = 1
        self.total_pkts_rx     = 0
        self.total_pkts_tx     = 0
        self.msg_queue         = deque(maxlen=queue_max)
        self.msg_queue_max     = queue_max
        self.online            = True
        self.client_cid        = None
        self.msgs_sent: List[dict] = []
        self.app_hint          = "unknown"

    def mark_connected(self, cid: str):
        self.online             = True
        self.client_cid         = cid
        self.last_connected     = time.time()
        self.last_seen          = time.time()
        self.total_connections += 1

    def mark_disconnected(self):
        self.online             = False
        self.client_cid         = None
        self.last_disconnected  = time.time()
        self.last_seen          = time.time()

    def enqueue(self, from_radio_bytes: bytes, label: str = ""):
        """Queue a FromRadio packet for replay when session reconnects."""
        self.msg_queue.append((time.time(), from_radio_bytes, label))

    def drain_queue(self) -> List[tuple]:
        """Pop all queued messages for replay, oldest first."""
        items = list(self.msg_queue)
        self.msg_queue.clear()
        return items

    def set_node_identity(self, from_num: int, md=None):
        if from_num and not self.from_node_id:
            self.from_node_id = "!{:08x}".format(from_num)
            if md:
                node = md.nodes.get(self.from_node_id, {})
                name = node.get("long_name") or node.get("short_name") or ""
                self.from_node_name = name or self.from_node_id

    def is_expired(self, ttl: float) -> bool:
        ref = self.last_disconnected or self.last_seen
        return not self.online and (time.time() - ref) > ttl

    def to_dict(self) -> dict:
        queued = len(self.msg_queue)
        return {
            "session_id":        self.session_id,
            "ip":                self.ip,
            "from_node_id":      self.from_node_id,
            "from_node_name":    self.from_node_name,
            "app_hint":          self.app_hint,
            "online":            self.online,
            "client_cid":        self.client_cid,
            "first_seen":        self.first_seen,
            "last_seen":         self.last_seen,
            "last_connected":    self.last_connected,
            "last_disconnected": self.last_disconnected,
            "total_connections": self.total_connections,
            "total_pkts_rx":     self.total_pkts_rx,
            "total_pkts_tx":     self.total_pkts_tx,
            "queued_messages":   queued,
            "msgs_sent":         list(self.msgs_sent[-20:]),
        }


# keyed by IP
_sessions: Dict[str, Session] = {}


def _get_or_create_session(ip: str, cid: str) -> Session:
    cfg  = _load_config()
    qmax = cfg.get("session_queue_max", 100)
    if ip in _sessions:
        s = _sessions[ip]
        s.mark_connected(cid)
        _dbg("INFO", cid, "info", "SESSION RESUMED",
             f"ip={ip} sid={s.session_id} queued={len(s.msg_queue)} "
             f"node={s.from_node_id or '?'} disconnected_ago="
             f"{int(time.time()-s.last_disconnected) if s.last_disconnected else '?'}s")
        return s
    s = Session(ip, queue_max=qmax)
    s.client_cid = cid
    _sessions[ip] = s
    _dbg("INFO", cid, "info", "SESSION CREATED", f"ip={ip} sid={s.session_id}")
    return s


def _expire_sessions():
    cfg     = _load_config()
    ttl     = cfg.get("session_ttl_secs", 7200)
    expired = [ip for ip, s in _sessions.items() if s.is_expired(ttl)]
    for ip in expired:
        _dbg("INFO", "server", "info", "Session expired",
             f"ip={ip} sid={_sessions[ip].session_id}")
        del _sessions[ip]


# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    cfg = DEFAULTS.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Logging
# ══════════════════════════════════════════════════════════════════════════════

def _dbg(level: str, cid: str, direction: str, msg: str, detail: str = ""):
    _debug_log.appendleft({
        "ts":        time.time(),
        "level":     level,
        "cid":       cid,
        "direction": direction,
        "msg":       msg,
        "detail":    detail,
    })
    fn = (logger.error   if level == "ERROR" else
          logger.warning  if level == "WARN"  else
          logger.debug)
    fn("[%s] %s %s %s", cid, direction, msg, detail[:120] if detail else "")


# ══════════════════════════════════════════════════════════════════════════════
# Wire framing
# ══════════════════════════════════════════════════════════════════════════════

def _frame(proto_bytes: bytes) -> bytes:
    length = len(proto_bytes)
    return bytes([START1, START2, (length >> 8) & 0xFF, length & 0xFF]) + proto_bytes


async def _write_frame(writer: asyncio.StreamWriter, proto_bytes: bytes,
                       client_cid: str, label: str,
                       pkts_tx_ref: Optional[list] = None):
    data = _frame(proto_bytes)
    writer.write(data)
    await asyncio.wait_for(writer.drain(), timeout=10.0)
    if pkts_tx_ref is not None:
        pkts_tx_ref[0] += 1
    _dbg("INFO", client_cid, "tx", label,
         f"len={len(proto_bytes)} hdr={data[:8].hex()}")


async def _read_frame(reader: asyncio.StreamReader,
                      cid: str, timeout: float = 120.0) -> Optional[bytes]:
    wake = 0
    while True:
        b = await asyncio.wait_for(reader.read(1), timeout=timeout)
        if not b:
            return None
        if b[0] == START2:
            wake += 1
            continue
        if b[0] == START1:
            if wake:
                _dbg("INFO", cid, "rx", f"drained {wake} wake bytes")
            break
        _dbg("INFO", cid, "rx", f"skip byte 0x{b[0]:02x}")

    b2 = await asyncio.wait_for(reader.read(1), timeout=5.0)
    if not b2 or b2[0] != START2:
        _dbg("WARN", cid, "rx", f"bad START2: {b2[0] if b2 else 'EOF':#04x}")
        return None

    lb     = await asyncio.wait_for(reader.readexactly(2), timeout=5.0)
    length = (lb[0] << 8) | lb[1]
    _dbg("INFO", cid, "rx", f"frame header OK len={length}")

    if length == 0:
        return b""
    if length > MAX_PACKET:
        _dbg("ERROR", cid, "rx", f"oversized frame {length} — drop")
        return None
    return await asyncio.wait_for(reader.readexactly(length), timeout=30.0)


# ══════════════════════════════════════════════════════════════════════════════
# Protobuf builders — config & module config
# ══════════════════════════════════════════════════════════════════════════════

def _build_my_info(interface) -> bytes:
    """Build FromRadio{my_info}."""
    fr = mesh_pb2.FromRadio()
    mi = mesh_pb2.MyNodeInfo()
    if interface and hasattr(interface, "myInfo") and interface.myInfo:
        mi.my_node_num = getattr(interface.myInfo, "my_node_num", 0)
    fr.my_info.CopyFrom(mi)
    return fr.SerializeToString()


def _build_metadata(interface, md_data) -> Optional[bytes]:
    """
    Build FromRadio{metadata}. Prefer live interface metadata, fall back to
    meshtastic_data dict, then emit a minimal valid struct.
    """
    try:
        fr   = mesh_pb2.FromRadio()
        meta = mesh_pb2.DeviceMetadata()

        # Best path: use metadata from the live interface if present
        if interface and hasattr(interface, "metadata") and interface.metadata:
            meta.CopyFrom(interface.metadata)
            fr.metadata.CopyFrom(meta)
            return fr.SerializeToString()

        # Fallback: reconstruct from meshtastic_data dict
        info = (md_data.local_node_info or {}) if md_data else {}
        fw   = (info.get("firmwareVersion") or info.get("firmware_version") or
                info.get("metadata", {}).get("firmwareVersion") or "2.5.0.0")
        meta.firmware_version = str(fw)

        hw_raw = (info.get("hwModel") or info.get("hw_model") or
                  info.get("user", {}).get("hwModel") or 0)
        hw = 0
        if isinstance(hw_raw, str):
            try:
                hw = mesh_pb2.HardwareModel.Value(hw_raw)
            except Exception:
                pass
        else:
            hw = int(hw_raw or 0)
        meta.hw_model = hw

        fr.metadata.CopyFrom(meta)
        return fr.SerializeToString()
    except Exception as e:
        logger.debug("metadata build: %s", e)
        return None


def _build_config_section(iface, section_name: str, md_data=None) -> bytes:
    """
    Build FromRadio{config{<section_name>}} using the real localConfig from
    the interface. Falls back to an empty section on any error so the
    handshake is never skipped.

    section_name must be one of:
      device, position, power, network, display, lora, bluetooth, security
    """
    fr  = mesh_pb2.FromRadio()
    cfg = config_pb2.Config()
    try:
        if (iface and hasattr(iface, "localNode") and iface.localNode and
                hasattr(iface.localNode, "localConfig") and
                iface.localNode.localConfig):
            sub = getattr(iface.localNode.localConfig, section_name, None)
            if sub is not None:
                getattr(cfg, section_name).CopyFrom(sub)
                logger.debug("config{%s}: from interface.localNode", section_name)
            else:
                logger.debug("config{%s}: field missing in localConfig", section_name)
        else:
            # Special fallback for lora from md_data dict
            if section_name == "lora" and md_data:
                _fill_lora_fallback(cfg, md_data)
            logger.debug("config{%s}: no localNode, using empty", section_name)
    except Exception as e:
        logger.debug("config{%s} build error: %s", section_name, e)
    fr.config.CopyFrom(cfg)
    return fr.SerializeToString()


def _fill_lora_fallback(cfg: config_pb2.Config, md_data):
    """Fill cfg.lora from meshtastic_data dict when localNode is unavailable."""
    try:
        info      = (md_data.local_node_info or {}) if md_data else {}
        lora_info = (info.get("loraConfig") or info.get("lora_config") or
                     info.get("config", {}).get("lora") or {})

        region_raw = lora_info.get("region") or info.get("region") or "EU_868"
        if isinstance(region_raw, str):
            try:
                cfg.lora.region = config_pb2.Config.LoRaConfig.RegionCode.Value(region_raw)
            except Exception:
                cfg.lora.region = 4  # EU_868
        else:
            cfg.lora.region = int(region_raw or 4)

        cfg.lora.use_preset = True
        preset_raw = (lora_info.get("modemPreset") or
                      lora_info.get("modem_preset") or "LONG_FAST")
        if isinstance(preset_raw, str):
            try:
                cfg.lora.modem_preset = (
                    config_pb2.Config.LoRaConfig.ModemPreset.Value(preset_raw))
            except Exception:
                cfg.lora.modem_preset = 0
        else:
            cfg.lora.modem_preset = int(preset_raw or 0)

        cfg.lora.hop_limit  = int(
            lora_info.get("hopLimit") or lora_info.get("hop_limit") or 3)
        cfg.lora.tx_enabled = bool(lora_info.get("txEnabled", True))
        cfg.lora.tx_power   = int(
            lora_info.get("txPower") or lora_info.get("tx_power") or 0)
    except Exception as e:
        logger.debug("lora fallback fill: %s", e)


def _build_session_key_config(iface) -> bytes:
    """
    Build FromRadio{config{sessionkey}}.
    The firmware sends this as a Config with the sessionkey oneof field populated.
    If sessionkey is not supported in the installed protobuf version (older
    meshtastic-python), we return an empty FromRadio frame and log a debug
    message rather than crashing — the handshake continues normally.
    """
    fr  = mesh_pb2.FromRadio()
    cfg = config_pb2.Config()
    try:
        # First check: does Config even have a sessionkey field?
        if not hasattr(cfg, "sessionkey"):
            logger.debug("config{sessionkey}: not supported in this protobuf version, skipping")
            return fr.SerializeToString()

        if (iface and hasattr(iface, "localNode") and iface.localNode and
                hasattr(iface.localNode, "localConfig") and
                iface.localNode.localConfig and
                hasattr(iface.localNode.localConfig, "sessionkey")):
            sk = iface.localNode.localConfig.sessionkey
            cfg.sessionkey.CopyFrom(sk)
        else:
            # Emit empty sessionkey config — valid protobuf, client sees the packet
            cfg.sessionkey.CopyFrom(config_pb2.Config.SessionkeyConfig())
        fr.config.CopyFrom(cfg)
    except AttributeError as e:
        # SessionkeyConfig class or sessionkey field doesn't exist on this version
        logger.debug("config{sessionkey}: AttributeError (%s), sending empty frame", e)
        return mesh_pb2.FromRadio().SerializeToString()
    except Exception as e:
        logger.debug("sessionkey config build: %s", e)
        return mesh_pb2.FromRadio().SerializeToString()
    return fr.SerializeToString()


def _build_module_config_section(iface, field_name: str) -> bytes:
    """
    Build FromRadio{moduleConfig{<field_name>}} using the real moduleConfig
    from the interface. Falls back to an empty section on any error.

    Handles both old meshtastic-python (camelCase FromRadio.moduleConfig field)
    and new versions (snake_case FromRadio.module_config field).
    If module_config_pb2 is unavailable entirely, returns an empty FromRadio frame
    so the handshake is never aborted.

    field_name must be one of:
      mqtt, serial, external_notification, store_forward, range_test,
      telemetry, canned_message, audio, remote_hardware, neighbor_info,
      ambient_lighting, detection_sensor, paxcounter
    """
    fr = mesh_pb2.FromRadio()

    # If module_config_pb2 is not available, emit a bare empty frame so the
    # client at least sees the right number of packets in the handshake.
    if module_config_pb2 is None:
        logger.debug("moduleConfig{%s}: module_config_pb2 unavailable, sending empty frame", field_name)
        return fr.SerializeToString()

    mc = module_config_pb2.ModuleConfig()
    try:
        if (iface and hasattr(iface, "localNode") and iface.localNode and
                hasattr(iface.localNode, "moduleConfig") and
                iface.localNode.moduleConfig):
            sub = getattr(iface.localNode.moduleConfig, field_name, None)
            if sub is not None:
                getattr(mc, field_name).CopyFrom(sub)
                logger.debug("moduleConfig{%s}: from interface.localNode", field_name)
            else:
                logger.debug("moduleConfig{%s}: field missing", field_name)
        else:
            logger.debug("moduleConfig{%s}: no localNode, using empty", field_name)
    except Exception as e:
        logger.debug("moduleConfig{%s} build error: %s", field_name, e)

    # Set the module_config field on FromRadio using the correct name for this
    # installed version of meshtastic-python.
    try:
        getattr(fr, _FROMRADIO_MODULE_CONFIG_FIELD).CopyFrom(mc)
    except AttributeError as e:
        # Neither field name worked — this version of the protobuf doesn't
        # support module configs at all. Return bare frame rather than crashing.
        logger.debug("moduleConfig{%s}: FromRadio has no module config field (%s), sending empty", field_name, e)
        return mesh_pb2.FromRadio().SerializeToString()
    except Exception as e:
        logger.debug("moduleConfig{%s}: unexpected error assigning field: %s", field_name, e)
        return mesh_pb2.FromRadio().SerializeToString()

    return fr.SerializeToString()


# ══════════════════════════════════════════════════════════════════════════════
# Protobuf builders — channels
# ══════════════════════════════════════════════════════════════════════════════

def _build_channel(index: int, is_primary: bool = False,
                   real_channel=None) -> bytes:
    """
    Build FromRadio{channel} packet.
    If real_channel is a Channel protobuf from localNode.channels, use it directly
    so actual PSK, name, and role are faithfully replicated.
    """
    fr = mesh_pb2.FromRadio()
    if real_channel is not None and hasattr(real_channel, "SerializeToString"):
        fr.channel.CopyFrom(real_channel)
    else:
        ch       = channel_pb2.Channel()
        ch.index = index
        ch.role  = (channel_pb2.Channel.Role.Value("PRIMARY") if is_primary
                    else channel_pb2.Channel.Role.Value("DISABLED"))
        if is_primary:
            cs      = channel_pb2.ChannelSettings()
            cs.name = "LongFast"
            ch.settings.CopyFrom(cs)
        fr.channel.CopyFrom(ch)
    return fr.SerializeToString()


# ══════════════════════════════════════════════════════════════════════════════
# Protobuf builders — node info
# ══════════════════════════════════════════════════════════════════════════════

def _build_node_info(node: dict) -> Optional[bytes]:
    try:
        fr       = mesh_pb2.FromRadio()
        ni       = mesh_pb2.NodeInfo()
        node_id  = node.get("node_id") or node.get("id") or ""
        node_num = node.get("node_num") or node.get("num") or 0

        if not node_num and isinstance(node_id, str) and node_id.startswith("!"):
            try:
                node_num = int(node_id[1:], 16)
            except Exception:
                pass
        if not node_num:
            return None

        ni.num = int(node_num)

        u            = mesh_pb2.User()
        u.id         = str(node_id)
        u.long_name  = str(node.get("long_name")  or node.get("longName")  or node_id)
        u.short_name = str(node.get("short_name") or node.get("shortName") or "???")
        hw           = node.get("hw_model") or node.get("hwModel") or 0
        if isinstance(hw, str):
            try:
                hw = mesh_pb2.HardwareModel.Value(hw)
            except Exception:
                hw = 0
        u.hw_model = int(hw)

        # Public key for PKI-based admin (critical for admin/traceroute auth)
        pk = node.get("public_key") or node.get("publicKey") or b""
        if isinstance(pk, str):
            import base64 as _b64
            try:
                pk = _b64.b64decode(pk)
            except Exception:
                pk = b""
        if pk:
            u.public_key = bytes(pk)

        ni.user.CopyFrom(u)

        lat = node.get("latitude")
        lon = node.get("longitude")
        alt = node.get("altitude")
        if lat is not None and lon is not None:
            pos             = mesh_pb2.Position()
            pos.latitude_i  = int(float(lat) * 1e7)
            pos.longitude_i = int(float(lon) * 1e7)
            if alt is not None:
                pos.altitude = int(float(alt))
            ni.position.CopyFrom(pos)

        bat = node.get("battery_level")
        snr = node.get("snr")
        if bat is not None:
            dm              = mesh_pb2.DeviceMetrics()
            dm.battery_level = int(float(bat))
            ni.device_metrics.CopyFrom(dm)
        if snr is not None:
            ni.snr = float(snr)

        lh = node.get("last_heard") or node.get("lastHeard")
        if lh:
            ni.last_heard = int(lh)

        # is_favorite / is_muted flags if present
        if node.get("isFavorite") or node.get("is_favorite"):
            ni.is_favorite = True

        fr.node_info.CopyFrom(ni)
        b = fr.SerializeToString()
        return b if b else None
    except Exception as e:
        logger.debug("node_info build: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Protobuf builders — incoming mesh packets
# ══════════════════════════════════════════════════════════════════════════════

def _build_from_radio_frame(packet: dict) -> Optional[bytes]:
    """
    Build a framed FromRadio protobuf from an incoming radio event.

    Handles two cases:
    1. Mesh packets (packet dict with 'raw' MeshPacket or decoded fields)
       → wraps in FromRadio{packet}
    2. Non-packet FromRadio payloads delivered by meshtastic-python
       (e.g. mqttClientProxyMessage, queueStatus) where the library
       stores the raw FromRadio bytes directly in packet["raw_from_radio"]
       → re-frames those bytes directly
    """
    try:
        # Case 1: raw FromRadio protobuf bytes stored directly (mqtt proxy, etc.)
        raw_fr = packet.get("raw_from_radio")
        if raw_fr and isinstance(raw_fr, bytes) and len(raw_fr) > 0:
            return raw_fr  # already serialized FromRadio bytes

        # Case 2: full raw MeshPacket protobuf stored in packet["raw"]
        fr  = mesh_pb2.FromRadio()
        raw = packet.get("raw")
        if raw is not None and hasattr(raw, "SerializeToString"):
            fr.packet.CopyFrom(raw)
        else:
            mp       = mesh_pb2.MeshPacket()
            from_num = packet.get("from") or packet.get("fromId") or 0
            to_num   = packet.get("to")   or packet.get("toId")   or 0xFFFFFFFF

            if isinstance(from_num, str) and from_num.startswith("!"):
                from_num = int(from_num[1:], 16)
            if isinstance(to_num, str) and to_num.startswith("!"):
                to_num = int(to_num[1:], 16)

            setattr(mp, "from", int(from_num) if from_num else 0)
            mp.to      = int(to_num) if to_num else 0xFFFFFFFF
            mp.id      = int(packet.get("id", 0))
            mp.channel = int(packet.get("channel", 0))

            snr  = packet.get("rxSnr")
            rssi = packet.get("rxRssi")
            if snr  is not None: mp.rx_snr  = float(snr)
            if rssi is not None: mp.rx_rssi = int(rssi)

            hl = packet.get("hopLimit")
            hs = packet.get("hopStart")
            if hl is not None: mp.hop_limit = int(hl)
            if hs is not None: mp.hop_start = int(hs)

            decoded  = packet.get("decoded") or {}
            port_num = decoded.get("portnum", 0)
            if isinstance(port_num, str):
                try:
                    port_num = portnums_pb2.PortNum.Value(port_num)
                except Exception:
                    port_num = 0

            payload = decoded.get("payload") or b""
            if isinstance(payload, str):
                try:
                    import base64 as _b64
                    payload = _b64.b64decode(payload)
                except Exception:
                    payload = payload.encode("utf-8", errors="replace")

            data         = mesh_pb2.Data()
            data.portnum = port_num
            data.payload = bytes(payload)

            # Propagate want_response so traceroute/admin replies work correctly
            if decoded.get("wantResponse") or decoded.get("want_response"):
                data.want_response = True
            if decoded.get("requestId") or decoded.get("request_id"):
                data.request_id = int(
                    decoded.get("requestId") or decoded.get("request_id", 0))
            if decoded.get("replyId") or decoded.get("reply_id"):
                data.reply_id = int(
                    decoded.get("replyId") or decoded.get("reply_id", 0))

            mp.decoded.CopyFrom(data)
            fr.packet.CopyFrom(mp)

        b = fr.SerializeToString()
        return b if b else None
    except Exception as e:
        logger.debug("from_radio frame build: %s", e)
        return None


def _port_name(portnum: int) -> str:
    try:
        return portnums_pb2.PortNum.Name(portnum)
    except Exception:
        return str(portnum)


# ══════════════════════════════════════════════════════════════════════════════
# Active clients
# ══════════════════════════════════════════════════════════════════════════════

class ProxyClient:
    __slots__ = ("cid", "peer_ip", "peer_port", "writer",
                 "connected_at", "pkts_rx", "pkts_tx",
                 "last_activity", "handshake_done", "config_id",
                 "session")

    def __init__(self, writer, peer):
        self.cid            = str(uuid.uuid4())[:8]
        self.peer_ip        = peer[0]
        self.peer_port      = peer[1]
        self.writer         = writer
        self.connected_at   = time.time()
        self.pkts_rx        = 0
        self.pkts_tx        = 0
        self.last_activity  = time.time()
        self.handshake_done = False
        self.config_id      = 0
        self.session: Optional[Session] = None

    def to_dict(self):
        return {
            "cid":            self.cid,
            "ip":             self.peer_ip,
            "port":           self.peer_port,
            "connected_at":   self.connected_at,
            "connected_secs": int(time.time() - self.connected_at),
            "pkts_rx":        self.pkts_rx,
            "pkts_tx":        self.pkts_tx,
            "last_activity":  self.last_activity,
            "handshake_done": self.handshake_done,
            "config_id":      self.config_id,
            "session_id":     self.session.session_id if self.session else None,
            "node_id":        self.session.from_node_id if self.session else None,
            "node_name":      self.session.from_node_name if self.session else None,
        }


_active_clients: Dict[str, ProxyClient] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Handshake — exact match of firmware PhoneAPI STATE machine
# ══════════════════════════════════════════════════════════════════════════════

# Full LocalConfig section order — matches firmware STATE_SEND_CONFIG exactly
_CONFIG_SECTIONS = [
    "device", "position", "power", "network", "display",
    "lora", "bluetooth", "security",
]

# Full ModuleConfig section order — matches firmware STATE_SEND_MODULE_CONFIG exactly
# Names here are protobuf field names on the ModuleConfig message.
_MODULE_CONFIG_SECTIONS = [
    ("mqtt",                  "mqtt"),
    ("serial",                "serial"),
    ("ext_notification",      "external_notification"),  # log label, pb field name
    ("store_forward",         "store_forward"),
    ("range_test",            "range_test"),
    ("telemetry",             "telemetry"),
    ("canned_message",        "canned_message"),
    ("audio",                 "audio"),
    ("remote_hardware",       "remote_hardware"),
    ("neighbor_info",         "neighbor_info"),
    ("ambient_lighting",      "ambient_lighting"),
    ("detection_sensor",      "detection_sensor"),
    ("paxcounter",            "paxcounter"),
]


async def _send_handshake(writer: asyncio.StreamWriter,
                          config_id: int,
                          client: ProxyClient,
                          slot,
                          session: Session):
    tx    = [0]
    cid   = client.cid
    md    = slot.meshtastic_data if slot else None
    iface = slot.connection_manager.interface if slot else None
    nodes = list(md.nodes.values()) if md else []

    _dbg("INFO", cid, "tx", "HANDSHAKE START",
         f"config_id={config_id} nodes={len(nodes)} queued={len(session.msg_queue)}")

    # ── 1. MyInfo ────────────────────────────────────────────────────────────
    await _write_frame(writer, _build_my_info(iface), cid,
                       "FromRadio{my_info}", tx)

    # ── 2. Device metadata ───────────────────────────────────────────────────
    b = _build_metadata(iface, md)
    if b:
        await _write_frame(writer, b, cid, "FromRadio{metadata}", tx)

    # ── 3. Channels (all 8 slots) ─────────────────────────────────────────────
    #    Firmware sends channels BEFORE LocalConfig sections.
    real_channels = None
    try:
        if iface and hasattr(iface, "localNode") and iface.localNode:
            real_channels = getattr(iface.localNode, "channels", None)
    except Exception:
        pass

    if real_channels and len(real_channels) > 0:
        ch_count = 0
        for ch_obj in real_channels:
            try:
                idx      = getattr(ch_obj, "index", ch_count)
                role_val = getattr(ch_obj, "role", 0)
                try:
                    role_name = channel_pb2.Channel.Role.Name(role_val)
                except Exception:
                    role_name = str(role_val)
                b   = _build_channel(idx, real_channel=ch_obj)
                lbl = f"FromRadio{{channel[{idx}] {role_name}}}"
                await _write_frame(writer, b, cid, lbl, tx)
                ch_count += 1
            except Exception as e:
                logger.debug("channel build error ch=%d: %s", ch_count, e)
        # Pad to 8 if needed (firmware always sends exactly 8 channel slots)
        for i in range(ch_count, 8):
            b = _build_channel(i, is_primary=False)
            await _write_frame(writer, b, cid,
                               f"FromRadio{{channel[{i}] DISABLED (pad)}}", tx)
    else:
        logger.debug("No real channels from interface, using synthetic")
        for i in range(8):
            b   = _build_channel(i, is_primary=(i == 0))
            lbl = f"FromRadio{{channel[{i}] {'PRIMARY' if i == 0 else 'DISABLED'}}}"
            await _write_frame(writer, b, cid, lbl, tx)

    # ── 4. LocalConfig sections (8 sections) ──────────────────────────────────
    for section in _CONFIG_SECTIONS:
        b = _build_config_section(iface, section, md_data=md)
        await _write_frame(writer, b, cid, f"FromRadio{{config{{{section}}}}}", tx)

    # ── 5. SessionKey config ──────────────────────────────────────────────────
    #    Firmware sends this after security config.
    #    Required for state-changing admin ops (traceroute, remote config, etc.)
    b = _build_session_key_config(iface)
    await _write_frame(writer, b, cid, "FromRadio{config{sessionkey}}", tx)

    # ── 6. ModuleConfig sections (13 sections) ────────────────────────────────
    for label, field in _MODULE_CONFIG_SECTIONS:
        b = _build_module_config_section(iface, field)
        await _write_frame(writer, b, cid,
                           f"FromRadio{{moduleConfig{{{label}}}}}", tx)

    # ── 7. All node infos ────────────────────────────────────────────────────
    node_count = 0
    for node in nodes:
        b = _build_node_info(node)
        if b:
            await _write_frame(writer, b, cid,
                               f"FromRadio{{node_info {node.get('node_id', '?')}}}", tx)
            node_count += 1

    # ── 8. Replay queued messages for this session ───────────────────────────
    queued = session.drain_queue()
    if queued:
        _dbg("INFO", cid, "tx", f"REPLAYING {len(queued)} queued messages",
             f"for session {session.session_id}")
        for (ts, proto_bytes, label) in queued:
            age = int(time.time() - ts)
            await _write_frame(writer, proto_bytes, cid,
                               f"REPLAY[{age}s ago] {label}", tx)
            _stats["msgs_replayed"] += 1
            client.pkts_tx += 1

    # ── 9. ConfigComplete ────────────────────────────────────────────────────
    fr = mesh_pb2.FromRadio()
    fr.config_complete_id = config_id
    await _write_frame(writer, fr.SerializeToString(), cid,
                       f"FromRadio{{config_complete_id={config_id}}}", tx)

    client.pkts_tx       += tx[0]
    client.handshake_done = True
    client.config_id      = config_id
    session.total_pkts_tx += tx[0]

    _dbg("INFO", cid, "tx", "HANDSHAKE COMPLETE",
         f"nodes={node_count} replayed={len(queued)} config_id={config_id}")


# ══════════════════════════════════════════════════════════════════════════════
# Client handler
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_client(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter):
    peer   = writer.get_extra_info("peername", ("?", 0))
    client = ProxyClient(writer, peer)
    cfg    = _load_config()

    async with _clients_lock:
        if len(_active_clients) >= cfg.get("max_clients", 8):
            _dbg("WARN", client.cid, "info", "MAX_CLIENTS — rejecting",
                 f"{peer[0]}:{peer[1]}")
            writer.close()
            return
        _active_clients[client.cid] = client
        _stats["total_connections"] += 1
        if len(_active_clients) > _stats["peak_clients"]:
            _stats["peak_clients"] = len(_active_clients)

    # Attach session
    async with _sessions_lock:
        session = _get_or_create_session(peer[0], client.cid)
    client.session = session

    _dbg("INFO", client.cid, "info", "CLIENT CONNECTED",
         f"{peer[0]}:{peer[1]} active={len(_active_clients)} "
         f"session={session.session_id} returning={session.total_connections > 1}")

    slot = _node_registry.get(cfg.get("slot_id", "node_0"))

    try:
        # ── Wait for want_config_id, swallowing heartbeats / spurious frames
        config_id = 1
        _loop     = asyncio.get_event_loop()
        _deadline = _loop.time() + 30.0
        while True:
            remaining = _deadline - _loop.time()
            if remaining <= 0:
                _dbg("WARN", client.cid, "rx",
                     "timeout waiting for want_config_id — using 1")
                break
            try:
                _pre = await asyncio.wait_for(
                    _read_frame(reader, client.cid), timeout=remaining)
            except asyncio.TimeoutError:
                _dbg("WARN", client.cid, "rx",
                     "timeout waiting for want_config_id — using 1")
                break
            if _pre is None:
                _dbg("INFO", client.cid, "rx", "EOF during init")
                return
            if len(_pre) == 0:
                continue  # heartbeat
            _tr2 = mesh_pb2.ToRadio()
            try:
                _tr2.ParseFromString(_pre)
            except Exception as _pe:
                _dbg("WARN", client.cid, "rx", f"parse error during init: {_pe}")
                continue
            _f2 = [f.name for f, _ in _tr2.ListFields()]
            _dbg("INFO", client.cid, "rx",
                 f"pre-config frame: ToRadio{{{','.join(_f2)}}}",
                 f"want_config_id={_tr2.want_config_id}")
            if _tr2.want_config_id:
                config_id = _tr2.want_config_id
                break

        # ── Full handshake
        await _send_handshake(writer, config_id, client, slot, session)

        # ── Main receive loop
        while True:
            payload = await _read_frame(reader, client.cid)
            if payload is None:
                _dbg("INFO", client.cid, "rx", "EOF")
                break
            if len(payload) == 0:
                continue  # heartbeat

            client.pkts_rx        += 1
            session.total_pkts_rx += 1
            client.last_activity   = time.time()
            session.last_seen      = time.time()
            _stats["pkts_from_clients"] += 1

            tr = mesh_pb2.ToRadio()
            try:
                tr.ParseFromString(payload)
            except Exception as e:
                _dbg("ERROR", client.cid, "rx", f"parse error: {e}",
                     payload[:32].hex())
                continue

            # Re-config request (app asking for fresh config)
            if tr.want_config_id:
                _dbg("INFO", client.cid, "rx", "RE-CONFIG REQUEST",
                     f"want_config_id={tr.want_config_id}")
                client.handshake_done = False
                await _send_handshake(writer, tr.want_config_id, client, slot, session)
                continue

            # Graceful disconnect
            try:
                if tr.HasField("disconnect") and tr.disconnect:
                    _dbg("INFO", client.cid, "rx", "CLIENT DISCONNECT")
                    break
            except Exception:
                pass

            if tr.HasField("packet"):
                mp       = tr.packet
                from_num = getattr(mp, "from", 0)
                from_id  = "!{:08x}".format(from_num) if from_num else "self"
                to_id    = ("^all" if mp.to == 0xFFFFFFFF
                            else "!{:08x}".format(mp.to))
                has_dec  = mp.HasField("decoded")
                port     = mp.decoded.portnum if has_dec else 0
                port_nm  = _port_name(port)
                text     = ""
                if port == 1 and has_dec:
                    try:
                        text = mp.decoded.payload.decode("utf-8", errors="replace")
                    except Exception:
                        pass

                # Identify session by from_num on first packet
                if from_num and not session.from_node_id:
                    md = slot.meshtastic_data if slot else None
                    session.set_node_identity(from_num, md)
                    _dbg("INFO", client.cid, "info", "SESSION IDENTITY CONFIRMED",
                         f"node={session.from_node_id} name={session.from_node_name}")

                detail = f"from={from_id} to={to_id} port={port_nm} ch={mp.channel}"
                if text:
                    detail += f" text={text[:80]}"

                _dbg("INFO", client.cid, "rx", "ToRadio{packet}", detail)

                if text:
                    session.msgs_sent.append({
                        "ts": time.time(), "to": to_id,
                        "text": text, "port": port_nm,
                    })
                    if len(session.msgs_sent) > 100:
                        session.msgs_sent.pop(0)

                # Detect app type from traffic patterns
                if port == 6 and session.app_hint == "unknown":
                    session.app_hint = "Meshtastic App (admin)"
                elif port == 70 and session.app_hint == "unknown":
                    session.app_hint = "Meshtastic App (traceroute)"
                elif port == 1 and session.app_hint in (
                        "unknown", "Meshtastic App (admin)"):
                    session.app_hint = "Meshtastic App"

                # Forward to radio
                slot = _node_registry.get(cfg.get("slot_id", "node_0"))
                if slot and slot.connection_manager.interface:
                    iface = slot.connection_manager.interface
                    if hasattr(iface, "_sendToRadio"):
                        fwd_tr = tr

                        if port == 6:
                            # ADMIN_APP: clear 'from' field so the radio does not
                            # reject the packet as an echo of a packet we sent,
                            # and track the packet ID so we can route the response
                            # back to this specific client only.
                            try:
                                fwd_mp = mesh_pb2.MeshPacket()
                                fwd_mp.CopyFrom(mp)
                                setattr(fwd_mp, "from", 0)
                                fwd_tr = mesh_pb2.ToRadio()
                                fwd_tr.packet.CopyFrom(fwd_mp)
                                _admin_pending[mp.id] = client.cid
                                _admin_pending_ts[mp.id] = time.time()
                            except Exception as _ae:
                                logger.debug("admin forward prep: %s", _ae)
                                fwd_tr = tr
                        # For TRACEROUTE_APP (port 70) and all other ports:
                        # forward as-is; responses will be broadcast to all clients.

                        # Record this packet ID for echo suppression.
                        # The meshtastic-python library may fire pubsub with this
                        # packet again after the radio processes it.
                        if mp.id:
                            _forwarded_packet_ids[mp.id] = time.time()

                        await asyncio.to_thread(iface._sendToRadio, fwd_tr)
                        client.pkts_tx        += 1
                        session.total_pkts_tx += 1
                        _dbg("INFO", client.cid, "tx", "forwarded to radio", detail)
                    else:
                        _dbg("WARN", client.cid, "tx", "_sendToRadio unavailable")
                else:
                    _dbg("WARN", client.cid, "tx", "radio not ready — dropped")
            else:
                # Non-packet, non-config ToRadio frames.
                # We SELECTIVELY forward only fields the radio genuinely needs.
                # Heartbeats are proxy-level keepalives — do NOT forward them to
                # the radio. Forwarding heartbeats via _sendToRadio causes the
                # meshtastic-python interface to relay them on its own radio
                # connection, which can trigger unexpected radio responses that
                # propagate back as pubsub events and disconnect our client.
                fields = [f.name for f, _ in tr.ListFields()]
                field_str = ",".join(fields)

                # Fields that should be forwarded to the radio:
                _FORWARD_FIELDS = frozenset({
                    "mqtt_client_proxy_message",  # MQTT Client Proxy gateway traffic
                    "xmodem_packet",              # OTA firmware update chunks
                })

                should_forward = any(f in _FORWARD_FIELDS for f in fields)

                if should_forward:
                    slot = _node_registry.get(cfg.get("slot_id", "node_0"))
                    if slot and slot.connection_manager.interface:
                        iface = slot.connection_manager.interface
                        if hasattr(iface, "_sendToRadio"):
                            await asyncio.to_thread(iface._sendToRadio, tr)
                            client.pkts_tx        += 1
                            session.total_pkts_tx += 1
                            _dbg("INFO", client.cid, "tx",
                                 f"forwarded ToRadio{{{field_str}}} to radio",
                                 f"len={len(payload)}")
                        else:
                            _dbg("WARN", client.cid, "rx",
                                 f"ToRadio{{{field_str}}} — _sendToRadio unavailable")
                    else:
                        _dbg("WARN", client.cid, "rx",
                             f"ToRadio{{{field_str}}} — radio not ready, dropped")
                else:
                    # Heartbeat, client_notification, or other proxy-consumed frames.
                    _dbg("INFO", client.cid, "rx",
                         f"ToRadio{{{field_str}}} consumed by proxy (not forwarded)")
                    
                    if "heartbeat" in fields:
                        # Echo a harmless QueueStatus packet to safely reset the app's timeout
                        fr_echo = mesh_pb2.FromRadio()
                        fr_echo.queueStatus.res = 0
                        await _write_frame(writer, fr_echo.SerializeToString(), client.cid, "heartbeat_echo")

    except asyncio.IncompleteReadError:
        _dbg("INFO", client.cid, "rx", "DISCONNECT — EOF")
    except asyncio.TimeoutError:
        _dbg("WARN", client.cid, "rx", "DISCONNECT — idle timeout")
    except Exception as e:
        _dbg("ERROR", client.cid, "rx", f"EXCEPTION: {type(e).__name__}: {e}")
        logger.warning("Client error [%s]: %s", client.cid, e)
    finally:
        async with _clients_lock:
            _active_clients.pop(client.cid, None)
        async with _sessions_lock:
            session.mark_disconnected()
            _expire_sessions()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        _dbg("INFO", client.cid, "info", "CLIENT REMOVED",
             f"active={len(_active_clients)} "
             f"session={session.session_id} "
             f"queued_for_replay={len(session.msg_queue)}")


# ══════════════════════════════════════════════════════════════════════════════
# Broadcast to all connected clients (incoming packets from the mesh)
# ══════════════════════════════════════════════════════════════════════════════

def _get_portnum(packet_dict: dict) -> int:
    """Return integer portnum from a packet dict."""
    decoded  = packet_dict.get("decoded") or {}
    port_raw = decoded.get("portnum", 0)
    if isinstance(port_raw, str):
        try:
            return portnums_pb2.PortNum.Value(port_raw)
        except Exception:
            return 0
    return int(port_raw or 0)


def _is_text_packet(packet_dict: dict) -> bool:
    return _get_portnum(packet_dict) in (1, 7)  # TEXT_MESSAGE_APP, TEXT_MESSAGE_COMPRESSED_APP


def _is_admin_response(packet_dict: dict) -> bool:
    return _get_portnum(packet_dict) == 6  # ADMIN_APP


def _get_reply_id(packet_dict: dict) -> Optional[int]:
    """
    Extract the reply_id (request_id) from an admin response packet so we can
    route it back to the correct client.
    Checks multiple locations: the raw protobuf decoded field, and the dict.
    """
    # Check raw protobuf
    raw = packet_dict.get("raw")
    if raw is not None and hasattr(raw, "decoded"):
        try:
            val = getattr(raw.decoded, "reply_id", None)
            if val:
                return int(val)
            val = getattr(raw.decoded, "request_id", None)
            if val:
                return int(val)
        except Exception:
            pass

    # Check decoded sub-dict
    decoded = packet_dict.get("decoded") or {}
    for key in ("replyId", "reply_id", "requestId", "request_id"):
        val = decoded.get(key)
        if val:
            try:
                return int(val)
            except Exception:
                pass

    # Check packet dict top-level
    for key in ("replyId", "reply_id"):
        val = packet_dict.get(key)
        if val:
            try:
                return int(val)
            except Exception:
                pass

    return None


async def _broadcast(proto_bytes: bytes, packet_dict: dict):
    """
    Deliver a FromRadio packet to connected clients.

    Routing rules:
      - Text messages   → all connected clients + queue for offline sessions
      - Admin responses → route to the specific client that made the request
                          (identified by reply_id matching an _admin_pending entry);
                          if no match found, broadcast to all (safe fallback)
      - Traceroute / all other packets → broadcast to all connected clients
    """
    if not proto_bytes:
        return

    is_text  = _is_text_packet(packet_dict)
    is_admin = _is_admin_response(packet_dict)
    portnum  = _get_portnum(packet_dict)
    from_id  = (packet_dict.get("fromId") or
                "!{:08x}".format(packet_dict.get("from", 0)))

    # Queue text messages for offline sessions so they are replayed on reconnect
    if is_text:
        async with _sessions_lock:
            decoded   = packet_dict.get("decoded") or {}
            payload   = decoded.get("payload") or b""
            text_body = ""
            if isinstance(payload, bytes):
                try:
                    text_body = payload.decode("utf-8", errors="replace")
                except Exception:
                    pass
            label = f"TEXT from={from_id} text={text_body[:40]}"
            for ip, s in _sessions.items():
                if not s.online:
                    s.enqueue(proto_bytes, label)
                    _dbg("INFO", "server", "info",
                         f"Queued for offline session {s.session_id}",
                         f"ip={ip} node={s.from_node_id or '?'} label={label[:60]}")

    if not _active_clients:
        return

    # For admin responses, try to route only to the requesting client
    target_cid = None
    if is_admin:
        reply_id = _get_reply_id(packet_dict)
        if reply_id and reply_id in _admin_pending:
            target_cid = _admin_pending.pop(reply_id)
            _admin_pending_ts.pop(reply_id, None)
            _dbg("INFO", target_cid or "?", "tx",
                 f"admin response routed → client {target_cid}",
                 f"reply_id={reply_id} port={_port_name(portnum)}")
        # If no match: fall through and broadcast (admin broadcast is safe)

    dead = []
    async with _clients_lock:
        clients = list(_active_clients.values())

    for c in clients:
        if not c.handshake_done:
            continue
        # Admin responses with a known target: skip other clients
        if target_cid and c.cid != target_cid:
            continue
        try:
            c.writer.write(_frame(proto_bytes))
            await asyncio.wait_for(c.writer.drain(), timeout=5.0)
            c.pkts_tx       += 1
            c.last_activity  = time.time()
            if c.session:
                c.session.total_pkts_tx += 1
                c.session.last_seen      = time.time()
            _stats["pkts_to_clients"] += 1
            _dbg("INFO", c.cid, "tx",
                 f"broadcast port={_port_name(portnum)} from={from_id}",
                 f"len={len(proto_bytes)} target={'routed' if target_cid else 'all'}")
        except Exception:
            dead.append(c.cid)

    if dead:
        async with _clients_lock:
            for cid in dead:
                _active_clients.pop(cid, None)


def _on_receive(packet, interface=None):
    """
    pubsub callback — called for every packet the radio delivers.
    Schedules _broadcast on the plugin event loop (thread-safe).

    Filtering applied here to prevent the app from disconnecting:

    1. Echo suppression: packets we recently forwarded FROM a client TO the
       radio are tracked by ID. If they appear in pubsub, we drop them.

    2. ROUTING_APP self-ACK filter: when we forward an admin packet via
       iface._sendToRadio(), the radio generates a routing ACK (portnum=5)
       addressed from/to the local node. The app does NOT expect these because
       it never sent the original packet over LoRa — receiving an unexpected
       routing ACK causes it to disconnect. Drop all self-addressed ROUTING_APP.

    3. ADMIN_APP outbound echo filter: ADMIN_APP packets with no reply_id
       originating from the local node are forwarded requests, not responses.
       Drop them so the app doesn't see its own requests reflected back.
    """
    if not _event_loop:
        return

    now     = time.time()
    pkt_id  = packet.get("id", 0)
    portnum = _get_portnum(packet)

    # Get local node ID once — used by multiple filters below
    slot     = next(iter(_node_registry.values()), None) if _node_registry else None
    local_id = (slot.meshtastic_data.local_node_id
                if slot and slot.meshtastic_data else None)
    from_id  = packet.get("fromId") or ""
    to_id    = packet.get("toId")   or ""

    # ── Echo suppression ──────────────────────────────────────────────────────
    if pkt_id and pkt_id in _forwarded_packet_ids:
        logger.debug("_on_receive: suppressed echo pkt_id=%d port=%s",
                     pkt_id, _port_name(portnum))
        return

    # ── ROUTING_APP self-ACK filter ───────────────────────────────────────────
    # portnum 5 = ROUTING_APP. Self-addressed routing packets (from==local) are
    # ACKs/NAKs the radio generated for admin packets we proxied via _sendToRadio.
    # The app never sent those packets over LoRa so it does not expect these ACKs.
    # Forwarding them causes the app to disconnect immediately.
    if portnum == 5:  # ROUTING_APP
        if local_id and from_id and from_id == local_id:
            logger.debug("_on_receive: suppressed ROUTING_APP self-ACK from=%s to=%s",
                         from_id, to_id)
            return

    # ── Admin request echo filter ─────────────────────────────────────────────
    # ADMIN_APP (portnum 6) packets with no reply_id are outbound requests we
    # forwarded, not responses from the radio. Drop them.
    if portnum == 6:  # ADMIN_APP
        decoded  = packet.get("decoded") or {}
        reply_id = (decoded.get("replyId") or decoded.get("reply_id") or
                    decoded.get("requestId") or decoded.get("request_id"))
        if not reply_id and local_id and from_id and from_id == local_id:
            logger.debug("_on_receive: suppressed local ADMIN_APP request echo from=%s",
                         from_id)
            return

    # ── Purge stale echo suppression entries ──────────────────────────────────
    if len(_forwarded_packet_ids) > 50:
        cutoff = now - _ECHO_SUPPRESS_TTL
        stale  = [k for k, ts in list(_forwarded_packet_ids.items()) if ts < cutoff]
        for k in stale:
            del _forwarded_packet_ids[k]

    proto_bytes = _build_from_radio_frame(packet)
    if proto_bytes:
        asyncio.run_coroutine_threadsafe(
            _broadcast(proto_bytes, packet), _event_loop)



# ══════════════════════════════════════════════════════════════════════════════
# Server lifecycle
# ══════════════════════════════════════════════════════════════════════════════

async def _server_main():
    global _server
    cfg  = _load_config()
    host = cfg.get("bind_host", "0.0.0.0")
    port = int(cfg.get("port", 4403))
    try:
        _server = await asyncio.start_server(_handle_client, host, port)
        addrs   = ", ".join(str(s.getsockname()) for s in _server.sockets)
        logger.info("TCP proxy listening on %s", addrs)
        _dbg("INFO", "server", "info", f"Listening on {addrs}")
        async with _server:
            await _server.serve_forever()
    except asyncio.CancelledError:
        pass
    except OSError as e:
        msg = (f"Bind failed {host}:{port} — {e}. "
               f"Use a different port if 4403 is taken by the radio TCP server.")
        logger.error(msg)
        _dbg("ERROR", "server", "info", msg)
    except Exception as e:
        logger.error("Server error: %s", e)
        _dbg("ERROR", "server", "info", f"Server error: {e}")
    finally:
        _server = None


async def _watchdog_loop(context):
    wd  = context.get("plugin_watchdog")
    pid = context.get("plugin_id", _plugin_id)
    while True:
        await asyncio.sleep(60)
        if wd is not None:
            wd[pid] = time.time()
        async with _sessions_lock:
            _expire_sessions()
        # Purge stale admin_pending entries (those older than 120s that never
        # got a response — prevents unbounded growth)
        now = time.time()
        stale = [k for k, ts in list(_admin_pending_ts.items())
                 if now - ts > 120]
        for k in stale:
            _admin_pending.pop(k, None)
            _admin_pending_ts.pop(k, None)
        if stale:
            _dbg("INFO", "server", "info",
                 f"Purged {len(stale)} stale admin_pending entries")

        # Purge stale forwarded packet ID echo suppression entries
        cutoff = now - _ECHO_SUPPRESS_TTL
        stale_echo = [k for k, ts in list(_forwarded_packet_ids.items())
                      if ts < cutoff]
        for k in stale_echo:
            del _forwarded_packet_ids[k]


async def _restart_server():
    global _server_task, _server
    if _server_task and not isinstance(_server_task, asyncio.Future):
        if not _server_task.done():
            _server_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(_server_task), timeout=5.0)
            except Exception:
                pass
    async with _clients_lock:
        for c in list(_active_clients.values()):
            try:
                c.writer.close()
            except Exception:
                pass
        _active_clients.clear()
    _server_task = asyncio.create_task(_server_main())
    _dbg("INFO", "server", "info", "Server restarted")


def init_plugin(context: dict):
    global _node_registry, _event_loop, _plugin_watchdog, _plugin_id
    global _clients_lock, _sessions_lock, _server_task

    _node_registry   = context.get("node_registry") or {}
    _event_loop      = context.get("event_loop")
    _plugin_watchdog = context.get("plugin_watchdog") or {}
    _plugin_id       = context.get("plugin_id", "tcp_proxy")

    # Subscribe to the general receive topic — this catches ALL portnums
    # including ADMIN_APP, TRACEROUTE_APP, POSITION_APP, etc.
    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
    try:
        pub.subscribe(_on_receive, "meshtastic.receive")
    except Exception as e:
        logger.error("pub.subscribe(meshtastic.receive) failed: %s", e)

    # Belt-and-suspenders: also subscribe to key subtopics explicitly.
    # Pubsub hierarchy means meshtastic.receive catches all subtopics, but
    # explicit subscriptions protect against broken hierarchy implementations.
    _extra_topics = [
        "meshtastic.receive.data.ADMIN_APP",
        "meshtastic.receive.data.TRACEROUTE_APP",
        "meshtastic.receive.data.POSITION_APP",
        "meshtastic.receive.data.NODEINFO_APP",
        "meshtastic.receive.data.TELEMETRY_APP",
        "meshtastic.receive.data.ROUTING_APP",
        "meshtastic.receive.data.TEXT_MESSAGE_APP",
        "meshtastic.receive.mqttClientProxyMessage",  # MQTT Client Proxy responses from radio
    ]
    for topic in _extra_topics:
        try:
            pub.unsubscribe(_on_receive, topic)
        except Exception:
            pass
        try:
            pub.subscribe(_on_receive, topic)
        except Exception:
            pass  # Non-fatal if subtopic subscribe not supported

    if _event_loop:
        _clients_lock  = asyncio.Lock()
        _sessions_lock = asyncio.Lock()
        _server_task   = asyncio.run_coroutine_threadsafe(
            _server_main(), _event_loop)
        asyncio.run_coroutine_threadsafe(_watchdog_loop(context), _event_loop)

    cfg = _load_config()
    logger.info("TCP Proxy — port=%d slot=%s",
                cfg.get("port", 4403), cfg.get("slot_id", "node_0"))
    _dbg("INFO", "server", "info",
         f"Plugin init port={cfg.get('port', 4403)} "
         f"slot={cfg.get('slot_id', 'node_0')}")


# ══════════════════════════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════════════════════════

@plugin_router.get("/status")
async def get_status():
    cfg  = _load_config()
    slot = _node_registry.get(cfg.get("slot_id", "node_0"))
    return {
        "version":           "1.4.0",
        "enabled":           cfg.get("enabled", True),
        "port":              cfg.get("port", 4403),
        "bind_host":         cfg.get("bind_host", "0.0.0.0"),
        "slot_id":           cfg.get("slot_id", "node_0"),
        "max_clients":       cfg.get("max_clients", 8),
        "session_ttl_secs":  cfg.get("session_ttl_secs", 7200),
        "active_clients":    len(_active_clients),
        "active_sessions":   sum(1 for s in _sessions.values() if s.online),
        "total_sessions":    len(_sessions),
        "peak_clients":      _stats["peak_clients"],
        "total_connections": _stats["total_connections"],
        "pkts_to_clients":   _stats["pkts_to_clients"],
        "pkts_from_clients": _stats["pkts_from_clients"],
        "msgs_replayed":     _stats["msgs_replayed"],
        "admin_pending":     len(_admin_pending),
        "radio_ready":       (slot.connection_manager.is_ready.is_set()
                              if slot else False),
        "known_nodes":       (len(slot.meshtastic_data.nodes)
                              if slot else 0),
        "server_running":    _server is not None,
        "available_slots":   list(_node_registry.keys()),
    }


@plugin_router.get("/config")
async def get_config():
    cfg = _load_config()
    cfg["available_slots"] = list(_node_registry.keys())
    return cfg


@plugin_router.post("/config")
async def post_config(body: dict):
    cfg     = _load_config()
    allowed = {"enabled", "port", "bind_host", "slot_id", "max_clients",
               "session_ttl_secs", "session_queue_max"}
    cfg.update({k: v for k, v in body.items() if k in allowed})
    _save_config(cfg)
    if _event_loop:
        asyncio.run_coroutine_threadsafe(_restart_server(), _event_loop)
    return {"status": "saved", "config": cfg}


@plugin_router.get("/clients")
async def list_clients():
    return {"clients": [c.to_dict() for c in _active_clients.values()],
            "count":   len(_active_clients)}


@plugin_router.get("/sessions")
async def list_sessions():
    return {
        "sessions": [s.to_dict() for s in sorted(
            _sessions.values(), key=lambda s: -s.last_seen)],
        "count": len(_sessions),
    }


@plugin_router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    async with _sessions_lock:
        for ip, s in list(_sessions.items()):
            if s.session_id == session_id:
                if s.online:
                    return {"error": "Session is currently active — kick client first"}
                del _sessions[ip]
                return {"status": "deleted", "session_id": session_id}
    raise Exception("Session not found")


@plugin_router.post("/sessions/{session_id}/clear-queue")
async def clear_session_queue(session_id: str):
    async with _sessions_lock:
        for s in _sessions.values():
            if s.session_id == session_id:
                count = len(s.msg_queue)
                s.msg_queue.clear()
                return {"status": "cleared", "count": count}
    raise Exception("Session not found")


@plugin_router.get("/debug")
async def debug_log(limit: int = 200):
    return {"log": list(_debug_log)[:limit], "count": len(_debug_log)}


@plugin_router.delete("/debug")
async def clear_debug():
    _debug_log.clear()
    return {"status": "cleared"}


@plugin_router.post("/clients/disconnect-all")
async def disconnect_all():
    count = 0
    async with _clients_lock:
        for c in list(_active_clients.values()):
            try:
                c.writer.close()
                count += 1
            except Exception:
                pass
        _active_clients.clear()
    return {"status": "disconnected", "count": count}