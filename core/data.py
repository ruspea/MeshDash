from typing import Dict, List, Optional, Set, Tuple, Any
import core.globals as g
from core.database import DatabaseManager
from core.connections import ConnectionState
from core.broadcast import broadcast_data
from core.auth import ensure_serializable
from core.evidence import _get_node_rf_history, detect_packet_source, _update_node_source_evidence
import base64, time, json, asyncio, logging
from collections import deque

try:
    from meshtastic.protobuf import portnums_pb2
except ImportError:
    try:
        from meshtastic import portnums_pb2
    except ImportError:
        portnums_pb2 = None

logger = logging.getLogger(__name__)

# Auto-extracted from meshtastic_dashboard.py

# LoRa region enum mapping
_REGION_MAP = {
    0: "UNSET", 1: "US", 2: "EU_433", 3: "EU_868", 4: "CN", 5: "JP",
    6: "ANZ", 7: "KR", 8: "TW", 9: "RU", 10: "IN", 11: "NZ_865",
    12: "TH", 13: "LORA_24", 14: "UA_433", 15: "UA_868", 16: "MY_433",
    17: "MY_919", 18: "SG_923", 19: "PH_433", 20: "PH_868", 21: "PH_915",
    22: "ANZ_433", 23: "KZ_433", 24: "KZ_863", 25: "NP_865", 26: "BR_902",
}

def _region_name(val):
    """Convert a region enum value (int or str) to a human-readable name."""
    try:
        return _REGION_MAP.get(int(val), str(val))
    except (ValueError, TypeError):
        return str(val)


_ROLE_MAP = {
    0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT",
    4: "REPEATER", 5: "TRACKER", 6: "SENSOR", 7: "TAK",
    8: "CLIENT_HIDDEN", 9: "LOST_AND_FOUND", 10: "TAK_TRACKER",
    11: "ROUTER_LATE", 12: "CLIENT_BASE",
}


def _role_name(val):
    """Convert a role enum value to a human-readable name."""
    try:
        return _ROLE_MAP.get(int(val), str(val))
    except (ValueError, TypeError):
        return str(val) if val else "Unknown"


class MeshtasticData:
    def __init__(self, db: DatabaseManager, max_packets: int, slot_id: str = "node_0"):
        self.db = db
        self.slot_id = slot_id
        self.packets: deque = deque(maxlen=max_packets)
        # Ephemeral DB starts empty  never load historical nodes from disk
        self.nodes: Dict[str, Dict] = {} if db.ephemeral else self.db.get_all_nodes()
        self.local_node_id: Optional[str] = None
        self.local_node_info: Optional[Dict] = None
        self.connection_status: str = "Initializing"
        self._connection_state: Optional[str] = None   # ConnectionState value string
        self._connection_detail: str = ""               # Human-readable detail string
        self._connection_transport: str = ""            # Active transport label
        self.last_error: Optional[str] = None
        self.channel_map: Dict = {}
        self.stats: Dict = {
            "packets_received_session": 0,
            "text_messages_session": 0,
            "position_updates_session": 0,
            "telemetry_reports_session": 0,
            "user_info_updates_session": 0,
            "waypoint_updates_session": 0,
            "other_packets_session": 0,
            "start_time": time.time(),
            "nodes_seen_session": set(self.nodes.keys()),
            "channels_seen_session": set(),
        }
        self.packet_counter: int = 0

    def add_packet(self, packet: Dict) -> Optional[Dict]:
        if not packet or not isinstance(packet, dict):
            return None
        processed = packet.copy()
        if "decoded" not in processed or not isinstance(processed["decoded"], dict):
            processed["decoded"] = {}
        if "id" in processed:
            processed["decoded"]["mesh_packet_id"] = processed["id"]

        processed["rxTime"] = processed.get("rxTime", int(time.time()))
        processed["timestamp"] = float(processed["rxTime"])
        if not processed.get("event_id"):
            processed["event_id"] = f"pkt_{time.time_ns()}_{self.packet_counter}"
        self.packet_counter += 1

        if "fromId" not in processed:
            val = processed.get("from")
            if val is not None and val != 0:
                processed["fromId"] = f"!{val:08x}" if isinstance(val, int) else str(val)
            elif val == 0:
                # from=0 means unset in Meshtastic — leave fromId absent rather than !00000000
                processed["fromId"] = None

        if "toId" not in processed:
            val = processed.get("to")
            if val is not None:
                if isinstance(val, int):
                    # 0xFFFFFFFF or 0 both mean broadcast in Meshtastic
                    processed["toId"] = "^all" if val in (4294967295, 0) else f"!{val:08x}"
                else:
                    processed["toId"] = str(val)

        if "original_channel_id" not in processed:
            processed["original_channel_id"] = processed.get("channel")
        if processed.get("channel") is None:
            processed["channel"] = 0

        if "rxSnr" in processed:
            processed["decoded"]["_snr"] = processed["rxSnr"]
        if "rxRssi" in processed:
            processed["decoded"]["_rssi"] = processed["rxRssi"]
        if "hopLimit" in processed:
            processed["decoded"]["_hopLimit"] = processed["hopLimit"]

        ptype, payload = self._classify(processed)
        processed["app_packet_type"] = ptype

        from_id = processed.get("fromId")
        
        # Get this node's RF history before detection
        node_history = _get_node_rf_history(from_id) if from_id else {}
        
        # Run full source detection
        source, confidence, reasons = detect_packet_source(processed, node_history)
        processed["source"] = source
        processed["source_confidence"] = round(confidence, 3)
        processed["source_reasons"] = reasons  # stored for debugging, not in DB
        
        # Update per-node evidence cache for future packets
        if from_id:
            _update_node_source_evidence(
                from_id, source,
                snr=processed.get("rxSnr"),
                rssi=processed.get("rxRssi"),
            )

        self.stats["packets_received_session"] += 1
        if processed.get("fromId"):
            self.stats["nodes_seen_session"].add(processed["fromId"])
        if processed.get("channel") is not None:
            self.stats["channels_seen_session"].add(processed["channel"])

        # Per-type session stats
        stat_map = {
            "Message":       "text_messages_session",
            "Position":     "position_updates_session",
            "Telemetry":    "telemetry_reports_session",
            "Node Info":    "user_info_updates_session",
            "Waypoint":     "waypoint_updates_session",
        }
        stat_key = stat_map.get(ptype)
        if stat_key:
            self.stats[stat_key] += 1
        elif ptype not in ("Unknown",) and not str(ptype).startswith("Raw:"):
            self.stats["other_packets_session"] += 1

        processed = ensure_serializable(processed)
        self.packets.append(processed)

        junk_packet_types = ["Unknown"]
        # Save encrypted packets too  we store them with text=NULL so the channels
        # UI can render a [ENCRYPTED] placeholder, and retroactive decryption (once
        # the user supplies a PSK) can still find them in the DB.
        # Only skip truly unclassifiable "Unknown" packets and raw binary blobs.
        if ptype not in junk_packet_types and not str(ptype).startswith("Raw:"):
            self.db.save_packet(processed)

        if processed.get("fromId"):
            sender = processed["fromId"]
            timestamp = processed["rxTime"]
            update: Dict = {
                "lastHeard": timestamp,
                "snr": processed.get("rxSnr"),
                "rssi": processed.get("rxRssi"),
                "source": processed.get("source"),
                "source_confidence": processed.get("source_confidence"),
            }
            if ptype == "Position" and payload:
                update["position"] = payload
                update["position_time"] = timestamp
            elif ptype == "Telemetry" and payload:
                if isinstance(payload, dict):
                    if payload.get("deviceMetrics"):
                        update["deviceMetrics"] = payload["deviceMetrics"]
                    if payload.get("environmentMetrics"):
                        update["environmentMetrics"] = payload["environmentMetrics"]
                update["telemetry_time"] = timestamp
            elif ptype == "Node Info" and isinstance(payload, dict):
                update["user"] = payload
                if "hwModel" in payload:
                    update["hw_model"] = payload["hwModel"]
                if "role" in payload:
                    update["role"] = payload["role"]
            elif ptype == "Neighbor Info" and isinstance(payload, list):
                self.db.save_neighbors(sender, payload)
            elif ptype == "Traceroute" and isinstance(payload, dict):
                route_to   = payload.get("route", [])
                route_back = payload.get("routeBack", [])
                snr_towards = payload.get("snrTowards", [])
                snr_back    = payload.get("snrBack", [])
                self.db.save_traceroute(
                    sender,
                    processed.get("toId", ""),
                    route_to,
                    timestamp,
                    route_back=route_back,
                    snr_towards=snr_towards,
                    snr_back=snr_back,
                    rssi=processed.get("rxRssi"),
                    snr=processed.get("rxSnr"),
                    hops_used=max(0, processed.get("hopStart", 0) - processed.get("hopLimit", 0)),
                )
            elif ptype == "Waypoint" and isinstance(payload, dict):
                self.db.save_waypoint(sender, payload, timestamp)
            elif ptype == "Admin":
                safe_payload = ensure_serializable(payload)
                if isinstance(safe_payload, dict):
                    self.db.log_hardware_event(sender, "Admin", safe_payload, timestamp)
            self.update_node(sender, update, broadcast=True)

        return processed

    def _classify(self, packet: Dict) -> Tuple[str, Any]:
        """Comprehensive packet classifier & extractor."""
        decoded = packet.get("decoded", {})
        if not isinstance(decoded, dict):
            return ("Encrypted" if packet.get("encrypted") else "Unknown"), "Raw Binary"

        pn = decoded.get("portnum")
        port_name = "UNKNOWN_APP"
        if isinstance(pn, int):
            try:
                if portnums_pb2 is not None:
                    port_name = portnums_pb2.PortNum.Name(pn)
                else:
                    port_name = f"APP_{pn}"
            except ValueError:
                port_name = "PRIVATE_APP" if pn >= 256 else f"APP_{pn}"
        elif hasattr(pn, "name"):
            port_name = pn.name
        else:
            port_name = str(pn)

        def get_text(d):
            return d.get("text") or d.get("payload") or d.get("message") or d.get("string")

        if port_name == "TEXT_MESSAGE_APP":
            return "Message", get_text(decoded)
        if port_name == "POSITION_APP" or "position" in decoded:
            return "Position", decoded.get("position", {})
        if port_name == "TELEMETRY_APP" or "telemetry" in decoded:
            return "Telemetry", decoded.get("telemetry", {})
        if port_name == "NODEINFO_APP" or "user" in decoded:
            return "Node Info", decoded.get("user", {})
        if port_name == "NEIGHBOR_INFO_APP" or "neighbor" in decoded:
            return "Neighbor Info", decoded.get("neighbor", {}).get("neighbors", [])
        if port_name == "TRACEROUTE_APP" or "traceroute" in decoded or "routeDiscovery" in decoded:
            tr = decoded.get("traceroute") or decoded.get("routeDiscovery") or {}
            return "Traceroute", tr
        if port_name == "ROUTING_APP":
            req_id = decoded.get("requestId")
            err = decoded.get("errorReason", "NONE")
            if err != "NONE":
                return "Routing Error", f"Reason: {err} (ReqID: {req_id})"
            return "Ack", f"Acknowledgment (ReqID: {req_id})"
        if port_name == "PAXCOUNTER_APP":
            pax = decoded.get("paxcounter", {})
            wifi = pax.get("wifi", 0)
            ble = pax.get("ble", 0)
            return "Paxcounter", {"wifi": wifi, "ble": ble, "total": wifi + ble}
        if port_name == "DETECTION_SENSOR_APP":
            return "Detection", decoded.get("detection", {})
        if port_name == "RANGE_TEST_APP":
            return "Range Test", {"seq": decoded.get("seqNumber"), "src": decoded.get("payload"), "snr": packet.get("rxSnr")}
        if port_name == "SERIAL_APP":
            return "Serial", f"Binary Payload ({len(decoded.get('payload', ''))} bytes)"
        if port_name == "STORE_FORWARD_APP":
            return "Store & Forward", f"History Stats: {decoded.get('history', {}).get('stats', {})}"
        if port_name == "WAYPOINT_APP" or "waypoint" in decoded:
            return "Waypoint", decoded.get("waypoint", {})
        if port_name == "ADMIN_APP" or "admin" in decoded:
            return "Admin", decoded.get("admin", {})

        txt = get_text(decoded)
        if txt and isinstance(txt, str) and len(txt) > 0:
            return "Message", txt

        try:
            return f"Raw: {port_name}", json.dumps(decoded, default=str)
        except Exception:
            return f"Raw: {port_name}", str(decoded)

    def update_node(self, node_id: str, data: Dict, broadcast: bool = False):
        if node_id not in self.nodes:
            self.nodes[node_id] = {"node_id": node_id}

        def merge(target, source):
            for k, v in source.items():
                if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                    merge(target[k], v)
                else:
                    target[k] = v

        merge(self.nodes[node_id], ensure_serializable(data))

        if node_id.startswith("!") and "node_num" not in self.nodes[node_id]:
            try:
                self.nodes[node_id]["node_num"] = int(node_id[1:], 16)
            except Exception:
                pass

        self.nodes[node_id]["isLocal"] = node_id == self.local_node_id

        db_data = self.nodes[node_id].copy()
        if "channelSettings" not in db_data and "channels" in db_data:
            db_data["channelSettings"] = db_data["channels"]
        self.db.save_node(node_id, db_data)

        if broadcast and g.main_event_loop:
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "node_update", "data": self.nodes[node_id]}, slot_id=self.slot_id),
                g.main_event_loop,
            )

    def set_local_node_info(self, info):
        if not info:
            self.local_node_id = None
            self.local_node_info = None
            self.channel_map.clear()
            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "local_node_info", "data": {}}, slot_id=self.slot_id),
                    g.main_event_loop,
                )
            return

        try:
            nid = f"!{info.my_node_num:08x}"
            self.local_node_id = nid

            chans = []
            # Resolve the connection manager for this data instance
            _cm = None
            for _slot in g.NODE_REGISTRY.values():
                if _slot.g.meshtastic_data is self:
                    _cm = _slot.g.connection_manager
                    break
            if _cm is None:
                _cm = g.connection_manager  # fallback to global for node_0
            if (
                _cm
                and hasattr(_cm, "interface")
                and _cm.interface
                and hasattr(_cm.interface, "localNode")
                and _cm.interface.localNode
                and hasattr(_cm.interface.localNode, "channels")
            ):
                chans = _cm.interface.localNode.channels

            if not chans:
                chans = getattr(info, "channels", []) or []
            if not isinstance(chans, list):
                chans = []

            logger.info(f"? Found {len(chans)} channels on local node")
            self.channel_map.clear()
            processed_channels = []

            for c in chans:
                try:
                    idx = getattr(c, "index", None)
                    if idx is None:
                        continue
                    settings = getattr(c, "settings", None)
                    if not settings:
                        continue
                    channel_data = {
                        "index": idx,
                        "role": str(getattr(c, "role", "DISABLED")),
                        "settings": {
                            "name": getattr(settings, "name", f"Channel {idx}"),
                            "psk": (
                                base64.b64encode(getattr(settings, "psk", b"")).decode("utf-8")
                                if hasattr(settings, "psk") else ""
                            ),
                            "channel_num": getattr(settings, "channel_num", idx),
                            "id": getattr(settings, "id", 0),
                            "uplink_enabled": getattr(settings, "uplink_enabled", False),
                            "downlink_enabled": getattr(settings, "downlink_enabled", False),
                        },
                    }
                    processed_channels.append(channel_data)
                    channel_id = getattr(settings, "id", None)
                    if channel_id is not None:
                        self.channel_map[channel_id] = idx
                except Exception as e:
                    logger.warning(f"Failed to process channel: {e}")

            chans = processed_channels
            logger.info(f" Processed {len(chans)} valid channels")

            # Initialize metadata and local_config
            local_config = None
            metadata = None
            local_db_node = {}
            
            if _cm and hasattr(_cm, "interface") and _cm.interface:
                # Safely pull metadata for accurate hardware and firmware strings
                metadata = getattr(_cm.interface, "metadata", None)

                if hasattr(_cm.interface, "localNode"):
                    local_node = _cm.interface.localNode
                    local_config = getattr(local_node, "localConfig", None)

                # Fetch the local radio's dynamic identity and telemetry from the NodeDB
                if hasattr(_cm.interface, "nodes"):
                    local_db_node = _cm.interface.nodes.get(info.my_node_num, {})

            # Robust fallback logic: Try metadata first, fallback to info, then None/Unknown
            fw_version = getattr(metadata, "firmware_version", getattr(info, "firmware_version", None)) if metadata else getattr(info, "firmware_version", None)
            hw_model_str = getattr(metadata, "hw_model_str", getattr(info, "hw_model_str", None)) if metadata else getattr(info, "hw_model_str", None)
            hw_model_enum = str(getattr(metadata, "hw_model", getattr(info, "hw_model", "Unknown"))) if metadata else str(getattr(info, "hw_model", "Unknown"))
            # If hw_model_str is not provided, resolve it from the protobuf enum
            if not hw_model_str and hw_model_enum not in ("Unknown", "None"):
                try:
                    from meshtastic.protobuf import mesh_pb2 as _mesh_pb2
                    hw_model_str = _mesh_pb2.HardwareModel.Name(int(hw_model_enum))
                except Exception:
                    try:
                        from meshtastic import mesh_pb2 as _mesh_pb2
                        hw_model_str = _mesh_pb2.HardwareModel.Name(int(hw_model_enum))
                    except Exception:
                        pass

            # Extract dynamic data from the NodeDB entry, not info
            user_data = ensure_serializable(local_db_node.get("user", {}))
            position_data = ensure_serializable(local_db_node.get("position", {}))
            metrics_data = ensure_serializable(local_db_node.get("deviceMetrics", {}))

            node_data = {
                "node_id": nid,
                "node_num": info.my_node_num,
                "isLocal": True,
                "hw_model": hw_model_str or hw_model_enum,
                "firmware_version": fw_version,
                "role": (_role_name(getattr(local_config.device, "role", None)) if local_config and hasattr(local_config, "device") else None) or _role_name(user_data.get("role")) or "CLIENT",
                "user": user_data,
                "position": position_data,
                "deviceMetrics": metrics_data,
            }
            self.update_node(nid, node_data, broadcast=False)

            self.local_node_info = {
                "node_id": nid,
                "node_num": info.my_node_num,
                "node_id_hex": f"!{info.my_node_num:08x}",
                "hardware_model": hw_model_enum,
                "hardware_model_string": hw_model_str or "Unknown",
                "firmware_version": fw_version or "Unknown",
                "long_name": user_data.get("longName", "Unknown"),
                "short_name": user_data.get("shortName", "N/A"),
                "macaddr": user_data.get("macaddr", "Unknown"),
                "role": (_role_name(getattr(local_config.device, "role", None)) if local_config and hasattr(local_config, "device") else None) or _role_name(user_data.get("role")) or "CLIENT",
                "region": _region_name(local_config.lora.region) if local_config and hasattr(local_config, "lora") else (user_data.get("region") or "Unknown"),
                "max_channels": getattr(info, "max_channels", 0),
                "latitude": node_data["position"].get("latitude"),
                "longitude": node_data["position"].get("longitude"),
                "altitude": node_data["position"].get("altitude"),
                "battery_level": node_data["deviceMetrics"].get("batteryLevel"),
                "voltage": node_data["deviceMetrics"].get("voltage"),
                "lora_region": (
                    _region_name(local_config.lora.region)
                    if local_config and hasattr(local_config, "lora") else None
                ),
                "lora_hop_limit": (
                    local_config.lora.hop_limit
                    if local_config and hasattr(local_config, "lora") else None
                ),
                "channels_json": json.dumps(ensure_serializable(chans)),
                "channel_count": len(chans),
                "nodedb_count": getattr(info, "nodedb_count", len(self.nodes)),
                "last_updated": time.time(),
            }

            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "local_node_info", "data": self.local_node_info}, slot_id=self.slot_id),
                    g.main_event_loop,
                )
            logger.info(f" Set local node info for {nid} ({self.local_node_info.get('long_name')})")

            # Post-set: try to refresh user data from the NodeDB if it was empty
            # (user data arrives via node_updated callback, which may fire after this)
            refreshed = False
            if self.local_node_info.get('long_name') in ('Unknown', None, ''):
                existing = self.nodes.get(nid, {})
                existing_user = existing.get('user', {})
                if isinstance(existing_user, dict) and existing_user.get('longName'):
                    self.local_node_info['long_name'] = existing_user.get('longName', 'Unknown')
                    self.local_node_info['short_name'] = existing_user.get('shortName', 'N/A')
                    self.local_node_info['macaddr'] = existing_user.get('macaddr', 'Unknown')
                    refreshed = True
            # Also try to refresh role/region from localConfig if still Unknown
            if self.local_node_info.get('role') in ('Unknown', None) or self.local_node_info.get('region') in ('Unknown', None):
                # Try to get localConfig from the interface
                for _slot in g.NODE_REGISTRY.values():
                    if _slot.g.meshtastic_data is self and _slot.g.connection_manager and _slot.g.connection_manager.interface:
                        _local_node = getattr(_slot.g.connection_manager.interface, 'localNode', None)
                        if _local_node:
                            _lc = getattr(_local_node, 'localConfig', None)
                            if _lc:
                                if self.local_node_info.get('role') in ('Unknown', None) and hasattr(_lc, 'device'):
                                    self.local_node_info['role'] = _role_name(getattr(_lc.device, 'role', 'CLIENT'))
                                if self.local_node_info.get('region') in ('Unknown', None) and hasattr(_lc, 'lora'):
                                    self.local_node_info['region'] = _region_name(_lc.lora.region)
                        break
            if refreshed or self.local_node_info.get('role') not in ('Unknown', None) and self.local_node_info.get('region') not in ('Unknown', None):
                logger.info(f" Refreshed local_node_info for {nid}")
                if g.main_event_loop:
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "local_node_info", "data": self.local_node_info}, slot_id=self.slot_id),
                        g.main_event_loop,
                    )
        except Exception as e:
            logger.error(f"Error setting local info: {e}", exc_info=True)

    def set_connection_state(self, state, detail: str = "", transport: str = "") -> None:
        """Update connection state using the formal ConnectionState enum.

        Emits structured SSE: {"event": "connection_status", "data": {"state": ..., "detail": ..., "transport": ...}}
        Also updates connection_status string for backward compatibility.
        """
        from core.connections import ConnectionState, is_valid_transition

        # Accept both enum instances and string values
        if isinstance(state, ConnectionState):
            state_value = state.value
            state_enum = state
        else:
            state_value = str(state)
            try:
                state_enum = ConnectionState(state_value)
            except ValueError:
                state_enum = None

        # Enforce valid transitions (only if we have enum values on both sides)
        if state_enum is not None and self._connection_state is not None:
            try:
                prev = ConnectionState(self._connection_state)
                if not is_valid_transition(prev, state_enum):
                    logger.warning(
                        f"Invalid state transition: {prev.value} -> {state_value} "
                        f"(detail={detail}). Allowing anyway for robustness."
                    )
            except ValueError:
                pass  # Previous state wasn't a known enum - allow anything

        self._connection_state = state_value
        self._connection_detail = detail
        if transport:
            self._connection_transport = transport

        # Build backward-compatible status string
        if state_value == ConnectionState.IDLE.value:
            compat_str = "Initializing"
        elif state_value == ConnectionState.CONNECTING.value:
            compat_str = f"Connecting{f' ({detail})' if detail else ''}"
        elif state_value == ConnectionState.CONNECTED.value:
            compat_str = f"Connected{f' ({detail})' if detail else ''}"
        elif state_value == ConnectionState.RECONNECTING.value:
            compat_str = f"Reconnecting{f' ({detail})' if detail else ''}"
        elif state_value == ConnectionState.DEGRADED.value:
            compat_str = f"Waiting{f' ({detail})' if detail else ''}"
        elif state_value == ConnectionState.DISCONNECTED.value:
            compat_str = f"Disconnected{f' ({detail})' if detail else ''}"
        elif state_value == ConnectionState.WEBSERIAL.value:
            compat_str = "Web Serial (Browser)"
        elif state_value == ConnectionState.MQTT.value:
            compat_str = "MQTT (managed by MQTTConnectionManager)"
        else:
            compat_str = state_value

        self.connection_status = compat_str
        self.db.log_connection_status(compat_str)
        logger.info(f"\U0001f517 Connection state: {state_value} detail={detail or '-'} transport={transport or '-'}")

        # Broadcast structured SSE
        if g.main_event_loop:
            try:
                sse_data = {
                    "state": state_value,
                    "detail": detail,
                    "transport": transport or self._connection_transport,
                    "label": compat_str,
                }
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "connection_status", "data": sse_data}, slot_id=self.slot_id),
                    g.main_event_loop,
                )
            except Exception as e:
                logger.error(f"Failed to broadcast connection state: {e}")

    def set_connection_status(self, status_str: str):
        """Legacy string-based connection status update - maps to the state machine."""
        from core.connections import ConnectionState

        # Preserve "Initializing" only if we genuinely haven't started connecting yet
        if self.connection_status == "Initializing" and status_str == "STREAM OPEN":
            pass  # allow transition Initializing -> STREAM OPEN
        elif self.connection_status == "Initializing" and status_str == "Connected":
            pass  # allow transition Initializing -> Connected (fast path)
        elif status_str == "Initializing":
            # Only set back to Initializing if genuinely not yet connected
            if not any(s in self.connection_status for s in ("STREAM OPEN", "Connected", "Reconnecting", "Waiting")):
                pass  # keep as-is or set below
            else:
                return  # don't regress from active states back to Initializing

        # Map legacy strings to ConnectionState
        s = status_str.lower()
        transport = ""
        detail = ""
        if s == "connected" or s == "connected (mqtt)":
            state = ConnectionState.CONNECTED
            if "mqtt" in s:
                transport = "MQTT"
            detail = status_str
        elif s.startswith("connected (meshcore"):
            state = ConnectionState.CONNECTED
            transport = "MESHCORE"
            detail = status_str
        elif s == "stream open":
            state = ConnectionState.DEGRADED
            detail = "Transport up, waiting for radio myInfo"
        elif s.startswith("reconnecting"):
            state = ConnectionState.RECONNECTING
            detail = status_str
        elif s.startswith("disconnected") or s.startswith("disconnected ("):
            state = ConnectionState.DISCONNECTED
            detail = status_str
        elif s.startswith("connecting"):
            state = ConnectionState.CONNECTING
            detail = status_str
        elif s.startswith("waiting"):
            state = ConnectionState.DEGRADED
            detail = status_str
        elif "web serial" in s:
            state = ConnectionState.WEBSERIAL
            detail = status_str
        elif "mqtt" in s and "managed" in s:
            state = ConnectionState.MQTT
            detail = status_str
        elif s == "disconnected":
            state = ConnectionState.DISCONNECTED
        elif "auth failed" in s or "error" in s:
            state = ConnectionState.DISCONNECTED
            detail = status_str
        else:
            # Unknown string - set as detail with RECONNECTING as default guess
            state = ConnectionState.RECONNECTING
            detail = status_str

        self.set_connection_state(state, detail=detail, transport=transport)

    def set_error(self, err: str):
        self.last_error = err
        if g.main_event_loop:
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "error", "data": err}, slot_id=self.slot_id), g.main_event_loop
            )

    def get_serializable_stats(self) -> Dict:
        s = self.stats.copy()
        s["nodes_seen_session"] = len(s["nodes_seen_session"])
        s["channels_seen_session"] = len(s["channels_seen_session"])
        s["elapsed_time_session"] = time.time() - s["start_time"]
        return ensure_serializable(s)

    def get_formatted_packets_from_memory(self, limit: int) -> List[Dict]:
        return list(reversed(list(self.packets)))[:limit]


