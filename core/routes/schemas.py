# Auto-extracted from meshtastic_dashboard.py
import asyncio
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Optional, Dict, Any, List, Set
from pydantic import BaseModel as PydanticBaseModel


class _SlotGlobalsProxy:
    """Proxies attribute access so slot.g.meshtastic_data returns the slot's own
    instance, not the shared core.globals (which always holds node_0's state).
    Falls back to core.globals for shared constants like MAX_SSE_CLIENTS."""
    __slots__ = ('_slot', '_globals')

    # Attributes that MUST resolve to the slot's own instances
    _SLOT_ATTRS = frozenset({
        'meshtastic_data', 'connection_manager', 'db_manager',
        'sse_queues', 'sse_queues_lock', 'all_sse_queues', 'all_sse_queues_lock',
    })

    def __init__(self, slot, globals_module):
        object.__setattr__(self, '_slot', slot)
        object.__setattr__(self, '_globals', globals_module)

    def __getattr__(self, name):
        if name in self._SLOT_ATTRS:
            # Return slot-scoped instance if the slot has it directly
            if hasattr(self._slot, name):
                return getattr(self._slot, name)
            # Name mismatches: global name -> slot field name
            _ALIASES = {
                'sse_queues_lock': 'sse_lock',
                'all_sse_queues': 'sse_queues',  # fallback: no separate all_queues on slot
                'all_sse_queues_lock': 'sse_lock',
            }
            if name in _ALIASES and hasattr(self._slot, _ALIASES[name]):
                return getattr(self._slot, _ALIASES[name])
            # connection_manager and db_manager are on the slot directly
            if name == 'connection_manager':
                return self._slot.connection_manager
            if name == 'db_manager':
                return self._slot.db_manager
            if name == 'meshtastic_data':
                return self._slot.meshtastic_data
        # Fall back to shared globals
        return getattr(self._globals, name)

    def __setattr__(self, name, value):
        if name in self._SLOT_ATTRS:
            _ALIASES = {
                'sse_queues_lock': 'sse_lock',
                'all_sse_queues': 'sse_queues',
                'all_sse_queues_lock': 'sse_lock',
            }
            target = _ALIASES.get(name, name)
            setattr(self._slot, target, value)
        else:
            setattr(self._globals, name, value)


class User(PydanticBaseModel):
    username: str
    disabled: Optional[bool] = None
    role: Optional[int] = 1  # 0=admin, 1=operator, 2=spectator


class TokenData(PydanticBaseModel):
    username: Optional[str] = None


@dataclass
class NodeSlot:
    slot_id: str
    label: str
    meshtastic_data: "MeshtasticData"
    db_manager: "DatabaseManager"
    connection_manager: "MeshtasticConnectionManager"
    packet_queue: asyncio.Queue
    tasks: Set[asyncio.Task] = dc_field(default_factory=set)
    # slot-scoped SSE queues: client_id  queue
    sse_queues: Dict[int, asyncio.Queue] = dc_field(default_factory=dict)
    sse_lock: asyncio.Lock = dc_field(default_factory=asyncio.Lock)
    # Stable unique DB identifier  used to name the SQLite file.

    @property
    def g(self):
        """Proxy that returns slot-scoped objects for slot.g.X access.

        For slot-specific attributes (meshtastic_data, connection_manager,
        db_manager, sse_queues, sse_queues_lock), returns the slot's own instances.
        For everything else, falls back to the shared core.globals module.
        This ensures additional slots (node_1, node_2, ...) read their own state
        instead of node_0's state via the global module.
        """
        import core.globals as _g
        return _SlotGlobalsProxy(self, _g)
    # Generated once at slot creation, persisted in slots.json.
    # Decoupled from slot_id so deleting+recreating a slot never reuses old data.
    db_uuid: str = dc_field(default_factory=lambda: uuid.uuid4().hex)


class MessageRequest(PydanticBaseModel):
    message: str
    destination: Optional[str] = None
    channel: int = 0
    slot_id: str = "node_0"


class URLRequest(PydanticBaseModel):
    url: str
    block_id: Optional[int] = None
    text_only: bool = False


class WebsiteMonitorRequest(PydanticBaseModel):
    url: str
    block_id: int
    prefix: str
    node_id: Optional[str] = None
    channel: int = 0
    slot_id: str = "node_0"


class ConsoleRequest(PydanticBaseModel):
    command: str
    slot_id: str = "node_0"


class TracerouteRequest(PydanticBaseModel):
    node_id: str
    hop_limit: int = 5
    slot_id: str = "node_0"


class AdminUserPayload(PydanticBaseModel):
    username: str
    password: str


class SetupWizardPayload(PydanticBaseModel):
    """Validated payload for POST /api/system/config/initial-setup.
    Matches the nested structure the setup wizard frontend sends:
    { adminUser: {username, password}, configValues: {...}, rawSelections: {...} }"""
    adminUser: AdminUserPayload
    configValues: dict = {}
    rawSelections: dict = {}


class InitialSetupRequest(PydanticBaseModel):
    username: str
    password: str
    AUTH_SECRET_KEY: str
    AUTH_TOKEN_EXPIRE_MINUTES: int
    MESHTASTIC_CONNECTION_TYPE: str
    MESHTASTIC_SERIAL_PORT: Optional[str] = ""
    MESHTASTIC_HOST: Optional[str] = ""
    MESHTASTIC_PORT: Optional[int] = 4403
    MESHTASTIC_BLE_MAC: Optional[str] = ""
    WEBSERVER_HOST: str = "0.0.0.0"
    WEBSERVER_PORT: int = 8000
    NETWORK_WEBSERVER_PORT: int = 8000
    DB_PATH: str = "meshtastic_data.db"
    TASK_DB_PATH: str = "tasks.db"
    MAX_PACKETS_MEMORY: int = 200
    HISTORY_DAYS: int = 30
    LOG_LEVEL: str = "INFO"
    COMMUNITY_API: bool = False
    COMMUNITY_API_KEY: Optional[str] = ""
    SEND_LOCAL_NODE_LOCATION: bool = True
    SEND_OTHER_NODES_LOCATION: bool = True
    LOCATION_OFFSET_ENABLED: bool = False
    LOCATION_OFFSET_METERS: float = 0
    HEARTBEAT_INTERVAL_MINUTES: int = 1
    SCHEDULER_MAX_RETRIES: int = 3
    SCHEDULER_RETRY_DELAY_SECONDS: int = 10
    SCHEDULER_CONNECT_TIMEOUT: float = 10
    SCHEDULER_RW_TIMEOUT: float = 30


class ConfigUpdateRequest(PydanticBaseModel):
    # AUTH_SECRET_KEY intentionally excluded  must be changed via config file edit
    AUTH_TOKEN_EXPIRE_MINUTES: Optional[int] = None
    MESHTASTIC_CONNECTION_TYPE: Optional[str] = None
    MESHTASTIC_SERIAL_PORT: Optional[str] = None
    MESHTASTIC_HOST: Optional[str] = None
    MESHTASTIC_PORT: Optional[int] = None
    MESHTASTIC_BLE_MAC: Optional[str] = None
    WEBSERVER_HOST: Optional[str] = None
    WEBSERVER_PORT: Optional[int] = None
    NETWORK_WEBSERVER_PORT: Optional[int] = None
    DB_PATH: Optional[str] = None
    TASK_DB_PATH: Optional[str] = None
    MAX_PACKETS_MEMORY: Optional[int] = None
    HISTORY_DAYS: Optional[int] = None
    LOG_LEVEL: Optional[str] = None
    COMMUNITY_API: Optional[bool] = None
    COMMUNITY_API_KEY: Optional[str] = None
    SEND_LOCAL_NODE_LOCATION: Optional[bool] = None
    SEND_OTHER_NODES_LOCATION: Optional[bool] = None
    LOCATION_OFFSET_ENABLED: Optional[bool] = None
    LOCATION_OFFSET_METERS: Optional[float] = None
    HEARTBEAT_INTERVAL_MINUTES: Optional[int] = None
    SCHEDULER_MAX_RETRIES: Optional[int] = None
    SCHEDULER_RETRY_DELAY_SECONDS: Optional[int] = None
    SCHEDULER_CONNECT_TIMEOUT: Optional[float] = None
    SCHEDULER_RW_TIMEOUT: Optional[float] = None
    C2_ACCESS_LEVEL: Optional[str] = None
    REMOTE_C2: Optional[bool] = None
    # Heartbeat API key/URLs are hardcoded server-side — not user-configurable.


class RemoteInstallRequest(PydanticBaseModel):
    url: str


class NodeConfigSaveRequest(PydanticBaseModel):
    changes: List[Dict[str, Any]]
    reboot: bool = True
    slot_id: str = "node_0"


class SlotCreateRequest(PydanticBaseModel):
    label:           str           = "New Radio"
    connection_type: str           = "TCP"
    host:            Optional[str] = None
    port:            Optional[int] = 4403
    serial_port:     Optional[str] = None
    ble_mac:         Optional[str] = None
    # MQTT fields  only used when connection_type == "MQTT"
    mqtt_broker:     Optional[str] = None   # hostname or IP
    mqtt_port:       Optional[int] = None   # default 1883 / 8883 for TLS
    mqtt_username:   Optional[str] = None
    mqtt_password:   Optional[str] = None
    mqtt_tls:        bool          = False  # enable TLS/SSL
    mqtt_region:     Optional[str] = None   # region code, e.g. "EU_868" or "#" for all
    mqtt_channel:    Optional[str] = None   # channel name, e.g. "LongFast" or "#" for all
    mqtt_node_id:    Optional[str] = None   # our node's !hexid (optional  observer mode if unset)
    mqtt_preset:     Optional[str] = None   # "meshtastic_public" | "meshtastic_public_tls" | "custom"
    # MeshCore fields  only used when connection_type == "MESHCORE"
    meshcore_transport:   Optional[str] = None   # "serial" | "tcp" | "ble"
    meshcore_serial_port: Optional[str] = None   # e.g. /dev/ttyUSB0 or COM3
    meshcore_baud:        Optional[int] = None   # serial baud rate (default 115200)
    meshcore_host:        Optional[str] = None   # TCP host
    meshcore_port:        Optional[int] = None   # TCP port (default 4000)
    meshcore_ble_mac:     Optional[str] = None   # BLE MAC address
    meshcore_ble_pin:     Optional[str] = None   # BLE PIN for pairing (optional)