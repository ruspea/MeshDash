"""
MeshtasticConnectionManager v8.1 — Hardened Connection Lifecycle Manager
"""

from core.routes.schemas import User
from pubsub import pub
import asyncio
import logging
import os
import random
import socket
import time
from typing import Any, Callable, Dict, List, Optional

import meshtastic
import meshtastic.serial_interface
from core.connections import ConnectionState
import meshtastic.tcp_interface
import meshtastic.util
from pubsub import pub

HAS_BLE = False
try:
    import meshtastic.ble_interface
    from bleak import BleakScanner
    HAS_BLE = True
except ImportError:
    pass

TOPIC_RECEIVED = "meshtastic.receive"
TOPIC_SENT = "meshtastic.sent"
TOPIC_CONNECTION_ESTABLISHED = "meshtastic.connection.established"
TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"
TOPIC_NODE_UPDATED = "meshtastic.node.updated"

try:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _SCRIPT_DIR = os.getcwd()

CONFIG_FILE_PATH = os.path.join(_SCRIPT_DIR, ".mesh-dash_config")


class MeshtasticConnectionManager:
    HEALTH_CHECK_INTERVAL: float = 20.0
    PROBE_TIMEOUT: float = 8.0
    MAX_STRIKES: int = 3
    MAX_RECONNECT_ATTEMPTS: int = 50
    RECONNECT_COOLDOWN: float = 3.0
    BASE_BACKOFF: float = 2.0
    MAX_BACKOFF: float = 15.0
    BACKOFF_JITTER: float = 1.0
    TRANSPORT_CHECK_TIMEOUT: float = 4.0
    INTERFACE_INIT_TIMEOUT: float = 25.0
    INITIAL_MYINFO_GRACE_SECONDS: float = 30.0
    DISCONNECTED_POLL_INTERVAL: float = 5.0
    TCP_KEEPALIVE_IDLE: int = 10
    TCP_KEEPALIVE_INTERVAL: int = 5
    TCP_KEEPALIVE_COUNT: int = 3

    def __init__(
        self,
        meshtastic_data,
        logger: Optional[logging.Logger] = None,
        connection_params: Optional[Dict[str, Any]] = None,
        slot_id: str = "node_0",
    ):
        self.meshtastic_data = meshtastic_data
        self.logger = logger or logging.getLogger(f"MeshConnection.{slot_id}")
        self.slot_id = slot_id
        self._connection_params = connection_params
        self.interface = None
        self._interface_ref = [None]
        self._send_lock = asyncio.Lock()
        self.is_ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._failure_strikes: int = 0
        self._consecutive_reconnect_failures: int = 0
        self._last_successful_probe: float = 0.0
        self._user_disconnected: bool = False  # True when user explicitly disconnected; cleared by force_reconnect
        self._wake_event: asyncio.Event = asyncio.Event()  # Set by force_reconnect to wake connect_loop
        self._config_mtime: float = 0.0
        self._active_transport: Optional[str] = None
        self._is_first_run: bool = True
        self._state: ConnectionState = ConnectionState.IDLE
        self._interface_up_at: float | None = None
        self._on_receive_cb: Optional[Callable] = None
        self._on_connection_cb: Optional[Callable] = None
        self._on_node_updated_cb: Optional[Callable] = None
        self._filtered_receive_cb: Optional[Callable] = None
        self._filtered_node_updated_cb: Optional[Callable] = None
        self._filtered_disconnect_cb: Optional[Callable] = None
        # Connection metrics
        self._connect_count: int = 0
        self._disconnect_count: int = 0
        self._last_connected_at: Optional[float] = None
        self._last_disconnected_at: Optional[float] = None
        self._last_disconnect_reason: str = ""
        self._total_uptime: float = 0.0
        self._current_connected_since: Optional[float] = None
        self._latency_samples: List[float] = []
        self._health_check_results: List[dict] = []
        self.config: Dict[str, Any] = {
            "MESHTASTIC_CONNECTION_TYPE": "SERIAL",
            "MESHTASTIC_HOST": "192.168.1.50",
            "MESHTASTIC_PORT": "4403",
            "MESHTASTIC_SERIAL_PORT": "/dev/ttyACM0",
            "MESHTASTIC_BLE_MAC": "",
        }
        if connection_params:
            for k, v in connection_params.items():
                if k in self.config:
                    self.config[k] = v

    def register_callbacks(self, on_receive: Callable, on_connection: Callable, on_node_updated: Callable) -> None:
        self._on_receive_cb = on_receive
        self._on_connection_cb = on_connection
        self._on_node_updated_cb = on_node_updated

        iface_ref = self._interface_ref

        if on_receive:
            def _filtered_receive(packet, interface=None):
                if interface is iface_ref[0]:
                    on_receive(packet, interface)
            self._filtered_receive_cb = _filtered_receive

        if on_node_updated:
            def _filtered_node_updated(node, interface=None):
                if interface is iface_ref[0]:
                    on_node_updated(node, interface)
            self._filtered_node_updated_cb = _filtered_node_updated

    async def sendText(self, text: str, destinationId: str = "^all", channelIndex: int = 0, wantAck: bool = False):
        if not self.interface:
            self.logger.error(f"❌ SEND FAILED — no active interface. Message '{text[:40]}' dropped.")
            return None
        if not self.is_ready.is_set():
            self.logger.warning("⚠️  SEND SKIPPED — interface not ready yet.")
            return None
        async with self._send_lock:
            try:
                self.logger.info(f"📤 Sending → dest={destinationId} ch={channelIndex} ack={wantAck} msg='{text[:60]}'")
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.interface.sendText, text,
                        destinationId=destinationId,
                        channelIndex=channelIndex,
                        wantAck=wantAck,
                    ),
                    timeout=10.0,
                )
                pub.sendMessage(TOPIC_SENT, interface=self.interface, packet=result)
                return result
            except asyncio.TimeoutError:
                self.logger.error("❌ SEND TIMEOUT: Radio not responding (10s).")
                self._failure_strikes = self.MAX_STRIKES
                return None
            except Exception as exc:
                self.logger.error(f"❌ sendText error: {exc}")
                return None

    def request_reboot(self) -> None:
        if self.interface:
            try:
                self.interface.reboot()
                self.logger.info("🔄 Reboot command sent.")
            except Exception as exc:
                self.logger.warning(f"Reboot failed: {exc}")

    async def shutdown(self) -> None:
        """Full teardown — used for slot deletion or server shutdown.
        Stops the connect_loop and closes the interface. The slot is dead after this."""
        self.logger.info("🛑 Shutdown requested.")
        self._stop_event.set()
        self.is_ready.clear()
        await self._close_interface("Shutdown")

    async def disconnect(self) -> None:
        """User-initiated disconnect — closes the interface but keeps the connect_loop alive.

        Unlike shutdown(), this does NOT set _stop_event, so the connect_loop continues
        running and will park in DISCONNECTED state. The user can then call
        force_reconnect() to resume the connection.
        """
        self.logger.info("🔌 User disconnect requested — closing interface, keeping connect_loop alive.")
        self._consecutive_reconnect_failures = 0
        self._failure_strikes = 0
        self._user_disconnected = True
        self.is_ready.clear()
        await self._close_interface("User disconnect")
        self._set_state(ConnectionState.DISCONNECTED, detail="Disconnected by user")

    async def disconnect_for_restart(self, settle_seconds: float = 3.0) -> None:
        """
        Cleanly disconnect the radio before the process restarts.
        Signals stop so connect_loop exits, closes the interface, then waits
        for the serial port / TCP socket to be fully released by the OS before
        returning. The caller should then do os.execv() or os._exit().
        """
        self.logger.info(f"🔌 disconnect_for_restart(): closing interface, settling {settle_seconds}s...")
        self._stop_event.set()
        self.is_ready.clear()
        await self._close_interface("Pre-restart disconnect")
        await asyncio.sleep(settle_seconds)
        self.logger.info("✅ disconnect_for_restart(): port released, safe to restart.")

    @property
    def my_node_id(self) -> Optional[int]:
        if self.interface and getattr(self.interface, "myInfo", None):
            return self.interface.myInfo.my_node_num
        return None

    @property
    def nodes(self) -> dict:
        if self.interface:
            return getattr(self.interface, "nodes", {})
        return {}

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        """Transition to a new connection state, enforcing valid transitions."""
        prev = self._state
        from core.connections import is_valid_transition
        if not is_valid_transition(prev, state):
            self.logger.warning(f"Invalid state transition: {prev.value} -> {state.value} (detail={detail})")
        self._state = state
        transport = self.config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL").upper()
        if transport == "MESHCORE":
            transport = self.config.get("MESHCORE_TRANSPORT", "serial").upper()
        self.meshtastic_data.set_connection_state(state, detail=detail, transport=transport)

    async def force_reconnect(self) -> None:
        """Reset retry counter and transition back to CONNECTING from DISCONNECTED.

        Called when user clicks Reconnect. Clears the _user_disconnected flag so
        the connect_loop resumes connection attempts. Wakes the connect_loop
        immediately via _wake_event.
        """
        self.logger.info("🔄 force_reconnect() called — resetting retry counter.")
        self._consecutive_reconnect_failures = 0
        self._failure_strikes = 0
        self._user_disconnected = False
        self._stop_event.clear()
        self._set_state(ConnectionState.CONNECTING, detail="User requested reconnect")
        self._wake_event.set()  # Wake the connect_loop immediately

    async def connect_loop(self) -> None:
        self.logger.info("🚀 Connection Manager v8.2 (State Machine) started.")
        self._set_state(ConnectionState.IDLE)

        if self._is_first_run:
            self._is_first_run = False
            self.logger.info("⏳ Boot grace period: 5s (allowing node to finish booting before first connect)...")
            self._set_state(ConnectionState.DEGRADED, detail="Boot grace")
            await self._interruptible_sleep(5.0)
        while not self._stop_event.is_set():
            config_changed = self._load_config()
            if config_changed and self.interface is not None:
                self.logger.info("🔄 Config change detected while connected — recycling connection.")
                await self._close_interface("Config changed")

            transport = self.config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL").upper()

            # ── WEBSERIAL mode — browser owns the radio, server does nothing ──
            if transport == "WEBSERIAL":
                if self.interface is not None:
                    await self._close_interface("Switched to WEBSERIAL mode")
                self._set_state(ConnectionState.WEBSERIAL)
                self.is_ready.set()   # mark ready so API endpoints work
                await self._interruptible_sleep(30.0)   # idle loop — just stay alive
                continue
            # ── End WEBSERIAL block ──────────────────────────────────────────

            # ── MQTT mode — MQTTConnectionManager owns its own connect_loop ──
            if transport == "MQTT":
                if self.interface is not None:
                    await self._close_interface("Switched to MQTT mode")
                self._set_state(ConnectionState.MQTT)
                self.is_ready.set()
                await self._interruptible_sleep(30.0)
                continue
            # ── End MQTT block ───────────────────────────────────────────────

            if self.interface is None:
                # If user explicitly disconnected, park here until force_reconnect
                if self._user_disconnected:
                    self.logger.debug("Parking in DISCONNECTED state — waiting for user reconnect.")
                    await self._interruptible_sleep(5.0)
                    continue

                # Check max retry cap
                if self._consecutive_reconnect_failures >= self.MAX_RECONNECT_ATTEMPTS:
                    self._set_state(
                        ConnectionState.DISCONNECTED,
                        detail=f"Max retries ({self.MAX_RECONNECT_ATTEMPTS}) reached — click Reconnect to retry"
                    )
                    self.logger.error(f"❌ Max reconnect attempts ({self.MAX_RECONNECT_ATTEMPTS}) reached — giving up.")
                    # Sleep in DISCONNECTED state, wake on force_reconnect or stop
                    await self._interruptible_sleep(60.0)
                    continue

                self._set_state(ConnectionState.RECONNECTING, detail=f"attempt {self._consecutive_reconnect_failures + 1}")
                success = await self._attempt_connection(transport)
                if success:
                    self._consecutive_reconnect_failures = 0
                    self._failure_strikes = 0
                    self._last_successful_probe = time.time()
                    self._set_state(ConnectionState.DEGRADED, detail="STREAM OPEN")
                    self.logger.info("✅ Connection established — entering monitoring.")
                    continue
                else:
                    self._consecutive_reconnect_failures += 1
                    backoff = self._calculate_backoff(self._consecutive_reconnect_failures)
                    self.logger.info(
                        f"⏳ Reconnect attempt {self._consecutive_reconnect_failures} failed. Retry in {backoff:.1f}s..."
                    )
                    self._set_state(
                        ConnectionState.RECONNECTING,
                        detail=f"attempt {self._consecutive_reconnect_failures}, next in {backoff:.0f}s"
                    )
                    await self._interruptible_sleep(backoff)
                    continue

            is_alive = await self._health_check(transport)
            if is_alive:
                if self._failure_strikes > 0:
                    self.logger.info(f"✅ Recovered after {self._failure_strikes} strike(s).")
                self._failure_strikes = 0
                self._last_successful_probe = time.time()
                if not self.is_ready.is_set():
                    self.is_ready.set()
                # Grace period: transport alive but myInfo not yet populated
                if self._interface_up_at is not None:
                    elapsed = time.time() - self._interface_up_at
                    in_grace = elapsed < self.INITIAL_MYINFO_GRACE_SECONDS
                else:
                    in_grace = False
                if in_grace and getattr(self.interface, "myInfo", None) is None:
                    self._set_state(ConnectionState.DEGRADED, detail="STREAM OPEN")
                else:
                    host_info = self._get_host_detail()
                    self._set_state(ConnectionState.CONNECTED, detail=host_info)
                await self._interruptible_sleep(self.HEALTH_CHECK_INTERVAL)
            else:
                self._failure_strikes += 1
                self.logger.warning(f"⚠️  Health check FAILED — strike {self._failure_strikes}/{self.MAX_STRIKES}")
                if self._failure_strikes >= self.MAX_STRIKES:
                    self.logger.error("❌ Max strikes reached — tearing down connection.")
                    await self._close_interface("Health check failed")
                    await self._interruptible_sleep(self.RECONNECT_COOLDOWN)
                else:
                    await self._interruptible_sleep(2.0)

    def _get_host_detail(self) -> str:
        """Return a human-readable host/transport detail string for state updates."""
        transport = self.config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL").upper()
        if transport == "TCP":
            host = self.config.get("MESHTASTIC_HOST", "")
            port = self.config.get("MESHTASTIC_PORT", "")
            return f"TCP {host}:{port}"
        elif transport == "SERIAL":
            port = self.config.get("MESHTASTIC_SERIAL_PORT", "/dev/ttyACM0")
            return f"Serial {port}"
        elif transport == "BLE":
            mac = self.config.get("MESHTASTIC_BLE_MAC", "")
            return f"BLE {mac}" if mac else "BLE"
        return transport

    async def _health_check(self, transport: str) -> bool:
        if not self.interface:
            return False
        try:
            check_start = time.time()
            transport_ok = await self._transport_alive_check(transport)
            if not transport_ok:
                self.logger.warning("💔 Layer 1: Transport-level check failed.")
                # Record failed health check
                self._health_check_results.append({
                    "ts": time.time(),
                    "alive": False,
                    "transport": transport,
                    "latency_ms": 0.0,
                })
                if len(self._health_check_results) > 50:
                    self._health_check_results.pop(0)
                return False
            probe_ok = await self._meshtastic_probe()
            probe_time = (time.time() - check_start) * 1000
            # Record health check result (probe already stored latency in _latency_samples)
            self._health_check_results.append({
                "ts": time.time(),
                "alive": probe_ok,
                "transport": transport,
                "latency_ms": probe_time,
            })
            if len(self._health_check_results) > 50:
                self._health_check_results.pop(0)
            if not probe_ok:
                self.logger.warning("💔 Layer 2: Meshtastic probe failed.")
                return False
            return True
        except Exception as e:
            self.logger.warning(f"💔 Health check exception: {e}")
            return False

    async def _transport_alive_check(self, transport: str) -> bool:
        try:
            if transport == "TCP":
                return await self._tcp_socket_alive()
            elif transport == "SERIAL":
                return await self._serial_device_alive()
            elif transport == "BLE":
                return await self._ble_gatt_alive()
            else:
                return True
        except Exception as e:
            self.logger.debug(f"Transport check error: {e}")
            return False

    async def _tcp_socket_alive(self) -> bool:
        """
        Locate the raw socket from TCPInterface and verify it is still connected.

        Modern meshtastic TCPInterface (>=2.3) stores the connection in:
          interface.stream  (a meshtastic.tcp_interface.TCPInterface._stream or similar)
          interface._socket
          interface.socket

        We walk several known attribute paths and fall back gracefully if none found.
        """
        def _check():
            try:
                sock = self._find_tcp_socket()
                if sock is None:
                    # Cannot locate socket — defer decision to Layer 2
                    self.logger.debug("TCP health: Could not locate raw socket, deferring to meshtastic probe.")
                    return True

                # Check 1: peer still reachable?
                try:
                    sock.getpeername()
                except (OSError, socket.error) as e:
                    self.logger.debug(f"TCP health: getpeername() failed — {e}")
                    return False

                # Check 2: OS-level socket error flag
                try:
                    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if err != 0:
                        self.logger.debug(f"TCP health: SO_ERROR={err}")
                        return False
                except Exception:
                    pass

                return True
            except Exception as e:
                self.logger.debug(f"TCP socket check error: {e}")
                return False

        try:
            return await asyncio.wait_for(asyncio.to_thread(_check), timeout=3.0)
        except asyncio.TimeoutError:
            self.logger.debug("TCP socket check timed out.")
            return False

    def _find_tcp_socket(self):
        """
        Walk all known attribute paths used by various meshtastic-python versions
        to store the underlying TCP socket. Returns the socket or None.
        """
        iface = self.interface
        if iface is None:
            return None

        # Direct socket attributes
        for attr in ("_socket", "socket"):
            candidate = getattr(iface, attr, None)
            if candidate is not None and hasattr(candidate, "getsockopt"):
                return candidate

        # stream-based storage (meshtastic >= 2.3)
        for stream_attr in ("stream", "_stream", "_tcp_stream", "conn", "_conn"):
            stream = getattr(iface, stream_attr, None)
            if stream is None:
                continue
            # asyncio StreamWriter wraps a transport
            if hasattr(stream, "_transport"):
                transport = stream._transport
                if hasattr(transport, "get_extra_info"):
                    sock = transport.get_extra_info("socket")
                    if sock is not None:
                        return sock
            # Direct socket on stream
            for sub_attr in ("_socket", "socket", "_sock"):
                candidate = getattr(stream, sub_attr, None)
                if candidate is not None and hasattr(candidate, "getsockopt"):
                    return candidate

        # meshtastic stores a _wantAck dict and internally a _meshConn
        mesh_conn = getattr(iface, "_meshConn", None)
        if mesh_conn is not None:
            for sub_attr in ("_socket", "socket", "_sock"):
                candidate = getattr(mesh_conn, sub_attr, None)
                if candidate is not None and hasattr(candidate, "getsockopt"):
                    return candidate

        return None

    async def _serial_device_alive(self) -> bool:
        def _check():
            port = self.config.get("MESHTASTIC_SERIAL_PORT", "/dev/ttyACM0")
            if hasattr(self.interface, "devPath"):
                port = self.interface.devPath
            if not os.path.exists(port):
                self.logger.debug(f"Serial health: {port} does not exist.")
                return False
            try:
                ser = None
                if hasattr(self.interface, "_serial"):
                    ser = self.interface._serial
                elif hasattr(self.interface, "serial"):
                    ser = self.interface.serial
                if ser is not None:
                    if not ser.is_open:
                        self.logger.debug("Serial health: Port is closed.")
                        return False
                    _ = ser.in_waiting
            except Exception as e:
                self.logger.debug(f"Serial health: Port error — {e}")
                return False
            return True

        try:
            return await asyncio.wait_for(asyncio.to_thread(_check), timeout=3.0)
        except asyncio.TimeoutError:
            return False

    async def _ble_gatt_alive(self) -> bool:
        if not HAS_BLE:
            return False
        def _check():
            try:
                client = None
                if hasattr(self.interface, "_client"):
                    client = self.interface._client
                elif hasattr(self.interface, "client"):
                    client = self.interface.client
                if client is None:
                    return True
                return client.is_connected
            except Exception:
                return False
        try:
            return await asyncio.wait_for(asyncio.to_thread(_check), timeout=3.0)
        except asyncio.TimeoutError:
            return False

    async def _meshtastic_probe(self) -> bool:
        """
        Layer 2: Verify the meshtastic interface is genuinely alive.

        During the grace period (first INITIAL_MYINFO_GRACE_SECONDS after transport
        comes up), we only verify transport-layer flags (socket alive, connection
        flags). After the grace period expires, we require myInfo to be present.

        getMyNodeInfo() was removed in meshtastic-python >= 2.x. We instead:
          1. Check myInfo is present and has a valid node number (populated during init).
          2. Verify the interface's internal connection flag (iface._connected or similar).
          3. For TCP: attempt a zero-byte write to the underlying socket to detect
             half-open connections — this is the critical check that catches a powered-off radio.
        """
        def _probe():
            try:
                iface = self.interface
                if iface is None:
                    return False

                # Check grace period: transport up but myInfo may not have arrived yet
                if self._interface_up_at is not None:
                    elapsed = time.time() - self._interface_up_at
                    in_grace = elapsed < self.INITIAL_MYINFO_GRACE_SECONDS
                else:
                    in_grace = False

                # Check 1: myInfo must be present with a valid node num
                # During grace period, skip this check — transport flags below are sufficient
                my_info = getattr(iface, "myInfo", None)
                if not in_grace:
                    if not my_info:
                        self.logger.debug("Probe: no myInfo on interface")
                        return False
                    if not getattr(my_info, "my_node_num", None):
                        self.logger.debug("Probe: myInfo has no node num")
                        return False

                # Check 2: internal connection state flags used by meshtastic-python
                # Different versions use different attribute names
                for connected_attr in ("_connected", "isConnected", "_isConnected"):
                    flag = getattr(iface, connected_attr, None)
                    if flag is not None and flag is False:
                        self.logger.debug(f"Probe: interface.{connected_attr} is False")
                        return False

                # Check 3: TCP half-open detection via socket write probe
                # A powered-off radio will have a half-open TCP socket — getpeername() passes
                # but send() will fail or block. We use MSG_DONTWAIT to avoid blocking.
                transport = self.config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL").upper()
                if transport == "TCP":
                    sock = self._find_tcp_socket()
                    if sock is not None:
                        try:
                            # Send empty bytes with MSG_DONTWAIT (non-blocking)
                            # On a live socket this succeeds or raises EAGAIN/EWOULDBLOCK (acceptable)
                            # On a dead socket this raises EPIPE/ECONNRESET/ENOTCONN
                            sock.send(b"", socket.MSG_DONTWAIT)
                        except BlockingIOError:
                            # EAGAIN / EWOULDBLOCK — socket buffer full, but connection is alive
                            pass
                        except OSError as e:
                            import errno
                            if e.errno in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN,
                                           errno.ECONNABORTED, errno.ETIMEDOUT, errno.ENETUNREACH):
                                self.logger.debug(f"Probe: TCP write probe failed — {e}")
                                return False
                            # Other errno (e.g. EINVAL on some platforms) — defer, don't fail
                            self.logger.debug(f"Probe: TCP write probe OSError (non-fatal) — {e}")

                return True

            except Exception as e:
                self.logger.debug(f"Meshtastic probe error: {e}")
                return False

        try:
            probe_start = time.time()
            result = await asyncio.wait_for(asyncio.to_thread(_probe), timeout=self.PROBE_TIMEOUT)
            probe_time = (time.time() - probe_start) * 1000  # ms
            # Store latency sample (keep last 20)
            self._latency_samples.append(probe_time)
            if len(self._latency_samples) > 20:
                self._latency_samples.pop(0)
            return result
        except asyncio.TimeoutError:
            self.logger.warning(
                f"⏰ Meshtastic probe timed out ({self.PROBE_TIMEOUT}s) — radio likely powered off or unreachable."
            )
            return False

    async def _attempt_connection(self, transport: str) -> bool:
        self.logger.info(f"🔌 Attempting {transport} connection...")

        reachable = await self._check_transport_availability(transport)
        if not reachable:
            self.logger.warning(f"⚠️  {transport} target not reachable at OS level.")
            self._set_state(ConnectionState.RECONNECTING, detail=f"{transport} target unreachable")
            return False

        try:
            interface = await asyncio.wait_for(
                self._create_interface_object(transport),
                timeout=self.INTERFACE_INIT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.logger.error(f"❌ {transport} interface init timed out ({self.INTERFACE_INIT_TIMEOUT}s).")
            return False
        except Exception as e:
            self.logger.error(f"❌ {transport} interface init failed: {e}")
            return False

        my_info = getattr(interface, "myInfo", None)
        if not my_info or not getattr(my_info, "my_node_num", None):
            self.logger.error("❌ Interface created but radio returned no node info (zombie interface). Destroying.")
            await asyncio.to_thread(self._safe_close, interface)
            return False

        self.interface = interface
        self._active_transport = transport

        if transport == "TCP":
            self._apply_tcp_keepalive()

        self._subscribe_events()

        hw = "Unknown"
        meta = getattr(interface, "metadata", None)
        if meta:
            hw = getattr(meta, "hw_model_str", "Unknown")

        node_id = f"!{my_info.my_node_num:08x}"
        self.logger.info(f"✅ {transport} interface ready: {node_id} ({hw})")

        # Connection metrics — successful connection
        self._connect_count += 1
        self._last_connected_at = time.time()
        self._current_connected_since = self._last_connected_at

        self._interface_up_at = time.time()
        self.is_ready.set()
        self._set_state(ConnectionState.DEGRADED, detail="STREAM OPEN")
        self.logger.info("🔗 Transport layer up — waiting for radio myInfo...")
        asyncio.create_task(self._hydrate_local_node(interface))
        return True

    async def _check_transport_availability(self, transport: str) -> bool:
        try:
            if transport == "SERIAL":
                port = self.config.get("MESHTASTIC_SERIAL_PORT", "/dev/ttyACM0")
                exists = await asyncio.to_thread(os.path.exists, port)
                if exists:
                    return True
                ports = await asyncio.to_thread(meshtastic.util.findPorts)
                if ports:
                    self.logger.info(f"📟 Configured port not found, discovered: {ports[0]}")
                    self.config["MESHTASTIC_SERIAL_PORT"] = ports[0]
                    return True
                return False

            elif transport == "TCP":
                # Do NOT open a probe connection here.
                # Many Meshtastic radios (e.g. Heltec V3) only support one TCP
                # client at a time. Opening a pre-check socket and closing it
                # leaves the radio's TCP stack in a half-closed state for
                # ~100-500ms, causing the immediately following TCPInterface()
                # constructor to get ECONNRESET / EPIPE.
                # Instead, just do a DNS/route reachability check via ping-style
                # socket with SO_REUSEADDR but no full connect, or simply skip
                # the pre-check and let TCPInterface() be the authoritative test.
                return True

            elif transport == "BLE":
                if not HAS_BLE:
                    self.logger.error("BLE support not installed.")
                    return False
                mac = self.config.get("MESHTASTIC_BLE_MAC", "")
                if not mac:
                    self.logger.error("No BLE MAC address configured.")
                    self._set_state(ConnectionState.DISCONNECTED, detail="BLE MAC not configured")
                    return False
                try:
                    device = await BleakScanner.find_device_by_address(mac, timeout=self.TRANSPORT_CHECK_TIMEOUT)
                    return device is not None
                except Exception as e:
                    self.logger.debug(f"BLE scan failed: {e}")
                    return False

            self.logger.error(f"Unknown transport type: {transport}")
            return False

        except Exception as e:
            self.logger.debug(f"Transport availability check error: {e}")
            return False

    async def _create_interface_object(self, transport: str):
        if transport == "SERIAL":
            port = self.config.get("MESHTASTIC_SERIAL_PORT", "/dev/ttyACM0")
            if not await asyncio.to_thread(os.path.exists, port):
                ports = await asyncio.to_thread(meshtastic.util.findPorts)
                if ports:
                    port = ports[0]
            self.logger.info(f"📟 Opening serial: {port}")
            return await asyncio.to_thread(
                meshtastic.serial_interface.SerialInterface,
                devPath=port,
                connectNow=True,
            )

        elif transport == "TCP":
            host = self.config.get("MESHTASTIC_HOST", "192.168.1.50")
            try:
                port = int(self.config.get("MESHTASTIC_PORT", 4403))
            except (ValueError, TypeError):
                port = 4403
            self.logger.info(f"🌐 Opening TCP: {host}:{port}")
            return await asyncio.to_thread(
                meshtastic.tcp_interface.TCPInterface,
                hostname=host,
                portNumber=port,
            )

        elif transport == "BLE":
            if not HAS_BLE:
                raise ImportError("BLE dependencies not installed (bleak)")
            mac = self.config.get("MESHTASTIC_BLE_MAC", "")
            self.logger.info(f"📶 Opening BLE: {mac}")
            return await meshtastic.ble_interface.BLEInterface(address=mac)

        raise ValueError(f"Unknown transport: {transport}")

    def _apply_tcp_keepalive(self):
        try:
            sock = self._find_tcp_socket()
            if sock is None:
                self.logger.debug("TCP keepalive: Could not locate raw socket.")
                return

            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, self.TCP_KEEPALIVE_IDLE)
            except (AttributeError, OSError):
                pass
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, self.TCP_KEEPALIVE_INTERVAL)
            except (AttributeError, OSError):
                pass
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, self.TCP_KEEPALIVE_COUNT)
            except (AttributeError, OSError):
                pass

            self.logger.info(
                f"🔧 TCP keepalive enabled "
                f"(idle={self.TCP_KEEPALIVE_IDLE}s, interval={self.TCP_KEEPALIVE_INTERVAL}s, count={self.TCP_KEEPALIVE_COUNT})"
            )
        except Exception as e:
            self.logger.warning(f"TCP keepalive setup failed: {e}")

    async def _hydrate_local_node(self, interface) -> None:
        try:
            await asyncio.sleep(1.5)
            if interface and getattr(interface, "myInfo", None):
                self.meshtastic_data.set_local_node_info(interface.myInfo)
        except Exception as e:
            self.logger.warning(f"Hydration warning: {e}")

    async def _close_interface(self, reason: str = "Unknown") -> None:
        self.logger.info(f"🔌 Closing interface — reason: {reason}")
        self.is_ready.clear()
        self._unsubscribe_all()
        self._interface_ref[0] = None

        # Connection metrics — disconnection
        if self._current_connected_since is not None:
            self._total_uptime += time.time() - self._current_connected_since
            self._current_connected_since = None
        self._disconnect_count += 1
        self._last_disconnected_at = time.time()
        self._last_disconnect_reason = reason

        self._interface_up_at = None
        self._active_transport = None
        if self.interface:
            iface = self.interface
            self.interface = None
            await asyncio.to_thread(self._safe_close, iface)
        self._set_state(ConnectionState.RECONNECTING, detail="Disconnected")

    @staticmethod
    def _safe_close(iface) -> None:
        try:
            iface.close()
        except Exception:
            pass  # defensive teardown — never let close() bubble up

    def _subscribe_events(self) -> None:
        self._unsubscribe_all()
        self._interface_ref[0] = self.interface

        iface_ref = self._interface_ref

        def _filtered_disconnect(interface=None, topic=None):
            if interface is None or interface is iface_ref[0]:
                self._on_library_disconnect(interface=interface, topic=topic)
        self._filtered_disconnect_cb = _filtered_disconnect

        pub.subscribe(self._filtered_disconnect_cb, TOPIC_CONNECTION_LOST)
        if self._filtered_receive_cb:
            pub.subscribe(self._filtered_receive_cb, TOPIC_RECEIVED)
        if self._filtered_node_updated_cb:
            pub.subscribe(self._filtered_node_updated_cb, TOPIC_NODE_UPDATED)
        if self._on_connection_cb and self.interface:
            try:
                self._on_connection_cb(self.interface, topic=TOPIC_CONNECTION_ESTABLISHED)
            except Exception as e:
                self.logger.warning(f"on_connection callback error: {e}")

    def _unsubscribe_all(self) -> None:
        listeners = [
            (TOPIC_RECEIVED, self._filtered_receive_cb),
            (TOPIC_NODE_UPDATED, self._filtered_node_updated_cb),
            (TOPIC_CONNECTION_LOST, self._filtered_disconnect_cb),
        ]
        for topic, listener in listeners:
            if listener:
                try:
                    pub.unsubscribe(listener, topic)
                except Exception:
                    pass

    def _on_library_disconnect(self, interface=None, topic=None) -> None:
        if self.is_ready.is_set():
            self.logger.error("🛑 Library reported disconnect — forcing immediate teardown.")
            self._failure_strikes = self.MAX_STRIKES
            self.is_ready.clear()

    def _load_config(self) -> bool:
        if self._connection_params is not None:
            return False
        if not os.path.exists(CONFIG_FILE_PATH):
            return False
        try:
            mtime = os.path.getmtime(CONFIG_FILE_PATH)
            if mtime == self._config_mtime:
                return False

            old_transport = self.config.get("MESHTASTIC_CONNECTION_TYPE", "")
            old_host = self.config.get("MESHTASTIC_HOST", "")
            old_port = self.config.get("MESHTASTIC_PORT", "")
            old_serial = self.config.get("MESHTASTIC_SERIAL_PORT", "")
            old_ble = self.config.get("MESHTASTIC_BLE_MAC", "")

            self._config_mtime = mtime

            with open(CONFIG_FILE_PATH, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'").strip('"')
                    if key in self.config:
                        self.config[key] = value

            changed = (
                self.config.get("MESHTASTIC_CONNECTION_TYPE", "") != old_transport
                or self.config.get("MESHTASTIC_HOST", "") != old_host
                or self.config.get("MESHTASTIC_PORT", "") != old_port
                or self.config.get("MESHTASTIC_SERIAL_PORT", "") != old_serial
                or self.config.get("MESHTASTIC_BLE_MAC", "") != old_ble
            )

            if changed:
                self.logger.info(
                    f"🔧 Config reloaded — connection settings changed. "
                    f"Transport: {self.config.get('MESHTASTIC_CONNECTION_TYPE')}"
                )
            return changed

        except Exception as e:
            self.logger.warning(f"Config load error: {e}")
            return False

    def _calculate_backoff(self, attempt: int) -> float:
        base = min(self.BASE_BACKOFF * (2 ** min(attempt, 3)), self.MAX_BACKOFF)
        jitter = random.uniform(-self.BACKOFF_JITTER, self.BACKOFF_JITTER)
        return max(1.0, base + jitter)

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
            # Clear the wake event so it doesn't immediately wake again
            self._wake_event.clear()
        except asyncio.TimeoutError:
            pass

    def __getattr__(self, name: str):
        if name == "sendText":
            raise AttributeError("Use `await manager.sendText()`")
        if self.interface and hasattr(self.interface, name):
            return getattr(self.interface, name)
        return lambda *a, **k: None