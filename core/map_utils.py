import core.globals as g
# Auto-extracted from meshtastic_dashboard.py

import sqlite3
import sqlite3
import sqlite3
import os
import json
import sqlite3
import threading
from typing import Optional, Dict

MAPS_CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "maps_config.json")
_mbtiles_cache: Dict[str, sqlite3.Connection] = {}
_mbtiles_lock = threading.Lock()

def _load_maps_config() -> dict:
    try:
        with open(MAPS_CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"active_file": None}


def _save_maps_config(cfg: dict):
    with open(MAPS_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _get_mbtiles_conn(filepath: str) -> Optional[sqlite3.Connection]:
    with _mbtiles_lock:
        if filepath in _mbtiles_cache:
            try:
                _mbtiles_cache[filepath].execute("SELECT 1")
                return _mbtiles_cache[filepath]
            except Exception:
                try:
                    _mbtiles_cache[filepath].close()
                except Exception:
                    pass
                del _mbtiles_cache[filepath]
        if not os.path.isfile(filepath):
            return None
        try:
            conn = sqlite3.connect(filepath, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA query_only=ON")
            _mbtiles_cache[filepath] = conn
            return conn
        except Exception:
            return None


def _close_mbtiles_conn(filepath: str):
    with _mbtiles_lock:
        conn = _mbtiles_cache.pop(filepath, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
