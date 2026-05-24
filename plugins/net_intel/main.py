"""
Network Intelligence Plugin — Backend API v1.0
================================================
Computes Link Quality Scores, Smart Hops, Network Entropy,
and Relay Contribution analysis from MeshDash's SQLite databases.

All computation is read-only SQL — no writes to the main DB.
"""

import asyncio
import json
import logging
import math
import sqlite3
import time
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

logger        = logging.getLogger("plugin.net_intel")
plugin_router = APIRouter()

_node_registry: Dict[str, Any] = {}
_event_loop:    Optional[asyncio.AbstractEventLoop] = None

# Score cache — recomputed every 2 minutes per slot
_lqs_cache: Dict[str, dict] = {}   # slot_id -> {node_id: lqs_dict}
_entropy_cache: Dict[str, dict] = {}  # slot_id -> entropy_dict
_cache_lock = threading.Lock()
_CACHE_TTL = 120  # seconds


def init_plugin(context):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    logger.info("Net Intel v1.0 — %d slot(s)", len(_node_registry))
    if _event_loop:
        try:
            asyncio.run_coroutine_threadsafe(_watchdog(context), _event_loop)
        except Exception as e:
            logger.error("Watchdog: %s", e)


async def _watchdog(context):
    wd, pid = context.get("plugin_watchdog"), context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Watchdog: %s", e)


# DB helpers

def _get_db_path(slot_id: str) -> str:
    slot = _node_registry.get(slot_id)
    if slot is None:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    db = getattr(slot, "db", None) or getattr(getattr(slot, "meshtastic_data", None), "db", None)
    if db is None:
        raise HTTPException(503, f"No database for slot '{slot_id}'")
    path = getattr(db, "db_path", None) or getattr(db, "_db_path_hint", None)
    if not path or path == ":memory:":
        raise HTTPException(503, f"Slot '{slot_id}' uses in-memory DB — no persistent data")
    return path


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10.0)
    c.row_factory = sqlite3.Row
    return c


def _list_slots_with_db():
    results = []
    for sid, slot in _node_registry.items():
        try:
            path = _get_db_path(sid)
            results.append({"slot_id": sid, "db_path": path,
                            "label": getattr(slot, "label", sid)})
        except Exception:
            pass
    return results


# Core computation functions

def _compute_lqs(path: str, node_id: str, now: float) -> dict:
    """Compute Link Quality Score (0-10) for one node."""
    score = 5.0
    components = []
    w24 = now - 86400
    w1  = now - 3600
    last_snr = None
    hop_avg = hop_min = hop_max = hop_std = None
    hops_list = []

    try:
        conn = _conn(path)

        # 1. Recent SNR
        snr_rows = conn.execute("""
            SELECT rx_snr, hop_limit, hop_start, timestamp
            FROM packets WHERE from_id=? AND rx_snr IS NOT NULL
              AND rx_snr != 0 AND rx_snr > -200 AND timestamp > ?
            ORDER BY timestamp DESC LIMIT 20
        """, (node_id, w24)).fetchall()

        if snr_rows:
            last_snr = snr_rows[0]["rx_snr"]
            if last_snr > -5:
                score += 1.0; components.append(("snr_strong", +1.0, f"SNR {last_snr:.1f}dB"))
            elif last_snr > -10:
                score += 0.5; components.append(("snr_ok", +0.5, f"SNR {last_snr:.1f}dB"))
            elif last_snr > -15:
                score -= 0.5; components.append(("snr_weak", -0.5, f"SNR {last_snr:.1f}dB"))
            else:
                score -= 1.5; components.append(("snr_poor", -1.5, f"SNR {last_snr:.1f}dB"))

        # 2. Hop stability
        hop_rows = conn.execute("""
            SELECT hop_start, hop_limit FROM packets
            WHERE from_id=? AND hop_start IS NOT NULL AND hop_limit IS NOT NULL
              AND hop_start >= hop_limit AND timestamp > ?
            ORDER BY timestamp DESC LIMIT 40
        """, (node_id, w24)).fetchall()

        hops_list = [r["hop_start"] - r["hop_limit"] for r in hop_rows]
        if len(hops_list) >= 2:
            hop_avg = sum(hops_list) / len(hops_list)
            hop_min = min(hops_list)
            hop_max = max(hops_list)
            hop_std = math.sqrt(sum((h - hop_avg)**2 for h in hops_list) / len(hops_list))
            if hop_std < 0.5:
                score += 0.5; components.append(("hop_stable", +0.5, f"Stable routing σ={hop_std:.2f}"))
            elif hop_std > 1.5:
                score -= 1.0; components.append(("hop_flapping", -1.0, f"Route flapping σ={hop_std:.2f}"))
        elif len(hops_list) == 1:
            hop_avg = hop_min = hop_max = float(hops_list[0])

        # 3. Traceroute success
        tr_rows = conn.execute("""
            SELECT route_path FROM traceroutes
            WHERE (from_id=? OR to_id=?) AND timestamp > ?
            ORDER BY timestamp DESC LIMIT 5
        """, (node_id, node_id, w1)).fetchall()

        tr_hit = False
        for tr in tr_rows:
            try:
                rp = json.loads(tr["route_path"]) if isinstance(tr["route_path"], str) else tr["route_path"]
                if rp and (rp.get("route_to") or rp.get("hops_used") is not None):
                    if not tr_hit:
                        score += 1.0; components.append(("traceroute_ok", +1.0, "Traceroute succeeded <1h"))
                        tr_hit = True
            except Exception:
                pass

        # 4. PKI errors
        pki = conn.execute("""
            SELECT COUNT(*) as n FROM hardware_logs
            WHERE node_id=? AND timestamp > ?
              AND (event_type LIKE '%PKI%' OR details LIKE '%PKI%'
                   OR details LIKE '%encrypt%' OR details LIKE '%UNKNOWN_PUBKEY%')
        """, (node_id, w1)).fetchone()
        if pki and pki["n"] > 0:
            score -= 5.0; components.append(("pki_error", -5.0, f"{pki['n']} PKI error(s)"))

        # 5. Silence detection
        node_row = conn.execute(
            "SELECT last_heard FROM nodes WHERE node_id=?", (node_id,)
        ).fetchone()
        silence_factor = None
        if node_row and node_row["last_heard"]:
            silence_secs = now - node_row["last_heard"]
            ni_rows = conn.execute("""
                SELECT timestamp FROM packets WHERE from_id=?
                  AND portnum LIKE '%NODEINFO%'
                ORDER BY timestamp DESC LIMIT 3
            """, (node_id,)).fetchall()
            expected = 3600
            if len(ni_rows) >= 2:
                obs = ni_rows[0]["timestamp"] - ni_rows[-1]["timestamp"]
                if 60 < obs < 86400:
                    expected = obs / max(len(ni_rows) - 1, 1)
            silence_factor = silence_secs / max(expected, 60)
            if silence_factor > 3.0:
                score -= 1.0; components.append(("silent", -1.0, f"Silent {silence_factor:.1f}× interval"))

        # 6. Neighbor confirmation
        nb = conn.execute("""
            SELECT snr FROM neighbors
            WHERE node_id=? OR neighbor_id=? LIMIT 1
        """, (node_id, node_id)).fetchone()
        if nb:
            score += 0.5
            snr_txt = f" SNR {nb['snr']}dB" if nb["snr"] else ""
            components.append(("neighbor_ok", +0.5, f"Direct RF neighbor{snr_txt}"))

        conn.close()
    except Exception as e:
        logger.debug("LQS error %s: %s", node_id, e)

    score = max(0.0, min(10.0, score))
    return {
        "node_id":    node_id,
        "score":      round(score, 1),
        "last_snr":   last_snr,
        "hop_avg":    round(hop_avg, 2) if hop_avg is not None else None,
        "hop_min":    int(hop_min) if hop_min is not None else None,
        "hop_max":    int(hop_max) if hop_max is not None else None,
        "hop_std":    round(hop_std, 2) if hop_std is not None else None,
        "hops_count": len(hops_list),
        "silence_factor": round(silence_factor, 2) if silence_factor is not None else None,
        "components": components,
        "computed_at": now,
    }


def _compute_smart_hops(path: str, node_id: str, now: float) -> List[dict]:
    """15-min bucketed hop counts over 24h."""
    w24 = now - 86400
    bucket = 900
    results = []
    try:
        conn = _conn(path)
        rows = conn.execute("""
            SELECT
                CAST((timestamp - ?) / ? AS INTEGER) * ? + ? AS bucket_ts,
                MIN(hop_start - hop_limit) AS hop_min,
                AVG(hop_start - hop_limit) AS hop_avg,
                MAX(hop_start - hop_limit) AS hop_max,
                COUNT(*) AS pkt_count
            FROM packets
            WHERE from_id=? AND hop_start IS NOT NULL AND hop_limit IS NOT NULL
              AND hop_start >= hop_limit AND timestamp > ?
            GROUP BY CAST((timestamp - ?) / ? AS INTEGER)
            ORDER BY bucket_ts ASC
        """, (w24, bucket, bucket, w24, node_id, w24, w24, bucket)).fetchall()
        for r in rows:
            results.append({
                "ts": r["bucket_ts"], "hop_min": r["hop_min"],
                "hop_avg": round(r["hop_avg"], 2), "hop_max": r["hop_max"],
                "pkt_count": r["pkt_count"],
            })
        conn.close()
    except Exception as e:
        logger.debug("smart_hops %s: %s", node_id, e)
    return results


def _compute_snr_history(path: str, node_id: str, now: float, hours: int = 24) -> List[dict]:
    """Hourly SNR/RSSI averages for a node."""
    window = now - hours * 3600
    bucket = 3600
    results = []
    try:
        conn = _conn(path)
        rows = conn.execute("""
            SELECT
                CAST((timestamp - ?) / ? AS INTEGER) * ? + ? AS bucket_ts,
                AVG(rx_snr) AS avg_snr,
                MIN(rx_snr) AS min_snr,
                MAX(rx_snr) AS max_snr,
                AVG(rx_rssi) AS avg_rssi,
                COUNT(*) AS pkt_count
            FROM packets
            WHERE from_id=? AND rx_snr IS NOT NULL
              AND rx_snr != 0 AND rx_snr > -200 AND timestamp > ?
            GROUP BY CAST((timestamp - ?) / ? AS INTEGER)
            ORDER BY bucket_ts ASC
        """, (window, bucket, bucket, window, node_id, window, window, bucket)).fetchall()
        for r in rows:
            results.append({
                "ts": r["bucket_ts"],
                "avg_snr": round(r["avg_snr"], 1) if r["avg_snr"] else None,
                "min_snr": round(r["min_snr"], 1) if r["min_snr"] else None,
                "max_snr": round(r["max_snr"], 1) if r["max_snr"] else None,
                "avg_rssi": round(r["avg_rssi"]) if r["avg_rssi"] else None,
                "pkt_count": r["pkt_count"],
            })
        conn.close()
    except Exception:
        pass
    return results


def _compute_network_entropy(path: str, now: float) -> dict:
    """Fleet-wide chaos metric 0-10."""
    w1h = now - 3600
    entropy = 0.0
    components = []
    try:
        conn = _conn(path)

        # Routing instability
        hop_rows = conn.execute("""
            SELECT hop_start - hop_limit AS hops FROM packets
            WHERE hop_start IS NOT NULL AND hop_limit IS NOT NULL
              AND hop_start >= hop_limit AND timestamp > ?
        """, (w1h,)).fetchall()
        if hop_rows:
            all_hops = [r["hops"] for r in hop_rows]
            avg_h = sum(all_hops) / len(all_hops)
            var_h = sum((h - avg_h)**2 for h in all_hops) / len(all_hops)
            ri = min(3.0, var_h * 0.75)
            entropy += ri
            components.append(("routing_instability", round(ri, 2),
                                f"Hop variance {var_h:.2f} ({len(all_hops)} pkts/hr)"))

        # Channel congestion
        util = conn.execute("""
            SELECT AVG(channel_utilization) AS u FROM nodes
            WHERE channel_utilization IS NOT NULL AND channel_utilization > 0
        """).fetchone()
        if util and util["u"]:
            u = util["u"]
            ce = min(3.0, u / 16.67)   # 50% util = 3.0
            entropy += ce
            components.append(("channel_congestion", round(ce, 2),
                                f"Avg channel util {u:.1f}%"))

        # Traffic concentration (Gini)
        pkt_rows = conn.execute("""
            SELECT from_id, COUNT(*) as cnt FROM packets
            WHERE timestamp > ? GROUP BY from_id
        """, (w1h,)).fetchall()
        if len(pkt_rows) >= 3:
            counts = sorted([r["cnt"] for r in pkt_rows])
            n = len(counts); total = sum(counts)
            if total > 0:
                gini = sum(abs(counts[i] - counts[j]) for i in range(n) for j in range(n)) / (2 * n * total)
                ge = min(2.0, gini * 2.0)
                entropy += ge
                components.append(("relay_concentration", round(ge, 2),
                                    f"Traffic Gini {gini:.2f}"))

        # Silent node ratio
        total_n = conn.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"]
        silent_n = conn.execute("""
            SELECT COUNT(*) AS n FROM nodes
            WHERE last_heard IS NOT NULL AND last_heard < ?
        """, (now - 10800,)).fetchone()["n"]
        if total_n > 0:
            se = min(2.0, (silent_n / total_n) * 4.0)
            entropy += se
            components.append(("silent_nodes", round(se, 2),
                                f"{silent_n}/{total_n} silent >3h"))

        conn.close()
    except Exception as e:
        logger.debug("entropy: %s", e)

    entropy = min(10.0, max(0.0, entropy))
    label = "STABLE" if entropy < 2 else "NORMAL" if entropy < 5 else "DEGRADED" if entropy < 8 else "CHAOTIC"
    color = "#00e87a" if entropy < 2 else "#ffa826" if entropy < 5 else "#ff8c00" if entropy < 8 else "#ff3050"
    return {
        "entropy": round(entropy, 2), "label": label, "color": color,
        "components": components, "computed_at": now,
    }


def _compute_relay_map(path: str, now: float) -> List[dict]:
    """Which nodes carry the most relay traffic?"""
    w24 = now - 86400
    results = []
    try:
        conn = _conn(path)

        # Get relay counts from traceroutes
        tr_relay: Dict[str, int] = {}
        for r in conn.execute("SELECT route_path FROM traceroutes WHERE timestamp > ?", (w24,)).fetchall():
            try:
                rp = json.loads(r["route_path"]) if isinstance(r["route_path"], str) else r["route_path"]
                for nid in (rp.get("route_to") or []):
                    tr_relay[nid] = tr_relay.get(nid, 0) + 1
            except Exception:
                pass

        # Packet volume where hop_start > hop_limit (was relayed)
        pkt_rows = conn.execute("""
            SELECT from_id, COUNT(*) AS total,
                   COUNT(DISTINCT to_id) AS unique_dests,
                   AVG(CAST(hop_start - hop_limit AS REAL)) AS avg_hops_consumed
            FROM packets
            WHERE timestamp > ? AND hop_start IS NOT NULL AND hop_limit IS NOT NULL
            GROUP BY from_id ORDER BY total DESC LIMIT 30
        """, (w24,)).fetchall()

        node_info = {r["node_id"]: dict(r) for r in
                     conn.execute("SELECT node_id, long_name, short_name, role FROM nodes").fetchall()}

        max_pkts = max((r["total"] for r in pkt_rows), default=1)
        for r in pkt_rows:
            nid = r["from_id"]
            ni  = node_info.get(nid, {})
            results.append({
                "node_id":         nid,
                "long_name":       ni.get("long_name") or nid,
                "short_name":      ni.get("short_name") or nid[-4:],
                "role":            ni.get("role") or "UNKNOWN",
                "packet_count":    r["total"],
                "unique_dests":    r["unique_dests"],
                "avg_hops":        round(r["avg_hops_consumed"], 2) if r["avg_hops_consumed"] else None,
                "traceroute_relays": tr_relay.get(nid, 0),
                "load_pct":        round(100 * r["total"] / max_pkts),
            })
        conn.close()
    except Exception as e:
        logger.debug("relay_map: %s", e)
    return results


def _compute_all_lqs(path: str, now: float) -> dict:
    """Batch LQS for all nodes in one DB pass."""
    results = {}
    try:
        conn = _conn(path)
        node_ids = [r["node_id"] for r in conn.execute("SELECT node_id FROM nodes").fetchall()]
        conn.close()
        for nid in node_ids:
            results[nid] = _compute_lqs(path, nid, now)
    except Exception as e:
        logger.debug("all_lqs: %s", e)
    return results


def _get_cached_lqs(slot_id: str, path: str) -> dict:
    """Return cached LQS or recompute if stale."""
    with _cache_lock:
        cached = _lqs_cache.get(slot_id)
        if cached and (time.time() - cached.get("_ts", 0)) < _CACHE_TTL:
            return cached
    now  = time.time()
    data = _compute_all_lqs(path, now)
    data["_ts"] = now
    with _cache_lock:
        _lqs_cache[slot_id] = data
    return data


def _get_cached_entropy(slot_id: str, path: str) -> dict:
    with _cache_lock:
        cached = _entropy_cache.get(slot_id)
        if cached and (time.time() - cached.get("_ts", 0)) < _CACHE_TTL:
            return cached
    now  = time.time()
    data = _compute_network_entropy(path, now)
    data["_ts"] = now
    with _cache_lock:
        _entropy_cache[slot_id] = data
    return data


# Routes

@plugin_router.get("")
@plugin_router.get("/")
async def health():
    return {"plugin": "net_intel", "version": "1.0.0", "status": "running",
            "slots": len(_node_registry)}


@plugin_router.get("/slots")
async def list_slots():
    return {"slots": _list_slots_with_db()}


@plugin_router.get("/lqs/{slot_id}")
async def get_all_lqs(slot_id: str):
    """All node LQS scores for a slot."""
    path = _get_db_path(slot_id)
    data = await asyncio.to_thread(_get_cached_lqs, slot_id, path)
    # Strip internal cache key
    return {k: v for k, v in data.items() if not k.startswith("_")}


@plugin_router.get("/lqs/{slot_id}/{node_id}")
async def get_node_lqs(slot_id: str, node_id: str):
    """LQS for a single node — uses cache."""
    path = _get_db_path(slot_id)
    data = await asyncio.to_thread(_get_cached_lqs, slot_id, path)
    result = data.get(node_id)
    if result is None:
        # Not in cache yet — compute fresh
        result = await asyncio.to_thread(_compute_lqs, path, node_id, time.time())
    return result


@plugin_router.get("/smart_hops/{slot_id}/{node_id}")
async def get_smart_hops(slot_id: str, node_id: str):
    path = _get_db_path(slot_id)
    now  = time.time()
    data = await asyncio.to_thread(_compute_smart_hops, path, node_id, now)
    return {"node_id": node_id, "buckets": data}


@plugin_router.get("/snr_history/{slot_id}/{node_id}")
async def get_snr_history(slot_id: str, node_id: str, hours: int = Query(24, ge=1, le=168)):
    path = _get_db_path(slot_id)
    now  = time.time()
    data = await asyncio.to_thread(_compute_snr_history, path, node_id, now, hours)
    return {"node_id": node_id, "hours": hours, "buckets": data}


@plugin_router.get("/entropy/{slot_id}")
async def get_entropy(slot_id: str):
    path = _get_db_path(slot_id)
    return await asyncio.to_thread(_get_cached_entropy, slot_id, path)


@plugin_router.get("/relay_map/{slot_id}")
async def get_relay_map(slot_id: str):
    path = _get_db_path(slot_id)
    now  = time.time()
    data = await asyncio.to_thread(_compute_relay_map, path, now)
    return {"slot_id": slot_id, "nodes": data}


@plugin_router.get("/fleet/{slot_id}")
async def get_fleet_summary(slot_id: str):
    """Combined fleet summary for dashboard overview."""
    path = _get_db_path(slot_id)
    now  = time.time()

    lqs_data    = await asyncio.to_thread(_get_cached_lqs, slot_id, path)
    entropy     = await asyncio.to_thread(_get_cached_entropy, slot_id, path)
    relay_data  = await asyncio.to_thread(_compute_relay_map, path, now)

    # Aggregate LQS stats
    scores = [v["score"] for k, v in lqs_data.items() if not k.startswith("_") and isinstance(v, dict)]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    healthy   = sum(1 for s in scores if s >= 7)
    degraded  = sum(1 for s in scores if 4 <= s < 7)
    critical  = sum(1 for s in scores if s < 4)

    # Top relays
    top_relays = sorted(relay_data, key=lambda x: x["packet_count"], reverse=True)[:5]

    # Nodes with LQS < 4 (critical)
    critical_nodes = sorted(
        [v for k, v in lqs_data.items() if not k.startswith("_") and isinstance(v, dict) and v["score"] < 4],
        key=lambda x: x["score"]
    )[:10]

    return {
        "slot_id":     slot_id,
        "node_count":  len(scores),
        "avg_lqs":     avg_score,
        "healthy":     healthy,
        "degraded":    degraded,
        "critical":    critical,
        "entropy":     entropy,
        "top_relays":  top_relays,
        "critical_nodes": critical_nodes,
        "computed_at": now,
    }


@plugin_router.get("/neighbors/{slot_id}")
async def get_neighbors(slot_id: str):
    """Neighbor graph with SNR for link visualization."""
    path = _get_db_path(slot_id)
    try:
        conn = _conn(path)
        nb = [dict(r) for r in conn.execute(
            "SELECT node_id, neighbor_id, snr, last_seen FROM neighbors ORDER BY last_seen DESC LIMIT 500"
        ).fetchall()]
        node_info = {r["node_id"]: dict(r) for r in conn.execute(
            "SELECT node_id, long_name, short_name FROM nodes"
        ).fetchall()}
        conn.close()
        return {"neighbors": nb, "nodes": node_info}
    except Exception as e:
        raise HTTPException(500, str(e))


@plugin_router.get("/traceroutes/{slot_id}")
async def get_traceroutes(slot_id: str, limit: int = Query(50, ge=1, le=200)):
    path = _get_db_path(slot_id)
    try:
        conn = _conn(path)
        rows = conn.execute(
            "SELECT * FROM traceroutes ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["route_path"] = json.loads(d["route_path"]) if isinstance(d["route_path"], str) else d["route_path"]
            except Exception:
                pass
            results.append(d)
        conn.close()
        return {"traceroutes": results}
    except Exception as e:
        raise HTTPException(500, str(e))


@plugin_router.post("/invalidate/{slot_id}")
async def invalidate_cache(slot_id: str):
    """Force cache refresh for a slot."""
    with _cache_lock:
        _lqs_cache.pop(slot_id, None)
        _entropy_cache.pop(slot_id, None)
    return {"status": "invalidated", "slot_id": slot_id}


# Fleet Overview — shown BEFORE a node is selected
# Everything computable from a single DB pass

def _compute_fleet_overview(path: str, now: float) -> dict:
    """
    Comprehensive fleet-wide health snapshot.
    Used for the 'no node selected' overview panel.
    """
    w1h  = now - 3600
    w6h  = now - 21600
    w24h = now - 86400
    w7d  = now - 604800

    result = {}
    try:
        conn = _conn(path)

        all_nodes = conn.execute("SELECT * FROM nodes WHERE is_local=FALSE").fetchall()
        total = len(all_nodes)
        active_1h  = sum(1 for n in all_nodes if n["last_heard"] and n["last_heard"] > w1h)
        active_6h  = sum(1 for n in all_nodes if n["last_heard"] and n["last_heard"] > w6h)
        active_24h = sum(1 for n in all_nodes if n["last_heard"] and n["last_heard"] > w24h)
        silent_3h  = sum(1 for n in all_nodes if not n["last_heard"] or n["last_heard"] < now - 10800)

        # Battery stats
        bats = [n["battery_level"] for n in all_nodes if n["battery_level"] and 0 < n["battery_level"] <= 100]
        avg_bat = round(sum(bats)/len(bats), 1) if bats else None
        low_bat = sum(1 for b in bats if b < 20)
        crit_bat = sum(1 for b in bats if b < 10)

        # Channel utilisation
        utils = [n["channel_utilization"] for n in all_nodes if n["channel_utilization"] and n["channel_utilization"] > 0]
        avg_util = round(sum(utils)/len(utils), 2) if utils else None
        high_util = sum(1 for u in utils if u > 25)

        # SNR stats from nodes table
        snrs = [n["snr"] for n in all_nodes if n["snr"] and n["snr"] > -200 and n["snr"] != 0]
        avg_snr = round(sum(snrs)/len(snrs), 1) if snrs else None
        poor_snr = sum(1 for s in snrs if s < -15)

        result["nodes"] = {
            "total": total, "active_1h": active_1h, "active_6h": active_6h,
            "active_24h": active_24h, "silent_3h": silent_3h,
        }
        result["battery"] = {
            "avg": avg_bat, "low_count": low_bat, "critical_count": crit_bat,
            "tracked": len(bats),
        }
        result["channel"] = {
            "avg_utilization": avg_util, "high_util_count": high_util,
        }
        result["snr"] = {
            "avg": avg_snr, "poor_count": poor_snr, "tracked": len(snrs),
        }

        pkt_24h = conn.execute(
            "SELECT COUNT(*) as n FROM packets WHERE timestamp > ?", (w24h,)
        ).fetchone()["n"]
        pkt_1h = conn.execute(
            "SELECT COUNT(*) as n FROM packets WHERE timestamp > ?", (w1h,)
        ).fetchone()["n"]
        msg_24h = conn.execute(
            "SELECT COUNT(*) as n FROM messages WHERE timestamp > ?", (w24h,)
        ).fetchone()["n"]
        tr_24h = conn.execute(
            "SELECT COUNT(*) as n FROM traceroutes WHERE timestamp > ?", (w24h,)
        ).fetchone()["n"]
        result["traffic"] = {
            "packets_24h": pkt_24h, "packets_1h": pkt_1h,
            "messages_24h": msg_24h, "traceroutes_24h": tr_24h,
        }

        bucket = 3600
        pkt_hourly = conn.execute("""
            SELECT
                CAST((timestamp - ?) / ? AS INTEGER) AS slot,
                COUNT(*) AS n
            FROM packets WHERE timestamp > ?
            GROUP BY slot ORDER BY slot ASC
        """, (w24h, bucket, w24h)).fetchall()
        # Build full 24-bucket array with gaps as 0
        rate_arr = [0] * 24
        for r in pkt_hourly:
            idx = min(23, max(0, int(r["slot"])))
            rate_arr[idx] = r["n"]
        result["packet_rate_24h"] = rate_arr

        snr_hist = conn.execute("""
            SELECT timestamp, average_snr, average_rssi, node_count
            FROM average_metrics_history
            WHERE timestamp > ?
            ORDER BY timestamp ASC LIMIT 200
        """, (w7d,)).fetchall()
        result["snr_history"] = [dict(r) for r in snr_hist]

        result["node_count_history"] = [
            {"ts": r["timestamp"], "count": r["node_count"]}
            for r in snr_hist
        ]

        roles = {}
        for n in all_nodes:
            r = n["role"] or "UNKNOWN"
            roles[r] = roles.get(r, 0) + 1
        result["roles"] = roles

        hw_models: dict = {}
        for n in all_nodes:
            hw = n["hw_model"] or "Unknown"
            hw_models[hw] = hw_models.get(hw, 0) + 1
        result["hw_models"] = dict(sorted(hw_models.items(), key=lambda x: -x[1])[:10])

        talkers = conn.execute("""
            SELECT from_id, COUNT(*) as n FROM packets
            WHERE timestamp > ? GROUP BY from_id ORDER BY n DESC LIMIT 10
        """, (w24h,)).fetchall()
        nmap = {n["node_id"]: (n["long_name"] or n["node_id"]) for n in all_nodes}
        nmap.update({n["node_id"]: n["node_id"] for n in all_nodes if not n["long_name"]})
        result["top_talkers"] = [
            {"node_id": r["from_id"], "name": nmap.get(r["from_id"], r["from_id"]), "count": r["n"]}
            for r in talkers
        ]

        # Computed from multiple signals, saved with timestamp for trending
        hs = 100.0
        if total > 0:
            # Activity penalty: if <50% of nodes active in 24h
            act_ratio = active_24h / total
            if act_ratio < 0.5: hs -= (0.5 - act_ratio) * 40
        if avg_util and avg_util > 25: hs -= min(20, (avg_util - 25) * 0.8)
        if avg_snr and avg_snr < -10:  hs -= min(20, abs(avg_snr + 10) * 2)
        if bats and low_bat > 0:       hs -= min(15, low_bat * 3)
        if silent_3h and total > 0:    hs -= min(10, (silent_3h / total) * 20)
        hs = max(0, min(100, round(hs, 1)))

        result["health_score"] = hs
        result["health_label"] = (
            "EXCELLENT" if hs >= 90 else
            "GOOD" if hs >= 75 else
            "FAIR" if hs >= 55 else
            "DEGRADED" if hs >= 35 else
            "CRITICAL"
        )
        result["health_color"] = (
            "#00e87a" if hs >= 90 else
            "#00c8f5" if hs >= 75 else
            "#ffa826" if hs >= 55 else
            "#ff8c00" if hs >= 35 else
            "#ff3050"
        )

        hw_events = conn.execute("""
            SELECT node_id, event_type, details, timestamp
            FROM hardware_logs ORDER BY timestamp DESC LIMIT 10
        """).fetchall()
        result["recent_events"] = [dict(r) for r in hw_events]

        conn.close()
    except Exception as e:
        logger.debug("fleet_overview: %s", e)

    result["computed_at"] = now
    return result


@plugin_router.get("/overview/{slot_id}")
async def get_fleet_overview(slot_id: str):
    """Full fleet overview — used for the pre-node-select dashboard."""
    path = _get_db_path(slot_id)
    now  = time.time()
    data = await asyncio.to_thread(_compute_fleet_overview, path, now)

    # Enrich with entropy and overall LQS distribution
    ent = await asyncio.to_thread(_get_cached_entropy, slot_id, path)
    lqs = await asyncio.to_thread(_get_cached_lqs, slot_id, path)

    scores = [v["score"] for k, v in lqs.items()
              if not k.startswith("_") and isinstance(v, dict) and "score" in v]
    data["lqs_distribution"] = {
        "scores": scores,
        "avg": round(sum(scores)/len(scores), 1) if scores else 0,
        "healthy":  sum(1 for s in scores if s >= 7),
        "degraded": sum(1 for s in scores if 4 <= s < 7),
        "critical": sum(1 for s in scores if s < 4),
    }
    data["entropy"] = ent
    return data