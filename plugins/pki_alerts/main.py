"""
PKI Alerts Plugin - Backend API v1.4
Implements Trust On First Use (TOFU) with RF Anomaly Profiling.
Fixed: SQLite Row dictionary coercion to prevent 500 errors.
"""

import os
import time
import json
import logging
import asyncio
import sqlite3
import threading
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("plugin.pki_alerts")
plugin_router = APIRouter()

DB_PATH = os.path.join(os.path.dirname(__file__), "pki_audit.db")
_db_lock = threading.Lock()

_config = {
    "enabled": True,
    "db_version": 0,
}

# Duplicate key scan results cache
_dup_scan_result: Dict[str, Any] = {
    "last_scan": None,
    "duplicates": [],
    "total_groups": 0,
    "total_affected_nodes": 0,
}
_dup_scan_lock = threading.Lock()

def _get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

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
            logger.info("PKI Alerts watchdog heartbeat stopped.")
            return
        except Exception as e:
            logger.warning("PKI Alerts watchdog error: %s", e)


def _run_duplicate_scan() -> Dict[str, Any]:
    """
    Synchronous duplicate key scan. Finds all public keys shared by more than
    one node_id. Returns structured results including per-group RF forensics.
    """
    with _db_lock:
        conn = _get_db()
        # Find all public keys that appear for more than one distinct node_id
        rows = conn.execute("""
            SELECT public_key, GROUP_CONCAT(node_id, '|') as node_ids,
                   COUNT(DISTINCT node_id) as node_count
            FROM trusted_keys
            WHERE public_key IS NOT NULL AND public_key != '' AND public_key != 'Unknown'
            GROUP BY public_key
            HAVING node_count > 1
            ORDER BY node_count DESC
        """).fetchall()

        duplicates = []
        total_affected = 0

        for row in rows:
            pub_key = row["public_key"]
            node_ids = [n for n in row["node_ids"].split("|") if n]
            node_count = row["node_count"]

            # Fetch full node details for each node in this group
            node_details = []
            for nid in node_ids:
                nd = conn.execute(
                    "SELECT * FROM trusted_keys WHERE node_id=?", (nid,)
                ).fetchone()
                if nd:
                    nd_dict = dict(nd)
                    # Get last seen from audit log
                    last_event = conn.execute(
                        "SELECT timestamp, event_type FROM audit_log WHERE node_id=? ORDER BY timestamp DESC LIMIT 1",
                        (nid,)
                    ).fetchone()
                    nd_dict["last_event_ts"] = last_event["timestamp"] if last_event else nd_dict.get("first_seen")
                    nd_dict["last_event_type"] = last_event["event_type"] if last_event else "NEW_NODE"
                    node_details.append(nd_dict)

            # Determine risk level for this group
            # HIGH if hardware models differ across nodes sharing a key
            hw_models = set(n.get("hardware_model", "Unknown") for n in node_details if n.get("hardware_model") not in (None, "", "Unknown"))
            mac_addrs = set(n.get("macaddr", "Unknown") for n in node_details if n.get("macaddr") not in (None, "", "Unknown"))
            risk = "CRITICAL" if (len(hw_models) > 1 or len(mac_addrs) > 1) else "HIGH"

            # Audit log this duplicate group (rate-limited to once per 6h per key)
            last_dup_log = conn.execute(
                "SELECT timestamp FROM audit_log WHERE event_type='DUPLICATE_KEY' AND details LIKE ? ORDER BY timestamp DESC LIMIT 1",
                (f"%{pub_key[:16]}%",)
            ).fetchone()
            if not last_dup_log or (time.time() - last_dup_log["timestamp"]) > 21600:
                conn.execute(
                    "INSERT INTO audit_log (timestamp, node_id, event_type, details, context) VALUES (?,?,?,?,?)",
                    (
                        time.time(),
                        node_ids[0],
                        "DUPLICATE_KEY",
                        f"Public key shared by {node_count} nodes: {', '.join(node_ids)}",
                        json.dumps({
                            "public_key_prefix": pub_key[:16] + "...",
                            "nodes": node_ids,
                            "risk": risk,
                            "hw_models": list(hw_models),
                        })
                    )
                )
                conn.commit()

            total_affected += node_count
            duplicates.append({
                "public_key": pub_key,
                "public_key_prefix": pub_key[:16] + "..." if len(pub_key) > 16 else pub_key,
                "node_count": node_count,
                "node_ids": node_ids,
                "nodes": node_details,
                "risk": risk,
                "hw_models": list(hw_models),
                "mac_addrs": list(mac_addrs),
                "hw_conflict": len(hw_models) > 1,
                "mac_conflict": len(mac_addrs) > 1,
            })

        conn.close()

    result = {
        "last_scan": time.time(),
        "duplicates": duplicates,
        "total_groups": len(duplicates),
        "total_affected_nodes": total_affected,
    }
    with _dup_scan_lock:
        _dup_scan_result.update(result)

    if duplicates:
        logger.warning("🔑 Duplicate Key Scan: %d group(s), %d affected nodes", len(duplicates), total_affected)
    else:
        logger.info("🔑 Duplicate Key Scan: No duplicates found.")

    return result


async def _duplicate_scan_worker() -> None:
    """Background worker — runs scan immediately on start, then every 24h."""
    await asyncio.sleep(10)  # brief boot delay
    while True:
        try:
            await asyncio.to_thread(_run_duplicate_scan)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error("Duplicate scan worker error: %s", e)
        try:
            await asyncio.sleep(86400)  # 24 hours
        except asyncio.CancelledError:
            return


def init_plugin(context: dict) -> None:
    with _db_lock:
        conn = _get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trusted_keys (
                node_id TEXT PRIMARY KEY, public_key TEXT, short_name TEXT, first_seen REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, node_id TEXT, event_type TEXT, details TEXT
            )
        """)

        for col in ["long_name TEXT", "hardware_model TEXT", "macaddr TEXT",
                    "avg_snr REAL DEFAULT 0.0", "avg_rssi REAL DEFAULT 0.0", "ping_count INTEGER DEFAULT 0",
                    "trust_score INTEGER DEFAULT 100"]:
            try: conn.execute(f"ALTER TABLE trusted_keys ADD COLUMN {col}")
            except sqlite3.OperationalError: pass

        try: conn.execute("ALTER TABLE audit_log ADD COLUMN context TEXT")
        except sqlite3.OperationalError: pass

        conn.commit()
        conn.close()
    logger.info("PKI Alerts plugin initialised.")

    loop = context.get("event_loop")
    if loop is None:
        logger.warning("PKI Alerts: event_loop not in context — watchdog will not start.")
        return
    try:
        asyncio.run_coroutine_threadsafe(_watchdog_heartbeat(context), loop)
        logger.info("PKI Alerts watchdog heartbeat started.")
        asyncio.run_coroutine_threadsafe(_duplicate_scan_worker(), loop)
        logger.info("PKI Alerts duplicate key scanner started (24h interval).")
    except Exception as e:
        logger.error("PKI Alerts: could not start background tasks: %s", e)

class ConfigModel(BaseModel):
    enabled: bool

class KeyCheckRequest(BaseModel):
    node_id: str
    public_key: str
    short_name: str = "Unknown"
    long_name: str = "Unknown"
    hardware_model: str = "Unknown"
    macaddr: str = "Unknown"
    snr: Optional[float] = None
    rssi: Optional[int] = None
    battery: Optional[int] = None

@plugin_router.get("/config")
async def get_config():
    return _config

@plugin_router.post("/config")
async def set_config(body: ConfigModel):
    _config["enabled"] = body.enabled
    return {"status": "ok", **_config}

@plugin_router.post("/wipe")
async def wipe_database():
    with _db_lock:
        conn = _get_db()
        conn.execute("DELETE FROM trusted_keys")
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        conn.execute("VACUUM")
        conn.close()
    _config["db_version"] += 1
    return {"status": "ok"}

@plugin_router.post("/check")
async def check_key(req: KeyCheckRequest):
    if not _config["enabled"]:
        return {"status": "IGNORED"}

    with _db_lock:
        conn = _get_db()
        row = conn.execute("SELECT * FROM trusted_keys WHERE node_id = ?", (req.node_id,)).fetchone()
        
        if not row:
            ctx = json.dumps({"snr": req.snr, "rssi": req.rssi, "bat": req.battery, "hw": req.hardware_model, "mac": req.macaddr})
            conn.execute(
                """INSERT INTO trusted_keys 
                   (node_id, public_key, short_name, long_name, hardware_model, macaddr, first_seen, avg_snr, avg_rssi, ping_count, trust_score) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (req.node_id, req.public_key, req.short_name, req.long_name, req.hardware_model, req.macaddr, time.time(), req.snr or 0.0, req.rssi or 0.0, 1, 100)
            )
            conn.execute(
                "INSERT INTO audit_log (timestamp, node_id, event_type, details, context) VALUES (?, ?, ?, ?, ?)",
                (time.time(), req.node_id, "NEW_NODE", f"Identity baselined and trusted.", ctx)
            )
            conn.commit()
            status = "KEY_VERIFIED"
            risk_level = "NONE"
        else:
            # FIX: Cast the sqlite3.Row to a standard dictionary so .get() works without crashing
            row_dict = dict(row)
            
            if row_dict.get("public_key") == req.public_key:
                ping_count = row_dict.get("ping_count") or 0
                avg_snr = row_dict.get("avg_snr") or 0.0
                avg_rssi = row_dict.get("avg_rssi") or 0.0
                trust_score = row_dict.get("trust_score") or 100
                
                new_pings = ping_count + 1
                new_snr = ((avg_snr * ping_count) + req.snr) / new_pings if req.snr is not None else avg_snr
                new_rssi = ((avg_rssi * ping_count) + req.rssi) / new_pings if req.rssi is not None else avg_rssi
                new_trust = min(100, trust_score + 1)

                conn.execute(
                    "UPDATE trusted_keys SET short_name=?, long_name=?, hardware_model=?, macaddr=?, avg_snr=?, avg_rssi=?, ping_count=?, trust_score=? WHERE node_id=?", 
                    (req.short_name, req.long_name, req.hardware_model, req.macaddr, new_snr, new_rssi, new_pings, new_trust, req.node_id)
                )
                conn.commit()
                status = "KEY_VERIFIED"
                risk_level = "NONE"
                
            else:
                ping_count = row_dict.get("ping_count") or 0
                avg_snr = row_dict.get("avg_snr") or 0.0
                avg_rssi = row_dict.get("avg_rssi") or 0.0
                
                risk_level = "LOW"
                new_trust = 50 
                reasons = []

                hw_changed = row_dict.get("hardware_model") != req.hardware_model and row_dict.get("hardware_model") not in ["Unknown", None]
                mac_changed = row_dict.get("macaddr") != req.macaddr and row_dict.get("macaddr") not in ["Unknown", None]
                
                if hw_changed:
                    risk_level = "HIGH"
                    new_trust = 0
                    reasons.append(f"HW Mismatch ({row_dict.get('hardware_model')} -> {req.hardware_model})")
                if mac_changed:
                    risk_level = "HIGH"
                    new_trust = 0
                    reasons.append("MAC Mismatch")

                if ping_count > 5:
                    if req.snr is not None and abs(req.snr - avg_snr) >= 6.0:
                        risk_level = "HIGH"
                        new_trust = 0
                        reasons.append(f"SNR Shift")
                    if req.rssi is not None and abs(req.rssi - avg_rssi) >= 15.0:
                        risk_level = "HIGH"
                        new_trust = 0
                        reasons.append(f"RSSI Shift")
                
                if not reasons:
                    reasons.append("RF profile matches baseline (Likely Reflash).")

                conn.execute("UPDATE trusted_keys SET trust_score=? WHERE node_id=?", (new_trust, req.node_id))

                ctx = json.dumps({
                    "snr": req.snr, "rssi": req.rssi, "bat": req.battery,
                    "hw": req.hardware_model, "mac": req.macaddr,
                    "risk": risk_level, "score": new_trust,
                    "baseline_snr": round(avg_snr, 1) if ping_count > 0 else None,
                    "baseline_rssi": round(avg_rssi, 1) if ping_count > 0 else None,
                    "baseline_hw": row_dict.get("hardware_model")
                })

                alert_msg = f"Key Mismatch! " + " | ".join(reasons)

                last = conn.execute("SELECT timestamp FROM audit_log WHERE node_id=? AND event_type='SPOOF_DETECTED' ORDER BY timestamp DESC LIMIT 1", (req.node_id,)).fetchone()
                if not last or (time.time() - last["timestamp"]) > 60:
                    conn.execute(
                        "INSERT INTO audit_log (timestamp, node_id, event_type, details, context) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), req.node_id, "SPOOF_DETECTED", alert_msg, ctx)
                    )
                    conn.commit()
                
                status = "SPOOF_DETECTED"

        conn.close()
        return {"status": status, "risk": risk_level}

@plugin_router.get("/keys")
async def get_keys():
    with _db_lock:
        conn = _get_db()
        rows = conn.execute("SELECT * FROM trusted_keys ORDER BY first_seen DESC").fetchall()
        conn.close()
        return [dict(r) for r in rows]

@plugin_router.get("/logs")
async def get_logs(limit: int = 2000):
    with _db_lock:
        conn = _get_db()
        rows = conn.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

@plugin_router.post("/forgive/{node_id}")
async def forgive_key(node_id: str):
    with _db_lock:
        conn = _get_db()
        conn.execute("DELETE FROM trusted_keys WHERE node_id = ?", (node_id,))
        conn.execute(
            "INSERT INTO audit_log (timestamp, node_id, event_type, details, context) VALUES (?, ?, ?, ?, ?)",
            (time.time(), node_id, "KEY_RESET", "Operator manually revoked trust. Baseline wiped.", "{}")
        )
        conn.commit()
        conn.close()
    return {"status": "ok"}


@plugin_router.get("/duplicates")
async def get_duplicates():
    """Return cached duplicate key scan results."""
    with _dup_scan_lock:
        return dict(_dup_scan_result)


@plugin_router.post("/duplicates/scan")
async def trigger_duplicate_scan():
    """Manually trigger a duplicate key scan immediately."""
    result = await asyncio.to_thread(_run_duplicate_scan)
    return result