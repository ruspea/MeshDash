# core.plugin_manager
# Extracted from meshtastic_dashboard.py — plugin loading, lifecycle, watchdog, recovery

import asyncio
import importlib.util
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, Set

# Deferred / lazy imports to avoid circular dependency at module load time
# These are set by the main dashboard file after it creates these objects
db_manager = None
meshtastic_data = None
connection_manager = None
node_registry = None
app = None  # FastAPI app instance — set by meshtastic_dashboard after app creation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WATCHDOG_AUTO_RECOVER = True   # Auto-recover hung plugins (attempt 3x, 5s apart)
_PLUGIN_HANG_TIMEOUT = 120  # seconds before a hung plugin is flagged & its routes blocked

# Plugin directory is always relative to the project root (parent of core/)
try:
    _THIS_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _THIS_DIR = os.getcwd()
PROJECT_ROOT = os.path.dirname(_THIS_DIR)  # core/ -> project root
PLUGIN_DIR = os.path.join(PROJECT_ROOT, "plugins")
if not os.path.exists(PLUGIN_DIR):
    os.makedirs(PLUGIN_DIR)

# ---------------------------------------------------------------------------
# Registry & Watchdog State
# ---------------------------------------------------------------------------
PLUGIN_REGISTRY: Dict[str, Dict] = {}

# Per-plugin watchdog: tracks last heartbeat timestamp from plugin tasks
_plugin_watchdog: Dict[str, float] = {}

# ---------------------------------------------------------------------------
# Per-plugin in-memory log capture
# ---------------------------------------------------------------------------
_PLUGIN_LOG_MAX_LINES = 250

class MemoryLogHandler(logging.Handler):
    """Thread-safe circular log buffer attached to a plugin logger (plugin.<pid>)."""

    def __init__(self, maxlen: int = _PLUGIN_LOG_MAX_LINES):
        super().__init__()
        self._buf: list = []
        self._maxlen = maxlen
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%H:%M:%S"
        ))

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
            with self._lock:
                self._buf.append({"t": record.created, "lvl": record.levelname, "msg": line})
                if len(self._buf) > self._maxlen:
                    self._buf = self._buf[-self._maxlen:]
        except Exception:
            pass

    def get_lines(self) -> list:
        with self._lock:
            return list(self._buf)

    def clear(self):
        with self._lock:
            self._buf.clear()


# pid -> MemoryLogHandler
_plugin_log_handlers: Dict[str, "MemoryLogHandler"] = {}


def _attach_plugin_log_handler(pid: str) -> "MemoryLogHandler":
    """Create and attach a MemoryLogHandler to plugin.<pid> logger. Idempotent."""
    if pid not in _plugin_log_handlers:
        handler = MemoryLogHandler()
        _plugin_log_handlers[pid] = handler
        pl = logging.getLogger(f"plugin.{pid}")
        pl.addHandler(handler)
        pl.setLevel(logging.DEBUG)
    return _plugin_log_handlers[pid]


# ---------------------------------------------------------------------------
# PluginManager
# ---------------------------------------------------------------------------

class PluginManager:
    contexts: Dict[str, Dict[str, Any]] = {}  # Stores plugin-provided context data

    @staticmethod
    def load_all(app_ref: "FastAPI"):
        logger = logging.getLogger("plugin_manager")
        logger.info("🧩 Mounting Plugin Routes & Static Files...")
        try:
            items = sorted(os.listdir(PLUGIN_DIR))
        except Exception as e:
            logger.error(f"Cannot list plugin directory: {e}")
            return
        for item in items:
            plugin_path = os.path.join(PLUGIN_DIR, item)
            manifest_path = os.path.join(plugin_path, "manifest.json")
            if os.path.isdir(plugin_path) and os.path.exists(manifest_path):
                PluginManager.load_plugin(app_ref, plugin_path, manifest_path, logger)

    @staticmethod
    def load_plugin(app_ref: "FastAPI", plugin_path: str, manifest_path: str, logger):
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

            # REQUIRED FIELD: "watchdog" must be explicitly declared in manifest.
            if "watchdog" not in manifest:
                PLUGIN_REGISTRY[pid] = {
                    "manifest": manifest,
                    "status": "invalid_manifest",
                    "error": 'Missing required field "watchdog" in manifest.json. '
                             'Set to true (monitored) or false (unmonitored).',
                    "path": plugin_path,
                    "module": None,
                    "loaded_at": time.time(),
                }
                logger.error(
                    f"🚫 Plugin '{pid}' rejected: manifest.json is missing required "
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
                logger.info(f"⏸️  Plugin {pid} is stopped (disabled marker found).")
                return

            # Mount Static Files
            static_dir = os.path.join(plugin_path, "static")
            static_prefix = manifest.get("static_prefix", f"/static/plugins/{pid}")
            if os.path.exists(static_dir):
                app_ref.mount(static_prefix, StaticFiles(directory=static_dir), name=f"plugin_static_{pid}")

            # Dynamic Python Import — sandboxed in a thread with timeout
            entry_file = os.path.join(plugin_path, manifest.get("entry_point", "main.py"))
            if os.path.exists(entry_file):
                plugin_module = PluginManager._import_with_timeout(pid, entry_file, timeout=10)
                if plugin_module is None:
                    raise RuntimeError(f"Plugin '{pid}' module import timed out (>10s)")

                PLUGIN_REGISTRY[pid]["module"] = plugin_module

                if hasattr(plugin_module, "plugin_router"):
                    # Capture pid in closure correctly
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
                            # Hang check: only applies to plugins that opted in via manifest watchdog:true
                            last_hb = _plugin_watchdog.get(plugin_id)
                            if last_hb is not None and (time.time() - last_hb) > _PLUGIN_HANG_TIMEOUT:
                                PLUGIN_REGISTRY[plugin_id]["status"] = "hung"
                                PLUGIN_REGISTRY[plugin_id]["error"] = "Plugin task hang detected — stopped"
                                raise HTTPException(
                                    503,
                                    detail={
                                        "detail": f"Plugin '{plugin_id}' is hung and has been stopped.",
                                        "status": "hung",
                                        "plugin_id": plugin_id,
                                    }
                                )
                        return plugin_state_check

                    app_ref.include_router(
                        plugin_module.plugin_router,
                        prefix=manifest.get("router_prefix", f"/api/plugins/{pid}"),
                        dependencies=[Depends(_make_state_check(pid))],
                    )

            PLUGIN_REGISTRY[pid]["status"] = "running"
            # Attach in-memory log capture for this plugin
            _attach_plugin_log_handler(pid)
            # Only register watchdog for plugins that explicitly opt-in via manifest.
            if manifest.get("watchdog", False):
                _plugin_watchdog[pid] = time.time()
                logger.info(f"🐕 Watchdog enabled for plugin: {pid}")
            logger.info(f"✅ Plugin mounted: {manifest.get('name', pid)}")

        except Exception as e:
            logger.error(f"❌ Plugin Mount Crash ({plugin_path}): {e}", exc_info=True)
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
                f"❌ Plugin '{pid}' import thread timed out after {timeout}s"
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
                        # Wrap init_plugin with a thread timeout
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
                            logger.error(f"❌ Plugin '{pid}' init_plugin timed out. Marking crashed.")
                            data["status"] = "crashed"
                            data["error"] = "init_plugin timed out"
                        elif not result["ok"]:
                            logger.error(f"❌ Plugin '{pid}' init crashed: {result['error']}")
                            data["status"] = "crashed"
                            data["error"] = result["error"]
                        else:
                            logger.info(f"✅ Plugin {pid} context injected.")
                            # Store any plugin-provided context data (keys ending with _plugin)
                            for key, value in ctx.items():
                                if key.endswith("_plugin"):
                                    PluginManager.contexts[key] = value
                                    logger.info(f"   ↳ Stored context key: {key}")
                            # Only refresh watchdog timestamp for opted-in plugins
                            if data.get("manifest", {}).get("watchdog", False):
                                _plugin_watchdog[pid] = time.time()
                    except Exception as e:
                        logger.error(f"❌ Plugin '{pid}' init_contexts outer error: {e}")
                        data["status"] = "crashed"
                        data["error"] = str(e)


# ---------------------------------------------------------------------------
# State-check helper (used by plugin routes)
# ---------------------------------------------------------------------------

def _make_state_check(plugin_id: str):
    """Build a FastAPI Depends() callable that checks plugin status before serving a route."""
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
            PLUGIN_REGISTRY[plugin_id]["error"] = "Plugin task hang detected — stopped"
            raise HTTPException(
                503,
                detail={
                    "detail": f"Plugin '{plugin_id}' is hung and has been stopped.",
                    "status": "hung",
                    "plugin_id": plugin_id,
                }
            )
    return plugin_state_check


# ---------------------------------------------------------------------------
# Plugin Watchdog Worker
# ---------------------------------------------------------------------------

async def plugin_watchdog_worker():
    """
    Background task that monitors plugins for hangs.
    ONLY plugins that set "watchdog": true in their manifest.json are monitored.
    Passive plugins that do not opt in are never flagged as hung, regardless of
    how long they have been idle. Opted-in plugins must periodically call:
        context['plugin_watchdog'][pid] = time.time()
    to reset their timer. If they fail to do so within _PLUGIN_HANG_TIMEOUT seconds,
    they are flagged as 'hung'. If WATCHDOG_AUTO_RECOVER is True (default), the
    watchdog will attempt to re-import and re-initialise the plugin automatically
    before giving up and leaving it permanently hung.
    """
    logger = logging.getLogger("plugin_watchdog")
    logger.info("🐕 Plugin Watchdog Worker Started (auto_recover=%s)", WATCHDOG_AUTO_RECOVER)
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for pid, last_hb in list(_plugin_watchdog.items()):
            data = PLUGIN_REGISTRY.get(pid)
            if not data or data.get("status") != "running":
                continue
            elapsed = now - last_hb
            if elapsed > _PLUGIN_HANG_TIMEOUT:
                logger.warning(
                    f"⚠️  Plugin '{pid}' has not heartbeated in {elapsed:.0f}s — marking as hung."
                )
                data["status"] = "hung"
                data["error"] = f"No activity for {elapsed:.0f}s — auto-stopped by watchdog"

                # Auto-recovery attempt
                if WATCHDOG_AUTO_RECOVER:
                    asyncio.create_task(_attempt_plugin_recovery(pid))


async def _attempt_plugin_recovery(pid: str, max_attempts: int = 3):
    """
    Attempt to recover a hung plugin by re-importing and re-mounting it.
    Up to max_attempts, waiting 5 seconds between each. If all attempts fail,
    the plugin is left in 'hung' state and a 503 with meaningful JSON is
    returned on its routes.
    """
    logger = logging.getLogger("plugin_watchdog")
    data = PLUGIN_REGISTRY.get(pid)
    if not data:
        logger.warning(f"  Recovery for '{pid}': plugin not in registry — skipping.")
        return

    manifest = data.get("manifest", {})
    plugin_path = data.get("path")
    entry_point = manifest.get("entry_point", "main.py")
    entry_file = os.path.join(plugin_path, entry_point) if plugin_path else None

    if not entry_file or not os.path.exists(entry_file):
        logger.warning(f"  Recovery for '{pid}': entry file not found — giving up.")
        data["status"] = "hung"
        data["error"] = "Auto-recovery failed: entry file not found. Restart the server."
        return

    for attempt in range(1, max_attempts + 1):
        logger.info(f"  🔄 Recovery attempt {attempt}/{max_attempts} for '{pid}'…")
        data["status"] = "recovering"
        data["error"] = f"Auto-recovery attempt {attempt}/{max_attempts} in progress."

        # Remove old routes by replacing the router
        old_module = data.get("module")
        if old_module and hasattr(old_module, "plugin_router"):
            try:
                # Find and remove the mounted router from app
                for route in list(app.routes):
                    if hasattr(route, "path") and route.path.startswith(
                        manifest.get("router_prefix", f"/api/plugins/{pid}")
                    ):
                        app.routes.remove(route)
                        logger.info(f"    Unmounted routes for '{pid}'.")
            except Exception as e:
                logger.warning(f"    Could not unmount routes: {e}")

        # Wait before re-importing
        await asyncio.sleep(5)

        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{pid}_recover", entry_file)
            new_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(new_module)
            data["module"] = new_module
            logger.info(f"    ✅ Re-imported '{pid}'.")
        except Exception as e:
            logger.error(f"    ❌ Re-import failed for '{pid}': {e}")
            data["status"] = "hung"
            data["error"] = f"Auto-recovery failed on attempt {attempt}: {e}. Restart the server."
            continue

        # Re-register watchdog heartbeat and remount routes
        if hasattr(new_module, "plugin_router"):
            def _make_state_check_recov(plugin_id: str):
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
                        PLUGIN_REGISTRY[plugin_id]["error"] = "Plugin task hang detected — stopped"
                        raise HTTPException(
                            503,
                            detail={
                                "detail": f"Plugin '{plugin_id}' is hung and has been stopped.",
                                "status": "hung",
                                "plugin_id": plugin_id,
                            }
                        )
                return plugin_state_check

            try:
                app.include_router(
                    new_module.plugin_router,
                    prefix=manifest.get("router_prefix", f"/api/plugins/{pid}"),
                    dependencies=[Depends(_make_state_check_recov(pid))],
                )
                logger.info(f"    ✅ Remounted routes for '{pid}'.")
            except Exception as e:
                logger.error(f"    ❌ Route remount failed for '{pid}': {e}")
                data["status"] = "hung"
                data["error"] = f"Route remount failed: {e}. Restart the server."
                continue

        # Re-inject context / call init_plugin
        try:
            ctx = {
                "db_manager":         getattr(PluginManager, "db_manager", None),
                "meshtastic_data":    getattr(PluginManager, "meshtastic_data", {}),
                "connection_manager": getattr(PluginManager, "connection_manager", {}),
                "node_registry":      getattr(PluginManager, "node_registry", {}),
                "event_loop":         g.main_event_loop,
                "logger":             logging.getLogger(f"plugin.{pid}"),
                "plugin_watchdog":    _plugin_watchdog,
                "plugin_id":          pid,
            }
            if hasattr(new_module, "init_plugin"):
                await asyncio.wait_for(
                    asyncio.to_thread(new_module.init_plugin, ctx),
                    timeout=15.0,
                )
                logger.info(f"    ✅ Re-initialised '{pid}'.")
            _plugin_watchdog[pid] = time.time()
            data["status"] = "running"
            data["error"] = None
            logger.info(f"  ✅ Plugin '{pid}' successfully recovered on attempt {attempt}.")

            # Broadcast recovery to SSE clients
            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "plugin_update", "data": {"id": pid, "status": "running"}}),
                    g.main_event_loop,
                )
            return  # Success!

        except Exception as e:
            logger.error(f"    ❌ Re-init failed for '{pid}': {e}")
            data["status"] = "hung"
            data["error"] = f"Auto-recovery failed on attempt {attempt}: {e}. Restart the server."

    logger.warning(f"  ⚠️  All {max_attempts} recovery attempts exhausted for '{pid}' — plugin is hung. Restart the server to recover.")
    data["status"] = "hung"
    data["error"] = f"Auto-recovery exhausted {max_attempts} attempts. Restart the server to recover."
    if g.main_event_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast_data({"event": "plugin_update", "data": {"id": pid, "status": "hung"}}),
            g.main_event_loop,
        )


# ---------------------------------------------------------------------------
# Imports needed by this module (FastAPI dependencies)
# ---------------------------------------------------------------------------
from starlette.staticfiles import StaticFiles
from fastapi import Depends, HTTPException, FastAPI, status

# broadcast_data is set by meshtastic_dashboard after it creates the SSE manager
broadcast_data = lambda **kwargs: None
