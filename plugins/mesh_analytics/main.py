import os
import sys
import json
import asyncio
import time
import math
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PLUGIN_DIR)

plugin_router = APIRouter()

_md = None
_db = None
_reg = None
_watchdog_task: Optional[asyncio.Task] = None


async def _watchdog_heartbeat():
    """Pings the MeshDash core watchdog every 30 s."""
    while True:
        try:
            await asyncio.sleep(30)
            wd = _md.get("plugin_watchdog") if isinstance(_md, dict) else None
            pid = _md.get("plugin_id") if isinstance(_md, dict) else None
            if wd is not None and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


def init_plugin(context: dict):
    global _md, _db, _reg, _watchdog_task
    _md  = context  # Store full context for watchdog access
    _db  = context["db_manager"]
    _reg = context["node_registry"]
    
    # Start watchdog heartbeat
    try:
        loop = context.get("event_loop") or asyncio.get_event_loop()
        _watchdog_task = loop.create_task(_watchdog_heartbeat())
    except Exception:
        pass



def _snr_score(snr):
    if snr is None: return 0.0
    return max(0.0, min(1.0, (float(snr) + 10) / 20.0))

def _rssi_score(rssi):
    if rssi is None: return 0.0
    return max(0.0, min(1.0, (float(rssi) + 120) / 50.0))

def _age_score(last_heard):
    if last_heard is None: return 0.0
    age = time.time() - last_heard
    if age < 300:   return 1.0
    if age < 3600:  return 0.8
    if age < 86400: return 0.4
    return 0.0

def _centrality_score(relay_count, neighbor_count, packet_count, total_packets):
    rc  = math.log1p(relay_count or 0)   / math.log1p(50)
    nc  = math.log1p(neighbor_count or 0) / math.log1p(20)
    pct = (packet_count or 0) / max(total_packets, 1)
    return min(1.0, (rc * 0.45 + nc * 0.30 + pct * 0.25))

def _battery_risk(batt, batt_avg, batt_min):
    if batt is None: return None
    b = float(batt)
    trend = 0.0
    if batt_avg is not None and batt_min is not None:
        drop = float(batt_avg) - float(batt_min)
        trend = min(1.0, drop / 40.0)
    base = 1.0 - (b / 100.0)
    return round(min(1.0, base * 0.7 + trend * 0.3), 3)

def _chan_stress(chan_util, air_util):
    if chan_util is None and air_util is None: return None
    c = float(chan_util or 0)
    a = float(air_util or 0)
    return round(min(1.0, (c / 100.0) * 0.6 + (a / 100.0) * 0.4), 3)

def _stddev(vals):
    if len(vals) < 2: return None
    mean = sum(vals) / len(vals)
    return round(math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)), 3)

def _pct(lst, pct):
    if not lst: return None
    s = sorted(lst)
    idx = max(0, int(len(s) * pct) - 1)
    return round(s[idx], 2)

def _linear_trend(vals):
    """Returns slope (positive = rising, negative = falling) normalised to per-hour."""
    n = len(vals)
    if n < 3: return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(vals) / n
    num = sum((i - x_mean) * (vals[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return round(num / den, 4) if den else None



async def _compute_analytics(db, target_ts: float) -> dict:
    def _run():
        conn = db._get_connection()
        T    = target_ts
        now  = time.time()

        nodes_raw = conn.execute("""
            SELECT node_id,long_name,short_name,role,is_local,last_heard,
                   battery_level,voltage,channel_utilization,air_util_tx,
                   snr,rssi,latitude,longitude
            FROM nodes
        """).fetchall()

        pkt_total = dict(conn.execute(
            "SELECT from_id,COUNT(*) FROM packets WHERE timestamp<=? GROUP BY from_id",(T,)
        ).fetchall())
        rf_stats = {}
        for r in conn.execute(
            "SELECT from_id,ROUND(AVG(rx_snr),2),ROUND(MIN(rx_snr),2),ROUND(MAX(rx_snr),2),"
            "ROUND(AVG(rx_rssi),1),COUNT(*) FROM packets WHERE rx_snr IS NOT NULL AND timestamp<=? GROUP BY from_id",(T,)
        ).fetchall():
            rf_stats[r[0]] = {"avg_snr":r[1],"min_snr":r[2],"max_snr":r[3],"avg_rssi":r[4],"rf_count":r[5]}

        snr_raw_per_node = {}
        for r in conn.execute(
            "SELECT from_id,rx_snr FROM packets WHERE rx_snr IS NOT NULL AND timestamp<=?",(T,)
        ).fetchall():
            snr_raw_per_node.setdefault(r[0],[]).append(r[1])
        snr_stddev = {}
        for nid, vals in snr_raw_per_node.items():
            snr_stddev[nid] = _stddev(vals)

        pkt_src = {}
        for r in conn.execute(
            "SELECT from_id,source,COUNT(*) FROM packets WHERE timestamp<=? AND source IS NOT NULL GROUP BY from_id,source",(T,)
        ).fetchall():
            pkt_src.setdefault(r[0],{})[r[1]] = r[2]

        hop_stats = {}
        for r in conn.execute(
            "SELECT from_id,ROUND(AVG(hop_limit),2),MIN(hop_limit),MAX(hop_start) "
            "FROM packets WHERE timestamp<=? AND hop_limit IS NOT NULL GROUP BY from_id",(T,)
        ).fetchall():
            hop_stats[r[0]] = {"avg":r[1],"min":r[2],"max_start":r[3],"std":None}

        hop_raw = {}
        for r in conn.execute(
            "SELECT from_id,hop_limit FROM packets WHERE timestamp<=? AND hop_limit IS NOT NULL",(T,)
        ).fetchall():
            hop_raw.setdefault(r[0],[]).append(r[1])
        for nid, vals in hop_raw.items():
            if nid in hop_stats and len(vals) >= 2:
                hop_stats[nid]["std"] = _stddev(vals)

        cutoff_24 = T - 86400
        cutoff_48 = T - 172800
        pkt_last24 = dict(conn.execute(
            "SELECT from_id,COUNT(*) FROM packets WHERE timestamp>? AND timestamp<=? GROUP BY from_id",(cutoff_24,T)
        ).fetchall())
        pkt_prior24 = dict(conn.execute(
            "SELECT from_id,COUNT(*) FROM packets WHERE timestamp>? AND timestamp<=? GROUP BY from_id",(cutoff_48,cutoff_24)
        ).fetchall())

        hourly_dist = [0] * 24
        for r in conn.execute(
            "SELECT CAST(strftime('%H', datetime(timestamp,'unixepoch')) AS INTEGER),COUNT(*) "
            "FROM packets WHERE timestamp>? GROUP BY 1",(T - 604800,)
        ).fetchall():
            if 0 <= r[0] < 24:
                hourly_dist[r[0]] = r[1]

        daily_traffic = []
        for r in conn.execute(
            "SELECT date(timestamp,'unixepoch') d,"
            "SUM(CASE WHEN to_id='^all' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN to_id!='^all' THEN 1 ELSE 0 END) "
            "FROM messages WHERE timestamp<=? GROUP BY d ORDER BY d ASC LIMIT 30",(T,)
        ).fetchall():
            if r[0]:
                daily_traffic.append({"date":r[0],"bcast":r[1] or 0,"dm":r[2] or 0})

        daily_rf = []
        for r in conn.execute(
            "SELECT date(timestamp,'unixepoch') d,"
            "ROUND(AVG(rx_snr),2),ROUND(AVG(rx_rssi),1),COUNT(*) "
            "FROM packets WHERE rx_snr IS NOT NULL AND timestamp<=? GROUP BY d ORDER BY d ASC LIMIT 30",(T,)
        ).fetchall():
            if r[0]:
                daily_rf.append({"date":r[0],"avg_snr":r[1],"avg_rssi":r[2],"count":r[3]})

        daily_active_nodes = []
        for r in conn.execute(
            "SELECT date(timestamp,'unixepoch') d, COUNT(DISTINCT from_id) "
            "FROM packets WHERE timestamp<=? GROUP BY d ORDER BY d ASC LIMIT 30",(T,)
        ).fetchall():
            if r[0]:
                daily_active_nodes.append({"date":r[0],"count":r[1]})

        hop_dist = {}
        for r in conn.execute(
            "SELECT hop_limit,COUNT(*) FROM packets WHERE timestamp<=? AND hop_limit IS NOT NULL GROUP BY hop_limit",(T,)
        ).fetchall():
            hop_dist[r[0]] = r[1]

        relay_cnt = {}
        route_appearances = {}
        link_usage = {}   # (a,b) sorted tuple -> count
        all_routes = conn.execute(
            "SELECT from_id,to_id,route_path,timestamp FROM traceroutes WHERE timestamp<=?",(T,)
        ).fetchall()
        for r in all_routes:
            try:
                rp = json.loads(r[2]) if r[2] else {}
                path = rp.get("route_to",[]) + rp.get("route_back",[])
                for nid in path:
                    relay_cnt[nid] = relay_cnt.get(nid,0) + 1
                for nid in set(path):
                    route_appearances[nid] = route_appearances.get(nid,0) + 1
                # track link pairs
                full_path = [r[0]] + rp.get("route_to",[]) + [r[1]]
                for i in range(len(full_path)-1):
                    a,b = full_path[i],full_path[i+1]
                    key = (min(a,b), max(a,b))
                    link_usage[key] = link_usage.get(key,0) + 1
            except Exception:
                pass

        tr_sent = dict(conn.execute(
            "SELECT from_id,COUNT(*) FROM traceroutes WHERE timestamp<=? GROUP BY from_id",(T,)
        ).fetchall())
        tr_recv = dict(conn.execute(
            "SELECT to_id,COUNT(*) FROM traceroutes WHERE timestamp<=? GROUP BY to_id",(T,)
        ).fetchall())

        nb_all = {}
        nb_snr_sum = {}
        for r in conn.execute("SELECT node_id,neighbor_id,snr FROM neighbors").fetchall():
            nb_all.setdefault(r[0],[]).append({"id":r[1],"snr":r[2]})
            nb_snr_sum.setdefault(r[0],[])
            if r[2] is not None:
                nb_snr_sum[r[0]].append(float(r[2]))

        msg_bcast = dict(conn.execute(
            "SELECT from_id,COUNT(*) FROM messages WHERE to_id='^all' AND timestamp<=? GROUP BY from_id",(T,)
        ).fetchall())
        msg_dm = dict(conn.execute(
            "SELECT from_id,COUNT(*) FROM messages WHERE to_id!='^all' AND to_id IS NOT NULL AND timestamp<=? GROUP BY from_id",(T,)
        ).fetchall())

        tlm_stats = {}
        for r in conn.execute(
            "SELECT node_id,MIN(battery_level),ROUND(AVG(battery_level),1),"
            "MIN(voltage),ROUND(AVG(voltage),2),ROUND(AVG(channel_utilization),2),"
            "ROUND(AVG(air_util_tx),2) FROM telemetry WHERE timestamp<=? AND battery_level IS NOT NULL GROUP BY node_id",(T,)
        ).fetchall():
            tlm_stats[r[0]] = {"batt_min":r[1],"batt_avg":r[2],"volt_min":r[3],"volt_avg":r[4],
                                "avg_chan":r[5],"avg_air":r[6]}

        env_data = {}
        for r in conn.execute(
            "SELECT node_id,"
            "ROUND(AVG(temperature),2),ROUND(MIN(temperature),2),ROUND(MAX(temperature),2),"
            "ROUND(AVG(relative_humidity),1),ROUND(AVG(barometric_pressure),2),"
            "ROUND(AVG(gas_resistance),1),ROUND(AVG(iaq),1),COUNT(*) "
            "FROM telemetry WHERE timestamp<=? AND temperature IS NOT NULL GROUP BY node_id",(T,)
        ).fetchall():
            env_data[r[0]] = {
                "avg_temp":r[1],"min_temp":r[2],"max_temp":r[3],
                "avg_humidity":r[4],"avg_pressure":r[5],
                "avg_gas":r[6],"avg_iaq":r[7],"reading_count":r[8]
            }

        # environmental timeline (last 7d for nodes with sensors)
        env_timeline = {}
        for r in conn.execute(
            "SELECT node_id, date(timestamp,'unixepoch') d,"
            "ROUND(AVG(temperature),2),ROUND(AVG(relative_humidity),1),"
            "ROUND(AVG(barometric_pressure),2) "
            "FROM telemetry WHERE timestamp>? AND temperature IS NOT NULL "
            "GROUP BY node_id,d ORDER BY node_id,d",(T-604800,)
        ).fetchall():
            env_timeline.setdefault(r[0],[]).append({
                "date":r[1],"temp":r[2],"humidity":r[3],"pressure":r[4]
            })

        fw_dist = {}
        hw_dist = {}
        for r in conn.execute("SELECT firmware_version,hw_model FROM nodes WHERE firmware_version IS NOT NULL").fetchall():
            fw = r[0] or "unknown"
            hw = r[1] or "unknown"
            if fw not in ("None",""):
                fw_dist[fw] = fw_dist.get(fw,0) + 1
            if hw not in ("None","Unknown",""):
                hw_dist[hw] = hw_dist.get(hw,0) + 1

        uptime_data = {}
        for r in conn.execute(
            "SELECT node_id,MAX(uptime_seconds),ROUND(AVG(uptime_seconds),0) "
            "FROM telemetry WHERE timestamp<=? AND uptime_seconds IS NOT NULL AND uptime_seconds>0 GROUP BY node_id",(T,)
        ).fetchall():
            uptime_data[r[0]] = {"max_uptime":r[1],"avg_uptime":r[2]}

        total_pkts = sum(pkt_total.values()) or 1
        nodes_out  = []

        for row in nodes_raw:
            nid,ln,sn,role,is_local,lh,batt,volt,chan,air,snr,rssi,lat,lon = row
            name = ln or sn or nid
            rf   = rf_stats.get(nid,{})
            hs   = hop_stats.get(nid,{})
            ts   = tlm_stats.get(nid,{})
            pkts = pkt_total.get(nid,0)
            src  = pkt_src.get(nid,{})
            nb   = nb_all.get(nid,[])
            nb_c = len(nb)
            avg_nb_snr = round(sum(nb_snr_sum.get(nid,[])) / len(nb_snr_sum[nid]),1) if nb_snr_sum.get(nid) else None
            rc   = relay_cnt.get(nid,0)
            ra   = route_appearances.get(nid,0)
            age  = (now - lh) if lh else None

            eff_snr  = rf.get("avg_snr",  snr)
            eff_rssi = rf.get("avg_rssi", rssi)
            eff_batt = ts.get("batt_avg", batt)
            eff_chan  = ts.get("avg_chan",  chan)
            eff_air  = ts.get("avg_air",   air)

            rf_health    = round((_snr_score(eff_snr) * 0.6 + _rssi_score(eff_rssi) * 0.4), 3)
            centrality   = round(_centrality_score(rc, nb_c, pkts, total_pkts), 3)
            freshness    = round(_age_score(lh), 3)
            batt_risk    = _battery_risk(eff_batt, ts.get("batt_avg"), ts.get("batt_min"))
            chan_stress   = _chan_stress(eff_chan, eff_air)

            # SNR stability (lower stddev = more stable)
            snr_std = snr_stddev.get(nid)
            snr_stability = None
            if snr_std is not None:
                snr_stability = round(max(0.0, 1.0 - snr_std / 20.0), 3)

            # packet trend: +1 growing, 0 stable, -1 shrinking
            p24 = pkt_last24.get(nid, 0)
            pp24 = pkt_prior24.get(nid, 0)
            if pp24 == 0 and p24 > 0:
                pkt_trend = 1
                pkt_trend_pct = None
            elif pp24 == 0:
                pkt_trend = 0
                pkt_trend_pct = None
            else:
                ratio = p24 / pp24
                pkt_trend = 1 if ratio > 1.1 else (-1 if ratio < 0.9 else 0)
                pkt_trend_pct = round((ratio - 1.0) * 100, 1)

            # overall node score
            node_score = round(
                centrality * 0.35 +
                rf_health  * 0.30 +
                freshness  * 0.20 +
                (1.0 - (batt_risk or 0.5)) * 0.15,
                3
            )

            # role classification
            inferred_role = "observer"
            if rc >= 3 and nb_c >= 2:
                inferred_role = "relay"
            if rc >= 5 and ra >= 3 and nb_c >= 3:
                inferred_role = "backbone"
            if pkts == 0 and nb_c == 0 and (age is None or age > 86400):
                inferred_role = "isolated"
            stated_role = role.upper() if role and role.upper() not in ("NONE","UNKNOWN","") else None

            hop_variance = round(float(hs["std"]), 2) if hs.get("std") is not None else None

            # env data for this node
            env = env_data.get(nid,{})
            uptime = uptime_data.get(nid,{})

            nodes_out.append({
                "node_id":       nid,
                "name":          name,
                "short_name":    sn,
                "role":          stated_role,
                "inferred_role": inferred_role,
                "is_local":      bool(is_local),
                "lat":           lat, "lon": lon,
                "last_heard":    lh,
                "age_seconds":   round(age) if age is not None else None,

                "packet_count":  pkts,
                "pkt_last24h":   p24,
                "pkt_prior24h":  pp24,
                "pkt_trend":     pkt_trend,
                "pkt_trend_pct": pkt_trend_pct,
                "rf_packets":    src.get("RF",0),
                "mqtt_packets":  src.get("MQTT",0),
                "relay_count":   rc,
                "route_appearances": ra,
                "neighbor_count": nb_c,
                "avg_nb_snr":    avg_nb_snr,
                "tr_sent":       tr_sent.get(nid,0),
                "tr_recv":       tr_recv.get(nid,0),
                "msg_broadcast": msg_bcast.get(nid,0),
                "msg_dm":        msg_dm.get(nid,0),

                "avg_snr":       eff_snr,
                "avg_rssi":      eff_rssi,
                "min_snr":       rf.get("min_snr"),
                "max_snr":       rf.get("max_snr"),
                "snr_stddev":    snr_std,
                "snr_stability": snr_stability,
                "battery":       eff_batt,
                "batt_min":      ts.get("batt_min"),
                "voltage":       volt,
                "chan_util":     eff_chan,
                "air_util":      eff_air,
                "avg_hop":       hs.get("avg"),
                "hop_variance":  hop_variance,

                "score_rf":      rf_health,
                "score_central": centrality,
                "score_fresh":   freshness,
                "score_batt_risk": batt_risk,
                "score_chan_stress": chan_stress,
                "score_snr_stability": snr_stability,
                "node_score":    node_score,

                # environmental
                "avg_temp":      env.get("avg_temp"),
                "min_temp":      env.get("min_temp"),
                "max_temp":      env.get("max_temp"),
                "avg_humidity":  env.get("avg_humidity"),
                "avg_pressure":  env.get("avg_pressure"),
                "avg_iaq":       env.get("avg_iaq"),
                "env_readings":  env.get("reading_count",0),

                # uptime
                "max_uptime":    uptime.get("max_uptime"),
                "avg_uptime":    uptime.get("avg_uptime"),

                "neighbors":     nb,
            })

        nodes_out.sort(key=lambda x: x["node_score"], reverse=True)

        # freshness classification
        for n in nodes_out:
            age = n["age_seconds"]
            if age is None:
                n["freshness_class"] = "offline"
            elif age < 300:
                n["freshness_class"] = "live"
            elif age < 3600:
                n["freshness_class"] = "recent"
            elif age < 86400:
                n["freshness_class"] = "stale"
            else:
                n["freshness_class"] = "offline"

        live_nodes    = [n for n in nodes_out if n["freshness_class"] == "live"]
        recent_nodes  = [n for n in nodes_out if n["freshness_class"] == "recent"]
        stale_nodes   = [n for n in nodes_out if n["freshness_class"] == "stale"]
        offline_nodes = [n for n in nodes_out if n["freshness_class"] == "offline"]

        backbone_nodes    = [n for n in nodes_out if n["inferred_role"] == "backbone"]
        relay_nodes       = [n for n in nodes_out if n["inferred_role"] == "relay"]
        isolated_nodes    = [n for n in nodes_out if n["inferred_role"] == "isolated"]

        snr_values  = [n["avg_snr"]  for n in nodes_out if n["avg_snr"]  is not None]
        rssi_values = [n["avg_rssi"] for n in nodes_out if n["avg_rssi"] is not None]
        batt_values = [n["battery"]  for n in nodes_out if n["battery"]  is not None]
        nb_counts   = [n["neighbor_count"] for n in nodes_out if n["neighbor_count"] > 0]
        scores      = [n["node_score"] for n in nodes_out]

        # RF quality distribution
        rf_good = sum(1 for v in snr_values if v >= 5)
        rf_ok   = sum(1 for v in snr_values if 0 <= v < 5)
        rf_warn = sum(1 for v in snr_values if -5 <= v < 0)
        rf_bad  = sum(1 for v in snr_values if v < -5)

        # battery distribution
        batt_ok   = sum(1 for v in batt_values if v >= 60)
        batt_warn = sum(1 for v in batt_values if 30 <= v < 60)
        batt_crit = sum(1 for v in batt_values if v < 30)

        total_bcast = sum(n["msg_broadcast"] for n in nodes_out)
        total_dm    = sum(n["msg_dm"] for n in nodes_out)
        total_relay = sum(n["relay_count"] for n in nodes_out)

        n_total = len(nodes_out)
        avg_nb  = (sum(nb_counts)/len(nb_counts)) if nb_counts else 0
        density = round(min(1.0, avg_nb / max(n_total-1,1)), 3)

        # Score: how much relay traffic does each backbone/relay node carry?
        # If a node fails, what % of relay paths are broken?
        total_relay_appearances = sum(route_appearances.values()) or 1
        resilience_risks = []
        for n in nodes_out:
            if n["inferred_role"] in ("backbone","relay") and n["route_appearances"] > 0:
                impact = round(n["route_appearances"] / total_relay_appearances, 3)
                redundancy = n["neighbor_count"]  # more neighbors = more alternative paths
                risk_score = round(impact / max(redundancy, 1), 4)
                resilience_risks.append({
                    "node_id": n["node_id"],
                    "name": n["name"],
                    "role": n["inferred_role"],
                    "route_appearances": n["route_appearances"],
                    "impact_pct": round(impact * 100, 1),
                    "neighbor_count": redundancy,
                    "risk_score": risk_score,
                })
        resilience_risks.sort(key=lambda x: x["risk_score"], reverse=True)

        # overall mesh resilience: inverse of top-node concentration
        if resilience_risks:
            top_impact = resilience_risks[0]["impact_pct"]
            mesh_resilience = round(max(0.0, 1.0 - top_impact / 100.0), 3)
        else:
            mesh_resilience = 1.0

        top_links = sorted(
            [{"a": k[0], "b": k[1], "count": v} for k,v in link_usage.items()],
            key=lambda x: x["count"], reverse=True
        )[:20]

        # build node name lookup for links
        name_map = {n["node_id"]: n["name"] for n in nodes_out}
        for lk in top_links:
            lk["a_name"] = name_map.get(lk["a"], lk["a"])
            lk["b_name"] = name_map.get(lk["b"], lk["b"])

        snr_trend_slope = None
        if len(daily_rf) >= 3:
            snr_trend_slope = _linear_trend([d["avg_snr"] for d in daily_rf[-7:]])

        top5  = [{"node_id":n["node_id"],"name":n["name"],"score":n["node_score"],"role":n["inferred_role"]} for n in nodes_out[:5]]
        bottlenecks = sorted(
            [n for n in nodes_out if n["relay_count"] >= 3],
            key=lambda x: x["relay_count"] / max(x["neighbor_count"],1),
            reverse=True
        )[:5]

        sensor_nodes = [n for n in nodes_out if n["env_readings"] > 0]
        env_summary = {}
        if sensor_nodes:
            temps = [n["avg_temp"] for n in sensor_nodes if n["avg_temp"] is not None]
            humids = [n["avg_humidity"] for n in sensor_nodes if n["avg_humidity"] is not None]
            pressures = [n["avg_pressure"] for n in sensor_nodes if n["avg_pressure"] is not None]
            iaqs = [n["avg_iaq"] for n in sensor_nodes if n["avg_iaq"] is not None]
            env_summary = {
                "sensor_node_count": len(sensor_nodes),
                "avg_temp": round(sum(temps)/len(temps),2) if temps else None,
                "min_temp": min(temps) if temps else None,
                "max_temp": max(temps) if temps else None,
                "avg_humidity": round(sum(humids)/len(humids),1) if humids else None,
                "avg_pressure": round(sum(pressures)/len(pressures),2) if pressures else None,
                "avg_iaq": round(sum(iaqs)/len(iaqs),1) if iaqs else None,
            }

        return {
            "summary": {
                "total_nodes":    n_total,
                "live":           len(live_nodes),
                "recent":         len(recent_nodes),
                "stale":          len(stale_nodes),
                "offline":        len(offline_nodes),
                "backbone_count": len(backbone_nodes),
                "relay_count":    len(relay_nodes),
                "isolated_count": len(isolated_nodes),
                "mesh_density":   density,
                "mesh_resilience": mesh_resilience,
                "avg_neighbor":   round(avg_nb,1),
                "total_packets":  sum(pkt_total.values()),
                "total_bcast":    total_bcast,
                "total_dm":       total_dm,
                "total_relay":    total_relay,
                "avg_snr":        round(sum(snr_values)/len(snr_values),2) if snr_values else None,
                "avg_rssi":       round(sum(rssi_values)/len(rssi_values),1) if rssi_values else None,
                "p25_snr":        _pct(snr_values,0.25),
                "p75_snr":        _pct(snr_values,0.75),
                "avg_batt":       round(sum(batt_values)/len(batt_values),1) if batt_values else None,
                "median_score":   _pct(scores, 0.50),
                "snr_trend_slope": snr_trend_slope,
                "sensor_node_count": len(sensor_nodes),
            },
            "rf_distribution":   {"good":rf_good,"ok":rf_ok,"warn":rf_warn,"bad":rf_bad},
            "batt_distribution": {"ok":batt_ok,"warn":batt_warn,"crit":batt_crit},
            "hop_distribution":  {str(k):v for k,v in hop_dist.items()},
            "daily_traffic":     daily_traffic,
            "daily_rf":          daily_rf,
            "daily_active_nodes": daily_active_nodes,
            "hourly_dist":       hourly_dist,
            "top_nodes":         top5,
            "bottlenecks":       [{"node_id":b["node_id"],"name":b["name"],"relays":b["relay_count"],"neighbors":b["neighbor_count"]} for b in bottlenecks],
            "isolated_nodes":    [{"node_id":n["node_id"],"name":n["name"],"age":n["age_seconds"]} for n in isolated_nodes],
            "resilience_risks":  resilience_risks[:8],
            "top_links":         top_links,
            "fw_distribution":   fw_dist,
            "hw_distribution":   hw_dist,
            "env_summary":       env_summary,
            "env_timeline":      env_timeline,
            "nodes":             nodes_out,
        }

    return await asyncio.to_thread(_run)



@plugin_router.get("/status")
async def get_status():
    return {"state": "ready", "ready": True, "engine": "math"}


@plugin_router.get("/analytics")
async def get_analytics(slot_id: str = Query("node_0"), date_str: str = Query(None)):
    slot = _reg.get(slot_id) or _reg.get("node_0")
    if not slot:
        raise HTTPException(404, "Slot not found")

    target_ts = time.time()
    if date_str:
        try:
            dt        = datetime.strptime(date_str, "%Y-%m-%d")
            target_ts = dt.replace(hour=23,minute=59,second=59).timestamp()
        except ValueError:
            pass

    try:
        return await _compute_analytics(slot.db_manager, target_ts)
    except Exception as e:
        raise HTTPException(500, f"Analytics error: {e}")


@plugin_router.get("/date_range")
async def get_date_range(slot_id: str = Query("node_0")):
    slot = _reg.get(slot_id) or _reg.get("node_0")
    if not slot:
        return {"min_date": None, "max_date": None}
    def _get():
        conn = slot.db_manager._get_connection()
        row  = conn.execute("SELECT MIN(timestamp),MAX(timestamp) FROM packets").fetchone()
        if not row or not row[0]: return None, None
        return datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d"), datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d")
    mn, mx = await asyncio.to_thread(_get)
    return {"min_date": mn, "max_date": mx}


@plugin_router.get("/export_json", response_class=PlainTextResponse)
async def export_json(slot_id: str = Query("node_0")):
    """Export full analytics snapshot as JSON for external tools."""
    slot = _reg.get(slot_id) or _reg.get("node_0")
    if not slot:
        return PlainTextResponse('{"error":"Slot not found"}', media_type="application/json")
    data = await _compute_analytics(slot.db_manager, time.time())
    return PlainTextResponse(json.dumps(data, indent=2, default=str), media_type="application/json")


@plugin_router.get("/export_csv", response_class=PlainTextResponse)
async def export_csv(slot_id: str = Query("node_0")):
    """Export node table as CSV."""
    slot = _reg.get(slot_id) or _reg.get("node_0")
    if not slot:
        return PlainTextResponse("error,Slot not found", media_type="text/csv")
    data = await _compute_analytics(slot.db_manager, time.time())
    cols = [
        "node_id","name","short_name","inferred_role","role","is_local","freshness_class",
        "age_seconds","packet_count","pkt_last24h","pkt_trend_pct","relay_count",
        "route_appearances","neighbor_count","avg_nb_snr","tr_sent","tr_recv",
        "msg_broadcast","msg_dm","avg_snr","avg_rssi","min_snr","max_snr",
        "snr_stddev","snr_stability","battery","batt_min","voltage","chan_util","air_util",
        "avg_hop","hop_variance","node_score","score_rf","score_central","score_fresh",
        "score_batt_risk","score_chan_stress","lat","lon",
        "avg_temp","avg_humidity","avg_pressure","avg_iaq","max_uptime"
    ]
    lines = [",".join(cols)]
    for n in data.get("nodes",[]):
        def _v(k):
            v = n.get(k)
            if v is None: return ""
            if isinstance(v, float): return str(round(v,4))
            return str(v).replace(",","").replace('"',"")
        lines.append(",".join(_v(c) for c in cols))
    return PlainTextResponse("\n".join(lines), media_type="text/csv",
                             headers={"Content-Disposition":"attachment; filename=mesh_nodes.csv"})