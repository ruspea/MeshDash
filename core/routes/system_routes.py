"""
System management routes (status, config, plugins, restart, updates).
Extracted from meshtastic_dashboard.py
"""
import core.globals as _globals
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel as PydanticBaseModel

from core.auth import User, get_current_active_user
from core.config import ABS_DASH_CONFIG_PATH, read_dash_config, write_dash_config

router = APIRouter(prefix="/api", tags=["system"])

# Lazy globals accessor (avoids circular imports at module load time)
def _get_globals():
    import core.globals as _g
    return {
        "PLUGIN_REGISTRY": _g.PLUGIN_REGISTRY,
        "_plugin_watchdog": getattr(_g, '_plugin_watchdog', None),
        "_plugin_log_handlers": getattr(_g, '_plugin_log_handlers', []),
        "_PLUGIN_LOG_MAX_LINES": getattr(_g, '_PLUGIN_LOG_MAX_LINES', 1000),
        "db_manager": _g.db_manager,
        "meshtastic_data": _g.meshtastic_data,
        "NODE_REGISTRY": _g.NODE_REGISTRY,
        "connection_manager": _g.connection_manager,
        "loaded_config": _g.loaded_config,
        "AUTH_SECRET_KEY": _g.AUTH_SECRET_KEY,
        "AUTH_TOKEN_EXPIRE_MINUTES": _g.AUTH_TOKEN_EXPIRE_MINUTES,
        "COMMUNITY_API_KEY": _g.COMMUNITY_API_KEY,
        "CONFIG_FILE_PATH": _g.CONFIG_FILE_PATH,
        "STATIC_DIR": _g.STATIC_DIR,
        "PLUGIN_DIR": _g.PLUGIN_DIR,
        "app": _g.app,
        "main_event_loop": getattr(_g, 'main_event_loop', None),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_heartbeat():
    """Resolve the community heartbeat URL from loaded config."""
    host = _globals.loaded_config.get("COMMUNITY_API_HOST", "https://meshdash.co.uk")
    return f"{host}/api/heartbeat"


# ---------------------------------------------------------------------------
# Status & Health
# ---------------------------------------------------------------------------

@router.get("/status")
async def api_status(request: Request, slot_id: str = "node_0"):
    """Returns system health with sliding session renewal."""
    from jose import jwt
    from datetime import datetime, timezone
    from core.auth import ALGORITHM, create_access_token

    g = _get_globals()
    PUBLIC_MODE = g["loaded_config"].get("PUBLIC_MODE", False)
    AUTH_SECRET_KEY = g["AUTH_SECRET_KEY"]
    AUTH_TOKEN_EXPIRE_MINUTES = g["AUTH_TOKEN_EXPIRE_MINUTES"]

    if not PUBLIC_MODE:
        try:
            token = request.cookies.get("access_token")
            if token and token.startswith("Bearer "):
                payload = jwt.decode(token.split(" ")[1], AUTH_SECRET_KEY, algorithms=[ALGORITHM])
                username = payload.get("sub")
                exp = payload.get("exp")
                now = datetime.now(timezone.utc).timestamp()
                time_left = (exp or 0) - now
                refresh_threshold = (AUTH_TOKEN_EXPIRE_MINUTES * 60) / 2
                if username and time_left < refresh_threshold:
                    new_token = create_access_token({"sub": username})
                    # Can't set cookie in JSONResponse directly — handled by middleware
        except Exception:
            pass

    NODE_REGISTRY = g["NODE_REGISTRY"]
    connection_manager = g["connection_manager"]
    meshtastic_data = g["meshtastic_data"]

    _slot = NODE_REGISTRY.get(slot_id) or NODE_REGISTRY.get("node_0")
    _cm = _slot.connection_manager if _slot else connection_manager
    _md = _slot.meshtastic_data if _slot else meshtastic_data
    hw_ready = _cm.is_ready.is_set() if _cm is not None else False
    return {
        "api_status": "online",
        "connection_status": _md.connection_status,
        "connection_state": _md._connection_state or "idle",
        "connection_detail": _md._connection_detail or "",
        "connection_transport": _md._connection_transport or "",
        "is_system_ready": hw_ready,
        "local_node_info": _md.local_node_info,
        "last_error": _md.last_error,
        "public_mode": PUBLIC_MODE,
        "slot_id": slot_id,
    }


@router.get("/system/connection_history")
async def api_conn_hist(limit: int = 60):
    g = _get_globals()
    import asyncio
    return await asyncio.to_thread(g["db_manager"].get_connection_history, limit)


# NOTE: All /api/system/plugins/*, /api/plugins/available, /api/plugins/bridges routes
# are served by plugin_routes.py (registered before this router).
# The previous duplicate definitions here have been removed.


# NOTE: /api/system/plugins/install and /api/system/plugins/install-remote
# are served by plugin_routes.py (registered before this router).


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config_models():
    """Lazily load Pydantic models for config from meshtastic_dashboard."""
    from core.auth import PYDANTIC_V2
    if PYDANTIC_V2:
        from pydantic import BaseModel as PydanticBaseModel, Field
    else:
        from pydantic import BaseModel as PydanticBaseModel  # type: ignore
        Field = None

    from typing import Optional

    class ConfigUpdateRequest(PydanticBaseModel):
        AUTH_SECRET_KEY: Optional[str] = None
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
        PUBLIC_MODE: Optional[bool] = None
        # Heartbeat API key/URLs are hardcoded server-side — not user-configurable.
        # C2 sync intervals, endpoint lists are hardcoded internally.

    return ConfigUpdateRequest


@router.get("/system/config")
async def get_system_config(user: User = Depends(get_current_active_user)):
    from collections import OrderedDict

    g = _get_globals()
    CONFIG_FILE_PATH = g["CONFIG_FILE_PATH"]
    from core.config import load_configuration
    import asyncio

    try:
        config = await asyncio.to_thread(load_configuration, CONFIG_FILE_PATH)
        # Ensure ordered output
        return JSONResponse(content=json.loads(json.dumps(config)))
    except Exception as e:
        logging.getLogger("meshtastic_dashboard").error(f"Failed to read config file: {e}")
        raise HTTPException(status_code=500, detail="Unable to read configuration")


@router.post("/system/config/update")
async def update_system_config(request: Request, user: User = Depends(get_current_active_user)):
    from datetime import datetime
    from core.auth import PYDANTIC_V2
    import asyncio

    ConfigUpdateRequest = _load_config_models()
    g = _get_globals()
    CONFIG_FILE_PATH = g["CONFIG_FILE_PATH"]
    meshtastic_data = g["meshtastic_data"]
    loaded_config = g["loaded_config"]

    # Parse JSON body and validate through Pydantic model
    try:
        raw_body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    cfg_obj = ConfigUpdateRequest(**raw_body)

    # Normalize request to dict
    if hasattr(cfg_obj, 'model_dump'):
        update_data = cfg_obj.model_dump(exclude_unset=True)
    elif hasattr(cfg_obj, 'dict'):
        update_data = cfg_obj.dict(exclude_unset=True)
    else:
        update_data = dict(cfg_obj)

    try:
        current_config = await asyncio.to_thread(
            __import__('core.config', fromlist=['load_configuration']).load_configuration,
            CONFIG_FILE_PATH
        )
        current_config.update(update_data)

        def _write_config():
            with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(f"# ---------------------------------------------------------\n")
                f.write(f"# MeshDash Configuration\n")
                f.write(f"# Updated via Web UI on {datetime.now().isoformat()}\n")
                f.write(f"# ---------------------------------------------------------\n\n")
                for key, value in current_config.items():
                    if key in ("INITIAL_ADMIN_USERNAME", "INITIAL_ADMIN_PASSWORD"):
                        continue
                    value_str = "" if value is None else ("True" if value is True else ("False" if value is False else str(value)))
                    f.write(f"{key}={value_str}\n")

        await asyncio.to_thread(_write_config)
        logging.getLogger("meshtastic_dashboard").info(f"✅ Configuration updated by user {user.username}")

        # Hot-reload global variables
        import meshtastic_dashboard as md
        reload_map = {
            "AUTH_SECRET_KEY": lambda v: setattr(md, "AUTH_SECRET_KEY", v),
            "AUTH_TOKEN_EXPIRE_MINUTES": lambda v: setattr(md, "AUTH_TOKEN_EXPIRE_MINUTES", int(v)),
            "MESHTASTIC_HOST": lambda v: setattr(md, "TARGET_HOST", v),
            "MESHTASTIC_PORT": lambda v: setattr(md, "TARGET_PORT", int(v)),
            "WEBSERVER_HOST": lambda v: setattr(md, "WEBSERVER_HOST", v),
            "WEBSERVER_PORT": lambda v: setattr(md, "WEBSERVER_PORT", int(v)),
            "DB_PATH": lambda v: setattr(md, "DB_PATH", v),
            "TASK_DB_PATH": lambda v: setattr(md, "TASK_DB_PATH", v),
            "HISTORY_DAYS": lambda v: setattr(md, "AVERAGE_METRICS_HISTORY_DAYS", int(v)),
            "COMMUNITY_API_KEY": lambda v: setattr(md, "COMMUNITY_API_KEY", v),
            "PUBLIC_MODE": lambda v: setattr(md, "PUBLIC_MODE", bool(v)),
        }
        for k, fn in reload_map.items():
            if k in update_data:
                try:
                    fn(current_config[k])
                except Exception:
                    pass

        if "MAX_PACKETS_MEMORY" in update_data:
            from collections import deque
            new_max = int(current_config["MAX_PACKETS_MEMORY"])
            md.MAX_PACKETS_IN_MEMORY = new_max
            md.meshtastic_data.packets = deque(md.meshtastic_data.packets, maxlen=new_max)

        if "LOG_LEVEL" in update_data:
            new_level = current_config["LOG_LEVEL"].upper()
            numeric_level = getattr(logging, new_level, None)
            if numeric_level:
                logging.getLogger().setLevel(numeric_level)
                md.LOG_LEVEL_STR = new_level
                md.LOG_LEVEL = numeric_level

        md.loaded_config.update(current_config)
        if "COMMUNITY_API" in update_data:
            md.loaded_config["COMMUNITY_API"] = bool(update_data["COMMUNITY_API"])

        return JSONResponse({
            "status": "success",
            "message": "Configuration saved. Some changes require a server restart.",
            "updated_keys": list(update_data.keys()),
        })
    except PermissionError:
        raise HTTPException(status_code=500, detail="Permission denied - cannot write config file")
    except Exception as e:
        logging.getLogger("meshtastic_dashboard").error(f"Failed to update config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Configuration save failed: {str(e)}")


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

@router.post("/system/restart")
async def restart(user: User = Depends(get_current_active_user)):
    g = _get_globals()
    connection_manager = g["connection_manager"]

    try:
        from core.sse import broadcast_data
        if g.main_event_loop:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "system_message", "data": {"message": "🔄 System restarting..."}}),
                g.main_event_loop
            )
    except Exception:
        pass

    if connection_manager:
        try:
            import asyncio
            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            loop.run_in_executor(None, lambda: connection_manager.disconnect_for_restart(settle_seconds=3.0))
        except Exception:
            pass

    os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------------
# Version & Updates
# ---------------------------------------------------------------------------

def _parse_version_number(v_str) -> tuple:
    try:
        clean = re.sub(r"[^0-9.]", "", str(v_str))
        parts = [int(p) for p in clean.split(".") if p.isdigit()]
        return tuple(parts) if parts else (0,)
    except Exception:
        return (0,)


@router.get("/system/version-status")
async def get_version_status(notify: bool = False):
    g = _get_globals()
    meshtastic_data = g["meshtastic_data"]
    COMMUNITY_API_KEY = g["COMMUNITY_API_KEY"]

    app = g["app"]
    local_ver = getattr(app, 'version', '0.0.0')
    remote_ver = local_ver
    status = "current"
    _node_id = meshtastic_data.local_node_id

    if not _node_id:
        return {"local": local_ver, "remote": remote_ver, "status": "current"}

    target_url = (
        f"{_resolve_heartbeat()}?view=version&action=check"
        f"&node_id={_node_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(target_url, headers={"X-Api-Key": COMMUNITY_API_KEY, "X-Node-Id": _node_id, "User-Agent": "MeshDash-Backend"})
            if resp.status_code == 200:
                data = resp.json()
                remote_ver = data.get("version", local_ver)
    except Exception as e:
        logging.getLogger("meshtastic_dashboard").warning(f"Version check failed: {e}")
        if notify:
            try:
                import asyncio
                if g.get("main_event_loop"):
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "system_message", "data": {"message": "⚠️ Version check failed: Could not reach update server."}}),
                        g["main_event_loop"]
                    )
            except Exception:
                pass

    l_val = _parse_version_number(local_ver)
    r_val = _parse_version_number(remote_ver)

    if r_val > l_val:
        status = "update_needed"
        if notify:
            try:
                import asyncio
                if g.get("main_event_loop"):
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "system_message", "data": {"message": f"🚀 <b>Update Available!</b> Remote version is {remote_ver}. You are running {local_ver}."}}),
                        g["main_event_loop"]
                    )
            except Exception:
                pass
    elif l_val > r_val:
        status = "beta"
        if notify:
            try:
                import asyncio
                if g.get("main_event_loop"):
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "system_message", "data": {"message": f"🧪 <b>Beta Mode:</b> Running version {local_ver} (Remote: {remote_ver})"}}),
                        g["main_event_loop"]
                    )
            except Exception:
                pass
    else:
        status = "current"
        if notify:
            try:
                import asyncio
                if g.main_event_loop:
                    asyncio.run_coroutine_threadsafe(
                        broadcast_data({"event": "system_message", "data": {"message": f"✅ <b>System check:</b> Running latest version ({local_ver})"}}),
                        g.main_event_loop
                    )
            except Exception:
                pass

    return {"local": local_ver, "remote": remote_ver, "status": status}


@router.post("/system/check-update")
async def check_update(user: User = Depends(get_current_active_user)):
    # Just call version-status with notify=True
    return await get_version_status(notify=True)


@router.post("/system/start-update")
async def start_update_process(user: User = Depends(get_current_active_user)):
    import asyncio
    g = _get_globals()
    meshtastic_data = g["meshtastic_data"]
    COMMUNITY_API_KEY = g["COMMUNITY_API_KEY"]

    app = g["app"]
    local_ver = getattr(app, 'version', '0.0.0')

    target_url = f"{_resolve_heartbeat()}?view=version&action=check&node_id={meshtastic_data.local_node_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(target_url, headers={"X-Api-Key": COMMUNITY_API_KEY, "X-Node-Id": meshtastic_data.local_node_id or "", "User-Agent": "MeshDash-Backend"})
            if resp.status_code != 200:
                raise HTTPException(502, "Update server returned error")
            data = resp.json()
            remote_ver = data.get("version", local_ver)
            download_url = data.get("url")
    except Exception as e:
        logging.getLogger("meshtastic_dashboard").error(f"Update check failed: {e}")
        raise HTTPException(502, f"Update check failed: {e}")

    l_val = _parse_version_number(local_ver)
    r_val = _parse_version_number(remote_ver)
    if r_val <= l_val:
        return JSONResponse({"status": "current", "message": "Already on latest version."})

    if not download_url:
        raise HTTPException(502, "No download URL in server response")

    # R3.0+: Detect major-version bump (R2.x → R3.x)
    is_major = l_val[0] < r_val[0] if l_val and r_val else False
    update_type = "major" if is_major else "incremental"

    # Write to data/ so check_and_apply_update can find it on boot
    data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data")
    os.makedirs(data_dir, exist_ok=True)
    temp_path = os.path.join(data_dir, "update.zip")

    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
        def _write_zip():
            with open(temp_path, "wb") as f:
                f.write(resp.content)
        await asyncio.to_thread(_write_zip)
    except Exception as e:
        logging.getLogger("meshtastic_dashboard").error(f"Update download failed: {e}")
        raise HTTPException(502, f"Update download failed: {e}")

    # Write update trigger flags
    flag_path = os.path.join(data_dir, "update.flag")
    with open(flag_path, "w") as f:
        f.write(str(int(time.time())))

    if is_major:
        major_flag_path = os.path.join(data_dir, "update.major")
        with open(major_flag_path, "w") as f:
            f.write(f"{local_ver}→{remote_ver}")

    try:
        from core.sse import broadcast_data
        if is_major:
            msg = f"🚀 <b>Major Update Downloaded:</b> {local_ver} → {remote_ver}. A full backup will be created before applying. Save your work and restart to upgrade."
        else:
            msg = f"✅ <b>Update Downloaded:</b> {remote_ver}. Restart to apply."
        if g.main_event_loop:
            asyncio.run_coroutine_threadsafe(
                broadcast_data({"event": "system_message", "data": {"message": msg}}),
                g.main_event_loop
            )
    except Exception:
        pass

    return JSONResponse({
        "status": "downloaded",
        "message": f"{update_type.capitalize()} update ready. Restart to apply.",
        "version": remote_ver,
        "update_type": update_type,
        "from_version": local_ver,
    })


# ---------------------------------------------------------------------------
# Config router and models (from system.py)
# ---------------------------------------------------------------------------

# NOTE: Config router models (ConfigUpdateRequest, AdminUserSetup, ConfigValuesSetup,
# RawSelectionsSetup, InitialSetupPayload) were removed along with config_router.
# Config validation for /system/config/update uses _load_config_models() locally.

# NOTE: config_router (GET/PUT config, initial-setup, request-restart) removed.
# These routes are now served by admin_routes.py and the main system_routes router.


