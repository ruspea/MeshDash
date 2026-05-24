import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
import asyncio
import json
import logging
import os
import secrets
import uuid
from typing import Dict, Optional, Any
from fastapi import APIRouter, Request, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from core.routes.schemas import User, NodeSlot, SlotCreateRequest
from core.auth import verify_csrf, get_current_active_user, ensure_serializable
from core.database import DatabaseManager
from core.data import MeshtasticData
from core.connections.meshtastic import MeshtasticConnectionManager
from core.broadcast import broadcast_data
from core.packet import on_receive, _make_slot_on_connection, _make_slot_on_node_updated, _packet_processing_worker_for_slot
from core.config_loader import _save_slots_file

logger = logging.getLogger(__name__)
router = APIRouter()

_sse_client_id = 0

DEFAULT_TARGET_HOST = "192.168.0.0"
DB_PATH = "meshtastic_data.db"
MAX_SLOTS = 16

try:
    from core.connections.mqtt import MQTTConnectionManager
    _HAS_MQTT = True
except ImportError:
    _HAS_MQTT = False

try:
    from core.connections.meshcore import MeshCoreConnectionManager
    _HAS_MESHCORE = True
except ImportError:
    _HAS_MESHCORE = False
@router.api_route("/sse", methods=["GET", "HEAD", "POST"])
async def sse(request: Request):
    global _sse_client_id

    # R2.X: Reject connections beyond cap
    async with g.sse_queues_lock:
        if len(g.sse_queues) >= g.MAX_SSE_CLIENTS:
            raise HTTPException(503, f"SSE client limit ({g.MAX_SSE_CLIENTS}) reached. Try again later.")
        _sse_client_id += 1
        cid = _sse_client_id
        # Bounded per-client queue: 200 events; older events are dropped if client is slow
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        g.sse_queues[cid] = q

    async def gen():
        try:
            cs = g.meshtastic_data
            yield {"event": "connection_status", "data": json.dumps({"state": cs._connection_state or "idle", "detail": cs._connection_detail or "", "transport": cs._connection_transport or "", "label": cs.connection_status or "Unknown"})}
            # Stream nodes in chunks for responsive loading
            _CHUNK = 500
            nodes_snapshot = list(g.meshtastic_data.nodes.values())
            if len(nodes_snapshot) <= _CHUNK:
                yield {"event": "nodes", "data": json.dumps(ensure_serializable(nodes_snapshot))}
            else:
                yield {"event": "nodes", "data": json.dumps(ensure_serializable(nodes_snapshot[:_CHUNK]))}
                for offset in range(_CHUNK, len(nodes_snapshot), _CHUNK):
                    chunk = nodes_snapshot[offset:offset + _CHUNK]
                    yield {"event": "node_batch", "data": json.dumps(ensure_serializable(chunk))}
                    await asyncio.sleep(0)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield payload
                    q.task_done()
                except asyncio.TimeoutError:
                    # R2.X: Send a keepalive comment so proxies don't kill idle connections
                    yield {"event": "ping", "data": "{}"}
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"SSE Payload Error: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"SSE Critical Error: {e}")
        finally:
            async with g.sse_queues_lock:
                g.sse_queues.pop(cid, None)
            logger.debug(f"SSE client {cid} disconnected. Active: {len(g.sse_queues)}")

    return EventSourceResponse(gen(), ping=15)


@router.get("/api/slots")
async def list_slots(user: User = Depends(get_current_active_user)):
    result = {}
    for sid, slot in g.NODE_REGISTRY.items():
        cm = slot.g.connection_manager
        conn_type = "UNKNOWN"
        if cm is not None:
            conn_type = cm.config.get("MESHTASTIC_CONNECTION_TYPE", "UNKNOWN").upper()

        # DB size  read from disk without opening the connection
        db_size_mb: Optional[float] = None
        db_path_str: Optional[str] = None
        if sid != "node_0" and not g.PUBLIC_MODE and slot.db_uuid:
            _p = f"meshtastic_data_{slot.db_uuid}.db"
        elif sid != "node_0" and not g.PUBLIC_MODE:
            _p = f"meshtastic_data_{sid}.db"  # legacy
        else:
            _p = None
        if _p:
            db_path_str = _p
            try:
                _sz = os.path.getsize(_p)
                db_size_mb = round(_sz / (1024 * 1024), 2)
            except OSError:
                db_size_mb = None

        # Collect connection detail fields for the frontend slot config display
        conn_detail = {}
        if cm is not None and cm.config:
            cfg = cm.config
            if conn_type == "TCP":
                conn_detail = {"host": cfg.get("MESHTASTIC_HOST", ""), "port": cfg.get("MESHTASTIC_PORT", "")}
            elif conn_type == "MQTT":
                conn_detail = {
                    "broker": cfg.get("MQTT_BROKER", ""),
                    "port": cfg.get("MQTT_PORT", ""),
                    "region": cfg.get("MQTT_REGION", ""),
                    "channel": cfg.get("MQTT_CHANNEL", ""),
                    "node_id": cfg.get("MQTT_NODE_ID", ""),
                    "tls": cfg.get("MQTT_TLS", "false"),
                }
            elif conn_type == "SERIAL":
                conn_detail = {"serial_port": cfg.get("MESHTASTIC_SERIAL_PORT", "")}
            elif conn_type == "BLE":
                conn_detail = {"ble_mac": cfg.get("MESHTASTIC_BLE_MAC", "")}
            elif conn_type == "MESHCORE":
                conn_detail = {
                    "transport": cfg.get("MESHCORE_TRANSPORT", ""),
                    "host": cfg.get("MESHCORE_HOST", ""),
                    "port": cfg.get("MESHCORE_PORT", ""),
                    "serial_port": cfg.get("MESHCORE_SERIAL_PORT", ""),
                    "baud": cfg.get("MESHCORE_BAUD", ""),
                    "ble_mac": cfg.get("MESHCORE_BLE_MAC", ""),
                }

        result[sid] = {
            "slot_id":          sid,
            "label":            slot.label,
            "connection_status":slot.g.meshtastic_data.connection_status,
            "is_ready":         cm.is_ready.is_set() if cm else False,
            "local_node_id":    slot.g.meshtastic_data.local_node_id,
            "node_count":       len(slot.g.meshtastic_data.nodes),
            "packets_session":  slot.g.meshtastic_data.stats.get("packets_received_session", 0),
            "connection_type":  conn_type,
            "connection_detail": conn_detail,
            "db_uuid":          slot.db_uuid if sid != "node_0" else None,
            "db_path":          db_path_str,
            "db_size_mb":       db_size_mb,
        }
    return result


@router.post("/api/slots")
async def create_slot(req: SlotCreateRequest, user: User = Depends(verify_csrf)):
    if len(g.NODE_REGISTRY) >= MAX_SLOTS:
        raise HTTPException(400, f"Maximum slot limit ({MAX_SLOTS}) reached.")

    slot_id = f"node_{len(g.NODE_REGISTRY)}"
    while slot_id in g.NODE_REGISTRY:
        slot_id = f"node_{secrets.token_hex(3)}"

    # Generate a stable unique identifier for this slot's database.
    # Using a UUID means the DB filename is independent of the slot position counter 
    # deleting node_1 and adding a new radio won't silently reuse the old node_1 DB.
    db_uuid = uuid.uuid4().hex
    db_path = f"meshtastic_data_{db_uuid}.db" if not g.PUBLIC_MODE else ":memory:"
    slot_db = DatabaseManager(db_path, ephemeral=g.PUBLIC_MODE)
    slot_md = MeshtasticData(slot_db, g.MAX_PACKETS_IN_MEMORY, slot_id=slot_id)
    slot_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    conn_type = req.connection_type.upper()

    if conn_type == "MQTT":
        #  MQTT slot 
        if not _HAS_MQTT:
            raise HTTPException(503, "MQTT support not available (core.connections.mqtt missing or paho-mqtt not installed)")

        # Apply preset values first, then override with any explicit fields
        from core.connections.mqtt import MQTT_PRESETS
        preset_key = (req.mqtt_preset or "meshtastic_public").lower().replace(" ", "_")
        preset = MQTT_PRESETS.get(preset_key, MQTT_PRESETS["meshtastic_public"])

        mqtt_params = {
            "MESHTASTIC_CONNECTION_TYPE": "MQTT",
            "MQTT_BROKER":   req.mqtt_broker   or preset["broker"],
            "MQTT_PORT":     str(req.mqtt_port or preset["port"]),
            "MQTT_USERNAME": req.mqtt_username if req.mqtt_username is not None else preset.get("username", ""),
            "MQTT_PASSWORD": req.mqtt_password if req.mqtt_password is not None else preset.get("password", ""),
            "MQTT_TLS":      "true" if (req.mqtt_tls or preset.get("tls", False)) else "false",
            "MQTT_REGION":   req.mqtt_region  or preset.get("region", "EU_868"),
            "MQTT_CHANNEL":  req.mqtt_channel or "#",
            "MQTT_NODE_ID":  req.mqtt_node_id or "",
            "MQTT_CLIENT_ID": "",
            "MQTT_ROOT_TOPIC": "",
        }

        slot_cm = MQTTConnectionManager(
            slot_md,
            logging.getLogger(f"MQTTConnection.{slot_id}"),
            connection_params=mqtt_params,
            slot_id=slot_id,
        )
        # Wire the packet queue so MQTT messages land in the right place
        slot_cm.set_packet_queue(slot_q)

    elif conn_type == "MESHCORE":
        #  MeshCore slot 
        if not _HAS_MESHCORE:
            raise HTTPException(
                503,
                "MeshCore support not available  install the library: "
                "pip install meshcore --break-system-packages"
            )
        mc_transport = (req.meshcore_transport or "serial").lower().strip()
        if mc_transport not in ("serial", "tcp", "ble"):
            raise HTTPException(400, f"Invalid MeshCore transport '{mc_transport}'. Use 'serial', 'tcp', or 'ble'.")

        # Validate required fields per transport
        if mc_transport == "serial" and not req.meshcore_serial_port:
            raise HTTPException(400, "meshcore_serial_port is required for serial transport.")
        if mc_transport == "tcp" and not req.meshcore_host:
            raise HTTPException(400, "meshcore_host is required for TCP transport.")
        # BLE: MAC is optional (will scan if absent)

        mc_params = {
            "MESHTASTIC_CONNECTION_TYPE": "MESHCORE",
            "MESHCORE_TRANSPORT":    mc_transport,
            "MESHCORE_SERIAL_PORT":  req.meshcore_serial_port or "",
            "MESHCORE_BAUD":         str(req.meshcore_baud    or 115200),
            "MESHCORE_HOST":         req.meshcore_host        or "",
            "MESHCORE_PORT":         str(req.meshcore_port    or 4000),
            "MESHCORE_BLE_MAC":      req.meshcore_ble_mac     or "",
            "MESHCORE_BLE_PIN":      req.meshcore_ble_pin     or "",
            "MESHCORE_LABEL":        req.label,
        }

        slot_cm = MeshCoreConnectionManager(
            slot_md,
            logging.getLogger(f"MeshCoreConnection.{slot_id}"),
            connection_params=mc_params,
            slot_id=slot_id,
        )
        slot_cm.set_packet_queue(slot_q)

    else:
        #  Serial / TCP / BLE / WebSerial slot (existing logic unchanged) 
        connection_params = {
            "MESHTASTIC_CONNECTION_TYPE": conn_type,
            "MESHTASTIC_HOST":        req.host or DEFAULT_TARGET_HOST,
            "MESHTASTIC_PORT":        str(req.port or 4403),
            "MESHTASTIC_SERIAL_PORT": req.serial_port or "",
            "MESHTASTIC_BLE_MAC":     req.ble_mac or "",
        }
        slot_cm = MeshtasticConnectionManager(
            slot_md,
            logging.getLogger(f"MeshConnection.{slot_id}"),
            connection_params=connection_params,
            slot_id=slot_id,
        )

    slot = NodeSlot(
        slot_id=slot_id,
        label=req.label,
        meshtastic_data=slot_md,
        db_manager=slot_db,
        connection_manager=slot_cm,
        packet_queue=slot_q,
        db_uuid=db_uuid,
    )
    g.NODE_REGISTRY[slot_id] = slot

    def _make_slot_receive(s: NodeSlot):
        def _receive(packet, interface):
            on_receive(packet, interface)
        return _receive

    slot_cm.register_callbacks(
        _make_slot_receive(slot),
        _make_slot_on_connection(slot),
        _make_slot_on_node_updated(slot),
    )

    def _handle_slot_task(task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Slot %s task crashed: %s", slot_id, e, exc_info=True)
        finally:
            slot.tasks.discard(task)

    for coro_fn in (slot_cm.connect_loop, lambda: _packet_processing_worker_for_slot(slot)):
        t = asyncio.create_task(coro_fn())
        t.set_name(f"Task-{slot_id}-{coro_fn.__name__ if hasattr(coro_fn, '__name__') else 'worker'}")
        slot.tasks.add(t)
        t.add_done_callback(_handle_slot_task)

    logger.info(" Slot '%s' created (%s / %s)", slot_id, req.label, conn_type)
    _save_slots_file()
    return {"status": "created", "slot_id": slot_id, "label": req.label}


@router.delete("/api/slots/{slot_id}")
async def delete_slot(
    request: Request,
    slot_id: str,
    delete_db: bool = False,
    user: User = Depends(verify_csrf),
):
    """
    Remove a radio slot.

    delete_db=false (default): disconnect and remove from registry.
      The database file is kept on disk. If you reconnect the same
      radio in a new slot, its history will be gone from the UI but
      the file still exists and could be manually recovered.

    delete_db=true: disconnect, remove from registry, AND permanently
      delete the slot's SQLite database file. All messages, nodes,
      positions and telemetry for this radio are destroyed. This cannot
      be undone.
    """
    if slot_id == "node_0":
        raise HTTPException(400, "Cannot remove the primary slot (node_0).")

    slot = g.NODE_REGISTRY.pop(slot_id, None)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")

    # Resolve the DB file path before teardown (slot.g.db_manager.db_path is the source of truth)
    _db_file: Optional[str] = None
    if not g.PUBLIC_MODE and slot.db_uuid:
        _db_file = f"meshtastic_data_{slot.db_uuid}.db"
    elif not g.PUBLIC_MODE:
        _db_file = getattr(slot.g.db_manager, "db_path", None)
        if _db_file == ":memory:":
            _db_file = None

    _save_slots_file()
    logger.info("??  Slot '%s' removed from registry (delete_db=%s).", slot_id, delete_db)

    async def _teardown():
        tasks_to_cancel = list(slot.tasks)
        for t in tasks_to_cancel:
            t.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        try:
            await asyncio.wait_for(slot.g.connection_manager.shutdown(), timeout=5.0)
        except Exception:
            pass

        if delete_db and _db_file:
            # Give the DatabaseManager a moment to finish any in-flight writes
            await asyncio.sleep(0.5)
            try:
                if os.path.exists(_db_file):
                    os.remove(_db_file)
                    # Also remove SQLite WAL/SHM sidecar files if present
                    for _suf in ("-wal", "-shm"):
                        _side = _db_file + _suf
                        if os.path.exists(_side):
                            os.remove(_side)
                    logger.info("??  Database file deleted: %s", _db_file)
                else:
                    logger.warning("?  DB file not found for deletion: %s", _db_file)
            except OSError as _del_err:
                logger.error(" Failed to delete DB file %s: %s", _db_file, _del_err)

    asyncio.create_task(_teardown())
    return {
        "status":        "removed",
        "slot_id":       slot_id,
        "db_deleted":    delete_db,
        "db_file":       _db_file,
    }


@router.delete("/api/slots/{slot_id}/db")
async def purge_slot_db(slot_id: str, user: User = Depends(verify_csrf)):
    """Delete a non-primary slot's database file. Stops the slot first."""
    if slot_id == "node_0":
        raise HTTPException(400, "Cannot purge the primary slot database.")

    slot = g.NODE_REGISTRY.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")

    # Resolve the DB file path
    db_file: Optional[str] = None
    if not g.PUBLIC_MODE and slot.db_uuid:
        db_file = f"meshtastic_data_{slot.db_uuid}.db"
    elif not g.PUBLIC_MODE:
        db_file = getattr(slot.g.db_manager, "db_path", f"meshtastic_data_{slot_id}.db")

    if not db_file or db_file == ":memory:":
        raise HTTPException(400, "No persistent database file to purge.")

    # Stop the slot first
    tasks_to_cancel = list(slot.tasks)
    for t in tasks_to_cancel:
        t.cancel()
    if tasks_to_cancel:
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    try:
        await asyncio.wait_for(slot.g.connection_manager.shutdown(), timeout=5.0)
    except Exception:
        pass

    # Close the database manager connection
    try:
        slot.g.db_manager.close()
    except Exception:
        pass

    # Delete the database file and sidecar files
    deleted = False
    await asyncio.sleep(0.5)  # Give DB a moment to finish writes
    for suffix in ("", "-wal", "-shm"):
        f = db_file + suffix
        try:
            if os.path.exists(f):
                os.remove(f)
                deleted = True
        except OSError as e:
            logger.warning("Failed to delete %s: %s", f, e)

    logger.info("🗑️ Purged DB for slot '%s': %s (deleted=%s)", slot_id, db_file, deleted)
    return {"status": "purged", "slot_id": slot_id, "db_file": db_file, "deleted": deleted}


@router.get("/api/slots/{slot_id}/db_info")
async def slot_db_info(slot_id: str, user: User = Depends(get_current_active_user)):
    """
    Return information about a slot's database file.
    Used by the UI to display DB size and existence before the delete confirmation.
    """
    if slot_id == "node_0":
        # Primary slot  DB path comes from config
        _db_file = DB_PATH
    else:
        slot = g.NODE_REGISTRY.get(slot_id)
        if not slot:
            raise HTTPException(404, f"Slot '{slot_id}' not found.")
        if g.PUBLIC_MODE:
            return {"slot_id": slot_id, "db_file": ":memory:", "exists": True,
                    "size_bytes": 0, "size_mb": 0.0, "delete_supported": False}
        if slot.db_uuid:
            _db_file = f"meshtastic_data_{slot.db_uuid}.db"
        else:
            _db_file = getattr(slot.g.db_manager, "db_path", f"meshtastic_data_{slot_id}.db")

    _exists = os.path.exists(_db_file)
    _size_bytes = 0
    if _exists:
        try:
            _size_bytes = os.path.getsize(_db_file)
        except OSError:
            pass

    return {
        "slot_id":         slot_id,
        "db_file":         _db_file,
        "exists":          _exists,
        "size_bytes":      _size_bytes,
        "size_mb":         round(_size_bytes / (1024 * 1024), 2),
        "delete_supported": slot_id != "node_0" and not g.PUBLIC_MODE,
    }


@router.get("/api/slots/{slot_id}/status")
async def slot_status(slot_id: str, user: User = Depends(get_current_active_user)):
    slot = g.NODE_REGISTRY.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")
    _md = slot.g.meshtastic_data
    _cm = slot.g.connection_manager
    # For MQTT: expose my_node_id so frontend knows our identity even in observer mode
    my_node_id = None
    if hasattr(_cm, 'my_node_id') and _cm.my_node_id:
        my_node_id = f"!{_cm.my_node_id:08x}"
    elif _md.local_node_id:
        my_node_id = _md.local_node_id
    return {
        "slot_id": slot_id,
        "label": slot.label,
        "connection_status": _md.connection_status,
        "connection_state": _md._connection_state or "",
        "connection_detail": _md._connection_detail or "",
        "connection_transport": _md._connection_transport or "",
        "is_ready": _cm.is_ready.is_set(),
        "local_node_id": _md.local_node_id,
        "my_node_id": my_node_id,
        "local_node_info": _md.local_node_info,
        "node_count": len(_md.nodes),
        "nodes": ensure_serializable(_md.nodes),
        "stats": _md.get_serializable_stats(),
    }


@router.api_route("/sse/all", methods=["GET", "HEAD", "POST"])
async def sse_all(request: Request):
    """Multiplexed SSE stream  receives events from every connected slot.
    Used by the frontend when slot_id === 'all'.
    On connect, immediately sends merged nodes from all slots so the UI
    populates without waiting for the next broadcast cycle.
    """
    global _sse_client_id
    async with g.all_sse_queues_lock:
        if len(g.all_sse_queues) >= g.MAX_SSE_CLIENTS:
            raise HTTPException(503, "SSE client limit reached for all-mode stream.")
        _sse_client_id += 1
        cid = _sse_client_id
        q: asyncio.Queue = asyncio.Queue(maxsize=500)  # larger buffer for multi-slot volume
        g.all_sse_queues[cid] = q

    async def gen():
        try:
            # Send connection status first (small, instant)
            cs = g.meshtastic_data
            yield {"event": "connection_status", "data": json.dumps({"state": cs._connection_state or "idle", "detail": cs._connection_detail or "", "transport": cs._connection_transport or "", "label": cs.connection_status or "Unknown"})}
            # Merge nodes from all slots, then stream in chunks
            merged_nodes: Dict[str, Any] = {}
            for sid, s in g.NODE_REGISTRY.items():
                for nid, ndata in s.g.meshtastic_data.nodes.items():
                    node = dict(ndata)
                    node["heard_by_slot"] = sid
                    merged_nodes[nid] = node
            all_nodes = list(merged_nodes.values())
            _CHUNK = 500
            if len(all_nodes) <= _CHUNK:
                yield {"event": "nodes", "data": json.dumps(ensure_serializable(all_nodes))}
            else:
                yield {"event": "nodes", "data": json.dumps(ensure_serializable(all_nodes[:_CHUNK]))}
                for offset in range(_CHUNK, len(all_nodes), _CHUNK):
                    chunk = all_nodes[offset:offset + _CHUNK]
                    yield {"event": "node_batch", "data": json.dumps(ensure_serializable(chunk))}
                    await asyncio.sleep(0)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield payload
                    q.task_done()
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("SSE all-mode error: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            async with g.all_sse_queues_lock:
                g.all_sse_queues.pop(cid, None)
            logger.debug("SSE all-mode client %s disconnected. Active: %d", cid, len(g.all_sse_queues))

    return EventSourceResponse(gen(), ping=15)


@router.api_route("/sse/{slot_id}", methods=["GET", "HEAD", "POST"])
async def sse_slot(request: Request, slot_id: str):
    slot = g.NODE_REGISTRY.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found.")

    global _sse_client_id
    async with slot.sse_lock:
        if len(slot.g.sse_queues) >= g.MAX_SSE_CLIENTS:
            raise HTTPException(503, f"SSE client limit reached for slot '{slot_id}'.")
        _sse_client_id += 1
        cid = _sse_client_id
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        slot.g.sse_queues[cid] = q

    async def gen():
        try:
            cs = slot.g.meshtastic_data
            _cm = slot.g.connection_manager
            _my_node_id = None
            if hasattr(_cm, 'my_node_id') and _cm.my_node_id:
                _my_node_id = f"!{_cm.my_node_id:08x}"
            elif cs.local_node_id:
                _my_node_id = cs.local_node_id
            # 1) Small metadata events first — arrive instantly so the UI
            #    populates stats/identity before the big node dump.
            yield {"event": "connection_status", "data": json.dumps({"state": cs._connection_state or "idle", "detail": cs._connection_detail or "", "transport": cs._connection_transport or "", "label": cs.connection_status or "Unknown"})}
            yield {"event": "stats", "data": json.dumps(slot.g.meshtastic_data.get_serializable_stats())}
            # Send local_node_info burst so frontend knows our identity immediately
            if cs.local_node_info or _my_node_id:
                info = cs.local_node_info or {}
                info.setdefault('node_id', cs.local_node_id or _my_node_id)
                yield {"event": "local_node_info", "data": json.dumps(ensure_serializable(info))}
            # 2) Yield control so the small events flush to the client before
            #    we start serialising potentially thousands of nodes.
            await asyncio.sleep(0)
            # 3) Stream nodes in chunks so the browser can render progressively.
            #    First chunk: full replace ("nodes" event).
            #    Subsequent chunks: incremental merge ("node_batch" event).
            _CHUNK = 500
            all_nodes = list(slot.g.meshtastic_data.nodes.values())
            if len(all_nodes) <= _CHUNK:
                yield {"event": "nodes", "data": json.dumps(ensure_serializable(all_nodes))}
            else:
                # First chunk — full replace
                yield {"event": "nodes", "data": json.dumps(ensure_serializable(all_nodes[:_CHUNK]))}
                # Remaining chunks — merge
                for offset in range(_CHUNK, len(all_nodes), _CHUNK):
                    chunk = all_nodes[offset:offset + _CHUNK]
                    yield {"event": "node_batch", "data": json.dumps(ensure_serializable(chunk))}
                    # Yield between chunks so the browser can paint
                    await asyncio.sleep(0)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield payload
                    q.task_done()
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("SSE slot %s error: %s", slot_id, e)
        except asyncio.CancelledError:
            pass
        finally:
            async with slot.sse_lock:
                slot.g.sse_queues.pop(cid, None)

    return EventSourceResponse(gen(), ping=15)


