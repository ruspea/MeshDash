"""
Theme Editor Plugin — Backend v1.0
Stores the active theme (a dict of CSS variable overrides) in SQLite.
The bridge.html reads it on load and injects a <style> tag into the parent
document to apply the theme without any page reload.
"""
import os
import json
import sqlite3
import asyncio
import threading
import logging
import time
from typing import Any, Dict, Optional
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("plugin.theme_editor")
plugin_router = APIRouter()

_DB_PATH   = os.path.join(os.path.dirname(__file__), "theme.db")
_DB_LOCK   = threading.Lock()
_cfg_lock  = threading.Lock()
_watchdog_task: Optional[asyncio.Task] = None

# These are the values users can override. Stored as a flat dict of
# CSS variable name → hex/value. Only the keys the user has changed
# are stored; the rest fall through to the stylesheet defaults.
_DEFAULT: Dict[str, str] = {
    "--acc":   "#00c8f5",
    "--ok":    "#00e87a",
    "--warn":  "#ffa826",
    "--err":   "#ff3050",
    "--pur":   "#b060ff",
    "--bg":    "#0a1018",
    "--bg2":   "#111a28",
    "--bg3":   "#162030",
    "--txt":   "#9bb5cf",
    "--txt2":  "#b8c8d8",
    "--txt3":  "#88a0b8",
    "--bd":    "#1e3048",
    "--bd2":   "#284058",
}

# Active overrides (empty = using stylesheet defaults)
_overrides: Dict[str, str] = {}


def _db_load() -> Dict[str, str]:
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS theme (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            row = conn.execute("SELECT value FROM theme WHERE key='overrides'").fetchone()
            conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.warning("Theme Editor: load error: %s", e)
    return {}


def _db_save(data: Dict[str, str]) -> None:
    try:
        with _DB_LOCK:
            conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            conn.execute("CREATE TABLE IF NOT EXISTS theme (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO theme (key, value) VALUES ('overrides', ?)",
                (json.dumps(data),),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Theme Editor: save error: %s", e)


async def _watchdog_heartbeat(context: dict) -> None:
    """
    Pings the MeshDash core watchdog every 30 s.
    Required because manifest.json has "watchdog": true.
    Without this the core marks the plugin as 'hung' after 120 s of silence.
    """
    wd  = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("Theme Editor watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("Theme Editor watchdog error: %s", e)


def init_plugin(context: dict) -> None:
    global _overrides
    saved = _db_load()
    with _cfg_lock:
        _overrides = saved
    logger.info(
        "Theme Editor v1.0 ready — %d active override(s).",
        len(saved),
    )

    # Launch the watchdog heartbeat on the main event loop.
    # init_plugin is called from a threading.Thread by the MeshDash core,
    # so we must use run_coroutine_threadsafe rather than create_task directly.
    loop = context.get("event_loop")
    if loop is None:
        logger.warning("Theme Editor: event_loop not in context — watchdog will not start.")
        return
    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("Theme Editor watchdog heartbeat started.")
    except Exception as e:
        logger.error("Theme Editor: could not start watchdog heartbeat: %s", e)


class ThemeModel(BaseModel):
    overrides: Dict[str, str] = {}


@plugin_router.get("/theme")
async def get_theme():
    """Returns defaults + current overrides so the bridge can build the full CSS."""
    with _cfg_lock:
        ovr = dict(_overrides)
    return {
        "defaults":  _DEFAULT,
        "overrides": ovr,
    }


@plugin_router.post("/theme")
async def set_theme(body: ThemeModel):
    """Persist a new set of overrides. Only recognised CSS variable keys are stored."""
    # Validate: only allow known CSS variable keys (--xxx pattern, no injection)
    import re
    clean = {
        k: v for k, v in body.overrides.items()
        if re.match(r'^--[a-zA-Z0-9_-]+$', k) and len(v) < 64
    }
    with _cfg_lock:
        global _overrides
        _overrides = clean
    await asyncio.to_thread(_db_save, clean)
    logger.info("Theme updated — %d override(s)", len(clean))
    return {"status": "ok", "overrides": clean}


@plugin_router.delete("/theme")
async def reset_theme():
    """Reset to defaults — clears all overrides."""
    with _cfg_lock:
        global _overrides
        _overrides = {}
    await asyncio.to_thread(_db_save, {})
    logger.info("Theme reset to defaults.")
    return {"status": "ok", "overrides": {}}