"""
meshcore_connection.py — MeshDash MeshCore Connection Manager v1.0
─────────────────────────────────────────────────────────────────────────────
Provides MeshCoreConnectionManager, a drop-in companion to both
MeshtasticConnectionManager and MQTTConnectionManager.

Shares the same external contract:
  - .is_ready              asyncio.Event
  - .connect_loop()        async — background task managed by meshtastic_dashboard.py
  - .sendText()            async — send text via MeshCore (DM or channel)
  - .shutdown()            async — clean disconnect
  - .disconnect_for_restart() async
  - .register_callbacks(on_receive, on_connection, on_node_updated)
  - .config                dict — standard key shape
  - .set_packet_queue(q)   inject the slot's packet queue

Architecture:
  Uses the official `meshcore` Python library (pip install meshcore ≥ 2.3).
  The library handles all transport framing (Serial / TCP / BLE), the binary
  companion protocol, and reconnection.  This manager:
    1. Creates a MeshCore instance via the appropriate transport factory
    2. Subscribes to all relevant EventType events
    3. Translates MeshCore events → MeshDash packet dicts (the exact shape
       that add_packet / save_packet / _classify expect)
    4. Places translated packets on the slot's asyncio.Queue
    5. Provides sendText() as the outbound path

Translation table (MeshCore → MeshDash):
  CONTACT_MSG_RECV  → app_packet_type="Message", fromId="!{pubkey_prefix}"
  CHANNEL_MSG_RECV  → app_packet_type="Message", toId="^all", channel=idx
  ADVERTISEMENT     → app_packet_type="Node Info" (position + user)
  SELF_INFO         → local node identity (populates local_node_id)
  ACK               → app_packet_type="Ack", decoded.requestId=matched_id
  NEW_CONTACT       → app_packet_type="Node Info" (contact discovery)
  RX_LOG_DATA       → SNR/RSSI enrichment, buffered per message key

Node ID convention:
  MeshCore identifies nodes by a 6-byte public key prefix (12 hex chars).
  MeshDash uses "!hexstring" for all node IDs.  We map:
    pubkey_prefix  →  "!{pubkey_prefix}"   e.g. "!a1b2c3d4e5f6"
  This is 12 hex chars instead of Meshtastic's 8, but the dashboard treats
  node IDs as opaque strings throughout — no arithmetic is performed on them.

Supported config keys:
  MESHCORE_TRANSPORT   "serial" | "tcp" | "ble"   (default: "serial")
  MESHCORE_SERIAL_PORT /dev/ttyUSB0 or COM3       (serial transport)
  MESHCORE_BAUD        115200                      (serial baud rate)
  MESHCORE_HOST        192.168.1.100               (tcp transport)
  MESHCORE_PORT        4000                        (tcp port, default 4000)
  MESHCORE_BLE_MAC     12:34:56:78:90:AB           (ble transport)
  MESHCORE_BLE_PIN     optional PIN for BLE pairing
  MESHCORE_LABEL       human-readable node name for display
"""

from core.routes.schemas import User, NodeSlot
from fastapi import Query, Request, status
import asyncio
import logging
import random
from core.connections import ConnectionState
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("MeshCoreConnection")

# ── meshcore library ──────────────────────────────────────────────────────────
try:
    from meshcore import MeshCore, EventType
    _HAS_MESHCORE = True
except ImportError:
    _HAS_MESHCORE = False
    logger.warning(
        "meshcore library not installed — MeshCore slots unavailable. "
        "Install with: pip install meshcore --break-system-packages"
    )

# ── RX_LOG correlation window (ms) — matches HA integration pattern ───────────
_RX_LOG_WINDOW = 0.6   # seconds to collect RX_LOG events before attaching to msg

# ── MeshCore node type codes (from firmware) ──────────────────────────────────
_MC_NODE_TYPES = {
    1: "CLIENT",
    2: "REPEATER",
    3: "ROOM",
    4: "SENSOR",
}


def _pubkey_to_node_id(pubkey_prefix: str) -> str:
    """
    Convert a MeshCore pubkey_prefix to a MeshDash node ID string.

    MeshCore prefix is 6 bytes (12 hex chars), e.g. "a1b2c3d4e5f6".
    MeshDash node IDs use "!hexstring" convention.
    We produce "!a1b2c3d4e5f6" — 12 hex chars, always lowercase.
    """
    if not pubkey_prefix:
        return "!000000000000"
    raw = pubkey_prefix.lstrip("!").lower()
    clean = "".join(c for c in raw if c in "0123456789abcdef")
    if len(clean) < 12:
        clean = clean.ljust(12, "0")
    else:
        clean = clean[:12]
    return f"!{clean}"


def _node_id_to_pubkey_prefix(node_id: str) -> str:
    """Convert a MeshDash node ID back to a MeshCore pubkey_prefix hex string."""
    return node_id.lstrip("!").lower()[:12]


def _safe_shortname(long_name: str, fallback: str = "") -> str:
    """
    Derive a ≤4-char shortName from a long name following Meshtastic convention:
    take the first letter of each word, up to 4 chars, stripping spaces.
    Falls back to first 4 non-space chars of long_name, then to fallback.
    """
    if not long_name:
        return (fallback[:4] if fallback else "MC")
    words = long_name.split()
    initials = "".join(w[0].upper() for w in words if w)[:4]
    if initials:
        return initials
    return "".join(c for c in long_name if c != " ")[:4] or fallback[:4] or "MC"


def _get_field(*dicts_and_keys):
    """
    Safe multi-key lookup: _get_field(payload, "bat", "battery", "battery_level")
    Returns first non-None value found.  Handles 0 correctly (not falsy).
    """
    payload = dicts_and_keys[0]
    for key in dicts_and_keys[1:]:
        v = payload.get(key)
        if v is not None:
            return v
    return None


def _safe_snr(raw_snr) -> Optional[float]:
    """
    MeshCore SNR is transmitted as an integer = SNR * 4 (signed byte).
    Convert to float dB.  Returns None if value is absent or clearly invalid.
    """
    if raw_snr is None:
        return None
    try:
        v = int(raw_snr)
        if not (-128 <= v <= 127):
            return None
        return round(v / 4.0, 2)
    except (ValueError, TypeError):
        return None


def _safe_snr_from_float(v) -> Optional[float]:
    """Accept already-decoded float SNR (some library versions decode it directly)."""
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (ValueError, TypeError):
        return None


def _path_len_to_hop_limit(path_len) -> Optional[int]:
    """
    MeshCore path_len: 0xFF = direct (no hop), otherwise = hop count.
    Map to Meshtastic hopLimit convention (0 = direct).
    """
    if path_len is None:
        return None
    try:
        v = int(path_len)
        return 0 if v == 0xFF else v
    except (ValueError, TypeError):
        return None


def _safe_node_type(raw_type) -> str:
    """
    Resolve MeshCore node type to a string role name.
    Handles both integer codes and string names from different firmware versions.
    """
    if raw_type is None:
        return "CLIENT"
    # Already a string (some firmware versions return "CLIENT", "REPEATER" etc.)
    if isinstance(raw_type, str):
        upper = raw_type.upper()
        if upper in _MC_NODE_TYPES.values():
            return upper
        # Try parsing as an integer string
        try:
            return _MC_NODE_TYPES.get(int(raw_type), "CLIENT")
        except (ValueError, TypeError):
            return "CLIENT"
    try:
        return _MC_NODE_TYPES.get(int(raw_type), "CLIENT")
    except (ValueError, TypeError):
        return "CLIENT"


class MeshCoreConnectionManager:
    """
    Manages a persistent MeshCore companion radio connection for one NodeSlot.

    Structurally mirrors MQTTConnectionManager so meshtastic_dashboard.py can
    interact with it identically — no changes needed in the core pipeline.
    """

    RECONNECT_DELAY_MIN:   float = 3.0
    RECONNECT_DELAY_MAX:   float = 60.0
    HEALTH_CHECK_INTERVAL: float = 30.0
    CONNECT_TIMEOUT:       float = 20.0
    SHUTDOWN_TIMEOUT:      float = 5.0
    MSG_FETCH_INTERVAL:    float = 2.0   # poll for queued messages this often
    MAX_RECONNECT_ATTEMPTS: int  = 50

    def __init__(
        self,
        meshtastic_data,
        logger_: Optional[logging.Logger] = None,
        connection_params: Optional[Dict[str, Any]] = None,
        slot_id: str = "node_0",
    ):
        self.meshtastic_data = meshtastic_data
        self.logger   = logger_ or logging.getLogger(f"MeshCoreConnection.{slot_id}")
        self.slot_id  = slot_id
        self.interface = None   # always None — satisfies duck-type checks

        self.is_ready    = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._send_lock  = asyncio.Lock()

        self._on_receive_cb:      Optional[Callable] = None
        self._on_connection_cb:   Optional[Callable] = None
        self._on_node_updated_cb: Optional[Callable] = None

        self._mc: Optional["MeshCore"] = None          # live MeshCore instance
        self._pkt_queue: Optional[asyncio.Queue] = None
        self._loop:      Optional[asyncio.AbstractEventLoop] = None
        self._user_disconnected: bool = False  # True when user explicitly disconnected; cleared by force_reconnect
        self._wake_event: asyncio.Event = asyncio.Event()  # Set by force_reconnect to wake connect_loop

        # ACK tracking: expected_ack_hex → packet_id stored in DB
        # When EventType.ACK fires we emit a Routing/Ack packet so save_packet
        # can UPDATE messages SET status='DELIVERED' WHERE mesh_packet_id=?
        self._pending_acks: Dict[str, int] = {}   # ack_code_hex → packet_id
        self._ack_lock = asyncio.Lock()

        # RX_LOG correlation: message_key → list of {snr, rssi, ...}
        self._pending_rx_logs: Dict[str, List[Dict]] = {}

        # Contacts cache: pubkey_prefix → contact dict (from get_contacts)
        self._contacts: Dict[str, Dict] = {}

        # Channel info cache: channel_idx → {name, secret, ...} (from get_channel)
        self._channel_info: Dict[int, Dict] = {}

        # Local node info populated from SELF_INFO event
        self._local_pubkey: str = ""   # 12-char hex (no "!" prefix)
        self._local_name:   str = ""
        self._state: ConnectionState = ConnectionState.IDLE

        # Config — matches the key shape convention used across all managers
        self.config: Dict[str, Any] = {
            "MESHTASTIC_CONNECTION_TYPE": "MESHCORE",
            "MESHCORE_TRANSPORT":    "serial",
            "MESHCORE_SERIAL_PORT":  "/dev/ttyUSB0",
            "MESHCORE_BAUD":         "115200",
            "MESHCORE_HOST":         "192.168.1.100",
            "MESHCORE_PORT":         "4000",
            "MESHCORE_BLE_MAC":      "",
            "MESHCORE_BLE_PIN":      "",
            "MESHCORE_LABEL":        "",
        }
        if connection_params:
            for k, v in connection_params.items():
                if k in self.config:
                    self.config[k] = str(v) if v is not None else ""

    # ── Queue injection ───────────────────────────────────────────────────────

    def set_packet_queue(self, queue: asyncio.Queue) -> None:
        self._pkt_queue = queue

    # ── Callback registration ─────────────────────────────────────────────────

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
        transport = self.config.get("MESHCORE_TRANSPORT", "serial").upper()
        self.meshtastic_data.set_connection_state(state, detail=detail, transport=f"MeshCore/{transport}")

    async def force_reconnect(self) -> None:
        """Reset retry counter and transition back to CONNECTING from DISCONNECTED.

        Called when user clicks Reconnect. Clears the _user_disconnected flag so
        the connect_loop resumes connection attempts. Wakes the connect_loop
        immediately via _wake_event.
        """
        self.logger.info("force_reconnect() called - resetting retry counter.")
        self._user_disconnected = False
        self._stop_event.clear()
        self._set_state(ConnectionState.CONNECTING, detail="User requested reconnect")
        self._wake_event.set()  # Wake the connect_loop immediately

    # ── Internal packet enqueue ───────────────────────────────────────────────

    def _enqueue(self, packet: dict) -> None:
        """Thread/callback-safe: place a translated packet on the slot queue."""
        if not self._pkt_queue or not self._loop or not self._loop.is_running():
            return
        packet.setdefault("slot_id",       self.slot_id)
        packet.setdefault("heard_by_slot", self.slot_id)
        try:
            self._loop.call_soon_threadsafe(self._pkt_queue.put_nowait, packet)
        except asyncio.QueueFull:
            self.logger.warning("MeshCore [%s]: packet queue full — dropping", self.slot_id)
        except RuntimeError:
            pass  # loop closed during shutdown

    # ── Event handlers (subscribed after connect) ─────────────────────────────

    async def _on_self_info(self, event) -> None:
        """SELF_INFO — device's own configuration, populated on appstart."""
        p = event.payload or {}
        pubkey = p.get("public_key", "") or ""

        # Reset local state — ensures clean slate on reconnect even if node ID changed
        self._local_pubkey = pubkey[:12].lower() if pubkey else ""
        self._local_name   = _get_field(p, "name", "adv_name") or ""

        lat = _get_field(p, "adv_lat", "latitude")
        lon = _get_field(p, "adv_lon", "longitude")
        bat = _get_field(p, "bat", "battery", "battery_level")   # _get_field handles 0%
        node_id = _pubkey_to_node_id(self._local_pubkey) if self._local_pubkey else f"!mc_{self.slot_id}"
        now = int(time.time())

        self.meshtastic_data.local_node_id = node_id
        self.meshtastic_data.local_node_info = {
            "node_id":               node_id,
            "node_id_hex":           node_id,
            "hardware_model_string": p.get("model", "MeshCore"),
            "firmware_version":      _get_field(p, "fw_version", "firmware") or "N/A",
            "long_name":             self._local_name or f"MeshCore [{self.slot_id}]",
            "short_name":            _safe_shortname(self._local_name, self.slot_id),
            "connection":            "MESHCORE",
            "meshcore_transport":    self.config.get("MESHCORE_TRANSPORT", "serial"),
        }

        # Broadcast local_node_info so frontend knows our identity immediately
        if g.main_event_loop:
            try:
                from core.broadcast import broadcast_data
                from core.auth import ensure_serializable
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "local_node_info", "data": ensure_serializable(self.meshtastic_data.local_node_info)}, slot_id=self.slot_id),
                    g.main_event_loop,
                )
            except Exception as e:
                self.logger.warning("Failed to broadcast local_node_info: %s", e)

        # Enqueue a NodeInfo packet so add_packet → save_node gives this node a DB row
        nodeinfo_pkt: Dict[str, Any] = {
            "id":              random.randint(1, 0x7FFFFFFF),
            "fromId":          node_id,
            "toId":            "^all",
            "channel":         0,
            "rxTime":          now,
            "app_packet_type": "Node Info",
            "decoded": {
                "portnum": "NODEINFO_APP",
                "user": {
                    "id":        node_id,
                    "longName":  self._local_name or f"MeshCore [{self.slot_id}]",
                    "shortName": _safe_shortname(self._local_name, self.slot_id),
                    "hwModel":   p.get("model", "MeshCore"),
                    "role":      "CLIENT",
                },
            },
            "meshcore":  True,
            "_is_local": True,
        }
        if bat is not None:
            try:
                nodeinfo_pkt["decoded"]["deviceMetrics"] = {"batteryLevel": int(bat)}
            except (ValueError, TypeError):
                pass
        self._enqueue(nodeinfo_pkt)

        # Position packet if valid coordinates available
        if lat is not None and lon is not None:
            try:
                flat, flon = float(lat), float(lon)
                if flat != 0.0 or flon != 0.0:
                    self._enqueue({
                        "id":              random.randint(1, 0x7FFFFFFF),
                        "fromId":          node_id,
                        "toId":            "^all",
                        "channel":         0,
                        "rxTime":          now,
                        "app_packet_type": "Position",
                        "decoded": {
                            "portnum":  "POSITION_APP",
                            "position": {"latitude": flat, "longitude": flon, "time": now},
                        },
                        "meshcore": True,
                    })
            except (ValueError, TypeError):
                pass

        self.logger.info("✅ MeshCore self info: %s name=%s", node_id, self._local_name)

    async def _on_advertisement(self, event) -> None:
        """
        ADVERTISEMENT — another node has broadcast its presence.
        Emits separate NodeInfo and Position packets (mirrors Meshtastic behaviour).
        """
        p = event.payload or {}
        pubkey = p.get("public_key", "") or ""
        prefix = pubkey[:12].lower() if pubkey else ""
        if not prefix:
            return

        node_id  = _pubkey_to_node_id(prefix)
        adv_name = _get_field(p, "adv_name", "name") or ""
        lat      = _get_field(p, "adv_lat", "latitude")
        lon      = _get_field(p, "adv_lon", "longitude")
        bat      = _get_field(p, "bat", "battery", "battery_level")  # handles 0%
        ntype    = _safe_node_type(p.get("type"))
        now      = int(time.time())

        # Update contacts cache — keyed by 12-char hex prefix (no "!")
        self._contacts[prefix] = p

        short = _safe_shortname(adv_name, prefix[:4])

        # ── Packet 1: NodeInfo ────────────────────────────────────────────────
        nodeinfo_pkt: Dict[str, Any] = {
            "id":              random.randint(1, 0x7FFFFFFF),
            "fromId":          node_id,
            "toId":            "^all",
            "channel":         0,
            "rxTime":          now,
            "app_packet_type": "Node Info",
            "decoded": {
                "portnum": "NODEINFO_APP",
                "user": {
                    "id":        node_id,
                    "longName":  adv_name or node_id,
                    "shortName": short,
                    "hwModel":   p.get("model", ntype),
                    "role":      ntype,
                },
            },
            "meshcore": True,
        }
        if bat is not None:
            try:
                nodeinfo_pkt["decoded"]["deviceMetrics"] = {"batteryLevel": int(bat)}
            except (ValueError, TypeError):
                pass
        self._enqueue(nodeinfo_pkt)

        # ── Packet 2: Position (only when valid GPS fix present) ─────────────
        if lat is not None and lon is not None:
            try:
                flat, flon = float(lat), float(lon)
                if flat != 0.0 or flon != 0.0:
                    self._enqueue({
                        "id":              random.randint(1, 0x7FFFFFFF),
                        "fromId":          node_id,
                        "toId":            "^all",
                        "channel":         0,
                        "rxTime":          now,
                        "app_packet_type": "Position",
                        "decoded": {
                            "portnum":  "POSITION_APP",
                            "position": {"latitude": flat, "longitude": flon, "time": now},
                        },
                        "meshcore": True,
                    })
            except (ValueError, TypeError):
                pass

    async def _on_new_contact(self, event) -> None:
        """NEW_CONTACT — same structure as an advertisement for a newly discovered node."""
        await self._on_advertisement(event)

    async def _on_contact_msg(self, event) -> None:
        """
        CONTACT_MSG_RECV — direct message received from another node.

        MeshCore payload:
          pubkey_prefix  : str  (12 hex chars)
          text           : str
          timestamp      : int  (sender's clock)
          path_len       : int  (0xFF = direct)
          snr            : int|float  (SNR*4 if raw, or already float)

        We emit a standard TEXT_MESSAGE_APP packet with:
          fromId  = "!{pubkey_prefix}"
          toId    = our local node ID (this IS a DM to us)
        """
        p = event.payload or {}
        prefix   = (p.get("pubkey_prefix") or "").lower()
        text     = p.get("text", "") or ""
        ts       = p.get("timestamp") or p.get("sender_timestamp") or int(time.time())
        path_len = p.get("path_len")
        snr_raw  = p.get("snr")

        if not prefix or not text:
            return

        from_id  = _pubkey_to_node_id(prefix)
        to_id    = _pubkey_to_node_id(self._local_pubkey) if self._local_pubkey else f"!mc_{self.slot_id}"
        snr      = _safe_snr(snr_raw) if isinstance(snr_raw, int) else _safe_snr_from_float(snr_raw)
        hop_lim  = _path_len_to_hop_limit(path_len)
        pkt_id   = random.randint(1, 0x7FFFFFFF)
        now      = int(time.time())

        # Use sender's clock for rxTime — covers queued messages received on reconnect.
        # Sanity-clamp: reject timestamps more than 7 days old or in the future.
        try:
            sender_ts = int(ts)
            if not (now - 7 * 86400 <= sender_ts <= now + 60):
                sender_ts = now
        except (TypeError, ValueError):
            sender_ts = now

        packet: Dict[str, Any] = {
            "id":              pkt_id,
            "fromId":          from_id,
            "toId":            to_id,
            "channel":         0,
            "rxTime":          sender_ts,
            "rxSnr":           snr,
            "hopLimit":        hop_lim,
            "wantAck":         True,
            "app_packet_type": "Message",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text":    text,
                "payload": text,
            },
            "meshcore":        True,
        }
        self._enqueue(packet)
        self.logger.debug("MeshCore DM from %s: %s", from_id, text[:60])

    async def _on_channel_msg(self, event) -> None:
        """
        CHANNEL_MSG_RECV — broadcast message on a mesh channel.

        NOTE: This event is known to not fire on some firmware versions (nRF52840
        Companion USB v1.11.x). See github.com/meshcore-dev/MeshCore/issues/1232.
        The _run_session() method also polls get_msg() as a fallback.

        Sender resolution priority:
          1. pubkey_prefix field (V3 firmware)
          2. Library's get_contact_by_name() with case-insensitive substring match
          3. Stable per-channel unknown ID (never "^all")

        Implements a 500ms RX_LOG correlation window matching the meshcore-ha
        integration pattern, so SNR/RSSI data is attached to the message packet.
        """
        p = event.payload or {}
        ch_idx   = int(p.get("channel_idx") or p.get("channel") or 0)
        text     = p.get("text", "") or ""
        now      = int(time.time())
        raw_ts   = p.get("timestamp") or p.get("sender_timestamp")
        try:
            sender_ts = int(raw_ts)
            if not (now - 7 * 86400 <= sender_ts <= now + 60):
                sender_ts = now
        except (TypeError, ValueError):
            sender_ts = now

        path_len = p.get("path_len")
        snr_raw  = p.get("snr")

        if not text:
            return

        # ── 500ms RX_LOG correlation window ──────────────────────────────────
        # Wait briefly for RX_LOG_DATA events that carry SNR/RSSI for this packet.
        # This mirrors the meshcore-ha integration (logbook.py, 500ms window).
        await asyncio.sleep(0.5)

        # Build correlation key matching how RX_LOG events are stored
        corr_key = f"{ch_idx}:{sender_ts}:{text[:40]}"
        rx_log_entries = self._pending_rx_logs.pop(corr_key, None)
        # Enrich SNR from RX_LOG if available and not already in the event
        if rx_log_entries and snr_raw is None:
            best = max(
                (e for e in rx_log_entries if e.get("snr") is not None),
                key=lambda e: e["snr"],
                default=None,
            )
            if best:
                snr_raw = best["snr"]

        # ── Sender resolution ─────────────────────────────────────────────────
        pubkey_prefix = p.get("pubkey_prefix", "") or ""
        from_id: Optional[str] = None

        if pubkey_prefix:
            from_id = _pubkey_to_node_id(pubkey_prefix.lower())
        elif ": " in text and self._mc:
            # Older firmware embeds sender name: "SenderName: message body"
            sender_name = text.split(": ", 1)[0].strip()
            if sender_name:
                # Use library's case-insensitive substring match (more robust than exact)
                contact = self._mc.get_contact_by_name(sender_name)
                if contact:
                    pk = (contact.get("public_key") or contact.get("pubkey") or "")[:12].lower()
                    if pk:
                        from_id = _pubkey_to_node_id(pk)

        if not from_id:
            from_id = f"!mc_ch{ch_idx}_unknown"

        snr     = _safe_snr(snr_raw) if isinstance(snr_raw, int) else _safe_snr_from_float(snr_raw)
        hop_lim = _path_len_to_hop_limit(path_len)
        pkt_id  = random.randint(1, 0x7FFFFFFF)

        pkt: Dict[str, Any] = {
            "id":              pkt_id,
            "fromId":          from_id,
            "toId":            "^all",
            "channel":         ch_idx,
            "rxTime":          sender_ts,
            "rxSnr":           snr,
            "hopLimit":        hop_lim,
            "wantAck":         False,
            "app_packet_type": "Message",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text":    text,
                "payload": text,
            },
            "meshcore": True,
        }
        if rx_log_entries:
            pkt["rx_log_data"] = rx_log_entries
        self._enqueue(pkt)
        self.logger.debug("MeshCore ch%d msg from %s: %s", ch_idx, from_id, text[:60])

    async def _on_ack(self, event) -> None:
        """
        ACK — message delivery confirmed by recipient.

        event.payload: {"code": "hexstring"}  (the expected_ack returned by send_msg)

        We match the ack code against _pending_acks to find the packet_id stored
        in the messages DB, then emit a Routing/Ack packet so save_packet() can
        UPDATE messages SET status='DELIVERED'.
        """
        p = event.payload or {}
        code_hex = str(p.get("code", "") or "").lower()
        if not code_hex:
            return

        async with self._ack_lock:
            entry = self._pending_acks.pop(code_hex, None)

        if entry is None:
            self.logger.debug("MeshCore ACK code %s — no pending match", code_hex)
            return

        # entry is (pkt_id, timestamp) — unpack defensively
        try:
            pkt_id = entry[0] if isinstance(entry, tuple) else int(entry)
        except (TypeError, IndexError, ValueError):
            self.logger.debug("MeshCore ACK: malformed entry for code %s", code_hex)
            return

        now = int(time.time())
        packet: Dict[str, Any] = {
            "id":              random.randint(1, 0x7FFFFFFF),
            "fromId":          None,
            "toId":            None,
            "channel":         0,
            "rxTime":          now,
            "app_packet_type": "Ack",
            "decoded": {
                "portnum":     "ROUTING_APP",   # string — no protobuf lookup needed
                "requestId":   pkt_id,
                "errorReason": "NONE",
                "routing": {
                    "requestId":   pkt_id,
                    "errorReason": "NONE",
                },
            },
            "meshcore": True,
        }
        self._enqueue(packet)
        self.logger.debug("MeshCore ACK matched pkt_id=%d", pkt_id)

    async def _on_rx_log(self, event) -> None:
        """
        RX_LOG_DATA — raw RF reception metadata (SNR, RSSI) for a received packet.

        Stored under a correlation key that _on_channel_msg uses to attach
        SNR/RSSI data to the translated packet after a 500ms window.

        Key format: "{channel_idx}:{sender_timestamp}:{text[:40]}"
        — matches meshcore-ha's create_message_correlation_key pattern.

        Also stored under hash/key fields for any other lookup callers.
        Capped at 200 entries with 30s TTL to prevent memory growth.
        """
        p = event.payload or {}
        now = time.time()
        entry = {
            "snr":   _safe_snr(p.get("snr")) if isinstance(p.get("snr"), int) else _safe_snr_from_float(p.get("snr")),
            "rssi":  p.get("rssi"),
            "ts":    now,
        }

        # Primary key from raw hash/key field (set by library for some event types)
        raw_key = p.get("hash") or p.get("key")
        # Correlation key built from channel/timestamp/text (for _on_channel_msg matching)
        ch_idx  = p.get("channel_idx") or p.get("channel")
        ts_raw  = p.get("timestamp") or p.get("sender_timestamp")
        text    = (p.get("text") or "")[:40]
        if ch_idx is not None and ts_raw is not None:
            corr_key = f"{int(ch_idx)}:{int(ts_raw)}:{text}"
            if corr_key not in self._pending_rx_logs:
                self._pending_rx_logs[corr_key] = []
            self._pending_rx_logs[corr_key].append(entry)
        elif raw_key:
            if raw_key not in self._pending_rx_logs:
                self._pending_rx_logs[raw_key] = []
            self._pending_rx_logs[raw_key].append(entry)

        # Purge entries older than 30s, cap total at 200 keys
        cutoff = now - 30.0
        stale = [k for k, entries in self._pending_rx_logs.items()
                 if not entries or entries[-1]["ts"] < cutoff]
        for k in stale:
            del self._pending_rx_logs[k]
        if len(self._pending_rx_logs) > 200:
            try:
                del self._pending_rx_logs[next(iter(self._pending_rx_logs))]
            except (StopIteration, KeyError):
                pass

    async def _on_telemetry(self, event) -> None:
        """
        TELEMETRY_RESPONSE / STATUS_RESPONSE — device telemetry data.

        Can be from our own node (periodic refresh) or from a remote node
        (response to req_status / req_telemetry).  Routes to the correct
        node_id based on pubkey_prefix in the payload when present.
        """
        p = event.payload or {}
        now = int(time.time())

        # Determine which node this telemetry is for
        remote_pubkey = p.get("pubkey_prefix", "") or p.get("public_key", "") or ""
        if remote_pubkey:
            node_id = _pubkey_to_node_id(remote_pubkey[:12].lower())
        else:
            # No pubkey in payload → assume it's our own node
            node_id = _pubkey_to_node_id(self._local_pubkey) if self._local_pubkey else f"!mc_{self.slot_id}"

        device_metrics: Dict[str, Any] = {}

        bat = _get_field(p, "bat", "battery", "battery_level")  # _get_field handles 0%
        if bat is not None:
            try:
                device_metrics["batteryLevel"] = int(bat)
            except (TypeError, ValueError):
                pass

        voltage = _get_field(p, "voltage", "volt")
        if voltage is not None:
            try:
                device_metrics["voltage"] = round(float(voltage), 3)
            except (TypeError, ValueError):
                pass

        uptime = _get_field(p, "uptime", "uptime_seconds")
        if uptime is not None:
            try:
                device_metrics["uptimeSeconds"] = int(uptime)
            except (TypeError, ValueError):
                pass

        noise_floor = _get_field(p, "noise_floor", "noiseFloor")
        if noise_floor is not None:
            try:
                device_metrics["noiseFloor"] = int(noise_floor)
            except (TypeError, ValueError):
                pass

        tx_queue = _get_field(p, "tx_queue", "txQueue")
        if tx_queue is not None:
            try:
                device_metrics["txQueueSize"] = int(tx_queue)
            except (TypeError, ValueError):
                pass

        if not device_metrics:
            return

        self._enqueue({
            "id":              random.randint(1, 0x7FFFFFFF),
            "fromId":          node_id,
            "toId":            "^all",
            "channel":         0,
            "rxTime":          now,
            "app_packet_type": "Telemetry",
            "decoded": {
                "portnum":   "TELEMETRY_APP",
                "telemetry": {"deviceMetrics": device_metrics},
            },
            "meshcore": True,
        })
        self.logger.debug("MeshCore telemetry node=%s: %s", node_id, device_metrics)

    async def _on_connected(self, event) -> None:
        """
        CONNECTED - library-level connection established (or reconnected).
        Drives connection status display more accurately than health-loop polling.
        """
        p = event.payload or {}
        reconnected = p.get("reconnected", False)
        conn_info   = p.get("connection_info", "")
        transport   = self.config.get("MESHCORE_TRANSPORT", "serial")
        state = ConnectionState.RECONNECTING if reconnected else ConnectionState.CONNECTED
        self._set_state(state, detail=f"MeshCore/{transport} - {conn_info}" if conn_info else f"MeshCore/{transport}")
        self.logger.info("MeshCore %s: %s", "reconnected" if reconnected else "connected", conn_info)

    async def _on_disconnected(self, event) -> None:
        """
        DISCONNECTED - library-level disconnect.
        Updates status immediately rather than waiting for next health check.
        """
        p = event.payload or {}
        reason = p.get("reason", "unknown")
        max_exceeded = p.get("max_attempts_exceeded", False)
        if max_exceeded:
            self._set_state(ConnectionState.DISCONNECTED, detail="MeshCore - max retries exceeded")
        else:
            self._set_state(ConnectionState.RECONNECTING, detail=f"MeshCore: {reason}")
        self.logger.warning("MeshCore disconnected: reason=%s max_exceeded=%s", reason, max_exceeded)

    async def _on_device_info(self, event) -> None:
        """
        DEVICE_INFO — response to get_device_info() query.
        Carries firmware version, radio parameters, and device capabilities.
        Updates local_node_info with accurate firmware/radio data.
        """
        p = event.payload or {}
        if not self._local_pubkey:
            return  # SELF_INFO hasn't arrived yet — ignore

        node_id = _pubkey_to_node_id(self._local_pubkey)

        # Extract radio params if present
        radio = {}
        freq   = _get_field(p, "freq", "frequency")
        bw     = _get_field(p, "bw", "bandwidth")
        sf     = _get_field(p, "sf", "spreading_factor")
        cr     = _get_field(p, "cr", "coding_rate")
        tx_pow = _get_field(p, "tx_power", "txPower")
        if freq is not None:
            try: radio["frequency"] = float(freq)
            except (TypeError, ValueError): pass
        if bw is not None:
            try: radio["bandwidth"] = int(bw)
            except (TypeError, ValueError): pass
        if sf is not None:
            try: radio["spreadingFactor"] = int(sf)
            except (TypeError, ValueError): pass
        if cr is not None:
            try: radio["codingRate"] = int(cr)
            except (TypeError, ValueError): pass
        if tx_pow is not None:
            try: radio["txPower"] = int(tx_pow)
            except (TypeError, ValueError): pass

        # Update local_node_info
        fw = _get_field(p, "fw_version", "firmware_version", "fw") or ""
        if fw and self.meshtastic_data.local_node_info:
            self.meshtastic_data.local_node_info["firmware_version"] = fw
        if radio and self.meshtastic_data.local_node_info:
            self.meshtastic_data.local_node_info["meshcore_radio"] = radio

        self.logger.debug("MeshCore device_info: fw=%s radio=%s", fw, radio)

    async def _on_path_update(self, event) -> None:
        """
        PATH_UPDATE — routing path discovered for a contact.
        Translates to a traceroute-like packet so the packet log shows path info.
        """
        p = event.payload or {}
        pubkey = p.get("public_key", "") or p.get("pubkey", "") or ""
        prefix = pubkey[:12].lower() if pubkey else ""
        if not prefix:
            return
        node_id = _pubkey_to_node_id(prefix)
        path    = p.get("path", []) or []
        now     = int(time.time())
        self._enqueue({
            "id":              random.randint(1, 0x7FFFFFFF),
            "fromId":          node_id,
            "toId":            _pubkey_to_node_id(self._local_pubkey) if self._local_pubkey else f"!mc_{self.slot_id}",
            "channel":         0,
            "rxTime":          now,
            "app_packet_type": "Traceroute",
            "decoded": {
                "portnum":    "TRACEROUTE_APP",
                "traceroute": {"route": path},
            },
            "meshcore": True,
        })

    async def _on_battery(self, event) -> None:
        """
        BATTERY — battery level response (from get_bat() command).
        Lightweight telemetry update for our own node.
        """
        p = event.payload or {}
        bat = _get_field(p, "level", "bat", "battery")
        if bat is None:
            return
        node_id = _pubkey_to_node_id(self._local_pubkey) if self._local_pubkey else f"!mc_{self.slot_id}"
        now = int(time.time())
        try:
            self._enqueue({
                "id":              random.randint(1, 0x7FFFFFFF),
                "fromId":          node_id,
                "toId":            "^all",
                "channel":         0,
                "rxTime":          now,
                "app_packet_type": "Telemetry",
                "decoded": {
                    "portnum":   "TELEMETRY_APP",
                    "telemetry": {"deviceMetrics": {"batteryLevel": int(bat)}},
                },
                "meshcore": True,
            })
        except (ValueError, TypeError):
            pass

    async def _on_messages_waiting(self, event) -> None:
        """
        MESSAGES_WAITING — firmware notification that queued messages are available.
        The library's auto-fetching handles retrieval; we log it for debugging.
        """
        self.logger.debug("MeshCore [%s]: MESSAGES_WAITING notification received", self.slot_id)

    async def _on_packet_stats(self, event) -> None:
        """
        STATS_RESPONSE — packet statistics from get_packet_stats() / get_stats().
        Translates to a Telemetry packet so the analytics panel shows TX/RX counts.
        Fields: rx_total, tx_total, rx_flood, tx_flood, rx_direct, tx_direct,
                recv_errors (newer firmware, 30-byte frame only).
        """
        p = event.payload or {}
        if not p:
            return
        node_id = _pubkey_to_node_id(self._local_pubkey) if self._local_pubkey else f"!mc_{self.slot_id}"
        now     = int(time.time())

        metrics: Dict[str, Any] = {}
        for key in ("rx_total", "tx_total", "rx_flood", "tx_flood",
                    "rx_direct", "tx_direct", "recv_errors"):
            val = p.get(key)
            if val is not None:
                try: metrics[key] = int(val)
                except (TypeError, ValueError): pass

        if not metrics:
            return

        self._enqueue({
            "id":              random.randint(1, 0x7FFFFFFF),
            "fromId":          node_id,
            "toId":            "^all",
            "channel":         0,
            "rxTime":          now,
            "app_packet_type": "Telemetry",
            "decoded": {
                "portnum":   "TELEMETRY_APP",
                "telemetry": {"deviceMetrics": metrics},
            },
            "meshcore": True,
        })
        self.logger.debug("MeshCore packet stats: %s", metrics)

    # ── Subscribe / unsubscribe all events ────────────────────────────────────

    def _subscribe_events(self, mc: "MeshCore") -> None:
        """
        Attach all event handlers to a live MeshCore instance.

        Subscribed events:
          SELF_INFO          — own node identity on appstart
          ADVERTISEMENT      — other node discovered (two packets: NodeInfo + Position)
          NEW_CONTACT        — contact added to device list
          CONTACT_MSG_RECV   — direct message received
          CHANNEL_MSG_RECV   — channel broadcast received
          ACK                — DM delivery confirmation
          RX_LOG_DATA        — per-hop SNR/RSSI for received packets
          TELEMETRY_RESPONSE — device telemetry (battery, voltage, uptime, noise floor)
          STATUS_RESPONSE    — alias for telemetry on some firmware versions
          BATTERY            — lightweight battery level event
          PATH_UPDATE        — routing path discovery
          MESSAGES_WAITING   — firmware notification of queued messages
          CONNECTED          — library-level connection established/reconnected
          DISCONNECTED       — library-level disconnection
          DEVICE_INFO        — firmware version, radio parameters
          TRACE_DATA         — per-hop trace data (try: may not exist in all versions)
          STATS_RESPONSE     — packet statistics (try: may not exist in all versions)
        """
        mc.subscribe(EventType.SELF_INFO,          self._on_self_info)
        mc.subscribe(EventType.ADVERTISEMENT,      self._on_advertisement)
        mc.subscribe(EventType.NEW_CONTACT,        self._on_new_contact)
        mc.subscribe(EventType.CONTACT_MSG_RECV,   self._on_contact_msg)
        mc.subscribe(EventType.CHANNEL_MSG_RECV,   self._on_channel_msg)
        mc.subscribe(EventType.ACK,                self._on_ack)
        mc.subscribe(EventType.RX_LOG_DATA,        self._on_rx_log)
        mc.subscribe(EventType.TELEMETRY_RESPONSE, self._on_telemetry)
        mc.subscribe(EventType.STATUS_RESPONSE,    self._on_telemetry)
        mc.subscribe(EventType.BATTERY,            self._on_battery)
        mc.subscribe(EventType.PATH_UPDATE,        self._on_path_update)
        mc.subscribe(EventType.MESSAGES_WAITING,   self._on_messages_waiting)
        mc.subscribe(EventType.CONNECTED,          self._on_connected)
        mc.subscribe(EventType.DISCONNECTED,       self._on_disconnected)
        mc.subscribe(EventType.DEVICE_INFO,        self._on_device_info)
        # Optional events — may not exist in all library/firmware versions
        for ev_name in ("TRACE_DATA", "STATS_RESPONSE"):
            try:
                mc.subscribe(getattr(EventType, ev_name),
                             self._on_packet_stats if ev_name == "STATS_RESPONSE"
                             else self._on_path_update)
            except Exception:
                pass

    # ── sendText ─────────────────────────────────────────────────────────────

    async def sendText(
        self,
        text: str,
        destinationId: str = "^all",
        channelIndex: int = 0,
        wantAck: bool = False,
    ):
        """
        Send a text message via MeshCore.

        destinationId:
          "^all" or falsy  → channel broadcast on channelIndex
          "!hexstring"     → DM to the contact whose pubkey prefix matches

        wantAck: Request acknowledgement from the destination. For DMs this
          causes the radio to relay the packet over RF. Note: the meshcore
          library's send_msg/send_chan_msg do not accept a wantAck parameter
          at the API level — the flag is encoded in the packet header bytes
          by the library; if the library version in use does not support it,
          wantAck=True is silently ignored.

        Returns {"id": packet_id} on success so send_msg can store mesh_packet_id
        for ACK tracking.  Returns None on failure.
        """
        if not _HAS_MESHCORE:
            return None
        # Capture mc once under the lock to prevent race with session teardown
        mc = self._mc
        if not mc or not self.is_ready.is_set():
            self.logger.error("❌ MeshCore sendText: not connected")
            return None

        # Expire stale pending ACKs (older than 5 minutes) to prevent unbounded growth
        now = time.time()
        async with self._ack_lock:
            stale_acks = [k for k, (_, ts) in self._pending_acks.items() if now - ts > 300]
            for k in stale_acks:
                del self._pending_acks[k]

        async with self._send_lock:
            try:
                is_broadcast = (not destinationId or destinationId == "^all")

                if is_broadcast:
                    result = await mc.commands.send_chan_msg(channelIndex, text)
                    if result.type == EventType.ERROR:
                        self.logger.error("❌ MeshCore send_chan_msg error: %s", result.payload)
                        return None
                    pkt_id = random.randint(1, 0x7FFFFFFF)
                    self.logger.info("📤 MeshCore broadcast → ch=%d msg='%s'", channelIndex, text[:60])
                    return {"id": pkt_id}

                else:
                    # DM — find contact by pubkey prefix
                    prefix = _node_id_to_pubkey_prefix(destinationId)

                    # Try library's live contact list first (most up to date)
                    contact = mc.get_contact_by_key_prefix(prefix)

                    # Fallback: our own cache (populated from advertisements + get_contacts)
                    if contact is None:
                        contact = self._contacts.get(prefix)

                    # Last resort: try alternate key formats the library may use
                    if contact is None:
                        for ck, cv in self._contacts.items():
                            pk = (cv.get("public_key") or cv.get("pubkey") or "")[:12].lower()
                            if pk == prefix:
                                contact = cv
                                break

                    if contact is None:
                        self.logger.error(
                            "❌ MeshCore sendText: no contact found for %s (prefix=%s). "
                            "The node must have been seen via advertisement or contacts list before a DM can be sent.",
                            destinationId, prefix
                        )
                        return None

                    result = await mc.commands.send_msg(contact, text)
                    if result.type == EventType.ERROR:
                        self.logger.error("❌ MeshCore send_msg error: %s", result.payload)
                        return None

                    pkt_id   = random.randint(1, 0x7FFFFFFF)
                    ack_code = None
                    try:
                        raw_ack = result.payload.get("expected_ack")
                        if isinstance(raw_ack, (bytes, bytearray)):
                            ack_code = raw_ack.hex().lower()
                        elif isinstance(raw_ack, str):
                            ack_code = raw_ack.lower()
                    except (AttributeError, TypeError):
                        pass

                    if ack_code:
                        async with self._ack_lock:
                            # Cap at 500; value is now (pkt_id, timestamp) for TTL
                            if len(self._pending_acks) >= 500:
                                try:
                                    del self._pending_acks[next(iter(self._pending_acks))]
                                except (StopIteration, KeyError):
                                    pass
                            self._pending_acks[ack_code] = (pkt_id, time.time())
                        self.logger.debug("MeshCore DM queued: pkt_id=%d ack_code=%s", pkt_id, ack_code)

                    self.logger.info(
                        "📤 MeshCore DM → dest=%s msg='%s' ack=%s",
                        destinationId, text[:60], ack_code or "none"
                    )
                    return {"id": pkt_id}

            except Exception as e:
                self.logger.warning("MeshCore sendText error: %s", e)
                return None

    # ── Shutdown / Disconnect ──────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Full teardown — used for slot deletion or server shutdown.
        Stops the connect_loop and disconnects MeshCore. The slot is dead after this."""
        self.logger.info("🛑 MeshCore shutdown requested.")
        self._stop_event.set()
        self.is_ready.clear()
        mc = self._mc
        self._mc = None
        if mc is not None:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=self.SHUTDOWN_TIMEOUT)
            except Exception:
                pass
        self._set_state(ConnectionState.DISCONNECTED, detail="Shutdown")

    async def disconnect(self) -> None:
        """User-initiated disconnect — closes MeshCore but keeps connect_loop alive.

        Unlike shutdown(), this does NOT set _stop_event. The connect_loop continues
        running and will park in DISCONNECTED state until force_reconnect() is called.
        """
        self.logger.info("🔌 MeshCore user disconnect requested — closing client, keeping connect_loop alive.")
        self._user_disconnected = True
        self.is_ready.clear()
        mc = self._mc
        self._mc = None
        if mc is not None:
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=self.SHUTDOWN_TIMEOUT)
            except Exception:
                pass
        self._set_state(ConnectionState.DISCONNECTED, detail="Disconnected by user")

    async def disconnect_for_restart(self, settle_seconds: float = 2.0) -> None:
        await self.shutdown()
        await asyncio.sleep(settle_seconds)

    async def _refresh_contacts(self, mc: "MeshCore") -> None:
        """
        Fetch the full contact list from the device and update the local cache.
        Handles both 'public_key' and 'pubkey' field names across library versions.
        Called on connect and periodically every ~5 minutes.
        """
        try:
            result = await mc.commands.get_contacts()
            if result.type == EventType.ERROR:
                self.logger.debug("MeshCore get_contacts error: %s", result.payload)
                return
            contacts = result.payload or {}
            loaded = 0
            # Evict stale contacts (>10 minutes old) before refreshing
            now = time.time()
            stale_keys = [k for k, v in self._contacts.items()
                          if now - v.get("_last_seen", 0) > 600]
            for k in stale_keys:
                del self._contacts[k]
            if stale_keys:
                self.logger.debug("MeshCore: evicted %d stale contacts", len(stale_keys))

            for ck, cv in contacts.items():
                if not isinstance(cv, dict):
                    continue
                # Handle both key name variants across library versions
                pk = (cv.get("public_key") or cv.get("pubkey") or "")[:12].lower()
                if pk:
                    cv["_last_seen"] = now
                    self._contacts[pk] = cv
                    loaded += 1
            self.logger.info("MeshCore: loaded/refreshed %d contacts (%d cached)", loaded, len(self._contacts))
        except Exception as e:
            self.logger.warning("MeshCore contacts refresh: %s", e)

    # ── Main connection loop ──────────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> None:
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

    async def connect_loop(self) -> None:
        if not _HAS_MESHCORE:
            self.logger.error("meshcore library not installed - slot cannot start")
            self._set_state(ConnectionState.DISCONNECTED, detail="pip install meshcore")
            return

        self._loop = asyncio.get_running_loop()
        self._set_state(ConnectionState.IDLE)
        transport  = self.config.get("MESHCORE_TRANSPORT", "serial").lower().strip()
        self.logger.info("MeshCore Connection Manager v1.1 (State Machine) starting - transport=%s slot=%s",
                         transport, self.slot_id)

        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            try:
                await self._run_session()
                attempt = 0   # session ended cleanly
            except asyncio.CancelledError:
                self.logger.info("MeshCore connect_loop cancelled.")
                break
            except Exception as e:
                self.logger.error("MeshCore session error (attempt %d): %s", attempt, e)

            if self._stop_event.is_set():
                break

            # If user explicitly disconnected, park here until force_reconnect
            if self._user_disconnected:
                self.logger.debug("Parking in DISCONNECTED state — waiting for user reconnect.")
                await self._interruptible_sleep(5.0)
                attempt = 0  # Don't count parking as a failed attempt
                continue

            # Check max retry cap
            if attempt >= self.MAX_RECONNECT_ATTEMPTS:
                self._set_state(
                    ConnectionState.DISCONNECTED,
                    detail=f"Max retries ({self.MAX_RECONNECT_ATTEMPTS}) reached - click Reconnect to retry"
                )
                self.logger.error("Max MeshCore reconnect attempts (%d) reached - giving up.", self.MAX_RECONNECT_ATTEMPTS)
                await self._interruptible_sleep(60.0)
                continue

            delay = min(self.RECONNECT_DELAY_MIN * (2 ** min(attempt - 1, 4)),
                        self.RECONNECT_DELAY_MAX)
            self.logger.info("MeshCore reconnecting in %.1fs...", delay)
            self._set_state(
                ConnectionState.RECONNECTING,
                detail=f"attempt {attempt}, next in {delay:.0f}s"
            )
            await self._interruptible_sleep(delay)

        self.logger.info("MeshCore connect_loop exited cleanly.")

    async def _run_session(self) -> None:
        """
        Create a MeshCore instance, connect, run until stop_event fires or
        the connection drops, then clean up.
        """
        transport = self.config.get("MESHCORE_TRANSPORT", "serial").lower().strip()
        self._set_state(ConnectionState.CONNECTING, detail=f"MeshCore/{transport}")

        mc: Optional["MeshCore"] = None
        try:
            mc = await asyncio.wait_for(
                self._create_meshcore_instance(transport),
                timeout=self.CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"MeshCore connect() timed out after {self.CONNECT_TIMEOUT:.0f}s "
                f"(transport={transport})"
            )

        self._mc = mc
        self._local_pubkey = ""   # reset — will be populated by SELF_INFO
        self._local_name   = ""
        self._subscribe_events(mc)

        # Enable auto-fetching so queued messages arrive as events
        await mc.start_auto_message_fetching()

        # Trigger appstart to get SELF_INFO
        try:
            await mc.commands.send_appstart()
        except Exception as e:
            self.logger.warning("MeshCore appstart warning: %s", e)

        # Fetch existing contacts to populate the contacts cache
        await self._refresh_contacts(mc)

        # Fetch channel info (indices 0-7) so the channels sidebar shows real names.
        self._channel_info = {}
        consecutive_errors = 0
        for ch_idx in range(8):
            try:
                ch_result = await mc.commands.get_channel(ch_idx)
                if ch_result.type == EventType.ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 2:
                        break  # 2 consecutive errors = no more channels on this device
                    continue
                consecutive_errors = 0
                if ch_result.payload:
                    self._channel_info[ch_idx] = ch_result.payload
                    self.logger.debug("MeshCore channel %d: %s", ch_idx,
                                     ch_result.payload.get("name", "?"))
            except Exception as e:
                self.logger.debug("MeshCore get_channel(%d) exception: %s", ch_idx, e)
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    break

        # Request own telemetry so battery/uptime appear immediately on connect
        try:
            await mc.commands.get_self_telemetry()
        except Exception as e:
            self.logger.debug("MeshCore self-telemetry request: %s", e)

        # Query device info for firmware version and radio parameters
        try:
            await mc.commands.get_device_info()
        except Exception as e:
            self.logger.debug("MeshCore get_device_info: %s", e)

        # Query packet statistics for analytics panel
        try:
            await mc.commands.get_packet_stats()
        except Exception as e:
            try:
                await mc.commands.get_stats()  # alternate name in some firmware versions
            except Exception:
                pass

        # Announce presence on the mesh so other nodes can discover us and
        # add us to their contact list — directly improves DM reachability.
        try:
            await mc.commands.send_advert(flood=False)  # local only on connect
            self.logger.info("📡 MeshCore: sent local advertisement")
        except Exception as e:
            self.logger.debug("MeshCore send_advert: %s", e)

        # Signal ready
        self.is_ready.set()
        self._set_state(ConnectionState.CONNECTED, detail=f"MeshCore/{transport}")
        self.logger.info("MeshCore connected - slot=%s transport=%s", self.slot_id, transport)

        # Fire on_connection callback so the dashboard runs its sync logic
        if self._on_connection_cb:
            try:
                self._on_connection_cb(self, topic="meshtastic.connection.established")
            except Exception as e:
                self.logger.warning("on_connection callback error: %s", e)

        # Health loop — run until stop_event or disconnect
        _telemetry_tick = 0
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)
                if self._stop_event.is_set():
                    break
                if not mc.is_connected:
                    self.logger.warning("MeshCore [%s]: device disconnected", self.slot_id)
                    break
                self._set_state(ConnectionState.CONNECTED, detail=f"MeshCore/{transport}")
                _telemetry_tick += 1

                # Refresh telemetry every 2nd tick (~60s)
                if _telemetry_tick % 2 == 0:
                    try:
                        await mc.commands.get_self_telemetry()
                    except Exception:
                        pass

                # Refresh contacts every 10th tick (~5min)
                if _telemetry_tick % 10 == 0:
                    await self._refresh_contacts(mc)

                # Flood advertisement every 20th tick (~10min) so remote nodes
                # keep us in their contact list and can DM us.
                if _telemetry_tick % 20 == 0:
                    try:
                        await mc.commands.send_advert(flood=True)
                        self.logger.debug("MeshCore: sent periodic flood advertisement")
                    except Exception:
                        pass
                    try:
                        await mc.commands.get_packet_stats()
                    except Exception:
                        try:
                            await mc.commands.get_stats()
                        except Exception:
                            pass

                # Fallback get_msg() poll — workaround for firmware versions where
                # CHANNEL_MSG_RECV events do not fire (nRF52840 Companion USB v1.11.x,
                # github.com/meshcore-dev/MeshCore/issues/1232).
                # auto_message_fetching handles this internally, but a manual poll
                # every health tick ensures nothing is silently dropped.
                try:
                    result = await mc.commands.get_msg()
                    if result and hasattr(result, "type") and result.type != EventType.ERROR:
                        if result.payload:
                            self.logger.debug("MeshCore get_msg() fallback: type=%s", result.type)
                except Exception:
                    pass

        except asyncio.CancelledError:
            pass
        finally:
            self.is_ready.clear()
            self._mc = None
            try:
                await mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await asyncio.wait_for(mc.disconnect(), timeout=self.SHUTDOWN_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
            self.logger.info("🔌 MeshCore session ended [slot=%s]", self.slot_id)

    async def _create_meshcore_instance(self, transport: str) -> "MeshCore":
        """Factory — creates and returns a connected MeshCore instance."""
        if transport == "serial":
            port = self.config.get("MESHCORE_SERIAL_PORT", "/dev/ttyUSB0").strip()
            try:
                baud = int(self.config.get("MESHCORE_BAUD", "115200") or "115200")
            except (ValueError, TypeError):
                baud = 115200
            self.logger.info("📟 MeshCore serial: %s @ %d", port, baud)
            return await MeshCore.create_serial(port, baud)

        elif transport == "tcp":
            host = self.config.get("MESHCORE_HOST", "192.168.1.100").strip()
            try:
                port = int(self.config.get("MESHCORE_PORT", "4000") or "4000")
            except (ValueError, TypeError):
                port = 4000
            self.logger.info("🌐 MeshCore TCP: %s:%d", host, port)
            return await MeshCore.create_tcp(host, port)

        elif transport == "ble":
            mac = self.config.get("MESHCORE_BLE_MAC", "").strip()
            pin = self.config.get("MESHCORE_BLE_PIN", "").strip() or None
            if not mac:
                self._set_state(ConnectionState.DISCONNECTED, detail="BLE MAC not configured")
                raise ValueError("BLE MAC not configured")
            self.logger.info("MeshCore BLE: %s pin=%s", mac, "set" if pin else "none")
            return await MeshCore.create_ble(mac, pin=pin)

        raise ValueError(f"Unknown MeshCore transport: '{transport}'. Use 'serial', 'tcp', or 'ble'.")

    # ── Duck-typing compatibility ──────────────────────────────────────────────

    @property
    def my_node_id(self) -> Optional[int]:
        """meshtastic_dashboard checks this; MeshCore has no numeric node num."""
        return None

    @property
    def nodes(self) -> dict:
        return self.meshtastic_data.nodes

    def request_reboot(self) -> None:
        self.logger.warning("request_reboot() not directly supported for MeshCore via this manager.")

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None