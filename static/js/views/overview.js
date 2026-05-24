/* ── Overview Module ──────────────────────────────────────────────────── */
window.C2_ITEMS_PER_PAGE = window.C2_ITEMS_PER_PAGE || 30;
window.c2CurrentPage     = window.c2CurrentPage || 1;

/* ── Persistent module state on window to survive view reloads ── */
/* All const/let that would redeclare on re-navigation live here  */
window._ov = window._ov || {
    metricsChart: null,
    sparkCharts:  {},
    sparks:       {},
    metData:      { snr:[], rssi:[], labels:[] },
    geoCache:     {},
    geoQueue:     [],
    geoRunning:   false,
    miniMaps:     {},   // elementId → Leaflet map instance
};
var _ov = window._ov;

/* Aliases — var so re-execution on view reload doesn't throw */
var _ovSparks      = _ov.sparks;
var _ovSparkCharts = _ov.sparkCharts;
var _ovMetData     = _ov.metData;
var _geoCache      = _ov.geoCache;
var _geoQueue      = _ov.geoQueue;

function _ovMetricsChart()   { return _ov.metricsChart; }
function _setMetricsChart(c) { _ov.metricsChart = c; }
function _geoIsRunning()     { return _ov.geoRunning; }
function _setGeoRunning(v)   { _ov.geoRunning = v; }
function _pushSpark(nodeId, val) {
    if (val == null) return;
    if (!_ovSparks[nodeId]) _ovSparks[nodeId] = [];
    _ovSparks[nodeId].push(val);
    if (_ovSparks[nodeId].length > 20) _ovSparks[nodeId].shift();
}

/* ── Color palette (deterministic per nodeId) ── */
var _ovPalette = window._ovPalette || ['#00c8f5','#00e87a','#ffa826','#b060ff','#ff3050','#46f0f0','#f032e6','#bcf60c','#008080','#e6194b','#3cb44b','#4363d8'];
window._ovPalette = _ovPalette;
function _ovColor(nodeId) {
    let h = 0;
    for (let i = 0; i < (nodeId||'').length; i++) h = (Math.imul(31,h) + nodeId.charCodeAt(i)) | 0;
    return _ovPalette[Math.abs(h) % _ovPalette.length];
}

/* ── Helpers ── */
function _ovBatColor(v) { return v==null?'var(--txt)':v<20?'#ff3050':v<40?'#ffa826':'#00e87a'; }
function _ovSnrColor(v) { return v==null?'var(--txt)':v<-15?'#ff3050':v<-5?'#ffa826':'#00e87a'; }
function _ovSigBars(snr) {
    // Return 0-5 signal bars from SNR
    if (snr == null) return 0;
    if (snr > 8)  return 5;
    if (snr > 4)  return 4;
    if (snr > 0)  return 3;
    if (snr > -8) return 2;
    return 1;
}

function _ovRing(id, txtId, pct, color) {
    const circ = document.getElementById(id);
    const txt  = document.getElementById(txtId);
    if (!circ || !txt) return;
    const len = 163.4;
    circ.setAttribute('stroke-dashoffset', (len * (1 - Math.max(0, Math.min(1, pct/100)))).toFixed(1));
    circ.setAttribute('stroke', color || circ.getAttribute('stroke'));
    txt.textContent = Math.round(pct) + '%';
}

/* ── Connection bar animation ── */
function _ovConnBars(level) { // 0-5
    const bars = document.querySelectorAll('.ov-conn-bar');
    const heights = [4,7,11,15,18];
    bars.forEach((b,i) => {
        b.style.background = i < level ? 'var(--acc)' : 'var(--bd2)';
        b.style.height = heights[i] + 'px';
    });
}

/* ── Update KPI strip + health ── */
window.c2UpdateOverviewStats = function() {
    const s = window.meshState?.stats || {};
    const nodes = Object.values(window.meshState?.nodes || {});

    // KPIs
    const setKPI = (id, val, sub) => {
        const el = document.getElementById(id);
        if (el) el.textContent = typeof val === 'number' ? val.toLocaleString() : (val || '0');
        const subEl = document.getElementById(id + '-sub');
        if (subEl && sub) subEl.textContent = sub;
    };
    setKPI('ov-kpi-pkts',  s.packets_received_session  || 0, `+${s.other_packets_session||0} other`);
    setKPI('ov-kpi-nodes', s.nodes_seen_session         || nodes.length, `${nodes.filter(n=>n.isLocal||n.is_local).length} local`);
    setKPI('ov-kpi-msgs',  s.text_messages_session      || 0, `pos: ${s.position_updates_session||0}`);
    setKPI('ov-kpi-chs',   s.channels_seen_session      || 0, `tlm: ${s.telemetry_reports_session||0}`);

    // Session uptime
    const upEl = document.getElementById('ov-uptime');
    if (upEl && s.elapsed_time_session) upEl.textContent = window.fmtUptime(s.elapsed_time_session);
    const posEl = document.getElementById('ov-pos'); if (posEl) posEl.textContent = s.position_updates_session||0;
    const telEl = document.getElementById('ov-tel'); if (telEl) telEl.textContent = s.telemetry_reports_session||0;

    // Connection status
    const status = window.meshState?.connectionStatus || 'Unknown';
    const statusEl = document.getElementById('ov-conn-status');
    const dotEl    = document.getElementById('ov-conn-dot');
    if (statusEl) statusEl.textContent = status.toUpperCase();
    if (dotEl) {
        const sl = status.toLowerCase();
        dotEl.className = 'ov-conn-dot ' + (sl==='connected'?'on':sl.includes('fail')||sl.includes('off')?'err':'warn');
    }
    const localId   = window.meshState?.local_node_id;
    const localNode = localId ? window.meshState.nodes[localId] : null;
    const nodeEl    = document.getElementById('ov-conn-node');
    if (nodeEl && localNode) {
        const name = window.getMeshVal(localNode,'long_name','longName','short_name','shortName') || localId;
        const bat  = window.getMeshVal(localNode,'battery_level','batteryLevel');
        nodeEl.textContent = `${name}${bat!=null?' · '+bat+'%':''}`;
    }
    _ovConnBars(status.toLowerCase()==='connected'?5:status.toLowerCase().includes('init')?2:0);

    // Mesh health
    const now = Date.now()/1000;
    const online   = nodes.filter(n => n.lastHeard && (now-n.lastHeard)<3600).length;
    const withBat  = nodes.filter(n => window.getMeshVal(n,'battery_level','batteryLevel')!=null);
    const avgBat   = withBat.length ? Math.round(withBat.reduce((a,n)=>a+(window.getMeshVal(n,'battery_level','batteryLevel')||0),0)/withBat.length) : null;
    const withSNR  = nodes.filter(n => n.snr!=null);
    const avgSNR   = withSNR.length ? (withSNR.reduce((a,n)=>a+(n.snr||0),0)/withSNR.length).toFixed(1) : null;
    const withGPS  = nodes.filter(n => { const lat=window.getMeshVal(n,'latitude'); return lat&&lat!==0; }).length;
    const withEnv  = nodes.filter(n => window.getMeshVal(n,'temperature')!=null).length;

    document.getElementById('ov-h-online').textContent = `${online}/${nodes.length}`;
    document.getElementById('ov-h-avgsnr').textContent = avgSNR!=null ? avgSNR+'dB' : '—';
    document.getElementById('ov-h-avgbat').textContent = avgBat!=null ? avgBat+'%' : '—';
    document.getElementById('ov-h-gps').textContent    = `${withGPS}/${nodes.length}`;

    const total = nodes.length || 1;
    _ovRing('ov-ring-conn','ov-ring-conn-txt', (online/total)*100, online>total*.5?'#00e87a':'#ffa826');
    _ovRing('ov-ring-bat', 'ov-ring-bat-txt',  avgBat||0, _ovBatColor(avgBat));
    _ovRing('ov-ring-gps', 'ov-ring-gps-txt',  (withGPS/total)*100, 'var(--acc)');
    _ovRing('ov-ring-env', 'ov-ring-env-txt',  (withEnv/total)*100, '#f032e6');

    const tEl = document.getElementById('ov-health-time');
    if (tEl) tEl.textContent = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});

    // Push avg metrics into mini chart
    _ovPushMetrics(avgSNR!=null?parseFloat(avgSNR):null, nodes.filter(n=>n.rssi!=null).length?nodes.filter(n=>n.rssi!=null).reduce((a,n)=>a+n.rssi,0)/nodes.filter(n=>n.rssi!=null).length:null);
};

/* ── Background metrics accumulator — called by app.js even when not on overview ── */
window._ovPushMetricsBackground = function() {
    const nodes = Object.values(window.meshState?.nodes || {});
    const withSNR  = nodes.filter(n => n.snr != null);
    const withRSSI = nodes.filter(n => n.rssi != null);
    const avgSNR   = withSNR.length  ? withSNR.reduce((a,n)=>a+n.snr,0)/withSNR.length   : null;
    const avgRSSI  = withRSSI.length ? withRSSI.reduce((a,n)=>a+n.rssi,0)/withRSSI.length : null;
    _ovPushMetrics(avgSNR != null ? parseFloat(avgSNR.toFixed(1)) : null, avgRSSI);
};

/* ── Avg metrics rolling chart ── */
function _ovPushMetrics(snr, rssi) {
    const t = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    _ovMetData.snr.push(snr);
    _ovMetData.rssi.push(rssi);
    _ovMetData.labels.push(t);
    if (_ovMetData.snr.length > 30) { _ovMetData.snr.shift(); _ovMetData.rssi.shift(); _ovMetData.labels.shift(); }

    const canvas = document.getElementById('ov-chart-metrics');
    if (!canvas) return;
    if (_ov.metricsChart) {
        _ov.metricsChart.data.labels           = _ovMetData.labels;
        _ov.metricsChart.data.datasets[0].data = _ovMetData.snr;
        _ov.metricsChart.data.datasets[1].data = _ovMetData.rssi;
        _ov.metricsChart.update('none');
        return;
    }
    _ov.metricsChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: _ovMetData.labels,
            datasets: [
                { label:'Avg SNR', data:_ovMetData.snr,  borderColor:'#b060ff', backgroundColor:'#b060ff15', borderWidth:1.5, pointRadius:0, tension:0.3, yAxisID:'y' },
                { label:'Avg RSSI',data:_ovMetData.rssi, borderColor:'#ff3050', backgroundColor:'#ff305015', borderWidth:1.5, pointRadius:0, tension:0.3, yAxisID:'y1' },
            ]
        },
        options: {
            responsive:true, maintainAspectRatio:false, animation:false,
            plugins:{ legend:{labels:{font:{size:8},boxWidth:8,padding:6,color:'#7a96b2'}}, tooltip:{titleFont:{size:8},bodyFont:{size:8}} },
            scales:{
                x:{ display:false },
                y:{ position:'left',  grid:{color:'#1e3048'}, ticks:{font:{size:8},color:'#7a96b2'}, title:{display:true,text:'SNR',color:'#7a96b2',font:{size:7}} },
                y1:{ position:'right', grid:{drawOnChartArea:false}, ticks:{font:{size:8},color:'#7a96b2'}, title:{display:true,text:'RSSI',color:'#7a96b2',font:{size:7}} }
            }
        }
    });
}

window.c2RenderFeed = function() {
    const feed    = document.getElementById('c2-live-feed');
    const counter = document.getElementById('feed-counter');
    if (!feed || !window.meshState?.packets) return;
    // Bail early if packet list hasn't changed since last render
    const _sig = (window.meshState.packets[0]?.id || '') + window.meshState.packets.length;
    if (feed._lastSig === _sig) return;
    feed._lastSig = _sig;
    if (counter) counter.textContent = window.meshState.packets.length + ' PKTS';
 
    const typeMap = {
        'Message':      ['MSG', 'var(--ok)'],
        'Position':     ['POS', 'var(--warn)'],
        'Telemetry':    ['TLM', '#b060ff'],
        'Node Info':    ['NFO', 'var(--acc)'],
        'Traceroute':   ['TRC', '#46f0f0'],
        'Ack':          ['ACK', '#00c8f5'],
        'Routing Error':['ROU', 'var(--err)'],
        'Neighbor Info':['NBR', '#ffa826'],
        'Waypoint':     ['WAY', '#ffa826'],
        'Detection':    ['DET', '#46f0f0'],
        'Range Test':   ['RNG', 'var(--txt2)'],
        'Paxcounter':   ['PAX', 'var(--txt2)'],
    };
 
    // Source tag + colour mapping — covers all known source values
    const srcTagMap = {
        'RF':        ['RF',  'var(--ok)'],
        'MQTT':      ['MQT', '#b060ff'],
        'LOCAL':     ['LOC', 'var(--acc)'],
        'WEBSERIAL': ['USB', '#00c8f5'],   // was "WE" — now "USB"
        'UNKNOWN':   ['???', 'var(--txt3)'],
    };
 
    feed.innerHTML = window.meshState.packets.slice(0, 60).map(p => {
        const type = p.app_packet_type || 'Unk';
        const [tc, col] = typeMap[type] || [type.substring(0, 3).toUpperCase(), 'var(--txt3)'];
 
        const fromRef = window.meshState.nodes?.[p.fromId];
        const from    = window.getMeshVal(fromRef, 'short_name', 'shortName') || (p.fromId || '???').slice(-4);
        const toRef   = window.meshState.nodes?.[p.toId];
        const to      = p.toId === '^all'
            ? 'ALL'
            : (window.getMeshVal(toRef, 'short_name', 'shortName') || (p.toId || '').slice(-4));
 
        // ── Payload extraction ──────────────────────────────────────────
        let payload = '';
        if (type === 'Message') {
            payload = p.decoded?.payload || p.decoded?.text || '';
        } else if (type === 'Position') {
            const pos = p.decoded?.position;
            if (pos) payload = `${(pos.latitude || 0).toFixed(3)},${(pos.longitude || 0).toFixed(3)}`;
        } else if (type === 'Telemetry') {
            const dm = p.decoded?.telemetry?.deviceMetrics;
            if (dm) payload = `bat:${dm.batteryLevel || 0}% ${dm.voltage?.toFixed(2) || '?'}V`;
        } else {
            payload = type;
        }
 
        // ── SNR ──────────────────────────────────────────────────────────
        const snr = p.rxSnr != null ? ` ${p.rxSnr > 0 ? '+' : ''}${p.rxSnr}dB` : '';
 
        // ── Timestamp — resolve from any available field ──────────────────
        // Web Serial injected packets have rxTime but not timestamp.
        // Packets through add_packet() have both; use whichever is present.
        const ts = p.timestamp || p.rxTime || p.rx_time;
 
        // ── Source tag ────────────────────────────────────────────────────
        let src = (p.source || '').toUpperCase();
        
        // ANTI-CONFUSION: If it's an unknown source but came from our own radio, tag it LOCAL
        if ((src === 'UNKNOWN' || !src) && p.fromId === window.meshState.local_node_id) {
            src = 'LOCAL';
        }

        const [srcTag, srcCol] = srcTagMap[src] || [
            // Fallback: first 3 chars but never show blank
            src ? src.substring(0, 3) : 'RX',
            'var(--txt3)'
        ];
 
        const confTip = p.source
            ? `${p.source}${p.source_confidence ? ' ' + (p.source_confidence * 100).toFixed(0) + '%' : ''}`
            : 'RX';
 
        return `<div class="ov-feed-row">
            <span class="ov-feed-ts">${window.fmtTime(ts)}</span>
            <span class="ov-feed-dir" style="color:${srcCol}" title="${window.escapeHtml(confTip)}">${srcTag}</span>
            <span class="ov-feed-typ" style="color:${col}">[${tc}]</span>
            <span class="ov-feed-pay">
                <span style="color:var(--acc);opacity:.9">${window.escapeHtml(from)}</span>
                <span style="color:var(--txt3)">→</span>
                <span style="color:var(--acc);opacity:.9">${window.escapeHtml(to)}</span>
                ${window.escapeHtml(String(payload).slice(0, 60))}
                ${snr ? `<span style="color:#7a96b2">${snr}</span>` : ''}
            </span>
        </div>`;
    }).join('') + `<div style="color:var(--ok);font-size:10px;margin-top:4px;font-family:var(--mono);">root@meshdash:~# _</div>`;
};

/* ── HW filter populate ── */
function _ovPopulateHWFilter() {
    const sel = document.getElementById('node-filter-hw');
    if (!sel) return;
    const hws = [...new Set(Object.values(window.meshState?.nodes||{}).map(n => {
        let h = window.getMeshVal(n,'hardware_model_string','hw_model_str','hw_model')||'';
        if (h.includes('.')) h = h.split('.').pop();
        return h;
    }).filter(Boolean))].sort();
    const cur = sel.value;
    sel.innerHTML = '<option value="">All HW</option>' + hws.map(h=>`<option value="${h}"${h===cur?' selected':''}>${window.escapeHtml(h)}</option>`).join('');
}

/* ── Geocoding — backend proxy + persistent JSON cache ─────────────────
 * Flow:
 *  1. On init: fetch /api/geocode-cache to pre-populate cache
 *  2. Per node: if cache hit → update card immediately
 *  3. Cache miss: queue request to /api/geocode?lat=X&lon=Y (backend proxy)
 *  4. Backend calls Nominatim at ≤1 req/s, caches to data/geocode_cache.json
 *  5. 429 or error → mark as unavailable, don't retry this session
 * ─────────────────────────────────────────────────────────────────────── */

// _geoCache and _geoQueue aliased from window._ov above

/* Round to 3 d.p. (~111m precision) — accurate enough for road names,
   tolerant of GPS drift between position updates */
function _geoKey(lat, lon) {
    return Math.round(lat*1e3)/1e3 + ',' + Math.round(lon*1e3)/1e3;
}

/* Populate in-memory cache from the on-disk JSON file the backend maintains */
async function _geoLoadDiskCache() {
    if (_ov.diskCacheLoaded) return;
    _ov.diskCacheLoaded = true;
    try {
        const r = await fetch('/api/geocode-cache?_=' + Date.now());
        if (!r.ok) return;                          // file doesn't exist yet — fine
        const data = await r.json();
        // data is { "lat,lon": { short, full, ... }, ... }
        Object.assign(_ov.latLonCache, data);
    } catch(e) { /* file missing on first run */ }
}

/* Lookup a nodeId against the lat/lon cache */
function _geoCacheHit(lat, lon) {
    const key = _geoKey(lat, lon);
    return _ov.latLonCache[key] || null;
}

async function _geoFetch(nodeId, lat, lon) {
    if (_geoCache[nodeId]) return;
    _geoCache[nodeId] = { short:'Looking up...', full:'', ts: Date.now() };

    const key = _geoKey(lat, lon);

    // Check lat/lon disk cache first
    if (_ov.latLonCache[key]) {
        _geoCache[nodeId] = { ..._ov.latLonCache[key], ts: Date.now() };
        _geoUpdateCard(nodeId);
        return;
    }

    try {
        const r = await fetch('/api/geocode?lat=' + lat + '&lon=' + lon);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const d = await r.json();

        if (d.error === 'rate_limited') {
            _geoCache[nodeId] = { short: 'Rate limited', full: '', ts: Date.now() };
            _geoUpdateCard(nodeId);
            // Stop the entire queue — we're rate limited for this session
            _geoQueue.length = 0;
            _ov.geoRunning = false;
            return;
        }
        if (d.error) {
            _geoCache[nodeId] = { short: 'Location unavailable', full: '', ts: Date.now() };
            _geoUpdateCard(nodeId);
            return;
        }

        const result = { short: d.short || 'Unknown location', full: d.full || '' };
        _geoCache[nodeId]     = { ...result, ts: Date.now() };
        _ov.latLonCache[key]  = result;   // update in-memory lat/lon cache too
    } catch(e) {
        _geoCache[nodeId] = { short: 'Unavailable', full: '', ts: Date.now() };
    }
    _geoUpdateCard(nodeId);
}

function _geoUpdateCard(nodeId) {
    const locEl = document.querySelector('.ov-nc[data-node-id="' + nodeId + '"] .ov-nc-loc');
    if (!locEl) return;
    const geo = _geoCache[nodeId];
    if (!geo) return;
    const txtEl = locEl.querySelector('.ov-nc-loc-txt');
    const tipEl = locEl.querySelector('.ov-geo-tip');
    if (txtEl) txtEl.textContent = geo.short;
    if (tipEl) tipEl.textContent = geo.full || geo.short;
    if (geo.short && geo.short !== 'Looking up...') locEl.classList.add('has-fix');

    // Cap cache sizes to prevent unbounded growth
    const GEO_MAX = 500;
    const geoKeys = Object.keys(_geoCache);
    if (geoKeys.length > GEO_MAX) {
        geoKeys.sort((a,b) => (_geoCache[a].ts||0) - (_geoCache[b].ts||0))
               .slice(0, geoKeys.length - GEO_MAX)
               .forEach(k => delete _geoCache[k]);
    }
    const llKeys = Object.keys(_ov.latLonCache);
    if (llKeys.length > GEO_MAX) {
        llKeys.slice(0, llKeys.length - GEO_MAX).forEach(k => delete _ov.latLonCache[k]);
    }
}

function _geoEnqueue(nodeId, lat, lon) {
    // Check lat/lon disk cache first — instant, no request needed
    const cached = _geoCacheHit(lat, lon);
    if (cached) {
        _geoCache[nodeId] = { ...cached, ts: Date.now() };
        _geoUpdateCard(nodeId);
        return;
    }
    if (_geoCache[nodeId]) { _geoUpdateCard(nodeId); return; }
    if (_geoQueue.find(function(q){return q.id===nodeId;})) return;
    _geoQueue.push({ id:nodeId, lat, lon });
    _geoProcess();
}

async function _geoProcess() {
    if (_ov.geoRunning || !_geoQueue.length) return;
    _ov.geoRunning = true;
    while (_geoQueue.length) {
        if (!_geoQueue.length) break;
        const item = _geoQueue.shift();
        await _geoFetch(item.id, item.lat, item.lon);
        // Small stagger to avoid bursting; backend already rate-limits Nominatim
        if (_geoQueue.length) await new Promise(function(res){setTimeout(res, 350);});
    }
    _ov.geoRunning = false;
}

/* Load disk cache once when the module first runs */
window._ov.latLonCache    = window._ov.latLonCache    || {};
window._ov.diskCacheLoaded = window._ov.diskCacheLoaded || false;
var _latLonCache = window._ov.latLonCache; // alias
_geoLoadDiskCache();


/* ── Role label ── */
function _ovRoleLabel(role) {
    const r = String(role||'').toUpperCase();
    if (r.includes('ROUTER')&&r.includes('CLIENT')) return 'R+C';
    if (r.includes('ROUTER'))   return 'RTR';
    if (r.includes('REPEATER')) return 'RPT';
    if (r.includes('TRACKER'))  return 'TRK';
    if (r.includes('CLIENT'))   return 'CLI';
    return role ? String(role).slice(0,3).toUpperCase() : '???';
}

/* ── Mini map helpers ── */
function _ovDestroyAllMiniMaps() {
    Object.keys(_ov.miniMaps).forEach(id => {
        try { _ov.miniMaps[id].remove(); } catch(e) {}
        delete _ov.miniMaps[id];
    });
}

function _ovInitMiniMap(elementId, lat, lon, hasFix, color) {
    // Destroy any existing instance on this container first
    if (_ov.miniMaps[elementId]) {
        try { _ov.miniMaps[elementId].remove(); } catch(e) {}
        delete _ov.miniMaps[elementId];
    }
    const container = document.getElementById(elementId);
    if (!container) return;

    // Clear any previous Leaflet state left on the DOM element
    delete container._leaflet_id;

    try {
        const center = hasFix ? [lat, lon] : [20, 0];
        const zoom   = hasFix ? 13 : 2;

        const map = L.map(elementId, {
            center,
            zoom,
            zoomControl:       false,
            attributionControl: false,
            dragging:          false,
            scrollWheelZoom:   false,
            doubleClickZoom:   false,
            boxZoom:           false,
            keyboard:          false,
            tap:               false,
            preferCanvas:      true,
        });

        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            subdomains: 'abcd',
            maxZoom:    19,
            maxNativeZoom: 15,
            detectRetina: false,
            keepBuffer: 0,
        }).addTo(map);

        if (hasFix) {
            L.circleMarker([lat, lon], {
                radius:      5,
                color:       color || '#00c8f5',
                fillColor:   color || '#00c8f5',
                fillOpacity: 0.85,
                weight:      2,
            }).addTo(map);
        } else {
            container.style.filter = 'grayscale(100%) opacity(0.25)';
        }

        _ov.miniMaps[elementId] = map;
    } catch(e) {
        // Leaflet not available or container already torn down — silent
    }
}

/* ── Node render ── */
window.c2RenderNodes = function(softUpdate = false) {
    const grid = document.getElementById('ov-node-grid');
    if (!grid) return;
    // Clear cached DB counts if active slot changed since last render
    const _renderSlot = window._activeSlotId || 'node_0';
    if (window._ovLastRenderSlot !== _renderSlot) {
        window._ovLastRenderSlot = _renderSlot;
        softUpdate = false; // slot change always needs full re-render
        Object.values(window.meshState?.nodes || {}).forEach(n => {
            delete n.historicalMessageCount;
            delete n.historicalPositionCount;
            delete n.historicalTelemetryCount;
            delete n.fetchingCounts;
        });
    }

    _ovPopulateHWFilter();

    const search  = (document.getElementById('node-search')?.value||'').toLowerCase();
    const sortKey = document.getElementById('node-sort')?.value || 'last_heard';
    const hwFilt  = document.getElementById('node-filter-hw')?.value || '';

    let nodes = Object.values(window.meshState?.nodes||{}).filter(n => {
        const name    = (window.getMeshVal(n,'long_name','longName','short_name','shortName')||n.node_id||'').toLowerCase();
        const hw      = (window.getMeshVal(n,'hardware_model_string','hw_model_str','hw_model')||'');
        const hwShort = hw.includes('.')?hw.split('.').pop():hw;
        return (!search || name.includes(search) || (n.node_id||'').toLowerCase().includes(search))
            && (!hwFilt || hwShort === hwFilt);
    });

    nodes.sort((a,b) => {
        const gv = window.getMeshVal;
        if (sortKey==='last_heard')  return (b.lastHeard||0)-(a.lastHeard||0);
        if (sortKey==='signal')      return (b.rssi||-999)-(a.rssi||-999);
        if (sortKey==='snr')         return (b.snr||-99)-(a.snr||-99);
        if (sortKey==='battery')     return (gv(b,'battery_level','batteryLevel')||0)-(gv(a,'battery_level','batteryLevel')||0);
        if (sortKey==='name')        return (gv(a,'long_name','longName')||'').localeCompare(gv(b,'long_name','longName')||'');
        if (sortKey==='online') {
            const now2 = Date.now()/1000;
            const ao = a.lastHeard&&(now2-a.lastHeard)<3600?1:0;
            const bo = b.lastHeard&&(now2-b.lastHeard)<3600?1:0;
            return bo!==ao ? bo-ao : (b.lastHeard||0)-(a.lastHeard||0);
        }
        if (sortKey==='ch_util')     return (gv(b,'channel_utilization','channelUtilization')||0)-(gv(a,'channel_utilization','channelUtilization')||0);
        if (sortKey==='uptime')      return (gv(b,'uptime_seconds','uptimeSeconds')||0)-(gv(a,'uptime_seconds','uptimeSeconds')||0);
        if (sortKey==='altitude')    return (gv(b,'altitude')||0)-(gv(a,'altitude')||0);
        if (sortKey==='gps') {
            const ag = gv(a,'latitude')&&gv(a,'latitude')!==0?1:0;
            const bg = gv(b,'latitude')&&gv(b,'latitude')!==0?1:0;
            return bg-ag;
        }
        if (sortKey==='hops')        return (b.hop_limit||-1)-(a.hop_limit||-1);
        if (sortKey==='msgs')        return (b.historicalMessageCount||0)-(a.historicalMessageCount||0);
        if (sortKey==='node_id') {
            const ai = parseInt((a.node_id||'').replace('!',''),16)||0;
            const bi = parseInt((b.node_id||'').replace('!',''),16)||0;
            return ai-bi;
        }
        if (sortKey==='role')        return (a.role||'').localeCompare(b.role||'');
        return 0;
    });

    const total = Math.ceil(nodes.length/window.C2_ITEMS_PER_PAGE)||1;
    if (window.c2CurrentPage>total) window.c2CurrentPage=total;
    const pageInd  = document.getElementById('page-indicator');
    const btnPrev  = document.getElementById('btn-prev-page');
    const btnNext  = document.getElementById('btn-next-page');
    if (pageInd) pageInd.textContent = `PAGE ${window.c2CurrentPage} / ${total}`;
    if (btnPrev) btnPrev.disabled = window.c2CurrentPage===1;
    if (btnNext) btnNext.disabled = window.c2CurrentPage===total;

    const paged = nodes.slice((window.c2CurrentPage-1)*window.C2_ITEMS_PER_PAGE, window.c2CurrentPage*window.C2_ITEMS_PER_PAGE);

    if (!paged.length) {
        grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:var(--txt3);padding:40px;font-family:var(--mono);font-size:11px;">[ NO NODES MATCHING QUERY ]</div>`;
        return;
    }

    // ── Soft update: patch values in-place without destroying the DOM ──────
    // Only applies when the same set of node cards is already rendered.
    // Avoids mini-map/sparkline destruction and the resulting flicker caused
    // by per-packet node_update SSE events on busy secondary slots.
    if (softUpdate) {
        const renderedIds = [...grid.querySelectorAll('.ov-nc[data-node-id]')].map(el => el.dataset.nodeId);
        const pagedIds    = paged.map(n => n.node_id);
        const sameSet     = renderedIds.length === pagedIds.length && pagedIds.every((id, i) => id === renderedIds[i]);
        if (sameSet) {
            const now2 = Date.now() / 1000;
            paged.forEach(n => {
                const card = grid.querySelector(`.ov-nc[data-node-id="${CSS.escape(n.node_id)}"]`);
                if (!card) return;
                _pushSpark(n.node_id, window.getMeshVal(n,'battery_level','batteryLevel'));
                const bat     = window.getMeshVal(n,'battery_level','batteryLevel');
                const batPct  = bat != null ? Math.min(100, Math.max(0, bat)) : null;
                const batCol  = _ovBatColor(bat);
                const snr     = n.snr;
                const rssi    = n.rssi;
                const isOnline = n.lastHeard && (now2 - n.lastHeard) < 3600;
                const ago     = window.fmtTimeAgo ? window.fmtTimeAgo(n.lastHeard) : '?';
                const sigBars = _ovSigBars(snr);
                // Battery bar
                const batLbl = card.querySelector('.ov-bat-lbl');
                const batVal = card.querySelector('.ov-bat-val');
                const batFill = card.querySelector('.ov-bat-fill');
                const volt = window.getMeshVal(n,'voltage');
                if (batLbl) batLbl.textContent = batPct!=null?'BATTERY'+(volt!=null?' · '+parseFloat(volt).toFixed(2)+'V':''):'NO POWER DATA';
                if (batVal) { batVal.textContent = batPct!=null?batPct+'%':''; batVal.style.color = batCol; }
                if (batFill) { batFill.style.width = (batPct||0)+'%'; batFill.style.background = batCol; }
                // Online dot + heard
                const dot = card.querySelector('.ov-online-dot');
                const heardEl = card.querySelector('.ov-nc-heard');
                if (dot)     { dot.className = 'ov-online-dot '+(isOnline?'on':'off'); }
                if (heardEl) { heardEl.textContent = ago; heardEl.style.color = isOnline?'var(--ok)':'var(--txt3)'; }
                // Stats cells — patch the 3 data values we can update cheaply
                const cells = card.querySelectorAll('.ov-nc-cell:not(.ov-nc-cell-empty) .ov-nc-cell-val');
                cells.forEach(cel => {
                    const lbl = cel.nextElementSibling?.textContent?.trim();
                    if (lbl === 'SNR'  && snr  != null) { cel.textContent = (snr>0?'+':'')+snr+'dB';  cel.style.color = _ovSnrColor(snr); }
                    if (lbl === 'RSSI' && rssi != null) { cel.textContent = String(rssi); }
                    if (lbl === 'HEARD') { cel.textContent = ago; cel.style.color = isOnline?'var(--ok)':'var(--txt3)'; }
                });
                // Signal bars
                const bars = card.querySelectorAll('.ov-sig-bar');
                bars.forEach((b, i) => {
                    const lit = i < sigBars;
                    b.className = 'ov-sig-bar'+(lit?' lit':'');
                    if (lit) b.style.background = _ovColor(n.node_id); else b.style.background = '';
                });
            });
            // Re-apply plugin bridge overlays after in-place patch
            try { if (typeof window.PluginBridge?._applyAll === 'function') window.PluginBridge._applyAll(); } catch(_e) {}
            return; // done — no DOM teardown, no flicker
        }
    }

    const now = Date.now()/1000;

    // Push battery samples for sparklines
    paged.forEach(n => _pushSpark(n.node_id, window.getMeshVal(n,'battery_level','batteryLevel')));

    // Destroy sparkline charts for nodes no longer on page
    Object.keys(_ovSparkCharts).forEach(id => {
        if (!paged.find(n=>n.node_id===id)) { try{_ovSparkCharts[id].destroy();}catch(e){} delete _ovSparkCharts[id]; }
    });

    // Destroy all existing mini maps before wiping the grid DOM
    _ovDestroyAllMiniMaps();

    grid.innerHTML = paged.map((n, idx) => {
        const name     = window.getMeshVal(n,'long_name','longName','short_name','shortName')||n.node_id||'Unknown';
        const shortN   = window.getMeshVal(n,'short_name','shortName')||'';
        const initials = name.slice(0,2).toUpperCase();
        const color    = _ovColor(n.node_id);
        const bat      = window.getMeshVal(n,'battery_level','batteryLevel');
        const volt     = window.getMeshVal(n,'voltage');
        const snr      = n.snr;
        const rssi     = n.rssi;
        const isOnline = n.lastHeard && (now-n.lastHeard)<3600;
        const ago      = window.fmtTimeAgo?window.fmtTimeAgo(n.lastHeard):'?';
        const sigBars  = _ovSigBars(snr);
        const role     = window.getMeshVal(n,'role')||'';
        let   hw       = window.getMeshVal(n,'hardware_model_string','hw_model_str','hw_model')||'';
        if (hw.includes('.')) hw = hw.split('.').pop();
        const fw       = window.getMeshVal(n,'firmware_version','firmwareVersion')||'';
        const util     = window.getMeshVal(n,'channel_utilization','channelUtilization');
        const airTx    = window.getMeshVal(n,'air_util_tx','airUtilTx');
        const uptime   = window.getMeshVal(n,'uptime_seconds','uptimeSeconds');
        const temp     = window.getMeshVal(n,'temperature');
        const hum      = window.getMeshVal(n,'relative_humidity','relativeHumidity');
        const lat      = window.getMeshVal(n,'latitude');
        const lon      = window.getMeshVal(n,'longitude');
        const hasFix   = lat!=null && lat!==0 && lon!=null && lon!==0;
        const hops     = window.getMeshVal(n,'hop_limit','hopLimit');
        const sparkId  = `ovspk-${n.node_id.replace(/[^a-zA-Z0-9]/g,'')}`;
        const mapId    = `ovmap-${n.node_id.replace(/[^a-zA-Z0-9]/g,'')}`;

        // 🟢 ANTI-CONFUSION: If it's an unknown source but came from our own radio, tag it LOCAL
        let cardSource = (n.source || '').toUpperCase();
        if ((cardSource === 'UNKNOWN' || !cardSource) && n.node_id === window.meshState.local_node_id) {
            cardSource = 'LOCAL';
        }

        // Battery bar
        const batPct   = bat!=null ? Math.min(100,Math.max(0,bat)) : null;
        const batCol   = _ovBatColor(bat);

        // Signal bars HTML — no nested backticks
        const barsHtml = [1,2,3,4,5].map(i=> {
            const lit = i<=sigBars;
            const bg  = lit ? 'background:'+color+';' : '';
            return '<div class="ov-sig-bar' + (lit?' lit':'') + '" style="height:' + (4+i*3) + 'px;' + bg + '"></div>';
        }).join('');

        // Location strip
        const geo      = _geoCache[n.node_id];
        const geoShort = geo?.short || (hasFix ? 'Resolving location...' : 'No GPS fix');
        const geoFull  = geo?.full  || '';
        const locClass = hasFix ? 'ov-nc-loc has-fix' : 'ov-nc-loc';
        const locClick = hasFix ? `onclick="event.stopPropagation()"` : '';

        // Stats for the 3-col grid — only show cells that have data
        const statCells = [
            snr!=null   ? `<div class="ov-nc-cell"><div class="ov-nc-cell-val" style="color:${_ovSnrColor(snr)}">${snr>0?'+':''}${snr}dB</div><div class="ov-nc-cell-lbl">SNR</div></div>` : null,
            rssi!=null  ? `<div class="ov-nc-cell"><div class="ov-nc-cell-val" style="color:${rssi>-100?'var(--txt2)':'var(--err)'}">${rssi}</div><div class="ov-nc-cell-lbl">RSSI</div></div>` : null,
            `<div class="ov-nc-cell"><div class="ov-nc-cell-val" style="color:${isOnline?'var(--ok)':'var(--txt3)'}">${ago}</div><div class="ov-nc-cell-lbl">HEARD</div></div>`,
            util!=null  ? `<div class="ov-nc-cell"><div class="ov-nc-cell-val" style="color:#ffa826">${util.toFixed(0)}%</div><div class="ov-nc-cell-lbl">CH UTIL</div></div>` : null,
            uptime!=null? `<div class="ov-nc-cell"><div class="ov-nc-cell-val" style="color:var(--txt2);font-size:9px;">${window.fmtUptime(uptime)}</div><div class="ov-nc-cell-lbl">UPTIME</div></div>` : null,
            temp!=null  ? `<div class="ov-nc-cell"><div class="ov-nc-cell-val" style="color:#ffa826">${parseFloat(temp).toFixed(1)}°</div><div class="ov-nc-cell-lbl">TEMP</div></div>` : null,
        ].filter(Boolean);

        // Always exactly 6 cells — 2 fixed rows of 3 — so grid height never varies
        while (statCells.length < 6) statCells.push('<div class="ov-nc-cell ov-nc-cell-empty"><div class="ov-nc-cell-val" style="color:var(--bd2)">—</div><div class="ov-nc-cell-lbl">&nbsp;</div></div>');
        const cellsHtml = statCells.slice(0,6).join('');

        // Always render footer count placeholders with stable IDs for async injection
        const safeId = n.node_id.replace(/[^a-zA-Z0-9]/g,'');
        const msgCnt = n.historicalMessageCount ?? null;
        const posCnt = n.historicalPositionCount ?? null;
        const tlmCnt = n.historicalTelemetryCount ?? null;

        // Enqueue geo lookup after render
        if (hasFix && !_geoCache[n.node_id]) {
            setTimeout(() => _geoEnqueue(n.node_id, lat, lon), idx * 80);
        }

        return `<div class="ov-nc${n.isLocal||n.is_local?' nc-local':''}"
            data-node-id="${window.escapeHtml(n.node_id)}"
            style="--nc-col:${color};animation-delay:${(idx%12)*0.025}s;"
            onclick="window.c2OpenNodeDetail(this.dataset.nodeId)">

            <!-- Header -->
            <div class="ov-nc-head">
                <div class="ov-nc-avatar" style="background:${color};">
                    ${window.escapeHtml(initials)}
                    ${role ? '<div class="ov-nc-role-ico" title="'+window.escapeHtml(role)+'">'+_ovRoleLabel(role)+'</div>' : ''}
                </div>
                <div class="ov-nc-meta">
                    <div class="ov-nc-name" title="${window.escapeHtml(name)}">${window.escapeHtml(name)}</div>
                    <div class="ov-nc-sub">${window.escapeHtml(n.node_id)}${hw?' · '+window.escapeHtml(hw):''}${fw?' · '+window.escapeHtml(fw.slice(0,8)):''}${shortN&&shortN!==name?' · ['+window.escapeHtml(shortN)+']':''}</div>
                </div>
                <div class="ov-nc-status">
                        ${cardSource ? `<div style="font-size:7px; font-family:var(--mono); padding:1px 3px; border-radius:2px; background:${cardSource==='RF'?'var(--ok)':cardSource==='MQTT'?'#b060ff':cardSource==='LOCAL'?'var(--acc)':'var(--bd2)'}; color:#000; font-weight:bold; margin-bottom:2px;" title="${(n.source_confidence ? (n.source_confidence*100).toFixed(0) + '% confidence' : 'Local Node')}">${cardSource}</div>` : ''}
                        <div style="display:flex; align-items:center; gap:4px;">
                            <div class="ov-online-dot ${isOnline?'on':'off'}"></div>
                            <div class="ov-nc-heard" style="color:${isOnline?'var(--ok)':'var(--txt3)'}">${ago}</div>
                        </div>
                    </div>
            </div>

            <!-- Battery bar (always present, placeholder if no data) -->
            <div class="ov-bat-wrap">
                <div class="ov-bat-row">
                    <span class="ov-bat-lbl">${batPct!=null?'BATTERY'+(volt!=null?' · '+parseFloat(volt).toFixed(2)+'V':''):'NO POWER DATA'}</span>
                    <span class="ov-bat-val" style="color:${batCol}">${batPct!=null?batPct+'%':''}</span>
                </div>
                <div class="ov-bat-track">
                    <div class="ov-bat-fill" style="width:${batPct!=null?batPct:0}%;background:${batCol};"></div>
                </div>
            </div>

            <!-- Battery sparkline -->
            <div class="ov-nc-spark-wrap" id="ovspk-wrap-${n.node_id.replace(/[^a-zA-Z0-9]/g,'')}">
                <canvas id="${sparkId}" height="20"></canvas>
            </div>

            <!-- Location strip -->
            <div class="${locClass}" ${locClick}>
                <span class="ov-nc-loc-ico">${hasFix?'⌖':'○'}</span>
                <span class="ov-nc-loc-txt">${window.escapeHtml(geoShort)}</span>
                ${hasFix ? '<span style="font-size:8px;color:var(--acc);flex-shrink:0;opacity:.7;">↗</span>' : ''}
                ${hasFix&&geoFull ? '<div class="ov-geo-tip">'+window.escapeHtml(geoFull)+'</div>' : ''}
            </div>

            <!-- Stats grid (3 cols, contextual data) -->
            <div class="ov-nc-grid">${cellsHtml}</div>

            <!-- Mini map — sits between stats and footer -->
            <div class="ov-nc-map">
                <div id="${mapId}" style="width:100%;height:100%;"></div>
                ${!hasFix ? '<div class="ov-nc-map-nofix">[ NO GPS FIX ]</div>' : ''}
                ${hasFix ? '<div class="ov-nc-map-badge" style="background:'+color+';">GPS</div>' : ''}
            </div>

            <!-- Plugin badge zone — absolute corner ribbon, zero layout impact -->
            <div class="pb-badge-zone" aria-hidden="true"></div>

            <!-- Footer: signal bars + counts + DM -->
            <div class="ov-nc-foot">
                <div class="ov-sig-bars">${barsHtml}</div>
                <div class="ov-nc-foot-stat" id="ov-cnt-msg-${safeId}" style="${msgCnt==null?'opacity:.3':''}"><i class="fas fa-comment-alt"></i>${msgCnt!=null?msgCnt:'—'}</div>
                <div class="ov-nc-foot-stat" id="ov-cnt-pos-${safeId}" style="${posCnt==null?'opacity:.3':''}"><i class="fas fa-map-marker-alt"></i>${posCnt!=null?posCnt:'—'}</div>
                <div class="ov-nc-foot-stat" id="ov-cnt-tlm-${safeId}" style="${tlmCnt==null?'opacity:.3':''}"><i class="fas fa-chart-area"></i>${tlmCnt!=null?tlmCnt:'—'}</div>
                ${temp!=null&&hum!=null ? '<div class="ov-nc-foot-stat" style="color:var(--txt2);">💧'+parseFloat(hum).toFixed(0)+'%</div>' : ''}
                <button class="ov-nc-dm-btn" onclick="event.stopPropagation();_ovQuickDM(this.dataset.nid)" data-nid="${window.escapeHtml(n.node_id)}" title="DM this node">✉ DM</button>
            </div>

            <!-- Plugin section zone -->
            <div class="pb-section-zone" aria-hidden="true"></div>

        </div>`;
    }).join('');

    // Render sparklines — bail if grid was re-rendered before RAF fires
    const _renderGen = (window._ovRenderGen = (window._ovRenderGen || 0) + 1);
    requestAnimationFrame(() => {
        if (window._ovRenderGen !== _renderGen) return; // stale RAF, newer render already ran
        paged.forEach(n => {
            const data    = _ovSparks[n.node_id];
            const sparkId = `ovspk-${n.node_id.replace(/[^a-zA-Z0-9]/g,'')}`;
            const canvas  = document.getElementById(sparkId);
            if (!canvas || typeof canvas.getContext !== 'function') return;
            const color   = _ovColor(n.node_id);
            if (_ovSparkCharts[n.node_id]) { try{_ovSparkCharts[n.node_id].destroy();}catch(e){} }

            if (!data || data.length < 2) {
                try {
                    const ctx = canvas.getContext('2d');
                    ctx.clearRect(0,0,canvas.width,canvas.height);
                    ctx.strokeStyle = color+'44';
                    ctx.lineWidth = 1;
                    ctx.beginPath(); ctx.moveTo(0,11); ctx.lineTo(canvas.width,11); ctx.stroke();
                } catch(e) {}
                return;
            }

            _ovSparkCharts[n.node_id] = new Chart(canvas.getContext('2d'), {
                type: 'line',
                data: { datasets: [{
                    data: data.map((v,i)=>({x:i,y:v})),
                    borderColor: color,
                    backgroundColor: color+'20',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    tension: 0.4,
                    fill: true,
                }] },
                options: {
                    responsive:true, maintainAspectRatio:false, animation:false,
                    plugins:{ legend:{display:false}, tooltip:{enabled:false} },
                    scales:{ x:{display:false}, y:{display:false, min:0, max:100} }
                }
            });
        });

        // Fetch historical counts and inject directly into footer spans
        paged.forEach(function(n) {
            if (n.historicalMessageCount !== undefined) return; // already fetched
            if (n.fetchingCounts) return;
            n.fetchingCounts = true;
            var safeId = n.node_id.replace(/[^a-zA-Z0-9]/g,'');
            var nid = n.node_id;
            var _slotQ = (window._activeSlotId && window._activeSlotId !== 'node_0') ? '?slot_id='+encodeURIComponent(window._activeSlotId) : '';
            Promise.all([
                fetch('/api/nodes/'+nid+'/count/messages_sent'+_slotQ).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
                fetch('/api/nodes/'+nid+'/count/positions'+_slotQ).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;}),
                fetch('/api/nodes/'+nid+'/count/telemetry'+_slotQ).then(function(r){return r.ok?r.json():null;}).catch(function(){return null;})
            ]).then(function(results) {
                var mc = results[0] && results[0].count != null ? results[0].count : null;
                var pc = results[1] && results[1].count != null ? results[1].count : null;
                var tc = results[2] && results[2].count != null ? results[2].count : null;
                n.historicalMessageCount  = mc;
                n.historicalPositionCount = pc;
                n.historicalTelemetryCount = tc;
                n.fetchingCounts = false;
                // Inject into spans — card may still be in DOM
                var mEl = document.getElementById('ov-cnt-msg-'+safeId);
                var pEl = document.getElementById('ov-cnt-pos-'+safeId);
                var tEl = document.getElementById('ov-cnt-tlm-'+safeId);
                if (mEl) { mEl.innerHTML = '<i class="fas fa-comment-alt"></i>'+(mc!=null?mc:'—'); mEl.style.opacity = mc!=null?'1':'0.3'; }
                if (pEl) { pEl.innerHTML = '<i class="fas fa-map-marker-alt"></i>'+(pc!=null?pc:'—'); pEl.style.opacity = pc!=null?'1':'0.3'; }
                if (tEl) { tEl.innerHTML = '<i class="fas fa-chart-area"></i>'+(tc!=null?tc:'—'); tEl.style.opacity = tc!=null?'1':'0.3'; }
            }).catch(function() {
                n.fetchingCounts = false;
            });
        });

        // Skip mini-map init entirely when tab is hidden — they'll init on next c2RenderNodes
        if (document.visibilityState !== 'hidden') {
            paged.forEach((n, idx) => {
                const mapId  = `ovmap-${n.node_id.replace(/[^a-zA-Z0-9]/g,'')}`;
                const lat    = window.getMeshVal(n,'latitude');
                const lon    = window.getMeshVal(n,'longitude');
                const hasFix = lat!=null && lat!==0 && lon!=null && lon!==0;
                const color  = _ovColor(n.node_id);
                setTimeout(() => _ovInitMiniMap(mapId, lat, lon, hasFix, color), idx * 30);
            });
        }
    });
};

/* ── Quick DM shortcut — navigate to DMES with node pre-selected ── */
function _ovQuickDM(nodeId) {
    if (typeof window.loadView === 'function') {
        window.loadView('dmes');
        setTimeout(() => {
            if (typeof window.C2CommsApp?.selectContact === 'function') {
                window.C2CommsApp.sendSlotId = window._activeSlotId || 'node_0';
                window.C2CommsApp.selectContact(nodeId);
            }
        }, 250);
    }
}

window.c2ChangePage = function(dir) {
    window.c2CurrentPage += dir;
    window.c2RenderNodes();
};

/* ── Init ── */
window.initOverviewCharts = function() {
    window.c2RenderNodes();
    window.c2RenderFeed();
    window.c2UpdateOverviewStats();
};

/* ── Live SSE push updates shim for app.js compatibility ── */
// Map old IDs used by app.js stat strip to new KPI IDs
var _ovStatMap = {
    'stat-pkt':   'ov-kpi-pkts',
    'stat-nodes': 'ov-kpi-nodes',
    'stat-msg':   'ov-kpi-msgs',
    'stat-err':   'ov-kpi-chs',
};
if (!window._ovStatShimInstalled) {
    window._ovStatShimInstalled = true;
    Object.keys(_ovStatMap).forEach(oldId => {
        if (document.getElementById(oldId)) return;
        const el = document.createElement('span');
        el.id = oldId;
        el.style.display = 'none';
        document.body.appendChild(el);
        const observer = new MutationObserver(() => {
            const newEl = document.getElementById(_ovStatMap[oldId]);
            if (newEl) newEl.textContent = el.textContent;
        });
        observer.observe(el, {childList:true, characterData:true, subtree:true});
    });
}