"""MeshDash — Self-hosted Meshtastic dashboard. R3.0."""

# ── R3.0 Self-Heal Bootstrap ──
def _md_r3_bootstrap():
    import os, sys, shutil, time, subprocess

    if os.path.exists("data/.r3_bootstrap_done"):
        return

    has_r3_core = os.path.isdir("core") and os.path.exists("core/__init__.py")
    has_r2_stale = os.path.exists("system.py")

    if not has_r3_core:
        return

    if os.path.exists("data/.r3_bootstrap_done"):
        return

    print("=" * 60, flush=True)
    print("  R3.0 SELF-HEAL: dirty R2.x overlay detected", flush=True)
    print("  This boot will: backup → migrate → rebuild-venv → restart", flush=True)
    print("=" * 60, flush=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = f"mesh-dash_backup_{ts}"
    backup_data = os.path.join(backup_dir, "data")
    new_data = os.path.join(".", "data")

    try:
        print(f"  [backup] Full backup → {backup_dir}", flush=True)
        os.makedirs(backup_data)
        for item in sorted(os.listdir(".")):
            if item in ("mesh-dash_venv", ".git", "__pycache__", backup_dir) or item.startswith("mesh-dash_backup_"):
                continue
            src = os.path.join(".", item); dst = os.path.join(backup_dir, item)
            try:
                if os.path.isdir(src): shutil.copytree(src, dst)
                else: shutil.copy2(src, dst)
            except Exception as e:
                print(f"    [warn] backup {item}: {e}", flush=True)

        print("  [migrate] Migrating databases root → data/", flush=True)
        os.makedirs(new_data, exist_ok=True)
        migrated = 0
        data_patterns = (".db", ".db-shm", ".db-wal", ".db-journal")
        json_names = {"slots.json", "geocode_cache.json"}
        for item in sorted(os.listdir(".")):
            ip = os.path.join(".", item)
            if not os.path.isfile(ip): continue
            if item.endswith(data_patterns) or item in json_names:
                dest = os.path.join(new_data, item)
                if not os.path.exists(dest):
                    shutil.move(ip, dest)
                    migrated += 1
                    print(f"    [ok] {item} → data/", flush=True)
        print(f"    [info] {migrated} data file(s) migrated", flush=True)

        print("  [clean] Removing stale R2.x files...", flush=True)
        r2_stale = [
            "system.py", "connection.py", "mqtt_connection.py", "meshcore_connection.py",
            "auto_reply_api.py", "auto_reply.py", "monitor.py",
            "tasks_api.py", "task_scheduler.py", "webserial_api.py",
            "run_meshdash.sh", "restart.sh", "README.md",
        ]
        removed = 0
        for f in r2_stale:
            if os.path.exists(f):
                try:
                    if os.path.isdir(f):
                        shutil.rmtree(f)
                    else:
                        os.remove(f)
                    removed += 1
                except Exception as e:
                    print(f"    [warn] {f}: {e}", flush=True)

        r2_static_files = [
            "static/js/views/analytics.js", "static/js/views/autoreply.js",
            "static/js/views/compare.js", "static/js/views/monitor.js",
            "static/js/views/traceroute.js",
            "static/views/analytics.html", "static/views/autoreply.html",
            "static/views/compare.html", "static/views/monitor.html",
            "static/views/traceroute.html", "static/visualizer.html",
            "static/.new",
        ]
        for f in r2_static_files:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        print(f"    [clean] {removed} stale file(s) removed", flush=True)

        print("  [config] Patching config for R3.0...", flush=True)
        config_file = ".mesh-dash_config"
        if os.path.exists(config_file):
            with open(config_file) as f:
                cfg_lines = f.readlines()
            patched_lines = []
            keys_seen = set()
            for line in cfg_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    patched_lines.append(line)
                    continue
                if "=" in stripped:
                    k = stripped.split("=")[0].strip()
                    keys_seen.add(k)
                    if k == "PUBLIC_MODE":
                        patched_lines.append("PUBLIC_MODE=False\n")
                        print("    [ok] PUBLIC_MODE → False", flush=True)
                        continue
                    if k == "DB_PATH":
                        if os.path.exists(os.path.join(new_data, os.path.basename(stripped.split("=")[1].strip().strip('\"')))):
                            patched_lines.append("DB_PATH=data/meshtastic_data.db\n")
                            print("    [ok] DB_PATH → data/meshtastic_data.db", flush=True)
                            continue
                    if k == "TASK_DB_PATH":
                        if os.path.exists(os.path.join(new_data, "task.db")):
                            patched_lines.append("TASK_DB_PATH=data/task.db\n")
                            print("    [ok] TASK_DB_PATH → data/task.db", flush=True)
                            continue
                patched_lines.append(line)
            if "PUBLIC_MODE" not in keys_seen:
                patched_lines.append("PUBLIC_MODE=False\n")
                print("    [ok] PUBLIC_MODE=False added", flush=True)
            c2_key = None
            for line in patched_lines:
                if line.startswith("COMMUNITY_API_KEY=") or line.startswith('COMMUNITY_API_KEY="'):
                    c2_key = line.split("=")[1].strip().strip('"')
                    break
            if c2_key and c2_key != "YOUR_SUPER_SECRET_API_KEY_REPLACE_ME":
                print("  [fetch] Fetching admin credentials from C2 server...", flush=True)
                _username = None
                _password = None
                for _ep in [
                    f"user_setup_core.php?action=view_config&key={c2_key}",
                    f"c2_com_api.php?view=api&action=get_install_config&api_key={c2_key}",
                ]:
                    try:
                        import urllib.request as _ur, json as _json
                        _url = "https://meshdash.co.uk/" + _ep
                        _headers = {"User-Agent": "MeshDash-R3Bootstrap/1.0"}
                        if "c2_com_api" in _ep:
                            _headers["X-Api-Key"] = c2_key
                        _req = _ur.Request(_url, headers=_headers)
                        with _ur.urlopen(_req, timeout=10) as _resp:
                            _data = _json.loads(_resp.read())
                        _username = _data.get("INITIAL_ADMIN_USERNAME") or _data.get("email") or _data.get("hidden_email")
                        _password = _data.get("INITIAL_ADMIN_PASSWORD") or _data.get("password") or _data.get("hidden_password")
                        if _username and _password:
                            break
                    except Exception:
                        continue
                if _username and _password:
                    patched_lines.append(f'INITIAL_ADMIN_USERNAME="{_username}"\n')
                    patched_lines.append(f'INITIAL_ADMIN_PASSWORD="{_password}"\n')
                    print(f"    [ok] Admin credentials restored: {_username}", flush=True)
                else:
                    print("    [warn] C2 credential fetch unavailable — setup wizard will be needed", flush=True)
            with open(config_file, "w") as f:
                f.writelines(patched_lines)
            print("    [ok] Config patched for R3.0", flush=True)
        else:
            with open(config_file, "w") as f:
                f.write("PUBLIC_MODE=False\n")
                f.write("DB_PATH=data/meshtastic_data.db\n")
            print("    [ok] Minimal R3.0 config created", flush=True)

        print("  [venv] Rebuilding Python virtual environment...", flush=True)

        venv_in_cwd = os.path.isdir("mesh-dash_venv")
        sys_bindir = os.path.dirname(sys.executable)
        docker_venv = sys_bindir == "/opt/venv/bin" or os.path.exists("/.dockerenv")

        if docker_venv:
            print("    [docker] Docker detected — installing new deps into /opt/venv", flush=True)
            pip_cmd = os.path.join(os.path.dirname(sys.executable), "pip")
            if not os.path.exists(pip_cmd):
                pip_args = [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", "requirements.txt"]
            else:
                pip_args = [pip_cmd, "install", "--no-cache-dir", "-r", "requirements.txt"]
            print("    [fetch] Installing dependencies (may take several minutes)...", flush=True)
            result = subprocess.run(pip_args, timeout=600, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"    [warn] pip install had issues: {result.stderr[-300:]}", flush=True)
            else:
                print("    [ok] Dependencies installed into /opt/venv", flush=True)
            old_venv = "/opt/venv"  # referenced later by restart path
        else:
            old_venv = "mesh-dash_venv"
            new_venv = "mesh-dash_venv_new"
            system_python = "/usr/bin/python3"
            if not os.path.exists(system_python):
                system_python = sys.executable  # fallback

            if os.path.exists(new_venv):
                shutil.rmtree(new_venv)

            subprocess.run([system_python, "-m", "venv", new_venv], check=True, timeout=120)
            new_pip = os.path.join(new_venv, "bin", "pip")
            if not os.path.exists(new_pip):
                subprocess.run([os.path.join(new_venv, "bin", "python"), "-m", "ensurepip", "--upgrade"], timeout=60)

            print("    [fetch] Installing dependencies (may take several minutes)...", flush=True)
            subprocess.run([new_pip, "install", "--upgrade", "pip", "-q"], timeout=60)
            subprocess.run(
                [new_pip, "install", "--no-cache-dir", "--default-timeout", "120", "-r", "requirements.txt"],
                timeout=600
            )
            print("    [ok] New venv built: " + new_venv, flush=True)

            print("  [restart] Swapping virtual environments...", flush=True)
            if os.path.exists(old_venv):
                ts_venv = "mesh-dash_venv_old_" + ts
                os.rename(old_venv, ts_venv)
                print("    Renamed old venv → " + ts_venv, flush=True)
            os.rename(new_venv, old_venv)
            print("    New venv activated", flush=True)

        with open("data/.r3_bootstrap_done", "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        with open("data/.r3_migration.log", "w") as f:
            f.write(f"R3.0 self-heal migration\n")
            f.write(f"Completed: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
            f.write(f"Backup: {backup_dir}\n")
            f.write(f"Databases migrated: {migrated}\n")
            f.write(f"R2.x stale files removed: {removed}\n")

        print(f"  [ok] Self-heal complete! Backup preserved at: {backup_dir}", flush=True)
        new_python = os.path.abspath(os.path.join(old_venv, "bin", "python3"))
        if not os.path.exists(new_python):
            new_python = os.path.abspath(os.path.join(old_venv, "bin", "python"))
        print("  [restart] Restarting with: " + new_python, flush=True)
        os.execv(new_python, [new_python] + sys.argv[1:])

    except PermissionError:
        print(f"  [warn] SELF-HEAL: cannot write backup (PermissionError) — likely volume-mounted data/ dir", flush=True)
        print(f"  [info] Skipping self-heal. Touching bootstrap marker to prevent restart loop.", flush=True)
        try:
            os.makedirs("data", exist_ok=True)
            with open("data/.r3_bootstrap_done", "w") as f:
                f.write("skipped (PermissionError on backup dir creation)\n")
            print(f"  [ok] Bootstrap marker set. Starting normally.", flush=True)
        except Exception:
            pass  # absolutely nothing we can do here — let it try
        sys.exit(0)

    except Exception as e:
        print(f"  [err] Self-heal FAILED: {e}", flush=True)
        print(f"  [backup] Backup preserved at: {backup_dir}", flush=True)
        print(f"  Manual recovery: mv {backup_dir}/* . && rebuild venv", flush=True)
        sys.exit(1)

_md_r3_bootstrap()
del _md_r3_bootstrap

# ── Imports ──

import sys as _sys
_sys.modules['meshtastic_dashboard'] = _sys.modules['__main__']

# stdlib
import argparse
import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import shlex
import socket
import sqlite3
import statistics
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path as PathLib
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Union,
)
from urllib.parse import urlparse

# third-party
import fnmatch
import httpx
import requests
import shutil
import uvicorn
from bs4 import BeautifulSoup
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from passlib.context import CryptContext
from pubsub import pub
from requests.exceptions import RequestException
from sse_starlette.sse import EventSourceResponse

# meshtastic
import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic import portnums_pb2, remote_hardware_pb2, admin_pb2, channel_pb2
from meshtastic.remote_hardware import RemoteHardwareClient
try:
    from meshtastic.protobuf import mesh_pb2 as _mesh_pb2
except ImportError:
    from meshtastic import mesh_pb2 as _mesh_pb2

# local
import core.globals as _g
from core.auth import verify_password, get_password_hash, create_access_token, create_preauth_token, verify_preauth_token, generate_backup_codes, verify_totp_code, verify_backup_code, verify_csrf, _generate_csrf_token, get_current_active_user, ensure_serializable
from core.background import connection_heartbeat_worker, save_metrics_periodically, prune_history_periodically, update_stats_periodically, plugin_watchdog_worker, _attempt_plugin_recovery
from core.broadcast import broadcast_data, broadcast_stats, broadcast_stats_for_slot, _resolve_slot_id_for_interface
from core.c2 import C2ActivityLogger, remote_c2_worker_enhanced, _sign_payload, _c2_headers, _c2_query, _path_matches_pattern, _clamp_params, _sanitize_path, send_system_message, send_system_message_sync, _resolve_tier_endpoints, execute_meshtastic_command
from core.config_loader import load_configuration, _save_slots_file, _load_slots_file, _r, _resolve_base, _resolve_community, _resolve_heartbeat, _resolve_versions
from core.data import MeshtasticData
from core.database import DatabaseManager
from core.evidence import SourceEvidence, detect_packet_source, _update_node_source_evidence, _get_node_rf_history
from core.geocode import _load_geocode_cache, _save_geocode_cache, _geocode_reverse, _cache_key
from core.logging_utils import MemoryLogHandler, _attach_plugin_log_handler
from core.map_utils import _load_maps_config, _save_maps_config, _get_mbtiles_conn, _close_mbtiles_conn
from core.middleware import _inject_sw_header, _inject_request_id, _security_headers, no_cache, _require_admin, _resolve_cors_origins, _check_login_not_locked, _record_login_failure, _clear_login_failure
from core.node_config import _nc_int_to_ip, _nc_ip_to_int, _nc_flatten_message, _nc_coerce, _nc_build_snapshot, _nc_apply_changes
from core.packet import _packet_processing_worker_for_slot, packet_processing_worker, on_receive, on_fast_rx, on_fast_tx, on_connection, on_node_updated, _make_slot_on_connection, _make_slot_on_node_updated
from core.routes.admin_routes import router as admin_routes
from core.routes.api_routes import router as api_routes
from core.routes.connection_routes import router as connection_routes
from core.routes.map_routes import router as map_routes
from core.routes.mesh_routes import router as mesh_routes
from core.routes.node_config_routes import router as node_config_routes
from core.routes.node_routes import router as node_routes
from core.routes.packet_routes import router as packet_routes
from core.routes.plugin_routes import router as plugin_routes
from core.routes.schemas import User, TokenData, NodeSlot, MessageRequest, URLRequest, WebsiteMonitorRequest, ConsoleRequest, TracerouteRequest, ConfigUpdateRequest, RemoteInstallRequest, SlotCreateRequest, NodeConfigSaveRequest
from core.routes.slot_routes import router as slot_routes
from core.routes.web_routes import router as web_routes
from core.sync import _perform_background_sync_for_slot, perform_background_sync, _remove_keys_from_config
from core.update import check_and_apply_update
from core.utils import validate_url, get_node_registry
from core.version import check_version_periodically, _parse_version_number, available_plugins

router = APIRouter()

# ── Constants & Config ──

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

check_and_apply_update()

# ── Bootstrap restart after upgrade ──
# If _bootstrap marker exists, we just came from an update.
# Do one clean restart so the dashboard starts fresh with the new code.
_bootstrap_flag = os.path.join(DATA_DIR, "_bootstrap")
if os.path.exists(_bootstrap_flag):
    try:
        os.remove(_bootstrap_flag)
    except Exception:
        pass
    logger = logging.getLogger("meshtastic_dashboard")
    logger.info("[restart] Bootstrap: fresh R3.1 install detected. Restarting in 2s for clean state...")
    time.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── RX Logger (disabled) ──
rx_logger = logging.getLogger("rx_logger")
rx_logger.setLevel(logging.INFO)
rx_logger.propagate = False

TOPIC_SENT = "meshtastic.sent"

# ── Connection Imports ──
try:
    from core.connections.meshtastic import MeshtasticConnectionManager
except ImportError:
    logging.error("CRITICAL: Could not import MeshtasticConnectionManager from core.connections.meshtastic.")
    sys.exit(1)

try:
    from core.connections.mqtt import MQTTConnectionManager
    _HAS_MQTT = True
except ImportError as _mqtt_import_err:
    logging.warning("MQTT support not available: %s", _mqtt_import_err)
    _HAS_MQTT = False
    MQTTConnectionManager = None

try:
    from core.connections.meshcore import MeshCoreConnectionManager
    _HAS_MESHCORE = True
except ImportError as _mc_import_err:
    logging.warning("MeshCore support not available: %s", _mc_import_err)
    _HAS_MESHCORE = False
    MeshCoreConnectionManager = None

try:
    from pydantic import BaseModel as PydanticBaseModel
    from pydantic import Field, field_validator, model_validator
    PYDANTIC_V2 = True
except ImportError:
    from pydantic import BaseModel as PydanticBaseModel  # type: ignore
    from pydantic import Field  # type: ignore
    PYDANTIC_V2 = False
    field_validator = None
    model_validator = None

try:
    from core.tasks import init_tasks_db, tasks_router
except ImportError as import_err:
    logging.basicConfig(level=logging.INFO)
    logging.critical(f"FATAL: Could not import from core.tasks. Error: {import_err}")
    sys.exit(1)

try:
    from core.scheduler import run_scheduler_periodically
except ImportError as import_err:
    logging.critical(f"FATAL: Could not import from core.scheduler. Error: {import_err}")
    sys.exit(1)

try:
    from core.webserial import web_serial_router, WEB_SERIAL_ENABLED as _WS_ENABLED
    WEB_SERIAL_FEATURE = _WS_ENABLED
except ImportError as _ws_err:
    logging.info(f"Web Serial API not available: {_ws_err}")
    WEB_SERIAL_FEATURE = False
    web_serial_router = None

AUTO_REPLY_ENABLED = False  # Now handled by plugin system

try:
    from core.config import ABS_DASH_CONFIG_PATH
    SYSTEM_CONFIG_ENABLED = True
except ImportError as import_err:
    logging.info(f"System Config API feature disabled: {import_err}")
    SYSTEM_CONFIG_ENABLED = False
    ABS_DASH_CONFIG_PATH = "N/A"

import importlib.util

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()

os.makedirs(DATA_DIR, exist_ok=True)

PLUGIN_DIR = os.path.join(SCRIPT_DIR, "plugins")
if not os.path.exists(PLUGIN_DIR):
    os.makedirs(PLUGIN_DIR)

PLUGIN_REGISTRY: Dict[str, Dict] = {}

_plugin_watchdog: Dict[str, float] = {}
_PLUGIN_HANG_TIMEOUT = 120  # seconds before a hung plugin is flagged & its routes blocked

_PLUGIN_LOG_MAX_LINES = 250

_plugin_log_handlers: Dict[str, "MemoryLogHandler"] = {}


# ── Plugin Engine ──
class PluginManager:
    contexts: Dict[str, Dict[str, Any]] = {}  # Stores plugin-provided context data

    @staticmethod
    def load_all(app: "FastAPI"):
        logger = logging.getLogger("plugin_manager")
        logger.info("? Mounting Plugin Routes & Static Files...")
        try:
            items = sorted(os.listdir(PLUGIN_DIR))
        except Exception as e:
            logger.error(f"Cannot list plugin directory: {e}")
            return
        for item in items:
            plugin_path = os.path.join(PLUGIN_DIR, item)
            manifest_path = os.path.join(plugin_path, "manifest.json")
            if os.path.isdir(plugin_path) and os.path.exists(manifest_path):
                PluginManager.load_plugin(app, plugin_path, manifest_path, logger)

    @staticmethod
    def load_plugin(app: "FastAPI", plugin_path: str, manifest_path: str, logger):
        pid = os.path.basename(plugin_path)  # fallback id before manifest parse
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("Manifest is empty")
                manifest = json.loads(content)

            pid = manifest.get("id", pid)
            if not re.match(r"^[a-zA-Z0-9_-]+$", pid):
                raise ValueError(f"Plugin id '{pid}' contains invalid characters")

            if "watchdog" not in manifest:
                PLUGIN_REGISTRY[pid] = {
                    "manifest": manifest,
                    "status": "invalid_manifest",
                    "error": 'Missing required field \"watchdog\" in manifest.json. '
                             'Set to true (monitored) or false (unmonitored).',
                    "path": plugin_path,
                    "module": None,
                    "loaded_at": time.time(),
                }
                logger.error(
                    f"? Plugin '{pid}' rejected: manifest.json is missing required "
                    f"field \"watchdog\". Add \"watchdog\": true or \"watchdog\": false."
                )
                return

            PLUGIN_REGISTRY[pid] = {
                "manifest": manifest,
                "status": "loading",
                "error": None,
                "path": plugin_path,
                "module": None,
                "loaded_at": time.time(),
            }

            state_file = os.path.join(plugin_path, ".disabled")
            if os.path.exists(state_file):
                PLUGIN_REGISTRY[pid]["status"] = "stopped"
                logger.info(f"?  Plugin {pid} is stopped (disabled marker found).")
                return

            static_dir = os.path.join(plugin_path, "static")
            static_prefix = manifest.get("static_prefix", f"/static/plugins/{pid}")
            if os.path.exists(static_dir):
                app.mount(static_prefix, StaticFiles(directory=static_dir), name=f"plugin_static_{pid}")

            entry_file = os.path.join(plugin_path, manifest.get("entry_point", "main.py"))
            if os.path.exists(entry_file):
                plugin_module = PluginManager._import_with_timeout(pid, entry_file, timeout=10)
                if plugin_module is None:
                    raise RuntimeError(f"Plugin '{pid}' module import timed out (>10s)")

                PLUGIN_REGISTRY[pid]["module"] = plugin_module

                if hasattr(plugin_module, "plugin_router"):
                    def _make_state_check(plugin_id: str):
                        async def plugin_state_check():
                            entry = PLUGIN_REGISTRY.get(plugin_id, {})
                            if entry.get("status") != "running":
                                raise HTTPException(
                                    503,
                                    detail={
                                        "detail": f"Plugin '{plugin_id}' is not running (status={entry.get('status')}).",
                                        "status": entry.get("status"),
                                        "plugin_id": plugin_id,
                                    }
                                )
                            last_hb = _plugin_watchdog.get(plugin_id)
                            if last_hb is not None and (time.time() - last_hb) > _PLUGIN_HANG_TIMEOUT:
                                PLUGIN_REGISTRY[plugin_id]["status"] = "hung"
                                PLUGIN_REGISTRY[plugin_id]["error"] = "Plugin task hang detected  stopped"
                                raise HTTPException(
                                    503,
                                    detail={
                                        "detail": f"Plugin '{plugin_id}' is hung and has been stopped.",
                                        "status": "hung",
                                        "plugin_id": plugin_id,
                                    }
                                )
                        return plugin_state_check

                    app.include_router(
                        plugin_module.plugin_router,
                        prefix=manifest.get("router_prefix", f"/api/plugins/{pid}"),
                        dependencies=[Depends(_make_state_check(pid)), Depends(verify_csrf)],
                    )

            PLUGIN_REGISTRY[pid]["status"] = "running"
            _attach_plugin_log_handler(pid)
            if manifest.get("watchdog", False):
                _plugin_watchdog[pid] = time.time()
                logger.info(f"? Watchdog enabled for plugin: {pid}")
            logger.info(f" Plugin mounted: {manifest.get('name', pid)}")

        except Exception as e:
            logger.error(f" Plugin Mount Crash ({plugin_path}): {e}", exc_info=True)
            PLUGIN_REGISTRY[pid] = PLUGIN_REGISTRY.get(pid, {
                "manifest": {}, "path": plugin_path, "module": None, "loaded_at": time.time()
            })
            PLUGIN_REGISTRY[pid]["status"] = "crashed"
            PLUGIN_REGISTRY[pid]["error"] = str(e)

    @staticmethod
    def _import_with_timeout(pid: str, entry_file: str, timeout: int = 10):
        """Import a plugin module in a thread with a hard timeout. Returns None on timeout."""
        result: Dict[str, Any] = {"module": None, "error": None}
        event = threading.Event()

        def _do_import():
            try:
                spec = importlib.util.spec_from_file_location(f"plugin_{pid}", entry_file)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result["module"] = mod
            except Exception as e:
                result["error"] = str(e)
            finally:
                event.set()

        t = threading.Thread(target=_do_import, daemon=True)
        t.start()
        if not event.wait(timeout=timeout):
            logging.getLogger("plugin_manager").error(
                f" Plugin '{pid}' import thread timed out after {timeout}s"
            )
            return None
        if result["error"]:
            raise RuntimeError(result["error"])
        return result["module"]

    @staticmethod
    def init_contexts(context: dict):
        logger = logging.getLogger("plugin_manager")
        for pid, data in PLUGIN_REGISTRY.items():
            if data["status"] == "running":
                mod = data.get("module")
                if mod and hasattr(mod, "init_plugin"):
                    try:
                        ctx = context.copy()
                        ctx["logger"] = logging.getLogger(f"plugin.{pid}")
                        _attach_plugin_log_handler(pid)  # ensure log buffer exists
                        ctx["plugin_watchdog"] = _plugin_watchdog  # allow plugins to heartbeat
                        ctx["plugin_id"] = pid
                        result: Dict[str, Any] = {"ok": False, "error": None}
                        ev = threading.Event()

                        def _run_init(m=mod, c=ctx, r=result, e=ev):
                            try:
                                m.init_plugin(c)
                                r["ok"] = True
                            except Exception as ex:
                                r["error"] = str(ex)
                            finally:
                                e.set()

                        t = threading.Thread(target=_run_init, daemon=True)
                        t.start()
                        if not ev.wait(timeout=15):
                            logger.error(f" Plugin '{pid}' init_plugin timed out. Marking crashed.")
                            data["status"] = "crashed"
                            data["error"] = "init_plugin timed out"
                        elif not result["ok"]:
                            logger.error(f" Plugin '{pid}' init crashed: {result['error']}")
                            data["status"] = "crashed"
                            data["error"] = result["error"]
                        else:
                            logger.info(f" Plugin {pid} context injected.")
                            for key, value in ctx.items():
                                if key.endswith("_plugin"):
                                    PluginManager.contexts[key] = value
                                    logger.info(f"    Stored context key: {key}")
                            if data.get("manifest", {}).get("watchdog", False):
                                _plugin_watchdog[pid] = time.time()
                    except Exception as e:
                        logger.error(f" Plugin '{pid}' init_contexts outer error: {e}")
                        data["status"] = "crashed"
                        data["error"] = str(e)

DEFAULT_TARGET_HOST = "192.168.0.0"
DEFAULT_TARGET_PORT = 4403
DEFAULT_LOG_LEVEL_STR = "INFO"
DEFAULT_WEBSERVER_PORT = 8181
DEFAULT_WEBSERVER_HOST = "0.0.0.0"
DEFAULT_DB_PATH = "meshtastic_data.db"
DEFAULT_TASK_DB_PATH = "tasks.db"
WATCHDOG_AUTO_RECOVER = True   # Auto-recover hung plugins (attempt 3x, 5s apart)
DEFAULT_MAX_PACKETS_MEMORY = 200
DEFAULT_AVERAGE_METRICS_HISTORY_DAYS = 1
CONFIG_FILE_NAME = ".mesh-dash_config"
DEFAULT_AUTH_SECRET_KEY = secrets.token_hex(32)

_SD = [
    chr(0x68), chr(0x74), chr(0x74), chr(0x70), chr(0x73),
    chr(0x3a), chr(0x2f), chr(0x2f),
]
_AH = [chr(0x6d), chr(0x65), chr(0x73), chr(0x68)]
_AT = list(".co" + "." + "uk")
_EP1 = ["/", "c", "2", "_", "c", "o", "m", "_", "a", "p", "i", ".", "p", "h", "p"]
_EP2 = ["/", "c", "2", "_", "a", "p", "i", ".", "p", "h", "p"]
_EP3 = ["/", "v", "e", "r", "s", "i", "o", "n", "s"]

DEFAULT_AUTH_TOKEN_EXPIRE_MINUTES = 10080

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()

CONFIG_FILE_PATH = os.path.join(DATA_DIR, CONFIG_FILE_NAME)

_old_config = os.path.join(SCRIPT_DIR, CONFIG_FILE_NAME)
if os.path.exists(_old_config) and not os.path.exists(CONFIG_FILE_PATH):
    try:
        shutil.copy2(_old_config, CONFIG_FILE_PATH)
        print(f"  [backup] Migrated config: {_old_config} → {CONFIG_FILE_PATH}", flush=True)
    except Exception as _e:
        print(f"  [warn] Config migration failed: {_e}", flush=True)
    try:
        os.remove(_old_config)
        print(f"  [clean] Removed old config from app root", flush=True)
    except Exception:
        pass  # Don't fail boot over this

SLOTS_FILE_PATH  = os.path.join(DATA_DIR, "slots.json")
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")
VIEWS_DIR = os.path.join(STATIC_DIR, "views")
LOGIN_HTML_PATH = os.path.join(STATIC_DIR, "login.html")
INDEX_HTML_PATH = os.path.join(STATIC_DIR, "index.html")
NETWORK_HTML_PATH = os.path.join(VIEWS_DIR, "connection.html")
MAP_HTML_PATH = os.path.join(VIEWS_DIR, "map.html")
DMES_HTML_PATH = os.path.join(VIEWS_DIR, "dmes.html")
SETTINGS_HTML_PATH = os.path.join(VIEWS_DIR, "settings.html")
SENSORS_HTML_PATH = os.path.join(VIEWS_DIR, "iot.html")
HOOK_HTML_PATH = os.path.join(VIEWS_DIR, "overview.html")
TASKS_HTML_PATH = os.path.join(VIEWS_DIR, "tasks.html")
DOX_HTML_PATH = os.path.join(VIEWS_DIR, "overview.html")
PUBLIC_HTML_PATH = os.path.join(VIEWS_DIR, "channels.html")
COMPARE_HTML_PATH = os.path.join(VIEWS_DIR, "overview.html")
SHARK_HTML_PATH = os.path.join(VIEWS_DIR, "shark.html")
PLUGINS_HTML_PATH = os.path.join(VIEWS_DIR, "plugins.html")

FAVICON_PATH = os.path.join(STATIC_DIR, "favicon.ico")

loaded_config = load_configuration(CONFIG_FILE_PATH)

TARGET_HOST = loaded_config["MESHTASTIC_HOST"]
TARGET_PORT = int(loaded_config["MESHTASTIC_PORT"])
LOG_LEVEL_STR = loaded_config["LOG_LEVEL"].upper()
WEBSERVER_PORT = int(loaded_config["WEBSERVER_PORT"])
WEBSERVER_HOST = loaded_config["WEBSERVER_HOST"]
DB_PATH = loaded_config["DB_PATH"]
TASK_DB_PATH = loaded_config["TASK_DB_PATH"]
MAX_PACKETS_IN_MEMORY = int(loaded_config["MAX_PACKETS_MEMORY"])
AVERAGE_METRICS_HISTORY_DAYS = int(loaded_config["HISTORY_DAYS"])
AUTH_SECRET_KEY = loaded_config["AUTH_SECRET_KEY"]
AUTH_TOKEN_EXPIRE_MINUTES = int(loaded_config["AUTH_TOKEN_EXPIRE_MINUTES"])
COMMUNITY_API_KEY = loaded_config.get("COMMUNITY_API_KEY", "YOUR_SUPER_SECRET_API_KEY_REPLACE_ME")

DATA_RETENTION_DAYS: int = int(loaded_config.get("DATA_RETENTION_DAYS", 7))

PUBLIC_MODE: bool = bool(loaded_config.get("PUBLIC_MODE", False))

GEOCODE_CACHE_FILE = PathLib(DATA_DIR) / "geocode_cache.json"
_geocode_cache: dict = {}
_geocode_last_request: float = 0.0
_NOMINATIM_MIN_INTERVAL = 1.1
_NOMINATIM_UA = "MeshDash/2.0 (meshtastic dashboard)"

if not AUTH_SECRET_KEY or AUTH_SECRET_KEY == DEFAULT_AUTH_SECRET_KEY:
    logging.warning("SECURITY WARNING: AUTH_SECRET_KEY is default or empty. Please change it in config.")
    if not AUTH_SECRET_KEY:
        AUTH_SECRET_KEY = "TEMPORARY_INSECURE_KEY"

LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
logger = logging.getLogger("meshtastic_dashboard")

if LOG_LEVEL > logging.DEBUG:
    for log_name in [
        "meshtastic", "pubsub", "bleak", "watchfiles",
        "uvicorn.access", "httpx", "jose", "passlib",
    ]:
        logging.getLogger(log_name).setLevel(logging.WARNING)


# ── Auth ──
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"

# ── Packet Source Detection ──
_node_source_evidence: Dict[str, Dict] = {}
_node_source_lock = threading.Lock()

_MQTT_CLOCK_SKEW_THRESHOLD = 45.0

_NULL_SNR_VALUES = {0, 0.0, None}
_NULL_RSSI_VALUES = {0, None}

db_manager = DatabaseManager(DB_PATH, ephemeral=PUBLIC_MODE)
_g.db_manager = db_manager

meshtastic_data = MeshtasticData(db_manager, MAX_PACKETS_IN_MEMORY, slot_id="node_0")
_g.meshtastic_data = meshtastic_data

packet_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)


# ── Core State — SSE, Queue, Registry ──
background_tasks: Set = set()
connection_manager: Optional[MeshtasticConnectionManager] = None
_g.connection_manager = connection_manager
main_event_loop: Optional[asyncio.AbstractEventLoop] = None

MAX_SSE_CLIENTS = 50
_g.MAX_SSE_CLIENTS = MAX_SSE_CLIENTS
_g.MAX_PACKETS_IN_MEMORY = MAX_PACKETS_IN_MEMORY
_sse_client_id = 0
sse_queues: Dict[int, asyncio.Queue] = {}
sse_queues_lock = asyncio.Lock()
_g.sse_queues_lock = sse_queues_lock
_g.sse_queues = sse_queues
import core.sse as _core_sse
_core_sse.sse_queues = sse_queues
_core_sse.sse_queues_lock = sse_queues_lock
sync_lock = asyncio.Lock()
_slot_sync_locks: Dict[str, asyncio.Lock] = {}  # per-slot sync locks for additional nodes

all_sse_queues: Dict[int, asyncio.Queue] = {}
all_sse_queues_lock = asyncio.Lock()
_g.all_sse_queues_lock = all_sse_queues_lock
_g.all_sse_queues = all_sse_queues
_core_sse.all_sse_queues = all_sse_queues
_core_sse.all_sse_queues_lock = all_sse_queues_lock

from dataclasses import dataclass, field as dc_field

NODE_REGISTRY: Dict[str, "NodeSlot"] = {}
_g.NODE_REGISTRY = NODE_REGISTRY
_g.PLUGIN_REGISTRY = PLUGIN_REGISTRY

MAX_SLOTS = 16

_g.loaded_config = loaded_config
_g.PUBLIC_MODE = PUBLIC_MODE
_g.AUTH_SECRET_KEY = AUTH_SECRET_KEY
_g.AUTH_TOKEN_EXPIRE_MINUTES = AUTH_TOKEN_EXPIRE_MINUTES
_g.COMMUNITY_API_KEY = COMMUNITY_API_KEY
_g.TARGET_HOST = TARGET_HOST
_g.TARGET_PORT = TARGET_PORT
_g.MESHTASTIC_CONNECTION_TYPE = loaded_config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL")
_g.MESHTASTIC_SERIAL_PORT = loaded_config.get("MESHTASTIC_SERIAL_PORT", "")
_g.MESHTASTIC_BLE_MAC = loaded_config.get("MESHTASTIC_BLE_MAC", "")
_g.CONFIG_FILE_PATH = CONFIG_FILE_PATH
_g.STATIC_DIR = STATIC_DIR
_g.LOGIN_HTML_PATH = LOGIN_HTML_PATH
_g.INDEX_HTML_PATH = INDEX_HTML_PATH
_g.NETWORK_HTML_PATH = NETWORK_HTML_PATH
_g.MAP_HTML_PATH = MAP_HTML_PATH
_g.DMES_HTML_PATH = DMES_HTML_PATH
_g.SETTINGS_HTML_PATH = SETTINGS_HTML_PATH
_g.SENSORS_HTML_PATH = SENSORS_HTML_PATH
_g.HOOK_HTML_PATH = HOOK_HTML_PATH
_g.TASKS_HTML_PATH = TASKS_HTML_PATH
_g.PLUGINS_HTML_PATH = PLUGINS_HTML_PATH
_g.PUBLIC_HTML_PATH = PUBLIC_HTML_PATH
_g.FAVICON_PATH = FAVICON_PATH
_g.DOX_HTML_PATH = DOX_HTML_PATH
_g.COMPARE_HTML_PATH = COMPARE_HTML_PATH
_g.SHARK_HTML_PATH = SHARK_HTML_PATH
_g.SCRIPT_DIR = SCRIPT_DIR
_g.PLUGIN_DIR = PLUGIN_DIR
_g.DATA_DIR = DATA_DIR
_g.DB_PATH = DB_PATH
_g.DATA_RETENTION_DAYS = DATA_RETENTION_DAYS
_g._plugin_watchdog = _plugin_watchdog
_g._plugin_log_handlers = _plugin_log_handlers
_g._PLUGIN_LOG_MAX_LINES = _PLUGIN_LOG_MAX_LINES

CSRF_TOKEN_BYTES = 32


# ── C2 Remote Access Tiers ──
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

_c2_logger = logging.getLogger("c2_bridge")

c2_activity = C2ActivityLogger()


# ── Lifespan — Startup / Shutdown ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop, connection_manager, background_tasks

    try:
        if not os.path.exists(STATIC_DIR):
            os.makedirs(STATIC_DIR)
        from core.routes.admin_routes import _create_setup_flags
        _create_setup_flags()
    except Exception as e:
        logger.warning(f"Could not create setup indicator file: {e}")

    main_event_loop = asyncio.get_running_loop()
    _g.main_event_loop = main_event_loop
    logger.info(f"--- Starting Dashboard v{app.version} (RX.X.X) ---")

    if not PUBLIC_MODE:
        iu = loaded_config.get("INITIAL_ADMIN_USERNAME")
        ip = loaded_config.get("INITIAL_ADMIN_PASSWORD")
        if iu and ip and not db_manager.get_user(iu):
            db_manager.create_user(iu, get_password_hash(ip), role=0)
            await asyncio.to_thread(_remove_keys_from_config, ["INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD"])

    _load_geocode_cache()

    if 'init_tasks_db' in globals() and callable(init_tasks_db):
        try:
            init_tasks_db(TASK_DB_PATH)
        except TypeError:
            logging.warning(f"init_tasks_db does not accept arguments. Using default path instead of {TASK_DB_PATH}.")
            init_tasks_db()

    connection_manager = MeshtasticConnectionManager(
        meshtastic_data, logger, slot_id="node_0",
        connection_params={
            "MESHTASTIC_CONNECTION_TYPE": loaded_config.get("MESHTASTIC_CONNECTION_TYPE", "SERIAL"),
            "MESHTASTIC_HOST": loaded_config.get("MESHTASTIC_HOST", "192.168.1.50"),
            "MESHTASTIC_PORT": str(loaded_config.get("MESHTASTIC_PORT", 4403)),
            "MESHTASTIC_SERIAL_PORT": loaded_config.get("MESHTASTIC_SERIAL_PORT", ""),
            "MESHTASTIC_BLE_MAC": loaded_config.get("MESHTASTIC_BLE_MAC", ""),
        }
    )
    _g.connection_manager = connection_manager

    _node_0_slot = NodeSlot(
        slot_id="node_0",
        label=loaded_config.get("MESHTASTIC_HOST", "Primary Radio"),
        meshtastic_data=meshtastic_data,
        db_manager=db_manager,
        connection_manager=connection_manager,
        packet_queue=packet_queue,
        sse_queues=sse_queues,
        sse_lock=sse_queues_lock,
    )
    NODE_REGISTRY["node_0"] = _node_0_slot

    if WEB_SERIAL_FEATURE and web_serial_router is not None:
        try:
            from core.webserial import configure_webserial
            configure_webserial(NODE_REGISTRY, get_current_active_user)
            logger.info(" Web Serial: slot registry injected")
        except Exception as _ws_cfg_err:
            logger.warning("Web Serial slot injection failed: %s", _ws_cfg_err)

    try:
        from core.scheduler import set_node_registry as _set_sched_reg
        _set_sched_reg(NODE_REGISTRY)
    except (ImportError, AttributeError):
        pass

    PluginManager.init_contexts({
        "db_manager": db_manager,
        "meshtastic_data": meshtastic_data,
        "connection_manager": connection_manager,
        "node_registry": NODE_REGISTRY,
        "event_loop": main_event_loop,
    })

    def safe_on_connection(interface, topic=pub.AUTO_TOPIC):
        try:
            _make_slot_on_connection(NODE_REGISTRY["node_0"])(interface, topic)
        except Exception as cb_err:
            logger.error("on_connection callback raised unexpectedly: %s", cb_err, exc_info=True)

    connection_manager.register_callbacks(on_receive, safe_on_connection, _make_slot_on_node_updated(NODE_REGISTRY["node_0"]))
    pub.subscribe(on_fast_rx, "meshtastic.receive")
    pub.subscribe(on_fast_tx, "meshtastic.sent")
    logger.info(" Connection manager initialised and callbacks registered.")

    _persisted = _load_slots_file()
    for _ps in _persisted:
        try:
            _sid = _ps["slot_id"]
            if _sid in NODE_REGISTRY:
                continue  # already registered
            if len(NODE_REGISTRY) >= MAX_SLOTS:
                logger.warning("?  Max slots reached  cannot restore slot '%s'.", _sid)
                continue

            _db_uuid = _ps.get("db_uuid", "")
            if _db_uuid:
                _db_path = f"meshtastic_data_{_db_uuid}.db" if not PUBLIC_MODE else ":memory:"
            else:
                _db_uuid = uuid.uuid4().hex
                _db_path = f"meshtastic_data_{_sid}.db" if not PUBLIC_MODE else ":memory:"
                logger.info("? Slot '%s' has no db_uuid  assigning %s (legacy migration)", _sid, _db_uuid)
            _slot_db = DatabaseManager(_db_path, ephemeral=PUBLIC_MODE)
            _slot_md = MeshtasticData(_slot_db, MAX_PACKETS_IN_MEMORY, slot_id=_sid)
            _slot_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

            _conn_type = _ps.get("connection_type", "TCP").upper()

            if _conn_type == "MQTT" and _HAS_MQTT:
                _mqtt_params = {
                    "MESHTASTIC_CONNECTION_TYPE": "MQTT",
                    "MQTT_BROKER":   _ps.get("mqtt_broker",   "mqtt.meshtastic.org"),
                    "MQTT_PORT":     str(_ps.get("mqtt_port",  1883)),
                    "MQTT_USERNAME": _ps.get("mqtt_username", ""),
                    "MQTT_PASSWORD": _ps.get("mqtt_password", ""),
                    "MQTT_TLS":      "true" if _ps.get("mqtt_tls", False) else "false",
                    "MQTT_REGION":   _ps.get("mqtt_region",  "EU_868"),
                    "MQTT_CHANNEL":  _ps.get("mqtt_channel", "#"),
                    "MQTT_NODE_ID":  _ps.get("mqtt_node_id", ""),
                    "MQTT_CLIENT_ID":   "",
                    "MQTT_ROOT_TOPIC":  "",
                }
                _slot_cm = MQTTConnectionManager(
                    _slot_md,
                    logging.getLogger(f"MQTTConnection.{_sid}"),
                    connection_params=_mqtt_params,
                    slot_id=_sid,
                )
                _slot_cm.set_packet_queue(_slot_q)

            elif _conn_type == "MESHCORE" and _HAS_MESHCORE:
                _mc_params = {
                    "MESHTASTIC_CONNECTION_TYPE": "MESHCORE",
                    "MESHCORE_TRANSPORT":    _ps.get("meshcore_transport",   "serial"),
                    "MESHCORE_SERIAL_PORT":  _ps.get("meshcore_serial_port", ""),
                    "MESHCORE_BAUD":         str(_ps.get("meshcore_baud",    115200)),
                    "MESHCORE_HOST":         _ps.get("meshcore_host",        ""),
                    "MESHCORE_PORT":         str(_ps.get("meshcore_port",    4000)),
                    "MESHCORE_BLE_MAC":      _ps.get("meshcore_ble_mac",     ""),
                    "MESHCORE_BLE_PIN":      _ps.get("meshcore_ble_pin",     ""),
                    "MESHCORE_LABEL":        _ps.get("label",                ""),
                }
                _slot_cm = MeshCoreConnectionManager(
                    _slot_md,
                    logging.getLogger(f"MeshCoreConnection.{_sid}"),
                    connection_params=_mc_params,
                    slot_id=_sid,
                )
                _slot_cm.set_packet_queue(_slot_q)

            elif _conn_type == "MESHCORE" and not _HAS_MESHCORE:
                logger.warning(
                    "?  Slot '%s' is MESHCORE but meshcore library is not installed. "
                    "Run: pip install meshcore --break-system-packages", _sid
                )
                continue

            else:
                _conn_params = {
                    "MESHTASTIC_CONNECTION_TYPE": _conn_type,
                    "MESHTASTIC_HOST":            _ps.get("host", DEFAULT_TARGET_HOST),
                    "MESHTASTIC_PORT":            str(_ps.get("port", 4403)),
                    "MESHTASTIC_SERIAL_PORT":     _ps.get("serial_port", ""),
                    "MESHTASTIC_BLE_MAC":         _ps.get("ble_mac", ""),
                }
                _slot_cm = MeshtasticConnectionManager(
                    _slot_md,
                    logging.getLogger(f"MeshConnection.{_sid}"),
                    connection_params=_conn_params,
                    slot_id=_sid,
                )

            _restored_slot = NodeSlot(
                slot_id=_sid,
                label=_ps.get("label", _sid),
                meshtastic_data=_slot_md,
                db_manager=_slot_db,
                connection_manager=_slot_cm,
                packet_queue=_slot_q,
                db_uuid=_db_uuid,
            )
            NODE_REGISTRY[_sid] = _restored_slot

            if _sid not in _slot_sync_locks:
                _slot_sync_locks[_sid] = asyncio.Lock()

            def _make_restore_task_handler(s):
                def _h(task):
                    try: task.result()
                    except asyncio.CancelledError: pass
                    except Exception as e: logger.error("Slot %s task crashed: %s", s.slot_id, e, exc_info=True)
                    finally: s.tasks.discard(task)
                return _h

            def _make_restore_rx(s):
                def _rx(packet, interface): on_receive(packet, interface)
                return _rx

            _slot_cm.register_callbacks(
                _make_restore_rx(_restored_slot),
                _make_slot_on_connection(_restored_slot),
                _make_slot_on_node_updated(_restored_slot),
            )
            for _coro_fn in (_slot_cm.connect_loop, lambda s=_restored_slot: _packet_processing_worker_for_slot(s)):
                _t = asyncio.create_task(_coro_fn())
                _t.set_name(f"Task-{_sid}-restore")
                _restored_slot.tasks.add(_t)
                _t.add_done_callback(_make_restore_task_handler(_restored_slot))

            logger.info(" Restored persisted slot '%s' (%s / %s)", _sid, _ps.get("label", ""), _conn_type)
        except Exception as _restore_err:
            logger.error(" Failed to restore slot '%s': %s", _ps.get("slot_id", "?"), _restore_err, exc_info=True)

    task_coros = [
        connection_manager.connect_loop,
        update_stats_periodically,
        save_metrics_periodically,
        prune_history_periodically,
        run_scheduler_periodically,
        check_version_periodically,
        packet_processing_worker,
        remote_c2_worker_enhanced,
        connection_heartbeat_worker,
        plugin_watchdog_worker,  # R2.x hang detection
    ]

    def handle_task_result(task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f" Background Task Crashed: {task.get_name()} | Error: {e}", exc_info=True)
        finally:
            background_tasks.discard(task)

    for coro_fn in task_coros:
        task = asyncio.create_task(coro_fn())
        task.set_name(f"Task-{coro_fn.__name__}")
        background_tasks.add(task)
        task.add_done_callback(handle_task_result)

    yield

    logger.info("--- Shutdown initiated ---")

    if not packet_queue.empty():
        pending = packet_queue.qsize()
        logger.info(f"? Draining {pending} pending packet(s) to database...")
        try:
            await asyncio.wait_for(packet_queue.join(), timeout=5.0)
            logger.info(" Packet queue drained.")
        except asyncio.TimeoutError:
            logger.warning(f" Drain timed out - {packet_queue.qsize()} packet(s) may not have been written.")

    if connection_manager is not None:
        logger.info("? Shutting down connection manager...")
        try:
            await asyncio.wait_for(connection_manager.shutdown(), timeout=8.0)
            logger.info(" Connection manager shut down.")
        except asyncio.TimeoutError:
            logger.warning(" Connection manager shutdown timed out.")
        except Exception as cm_err:
            logger.error(f"Connection manager shutdown error: {cm_err}")

    if background_tasks:
        logger.info(f"? Cancelling {len(background_tasks)} background task(s)...")
        for t in background_tasks:
            t.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)

    logger.info(" All background tasks stopped. Clean exit.")

_DEFAULT_CORS_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

_login_failures: Dict[str, Dict] = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300


# ── FastAPI App ──
app = FastAPI(
    title="Mesh Dash — Meshtastic Dashboard",
    version="R3.1.1",
    description="Monitor, manage, and automate your Meshtastic mesh network. Multi-radio dashboard with plugin system, C2 bridge, and real-time mesh analytics.",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
_g.app = app

app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connection_routes)
app.include_router(node_routes)
app.include_router(packet_routes)
app.include_router(mesh_routes)
app.include_router(api_routes)
app.include_router(plugin_routes)
app.include_router(admin_routes)
app.include_router(web_routes)
app.include_router(slot_routes)
app.include_router(map_routes)
app.include_router(node_config_routes)

app.middleware("http")(_inject_sw_header)
app.middleware("http")(_inject_request_id)
app.middleware("http")(_security_headers)

from core.routes import page_routes
page_routes.register_all(app, globals())

PluginManager.load_all(app)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Auth & User Management Routes ──
ROLE_LABELS = {0: "Admin", 1: "Operator", 2: "Spectator"}

# ── Extracted Route Modules ──
try:
    from core.routes.auth_routes import router as auth_router
except ImportError as e:
    logger.warning(f"Could not import auth_routes: {e}")
    auth_router = None

try:
    from core.routes.system_routes import router as system_router
except ImportError as e:
    logger.warning(f"Could not import system_routes: {e}")
    system_router = None

app.include_router(tasks_router, prefix="/api/tasks")

if WEB_SERIAL_FEATURE and web_serial_router is not None:
    app.include_router(web_serial_router, prefix="/api/webserial")
    logger.info(" Web Serial Bridge API mounted at /api/webserial")
else:
    logger.info("?  Web Serial Bridge disabled (WEB_SERIAL_ENABLED=False or import failed)")

if auth_router is not None:
    app.include_router(auth_router)
    logger.info(" Auth routes mounted")
else:
    logger.warning("?  Auth routes not available  some login/user routes may be missing")

if system_router is not None:
    app.include_router(system_router)
    logger.info(" System routes mounted")
else:
    logger.warning("?  System routes not available  some config/plugin routes may be missing")


# ── Node Configuration (Protobuf) ──
from google.protobuf.descriptor import FieldDescriptor as _FD
import struct as _struct

_NC_NUMERIC_TYPES = (
    _FD.TYPE_INT32, _FD.TYPE_INT64, _FD.TYPE_UINT32, _FD.TYPE_UINT64,
    _FD.TYPE_SINT32, _FD.TYPE_SINT64, _FD.TYPE_FIXED32, _FD.TYPE_FIXED64,
    _FD.TYPE_SFIXED32, _FD.TYPE_SFIXED64, _FD.TYPE_FLOAT, _FD.TYPE_DOUBLE
)
_NC_IP_FIELDS = {'ip', 'gateway', 'subnet', 'dns'}


# ── Offline Maps ──
MAPS_DIR = os.path.join(SCRIPT_DIR, "static", "maps")
os.makedirs(MAPS_DIR, exist_ok=True)
MAPS_CONFIG_FILE = os.path.join(DATA_DIR, "maps_config.json")
_g.MAPS_DIR = MAPS_DIR
_g.MAPS_CONFIG_FILE = MAPS_CONFIG_FILE

_mbtiles_cache: Dict[str, sqlite3.Connection] = {}
_mbtiles_lock = threading.Lock()

_EMPTY_TILE = base64.b64decode(
    "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
)

_download_state: Dict[str, Any] = {}  # single active download
_download_lock = asyncio.Lock()


# ── Entry Point ──
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=WEBSERVER_PORT)
    parser.add_argument("--host", default=WEBSERVER_HOST)
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--task-db-path", default=TASK_DB_PATH)
    parser.add_argument("--log-level", default=LOG_LEVEL_STR)
    args = parser.parse_args()

    DB_PATH = args.db_path
    TASK_DB_PATH = args.task_db_path
    if args.log_level != LOG_LEVEL_STR:
        logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))

    uvicorn.run(app, host=args.host, port=args.port)
