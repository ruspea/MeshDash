import core.globals as g
"""
SSE (Server-Sent Events) — extracted from meshtastic_dashboard.py
Server-Sent Events broadcast and queue management.

Broadcast functions are defined in core.broadcast.py and re-exported here
for backward compatibility.  The canonical implementations live in broadcast.py;
this module only provides the SSE queue constants used by slot_routes.py and
the MAX_SSE_CLIENTS / client-id counter.
"""
from core.broadcast import broadcast_data, broadcast_stats, broadcast_stats_for_slot  # noqa: F401
import asyncio
import logging
from typing import Dict

logger = logging.getLogger("core")

# Constants and global state
MAX_SSE_CLIENTS = 50
_sse_client_id = 0

# NOTE: The actual SSE queues live on core.globals (g.sse_queues, g.all_sse_queues)
# and are set by meshtastic_dashboard.py at startup.  slot_routes.py registers
# clients into g.sse_queues / g.all_sse_queues.  The dicts below are kept only
# as fallback defaults — they are replaced by meshtastic_dashboard before any
# client connects.
sse_queues: Dict[int, asyncio.Queue] = {}
sse_queues_lock = asyncio.Lock()
all_sse_queues: Dict[int, asyncio.Queue] = {}
all_sse_queues_lock = asyncio.Lock()