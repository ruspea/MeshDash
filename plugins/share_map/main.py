"""
Share Map Plugin — v1.0.0
==========================
Create public embeddable widgets and share them anywhere.

Widget types
  map        — Live Leaflet map of node positions + trails + links
  stats      — Node count, online count, GPS count summary card
  nodelist   — Scrolling table of nodes with last-heard, battery, SNR
  signal     — SNR/RSSI bar chart (top N nodes)
  activity   — Recent packet / message activity feed
  nodecard   — Single node deep-dive card (name, battery, GPS, hops)

Every widget has:
  - a unique token (URL-safe, 16 chars)
  - configurable filters (name, age, role, GPS-only, online-only)
  - configurable display options (theme, refresh rate, title)
  - a public URL at /api/plugins/share_map/w/<token>
  - NO authentication on widget render endpoints

The plugin stores widget configs in SQLite. The widget renderer
returns a complete standalone HTML page — no external dependencies
other than Leaflet CDN (for map type).
"""

import asyncio
import contextlib
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import os

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

logger        = logging.getLogger("plugin.share_map")
plugin_router = APIRouter()

_DB_PATH = os.path.join(PLUGIN_DIR, "share_map.db")
_DB_LOCK = threading.Lock()

_node_registry: Dict[str, Any] = {}
_event_loop:    Optional[asyncio.AbstractEventLoop] = None

WIDGET_TYPES = ["map", "stats", "nodelist", "signal", "activity", "nodecard"]


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _db_init():
    with _DB_LOCK:
        with _db() as conn:
            conn.executescript("""
        CREATE TABLE IF NOT EXISTS widgets (
            token       TEXT PRIMARY KEY,
            slot_id     TEXT NOT NULL DEFAULT 'node_0',
            type        TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            config      TEXT NOT NULL DEFAULT '{}',
            created_at  REAL,
            last_viewed REAL,
            view_count  INTEGER DEFAULT 0,
            active      BOOLEAN DEFAULT 1
        );
        """)
            conn.commit()


def _db_conn():
    return sqlite3.connect(_DB_PATH, check_same_thread=False)


@contextlib.contextmanager
def _db():
    """Context manager: get a DB connection, auto-close on exit or exception."""
    conn = _db_conn()
    try:
        yield conn
    finally:
        conn.close()


def _get_widget(token: str) -> Optional[dict]:
    with _DB_LOCK:
        with _db() as conn:
            cur = conn.execute("SELECT * FROM widgets WHERE token=? AND active=1", (token,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
    if not row:
        return None
    d = dict(zip(cols, row))
    try:
        d["config"] = json.loads(d["config"])
    except Exception:
        d["config"] = {}
    return d


def _bump_view(token: str):
    with _DB_LOCK:
        with _db() as conn:
            conn.execute(
                "UPDATE widgets SET last_viewed=?, view_count=view_count+1 WHERE token=?",
                (time.time(), token)
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Node data helpers
# ---------------------------------------------------------------------------

def _get_nodes(slot_id: str) -> List[dict]:
    """Return all nodes for a slot as list of plain dicts."""
    slot = _node_registry.get(slot_id)
    if not slot:
        return []
    local_id = getattr(slot.meshtastic_data, "local_node_id", None)
    now = time.time()
    result = []
    for nid, nd in slot.meshtastic_data.nodes.items():
        u   = nd.get("user") or {}
        pos = nd.get("position") or {}
        dm  = nd.get("deviceMetrics") or {}
        lat = pos.get("latitude")  or nd.get("latitude")
        lon = pos.get("longitude") or nd.get("longitude")
        lh  = nd.get("lastHeard") or nd.get("last_heard") or 0
        result.append({
            "node_id":    nid,
            "long_name":  u.get("longName")  or nd.get("long_name")  or nid,
            "short_name": u.get("shortName") or nd.get("short_name") or nid[-4:],
            "role":       nd.get("role") or "",
            "hw_model":   u.get("hwModel") or nd.get("hw_model") or "",
            "lat":        lat,
            "lon":        lon,
            "has_gps":    bool(lat and lon and lat != 0),
            "last_heard": lh,
            "age_s":      (now - lh) if lh else None,
            "online":     bool(lh and (now - lh) < 3600),
            "battery":    dm.get("batteryLevel") or nd.get("battery_level"),
            "snr":        nd.get("snr") or nd.get("rx_snr"),
            "rssi":       nd.get("rssi") or nd.get("rx_rssi"),
            "hops":       nd.get("hopsAway") or nd.get("hops_away"),
            "is_local":   nid == local_id,
        })
    result.sort(key=lambda n: -(n["last_heard"] or 0))
    return result


def _filter_nodes(nodes: List[dict], cfg: dict) -> List[dict]:
    """Apply widget config filters to node list."""
    now    = time.time()
    search = (cfg.get("filter_name") or "").strip().lower()
    role   = (cfg.get("filter_role") or "").strip().upper()
    age_h  = float(cfg.get("filter_age_h") or 0)
    gps    = bool(cfg.get("filter_gps_only"))
    online = bool(cfg.get("filter_online_only"))
    max_n  = int(cfg.get("max_nodes") or 200)

    out = []
    for n in nodes:
        if search and search not in n["long_name"].lower() and search not in n["node_id"].lower():
            continue
        if role and role not in str(n["role"]).upper():
            continue
        if age_h > 0:
            if not n["last_heard"] or (now - n["last_heard"]) > age_h * 3600:
                continue
        if gps and not n["has_gps"]:
            continue
        if online and not n["online"]:
            continue
        out.append(n)
    return out[:max_n]


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def init_plugin(context: dict):
    global _node_registry, _event_loop
    _node_registry = context.get("node_registry") or {}
    _event_loop    = context.get("event_loop")
    _db_init()
    logger.info("Share Map v1.0.0 — ready")
    if _event_loop:
        asyncio.run_coroutine_threadsafe(_watchdog(context), _event_loop)


async def _watchdog(context):
    wd, pid = context.get("plugin_watchdog"), context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Management API (authenticated — normal plugin routes)
# ---------------------------------------------------------------------------

class WidgetCreateReq(BaseModel):
    slot_id: str = "node_0"
    type:    str = "map"
    title:   str = ""
    config:  dict = {}


@plugin_router.get("/widgets")
async def list_widgets(slot_id: str = ""):
    with _DB_LOCK:
        with _db() as conn:
            q = "SELECT * FROM widgets" + (" WHERE slot_id=? AND active=1" if slot_id else " WHERE active=1") + " ORDER BY created_at DESC"
            cur = conn.execute(q, (slot_id,) if slot_id else ())
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
    result = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["config"] = json.loads(d["config"])
        except Exception:
            d["config"] = {}
        result.append(d)
    return {"widgets": result}


@plugin_router.post("/widgets")
async def create_widget(r: WidgetCreateReq, request: Request):
    if r.type not in WIDGET_TYPES:
        raise HTTPException(400, f"Unknown widget type '{r.type}'. Must be one of: {WIDGET_TYPES}")
    token = secrets.token_urlsafe(12)
    now   = time.time()
    title = r.title or f"{r.type.title()} Widget"
    base_url = str(request.base_url).rstrip("/")
    with _DB_LOCK:
        with _db() as conn:
            conn.execute(
                "INSERT INTO widgets (token,slot_id,type,title,config,created_at,view_count,active) "
                "VALUES (?,?,?,?,?,?,0,1)",
                (token, r.slot_id, r.type, title, json.dumps(r.config), now)
            )
            conn.commit()
    widget_url = f"{base_url}/api/plugins/share_map/w/{token}"
    iframe_code = (
        f'<iframe src="{widget_url}" '
        f'width="800" height="500" frameborder="0" '
        f'style="border:1px solid #1e3048;border-radius:6px;" '
        f'allowfullscreen></iframe>'
    )
    return {
        "token":      token,
        "url":        widget_url,
        "iframe":     iframe_code,
        "type":       r.type,
        "title":      title,
    }


@plugin_router.patch("/widgets/{token}")
async def update_widget(token: str, r: WidgetCreateReq):
    w = _get_widget(token)
    if not w:
        raise HTTPException(404, "Widget not found")
    with _DB_LOCK:
        with _db() as conn:
            conn.execute(
                "UPDATE widgets SET title=?, config=?, type=? WHERE token=?",
                (r.title or w["title"], json.dumps(r.config), r.type or w["type"], token)
            )
            conn.commit()
    return {"status": "updated"}


@plugin_router.delete("/widgets/{token}")
async def delete_widget(token: str):
    with _DB_LOCK:
        with _db() as conn:
            conn.execute("UPDATE widgets SET active=0 WHERE token=?", (token,))
            conn.commit()
    return {"status": "deleted"}


@plugin_router.get("/preview/{slot_id}")
async def preview_data(slot_id: str, type: str = "stats", config: str = "{}"):
    """Return live data for widget preview. Used by the builder."""
    try:
        cfg = json.loads(config)
    except Exception:
        cfg = {}
    nodes = _get_nodes(slot_id)
    filtered = _filter_nodes(nodes, cfg)
    return _build_data_payload(type, filtered, cfg)


def _build_data_payload(wtype: str, nodes: List[dict], cfg: dict) -> dict:
    now = time.time()
    total   = len(nodes)
    online  = sum(1 for n in nodes if n["online"])
    gps_ct  = sum(1 for n in nodes if n["has_gps"])

    if wtype == "stats":
        return {
            "type": "stats",
            "total": total, "online": online, "gps": gps_ct,
            "offline": total - online,
            "ts": int(now),
        }

    if wtype == "map":
        return {
            "type": "map",
            "nodes": [n for n in nodes if n["has_gps"]],
            "ts": int(now),
        }

    if wtype == "nodelist":
        return {
            "type": "nodelist",
            "nodes": nodes,
            "ts": int(now),
        }

    if wtype == "signal":
        sig_nodes = [n for n in nodes if n.get("snr") is not None]
        sig_nodes.sort(key=lambda n: -(n["snr"] or -999))
        return {
            "type": "signal",
            "nodes": sig_nodes[:int(cfg.get("max_nodes") or 20)],
            "ts": int(now),
        }

    if wtype == "activity":
        # Return most recently heard nodes as proxy for "activity"
        recent = sorted(nodes, key=lambda n: -(n["last_heard"] or 0))[:int(cfg.get("max_nodes") or 15)]
        return {
            "type": "activity",
            "nodes": recent,
            "ts": int(now),
        }

    if wtype == "nodecard":
        target_id = cfg.get("node_id") or ""
        target = next((n for n in nodes if n["node_id"] == target_id), None)
        return {
            "type": "nodecard",
            "node": target,
            "ts": int(now),
        }

    return {"type": wtype, "nodes": nodes, "ts": int(now)}


# ---------------------------------------------------------------------------
# PUBLIC widget render — NO AUTH
# These routes serve complete standalone HTML pages.
# ---------------------------------------------------------------------------

@plugin_router.get("/w/{token}", response_class=HTMLResponse, include_in_schema=False)
async def render_widget(token: str, request: Request):
    """
    Public endpoint — serves a complete standalone HTML page.
    No authentication. Read-only.
    """
    w = _get_widget(token)
    if not w:
        return HTMLResponse(_error_page("Widget not found or has been deleted."), status_code=404)

    _bump_view(token)

    nodes   = _get_nodes(w["slot_id"])
    cfg     = w["config"]
    filtered = _filter_nodes(nodes, cfg)
    data    = _build_data_payload(w["type"], filtered, cfg)

    theme   = cfg.get("theme", "dark")
    title   = w["title"]
    refresh = int(cfg.get("refresh_s") or 30)

    base_url = str(request.base_url).rstrip("/")
    data_url = f"{base_url}/api/plugins/share_map/w/{token}/data"

    html = _render_widget_html(w["type"], data, cfg, title, theme, refresh, data_url, token)
    return HTMLResponse(html)


@plugin_router.get("/w/{token}/data", include_in_schema=False)
async def widget_data_api(token: str):
    """
    Public data endpoint — returns JSON for widget auto-refresh.
    No authentication.
    """
    w = _get_widget(token)
    if not w:
        raise HTTPException(404, "Widget not found")
    nodes    = _get_nodes(w["slot_id"])
    filtered = _filter_nodes(nodes, w["config"])
    return _build_data_payload(w["type"], filtered, w["config"])


# ---------------------------------------------------------------------------
# Widget HTML renderers
# ---------------------------------------------------------------------------

_DARK_VARS = """
    --bg:#060b12; --bg1:#0b1320; --bg2:#0d1a2d;
    --txt:#c8d8e8; --txt2:#8aa0b8; --txt3:#4a6a88;
    --acc:#00c8f5; --ok:#00e87a; --warn:#ffa826; --err:#ff3050;
    --bd:#162338; --bd2:#1e3048; --mono:'Fira Code',monospace;
"""
_LIGHT_VARS = """
    --bg:#f4f7fb; --bg1:#ffffff; --bg2:#edf1f7;
    --txt:#1a2a3a; --txt2:#4a6a88; --txt3:#8aa0b8;
    --acc:#007bb5; --ok:#00874a; --warn:#d47a00; --err:#c0392b;
    --bd:#c8d8e8; --bd2:#b0c4d8; --mono:'Fira Code',monospace;
"""


def _base_html(title: str, theme: str, body: str, head_extra: str = "", refresh_s: int = 0,
               data_url: str = "", token: str = "") -> str:
    vars_ = _DARK_VARS if theme == "dark" else _LIGHT_VARS
    bg    = "#060b12" if theme == "dark" else "#f4f7fb"
    refresh_js = ""
    if refresh_s > 0 and data_url:
        refresh_js = f"""
<script>
(function(){{
  const DATA_URL = '{data_url}';
  const INTERVAL = {refresh_s * 1000};
  function _refresh(){{
    fetch(DATA_URL).then(r=>r.json()).then(d=>window._widgetUpdate?.(d)).catch(()=>{{}});
  }}
  setTimeout(function loop(){{ _refresh(); setTimeout(loop, INTERVAL); }}, INTERVAL);
}})();
</script>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
{head_extra}
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
:root{{{vars_}}}
body{{background:var(--bg);color:var(--txt);font-family:var(--mono);min-height:100vh;overflow:hidden;}}
.wg-wrap{{display:flex;flex-direction:column;height:100vh;overflow:hidden;}}
.wg-header{{
  display:flex;align-items:center;gap:8px;
  padding:7px 12px;
  background:var(--bg1);
  border-bottom:1px solid var(--bd2);
  flex-shrink:0;
}}
.wg-title{{font-size:11px;font-weight:800;letter-spacing:1px;color:var(--acc);}}
.wg-badge{{
  font-size:8px;padding:2px 6px;border-radius:2px;letter-spacing:.5px;
  background:rgba(0,200,245,.1);border:1px solid var(--acc);color:var(--acc);
}}
.wg-live{{
  display:flex;align-items:center;gap:4px;
  font-size:8px;color:var(--ok);letter-spacing:.5px;margin-left:auto;
}}
.wg-dot{{
  width:6px;height:6px;border-radius:50%;
  background:var(--ok);animation:blink 2s infinite;
}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.wg-body{{flex:1;overflow:hidden;position:relative;}}
.wg-footer{{
  padding:4px 12px;background:var(--bg1);border-top:1px solid var(--bd);
  font-size:8px;color:var(--txt3);display:flex;align-items:center;justify-content:space-between;
  flex-shrink:0;
}}
</style>
</head>
<body>
{body}
{refresh_js}
</body>
</html>"""


def _esc(s: str) -> str:
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")


def _error_page(msg: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Widget Error</title>
<style>body{{background:#060b12;color:#ff3050;font-family:monospace;display:flex;align-items:center;
justify-content:center;height:100vh;font-size:12px;letter-spacing:1px;}}</style>
</head><body>⚠ {_esc(msg)}</body></html>"""


def _fmt_time(ts) -> str:
    if not ts:
        return "—"
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return "—"


def _fmt_ago(age_s) -> str:
    if age_s is None:
        return "—"
    age_s = int(age_s)
    if age_s < 60:
        return f"{age_s}s ago"
    if age_s < 3600:
        return f"{age_s//60}m ago"
    if age_s < 86400:
        return f"{age_s//3600}h ago"
    return f"{age_s//86400}d ago"


def _bat_color(v) -> str:
    if v is None:
        return "var(--txt3)"
    if v < 20:
        return "var(--err)"
    if v < 40:
        return "var(--warn)"
    return "var(--ok)"


def _snr_color(v) -> str:
    if v is None:
        return "var(--txt3)"
    if v < -15:
        return "var(--err)"
    if v < -5:
        return "var(--warn)"
    return "var(--ok)"


# ── MAP widget ─────────────────────────────────────────────────────────────

def _render_widget_html(wtype, data, cfg, title, theme, refresh_s, data_url, token):
    if wtype == "map":
        return _render_map(data, cfg, title, theme, refresh_s, data_url, token)
    if wtype == "stats":
        return _render_stats(data, cfg, title, theme, refresh_s, data_url)
    if wtype == "nodelist":
        return _render_nodelist(data, cfg, title, theme, refresh_s, data_url)
    if wtype == "signal":
        return _render_signal(data, cfg, title, theme, refresh_s, data_url)
    if wtype == "activity":
        return _render_activity(data, cfg, title, theme, refresh_s, data_url)
    if wtype == "nodecard":
        return _render_nodecard(data, cfg, title, theme, refresh_s, data_url)
    return _error_page(f"Unknown widget type: {wtype}")


def _render_map(data, cfg, title, theme, refresh_s, data_url, token):
    nodes     = data.get("nodes", [])
    map_style = cfg.get("map_style", "dark")
    show_trails = bool(cfg.get("show_trails", False))
    show_links  = bool(cfg.get("show_links",  False))
    zoom      = int(cfg.get("zoom", 7))

    tile_url  = {
        "dark":      "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "osm":       "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    }.get(map_style, "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png")

    nodes_json = json.dumps(nodes)
    cfg_json   = json.dumps({"zoom": zoom, "show_trails": show_trails,
                              "show_links": show_links, "tile_url": tile_url,
                              "theme": theme})
    bg_css = "#060b12" if theme == "dark" else "#f4f7fb"

    head_extra = '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
    body = f"""
<div class="wg-wrap">
  <div class="wg-header">
    <span class="wg-title">◫ {_esc(title)}</span>
    <span class="wg-badge">LIVE MAP</span>
    <div class="wg-live"><div class="wg-dot"></div>LIVE</div>
  </div>
  <div class="wg-body">
    <div id="wmap" style="width:100%;height:100%;background:{bg_css};"></div>
  </div>
  <div class="wg-footer">
    <span id="map-count">— nodes</span>
    <span>MeshDash · Share Map</span>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){{
  const NODES  = {nodes_json};
  const CFG    = {cfg_json};
  const DATA_URL = '{data_url}';
  const REFRESH = {refresh_s * 1000 if refresh_s > 0 else 30000};

  const PALETTE = ['#00c8f5','#00e87a','#ffa826','#ff3050','#b060ff',
                   '#4363d8','#46f0f0','#f032e6','#bcf60c'];
  const colors  = {{}};
  let colorIdx  = 0;
  let map, markers = {{}};

  function _color(nid) {{
    if (!colors[nid]) colors[nid] = PALETTE[colorIdx++ % PALETTE.length];
    return colors[nid];
  }}

  function _icon(n, col) {{
    const online = n.online;
    const pulse  = online ? `<div style="position:absolute;inset:-4px;border-radius:50%;border:2px solid ${{col}};opacity:.3;animation:pulse 2s infinite;pointer-events:none;"></div>` : '';
    const role   = (n.role||'').toUpperCase();
    const sym    = role.includes('ROUTER') ? '⬡' : n.long_name?.toLowerCase().includes('base') ? '⌂' : '◈';
    return L.divIcon({{
      html: `<div style="position:relative;width:30px;height:30px;">${{pulse}}<div style="background:${{col}};width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:2px solid rgba(0,0,0,.5);color:#0a0f18;font-size:13px;font-weight:900;box-shadow:0 0 10px ${{col}}66;">${{sym}}</div></div>`,
      className:'', iconSize:[30,30], iconAnchor:[15,15]
    }});
  }}

  function _drawNodes(nodes) {{
    const seen = new Set();
    nodes.forEach(n => {{
      if (!n.has_gps) return;
      seen.add(n.node_id);
      const col = _color(n.node_id);
      const tip = `<b style="color:${{col}}">${{n.long_name}}</b><br><span style="font-size:9px;color:#8aa0b8;">${{n.node_id}}</span><br>` +
        (n.battery != null ? `🔋 ${{n.battery}}%&nbsp; ` : '') +
        (n.snr != null ? `SNR ${{n.snr.toFixed(1)}}dB&nbsp; ` : '') +
        `<br><span style="font-size:8px;color:#4a6a88;">${{_ago(n.age_s)}}</span>`;
      if (markers[n.node_id]) {{
        markers[n.node_id].setLatLng([n.lat, n.lon]);
        markers[n.node_id].setIcon(_icon(n, col));
        markers[n.node_id].getPopup()?.setContent(tip);
      }} else {{
        const mk = L.marker([n.lat, n.lon], {{icon: _icon(n, col)}})
          .bindPopup(`<div style="font-family:monospace;font-size:10px;line-height:1.6;">${{tip}}</div>`, {{maxWidth:220}})
          .addTo(map);
        markers[n.node_id] = mk;
      }}
    }});
    // Remove gone nodes
    Object.keys(markers).forEach(nid => {{
      if (!seen.has(nid)) {{ map.removeLayer(markers[nid]); delete markers[nid]; }}
    }});
    const cnt = document.getElementById('map-count');
    if (cnt) cnt.textContent = `${{seen.size}} node${{seen.size!==1?'s':''}} plotted`;
  }}

  function _ago(s) {{
    if (s==null) return '—';
    s = Math.round(s);
    if (s<60) return s+'s ago';
    if (s<3600) return Math.floor(s/60)+'m ago';
    if (s<86400) return Math.floor(s/3600)+'h ago';
    return Math.floor(s/86400)+'d ago';
  }}

  // Init map
  const isDark = CFG.theme === 'dark';
  map = L.map('wmap', {{zoomControl:true, attributionControl:false}})
    .setView([20, 0], CFG.zoom);
  L.tileLayer(CFG.tile_url, {{subdomains:'abcd',maxZoom:19}}).addTo(map);

  // Inject Leaflet CSS overrides
  const s = document.createElement('style');
  s.textContent = `
    .leaflet-popup-content-wrapper{{background:var(--bg1);color:var(--txt);border:1px solid var(--bd2);font-family:var(--mono);border-radius:4px;box-shadow:0 8px 24px rgba(0,0,0,.7);}}
    .leaflet-popup-tip{{background:var(--bg1);}}
    .leaflet-popup-close-button{{color:var(--txt3)!important;}}
    .leaflet-container{{background:var(--bg2)!important;}}
    @keyframes pulse{{0%,100%{{transform:scale(1);opacity:.3}}50%{{transform:scale(1.6);opacity:.08}}}}
  `;
  document.head.appendChild(s);

  _drawNodes(NODES);

  // Fit bounds
  const coords = NODES.filter(n=>n.has_gps).map(n=>[n.lat,n.lon]);
  if (coords.length > 0) {{
    try {{ map.fitBounds(coords, {{padding:[30,30],maxZoom:16}}); }} catch(e) {{}}
  }}

  // Auto-refresh
  window._widgetUpdate = function(d) {{
    if (d && d.nodes) _drawNodes(d.nodes);
  }};
  setInterval(() => {{
    fetch(DATA_URL).then(r=>r.json()).then(d=>window._widgetUpdate(d)).catch(()=>{{}});
  }}, REFRESH);
}})();
</script>"""
    return _base_html(title, theme, body, head_extra, 0, data_url)


# ── STATS widget ───────────────────────────────────────────────────────────

def _render_stats(data, cfg, title, theme, refresh_s, data_url):
    total   = data.get("total",   0)
    online  = data.get("online",  0)
    gps     = data.get("gps",     0)
    offline = data.get("offline", 0)
    pct     = round(100 * online / total) if total else 0

    body = f"""
<div class="wg-wrap">
  <div class="wg-header">
    <span class="wg-title">◈ {_esc(title)}</span>
    <span class="wg-badge">NETWORK STATS</span>
    <div class="wg-live"><div class="wg-dot"></div>LIVE</div>
  </div>
  <div class="wg-body" style="display:flex;align-items:center;justify-content:center;padding:20px;">
    <div id="stats-inner" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;width:100%;max-width:420px;">
      {_stat_tile("TOTAL NODES",   total,   "var(--acc)", "fa-broadcast-tower")}
      {_stat_tile("ONLINE",        online,  "var(--ok)",  "fa-circle", f"{pct}% online")}
      {_stat_tile("WITH GPS",      gps,     "var(--warn)", "fa-location-dot")}
      {_stat_tile("OFFLINE",       offline, "var(--txt3)", "fa-circle-minus")}
    </div>
  </div>
  <div class="wg-footer">
    <span id="stats-ts">—</span>
    <span>MeshDash · Share Map</span>
  </div>
</div>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<script>
document.getElementById('stats-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
window._widgetUpdate = function(d) {{
  if (!d) return;
  const g = id => document.getElementById(id);
  const total = d.total||0, online = d.online||0, gps = d.gps||0;
  const pct = total ? Math.round(100*online/total) : 0;
  if(g('sv-total'))   g('sv-total').textContent   = total;
  if(g('sv-online'))  g('sv-online').textContent  = online;
  if(g('sv-gps'))     g('sv-gps').textContent     = gps;
  if(g('sv-offline')) g('sv-offline').textContent = d.offline||0;
  if(g('sv-sub-online')) g('sv-sub-online').textContent = pct + '% online';
  if(g('stats-ts')) g('stats-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
}};
</script>"""
    return _base_html(title, theme, body, "", refresh_s, data_url)


def _stat_tile(label, value, color, icon, sub=""):
    sub_html = f'<div style="font-size:9px;color:var(--txt3);margin-top:3px;">{sub}</div>' if sub else ""
    return f"""
<div style="background:var(--bg2);border:1px solid var(--bd2);border-radius:6px;
  padding:18px 14px;text-align:center;border-top:3px solid {color};">
  <div style="font-size:11px;color:var(--txt3);letter-spacing:1px;margin-bottom:8px;">{label}</div>
  <div id="sv-{label.lower().replace(' ','')}" style="font-size:36px;font-weight:900;color:{color};line-height:1;">{value}</div>
  {sub_html}
</div>"""


# ── NODELIST widget ────────────────────────────────────────────────────────

def _render_nodelist(data, cfg, title, theme, refresh_s, data_url):
    nodes   = data.get("nodes", [])
    rows_html = "".join(_nodelist_row(n) for n in nodes) or \
        '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--txt3);">NO NODES</td></tr>'

    body = f"""
<div class="wg-wrap">
  <div class="wg-header">
    <span class="wg-title">≡ {_esc(title)}</span>
    <span class="wg-badge">NODE LIST</span>
    <span id="nl-count" style="font-size:9px;color:var(--txt3);margin-left:6px;">{len(nodes)} nodes</span>
    <div class="wg-live"><div class="wg-dot"></div>LIVE</div>
  </div>
  <div class="wg-body" style="overflow-y:auto;">
    <table id="nl-table" style="width:100%;border-collapse:collapse;font-size:10px;">
      <thead>
        <tr style="background:var(--bg2);border-bottom:1px solid var(--bd2);position:sticky;top:0;">
          <th style="padding:7px 10px;text-align:left;color:var(--txt3);letter-spacing:.5px;font-weight:600;">NODE</th>
          <th style="padding:7px 8px;text-align:center;color:var(--txt3);letter-spacing:.5px;font-weight:600;">STATUS</th>
          <th style="padding:7px 8px;text-align:center;color:var(--txt3);letter-spacing:.5px;font-weight:600;">BATTERY</th>
          <th style="padding:7px 8px;text-align:center;color:var(--txt3);letter-spacing:.5px;font-weight:600;">SNR</th>
          <th style="padding:7px 10px;text-align:right;color:var(--txt3);letter-spacing:.5px;font-weight:600;">LAST HEARD</th>
        </tr>
      </thead>
      <tbody id="nl-body">{rows_html}</tbody>
    </table>
  </div>
  <div class="wg-footer">
    <span id="nl-ts">—</span>
    <span>MeshDash · Share Map</span>
  </div>
</div>
<style>
#nl-table tbody tr{{border-bottom:1px solid var(--bd);transition:background .1s;}}
#nl-table tbody tr:hover{{background:rgba(0,200,245,.04);}}
</style>
<script>
document.getElementById('nl-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
function _nlRow(n) {{
  const online  = n.online;
  const dot     = `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${{online?'var(--ok)':'var(--bd2)'}};${{online?'box-shadow:0 0 4px var(--ok)':''}};"></span>`;
  const bat     = n.battery!=null ? `<span style="color:${{n.battery<20?'var(--err)':n.battery<40?'var(--warn)':'var(--ok)'}}">${{n.battery}}%</span>` : '—';
  const snr     = n.snr!=null ? `<span style="color:${{n.snr<-15?'var(--err)':n.snr<-5?'var(--warn)':'var(--ok)'}}">${{n.snr.toFixed(1)}}dB</span>` : '—';
  const ago_s   = n.age_s;
  const ago     = ago_s==null?'—':ago_s<60?ago_s+'s':ago_s<3600?Math.floor(ago_s/60)+'m':Math.floor(ago_s/3600)+'h';
  return `<tr>
    <td style="padding:7px 10px;"><div style="font-weight:700;color:var(--txt)">${{n.long_name}}</div><div style="font-size:8px;color:var(--txt3)">${{n.node_id}}</div></td>
    <td style="padding:7px 8px;text-align:center;">${{dot}} <span style="font-size:9px;color:${{online?'var(--ok)':'var(--txt3)'}}">${{online?'ONLINE':'OFFLINE'}}</span></td>
    <td style="padding:7px 8px;text-align:center;">${{bat}}</td>
    <td style="padding:7px 8px;text-align:center;">${{snr}}</td>
    <td style="padding:7px 10px;text-align:right;color:var(--txt3)">${{ago}} ago</td>
  </tr>`;
}}
window._widgetUpdate = function(d) {{
  if (!d || !d.nodes) return;
  const tb = document.getElementById('nl-body');
  if (tb) tb.innerHTML = d.nodes.length ? d.nodes.map(_nlRow).join('') : '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--txt3)">NO NODES</td></tr>';
  const cnt = document.getElementById('nl-count');
  if (cnt) cnt.textContent = d.nodes.length + ' nodes';
  const ts = document.getElementById('nl-ts');
  if (ts) ts.textContent = 'Updated ' + new Date().toLocaleTimeString();
}};
</script>"""
    return _base_html(title, theme, body, "", refresh_s, data_url)


def _nodelist_row(n: dict) -> str:
    online   = n.get("online", False)
    dot_col  = "var(--ok)" if online else "var(--bd2)"
    glow     = "box-shadow:0 0 4px var(--ok);" if online else ""
    bat      = n.get("battery")
    snr      = n.get("snr")
    bat_col  = _bat_color(bat)
    snr_col  = _snr_color(snr)
    bat_str  = f'<span style="color:{bat_col}">{bat}%</span>' if bat is not None else "—"
    snr_str  = f'<span style="color:{snr_col}">{snr:.1f}dB</span>' if snr is not None else "—"
    ago      = _fmt_ago(n.get("age_s"))
    status   = "ONLINE" if online else "OFFLINE"
    st_col   = "var(--ok)" if online else "var(--txt3)"
    return f"""<tr>
      <td style="padding:7px 10px;">
        <div style="font-weight:700;color:var(--txt);">{_esc(n['long_name'])}</div>
        <div style="font-size:8px;color:var(--txt3);">{_esc(n['node_id'])}</div>
      </td>
      <td style="padding:7px 8px;text-align:center;">
        <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{dot_col};{glow}"></span>
        <span style="font-size:9px;color:{st_col};">&nbsp;{status}</span>
      </td>
      <td style="padding:7px 8px;text-align:center;">{bat_str}</td>
      <td style="padding:7px 8px;text-align:center;">{snr_str}</td>
      <td style="padding:7px 10px;text-align:right;color:var(--txt3);">{_esc(ago)}</td>
    </tr>"""


# ── SIGNAL widget ──────────────────────────────────────────────────────────

def _render_signal(data, cfg, title, theme, refresh_s, data_url):
    nodes = data.get("nodes", [])[:20]
    bars  = "".join(_signal_bar(n) for n in nodes) or \
        '<div style="color:var(--txt3);text-align:center;padding:30px;font-size:10px;">NO SIGNAL DATA</div>'

    body = f"""
<div class="wg-wrap">
  <div class="wg-header">
    <span class="wg-title">▊ {_esc(title)}</span>
    <span class="wg-badge">SIGNAL</span>
    <div class="wg-live"><div class="wg-dot"></div>LIVE</div>
  </div>
  <div class="wg-body" style="overflow-y:auto;padding:10px 14px;" id="sig-body">
    {bars}
  </div>
  <div class="wg-footer">
    <span id="sig-ts">—</span>
    <span>MeshDash · Share Map</span>
  </div>
</div>
<script>
document.getElementById('sig-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
function _sigBar(n) {{
  const snr = n.snr;
  if (snr == null) return '';
  const col = snr < -15 ? 'var(--err)' : snr < -5 ? 'var(--warn)' : 'var(--ok)';
  const pct = Math.min(100, Math.max(0, (snr + 20) * 3.3));
  return `<div style="margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;font-size:9px;margin-bottom:3px;">
      <span style="color:var(--txt);font-weight:700;">${{n.long_name}}</span>
      <span style="color:${{col}};font-weight:bold;">${{snr.toFixed(1)}} dB</span>
    </div>
    <div style="background:var(--bd);border-radius:2px;height:6px;overflow:hidden;">
      <div style="height:100%;width:${{pct}}%;background:${{col}};border-radius:2px;transition:width .4s;"></div>
    </div>
  </div>`;
}}
window._widgetUpdate = function(d) {{
  if (!d || !d.nodes) return;
  const b = document.getElementById('sig-body');
  if (b) b.innerHTML = d.nodes.length ? d.nodes.map(_sigBar).join('') : '<div style="color:var(--txt3);text-align:center;padding:30px;font-size:10px;">NO SIGNAL DATA</div>';
  const ts = document.getElementById('sig-ts');
  if (ts) ts.textContent = 'Updated ' + new Date().toLocaleTimeString();
}};
</script>"""
    return _base_html(title, theme, body, "", refresh_s, data_url)


def _signal_bar(n: dict) -> str:
    snr = n.get("snr")
    if snr is None:
        return ""
    col  = _snr_color(snr)
    pct  = max(0, min(100, (snr + 20) * 3.3))
    return f"""
<div style="margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;font-size:9px;margin-bottom:3px;">
    <span style="color:var(--txt);font-weight:700;">{_esc(n['long_name'])}</span>
    <span style="color:{col};font-weight:bold;">{snr:.1f} dB</span>
  </div>
  <div style="background:var(--bd);border-radius:2px;height:6px;overflow:hidden;">
    <div style="height:100%;width:{pct:.1f}%;background:{col};border-radius:2px;"></div>
  </div>
</div>"""


# ── ACTIVITY widget ────────────────────────────────────────────────────────

def _render_activity(data, cfg, title, theme, refresh_s, data_url):
    nodes = data.get("nodes", [])
    rows  = "".join(_activity_row(n) for n in nodes) or \
        '<div style="color:var(--txt3);text-align:center;padding:30px;font-size:10px;">NO ACTIVITY</div>'

    body = f"""
<div class="wg-wrap">
  <div class="wg-header">
    <span class="wg-title">⚡ {_esc(title)}</span>
    <span class="wg-badge">ACTIVITY FEED</span>
    <div class="wg-live"><div class="wg-dot"></div>LIVE</div>
  </div>
  <div class="wg-body" style="overflow-y:auto;padding:8px 0;" id="act-body">
    {rows}
  </div>
  <div class="wg-footer">
    <span id="act-ts">—</span>
    <span>MeshDash · Share Map</span>
  </div>
</div>
<script>
document.getElementById('act-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
function _actRow(n) {{
  const online = n.online;
  const col    = online ? 'var(--ok)' : 'var(--txt3)';
  const ago_s  = n.age_s;
  const ago    = ago_s==null?'—':ago_s<60?ago_s+'s':ago_s<3600?Math.floor(ago_s/60)+'m':Math.floor(ago_s/3600)+'h';
  const role   = (n.role||'CLIENT').replace(/_/g,' ');
  const bat    = n.battery!=null ? ` 🔋${{n.battery}}%` : '';
  return `<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--bd);">
    <div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${{col}};${{online?'box-shadow:0 0 5px var(--ok)':''}}"></div>
    <div style="flex:1;overflow:hidden;">
      <div style="font-weight:700;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${{n.long_name}}</div>
      <div style="font-size:8px;color:var(--txt3);">${{role}}${{bat}}</div>
    </div>
    <div style="font-size:9px;color:${{col}};white-space:nowrap;flex-shrink:0;">${{ago}} ago</div>
  </div>`;
}}
window._widgetUpdate = function(d) {{
  if (!d || !d.nodes) return;
  const b = document.getElementById('act-body');
  if (b) b.innerHTML = d.nodes.length ? d.nodes.map(_actRow).join('') : '<div style="color:var(--txt3);text-align:center;padding:30px;font-size:10px;">NO ACTIVITY</div>';
  const ts = document.getElementById('act-ts');
  if (ts) ts.textContent = 'Updated ' + new Date().toLocaleTimeString();
}};
</script>"""
    return _base_html(title, theme, body, "", refresh_s, data_url)


def _activity_row(n: dict) -> str:
    online  = n.get("online", False)
    col     = "var(--ok)" if online else "var(--txt3)"
    glow    = "box-shadow:0 0 5px var(--ok);" if online else ""
    ago     = _fmt_ago(n.get("age_s"))
    role    = str(n.get("role") or "CLIENT").replace("_", " ")
    bat_str = f" 🔋{n['battery']}%" if n.get("battery") is not None else ""
    return f"""
<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--bd);">
  <div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:{col};{glow}"></div>
  <div style="flex:1;overflow:hidden;">
    <div style="font-weight:700;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_esc(n['long_name'])}</div>
    <div style="font-size:8px;color:var(--txt3);">{_esc(role)}{bat_str}</div>
  </div>
  <div style="font-size:9px;color:{col};white-space:nowrap;flex-shrink:0;">{_esc(ago)}</div>
</div>"""


# ── NODECARD widget ────────────────────────────────────────────────────────

def _render_nodecard(data, cfg, title, theme, refresh_s, data_url):
    n = data.get("node")
    if not n:
        body = f"""
<div class="wg-wrap">
  <div class="wg-header"><span class="wg-title">◈ {_esc(title)}</span><span class="wg-badge">NODE CARD</span></div>
  <div class="wg-body" style="display:flex;align-items:center;justify-content:center;">
    <div style="color:var(--txt3);font-size:10px;letter-spacing:1px;">NODE NOT FOUND</div>
  </div>
</div>"""
        return _base_html(title, theme, body, "", 0, "")

    online  = n.get("online", False)
    col     = "var(--ok)" if online else "var(--txt3)"
    glow    = f"box-shadow:0 0 12px {col}44;" if online else ""
    bat     = n.get("battery")
    snr     = n.get("snr")
    role    = str(n.get("role") or "CLIENT").replace("_", " ")
    hw      = n.get("hw_model") or "—"
    hops    = n.get("hops")

    def _kv(k, v, vc="var(--acc)"):
        return f'<div style="background:var(--bg2);border:1px solid var(--bd);border-radius:4px;padding:10px;text-align:center;"><div style="font-size:8px;color:var(--txt3);letter-spacing:.5px;margin-bottom:4px;">{k}</div><div style="font-size:16px;font-weight:900;color:{vc};">{v}</div></div>'

    body = f"""
<div class="wg-wrap">
  <div class="wg-header">
    <span class="wg-title">◈ {_esc(title)}</span>
    <span class="wg-badge">NODE CARD</span>
    <div class="wg-live"><div class="wg-dot" style="background:{col};"></div>{'ONLINE' if online else 'OFFLINE'}</div>
  </div>
  <div class="wg-body" style="display:flex;align-items:center;justify-content:center;padding:20px;">
    <div style="width:100%;max-width:380px;">

      <!-- Name + ID -->
      <div style="text-align:center;margin-bottom:20px;">
        <div style="font-size:22px;font-weight:900;color:{col};{glow}margin-bottom:4px;">{_esc(n['long_name'])}</div>
        <div style="font-size:10px;color:var(--txt3);">{_esc(n['node_id'])} · {_esc(n.get('short_name',''))}</div>
        <div style="font-size:9px;color:var(--txt3);margin-top:3px;">{_esc(role)} · {_esc(hw)}</div>
      </div>

      <!-- Stat grid -->
      <div id="nc-grid" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:14px;">
        {_kv("BATTERY", f"{bat}%" if bat is not None else "—", _bat_color(bat))}
        {_kv("SNR", f"{snr:.1f}dB" if snr is not None else "—", _snr_color(snr))}
        {_kv("HOPS", str(hops) if hops is not None else "—", "var(--acc)")}
      </div>

      <!-- GPS -->
      {f'<div style="background:var(--bg2);border:1px solid var(--bd);border-radius:4px;padding:10px;font-size:9px;color:var(--txt2);text-align:center;"><span style="color:var(--warn)">GPS</span> {n["lat"]:.5f}, {n["lon"]:.5f}</div>' if n.get("has_gps") and n.get("lat") else ""}

      <!-- Last heard -->
      <div style="text-align:center;margin-top:10px;font-size:9px;color:var(--txt3);">
        Last heard: {_esc(_fmt_ago(n.get("age_s")))}
      </div>

    </div>
  </div>
  <div class="wg-footer">
    <span id="nc-ts">—</span>
    <span>MeshDash · Share Map</span>
  </div>
</div>
<script>
document.getElementById('nc-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
window._widgetUpdate = function(d) {{
  if (!d || !d.node) return;
  location.reload(); // simplest approach for nodecard
}};
</script>"""
    return _base_html(title, theme, body, "", refresh_s, data_url)


# ---------------------------------------------------------------------------
# Preview-HTML endpoint — returns a full rendered widget page for the builder
# iframe to display, using current form config without saving to DB.
# ---------------------------------------------------------------------------

@plugin_router.get("/preview-html/{slot_id}", response_class=HTMLResponse, include_in_schema=False)
async def preview_html(slot_id: str, type: str = "stats", config: str = "{}"):
    """
    Returns a fully rendered widget HTML page for use in the studio preview iframe.
    Not saved to DB — no token — just a live render of current config.
    """
    try:
        cfg = json.loads(config)
    except Exception:
        cfg = {}
    nodes    = _get_nodes(slot_id)
    filtered = _filter_nodes(nodes, cfg)
    data     = _build_data_payload(type, filtered, cfg)
    theme    = cfg.get("theme", "dark")
    title    = cfg.get("_title", "Preview")
    html     = _render_widget_html(type, data, cfg, title, theme, 0, "", "")
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Nodes endpoint (used by index.html nodecard picker)
# ---------------------------------------------------------------------------

@plugin_router.get("/nodes/{slot_id}")
async def nodes_for_picker(slot_id: str):
    """All nodes for slot — used by widget builder node picker."""
    slot = _node_registry.get(slot_id)
    if not slot:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    now = time.time()
    result = []
    for nid, nd in slot.meshtastic_data.nodes.items():
        u   = nd.get("user") or {}
        lh  = nd.get("lastHeard") or nd.get("last_heard") or 0
        result.append({
            "node_id":   nid,
            "long_name": u.get("longName") or nd.get("long_name") or nid,
            "last_heard": lh,
        })
    result.sort(key=lambda n: -(n["last_heard"] or 0))
    return {"slot_id": slot_id, "nodes": result}
