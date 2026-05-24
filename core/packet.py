import core.globals as g
from core.c2 import send_system_message, send_system_message_sync
from core.broadcast import broadcast_data, broadcast_stats, broadcast_stats_for_slot, _resolve_slot_id_for_interface
from core.sync import _perform_background_sync_for_slot
# Auto-extracted from meshtastic_dashboard.py
import datetime
from core.routes.schemas import NodeSlot
from pubsub import pub
import logging
import asyncio

logger = logging.getLogger(__name__)
rx_logger = logging.getLogger("rx_logger")
rx_logger.setLevel(logging.INFO)
rx_logger.propagate = False

async def _packet_processing_worker_for_slot(slot: "NodeSlot"):
    """Consumer for a single slot's packet queue."""
    slot_logger = logging.getLogger(f"worker.{slot.slot_id}")
    slot_logger.info("?  Packet Worker started for slot '%s'", slot.slot_id)
    while True:
        packet = await slot.packet_queue.get()
        try:
            # Stamp the originating slot so the UI knows which radio heard this packet
            if isinstance(packet, dict):
                packet.setdefault("slot_id", slot.slot_id)
                packet.setdefault("heard_by_slot", slot.slot_id)
            processed = await asyncio.to_thread(slot.g.meshtastic_data.add_packet, packet)
            if processed:
                await broadcast_data({"event": "packet", "data": processed}, slot_id=slot.slot_id)
                await broadcast_stats_for_slot(slot)
                
                # Auto-Reply Plugin Integration
                # If the auto_reply plugin is installed and loaded, it exposes its functions
                # through PluginManager.contexts["auto_reply_plugin"]
                try:
                    from meshtastic_dashboard import PluginManager as _PM
                    _ar_plugin = _PM.contexts.get("auto_reply_plugin")
                except Exception:
                    _ar_plugin = None
                if processed.get("app_packet_type") == "Message" and _ar_plugin is not None:
                    if _ar_plugin and _ar_plugin.get("is_enabled", lambda: False)():
                        _ar_decoded = processed.get("decoded") or {}
                        _ar_text    = _ar_decoded.get("text") or _ar_decoded.get("payload")
                        if isinstance(_ar_text, (bytes, bytearray)):
                            try:
                                _ar_text = _ar_text.decode("utf-8", errors="replace")
                            except Exception:
                                _ar_text = None
                        _ar_sender  = processed.get("fromId")
                        _ar_chan    = processed.get("channel", 0)
                        _ar_to_id  = processed.get("toId") or ""
                        _local_id  = slot.g.meshtastic_data.local_node_id or ""
                        _is_dm     = bool(_ar_to_id and _ar_to_id == _local_id)

                        # Build the set of our own node IDs (self-reply guard)
                        _local_ids: set = set()
                        if _local_id:
                            _local_ids.add(_local_id)
                        for _n in slot.g.meshtastic_data.nodes.values():
                            if (_n.get("isLocal") or _n.get("is_local")) and _n.get("node_id"):
                                _local_ids.add(_n["node_id"])

                        if _ar_sender and _ar_text:
                            _get_rules = _ar_plugin.get("get_rules")
                            _check_msg = _ar_plugin.get("check_message")
                            _replace_ph = _ar_plugin.get("replace_placeholders")
                            if _get_rules and _check_msg:
                                _ar_rules = await asyncio.to_thread(_get_rules, only_enabled=True)
                                _ar_replies = _check_msg(
                                    _ar_text,
                                    _ar_sender,
                                    _ar_chan,
                                    _ar_rules,
                                    slot_id=slot.slot_id,
                                    local_node_ids=_local_ids,
                                    is_direct_message=_is_dm,
                                )
                                for _ar_r in _ar_replies:
                                    _node_info = slot.g.meshtastic_data.nodes.get(_ar_sender) or {}
                                    _ar_msg = _replace_ph(_ar_r["message"], _node_info) if _replace_ph else _ar_r["message"]
                                    # Add prefix for channel replies (e.g., "@!nodeId ")
                                    _reply_prefix = _ar_r.get("reply_prefix", "")
                                    if _reply_prefix:
                                        _ar_msg = _reply_prefix + _ar_msg
                                    _reply_channel = _ar_r.get("channel", 0)
                                    _is_dm_reply = _ar_r.get("is_dm_reply", True)
                                    slot_logger.info(
                                        "? AUTO-REPLY [plugin] rule=%d to=%s slot=%s ch=%d dm=%s: %s",
                                        _ar_r.get("rule_id", 0),
                                        _ar_sender,
                                        slot.slot_id,
                                        _reply_channel,
                                        _is_dm_reply,
                                        _ar_msg[:80],
                                    )
                                    if slot.g.connection_manager:
                                        await slot.g.connection_manager.sendText(
                                            _ar_msg,
                                            destinationId=_ar_r["destination"],
                                            channelIndex=_reply_channel,
                                        )
        except Exception as e:
            slot_logger.error(" Worker Error: %s", e, exc_info=True)
        finally:
            slot.packet_queue.task_done()


async def packet_processing_worker():
    """Legacy single-slot worker  delegates to slot node_0."""
    slot = g.NODE_REGISTRY.get("node_0")
    if slot:
        await _packet_processing_worker_for_slot(slot)
    else:
        logger.error("packet_processing_worker: node_0 slot not found")


def on_fast_rx(packet, interface=None):
    if g.main_event_loop:
        sid = _resolve_slot_id_for_interface(interface)
        g.main_event_loop.call_soon_threadsafe(
            lambda s=sid: asyncio.create_task(broadcast_data({"event": "activity", "data": "RX"}, slot_id=s))
        )


def on_fast_tx(packet, interface):
    if g.main_event_loop:
        sid = _resolve_slot_id_for_interface(interface)
        g.main_event_loop.call_soon_threadsafe(
            lambda s=sid: asyncio.create_task(broadcast_data({"event": "activity", "data": "TX"}, slot_id=s))
        )


def on_receive(packet, interface):
    """Main packet handler  immediately logs then pushes to the correct slot's queue."""
    try:
        try:
            decoded = packet.get("decoded", {})
            from_id = packet.get("fromId", packet.get("from", "Unknown"))
            to_id = packet.get("toId", packet.get("to", "Unknown"))
            portnum = decoded.get("portnum", "UNKNOWN")
            log_type = "OTHER"
            payload_str = "Raw Data"
            if portnum == "TEXT_MESSAGE_APP":
                log_type = "MSG"
                payload_str = decoded.get("text", decoded.get("payload", ""))
            elif portnum == "ROUTING_APP":
                log_type = "ACK"
                payload_str = f"Ack ReqID: {decoded.get('requestId')}"
            elif portnum == "POSITION_APP":
                log_type = "POS"
                pos = decoded.get("position", {})
                payload_str = f"Lat: {pos.get('latitude', 0):.4f}"
            elif portnum == "TELEMETRY_APP":
                log_type = "TLM"
                payload_str = "Telemetry Data"
            log_line = (
                f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[{log_type:<8}] {from_id} -> {to_id} | {payload_str}"
            )
            rx_logger.info(log_line)
        except Exception as log_e:
            logger.error("?  RX Log Error: %s", log_e)

        if g.main_event_loop:
            # Resolve which slot owns this interface
            target_queue = None
            for slot in g.NODE_REGISTRY.values():
                if slot.g.connection_manager and slot.g.connection_manager.interface is interface:
                    target_queue = slot.packet_queue
                    break
            if target_queue is None:
                # Fallback: use node_0 slot's queue
                _node0 = g.NODE_REGISTRY.get("node_0")
                if _node0:
                    target_queue = _node0.packet_queue
            try:
                g.main_event_loop.call_soon_threadsafe(target_queue.put_nowait, packet)
            except asyncio.QueueFull:
                logger.warning("?  Packet queue full for slot  dropping packet.")
    except Exception as e:
        logger.error(" CRITICAL ERROR in on_receive: %s", e, exc_info=True)


def _make_slot_on_connection(slot: "NodeSlot"):
    """Returns a connection callback bound to the given slot."""
    def _on_connection(interface, topic=pub.AUTO_TOPIC):
        tname = topic.getName() if hasattr(topic, "getName") else str(topic)
        logger.info("? Connection Event [%s]: %s", slot.slot_id, tname)

        if "established" in tname.lower():
            logger.info(" Transport link established [%s].", slot.slot_id)
            try:
                if hasattr(interface, "myInfo") and interface.myInfo and not callable(interface.myInfo):
                    slot.g.meshtastic_data.set_local_node_info(interface.myInfo)
                    my_node_id = f"!{interface.myInfo.my_node_num:08x}"
                    hw = "Unknown"
                    if hasattr(interface, "metadata") and interface.metadata and not callable(interface.metadata):
                        hw = getattr(interface.metadata, "hw_model_str", "Unknown")
                    send_system_message_sync(f" <b>[{slot.slot_id}] Link Up:</b> Node {my_node_id} online. HW: {hw}")
            except Exception as e:
                logger.error("Local hydration error in on_connection [%s]: %s", slot.slot_id, e)

            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    _perform_background_sync_for_slot(interface, slot), g.main_event_loop
                )
            else:
                logger.error(" g.main_event_loop not available for background sync [%s].", slot.slot_id)

        elif "lost" in tname.lower():
            logger.warning("?  Transport link lost [%s].", slot.slot_id)
            send_system_message_sync(f"? <b>[{slot.slot_id}] Link Down:</b> Radio disconnected - reconnecting...")

    return _on_connection


def _make_slot_on_node_updated(slot: "NodeSlot"):
    """Returns a node_updated callback bound to the given slot."""
    def _on_node_updated(node, interface):
        if isinstance(node, dict) and "num" in node:
            nid = f"!{node['num']:08x}"
            slot.g.meshtastic_data.update_node(nid, node, broadcast=True)
            # Refresh local_node_info when the local node gets user data
            local_nid = getattr(slot.g.meshtastic_data, 'local_node_id', None) or (
                getattr(slot.g.meshtastic_data, 'local_node_info', None) or {}
            ).get('node_id_hex')
            if nid == local_nid:
                try:
                    from core.data import _role_name, _region_name
                    lni = slot.g.meshtastic_data.local_node_info
                    # Merge node DB data into local_node_info if it exists
                    if lni is not None:
                        node_entry = slot.g.meshtastic_data.nodes.get(nid, {})
                        user_data = node_entry.get('user', {})
                        refreshed = False
                        if isinstance(user_data, dict) and user_data.get('longName'):
                            lni['long_name'] = user_data.get('longName', lni.get('long_name', 'Unknown'))
                            lni['short_name'] = user_data.get('shortName', lni.get('short_name', 'N/A'))
                            lni['macaddr'] = user_data.get('macaddr', lni.get('macaddr', 'Unknown'))
                            refreshed = True
                        # Also try to refresh role/region from localConfig
                        if lni.get('role') in ('Unknown', None) or lni.get('region') in ('Unknown', None):
                            _iface = getattr(slot.g.connection_manager, 'interface', None)
                            if _iface:
                                _local_node = getattr(_iface, 'localNode', None)
                                if _local_node:
                                    _lc = getattr(_local_node, 'localConfig', None)
                                    if _lc:
                                        if lni.get('role') in ('Unknown', None) and hasattr(_lc, 'device'):
                                            lni['role'] = _role_name(getattr(_lc.device, 'role', 'CLIENT'))
                                        if lni.get('region') in ('Unknown', None) and hasattr(_lc, 'lora'):
                                            lni['region'] = _region_name(_lc.lora.region)
                        if refreshed or lni.get('role') not in ('Unknown', None) or lni.get('region') not in ('Unknown', None):
                            if g.main_event_loop:
                                asyncio.run_coroutine_threadsafe(
                                    broadcast_data({"event": "local_node_info", "data": lni}, slot_id=slot.slot_id),
                                    g.main_event_loop,
                                )
                except Exception as e:
                    logger.debug(f"Could not refresh local_node_info from node update: {e}")
    return _on_node_updated


def on_connection(interface, topic=pub.AUTO_TOPIC):
    """Legacy connection handler for node_0  delegates to slot-aware implementation."""
    slot = g.NODE_REGISTRY.get("node_0")
    if slot:
        _make_slot_on_connection(slot)(interface, topic)
    else:
        # Fallback for very early startup before g.NODE_REGISTRY is populated
        tname = topic.getName() if hasattr(topic, "getName") else str(topic)
        logger.info(f"? Connection Event: {tname}")
        if "established" in tname.lower():
            logger.info(" Transport link established.")
            try:
                if hasattr(interface, "myInfo") and interface.myInfo and not callable(interface.myInfo):
                    g.meshtastic_data.set_local_node_info(interface.myInfo)
                    my_node_id = f"!{interface.myInfo.my_node_num:08x}"
                    hw = "Unknown"
                    if hasattr(interface, "metadata") and interface.metadata and not callable(interface.metadata):
                        hw = getattr(interface.metadata, "hw_model_str", "Unknown")
                    send_system_message_sync(f" <b>Link Up:</b> Node {my_node_id} online. HW: {hw}")
            except Exception as e:
                logger.error(f"Local hydration error in on_connection: {e}")
            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(perform_background_sync(interface), g.main_event_loop)
            else:
                logger.error(" g.main_event_loop not available for background sync.")
        elif "lost" in tname.lower():
            logger.warning("?  Transport link lost event received.")
            send_system_message_sync("? <b>Link Down:</b> Radio disconnected - reconnecting...")


def on_node_updated(node, interface):
    """Legacy node_updated handler for node_0."""
    slot = g.NODE_REGISTRY.get("node_0")
    if slot:
        _make_slot_on_node_updated(slot)(node, interface)
    elif isinstance(node, dict) and "num" in node:
        nid = f"!{node['num']:08x}"
        g.meshtastic_data.update_node(nid, node, broadcast=True)


