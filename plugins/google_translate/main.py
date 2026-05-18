"""
Google Translate Plugin — Backend v1.0
Minimal backend — just stores config (enabled, position, language).
All UI work is done in bridge.html.
"""
import logging
import os
import time
import threading
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("plugin.google_translate")
plugin_router = APIRouter()

import asyncio
_lock   = threading.Lock()
_config = {
    "enabled":           True,
    "position":          "bottom-right",
    "default_language":  "",
    "show_original":     True,
    "compact":           False,
}

_DB_PATH = os.path.join(os.path.dirname(__file__), "google_translate_config.db")

def _db_load():
    try:
        import sqlite3, json
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        row = conn.execute("SELECT value FROM config WHERE key='settings'").fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return {}

def _db_save(cfg):
    try:
        import sqlite3, json
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES ('settings', ?)",
            (json.dumps(cfg),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("Google Translate: failed to persist config: %s", e)


async def _watchdog_heartbeat(context):
    wd  = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid is not None:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            logger.info("Google Translate watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("Google Translate watchdog error: %s", e)


def init_plugin(context: dict) -> None:
    global _config
    saved = _db_load()
    if saved:
        with _lock:
            _config.update({k: v for k, v in saved.items() if k in _config})
        logger.info(
            "Google Translate: loaded persisted config — enabled=%s position=%s",
            _config["enabled"], _config["position"],
        )
    logger.info("Google Translate plugin v1.0 initialised.")

    loop = context.get("event_loop")
    if loop is None:
        logger.warning("Google Translate: event_loop not in context — watchdog will not start.")
        return
    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("Google Translate watchdog heartbeat started.")
    except Exception as e:
        logger.error("Google Translate: could not start watchdog heartbeat: %s", e)


class ConfigModel(BaseModel):
    enabled:          bool = True
    position:         str  = Field("bottom-right", pattern="^(bottom-right|bottom-left|top-right|top-left)$")
    default_language: str  = ""
    show_original:    bool = True
    compact:          bool = False


@plugin_router.get("/config")
async def get_config():
    with _lock:
        return dict(_config)


@plugin_router.post("/config")
async def set_config(body: ConfigModel):
    with _lock:
        _config.update(body.dict())
    return {"status": "ok", **_config}