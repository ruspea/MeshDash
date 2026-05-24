import core.globals as g
import logging
# Auto-extracted from meshtastic_dashboard.py
from core.routes.schemas import NodeSlot
import asyncio

logger = logging.getLogger(__name__)

async def _perform_background_sync_for_slot(interface, slot: "NodeSlot"):
    """Slot-aware background node database hydration. Works for any slot including node_0."""
    sid = slot.slot_id
    _md = slot.g.meshtastic_data
    _cm = slot.g.connection_manager

    # Per-slot lock so two slots can sync concurrently without blocking each other
    if sid not in _slot_sync_locks:
        _slot_sync_locks[sid] = asyncio.Lock()
    lock = _slot_sync_locks[sid]

    if lock.locked():
        logger.info(" Background sync already running for slot '%s'  skipping.", sid)
        return

    async with lock:
        logger.info("? Background Sync [%s]: Starting node database hydration...", sid)
        await broadcast_data({"event": "sync_status", "data": {"is_syncing": True, "current": "Initializing..."}}, slot_id=sid)
        await asyncio.sleep(2.0)

        synced = False
        current_nodes = []
        try:
            for attempt in range(10):
                if not _cm.is_ready.is_set():
                    logger.warning("? Sync [%s] aborted  connection lost during sync.", sid)
                    return

                current_nodes = list(interface.nodes.items()) if hasattr(interface, "nodes") else []
                if current_nodes:
                    await broadcast_data({
                        "event": "sync_status",
                        "data": {"is_syncing": True, "current": f"Syncing {len(current_nodes)} nodes..."},
                    }, slot_id=sid)
                    # Run all node writes in ONE thread call to avoid inter-thread
                    # WAL contention.  91 sequential asyncio.to_thread() calls
                    # each open a separate thread-local DB connection; with the
                    # MQTT packet worker also writing, this causes "database is
                    # locked" errors.  A single thread call serialises everything.
                    def _batch_write_nodes(nodes_snapshot, md=_md):
                        count = 0
                        for key, node_data in nodes_snapshot:
                            try:
                                node_num = None
                                if isinstance(node_data, dict) and isinstance(node_data.get("num"), int):
                                    node_num = node_data["num"]
                                elif isinstance(key, int):
                                    node_num = key
                                elif isinstance(key, str) and key.startswith("!"):
                                    try:
                                        node_num = int(key[1:], 16)
                                    except ValueError:
                                        pass
                                if node_num is None:
                                    continue
                                nid = f"!{node_num:08x}"
                                if isinstance(node_data, dict):
                                    node_data["num"] = node_num
                                md.update_node(nid, node_data, False)
                                count += 1
                            except Exception as node_err:
                                logger.debug("Sync [%s]: skipped node %s: %s", sid, key, node_err)
                        return count

                    count_ok = await asyncio.to_thread(_batch_write_nodes, current_nodes)

                    if count_ok > 0:
                        synced = True
                        logger.info(" Sync [%s] pass %d: wrote %d nodes.", sid, attempt + 1, count_ok)
                        # Broadcast the full node list to this slot's SSE clients
                        await broadcast_data(
                            {"event": "nodes", "data": list(_md.nodes.values())},
                            slot_id=sid,
                        )
                        if attempt >= 1:
                            break
                await asyncio.sleep(1.0)

            if not synced or len(current_nodes) < 2:
                if _cm.is_ready.is_set() and hasattr(interface, "requestNodes"):
                    logger.info("? [%s] Node list empty/small  requesting fresh NodeInfo.", sid)
                    interface.requestNodes()
                    await send_system_message(f"? [{sid}] Requesting fresh NodeInfo from the mesh...")
        except Exception as exc:
            logger.error(" Background sync error [%s]: %s", sid, exc, exc_info=True)
        finally:
            await broadcast_data({"event": "sync_status", "data": {"is_syncing": False, "current": "Connected"}}, slot_id=sid)
            logger.info(" Background Sync [%s]: Complete.", sid)


async def perform_background_sync(interface):
    """Legacy wrapper  routes to the slot-aware implementation for node_0."""
    slot = g.NODE_REGISTRY.get("node_0")
    if slot:
        await _perform_background_sync_for_slot(interface, slot)
    else:
        # Fallback for early-startup before g.NODE_REGISTRY is populated
        if sync_lock.locked():
            logger.info(" Background sync already running - skipping duplicate.")
            return
        async with sync_lock:
            logger.info("? Background Sync: Starting node database hydration...")
            await broadcast_data({"event": "sync_status", "data": {"is_syncing": True, "current": "Initializing..."}})
            await asyncio.sleep(2.0)
            try:
                current_nodes = list(interface.nodes.items()) if hasattr(interface, "nodes") else []
                def _batch_write_legacy(nodes_snapshot):
                        count = 0
                        for key, node_data in nodes_snapshot:
                            try:
                                node_num = None
                                if isinstance(node_data, dict) and isinstance(node_data.get("num"), int):
                                    node_num = node_data["num"]
                                elif isinstance(key, int):
                                    node_num = key
                                elif isinstance(key, str) and key.startswith("!"):
                                    try: node_num = int(key[1:], 16)
                                    except ValueError: pass
                                if node_num is None: continue
                                nid = f"!{node_num:08x}"
                                if isinstance(node_data, dict): node_data["num"] = node_num
                                g.meshtastic_data.update_node(nid, node_data, False)
                                count += 1
                            except Exception as node_err:
                                logger.debug("Sync: skipped node %s: %s", key, node_err)
                        return count
                count_ok = await asyncio.to_thread(_batch_write_legacy, current_nodes)
                if count_ok > 0:
                    logger.info(" Sync pass 1: wrote %d nodes.", count_ok)
            except Exception as exc:
                logger.error(" Background sync error: %s", exc, exc_info=True)
            finally:
                await broadcast_data({"event": "sync_status", "data": {"is_syncing": False, "current": "Connected"}})


def _remove_keys_from_config(keys):
    try:
        with open(CONFIG_FILE_PATH, "r") as f:
            lines = f.readlines()
        with open(CONFIG_FILE_PATH, "w") as f:
            for line in lines:
                if not any(k in line for k in keys):
                    f.write(line)
    except Exception:
        pass


