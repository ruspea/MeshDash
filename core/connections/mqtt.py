"""
mqtt_connection.py — MeshDash MQTT Connection Manager v1.1
─────────────────────────────────────────────────────────────────────────────
Provides MQTTConnectionManager, a drop-in companion to MeshtasticConnectionManager.
It shares the same external contract:
  - .is_ready          asyncio.Event
  - .connect_loop()    async — called as a background task by meshtastic_dashboard.py
  - .sendText()        async — publish a text message to the mesh via MQTT
  - .shutdown()        async — clean disconnect
  - .register_callbacks(on_receive, on_connection, on_node_updated)
  - .config            dict — mirrors MeshtasticConnectionManager.config shape

Architecture:
  paho-mqtt runs its own network thread (non-blocking, battle-tested).
  Incoming ServiceEnvelope payloads are decoded from protobuf and placed onto
  an asyncio.Queue by a thread-safe call_soon_threadsafe(). The existing
  MeshDash packet processing worker (slot.packet_queue) consumes them exactly
  as it does for Serial/TCP/WebSerial packets.

Supported config keys (all prefixed MQTT_ in the connection_params dict):
  MQTT_BROKER           broker hostname  (default: mqtt.meshtastic.org)
  MQTT_PORT             broker port      (default: 1883; use 8883 for TLS)
  MQTT_USERNAME         optional username
  MQTT_PASSWORD         optional password
  MQTT_TLS              "true" to enable TLS/SSL
  MQTT_REGION           region code used in topic, e.g. "EU_868" (default: "#" = all)
  MQTT_CHANNEL          channel name filter, e.g. "LongFast" (default: "#" = all)
  MQTT_CLIENT_ID        MQTT client ID (default: auto-generated)
  MQTT_ROOT_TOPIC       override the full root topic (default: msh/REGION/2/e/CHANNEL/#)

Node identity:
  MQTT provides no myInfo burst like Serial/TCP does. The manager synthesises
  a virtual local_node_id from the MQTT client ID so the rest of MeshDash has
  something to display. If a NodeInfo packet from a node matching
  MQTT_NODE_ID (optional) is received, that node is promoted to local.

  MQTT_NODE_ID          hex node ID of your node (e.g. !aabbccdd) — optional.
                        If set, NodeInfo packets from this node are treated as
                        local node info. If not set, node acts as an observer.
"""

from core.routes.schemas import User, NodeSlot
from fastapi import status
import asyncio
import logging
import os
from core.connections import ConnectionState
import random
import ssl
import string
import struct
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("MQTTConnection")

try:
    from meshtastic import mesh_pb2, portnums_pb2, mqtt_pb2, telemetry_pb2
    _HAS_PROTO = True
except ImportError:
    _HAS_PROTO = False
    logger.error("meshtastic protobuf not available — MQTT decode disabled")

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO = True
except ImportError:
    _HAS_PAHO = False
    logger.error("paho-mqtt not available — install meshtastic-python")

try:
    from Crypto.Cipher import AES
    _HAS_CRYPTO = True
except ImportError:
    try:
        from Cryptodome.Cipher import AES
        _HAS_CRYPTO = True
    except ImportError:
        _HAS_CRYPTO = False
        logger.warning(
            "PyCryptodome not available — encrypted MQTT packets cannot be decrypted. "
            "Install with: pip install pycryptodome --break-system-packages"
        )

# Default Meshtastic channel PSK.
# Meshtastic firmware pads the single-byte seed 0x01 with zeros to 16 bytes.
# That IS the key directly — no PBKDF2, no hashing.
# Reference: firmware/src/mesh/CryptoEngine.cpp initKey() + crypto.cpp
_DEFAULT_PSK = bytes([
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])
# Map of short PSK seeds → expanded keys for common channels
_KNOWN_PSKS: Dict[str, bytes] = {
    "AQ==": _DEFAULT_PSK,
    "1PG7OiApB1nwvP+rz05pAQ==": _DEFAULT_PSK,  # some firmware versions ship this
}


def _expand_psk(psk_b64: str) -> Optional[bytes]:
    """
    Expand a base64 PSK to the 16-byte (or 32-byte) AES key Meshtastic uses.
    Single byte 0x01 ('AQ==') → padded to 16 bytes with zeros (firmware default).
    """
    import base64
    if not psk_b64:
        return None
    if psk_b64 in _KNOWN_PSKS:
        return _KNOWN_PSKS[psk_b64]
    try:
        raw = base64.b64decode(psk_b64)
    except Exception:
        return None
    if len(raw) == 1 and raw[0] == 0x01:
        return _DEFAULT_PSK
    if len(raw) in (16, 32):
        return raw
    if len(raw) < 16:
        return (raw + b'\x00' * 16)[:16]
    return raw[:32]


def _aes_ctr_crypt(data: bytes, packet_id: int, from_node: int, key: bytes) -> Optional[bytes]:
    """
    AES-CTR encrypt or decrypt (CTR is symmetric — same function for both).

    Meshtastic nonce construction (firmware crypto.cpp):
      nonce[0..7]  = packet_id as little-endian uint64
      nonce[8..15] = from_node_num as little-endian uint64
    PyCryptodome CTR: nonce is first 8 bytes, initial_value is last 8 bytes as int.
    """
    if not _HAS_CRYPTO or not data:
        return None
    try:
        nonce = struct.pack("<QQ",
                            packet_id & 0xFFFFFFFFFFFFFFFF,
                            from_node  & 0xFFFFFFFFFFFFFFFF)
        initial_value = int.from_bytes(nonce[8:], byteorder='little')
        cipher = AES.new(key, AES.MODE_CTR, nonce=nonce[:8], initial_value=initial_value)
        return cipher.encrypt(data)
    except Exception:
        return None


def _try_decrypt_packet(encrypted_bytes: bytes, packet_id: int, from_node: int) -> Optional[bytes]:
    """Attempt AES-CTR decryption using the default Meshtastic public channel key."""
    return _aes_ctr_crypt(encrypted_bytes, packet_id, from_node, _DEFAULT_PSK)


def _try_decrypt_with_key(encrypted_bytes: bytes, packet_id: int, from_node: int, key: bytes) -> Optional[bytes]:
    """Attempt AES-CTR decrypt with an arbitrary key."""
    return _aes_ctr_crypt(encrypted_bytes, packet_id, from_node, key)


# Public Meshtastic MQTT broker presets
MQTT_PRESETS = {
    "meshtastic_public": {
        "broker":   "mqtt.meshtastic.org",
        "port":     1883,
        "username": "meshdev",
        "password": "large4cats",
        "tls":      False,
        "region":   "EU_868",
    },
    "meshtastic_public_tls": {
        "broker":   "mqtt.meshtastic.org",
        "port":     8883,
        "username": "meshdev",
        "password": "large4cats",
        "tls":      True,
        "region":   "EU_868",
    },
}


# Packet decoder
_PORTNUM_NAME: Dict[int, str] = {
    1:   "Message",
    3:   "Position",
    4:   "NodeInfo",
    5:   "Routing",
    64:  "Traceroute",
    67:  "Telemetry",
    70:  "RemoteHardware",
    72:  "StoreAndForward",
    73:  "RangeTest",
    74:  "NeighborInfo",
    257: "Admin",
}


def _decode_portnum(d, pnum: int, decoded: dict) -> None:
    """Decode a known portnum payload into the decoded dict in-place."""
    decoded["portnum"] = pnum
    try:
        if pnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP:
            text = d.payload.decode("utf-8", errors="replace")
            decoded["text"] = text
            decoded["payload"] = text

        elif pnum == portnums_pb2.PortNum.POSITION_APP:
            pos = mesh_pb2.Position()
            pos.ParseFromString(d.payload)
            pd: Dict[str, Any] = {}
            if pos.latitude_i:   pd["latitude"]   = pos.latitude_i  * 1e-7; pd["latitudeI"]  = pos.latitude_i
            if pos.longitude_i:  pd["longitude"]  = pos.longitude_i * 1e-7; pd["longitudeI"] = pos.longitude_i
            if pos.altitude:     pd["altitude"]   = pos.altitude
            if pos.sats_in_view: pd["satsInView"] = pos.sats_in_view
            if pos.time:         pd["time"]       = pos.time
            decoded["position"] = pd

        elif pnum == portnums_pb2.PortNum.TELEMETRY_APP:
            tel = telemetry_pb2.Telemetry()
            tel.ParseFromString(d.payload)
            td: Dict[str, Any] = {}
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

        elif pnum == portnums_pb2.PortNum.NODEINFO_APP:
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

        elif pnum == portnums_pb2.PortNum.ROUTING_APP:
            r = mesh_pb2.Routing()
            r.ParseFromString(d.payload)
            decoded["routing"]     = {"errorReason": str(r.error_reason), "requestId": r.request_id}
            decoded["requestId"]   = r.request_id
            decoded["errorReason"] = str(r.error_reason)

    except Exception as e:
        import base64
        decoded["raw_payload_b64"] = base64.b64encode(d.payload).decode()
        logger.debug("MQTT: portnum %d decode error: %s", pnum, e)


def _decode_service_envelope(raw_payload: bytes) -> Optional[Dict]:
    """
    Decode a raw MQTT payload (ServiceEnvelope protobuf) into a packet dict
    compatible with MeshDash's existing packet processing pipeline.
    """
    if not _HAS_PROTO:
        return None

    try:
        envelope = mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(raw_payload)
    except Exception as e:
        logger.debug("MQTT: ServiceEnvelope decode failed: %s", e)
        return None

    if not envelope.HasField("packet"):
        return None

    mp = envelope.packet
    from_num = getattr(mp, "from", 0)
    to_num   = mp.to

    packet: Dict[str, Any] = {
        "id":        mp.id,
        "from":      from_num,
        "to":        to_num,
        "channel":   mp.channel,
        "hopLimit":  mp.hop_limit,
        "hopStart":  mp.hop_start,
        "wantAck":   mp.want_ack,
        "rxSnr":     mp.rx_snr,
        "rxRssi":    mp.rx_rssi,
        "rxTime":    mp.rx_time or int(time.time()),
        "viaMqtt":   True,
        "priority":  mp.priority,
        "_mqtt_channel_id": envelope.channel_id,
        "_mqtt_gateway_id": envelope.gateway_id,
    }

    if from_num:
        packet["fromId"] = f"!{from_num:08x}"
    if to_num:
        packet["toId"] = "^all" if to_num == 0xFFFFFFFF else f"!{to_num:08x}"

    if mp.HasField("decoded"):
        d = mp.decoded
        decoded: Dict[str, Any] = {"viaMqtt": True}
        pnum = d.portnum
        packet["app_packet_type"] = _PORTNUM_NAME.get(pnum, f"port_{pnum}")
        _decode_portnum(d, pnum, decoded)
        packet["decoded"]   = decoded
        packet["encrypted"] = False

    else:
        # ── Encrypted — attempt AES-CTR decryption with default key first ──
        decrypted_data = None
        if _HAS_CRYPTO and mp.encrypted:
            raw_plain = _try_decrypt_packet(bytes(mp.encrypted), mp.id, from_num)
            if raw_plain is not None:
                try:
                    data_obj = mesh_pb2.Data()
                    data_obj.ParseFromString(raw_plain)
                    if data_obj.portnum in _PORTNUM_NAME or 0 < data_obj.portnum < 1000:
                        decrypted_data = data_obj
                        logger.debug("MQTT: decrypted packet %d with default key, portnum=%d",
                                     mp.id, data_obj.portnum)
                except Exception:
                    decrypted_data = None

        if decrypted_data is not None:
            pnum = decrypted_data.portnum
            decoded = {"viaMqtt": True, "decryptedWithDefaultKey": True}
            packet["app_packet_type"] = _PORTNUM_NAME.get(pnum, f"port_{pnum}")
            _decode_portnum(decrypted_data, pnum, decoded)
            packet["decoded"]   = decoded
            packet["encrypted"] = False
        else:
            packet["encrypted"]       = True
            packet["decoded"]         = {"viaMqtt": True}
            packet["app_packet_type"] = "Encrypted"

    packet["source"]            = "MQTT"
    packet["source_confidence"] = 1.0
    return packet


# Outbound encoding — MUST encrypt for the 2/e/ topic path

def _encode_text_to_mqtt(
    text: str,
    destination_id: str,
    channel_index: int,
    from_node_num: int,
    channel_id: str = "LongFast",
    gateway_id: str = "!00000000",
    psk: Optional[bytes] = None,
    packet_id: Optional[int] = None,
) -> bytes:
    """
    Encode a text message into an AES-encrypted ServiceEnvelope for MQTT publish.

    The msh/REGION/2/e/ topic path REQUIRES encrypted payloads.  Radios on the
    mesh reject plaintext packets on this path.  We encrypt the Data protobuf
    using AES-CTR with the channel PSK, using the same nonce the firmware uses
    so any standard Meshtastic node can decrypt the message.

    packet_id: pre-generated ID supplied by the caller so it can be stored for
               ACK tracking.  If None, a random ID is generated here.
    psk: AES key bytes.  Defaults to _DEFAULT_PSK (the public default channel key).
    """
    if not _HAS_PROTO:
        raise RuntimeError("meshtastic protobuf not available")

    if psk is None:
        psk = _DEFAULT_PSK

    if not _HAS_CRYPTO:
        raise RuntimeError(
            "pycryptodome required for MQTT send. "
            "Install: pip install pycryptodome --break-system-packages"
        )

    data = mesh_pb2.Data()
    data.portnum       = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    data.payload       = text.encode("utf-8")
    data.want_response = False

    mp = mesh_pb2.MeshPacket()
    mp.channel   = channel_index
    mp.want_ack  = destination_id != "^all"
    mp.hop_limit = 3
    mp.id        = packet_id if packet_id is not None else random.randint(1, 0xFFFFFFFF)

    if from_node_num:
        setattr(mp, "from", from_node_num)

    if destination_id == "^all":
        mp.to = 0xFFFFFFFF
    elif destination_id.startswith("!"):
        try:
            mp.to = int(destination_id[1:], 16)
        except ValueError:
            mp.to = 0xFFFFFFFF
    else:
        mp.to = 0xFFFFFFFF

    # Encrypt using AES-CTR — same nonce scheme as decryption so it's symmetric
    raw_data = data.SerializeToString()
    encrypted = _aes_ctr_crypt(raw_data, mp.id, from_node_num, psk)
    if encrypted is None:
        raise RuntimeError("Encryption failed — check pycryptodome installation")
    mp.encrypted = encrypted

    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.packet.CopyFrom(mp)
    envelope.channel_id = channel_id
    envelope.gateway_id = gateway_id

    return envelope.SerializeToString()


# MQTTConnectionManager

class MQTTConnectionManager:
    """
    Manages a persistent MQTT connection on behalf of a MeshDash NodeSlot.
    """

    RECONNECT_DELAY_MIN:   float = 2.0
    RECONNECT_DELAY_MAX:   float = 60.0
    HEALTH_CHECK_INTERVAL: float = 15.0
    CONNECT_TIMEOUT:       float = 15.0
    SHUTDOWN_TIMEOUT:      float = 5.0
    MAX_RECONNECT_ATTEMPTS: int  = 50

    def __init__(
        self,
        meshtastic_data,
        logger_: Optional[logging.Logger] = None,
        connection_params: Optional[Dict[str, Any]] = None,
        slot_id: str = "node_0",
    ):
        self.meshtastic_data = meshtastic_data
        self.logger   = logger_ or logging.getLogger(f"MQTTConnection.{slot_id}")
        self.slot_id  = slot_id
        self.interface = None

        self.is_ready    = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._send_lock  = asyncio.Lock()

        self._on_receive_cb:      Optional[Callable] = None
        self._on_connection_cb:   Optional[Callable] = None
        self._on_node_updated_cb: Optional[Callable] = None

        self._mqtt_client: Optional["mqtt.Client"] = None
        self._loop:        Optional[asyncio.AbstractEventLoop] = None
        self._pkt_queue:   Optional[asyncio.Queue] = None

        self._connected:       bool  = False
        self._user_disconnected: bool  = False  # True when user explicitly disconnected; cleared by force_reconnect
        self._wake_event: asyncio.Event = asyncio.Event()  # Set by force_reconnect to wake connect_loop
        self._reconnect_delay: float = self.RECONNECT_DELAY_MIN
        self._last_msg_time:   float = 0.0
        self._from_node_num:   int   = 0
        self._state: ConnectionState = ConnectionState.IDLE

        self.config: Dict[str, Any] = {
            "MESHTASTIC_CONNECTION_TYPE": "MQTT",
            "MQTT_BROKER":      "mqtt.meshtastic.org",
            "MQTT_PORT":        "1883",
            "MQTT_USERNAME":    "meshdev",
            "MQTT_PASSWORD":    "large4cats",
            "MQTT_TLS":         "false",
            "MQTT_REGION":      "EU_868",
            "MQTT_CHANNEL":     "#",
            "MQTT_CLIENT_ID":   "",
            "MQTT_NODE_ID":     "",
            "MQTT_ROOT_TOPIC":  "",
            "MQTT_DROP_TELEMETRY": "false",
            "MQTT_DROP_NEIGHBOR":  "false",
            "MQTT_DROP_POSITION":  "false",
            "MQTT_DROP_NODEINFO":  "false",
            "MQTT_DROP_ENCRYPTED": "false",
            "MQTT_DROP_ROUTING":   "false",
        }
        if connection_params:
            for k, v in connection_params.items():
                if k in self.config:
                    self.config[k] = str(v) if v is not None else ""

        node_id_str = self.config.get("MQTT_NODE_ID", "").strip()
        if node_id_str.startswith("!"):
            try:
                self._from_node_num = int(node_id_str[1:], 16)
            except ValueError:
                pass

        # Channel PSK store: {channel_name: expanded_key_bytes}
        # Populated via set_channel_psk().  Capped at 64 entries.
        self._channel_psks: Dict[str, bytes] = {}


    def set_channel_psk(self, channel_id: str, psk_b64: str) -> bool:
        """
        Store a PSK for a named channel so encrypted packets on that channel
        can be decrypted on receive, and used as the encryption key on send.
        psk_b64 is the base64-encoded key shown in the Meshtastic app settings.
        Returns True if the key was successfully parsed and stored.
        """
        key = _expand_psk(psk_b64)
        if not key:
            self.logger.warning("MQTT: failed to parse PSK for channel '%s'", channel_id)
            return False
        if len(self._channel_psks) >= 64:
            oldest = next(iter(self._channel_psks))
            del self._channel_psks[oldest]
        self._channel_psks[channel_id] = key
        self.logger.info("MQTT: PSK set for channel '%s' (%d bytes)", channel_id, len(key))
        return True


    def set_packet_queue(self, queue: asyncio.Queue) -> None:
        self._pkt_queue = queue


    def register_callbacks(
        self,
        on_receive: Callable,
        on_connection: Callable,
        on_node_updated: Callable,
    ) -> None:
        self._on_receive_cb      = on_receive
        self._on_connection_cb   = on_connection
        self._on_node_updated_cb = on_node_updated

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        """Transition to a new connection state, enforcing valid transitions."""
        from core.connections import is_valid_transition
        prev = self._state
        if not is_valid_transition(prev, state):
            self.logger.warning(f"Invalid state transition: {prev.value} -> {state.value} (detail={detail})")
        self._state = state
        broker = self.config.get("MQTT_BROKER", "")
        port = self.config.get("MQTT_PORT", "1883")
        transport_info = f"MQTT {broker}:{port}" if broker else "MQTT"
        self.meshtastic_data.set_connection_state(state, detail=detail, transport=transport_info)

    async def force_reconnect(self) -> None:
        """Reset retry counter and transition back to CONNECTING from DISCONNECTED.

        Called when user clicks Reconnect. Clears the _user_disconnected flag so
        the connect_loop resumes connection attempts. Wakes the connect_loop
        immediately via _wake_event.
        """
        self.logger.info("force_reconnect() called - resetting retry counter.")
        self._user_disconnected = False
        self._connected = False
        self._stop_event.clear()
        self._set_state(ConnectionState.CONNECTING, detail="User requested reconnect")
        self._wake_event.set()  # Wake the connect_loop immediately


    def _build_subscribe_topic(self) -> str:
        override = self.config.get("MQTT_ROOT_TOPIC", "").strip()
        if override:
            return override
        region  = self.config.get("MQTT_REGION",  "EU_868").strip() or "EU_868"
        channel = self.config.get("MQTT_CHANNEL", "#").strip() or "#"
        # Never produce "msh/#" — the public Meshtastic broker rejects
        # fully-wildcard subscriptions and disconnects the client.
        # If region is "#", fall back to EU_868 (most common for self-hosted).
        if region == "#":
            region = "EU_868"
        if channel == "#":
            return f"msh/{region}/2/e/#"
        return f"msh/{region}/2/e/{channel}/#"

    def _build_publish_topic(self, channel_name: str = "LongFast") -> str:
        region = self.config.get("MQTT_REGION", "EU_868").strip()
        if not region or region == "#":
            region = "EU_868"
        gw = self.config.get("MQTT_NODE_ID", "!00000000").strip() or "!00000000"
        return f"msh/{region}/2/e/{channel_name}/{gw}"

    def _make_client_id(self) -> str:
        cid = self.config.get("MQTT_CLIENT_ID", "").strip()
        if cid:
            return cid
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        return f"meshdash_{self.slot_id}_{suffix}"


    def _on_paho_connect(self, client, userdata, flags, rc, props=None) -> None:
        # paho v2 adds a 'props' arg; accept it but ignore (we use MQTT v3.1.1)
        if rc == 0:
            self.logger.info("✅ MQTT connected to %s", self.config.get("MQTT_BROKER"))
            self._connected       = True
            self._reconnect_delay = self.RECONNECT_DELAY_MIN

            topic = self._build_subscribe_topic()
            client.subscribe(topic, qos=0)
            self.logger.info("📡 MQTT subscribed to: %s", topic)

            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self.is_ready.set)
                self._loop.call_soon_threadsafe(
                    self._set_state, ConnectionState.CONNECTED, "MQTT session active"
                )

            if self._on_connection_cb and self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    lambda: self._on_connection_cb(self, topic="meshtastic.connection.established")
                )

            if self._from_node_num and self._loop and self._loop.is_running():
                nid = f"!{self._from_node_num:08x}"
                self._loop.call_soon_threadsafe(self._set_synthetic_local_node, nid)

        else:
            rc_msgs = {
                1: "bad protocol", 2: "bad client ID", 3: "broker unavailable",
                4: "bad credentials", 5: "not authorised",
            }
            self.logger.error("❌ MQTT connect failed: rc=%d (%s)", rc, rc_msgs.get(rc, "unknown"))
            self._connected = False
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self.is_ready.clear)
                self._loop.call_soon_threadsafe(
                    self._set_state, ConnectionState.DISCONNECTED, f"MQTT auth failed (rc={rc})"
                )

    def _on_paho_disconnect(self, client, userdata, flags, rc, props=None) -> None:
        # paho v2 adds 'flags' and 'props' args to disconnect callback
        self._connected = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.is_ready.clear)
            # Auth failures and protocol errors should NOT auto-reconnect
            _fatal_rc = {4, 5}  # 4=bad credentials, 5=not authorised
            if rc in _fatal_rc:
                self._loop.call_soon_threadsafe(
                    self._set_state, ConnectionState.DISCONNECTED, f"MQTT auth failed (rc={rc})"
                )
                self.logger.error("❌ MQTT auth failure (rc=%d) — not reconnecting", rc)
            else:
                self._loop.call_soon_threadsafe(
                    self._set_state, ConnectionState.RECONNECTING, f"MQTT disconnected (rc={rc})"
                )
        if rc != 0:
            self.logger.warning("MQTT disconnected unexpectedly (rc=%d)", rc)

    def _on_paho_message(self, client, userdata, msg) -> None:
        """Called on paho network thread for every received MQTT message."""
        self._last_msg_time = time.time()
        try:
            packet = _decode_service_envelope(msg.payload)
            if packet is None:
                return

            packet["slot_id"]       = self.slot_id
            packet["heard_by_slot"] = self.slot_id
            packet["_mqtt_topic"]   = msg.topic

            # Guarantee fromId / toId are always set
            from_num = packet.get("from", 0)
            to_num   = packet.get("to",   0)
            if not packet.get("fromId"):
                packet["fromId"] = f"!{from_num:08x}" if from_num else None
            if not packet.get("toId"):
                if to_num == 0xFFFFFFFF:
                    packet["toId"] = "^all"
                elif to_num:
                    packet["toId"] = f"!{to_num:08x}"
                else:
                    # to=0 means implicit/broadcast in Meshtastic — treat as ^all
                    packet["toId"] = "^all"

            # If still encrypted after default-key attempt in _decode_service_envelope,
            # try any user-supplied PSK keyed by the MQTT channel_id.
            if packet.get("encrypted") and self._channel_psks and _HAS_CRYPTO:
                channel_id = packet.get("_mqtt_channel_id", "")
                user_key   = self._channel_psks.get(channel_id)
                if user_key:
                    try:
                        env2 = mqtt_pb2.ServiceEnvelope()
                        env2.ParseFromString(msg.payload)
                        enc_bytes = bytes(env2.packet.encrypted)
                        raw = _try_decrypt_with_key(enc_bytes, packet.get("id", 0),
                                                     packet.get("from", 0), user_key)
                        if raw:
                            data_obj = mesh_pb2.Data()
                            data_obj.ParseFromString(raw)
                            decoded: Dict[str, Any] = {"viaMqtt": True}
                            _decode_portnum(data_obj, data_obj.portnum, decoded)
                            packet["decoded"]         = decoded
                            packet["encrypted"]       = False
                            packet["app_packet_type"] = _PORTNUM_NAME.get(
                                data_obj.portnum, f"port_{data_obj.portnum}"
                            )
                    except Exception:
                        pass

            # Echo suppression: drop packets from our own node
            if self._from_node_num and packet.get("from") == self._from_node_num:
                return

            # Firehose filters
            ptype = packet.get("app_packet_type", "")

            def is_dropped(config_key: str) -> bool:
                val = self.config.get(config_key, "false")
                return (val.lower() if isinstance(val, str) else str(val)) in ("true", "1", "yes")

            if ptype == "Telemetry"   and is_dropped("MQTT_DROP_TELEMETRY"):  return
            if ptype == "Position"    and is_dropped("MQTT_DROP_POSITION"):   return
            if ptype == "NodeInfo"    and is_dropped("MQTT_DROP_NODEINFO"):   return
            if ptype == "NeighborInfo" and is_dropped("MQTT_DROP_NEIGHBOR"): return
            if ptype in ("Routing", "Traceroute") and is_dropped("MQTT_DROP_ROUTING"): return
            if packet.get("encrypted") and is_dropped("MQTT_DROP_ENCRYPTED"): return

            if self._pkt_queue and self._loop and self._loop.is_running():
                try:
                    self._loop.call_soon_threadsafe(self._pkt_queue.put_nowait, packet)
                except asyncio.QueueFull:
                    self.logger.warning("MQTT [%s]: packet queue full — dropping", self.slot_id)
                except RuntimeError:
                    pass  # loop closed during shutdown

        except Exception as e:
            self.logger.error("MQTT message handler error: %s", e, exc_info=True)


    def _set_synthetic_local_node(self, nid: str) -> None:
        md = self.meshtastic_data
        md.local_node_id = nid
        if nid not in md.nodes:
            md.nodes[nid] = {}
        md.nodes[nid].update({"node_id": nid, "isLocal": True, "is_local": True})
        try:
            node_num = int(nid[1:], 16)
        except (ValueError, IndexError):
            node_num = 0
        md.local_node_info = {
            "node_id":               nid,
            "node_num":              node_num,
            "node_id_hex":           nid,
            "hardware_model_string": "MQTT Gateway",
            "firmware_version":      "N/A",
            "long_name":             f"MQTT [{self.slot_id}]",
            "short_name":            "MQTT",
            "connection":            "MQTT",
            "mqtt_broker":           self.config.get("MQTT_BROKER", ""),
            "mqtt_topic":            self._build_subscribe_topic(),
        }
        self.logger.info("✅ MQTT synthetic local node: %s", nid)
        # Broadcast so frontend immediately knows our identity
        if g.main_event_loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "local_node_info", "data": md.local_node_info}, slot_id=self.slot_id),
                    g.main_event_loop,
                )
            except Exception as e:
                self.logger.warning("Failed to broadcast local_node_info: %s", e)


    def resolve_channel_name(self, channel_index: int) -> str:
        """Resolve a Meshtastic channel index to its MQTT channel name.

        Uses the channel_map built from NodeInfo packets seen on the mesh.
        Falls back to the MQTT_CHANNEL config, then to 'LongFast'.
        """
        # channel_map maps channel_id → index; we need the reverse.
        # Build a reverse lookup: index → channel_id/name
        md = self.meshtastic_data
        if md and md.local_node_info and md.local_node_info.get("channels_json"):
            try:
                import json
                chans = json.loads(md.local_node_info["channels_json"])
                for ch in chans:
                    if ch.get("index") == channel_index:
                        name = ch.get("settings", {}).get("name", "")
                        if name:
                            return name
            except Exception:
                pass

        # Fallback: scan channel_map for matching index
        if md and hasattr(md, 'channel_map'):
            for ch_id, ch_idx in md.channel_map.items():
                if ch_idx == channel_index and ch_id:
                    return str(ch_id)

        # Final fallback: default names for standard indices
        _defaults = {0: "LongFast"}
        return _defaults.get(channel_index, self.config.get("MQTT_CHANNEL", "LongFast").strip() or "LongFast")

    async def sendText(
        self,
        text: str,
        destinationId: str = "^all",
        channelIndex: int = 0,
        wantAck: bool = False,
        psk: Optional[bytes] = None,
    ):
        """
        Publish an AES-encrypted text message to the mesh via MQTT.

        Returns {"id": packet_id} on success so send_msg can store the packet ID
        as mesh_packet_id, enabling ACK tracking:
          recipient → ROUTING_APP ACK (requestId=packet_id) → save_packet() →
          UPDATE messages SET status='DELIVERED' WHERE mesh_packet_id=packet_id →
          broadcast message_status_update SSE event → UI tick marks delivered.

        PSK resolution: caller-supplied > _channel_psks[channel_name] > _DEFAULT_PSK.
        """
        if not self._mqtt_client or not self._connected:
            self.logger.error("❌ MQTT sendText: not connected")
            return None
        if not self._from_node_num:
            self.logger.warning(
                "⚠️  MQTT sendText: MQTT_NODE_ID not set — set it in the slot config."
            )
            return None

        try:
            # Resolve channel name from index — critical for correct MQTT topic routing
            channel_name = self.resolve_channel_name(channelIndex)

            # Resolve PSK: caller > stored by channel name > default
            if psk is None:
                psk = self._channel_psks.get(channel_name) or _DEFAULT_PSK

            # Generate packet ID here so we can return it to the caller.
            # The receiving radio echoes this in its ROUTING_APP ACK (requestId).
            mp_id = random.randint(1, 0xFFFFFFFF)

            gw_id = self.config.get("MQTT_NODE_ID", "!00000000").strip() or "!00000000"
            payload = _encode_text_to_mqtt(
                text=text,
                destination_id=destinationId,
                channel_index=channelIndex,
                from_node_num=self._from_node_num,
                channel_id=channel_name,
                gateway_id=gw_id,
                psk=psk,
                packet_id=mp_id,
            )
            topic = self._build_publish_topic(channel_name)

            async with self._send_lock:
                await asyncio.to_thread(
                    self._mqtt_client.publish, topic, payload, qos=0
                )
            self.logger.info(
                "📤 MQTT sent → dest=%s ch=%d name=%s topic=%s id=%d encrypted=True",
                destinationId, channelIndex, channel_name, topic, mp_id,
            )
            # Return dict with id so send_msg can do mesh_packet.get("id")
            return {"id": mp_id}

        except Exception as e:
            self.logger.error("❌ MQTT sendText error: %s", e)
            return None


    async def shutdown(self) -> None:
        """Full teardown — used for slot deletion or server shutdown.
        Stops the connect_loop and disconnects MQTT. The slot is dead after this."""
        self.logger.info("🛑 MQTT shutdown requested.")
        self._stop_event.set()
        self.is_ready.clear()
        client = self._mqtt_client
        if client:
            self._mqtt_client = None
            try:
                await asyncio.wait_for(asyncio.to_thread(client.loop_stop),
                                       timeout=self.SHUTDOWN_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
            try:
                await asyncio.wait_for(asyncio.to_thread(client.disconnect), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
        self._set_state(ConnectionState.DISCONNECTED, detail="Shutdown")

    async def disconnect(self) -> None:
        """User-initiated disconnect — closes MQTT but keeps connect_loop alive.

        Unlike shutdown(), this does NOT set _stop_event. The connect_loop continues
        running and will park in DISCONNECTED state until force_reconnect() is called.
        """
        self.logger.info("🔌 MQTT user disconnect requested — closing client, keeping connect_loop alive.")
        self._user_disconnected = True
        self._connected = False
        self.is_ready.clear()
        client = self._mqtt_client
        if client:
            self._mqtt_client = None
            try:
                await asyncio.wait_for(asyncio.to_thread(client.loop_stop),
                                       timeout=self.SHUTDOWN_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
            try:
                await asyncio.wait_for(asyncio.to_thread(client.disconnect), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
        self._set_state(ConnectionState.DISCONNECTED, detail="Disconnected by user")

    async def disconnect_for_restart(self, settle_seconds: float = 2.0) -> None:
        await self.shutdown()
        await asyncio.sleep(settle_seconds)


    async def connect_loop(self) -> None:
        if not _HAS_PAHO:
            self.logger.error("paho-mqtt not available - MQTT slot cannot start")
            self._set_state(ConnectionState.DISCONNECTED, detail="paho-mqtt missing")
            return

        self._loop = asyncio.get_running_loop()
        self._set_state(ConnectionState.IDLE)
        self.logger.info("MQTT Connection Manager v1.2 (State Machine) started for slot '%s'", self.slot_id)

        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            session_ok = False
            try:
                session_ok = await self._run_paho_session()
            except asyncio.CancelledError:
                self.logger.info("MQTT connect_loop cancelled.")
                break
            except Exception as e:
                self.logger.error("MQTT session error (attempt %d): %s", attempt, e)

            if self._stop_event.is_set():
                break

            # If user explicitly disconnected, park here until force_reconnect
            if self._user_disconnected:
                self.logger.debug("Parking in DISCONNECTED state — waiting for user reconnect.")
                await self._interruptible_sleep_mqtt(5.0)
                attempt = 0  # Don't count parking as a failed attempt
                continue

            if session_ok:
                attempt = 0
                continue

            # Check max retry cap
            if attempt >= self.MAX_RECONNECT_ATTEMPTS:
                self._set_state(
                    ConnectionState.DISCONNECTED,
                    detail=f"Max retries ({self.MAX_RECONNECT_ATTEMPTS}) reached - click Reconnect to retry"
                )
                self.logger.error("Max MQTT reconnect attempts (%d) reached - giving up.", self.MAX_RECONNECT_ATTEMPTS)
                await self._interruptible_sleep_mqtt(60.0)
                continue

            delay = min(self.RECONNECT_DELAY_MIN * (2 ** min(attempt, 5)), self.RECONNECT_DELAY_MAX)
            self.logger.info("MQTT reconnecting in %.1fs...", delay)
            self._set_state(
                ConnectionState.RECONNECTING,
                detail=f"attempt {attempt}, next in {delay:.0f}s"
            )
            await self._interruptible_sleep_mqtt(delay)

        self.logger.info("MQTT connect_loop exited cleanly.")

    async def _interruptible_sleep_mqtt(self, seconds: float) -> None:
        """Sleep that can be interrupted by _stop_event (shutdown) or _wake_event (reconnect)."""
        try:
            done, pending = await asyncio.wait(
                {asyncio.create_task(self._stop_event.wait()),
                 asyncio.create_task(self._wake_event.wait())},
                timeout=seconds,
            )
            for p in pending:
                p.cancel()
            self._wake_event.clear()
        except asyncio.TimeoutError:
            pass

    async def _run_paho_session(self) -> bool:
        """Run a single MQTT session. Returns True if session ended gracefully (shutdown),
        False if it ended due to disconnect (needs retry)."""
        broker   = (self.config.get("MQTT_BROKER", "") or "mqtt.meshtastic.org").strip()
        try:
            port = int(self.config.get("MQTT_PORT", "1883") or "1883")
        except (ValueError, TypeError):
            port = 1883
        username = self.config.get("MQTT_USERNAME", "").strip()
        password = self.config.get("MQTT_PASSWORD", "").strip()
        use_tls  = self.config.get("MQTT_TLS", "false").lower() in ("true", "1", "yes")
        cid      = self._make_client_id()

        self.logger.info("🔌 MQTT connecting: broker=%s port=%d tls=%s topic=%s",
                         broker, port, use_tls, self._build_subscribe_topic())
        self._set_state(ConnectionState.CONNECTING, detail=f"{broker}:{port}")

        # paho-mqtt v2.x requires callback_api_version as the first arg.
        # v1 is just mqtt.Client(client_id=...)
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=cid,
                clean_session=True,
                protocol=mqtt.MQTTv311,
            )
        else:
            client = mqtt.Client(client_id=cid, clean_session=True, protocol=mqtt.MQTTv311)

        if username:
            client.username_pw_set(username, password or None)

        if use_tls:
            try:
                tls_ctx = ssl.create_default_context()
                client.tls_set_context(tls_ctx)
            except Exception as tls_err:
                self.logger.error("❌ MQTT TLS setup failed: %s", tls_err)
                raise

        client.on_connect    = self._on_paho_connect
        client.on_disconnect = self._on_paho_disconnect
        client.on_message    = self._on_paho_message

        # Disable paho's internal reconnect — our outer loop owns all retries
        client.reconnect_delay_set(min_delay=1, max_delay=1)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(client.connect, broker, port, keepalive=60),
                timeout=self.CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.logger.error("❌ MQTT connect() timed out after %.0fs (broker=%s:%d)",
                              self.CONNECT_TIMEOUT, broker, port)
            raise ConnectionError(f"MQTT connect timeout ({broker}:{port})")
        except Exception as e:
            self.logger.error("❌ MQTT connect() failed: %s", e)
            raise

        self._mqtt_client = client
        client.loop_start()
        self.logger.info("🐝 paho network thread started for slot '%s'", self.slot_id)

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
                if self._stop_event.is_set():
                    break
                if not self._connected:
                    self.logger.warning("⚠️  MQTT [%s]: broker disconnected — retrying", self.slot_id)
                    # Session ended due to disconnect, not shutdown
                    self._session_healthy = False
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.logger.info("🔌 MQTT session ending [slot=%s]", self.slot_id)
            self.is_ready.clear()
            self._connected   = False
            self._mqtt_client = None
            try:
                await asyncio.wait_for(asyncio.to_thread(client.loop_stop),
                                       timeout=self.SHUTDOWN_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
            try:
                await asyncio.wait_for(asyncio.to_thread(client.disconnect), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
        return getattr(self, '_session_healthy', True)


    @property
    def my_node_id(self) -> Optional[int]:
        return self._from_node_num or None

    @property
    def nodes(self) -> dict:
        return self.meshtastic_data.nodes

    def request_reboot(self) -> None:
        self.logger.warning("request_reboot() not supported for MQTT connections.")

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None