from fastapi import status
from typing import Any
from enum import Enum


class ConnectionState(str, Enum):
    """Formal connection state machine for all connection managers.

    Each manager tracks self._state and must only transition through
    valid paths. The UI receives structured state+detail instead of
    arbitrary status strings.
    """
    IDLE = "idle"                  # Before first connect attempt
    CONNECTING = "connecting"       # Actively trying to connect
    CONNECTED = "connected"          # Fully connected and operational
    RECONNECTING = "reconnecting"    # Lost connection, retrying
    DEGRADED = "degraded"            # Transport up but radio not responding (myInfo missing after grace)
    DISCONNECTED = "disconnected"    # Gave up reconnecting (max retries hit)
    WEBSERIAL = "webserial"          # Browser-serial mode
    MQTT = "mqtt"                   # MQTT managed mode


# Valid state transitions — keys are source states, values are sets of
# allowed destination states.  Any transition not in this map is rejected.
_VALID_TRANSITIONS: dict = {
    ConnectionState.IDLE:         {ConnectionState.CONNECTING, ConnectionState.DISCONNECTED, ConnectionState.WEBSERIAL, ConnectionState.MQTT},
    ConnectionState.CONNECTING:   {ConnectionState.CONNECTED, ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED, ConnectionState.DEGRADED},
    ConnectionState.CONNECTED:   {ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED, ConnectionState.DEGRADED},
    ConnectionState.RECONNECTING:{ConnectionState.CONNECTED, ConnectionState.DISCONNECTED, ConnectionState.CONNECTING},
    ConnectionState.DEGRADED:    {ConnectionState.CONNECTED, ConnectionState.RECONNECTING, ConnectionState.DISCONNECTED},
    ConnectionState.DISCONNECTED:{ConnectionState.CONNECTING, ConnectionState.IDLE},  # force_reconnect goes CONNECTING
    ConnectionState.WEBSERIAL:    {ConnectionState.IDLE, ConnectionState.DISCONNECTED},  # can disconnect to reconnect differently
    ConnectionState.MQTT:        {ConnectionState.IDLE, ConnectionState.DISCONNECTED},  # can disconnect to reconnect differently
}


def is_valid_transition(from_state: ConnectionState, to_state: ConnectionState) -> bool:
    """Check whether transitioning from from_state to to_state is allowed."""
    allowed = _VALID_TRANSITIONS.get(from_state, set())
    return to_state in allowed


from .meshtastic import MeshtasticConnectionManager
from .mqtt import MQTTConnectionManager
from .meshcore import MeshCoreConnectionManager

__all__ = [
    "ConnectionState", "is_valid_transition",
    "MeshtasticConnectionManager", "MQTTConnectionManager", "MeshCoreConnectionManager",
]
