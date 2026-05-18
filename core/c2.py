import core.globals as g
from typing import Set, Dict, Optional, Any, List, Tuple
from collections import deque

import secrets
from core.auth import create_access_token, ensure_serializable
from core.config_loader import _resolve_community
from core.broadcast import broadcast_data
from fastapi import Request, Response, status
import urllib
import httpx
import time
import logging
import hmac
import hashlib
import fnmatch
import datetime
from datetime import timedelta
import contextlib
import asyncio
import io
import shlex
import argparse
import base64
import re
import json
import os
import urllib.parse

logger = logging.getLogger(__name__)
_c2_logger = logging.getLogger("c2_bridge")

try:
    from meshtastic import channel_pb2
    from meshtastic.remote_hardware import RemoteHardwareClient
except ImportError:
    channel_pb2 = None
    RemoteHardwareClient = None

WEBSERVER_PORT = 8181  # Default; overridden by g.loaded_config at runtime

C2_TIER_ENDPOINTS: Dict[str, Dict[str, Set[str]]] = {
    "heartbeat": {
        "GET": {"/api/status", "/api/stats", "/api/system/version-status"},
        "POST": set(),
    },
    "monitor": {
        "GET": {
            "/api/status", "/api/stats", "/api/system/version-status",
            "/api/nodes", "/api/nodes/*",
            "/api/channels", "/api/neighbors", "/api/local_node/full",
        },
        "POST": set(),
    },
    "read": {
        "GET": {
            "/api/status", "/api/stats", "/api/system/version-status",
            "/api/nodes", "/api/nodes/*",
            "/api/packets", "/api/packets/history", "/api/messages/history",
            "/api/metrics/averages", "/api/counts/totals", "/api/neighbors",
            "/api/traceroutes", "/api/waypoints", "/api/hardware_logs",
            "/api/channels", "/api/local_node/full",
            "/api/system/connection_history",
        },
        "POST": set(),
    },
    "operator": {
        "GET": {
            "/api/status", "/api/stats", "/api/system/version-status",
            "/api/nodes", "/api/nodes/*",
            "/api/packets", "/api/packets/history", "/api/messages/history",
            "/api/metrics/averages", "/api/counts/totals", "/api/neighbors",
            "/api/traceroutes", "/api/waypoints", "/api/hardware_logs",
            "/api/channels", "/api/local_node/full",
            "/api/system/connection_history",
        },
        "POST": {
            "/api/messages", "/api/alert", "/api/monitor", "/extract",
        },
    },
    "full": {
        "GET": {
            "/api/status", "/api/stats", "/api/system/version-status",
            "/api/nodes", "/api/nodes/*",
            "/api/packets", "/api/packets/history", "/api/messages/history",
            "/api/metrics/averages", "/api/counts/totals", "/api/neighbors",
            "/api/traceroutes", "/api/waypoints", "/api/hardware_logs",
            "/api/channels", "/api/local_node/full",
            "/api/system/connection_history",
        },
        "POST": {
            "/api/messages", "/api/console", "/api/alert",
            "/api/system/restart", "/api/system/start-update",
            "/api/system/check-update", "/api/monitor", "/extract",
            "/api/tasks/*", "/api/auto_reply/*",
        },
    },
}

C2_ABSOLUTE_BLACKLIST: Set[str] = {
    "/api/system/config",
    "/api/system/config/update",
    "/api/system/config/initial-setup",
    "/login", "/logout", "/setup", "/sse", "/sse-debug",
}

C2_PARAM_LIMITS: Dict[str, Dict[str, int]] = {
    "/api/messages/history": {"limit": 10000},
    "/api/packets": {"limit": 10000},
    "/api/packets/history": {"limit": 10000},
    "/api/traceroutes": {"limit": 10000},
    "/api/hardware_logs": {"limit": 10000},
    "/api/metrics/averages": {"limit": 5000},
    "/api/system/connection_history": {"limit": 5000},
    "/api/nodes/*/history/*": {"limit": 10000},
    "/api/nodes/*/count/*": {},
}

def execute_meshtastic_command(cmd_line: str, slot_id: str = "node_0") -> str:
    """
    Fully featured 'meshtastic' CLI bridge.
    Supports: GPIO, Positioning, Channel Mgmt, Canned Msgs, Telemetry, etc.
    """
    logger.info(f"? CONSOLE [{slot_id}]: Executing '{cmd_line}'")

    _slot = g.NODE_REGISTRY.get(slot_id) or g.NODE_REGISTRY.get("node_0")
    _cm = _slot.g.connection_manager if _slot else g.connection_manager
    if not _cm or not _cm.interface:
        return "Error: No active connection to a Meshtastic device."

    iface = _cm.interface
    local_node = iface.localNode
    output = io.StringIO()

    try:
        args = shlex.split(cmd_line)
        if args and args[0].lower() == "meshtastic":
            args = args[1:]
    except Exception as e:
        return f"Syntax Error: {e}"

    parser = argparse.ArgumentParser(prog="meshtastic", add_help=False)

    parser.add_argument("--info", action="store_true")
    parser.add_argument("--nodes", action="store_true")
    parser.add_argument("--channels", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--qr", action="store_true")
    parser.add_argument("--get", type=str)
    parser.add_argument("--set", nargs=2, metavar=("KEY", "VAL"))
    parser.add_argument("--seturl", type=str)
    parser.add_argument("--set-owner", type=str)
    parser.add_argument("--set-owner-short", type=str)
    parser.add_argument("--set-canned-message", type=str)
    parser.add_argument("--get-canned-message", action="store_true")
    parser.add_argument("--setlat", type=float)
    parser.add_argument("--setlon", type=float)
    parser.add_argument("--setalt", type=int)
    parser.add_argument("--remove-position", action="store_true")
    parser.add_argument("--ch-index", type=int, default=0)
    parser.add_argument("--ch-add", type=str)
    parser.add_argument("--ch-del", action="store_true")
    parser.add_argument("--ch-enable", action="store_true")
    parser.add_argument("--ch-disable", action="store_true")
    parser.add_argument("--ch-set", nargs=2, metavar=("KEY", "VAL"))
    parser.add_argument("--ch-longfast", action="store_true")
    parser.add_argument("--ch-longslow", action="store_true")
    parser.add_argument("--ch-medfast", action="store_true")
    parser.add_argument("--ch-medslow", action="store_true")
    parser.add_argument("--ch-shortfast", action="store_true")
    parser.add_argument("--ch-shortslow", action="store_true")
    parser.add_argument("--sendtext", type=str)
    parser.add_argument("--dest", type=str, default="^all")
    parser.add_argument("--traceroute", type=str)
    parser.add_argument("--request-telemetry", action="store_true")
    parser.add_argument("--request-position", action="store_true")
    parser.add_argument("--reboot", action="store_true")
    parser.add_argument("--shutdown", action="store_true")
    parser.add_argument("--factory-reset", action="store_true")
    parser.add_argument("--remove-node", type=str)
    parser.add_argument("--gpio-wrb", nargs=2, type=int, metavar=("PIN", "STATE"))
    parser.add_argument("--gpio-rd", type=str)
    parser.add_argument("--gpio-watch", type=str)
    parser.add_argument("--help", "-h", action="store_true")

    try:
        parsed, unknown = parser.parse_known_args(args)
        if unknown:
            return f"Error: Unrecognized arguments: {unknown}"

        with contextlib.redirect_stdout(output):
            if parsed.help:
                parser.print_help()
                return output.getvalue()

            if parsed.set_canned_message:
                print(f"Setting canned messages to: {parsed.set_canned_message}")
                local_node.localConfig.canned_message.messages = parsed.set_canned_message
                local_node.writeConfig("canned_message")
                print("Written to device.")

            if parsed.get_canned_message:
                print(f"Canned Messages:\n{local_node.localConfig.canned_message.messages}")

            if parsed.set_owner:
                print(f"Setting owner name: {parsed.set_owner}")
                local_node.setOwner(long_name=parsed.set_owner)

            if parsed.set_owner_short:
                print(f"Setting short name: {parsed.set_owner_short}")
                local_node.setOwner(short_name=parsed.set_owner_short)

            if parsed.set:
                path = parsed.set[0].split('.')
                val_str = parsed.set[1]
                if len(path) != 2:
                    print("Error: Use format 'section.option' (e.g. lora.region)")
                else:
                    try:
                        section = getattr(local_node.localConfig, path[0])
                        if val_str.lower() in ('true', 'yes'): val = True
                        elif val_str.lower() in ('false', 'no'): val = False
                        elif val_str.isdigit(): val = int(val_str)
                        else: val = val_str
                        setattr(section, path[1], val)
                        local_node.writeConfig(path[0])
                        print(f"Set {parsed.set[0]} = {val}")
                    except Exception as e:
                        print(f"Config Error: {e}")

            if parsed.setlat is not None or parsed.setlon is not None or parsed.setalt is not None:
                print("Updating Fixed Position...")
                local_node.localConfig.position.fixed_position = True
                if parsed.setlat is not None:
                    local_node.localConfig.position.latitude_i = int(parsed.setlat * 1e7)
                if parsed.setlon is not None:
                    local_node.localConfig.position.longitude_i = int(parsed.setlon * 1e7)
                if parsed.setalt is not None:
                    local_node.localConfig.position.altitude = parsed.setalt
                local_node.writeConfig("position")
                print(f"Position updated. Fixed Mode: ENABLED.")

            if parsed.remove_position:
                print("Disabling Fixed Position and clearing coords...")
                local_node.localConfig.position.fixed_position = False
                local_node.localConfig.position.latitude_i = 0
                local_node.localConfig.position.longitude_i = 0
                local_node.localConfig.position.altitude = 0
                local_node.writeConfig("position")
                print("Done.")

            ch_idx = parsed.ch_index
            target_ch = local_node.channels[ch_idx] if ch_idx < len(local_node.channels) else None

            if parsed.ch_add:
                found_idx = -1
                for c in local_node.channels:
                    if c.role == channel_pb2.Channel.Role.DISABLED:
                        found_idx = c.index
                        break
                if found_idx == -1:
                    if len(local_node.channels) >= 8:
                        print("Error: No free channels available.")
                    else:
                        print(f"Adding channel '{parsed.ch_add}' at index {ch_idx}...")
                        ch = local_node.channels[ch_idx]
                        ch.settings.name = parsed.ch_add
                        ch.role = channel_pb2.Channel.Role.SECONDARY
                        local_node.writeChannel(ch_idx)
                        print("Channel added.")
                else:
                    print(f"Adding channel '{parsed.ch_add}' at free index {found_idx}...")
                    ch = local_node.channels[found_idx]
                    ch.settings.name = parsed.ch_add
                    ch.role = channel_pb2.Channel.Role.SECONDARY
                    local_node.writeChannel(found_idx)
                    print("Channel added.")

            if parsed.ch_del or parsed.ch_disable:
                print(f"Deleting/Disabling channel {ch_idx}...")
                ch = local_node.channels[ch_idx]
                ch.role = channel_pb2.Channel.Role.DISABLED
                local_node.writeChannel(ch_idx)
                print("Done.")

            if parsed.ch_enable:
                print(f"Enabling channel {ch_idx}...")
                ch = local_node.channels[ch_idx]
                if ch.role == channel_pb2.Channel.Role.DISABLED:
                    ch.role = channel_pb2.Channel.Role.SECONDARY
                local_node.writeChannel(ch_idx)
                print("Done.")

            if parsed.ch_set:
                key, val = parsed.ch_set
                print(f"Setting Ch {ch_idx} {key} = {val}...")
                ch = local_node.channels[ch_idx]
                if val.lower() == 'true': val = True
                elif val.lower() == 'false': val = False
                elif val.isdigit(): val = int(val)
                if key == "psk":
                    if val == "none":
                        ch.settings.psk = b''
                    elif val == "random":
                        ch.settings.psk = secrets.token_bytes(32)
                    elif val == "default":
                        ch.settings.psk = b'\x01'
                    else:
                        try:
                            if isinstance(val, str) and val.startswith("base64:"):
                                ch.settings.psk = base64.b64decode(val.split(":")[1])
                            else:
                                ch.settings.psk = val.encode() if isinstance(val, str) else val
                        except Exception:
                            print("Error parsing PSK")
                else:
                    if hasattr(ch.settings, key):
                        setattr(ch.settings, key, val)
                    else:
                        print(f"Unknown channel setting: {key}")
                local_node.writeChannel(ch_idx)
                print("Channel updated.")

            modem_preset = None
            if parsed.ch_longfast: modem_preset = channel_pb2.ChannelSettings.ModemConfig.Bw125Cr48Sf4096
            if parsed.ch_longslow: modem_preset = channel_pb2.ChannelSettings.ModemConfig.Bw125Cr48Sf4096
            if parsed.ch_medfast:  modem_preset = channel_pb2.ChannelSettings.ModemConfig.Bw250Cr46Sf2048
            if modem_preset:
                print(f"Applying Modem Preset to Primary Channel...")
                ch = local_node.channels[0]
                ch.settings.modem_config = modem_preset
                local_node.writeChannel(0)
                print("Preset applied.")

            target_node = parsed.dest

            if parsed.gpio_wrb:
                pin, state = parsed.gpio_wrb
                if target_node == "^all":
                    print("Error: --dest required for GPIO operations")
                else:
                    print(f"Writing GPIO {pin} to {state} on {target_node}...")
                    rh = RemoteHardwareClient(iface)
                    mask = 1 << pin
                    val = state << pin
                    rh.writeGPIOs(target_node, mask, val)
                    print("Command sent.")

            if parsed.gpio_rd:
                try:
                    mask = int(parsed.gpio_rd, 0)
                    if target_node == "^all":
                        print("Error: --dest required")
                    else:
                        print(f"Reading GPIO mask {hex(mask)} from {target_node}...")
                        rh = RemoteHardwareClient(iface)
                        rh.readGPIOs(target_node, mask)
                        print("Request sent. Watch logs for 'RemoteHardware' packets.")
                except ValueError:
                    print("Error: Mask must be an integer or hex string (e.g. 0x10)")

            if parsed.gpio_watch:
                try:
                    mask = int(parsed.gpio_watch, 0)
                    if target_node == "^all":
                        print("Error: --dest required")
                    else:
                        print(f"Watching GPIO mask {hex(mask)} on {target_node}...")
                        rh = RemoteHardwareClient(iface)
                        rh.watchGPIOs(target_node, mask)
                        print("Watch started.")
                except ValueError:
                    print("Error: Mask must be hex string")

            if parsed.info:
                print(f"Owner: {iface.getLongName()} ({iface.getShortName()})")
                print(f"Nodes: {len(iface.nodes)}")
                print(f"Firmware: {getattr(iface.metadata, 'firmware_version', '?')}")

            if parsed.nodes:
                print(f"{'ID':<12} {'Name':<20} {'SNR':<6} {'Last Heard'}")
                print("-" * 60)
                for n in iface.nodes.values():
                    nid = f"!{n.get('num', 0):08x}"
                    name = n.get("user", {}).get("longName", "Unknown")
                    snr = n.get("snr", 0.0)
                    lh = datetime.fromtimestamp(n.get("lastHeard", 0)).strftime("%H:%M") if n.get("lastHeard") else "Never"
                    print(f"{nid:<12} {name[:20]:<20} {snr:<6.2f} {lh}")

            if parsed.traceroute:
                print(f"Traceroute to {parsed.traceroute}...")
                iface.sendTraceRoute(parsed.traceroute, 3)
                print("Sent.")

            if parsed.sendtext:
                if g.main_event_loop:
                    future = asyncio.run_coroutine_threadsafe(
                        g.connection_manager.sendText(parsed.sendtext, destinationId=parsed.dest, channelIndex=parsed.ch_index),
                        g.main_event_loop,
                    )
                    try:
                        future.result(timeout=5)
                        print(f"Sent to {parsed.dest}")
                    except Exception as e:
                        print(f"Send Error: {e}")

            if parsed.reboot:
                print("Rebooting...")
                iface.reboot()

    except Exception as e:
        logger.error(f"Cmd Error: {e}", exc_info=True)
        return f"Error: {e}"

    return output.getvalue()


def _resolve_tier_endpoints(tier_name: str, extra: Set[str] = None, blocked: Set[str] = None) -> Dict[str, Set[str]]:
    if tier_name not in C2_TIER_ENDPOINTS:
        return {"GET": set(), "POST": set()}
    allowed: Dict[str, Set[str]] = {
        "GET": set(C2_TIER_ENDPOINTS[tier_name]["GET"]),
        "POST": set(C2_TIER_ENDPOINTS[tier_name]["POST"]),
    }
    for bl in C2_ABSOLUTE_BLACKLIST:
        allowed["GET"].discard(bl)
        allowed["POST"].discard(bl)
    if extra:
        for ep in extra:
            ep = ep.strip()
            if not ep:
                continue
            allowed["GET"].add(ep)
            if any(verb in ep.lower() for verb in
                   ["send", "message", "console", "alert", "restart", "update", "setup", "config", "monitor", "extract"]):
                allowed["POST"].add(ep)
    if blocked:
        for ep in blocked:
            ep = ep.strip()
            allowed["GET"].discard(ep)
            allowed["POST"].discard(ep)
    return allowed


def _path_matches_pattern(path: str, patterns: Set[str]) -> bool:
    path = path.rstrip("/")
    for pattern in patterns:
        pattern = pattern.rstrip("/")
        if path == pattern:
            return True
        if "*" in pattern and fnmatch.fnmatch(path, pattern):
            return True
    return False


def _clamp_params(path: str, params: dict) -> dict:
    if not params:
        return params
    for pattern, limits in C2_PARAM_LIMITS.items():
        if fnmatch.fnmatch(path, pattern) or path == pattern:
            for param_name, max_val in limits.items():
                if param_name in params:
                    try:
                        val = int(params[param_name])
                        if val > max_val:
                            _c2_logger.warning(f"?? C2 clamped {param_name}={val}  {max_val} for {path}")
                            params[param_name] = max_val
                    except (ValueError, TypeError):
                        pass
            break
    return params


def _sanitize_path(path: str) -> str:
    if not path or not isinstance(path, str):
        return ""
    if not path.startswith("/"):
        path = "/" + path
    if ".." in path or "//" in path:
        _c2_logger.warning(f"?? C2 blocked path traversal attempt: {path}")
        return ""
    if "?" in path:
        path = path.split("?")[0]
    if not re.match(r"^[a-zA-Z0-9/_\-\.!]+$", path):
        _c2_logger.warning(f"?? C2 blocked suspicious path characters: {path}")
        return ""
    return path


def _sign_payload(payload_bytes: bytes, api_key: str) -> str:
    return hmac.new(
        api_key.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()


def _c2_headers(api_key: str, node_id: str, extra: dict = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Node-Id": node_id or "unknown",
    }
    if extra:
        h.update(extra)
    return h


def _c2_query(api_key: str, node_id: str, action: str) -> str:
    from urllib.parse import quote
    return f"?action={action}&api_key={quote(api_key)}&node_id={quote(node_id or 'unknown')}"


class C2ActivityLogger:
    def __init__(self, maxlen=200):
        self.logs: deque = deque(maxlen=maxlen)
        self.stats: Dict[str, Any] = {
            "heartbeats_sent": 0,
            "heartbeat_failures": 0,
            "proxy_requests_received": 0,
            "proxy_responses_sent": 0,
            "outbox_messages_forwarded": 0,
            "admin_commands_received": 0,
            "admin_responses_sent": 0,
            "last_contact": None,
            "last_error": None,
            "last_error_time": None,
        }

    def add_entry(self, direction: str, type_: str, details: dict):
        self.logs.append({
            "timestamp": time.time(),
            "direction": direction,
            "type": type_,
            "details": details,
        })

    def get_snapshot(self) -> Dict:
        return {"logs": list(self.logs), "stats": self.stats.copy()}


c2_activity = C2ActivityLogger()


async def remote_c2_worker_enhanced():
    """Secure C2 Bridge Worker with full activity logging."""
    # Remote C2 is gated by REMOTE_C2 toggle (heartbeat is always active in scheduler)
    c2_enabled = g.loaded_config.get("REMOTE_C2", False)
    c2_url = _resolve_community()
    c2_key = g.loaded_config.get("COMMUNITY_API_KEY", "")
    access_level = str(g.loaded_config.get("C2_ACCESS_LEVEL", "read")).lower().strip()
    max_requests_per_sync = 10  # hardcoded safe defaults
    max_response_kb = 512
    sync_interval = 15

    if not c2_enabled or not c2_key:
        _c2_logger.info("? Remote C2 Bridge is DISABLED (REMOTE_C2=off or no API key).")
        return
    if access_level == "off":
        _c2_logger.info("? Remote C2 Bridge is DISABLED (access_level=off).")
        return
    if access_level not in ("heartbeat", "monitor", "read", "operator", "full"):
        _c2_logger.error(f" Invalid C2_ACCESS_LEVEL '{access_level}'. Defaulting to 'heartbeat'.")
        access_level = "heartbeat"
    allowed_endpoints = _resolve_tier_endpoints(access_level)
    _c2_logger.info(f"? Remote C2 Bridge Active | Level: {access_level.upper()}")

    local_base = f"http://127.0.0.1:{g.loaded_config.get('WEBSERVER_PORT', WEBSERVER_PORT)}"
    local_limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
    local_timeout = httpx.Timeout(15.0, connect=5.0)
    remote_limits = httpx.Limits(max_keepalive_connections=3, max_connections=5)
    remote_timeout = httpx.Timeout(30.0, connect=10.0)
    audit_log = logging.getLogger("c2_audit")
    consecutive_errors = 0
    MAX_BACKOFF_SECONDS = 300

    async with (
        httpx.AsyncClient(limits=local_limits, timeout=local_timeout) as local_client,
        httpx.AsyncClient(limits=remote_limits, timeout=remote_timeout) as remote_client,
    ):
        def radio_ready() -> bool:
            return (
                g.connection_manager is not None
                and g.connection_manager.is_ready.is_set()
                and g.meshtastic_data.local_node_id is not None
            )

        async def proxy_request(req: dict) -> dict:
            req_id = req.get("id", "unknown")
            method = str(req.get("method", "GET")).upper()
            raw_path = str(req.get("path", ""))
            params = req.get("params") or {}
            result: Dict[str, Any] = {"id": req_id, "status": 403, "data": None, "error": None}

            path = _sanitize_path(raw_path)
            if not path:
                result["error"] = "Invalid path"
                result["status"] = 400
                audit_log.warning(f"BLOCKED | {method} {raw_path} | Reason: sanitization failed")
                c2_activity.add_entry("incoming", "proxy_request_blocked", {"path": raw_path, "reason": "sanitization"})
                return result

            if method not in ("GET", "POST"):
                result["error"] = f"Method {method} not allowed"
                result["status"] = 405
                audit_log.warning(f"BLOCKED | {method} {path} | Reason: invalid method")
                c2_activity.add_entry("incoming", "proxy_request_blocked", {"method": method, "path": path})
                return result

            for bl in C2_ABSOLUTE_BLACKLIST:
                    if path == bl or path.startswith(bl + "/"):
                        result["error"] = "Endpoint permanently blocked"
                        result["status"] = 403
                        audit_log.warning(f"BLOCKED | {method} {path} | Reason: absolute blacklist")
                        c2_activity.add_entry("incoming", "proxy_request_blocked", {"path": path, "reason": "blacklist"})
                        return result

            if not _path_matches_pattern(path, allowed_endpoints.get(method, set())):
                result["error"] = f"Endpoint not allowed at tier '{access_level}'"
                result["status"] = 403
                audit_log.warning(f"BLOCKED | {method} {path} | Reason: not in {access_level} tier")
                c2_activity.add_entry("incoming", "proxy_request_blocked", {"path": path, "reason": "tier"})
                return result

            if isinstance(params, dict):
                params = _clamp_params(path, params)

            c2_activity.stats["proxy_requests_received"] += 1
            c2_activity.add_entry("incoming", "proxy_request", {"id": req_id, "method": method, "path": path, "params": params})

            try:
                url = f"{local_base}{path}"
                internal_token = create_access_token(
                    {"sub": "__c2_bridge__", "internal": True},
                    expires_delta=timedelta(seconds=30),
                )
                cookies = {"access_token": f"Bearer {internal_token}"}
                if method == "GET":
                    resp = await local_client.get(url, params=params, cookies=cookies)
                else:
                    resp = await local_client.post(url, json=params, cookies=cookies)

                body_bytes = resp.content
                size_kb = len(body_bytes) / 1024
                if size_kb > max_response_kb:
                    result["status"] = 413
                    result["error"] = f"Response too large: {size_kb:.0f}KB (limit: {max_response_kb}KB)"
                    audit_log.warning(f"CAPPED  | {method} {path} | Size: {size_kb:.0f}KB > {max_response_kb}KB")
                    c2_activity.add_entry("outgoing", "proxy_response", {"id": req_id, "status": 413, "size_kb": size_kb})
                    return result

                result["status"] = resp.status_code
                try:
                    result["data"] = resp.json()
                except Exception:
                    result["data"] = resp.text[:max_response_kb * 1024]

                audit_log.info(f"PROXIED | {method} {path} | Status: {resp.status_code} | Size: {size_kb:.1f}KB")
                c2_activity.stats["proxy_responses_sent"] += 1
                c2_activity.add_entry("outgoing", "proxy_response", {"id": req_id, "status": resp.status_code, "size_kb": size_kb})

            except httpx.ConnectError:
                result["status"] = 502
                result["error"] = "Local API unreachable"
                audit_log.error(f"ERROR   | {method} {path} | Local API connection refused")
                c2_activity.add_entry("outgoing", "proxy_response", {"id": req_id, "status": 502, "error": "Local API unreachable"})
            except httpx.TimeoutException:
                result["status"] = 504
                result["error"] = "Local API timeout"
                audit_log.error(f"ERROR   | {method} {path} | Local API timed out")
                c2_activity.add_entry("outgoing", "proxy_response", {"id": req_id, "status": 504, "error": "Timeout"})
            except Exception as e:
                result["status"] = 500
                result["error"] = f"Proxy error: {type(e).__name__}"
                audit_log.error(f"ERROR   | {method} {path} | {type(e).__name__}: {e}")
                c2_activity.add_entry("outgoing", "proxy_response", {"id": req_id, "status": 500, "error": str(e)})

            return result

        async def build_heartbeat() -> dict:
            stats = g.meshtastic_data.get_serializable_stats()
            # Gate: never send a C2 heartbeat until local_node_id is confirmed.
            # This prevents "unknown" node IDs from permanently blocking auto-onboarding.
            node_id = g.meshtastic_data.local_node_id
            if node_id is None:
                return None
            return {
                "action": "c2_heartbeat",
                "version": "2.2",
                "api_key": c2_key,
                "node_id": node_id,
                "access_level": access_level,
                "status": g.meshtastic_data.connection_status,
                "node_count": len(g.meshtastic_data.nodes),
                "packets_session": stats.get("packets_received_session", 0),
                "uptime": stats.get("elapsed_time_session", 0),
                "timestamp": time.time(),
                "available_endpoints": {
                    "GET": sorted(allowed_endpoints["GET"]),
                    "POST": sorted(allowed_endpoints["POST"]),
                },
            }

        _c2_logger.info(" C2 Bridge: Entering main sync loop.")
        c2_activity.add_entry("system", "startup", {"level": access_level})

        while True:
            try:
                if not radio_ready():
                    await asyncio.sleep(5)
                    continue

                heartbeat = await build_heartbeat()
                if heartbeat is None:
                    # Radio ready but node ID not yet populated — wait for it.
                    await asyncio.sleep(5)
                    continue

                payload_json = json.dumps(ensure_serializable(heartbeat))
                signature = _sign_payload(payload_json.encode(), c2_key)
                _node_id = heartbeat.get("node_id") or g.meshtastic_data.local_node_id or "unknown"

                c2_activity.stats["heartbeats_sent"] += 1
                c2_activity.add_entry("outgoing", "heartbeat", {"node_id": _node_id, "url": c2_url})

                try:
                    resp = await remote_client.post(
                        f"{c2_url}{_c2_query(c2_key, _node_id, 'c2_sync')}",
                        content=payload_json,
                        headers=_c2_headers(c2_key, _node_id, {"X-Signature": signature, "X-Access-Level": access_level}),
                    )
                    c2_activity.add_entry("incoming", "sync_response", {"status_code": resp.status_code})

                    if resp.status_code == 403:
                        _c2_logger.error(" C2 Auth Failed: API Key rejected.")
                        c2_activity.stats["heartbeat_failures"] += 1
                        c2_activity.stats["last_error"] = "Auth failed (403)"
                        c2_activity.stats["last_error_time"] = time.time()
                        consecutive_errors += 1
                        await asyncio.sleep(min(60 * consecutive_errors, MAX_BACKOFF_SECONDS))
                        continue

                    if resp.status_code != 200:
                        _c2_logger.warning(f"?  C2 server returned {resp.status_code}")
                        consecutive_errors += 1
                        c2_activity.stats["heartbeat_failures"] += 1
                        c2_activity.stats["last_error"] = f"HTTP {resp.status_code}"
                        c2_activity.stats["last_error_time"] = time.time()
                        c2_activity.add_entry("error", "bad_status", {"status": resp.status_code})
                    else:
                        consecutive_errors = 0
                        c2_activity.stats["last_contact"] = time.time()
                        data = resp.json()

                        # Handle proxy requests
                        proxy_reqs = data.get("requests", [])
                        if isinstance(proxy_reqs, list) and proxy_reqs:
                            proxy_reqs = proxy_reqs[:max_requests_per_sync]
                            results = []
                            for req in proxy_reqs:
                                r = await proxy_request(req)
                                results.append(r)
                            if results:
                                try:
                                    await remote_client.post(
                                        f"{c2_url}{_c2_query(c2_key, _node_id, 'c2_proxy_results')}",
                                        json={"results": results, "api_key": c2_key, "node_id": _node_id},
                                        headers=_c2_headers(c2_key, _node_id),
                                    )
                                except Exception as e:
                                    _c2_logger.error(f" Proxy result upload failed: {e}")

                        # Handle outbox messages
                        outbox = data.get("outbox", [])
                        if outbox and access_level in ("operator", "full"):
                            for msg in outbox[:10]:
                                dest = msg.get("to_id", msg.get("destination", "^all"))
                                text = str(msg.get("message", msg.get("text", "")))[:230]
                                ch = int(msg.get("channel", 0))
                                if text and g.connection_manager:
                                    try:
                                        await g.connection_manager.sendText(text, destinationId=dest, channelIndex=ch)
                                        c2_activity.stats["outbox_messages_forwarded"] += 1
                                        c2_activity.add_entry("incoming", "outbox_sent", {"to": dest, "ch": ch})
                                        msg_id = msg.get("id")
                                        if msg_id:
                                            try:
                                                await remote_client.post(
                                                    f"{c2_url}{_c2_query(c2_key, _node_id, 'confirm_sent')}",
                                                    json={"msg_id": msg_id, "api_key": c2_key, "node_id": _node_id},
                                                    headers=_c2_headers(c2_key, _node_id),
                                                )
                                            except Exception as ce:
                                                _c2_logger.debug(f"confirm_sent failed for msg {msg_id}: {ce}")
                                    except Exception as e:
                                        _c2_logger.error(f" Outbox send failed: {e}")
                                        c2_activity.add_entry("error", "outbox_failed", {"error": str(e), "to": dest})
                        elif outbox and access_level not in ("operator", "full"):
                            _c2_logger.warning(f"?? C2 sent outbox messages but tier is '{access_level}' (need operator or full).")
                            audit_log.warning(f"BLOCKED | Outbox ({len(outbox)} msgs) | Reason: tier '{access_level}'")
                            c2_activity.add_entry("incoming", "outbox_blocked", {"count": len(outbox), "reason": "tier"})

                        # Handle admin commands
                        admin_cmd = data.get("admin_command")
                        if admin_cmd and access_level == "full":
                            cmd_str = str(admin_cmd)[:500]
                            _c2_logger.info(f"??  C2 Admin Command: {cmd_str}")
                            audit_log.info(f"ADMIN   | Command: {cmd_str}")
                            c2_activity.stats["admin_commands_received"] += 1
                            c2_activity.add_entry("incoming", "admin_command", {"command": cmd_str})
                            try:
                                result_str = await asyncio.to_thread(execute_meshtastic_command, cmd_str)
                                await remote_client.post(
                                    f"{c2_url}{_c2_query(c2_key, _node_id, 'command_result')}",
                                    json={"result": str(result_str)[:5000], "api_key": c2_key, "node_id": _node_id},
                                    headers=_c2_headers(c2_key, _node_id),
                                )
                                c2_activity.stats["admin_responses_sent"] += 1
                                c2_activity.add_entry("outgoing", "admin_response", {"preview": str(result_str)[:100]})
                            except Exception as e:
                                _c2_logger.error(f" Admin command failed: {e}")
                                c2_activity.add_entry("error", "admin_command_failed", {"error": str(e)})
                        elif admin_cmd and access_level not in ("full",):
                            _c2_logger.warning(f"?? C2 sent admin command but tier is '{access_level}' (need full).")
                            audit_log.warning(f"BLOCKED | Admin cmd: {str(admin_cmd)[:80]} | Reason: tier '{access_level}'")
                            c2_activity.add_entry("incoming", "admin_blocked", {"command": str(admin_cmd)[:80]})

                except httpx.RequestError as e:
                    _c2_logger.debug(f"C2 network error: {type(e).__name__}: {e}")
                    c2_activity.stats["heartbeat_failures"] += 1
                    c2_activity.stats["last_error"] = str(e)
                    c2_activity.stats["last_error_time"] = time.time()
                    c2_activity.add_entry("error", "network_error", {"error": str(e)})
                    consecutive_errors += 1
                except json.JSONDecodeError:
                    _c2_logger.warning("?  C2 server returned invalid JSON")
                    c2_activity.stats["heartbeat_failures"] += 1
                    c2_activity.stats["last_error"] = "Invalid JSON response"
                    c2_activity.stats["last_error_time"] = time.time()
                    c2_activity.add_entry("error", "bad_json", {})
                    consecutive_errors += 1
                except Exception as e:
                    _c2_logger.error(f"?  C2 sync error: {e}")
                    c2_activity.stats["last_error"] = str(e)
                    c2_activity.stats["last_error_time"] = time.time()
                    c2_activity.add_entry("error", "sync_exception", {"error": str(e)})
                    consecutive_errors += 1

            except asyncio.CancelledError:
                _c2_logger.info("? C2 Bridge: Shutting down gracefully.")
                c2_activity.add_entry("system", "shutdown", {})
                return
            except Exception as fatal:
                _c2_logger.error(f" C2 Bridge fatal loop error: {fatal}", exc_info=True)
                c2_activity.add_entry("error", "fatal_loop_error", {"error": str(fatal)})
                consecutive_errors += 1

            if consecutive_errors > 0:
                backoff = min(sync_interval * (2 ** min(consecutive_errors, 6)), MAX_BACKOFF_SECONDS)
                _c2_logger.debug(f"C2 backoff: {backoff:.0f}s (errors: {consecutive_errors})")
                await asyncio.sleep(backoff)
            else:
                await asyncio.sleep(sync_interval)


async def send_system_message(text: str):
    await broadcast_data({"event": "system_update", "data": ensure_serializable({"message": text})})


def send_system_message_sync(text: str):
    if g.main_event_loop:
        asyncio.run_coroutine_threadsafe(send_system_message(text), g.main_event_loop)


