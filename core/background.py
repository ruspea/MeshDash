import core.globals as g
import asyncio
import time
import logging
import importlib
import os
from fastapi import Depends, HTTPException
from core.broadcast import broadcast_data, broadcast_stats_for_slot
# Auto-extracted from meshtastic_dashboard.py

_plugin_watchdog: dict = {}
_PLUGIN_HANG_TIMEOUT = 120
WATCHDOG_AUTO_RECOVER = True
app = None
verify_csrf = None
PluginManager = None

logger = logging.getLogger(__name__)

async def plugin_watchdog_worker():
    """
    R2.X: Background task that monitors plugins for hangs.
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
    logger.info("? Plugin Watchdog Worker Started (auto_recover=%s)", WATCHDOG_AUTO_RECOVER)
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for pid, last_hb in list(_plugin_watchdog.items()):
            data = g.PLUGIN_REGISTRY.get(pid)
            if not data or data.get("status") != "running":
                continue
            elapsed = now - last_hb
            if elapsed > _PLUGIN_HANG_TIMEOUT:
                logger.warning(
                    f"?  Plugin '{pid}' has not heartbeated in {elapsed:.0f}s  marking as hung."
                )
                data["status"] = "hung"
                data["error"] = f"No activity for {elapsed:.0f}s  auto-stopped by watchdog"

                #  Auto-recovery attempt 
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
    data = g.PLUGIN_REGISTRY.get(pid)
    if not data:
        logger.warning(f"  Recovery for '{pid}': plugin not in registry  skipping.")
        return

    manifest = data.get("manifest", {})
    plugin_path = data.get("path")
    entry_point = manifest.get("entry_point", "main.py")
    entry_file = os.path.join(plugin_path, entry_point) if plugin_path else None

    if not entry_file or not os.path.exists(entry_file):
        logger.warning(f"  Recovery for '{pid}': entry file not found  giving up.")
        data["status"] = "hung"
        data["error"] = "Auto-recovery failed: entry file not found. Restart the server."
        return

    for attempt in range(1, max_attempts + 1):
        logger.info(f"  ? Recovery attempt {attempt}/{max_attempts} for '{pid}'")
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

        # Re-import the plugin module
        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{pid}_recover", entry_file)
            new_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(new_module)
            data["module"] = new_module
            logger.info(f"     Re-imported '{pid}'.")
        except Exception as e:
            logger.error(f"     Re-import failed for '{pid}': {e}")
            data["status"] = "hung"
            data["error"] = f"Auto-recovery failed on attempt {attempt}: {e}. Restart the server."
            continue

        # Re-register watchdog heartbeat and remount routes
        if hasattr(new_module, "plugin_router"):
            def _make_state_check(plugin_id: str):
                async def plugin_state_check():
                    entry = g.PLUGIN_REGISTRY.get(plugin_id, {})
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
                        g.PLUGIN_REGISTRY[plugin_id]["status"] = "hung"
                        g.PLUGIN_REGISTRY[plugin_id]["error"] = "Plugin task hang detected  stopped"
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
                    dependencies=[Depends(_make_state_check(pid)), Depends(verify_csrf)],
                )
                logger.info(f"     Remounted routes for '{pid}'.")
            except Exception as e:
                logger.error(f"     Route remount failed for '{pid}': {e}")
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
                logger.info(f"     Re-initialised '{pid}'.")
            _plugin_watchdog[pid] = time.time()
            data["status"] = "running"
            data["error"] = None
            logger.info(f"   Plugin '{pid}' successfully recovered on attempt {attempt}.")

            # Broadcast recovery to SSE clients
            if g.main_event_loop:
                asyncio.run_coroutine_threadsafe(
                    broadcast_data({"event": "plugin_update", "data": {"id": pid, "status": "running"}}),
                    g.main_event_loop,
                )
            return  # Success!

        except Exception as e:
            logger.error(f"     Re-init failed for '{pid}': {e}")
            data["status"] = "hung"
            data["error"] = f"Auto-recovery failed on attempt {attempt}: {e}. Restart the server."

    logger.warning(f"  ?  All {max_attempts} recovery attempts exhausted for '{pid}'  plugin is hung. Restart the server to recover.")
    data["status"] = "hung"
    data["error"] = f"Auto-recovery exhausted {max_attempts} attempts. Restart the server to recover."
    if g.main_event_loop:
        asyncio.run_coroutine_threadsafe(
            broadcast_data({"event": "plugin_update", "data": {"id": pid, "status": "hung"}}),
            g.main_event_loop,
        )


async def update_stats_periodically():
    while True:
        await asyncio.sleep(10)
        for slot in list(g.NODE_REGISTRY.values()):
            try:
                await broadcast_stats_for_slot(slot)
            except Exception:
                pass


async def connection_heartbeat_worker():
    """Logs the current connection status every 60 seconds for the graph  all slots."""
    logger.info("? Connection Heartbeat Worker Started")
    while True:
        await asyncio.sleep(60)
        try:
            for slot in list(g.NODE_REGISTRY.values()):
                try:
                    current_status = slot.g.meshtastic_data.connection_status
                    await asyncio.to_thread(slot.g.db_manager.log_connection_status, current_status)
                    logger.debug("? Heartbeat [%s]: Logged status '%s'", slot.slot_id, current_status)
                except Exception as slot_e:
                    logger.error(" Heartbeat Worker Error [%s]: %s", slot.slot_id, slot_e)
        except Exception as e:
            logger.error(" Heartbeat Worker Error: %s", e)


async def save_metrics_periodically():
    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(g.db_manager.calculate_and_save_average_metrics)
        except Exception as e:
            logger.error(f"Error saving periodic metrics: {e}")


async def prune_history_periodically():
    """Flush buffered node writes every 10 s, prune old data hourly,
    run PRAGMA optimize daily (file DB only).

    The 10-second flush keeps WAL from growing large on busy meshes while
    avoiding a commit on every individual heartbeat packet.
    """
    # How often to flush buffered node writes (seconds).
    NODE_WRITE_FLUSH_INTERVAL = 10
    flush_tick = 0
    prune_cycle = 0

    while True:
        await asyncio.sleep(NODE_WRITE_FLUSH_INTERVAL)
        flush_tick += 1

        #  Hourly prune (every 360  10 s = 3 600 s) 
        if flush_tick % 360 == 0:
            prune_cycle += 1
            await asyncio.to_thread(g.db_manager.prune_old_data, g.DATA_RETENTION_DAYS)

            #  Daily PRAGMA optimize (every 24 cycles of hourly prune) 
            if prune_cycle % 24 == 0 and not g.db_manager.ephemeral:
                try:
                    def _optimize():
                        conn = g.db_manager._get_connection()
                        conn.execute("PRAGMA optimize;")
                    await asyncio.to_thread(_optimize)
                    logger.info(" Database PRAGMA optimize completed.")
                except Exception as e:
                    logger.error(f"PRAGMA optimize failed: {e}")


