/* ==========================================================================
 * MeshDash — Map Module v4.0 — MQTT-aware. If you see this in DevTools→Sources, new file loaded.
 * ========================================================================== */

window.C2MapApp = {
    map: null,
    markers: {},
    trajectories: {},
    neighbourLines: {},
    tacticalOverlays: [],
    currentTileLayer: null,
    refreshInterval: null,
    nodeColors: {},
    _activeNodeId: null,
    _panelCharts: {},
    colorPalette: [
        '#00c8f5','#00e87a','#ffa826','#ff3050','#b060ff',
        '#4363d8','#46f0f0','#f032e6','#bcf60c','#fabebe',
        '#008080','#e6194b','#3cb44b','#ffe119','#4363d8'
    ],
    tileLayers: {
        dark: {
            url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
            options: { attribution: '&copy; CARTO', subdomains: 'abcd', maxZoom: 19 }
        },
        satellite: {
            url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            options: { attribution: '&copy; Esri', maxZoom: 18 }
        },
        osm: {
            url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
            options: { attribution: '&copy; OpenStreetMap', maxZoom: 18 }
        },
        offline: {
            // NOTE: offline tiles are Shortbread vector (MVT/protobuf) — rendered via
            // L.vectorGrid.protobuf() in c2ChangeMapStyle, NOT L.tileLayer().
            url: '/api/map/tiles/{z}/{x}/{y}',
            options: { attribution: 'Offline Archive · Shortbread', maxNativeZoom: 14, maxZoom: 18 }
        }
    }
};

/* ── Leaflet CSS overrides ─────────────────────────────────────────────── */
(function() {
    const s = document.createElement('style');
    s.innerHTML = `
        .leaflet-popup-content-wrapper{background:var(--bg2);color:var(--txt);border:1px solid var(--bd2);border-radius:4px;font-family:var(--mono);box-shadow:0 10px 30px rgba(0,0,0,.8);}
        .leaflet-popup-tip{background:var(--bg2);}
        .leaflet-popup-close-button{color:var(--txt3)!important;font-size:16px!important;margin-top:4px;margin-right:4px;}
        .c2-legend{background:rgba(12,20,32,.92);border:1px solid var(--bd2);padding:10px;border-radius:4px;color:var(--txt);font-family:var(--mono);font-size:10px;backdrop-filter:blur(4px);box-shadow:0 4px 15px rgba(0,0,0,.7);max-height:300px;overflow-y:auto;min-width:160px;}
        .c2-legend h4{margin:0 0 8px;color:var(--acc);letter-spacing:1px;font-size:11px;}
        .c2-legend input{background:var(--bg);border:1px solid var(--bd);color:var(--txt);padding:4px;width:100%;margin-bottom:8px;font-family:var(--mono);font-size:9px;box-sizing:border-box;}
        .c2-legend-item{display:flex;align-items:center;gap:8px;margin-bottom:5px;cursor:pointer;padding:2px 4px;border-radius:2px;}
        .c2-legend-item:hover{background:rgba(0,200,245,.08);color:var(--acc);}
        .c2-legend-color{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
        .c2-legend-bat{font-size:8px;margin-left:auto;color:var(--txt3);}
        .leaflet-tile{margin-top:-1px!important;margin-left:-1px!important;width:257px!important;height:257px!important;}
        .leaflet-container{background:var(--bg2)!important;}
        .c2-node-marker{cursor:pointer!important;pointer-events:auto!important;}
        .leaflet-marker-icon.c2-node-marker{pointer-events:auto!important;}
        #c2-sonar-canvas{position:absolute;inset:0;pointer-events:none;z-index:500;}
    `;
    document.head.appendChild(s);
})();

/* ── Helpers ───────────────────────────────────────────────────────────── */
const _pick = (n, ...paths) => {
    for (const p of paths) {
        let v = n;
        for (const k of p.split('.')) v = v?.[k];
        if (v != null) return v;
    }
    return null;
};

const _esc = s => typeof window.escapeHtml === 'function'
    ? window.escapeHtml(String(s||''))
    : String(s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const _fmtTime = ts => ts ? new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
const _fmtDateTime = ts => ts ? new Date(ts*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const _fmtUptime = sec => {
    if (sec==null) return '—';
    if (typeof window.fmtUptime==='function') return window.fmtUptime(sec);
    const d=Math.floor(sec/86400),h=Math.floor((sec%86400)/3600),m=Math.floor((sec%3600)/60);
    return d>0?`${d}d ${h}h`:h>0?`${h}h ${m}m`:`${m}m`;
};
const _nodeLabel = n => _pick(n,'user.longName','user.shortName','long_name','short_name') || n?.node_id || '?';
const _batColor = v => v==null?'var(--txt)':v<20?'var(--err)':v<40?'#ffa826':'var(--ok)';
const _snrColor = v => v==null?'var(--txt)':v<-15?'var(--err)':v<-5?'#ffa826':'var(--ok)';

/* ── Mini Chart ────────────────────────────────────────────────────────── */
const _panelCharts = {};

const _miniAxis = (pos, title, extra={}) => ({
    position:pos, title:{display:true,text:title,color:'#7a96b2',font:{size:8}},
    grid:{color:pos==='left'?'#1e3048':'transparent'}, ticks:{font:{size:8},color:'#7a96b2'}, ...extra
});

const _mkDs = (arr, key, label, color, yID='y', extra={}) => ({
    label, yAxisID:yID, borderColor:color, backgroundColor:color+'25',
    borderWidth:1.5, pointRadius:0, tension:0.3,
    data: arr.map(r=>({x:r.timestamp*1000,y:r[key]})).filter(p=>p.y!=null),
    ...extra
});

function _miniChart(cid, datasets, scales, type='line') {
    if (_panelCharts[cid]) { try{_panelCharts[cid].destroy();}catch(e){} delete _panelCharts[cid]; }
    const canvas = document.getElementById(cid);
    if (!canvas) return false;
    const wrap = canvas.closest('.np-mini-chart') || canvas.parentElement;
    const hasData = datasets.some(ds => ds.data?.length > 0);
    if (!hasData) { if(wrap) wrap.style.display='none'; return false; }
    if(wrap) wrap.style.display='';
    _panelCharts[cid] = new Chart(canvas.getContext('2d'), {
        type, data:{datasets},
        options:{
            responsive:true, maintainAspectRatio:false, animation:false,
            plugins:{
                legend:{labels:{font:{size:8},boxWidth:8,padding:6,color:'#7a96b2'}},
                tooltip:{titleFont:{size:8},bodyFont:{size:8}}
            },
            scales:{x:{type:'time',grid:{color:'#1e3048'},ticks:{font:{size:8},maxTicksLimit:5,color:'#7a96b2'}},...scales}
        }
    });
    return true;
}

/* ── Node type icon ────────────────────────────────────────────────────── */
function _nodeIcon(node) {
    const name = String(_pick(node,'user.longName','long_name')||'').toLowerCase();
    const role = String(node?.role||'').toUpperCase();
    if (role.includes('ROUTER'))                            return '⬡';
    if (name.includes('track')||role.includes('TRACKER'))  return '◉';
    if (name.includes('base')||name.includes('home'))       return '⌂';
    return '◈';
}

/* initC2Map — defined below with MQTT support */

/* c2ChangeMapStyle — defined below */

/* c2ToggleAutoRefresh — defined below with MQTT support */


/* ════════════════════════════════════════════════════════════════════════
 * MQTT FILTER MODAL + LOADING OVERLAY + MQTT DETECTION + MAP INIT
 * ════════════════════════════════════════════════════════════════════════ */

window.c2ChangeMapStyle = function(key) {
    const app = window.C2MapApp;
    if (!app.map) return;
    if (app.currentTileLayer) { app.map.removeLayer(app.currentTileLayer); app.currentTileLayer = null; }

    const effectiveKey = (!navigator.onLine && key !== 'offline')
        ? (() => { window.triggerToast?.('Offline mode active — switching to local archive', 'warn'); return 'offline'; })()
        : key;

    if (effectiveKey === 'offline') {
        fetch('/api/map/status').then(r => r.json()).then(data => {
            if (!data.available || !data.active_file) {
                window.triggerToast?.('No offline map loaded. Open MAPS panel to download or upload an .mbtiles archive.', 'warn');
                const fallback = app.tileLayers.dark;
                app.currentTileLayer = L.tileLayer(fallback.url, fallback.options).addTo(app.map);
                return;
            }
            const vectorStyle = {
                'earth':              () => ({ fill: true, fillColor: '#0b1320', fillOpacity: 1, weight: 0, color: '#0b1320' }),
                'land':               () => ({ fill: true, fillColor: '#0b1320', fillOpacity: 1, weight: 0, color: '#0b1320' }),
                'landcover':          () => ({ fill: true, fillColor: '#0d1a0d', fillOpacity: 1, weight: 0, color: '#0d1a0d' }),
                'water':              () => ({ fill: true, fillColor: '#0d2135', fillOpacity: 1, weight: 0, color: '#0d2135' }),
                'ocean':              () => ({ fill: true, fillColor: '#0d2135', fillOpacity: 1, weight: 0, color: '#0d2135' }),
                'waterway':           () => ({ fill: false, color: '#1a3a5c', weight: 1, opacity: 0.8 }),
                'roads':              (p) => { const k=(p&&(p.kind||p.highway||p.class))||''; if(/motorway|trunk/.test(k)) return {color:'#2a5080',weight:2.5,opacity:0.9,fill:false}; if(/primary|secondary/.test(k)) return {color:'#1e3a5c',weight:1.8,opacity:0.85,fill:false}; return {color:'#162a3e',weight:1,opacity:0.7,fill:false}; },
                'road':               (p) => { const k=(p&&(p.kind||p.highway||p.class))||''; if(/motorway|trunk/.test(k)) return {color:'#2a5080',weight:2.5,opacity:0.9,fill:false}; if(/primary|secondary/.test(k)) return {color:'#1e3a5c',weight:1.8,opacity:0.85,fill:false}; return {color:'#162a3e',weight:1,opacity:0.7,fill:false}; },
                'streets':            () => ({ color: '#162a3e', weight: 0.8, opacity: 0.7, fill: false }),
                'street_labels':      () => ({ weight: 0, opacity: 0, fill: false }),
                'paths':              () => ({ color: '#0f1e2e', weight: 0.6, opacity: 0.6, fill: false }),
                'railways':           () => ({ color: '#1e2e40', weight: 1, opacity: 0.6, fill: false, dashArray: '4,4' }),
                'buildings':          () => ({ fill: true, fillColor: '#0f1a28', fillOpacity: 0.85, color: '#162338', weight: 0.5 }),
                'building':           () => ({ fill: true, fillColor: '#0f1a28', fillOpacity: 0.85, color: '#162338', weight: 0.5 }),
                'boundaries':         () => ({ fill: false, color: '#2a4a6a', weight: 1, opacity: 0.7, dashArray: '5,4' }),
                'boundary':           () => ({ fill: false, color: '#2a4a6a', weight: 1, opacity: 0.7, dashArray: '5,4' }),
                'country_boundaries': () => ({ fill: false, color: '#3a5a7a', weight: 1.5, opacity: 0.8, dashArray: '6,4' }),
                'places':             () => ({ weight: 0, opacity: 0, fill: false, fillOpacity: 0 }),
                'place_labels':       () => ({ weight: 0, opacity: 0, fill: false, fillOpacity: 0 }),
                'sites':              () => ({ fill: true, fillColor: '#0e1c2e', fillOpacity: 0.5, weight: 0, color: '#0e1c2e' }),
                'pois':               () => ({ weight: 0, opacity: 0, fill: false, fillOpacity: 0 }),
            };
            const defaultVectorStyle = () => ({ fill: true, fillColor: '#0b1320', fillOpacity: 0.5, color: '#1e3048', weight: 0.5, opacity: 0.6 });
            const styleProxy = new Proxy(vectorStyle, { get(t, p) { return p in t ? t[p] : defaultVectorStyle; } });

            app.currentTileLayer = L.vectorGrid.protobuf('/api/map/tiles/{z}/{x}/{y}', {
                vectorTileLayerStyles: styleProxy,
                maxNativeZoom: 14, maxZoom: 18, minZoom: 0,
                attribution: 'Offline Archive · Shortbread',
                interactive: false,
                rendererFactory: L.canvas.tile,
            });

            // VectorGrid's bundled pbf throws "Unimplemented type: 3/7" for
            // protobuf group wire types used in Shortbread tiles. The error is
            // thrown inside a fetch().then() chain inside createTile, becoming an
            // unhandled rejection. Wrap createTile to swallow only these errors.
            const _origCreateTile = app.currentTileLayer.createTile.bind(app.currentTileLayer);
            app.currentTileLayer.createTile = function(coords, done) {
                return _origCreateTile(coords, function(err, tile) {
                    if (err && err.message && err.message.includes('Unimplemented type')) {
                        return done(null, tile); // treat as success — partial render is fine
                    }
                    done(err, tile);
                });
            };

            app.currentTileLayer.addTo(app.map);
            window.triggerToast?.('Offline map active: ' + data.active_file, 'ok');
        }).catch(() => {
            window.triggerToast?.('Failed to reach /api/map/status', 'err');
            const fallback = app.tileLayers.dark;
            app.currentTileLayer = L.tileLayer(fallback.url, fallback.options).addTo(app.map);
        });
        return;
    }
    const style = app.tileLayers[effectiveKey] || app.tileLayers.dark;
    app.currentTileLayer = L.tileLayer(style.url, style.options).addTo(app.map);
};

window.addEventListener('offline', () => window.c2ChangeMapStyle(document.getElementById('map-style')?.value || 'dark'));
window.addEventListener('online', () => window.c2ChangeMapStyle(document.getElementById('map-style')?.value || 'dark'));

const _mqttFilter = (() => {
    const DEFAULTS = { maxNodes:200, maxAgeDays:7, showTrails:false, showNeighbors:false, nameFilter:'', onlyWithGps:false };
    let _onConfirm=null, _onCancel=null;
    function _inject() {
        if (document.getElementById('mqtt-filter-overlay')) return;
        const s=document.createElement('style'); s.id='mqtt-filter-style';
        s.textContent='#mqtt-filter-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:30000;display:flex;align-items:center;justify-content:center;}#mqtt-filter-box{background:var(--bg1,#0b1320);border:1px solid var(--acc,#00c8f5);border-radius:6px;width:420px;max-width:95vw;font-family:var(--mono,monospace);box-shadow:0 20px 60px rgba(0,0,0,.9);}#mqtt-filter-box h2{margin:0;padding:14px 18px;font-size:12px;letter-spacing:2px;color:var(--acc,#00c8f5);border-bottom:1px solid var(--bd2,#1e3048);display:flex;align-items:center;gap:10px;}.mff-body{padding:16px 18px;display:flex;flex-direction:column;gap:12px;}.mff-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}.mff-label{font-size:10px;color:var(--txt2,#8aa0b8);flex:1;}.mff-hint{font-size:8px;color:var(--txt3,#4a6a88);margin-top:2px;}.mff-inp{background:var(--bg,#080f1a);border:1px solid var(--bd,#162338);color:var(--txt,#c8d8e8);padding:5px 8px;font-family:inherit;font-size:10px;border-radius:3px;width:90px;text-align:right;}.mff-inp.wide{width:140px;text-align:left;}.mff-sep{border:none;border-top:1px solid var(--bd,#162338);margin:4px 0;}.mff-footer{padding:12px 18px;border-top:1px solid var(--bd2,#1e3048);display:flex;gap:8px;justify-content:flex-end;}.mff-warn{font-size:9px;color:#ffa826;padding:8px 12px;background:rgba(255,168,38,.08);border-radius:3px;border-left:2px solid #ffa826;line-height:1.5;}.mff-preset-row{display:flex;gap:6px;flex-wrap:wrap;}.mff-preset{background:var(--bg,#080f1a);border:1px solid var(--bd,#162338);color:var(--txt3,#4a6a88);padding:4px 10px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:9px;}';
        document.head.appendChild(s);
        const overlay=document.createElement('div'); overlay.id='mqtt-filter-overlay';
        overlay.innerHTML=`<div id="mqtt-filter-box"><h2><i class="fas fa-tower-broadcast"></i> MQTT MAP FILTER <span style="margin-left:auto;font-size:9px;color:var(--txt3)">CONFIGURE BEFORE LOADING</span></h2><div class="mff-body"><div class="mff-warn">⚠ This MQTT server streams a large mesh. Adjust filters to avoid overloading your browser.</div><div style="font-size:9px;color:var(--acc);letter-spacing:1px;margin-bottom:-4px;">QUICK PRESETS</div><div class="mff-preset-row"><button class="mff-preset" onclick="_mqttFilter.applyPreset('minimal')">⚡ MINIMAL (50 nodes)</button><button class="mff-preset" onclick="_mqttFilter.applyPreset('balanced')">⚖ BALANCED (200 nodes)</button><button class="mff-preset" onclick="_mqttFilter.applyPreset('full')">🌐 FULL (500+trails)</button></div><hr class="mff-sep"><div class="mff-row"><div><div class="mff-label">MAX NODES</div><div class="mff-hint">Cap — oldest-heard dropped first</div></div><input class="mff-inp" id="mff-maxNodes" type="number" min="10" max="2000" value="200"></div><div class="mff-row"><div><div class="mff-label">MAX AGE (DAYS)</div><div class="mff-hint">0 = no filter</div></div><input class="mff-inp" id="mff-maxAgeDays" type="number" min="0" max="365" value="7"></div><div class="mff-row"><div><div class="mff-label">NAME FILTER</div><div class="mff-hint">Blank = all nodes</div></div><input class="mff-inp wide" id="mff-nameFilter" type="text" placeholder="e.g. UK or !aabb" value=""></div><hr class="mff-sep"><div class="mff-row"><div><div class="mff-label">TRAIL HISTORY</div><div class="mff-hint">1 API call per node — slow</div></div><label class="tog" style="flex-shrink:0"><input type="checkbox" id="mff-showTrails"><span class="tog-sl"></span></label></div><div class="mff-row"><div><div class="mff-label">NEIGHBOUR LINKS</div><div class="mff-hint">RF link lines</div></div><label class="tog" style="flex-shrink:0"><input type="checkbox" id="mff-showNeighbors"><span class="tog-sl"></span></label></div><div class="mff-row"><div><div class="mff-label">GPS NODES ONLY</div><div class="mff-hint">Skip nodes with no position</div></div><label class="tog" style="flex-shrink:0"><input type="checkbox" id="mff-onlyWithGps"><span class="tog-sl"></span></label></div></div><div class="mff-footer"><button class="btn btn-sm" onclick="_mqttFilter.cancel()">✕ CANCEL</button><button class="btn btn-sm btn-acc" onclick="_mqttFilter.confirm()">▶ LOAD MAP</button></div></div>`;
        document.body.appendChild(overlay);
    }
    function _read() { return { maxNodes:Math.max(1,parseInt(document.getElementById('mff-maxNodes')?.value||200)), maxAgeDays:Math.max(0,parseInt(document.getElementById('mff-maxAgeDays')?.value||7)), nameFilter:(document.getElementById('mff-nameFilter')?.value||'').trim().toLowerCase(), showTrails:document.getElementById('mff-showTrails')?.checked||false, showNeighbors:document.getElementById('mff-showNeighbors')?.checked||false, onlyWithGps:document.getElementById('mff-onlyWithGps')?.checked!==false }; }
    function _hide() { document.getElementById('mqtt-filter-overlay')?.remove(); }
    return {
        show() { return new Promise((resolve,reject)=>{ _onConfirm=resolve; _onCancel=reject; _inject(); const set=(id,v)=>{const el=document.getElementById(id);if(el){el.type==='checkbox'?(el.checked=!!v):(el.value=v);}}; const f=DEFAULTS; set('mff-maxNodes',f.maxNodes);set('mff-maxAgeDays',f.maxAgeDays);set('mff-nameFilter',f.nameFilter);set('mff-showTrails',f.showTrails);set('mff-showNeighbors',f.showNeighbors);set('mff-onlyWithGps',f.onlyWithGps); }); },
        confirm() { const cfg=_read(); _hide(); if(_onConfirm){_onConfirm(cfg);_onConfirm=null;_onCancel=null;} },
        cancel()  { _hide(); if(_onCancel){_onCancel(new Error('cancelled'));_onConfirm=null;_onCancel=null;} },
        applyPreset(name) { const p={minimal:{maxNodes:50,maxAgeDays:3,showTrails:false,showNeighbors:false},balanced:{maxNodes:200,maxAgeDays:7,showTrails:false,showNeighbors:false},full:{maxNodes:500,maxAgeDays:30,showTrails:true,showNeighbors:true}}[name]||{}; const set=(id,v)=>{const el=document.getElementById(id);if(el){el.type==='checkbox'?(el.checked=!!v):(el.value=v);}}; if(p.maxNodes!==undefined)set('mff-maxNodes',p.maxNodes);if(p.maxAgeDays!==undefined)set('mff-maxAgeDays',p.maxAgeDays);if(p.showTrails!==undefined)set('mff-showTrails',p.showTrails);if(p.showNeighbors!==undefined)set('mff-showNeighbors',p.showNeighbors); },
        DEFAULTS,
    };
})();

const _mapLoad = {
    show(mode) {
        const el=document.getElementById('map-loading'); if(!el) return;
        el.classList.remove('fading'); el.style.display='flex';
        const icon=document.getElementById('map-load-icon'),title=document.getElementById('map-load-title'),sub=document.getElementById('map-load-sub'),bar=document.getElementById('map-load-bar'),count=document.getElementById('map-load-count');
        if(mode==='mqtt'){ if(icon)icon.className='fas fa-tower-broadcast fa-pulse'; if(title)title.textContent='SCANNING MQTT MESH...'; if(sub)sub.textContent='APPLYING FILTERS AND PLOTTING NODES'; if(bar)bar.style.width='30%'; if(count)count.textContent=''; }
        else{ if(icon)icon.className='fas fa-satellite-dish fa-spin'; if(title)title.textContent='ACQUIRING TELEMETRY...'; if(sub)sub.textContent='LOADING MAP DATA'; if(bar)bar.style.width='30%'; if(count)count.textContent=''; }
    },
    hide() { const el=document.getElementById('map-loading'); if(!el||el.style.display==='none') return; const bar=document.getElementById('map-load-bar'); if(bar)bar.style.width='100%'; el.classList.add('fading'); setTimeout(()=>{if(el.classList.contains('fading')){el.style.display='none';el.classList.remove('fading');}},420); },
    forceHide() { const el=document.getElementById('map-loading'); if(el){el.style.display='none';el.classList.remove('fading');} }
};

async function _isActiveMqttSlot() {
    const sid=window._activeSlotId;
    if(!sid||sid==='node_0'||sid==='all'){console.log('[Map] not MQTT: sid='+sid);return false;}
    const known=window._knownSlots||{};
    if(known[sid]){const ct=(known[sid].connection_type||'').toUpperCase();console.log('[Map] MQTT check: '+sid+' ct='+ct+' (cache)');return ct==='MQTT';}
    console.log('[Map] MQTT check: fetching /api/slots for '+sid);
    try{const r=await fetch('/api/slots',{cache:'no-store'});if(!r.ok)return false;const slots=await r.json();window._knownSlots=Object.assign(window._knownSlots||{},slots);const ct=(slots[sid]?.connection_type||'').toUpperCase();console.log('[Map] MQTT check fetched: '+sid+' ct='+ct);return ct==='MQTT';}catch(e){return false;}
}

function _showMqttBanner(plottedCount, totalRaw) {
    document.getElementById('mqtt-map-banner')?.remove();
    const b=document.createElement('div'); b.id='mqtt-map-banner';
    b.style.cssText='position:absolute;bottom:12px;left:50%;transform:translateX(-50%);z-index:2000;background:rgba(6,11,18,.92);border:1px solid #ffa826;color:#ffa826;font-family:var(--mono,monospace);font-size:10px;padding:8px 16px;border-radius:4px;display:flex;align-items:center;gap:12px;box-shadow:0 4px 20px rgba(0,0,0,.7);pointer-events:auto;white-space:nowrap;max-width:90vw;';
    const countStr = (plottedCount != null && totalRaw != null)
        ? ` <span style="color:var(--txt3);margin:0 4px;">|</span> <span style="color:var(--txt2);">${plottedCount} plotted of ${totalRaw} nodes</span>`
        : '';
    b.innerHTML=`<i class="fas fa-tower-broadcast" style="font-size:13px;flex-shrink:0;"></i><span><b>MQTT MODE</b> — Static snapshot. Auto-refresh disabled.</span>${countStr}<button onclick="location.reload()" style="background:rgba(255,168,38,.15);border:1px solid #ffa826;color:#ffa826;padding:3px 10px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:9px;letter-spacing:1px;margin-left:8px;">↻ RELOAD</button>`;
    const mapEl=document.getElementById('main-c2-map'); if(mapEl)mapEl.appendChild(b);
}
function _hideMqttBanner(){document.getElementById('mqtt-map-banner')?.remove();}

function _snapshotNodes(allNodes,filter){
    if(!filter)return allNodes;
    const now=Date.now()/1000,ageCutoff=filter.maxAgeDays>0?(now-filter.maxAgeDays*86400):0,nameQ=(filter.nameFilter||'').toLowerCase();
    const sorted=Object.entries(allNodes).sort(([,a],[,b])=>((b.lastHeard||b.last_heard||0)-(a.lastHeard||a.last_heard||0)));
    const out={};
    for(const [id,n] of sorted){
        if(Object.keys(out).length>=filter.maxNodes)break;
        if(ageCutoff>0&&(n.lastHeard||n.last_heard||0)<ageCutoff)continue;
        if(nameQ){const nm=(_pick(n,'user.longName','user.shortName','long_name','short_name')||n.node_id||'').toLowerCase();if(!nm.includes(nameQ))continue;}
        if(filter.onlyWithGps){
            // Check all known position paths — MQTT packets store under position.latitude,
            // DB-loaded nodes also have raw latitude scalar column.
            const lat=_pick(n,'position.latitude','position_info.latitude','latitude');
            const lon=_pick(n,'position.longitude','position_info.longitude','longitude');
            if(typeof lat!=='number'||typeof lon!=='number'||isNaN(lat)||isNaN(lon)||(lat===0&&lon===0))continue;
        }
        out[id]=n;
    }
    return out;
}

function _clearMapLayers(){
    const app=window.C2MapApp;
    Object.values(app.markers).flat().forEach(m=>{try{app.map.removeLayer(m);}catch(e){}});
    Object.values(app.trajectories).forEach(t=>{try{app.map.removeLayer(t);}catch(e){}});
    Object.values(app.neighbourLines).forEach(l=>{try{app.map.removeLayer(l);}catch(e){}});
    app.markers={}; app.trajectories={}; app.neighbourLines={};
}

async function _mqttDrawOnce(){
    const app=window.C2MapApp,sid=window._activeSlotId;
    clearInterval(app.refreshInterval); app.refreshInterval=null;
    app._mqttSlotFlag=sid; // block c2FetchAndDrawMap immediately
    let filterCfg;
    try{filterCfg=await _mqttFilter.show();}catch(e){_mapLoad.forceHide();_showMqttBanner();return;}
    app._mqttFilter=filterCfg; app._initialLoad=true;
    _mapLoad.show('mqtt');
    const raw=window.meshState?.nodes||{};
    app._mqttNodeSnapshot=_snapshotNodes(raw,filterCfg);
    const totalRaw=Object.keys(raw).length;
    const snapshotCount=Object.keys(app._mqttNodeSnapshot).length;
    console.log('[Map] MQTT drawing '+snapshotCount+' of '+totalRaw+' nodes (filter: onlyGps='+filterCfg.onlyWithGps+')');
    try{await window.c2FetchAndDrawMap();}catch(e){console.error('MQTT draw:',e);}
    _mapLoad.hide();

    // Count how many actually plotted (had valid GPS)
    const plottedCount=Object.keys(window.C2MapApp.markers).length;
    _showMqttBanner(plottedCount, totalRaw);

    const autoWrap=document.getElementById('map-auto-ref')?.closest('div');
    if(autoWrap)autoWrap.style.display='none';
}

window.c2InitTacticalDragDrop = function() {
    const mapEl = document.getElementById('main-c2-map');
    if (!mapEl) return;
    mapEl.addEventListener('dragover', e => { e.preventDefault(); e.stopPropagation(); mapEl.style.border = '2px dashed var(--acc)'; });
    mapEl.addEventListener('dragleave', e => { e.preventDefault(); e.stopPropagation(); mapEl.style.border = 'none'; });
    mapEl.addEventListener('drop', async e => {
        e.preventDefault();
        e.stopPropagation();
        mapEl.style.border = 'none';
        if (!e.dataTransfer.files || !e.dataTransfer.files.length) return;
        const file = e.dataTransfer.files[0];
        const ext = file.name.split('.').pop().toLowerCase();
        const reader = new FileReader();
        reader.onload = async (ev) => {
            let geojsonData = null;
            if (ext === 'geojson' || ext === 'json') {
                geojsonData = JSON.parse(ev.target.result);
            } else if (ext === 'kml') {
                const parser = new DOMParser();
                const xmlDoc = parser.parseFromString(ev.target.result, "text/xml");
                geojsonData = await window.c2ParseKML(xmlDoc);
            } else if (ext === 'kmz') {
                if (!window.JSZip) return window.triggerToast?.('JSZip required for KMZ', 'err');
                const zip = await JSZip.loadAsync(file);
                const kmlFile = Object.values(zip.files).find(f => f.name.toLowerCase().endsWith('.kml'));
                if (kmlFile) {
                    const kmlText = await kmlFile.async('text');
                    const xmlDoc = new DOMParser().parseFromString(kmlText, "text/xml");
                    geojsonData = await window.c2ParseKML(xmlDoc);
                }
            }
            if (geojsonData) {
                const layer = L.geoJSON(geojsonData, { style: { color: '#ff3050', weight: 2, fillOpacity: 0.2 } }).addTo(window.C2MapApp.map);
                window.C2MapApp.tacticalOverlays.push({ name: file.name, layer: layer });
                window.C2MapApp.map.fitBounds(layer.getBounds());
                window.triggerToast?.(`Tactical Overlay Loaded: ${file.name}`, 'ok');
                window.c2RenderOverlayList?.();
            }
        };
        if (ext === 'kmz') reader.readAsArrayBuffer(file);
        else reader.readAsText(file);
    });
};

window.c2ToggleOverlayDrawer = function() {
    const d = document.getElementById('map-overlay-drawer');
    if (d) d.style.maxHeight = d.style.maxHeight === '200px' ? '0' : '200px';
    window.c2RenderOverlayList();
};

window.c2RenderOverlayList = function() {
    const list = document.getElementById('overlay-items');
    const badge = document.getElementById('map-overlay-badge');
    if (!list) return;
    if (window.C2MapApp.tacticalOverlays.length === 0) {
        list.innerHTML = '<div style="font-size: 9px; color: var(--txt3);">No offline vector files loaded. Drop a file onto the map.</div>';
        if (badge) badge.style.display = 'none';
        return;
    }
    list.innerHTML = '';
    let activeCount = 0;
    window.C2MapApp.tacticalOverlays.forEach((overlay, idx) => {
        const isVisible = window.C2MapApp.map.hasLayer(overlay.layer);
        if (isVisible) activeCount++;
        list.innerHTML += `<div style="display:flex; align-items:center; justify-content:space-between; background:var(--bg, #080f1a); padding:6px 10px; border:1px solid var(--bd, #162338); border-radius:3px;">
            <label style="display:flex; align-items:center; gap:8px; font-size:10px; color:var(--txt); cursor:pointer; margin:0;">
                <input type="checkbox" ${isVisible ? 'checked' : ''} onchange="window.c2ToggleOverlay(${idx}, this.checked)">
                ${window.escapeHtml(overlay.name)}
            </label>
            <button class="btn btn-sm" style="border:1px solid var(--bd2); background:transparent; color:#ff3050; padding:2px 8px; font-size:9px;" onclick="window.c2RemoveOverlay(${idx})">✕ REMOVE</button>
        </div>`;
    });
    if (badge) {
        badge.textContent = activeCount;
        badge.style.display = activeCount > 0 ? 'inline-block' : 'none';
    }
};

window.c2ToggleOverlay = function(idx, show) {
    const overlay = window.C2MapApp.tacticalOverlays[idx];
    if (show) overlay.layer.addTo(window.C2MapApp.map);
    else window.C2MapApp.map.removeLayer(overlay.layer);
    window.c2RenderOverlayList();
};

window.c2RemoveOverlay = function(idx) {
    const overlay = window.C2MapApp.tacticalOverlays[idx];
    if (window.C2MapApp.map.hasLayer(overlay.layer)) window.C2MapApp.map.removeLayer(overlay.layer);
    window.C2MapApp.tacticalOverlays.splice(idx, 1);
    window.c2RenderOverlayList();
};

window.c2ParseKML = async function(xmlDoc) {
    const geojson = { type: "FeatureCollection", features: [] };
    const placemarks = xmlDoc.getElementsByTagName("Placemark");
    for (let i = 0; i < placemarks.length; i++) {
        const node = placemarks[i];
        const name = node.getElementsByTagName("name")[0]?.textContent || "";
        const poly = node.getElementsByTagName("Polygon")[0];
        const line = node.getElementsByTagName("LineString")[0];
        const point = node.getElementsByTagName("Point")[0];
        let coords = [];
        let type = "";
        if (poly) {
            type = "Polygon";
            const coordStr = poly.getElementsByTagName("coordinates")[0]?.textContent.trim();
            coords = [coordStr.split(/\s+/).map(c => c.split(',').map(Number).slice(0, 2))];
        } else if (line) {
            type = "LineString";
            const coordStr = line.getElementsByTagName("coordinates")[0]?.textContent.trim();
            coords = coordStr.split(/\s+/).map(c => c.split(',').map(Number).slice(0, 2));
        } else if (point) {
            type = "Point";
            const coordStr = point.getElementsByTagName("coordinates")[0]?.textContent.trim();
            coords = coordStr.split(',').map(Number).slice(0, 2);
        }
        if (type) geojson.features.push({ type: "Feature", properties: { name }, geometry: { type, coordinates: coords } });
    }
    return geojson;
};

window.initC2Map = async function(){
    const app=window.C2MapApp;
    console.group('[Map] initC2Map — START');
    console.log('[Map] slot='+window._activeSlotId+' | meshState.nodes count='+Object.keys(window.meshState?.nodes||{}).length);

    if(app.map){
        console.log('[Map] Destroying existing map instance');
        app.map.remove(); app.map=null;
    }

    // Defer Leaflet init by two rAFs so the browser finishes painting the flex
    // layout (calc(100vh - header - terminal) chain) before Leaflet measures the
    // container. Without this, innerHTML-injected views report 0×0 and Leaflet
    // renders a blank canvas even though data arrives correctly.
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

    const mapEl=document.getElementById('main-c2-map');
    if(!mapEl){ console.error('[Map] #main-c2-map NOT FOUND after rAF — aborting'); console.groupEnd(); return; }

    const rect=mapEl.getBoundingClientRect();
    console.log('[Map] #main-c2-map dimensions after rAF: '+Math.round(rect.width)+'×'+Math.round(rect.height)+'px');
    if(rect.width===0||rect.height===0){
        console.error('[Map] ⚠️  Container is zero-size — tiles will not render. Check flex layout chain.');
    }

    app.map=L.map('main-c2-map',{preferCanvas:true,zoomControl:true}).setView([20,0],2);
    app.currentTileLayer=L.tileLayer(app.tileLayers.dark.url,app.tileLayers.dark.options).addTo(app.map);
    app.map.invalidateSize({animate:false});
    console.log('[Map] Leaflet map created. Map size: '+JSON.stringify(app.map.getSize()));

    app.map.on('resize zoomend moveend',()=>window.C2SonarPing?.resize());
    app._initialLoad=true; app._mqttSlotFlag=null;

    mapEl.addEventListener('click',e=>{let el=e.target;for(let i=0;i<6&&el&&el!==mapEl;i++){if(el.dataset?.nodeid){window.c2OpenPanel(el.dataset.nodeid);return;}el=el.parentElement;}},true);
    clearInterval(app.refreshInterval); app.refreshInterval=null;

    window.c2InitTacticalDragDrop();

    const isMqtt=await _isActiveMqttSlot();
    console.log('[Map] MQTT slot check: '+isMqtt);
    console.groupEnd();

    if(isMqtt){
        await _mqttDrawOnce();
    } else {
        _hideMqttBanner(); app._mqttFilter=null;
        _mapLoad.show('normal');
        await window.c2FetchAndDrawMap();
        try{app.map.invalidateSize({animate:false});}catch(e){}
        _mapLoad.hide();
        const autoEl=document.getElementById('map-auto-ref');
        if(autoEl?.checked!==false){app.refreshInterval=setInterval(window.c2FetchAndDrawMap,30000);}
    }
};

window.c2ManualRefresh = async function(){
    const app=window.C2MapApp; if(!app.map)return;
    if(await _isActiveMqttSlot()){clearInterval(app.refreshInterval);app.refreshInterval=null;_clearMapLayers();app.nodeColors={};app._mqttNodeSnapshot=null;await _mqttDrawOnce();}
    else{app._initialLoad=true;_mapLoad.show('normal');window.c2FetchAndDrawMap().then(()=>_mapLoad.hide()).catch(()=>_mapLoad.forceHide());}
};

window.c2ResetMapForSlotSwitch = async function(){
    const app=window.C2MapApp; if(!app.map)return;
    clearInterval(app.refreshInterval); app.refreshInterval=null; app._mqttSlotFlag=null;
    _clearMapLayers(); app.nodeColors={}; app._mqttNodeSnapshot=null; app._initialLoad=true; _hideMqttBanner();
    const isMqtt=await _isActiveMqttSlot();
    if(isMqtt){await _mqttDrawOnce();}
    else{app._mqttFilter=null;_mapLoad.show('normal');const autoWrap=document.getElementById('map-auto-ref')?.closest('div');if(autoWrap)autoWrap.style.display='';window.c2FetchAndDrawMap().then(()=>{_mapLoad.hide();const autoEl=document.getElementById('map-auto-ref');if(autoEl?.checked){app.refreshInterval=setInterval(window.c2FetchAndDrawMap,30000);}}).catch(()=>_mapLoad.forceHide());}
};

window.c2ToggleAutoRefresh = function(el){
    const app=window.C2MapApp,sid=window._activeSlotId;
    const isMqttSync=sid&&sid!=='node_0'&&sid!=='all'&&((window._knownSlots?.[sid]?.connection_type)||'').toUpperCase()==='MQTT';
    if(isMqttSync||app._mqttSlotFlag===sid){el.checked=false;window.triggerToast?.('Auto-Refresh disabled for MQTT','warn');return;}
    clearInterval(app.refreshInterval); app.refreshInterval=null;
    if(el.checked){app.refreshInterval=setInterval(window.c2FetchAndDrawMap,30000);window.triggerToast?.('Auto-Refresh ON (30s)','ok');}
    else{window.triggerToast?.('Auto-Refresh OFF','warn');}
};


/* ── Main draw ─────────────────────────────────────────────────────────── */
window.c2FetchAndDrawMap = async function() {
    const app = window.C2MapApp;
    if (!app.map) { console.warn('[Map] c2FetchAndDrawMap: no map instance, skipping'); return; }

    const _gsid = window._activeSlotId;
    const _mqttBlocked = app._mqttSlotFlag === _gsid ||
        (_gsid && _gsid !== 'node_0' && _gsid !== 'all' &&
         ((window._knownSlots?.[_gsid]?.connection_type)||'').toUpperCase() === 'MQTT');
    if (_mqttBlocked) {
        clearInterval(app.refreshInterval); app.refreshInterval=null;
        app._drawing=false; app._drawPending=false;
        console.warn('[Map] c2FetchAndDrawMap BLOCKED — MQTT slot. Interval killed.');
        return;
    }

    if (app._drawing) { app._drawPending = true; return; }
    app._drawing = true;
    app._drawPending = false;

    const isFirstLoad = !!app._initialLoad;
    app._initialLoad = false;

    const filter = app._mqttFilter || null;
    const isMqttCtx = !!(app._mqttSlotFlag);
    let nodes;
    if (isMqttCtx && app._mqttNodeSnapshot) {
        nodes = app._mqttNodeSnapshot;
        app._mqttNodeSnapshot = null;
    } else if (isMqttCtx && filter) {
        nodes = _snapshotNodes(window.meshState?.nodes||{}, filter);
    } else {
        nodes = window.meshState?.nodes || {};
    }

    // ── DIAGNOSTIC BLOCK ──────────────────────────────────────────────────
    console.group('[Map] c2FetchAndDrawMap — isFirstLoad='+isFirstLoad);
    const _totalNodes = Object.keys(nodes).length;
    console.log('[Map] meshState.nodes total: '+_totalNodes);

    if (_totalNodes === 0) {
        console.warn('[Map] ⚠️  meshState.nodes is EMPTY — no nodes to plot. SSE may not have delivered a "nodes" event yet, or the backend returned an empty list.');
    } else {
        // Sample up to 3 nodes to show their structure
        const _sample = Object.entries(nodes).slice(0, 3);
        _sample.forEach(([id, n]) => {
            const lat = _pick(n,'position.latitude','position_info.latitude','latitude');
            const lon = _pick(n,'position.longitude','position_info.longitude','longitude');
            const hasPos = typeof lat==='number' && typeof lon==='number' && !isNaN(lat) && !isNaN(lon) && !(lat===0&&lon===0);
            console.log('[Map] node sample — id='+id+' lat='+lat+' lon='+lon+' hasGPS='+hasPos, n);
        });
    }

    const _mapSize = app.map.getSize();
    console.log('[Map] Leaflet map size at draw time: '+_mapSize.x+'×'+_mapSize.y+'px');
    if (_mapSize.x === 0 || _mapSize.y === 0) {
        console.error('[Map] ⚠️  Map is ZERO SIZE at draw time — tiles and markers will be invisible. invalidateSize() will be called but layout needs fixing.');
        app.map.invalidateSize({animate:false});
    }
    // ── END DIAGNOSTIC BLOCK ──────────────────────────────────────────────

    const showTrails    = isMqttCtx ? !!(filter?.showTrails)    : true;
    const showNeighbors = isMqttCtx ? !!(filter?.showNeighbors) : true;

    try {
        try { app.map.invalidateSize(); } catch(e) {}
        let colorIdx = Object.keys(app.nodeColors).length;
        const _mapSQ = (window._activeSlotId && window._activeSlotId !== 'node_0') ? `?slot_id=${encodeURIComponent(window._activeSlotId)}` : '';

        let allNeighbours = [];
        if (showNeighbors) {
            try { allNeighbours = await fetch('/api/neighbors'+_mapSQ).then(r=>r.ok?r.json():[]); } catch(e){}
        }

        const _hasGps = (n) => {
            const lat = _pick(n,
                'position.latitude',
                'position_info.latitude',
                'latitude',
            );
            const lon = _pick(n,
                'position.longitude',
                'position_info.longitude',
                'longitude',
            );
            return typeof lat === 'number' && typeof lon === 'number' &&
                   !isNaN(lat) && !isNaN(lon) && !(lat === 0 && lon === 0);
        };

        const allNodeEntries = Object.entries(nodes);
        const withGps    = allNodeEntries.filter(([,n])=>_hasGps(n));
        const withoutGps = allNodeEntries.filter(([,n])=>!_hasGps(n));
        console.log('[Map] Nodes with valid GPS: '+withGps.length+' / '+allNodeEntries.length+' total');
        if (withoutGps.length > 0) {
            console.log('[Map] Nodes WITHOUT GPS (will not be plotted):',
                withoutGps.slice(0,10).map(([id,n])=>{
                    return id+' lat='+_pick(n,'position.latitude','position_info.latitude','latitude')+
                           ' lon='+_pick(n,'position.longitude','position_info.longitude','longitude');
                })
            );
        }

        const newNodeIds = new Set(withGps.map(([id])=>id));

        Object.keys(app.markers).filter(id=>!newNodeIds.has(id)).forEach(nodeId=>{
            app.markers[nodeId]?.forEach(m=>{try{app.map.removeLayer(m);}catch(e){}});
            if(app.trajectories[nodeId]){try{app.map.removeLayer(app.trajectories[nodeId]);}catch(e){}}
            delete app.markers[nodeId]; delete app.trajectories[nodeId];
        });
        Object.values(app.neighbourLines).forEach(l=>{try{app.map.removeLayer(l);}catch(e){}});
        app.neighbourLines={};

        let plotted=0, updated=0;
        const newNodeQueue=[];
        for (const [nodeId, node] of allNodeEntries) {
            try {
                const lat=_pick(node,'position.latitude','position_info.latitude','latitude');
                const lon=_pick(node,'position.longitude','position_info.longitude','longitude');
                if(typeof lat!=='number'||typeof lon!=='number'||isNaN(lat)||isNaN(lon)||(lat===0&&lon===0))continue;
                if(!app.nodeColors[nodeId]){app.nodeColors[nodeId]=app.colorPalette[colorIdx%app.colorPalette.length];colorIdx++;}
                const color=app.nodeColors[nodeId];
                const bat=_pick(node,'deviceMetrics.batteryLevel','battery_level');
                const lh=node.lastHeard||node.last_heard, isOnline=lh&&(Date.now()/1000-lh)<3600;
                const iconHtml=`<div data-nodeid="${nodeId}" style="position:relative;width:34px;height:34px;cursor:pointer;">${isOnline?`<div style="position:absolute;inset:-4px;border-radius:50%;border:2px solid ${color};opacity:.35;animation:pulse 2s infinite;pointer-events:none;"></div>`:''}<div style="background:${color};width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:2px solid rgba(0,0,0,.5);color:#0a0f18;font-size:15px;font-weight:900;box-shadow:0 0 12px ${color}66;">${_nodeIcon(node)}</div>${bat!=null?`<div style="position:absolute;bottom:-12px;left:50%;transform:translateX(-50%);font-size:8px;font-family:monospace;color:${_batColor(bat)};white-space:nowrap;background:rgba(6,11,18,.85);padding:1px 3px;border-radius:2px;pointer-events:none;">${bat}%</div>`:''}</div>`;
                const existingMain=app.markers[nodeId]?.find(m=>m instanceof L.Marker);
                if(existingMain){
                    const cll=existingMain.getLatLng();
                    if(Math.abs(cll.lat-lat)>0.000001||Math.abs(cll.lng-lon)>0.000001){existingMain.setLatLng([lat,lon]);existingMain._nodeLatLng=[lat,lon];}
                    existingMain.setIcon(L.divIcon({html:iconHtml,className:'c2-node-marker',iconSize:[34,46],iconAnchor:[17,17],popupAnchor:[0,-20]}));
                    updated++;
                } else {
                    app.markers[nodeId]=[];
                    const mk=L.marker([lat,lon],{icon:L.divIcon({html:iconHtml,className:'c2-node-marker',iconSize:[34,46],iconAnchor:[17,17],popupAnchor:[0,-20]})}).addTo(app.map);
                    mk._nodeLatLng=[lat,lon]; mk._nodeId=nodeId;
                    mk.on('click',()=>window.c2OpenPanel(nodeId));
                    app.markers[nodeId].push(mk);
                    if(showTrails)newNodeQueue.push({nodeId,node,lat,lon,color});
                    plotted++;
                }
            } catch(e){console.warn('[Map] Plot failed for node '+nodeId+':',e);}
        }

        console.log('[Map] Markers: '+plotted+' new, '+updated+' updated, '+Object.keys(app.markers).length+' total on map');

        if(newNodeQueue.length>0){
            console.log('[Map] Fetching position history for '+newNodeQueue.length+' new nodes');
            const BATCH=8;
            for(let i=0;i<newNodeQueue.length;i+=BATCH){
                await Promise.all(newNodeQueue.slice(i,i+BATCH).map(({nodeId,node,lat,lon,color})=>
                    fetch(`/api/nodes/${encodeURIComponent(nodeId)}/history/positions?limit=50${_mapSQ?'&'+_mapSQ.slice(1):''}`)
                    .then(r=>r.ok?r.json():[]).then(positions=>{
                        if(!app.markers[nodeId]||!positions?.length)return;
                        app.markers[nodeId].filter(m=>m instanceof L.CircleMarker).forEach(m=>{try{app.map.removeLayer(m);}catch(e){}});
                        app.markers[nodeId]=app.markers[nodeId].filter(m=>!(m instanceof L.CircleMarker));
                        if(app.trajectories[nodeId]){try{app.map.removeLayer(app.trajectories[nodeId]);}catch(e){}delete app.trajectories[nodeId];}
                        const valid=positions.filter(p=>typeof p.latitude==='number'&&!isNaN(p.latitude)&&p.latitude!==0&&p.longitude!==0).sort((a,b)=>(a.timestamp||0)-(b.timestamp||0));
                        const latlngs=[];let last=null;
                        valid.forEach(p=>{if(!last||Math.abs(last.latitude-p.latitude)>0.0001||Math.abs(last.longitude-p.longitude)>0.0001){const hm=L.circleMarker([p.latitude,p.longitude],{radius:2,color,weight:1,fillColor:color,fillOpacity:0.35});hm.bindTooltip(_nodeLabel(node)+' · '+_fmtTime(p.timestamp),{permanent:false,direction:'top'});app.markers[nodeId].push(hm);latlngs.push([p.latitude,p.longitude]);last=p;}});
                        if(latlngs.length>0){const ll=latlngs[latlngs.length-1];if(Math.abs(ll[0]-lat)>0.0001||Math.abs(ll[1]-lon)>0.0001)latlngs.push([lat,lon]);}
                        if(latlngs.length>1)app.trajectories[nodeId]=L.polyline(latlngs,{color,weight:2,opacity:0.55,lineCap:'round',lineJoin:'round',dashArray:'4 4'}).addTo(app.map);
                    }).catch(e=>console.warn('[Map] History fetch failed for '+nodeId+':',e))
                ));
                if(i+BATCH<newNodeQueue.length)await new Promise(r=>setTimeout(r,80));
            }
        }

        if(showNeighbors)_drawNeighbourLinks(nodes, allNeighbours);
        _buildLegend(nodes);
        window.c2UpdateMapVisibility();
        if(isFirstLoad){
            console.log('[Map] First load — fitting bounds to '+Object.keys(app.markers).length+' markers');
            window.c2FitMapBounds();
        }

        console.log('[Map] Draw complete. Final marker count on map: '+Object.keys(app.markers).length);
        console.groupEnd();

    } catch(e) {
        console.error('[Map] Draw error:',e);
        console.groupEnd();
        window.triggerToast?.('Map render error','err');
    } finally {
        app._drawing = false;
        if (app._drawPending) { app._drawPending=false; setTimeout(window.c2FetchAndDrawMap,50); }
    }
};

/* ── Neighbour link overlay ────────────────────────────────────────────── */
function _drawNeighbourLinks(nodes, neighbours) {
    const app = window.C2MapApp;
    const drawn = new Set();
    neighbours.forEach(nb => {
        const key = [nb.node_id,nb.neighbor_id].sort().join('|');
        if (drawn.has(key)) return; drawn.add(key);
        const a=nodes[nb.node_id], b=nodes[nb.neighbor_id];
        if (!a||!b) return;
        const la=_pick(a,'position.latitude','latitude'),loa=_pick(a,'position.longitude','longitude');
        const lb=_pick(b,'position.latitude','latitude'),lob=_pick(b,'position.longitude','longitude');
        if (!la||!lb||(la===0&&loa===0)||(lb===0&&lob===0)) return;
        const snr=nb.snr;
        const col=snr==null?'#7a96b2':snr>5?'#00e87a':snr>0?'#00c8f5':snr>-10?'#ffa826':'#ff3050';
        const line=L.polyline([[la,loa],[lb,lob]],{color:col,weight:1.5,opacity:0.6,dashArray:'6 4'});
        line.bindTooltip(`${_nodeLabel(a)} ↔ ${_nodeLabel(b)}<br/>SNR: ${snr??'?'} dB`,{permanent:false});
        app.neighbourLines[key]=line;
    });
}

/* ── Visibility ────────────────────────────────────────────────────────── */
/* ── Active filter state — read by c2UpdateMapVisibility ─────────────── */
window._mapFilters = {
    search: '', role: '', age: 0, snr: null, hops: null,
    bat: null, hw: '', onlineOnly: false, gpsOnly: false,
};

window.c2ApplyMapFilters = function() {
    const g = id => document.getElementById(id);
    const f = window._mapFilters;
    f.search     = (g('mfd-search')?.value || '').trim().toLowerCase();
    f.role       = g('mfd-role')?.value || '';
    f.age        = parseFloat(g('mfd-age')?.value || '0') || 0;
    f.snr        = g('mfd-snr')?.value !== '' ? parseFloat(g('mfd-snr')?.value) : null;
    f.hops       = g('mfd-hops')?.value !== '' ? parseFloat(g('mfd-hops')?.value) : null;
    f.bat        = g('mfd-bat')?.value !== '' ? parseFloat(g('mfd-bat')?.value) : null;
    f.hw         = g('mfd-hw')?.value || '';
    f.onlineOnly = g('mfd-online')?.checked || false;
    f.gpsOnly    = g('mfd-gps')?.checked || false;

    // Count active filters for badge
    let active = 0;
    if (f.search)  active++;
    if (f.role)    active++;
    if (f.age > 0) active++;
    if (f.snr  != null) active++;
    if (f.hops != null) active++;
    if (f.bat  != null) active++;
    if (f.hw)  active++;
    if (f.onlineOnly) active++;
    if (f.gpsOnly)    active++;
    const badge = document.getElementById('map-filter-badge');
    if (badge) { badge.textContent = active; badge.style.display = active ? 'inline-block' : 'none'; }

    window.c2UpdateMapVisibility();
};

window.c2ClearMapFilters = function() {
    const g = id => document.getElementById(id);
    const ids = ['mfd-search','mfd-role','mfd-age','mfd-snr','mfd-hops','mfd-bat','mfd-hw'];
    ids.forEach(id => { const el = g(id); if (el) el.value = id === 'mfd-age' ? '0' : ''; });
    ['mfd-online','mfd-gps'].forEach(id => { const el = g(id); if (el) el.checked = false; });
    window.c2ApplyMapFilters();
};

window.c2ToggleFilterDrawer = function() {
    const d = document.getElementById('map-filter-drawer');
    if (d) d.classList.toggle('open');
    // Populate hardware dropdown from live nodes on first open
    const hw = document.getElementById('mfd-hw');
    if (hw && hw.options.length <= 1) {
        const models = new Set();
        Object.values(window.meshState?.nodes || {}).forEach(n => {
            const m = _pick(n, 'user.hwModel', 'hw_model');
            if (m) models.add(String(m).toUpperCase());
        });
        [...models].sort().forEach(m => {
            const o = document.createElement('option'); o.value = m; o.textContent = m;
            hw.appendChild(o);
        });
    }
};

function _nodeMatchesFilters(nodeId, node) {
    const f = window._mapFilters;
    const now = Date.now() / 1000;
    const lh = node?.lastHeard || node?.last_heard || 0;

    if (f.search) {
        const name = (_nodeLabel(node) || '').toLowerCase();
        const id   = (nodeId || '').toLowerCase();
        if (!name.includes(f.search) && !id.includes(f.search)) return false;
    }
    if (f.role) {
        const r = String(node?.role || '').toUpperCase();
        if (!r.includes(f.role)) return false;
    }
    if (f.age > 0) {
        const cutoff = now - f.age * 3600;
        if (!lh || lh < cutoff) return false;
    }
    if (f.onlineOnly) {
        if (!lh || (now - lh) > 3600) return false;
    }
    if (f.gpsOnly) {
        const lat = _pick(node, 'position.latitude', 'latitude');
        const lon = _pick(node, 'position.longitude', 'longitude');
        if (typeof lat !== 'number' || typeof lon !== 'number' || isNaN(lat) || (lat === 0 && lon === 0)) return false;
    }
    if (f.snr != null) {
        const snr = node?.snr ?? node?.rx_snr ?? null;
        if (snr == null || snr < f.snr) return false;
    }
    if (f.hops != null) {
        const h = node?.hopsAway ?? node?.hops_away ?? null;
        if (h == null || h > f.hops) return false;
    }
    if (f.bat != null) {
        const b = _pick(node, 'deviceMetrics.batteryLevel', 'battery_level');
        if (b == null || b < f.bat) return false;
    }
    if (f.hw) {
        const m = String(_pick(node, 'user.hwModel', 'hw_model') || '').toUpperCase();
        if (!m.includes(f.hw)) return false;
    }
    return true;
}

window.c2UpdateMapVisibility = function() {
    const app = window.C2MapApp;
    if (!app.map) return;
    const showNodes  = document.getElementById('map-show-all')?.checked !== false;
    const showTraj   = document.getElementById('map-show-traj')?.checked !== false;
    const showLinks  = document.getElementById('map-show-links')?.checked;
    // legend search also still works (in case legend exists)
    const legSearch  = (document.getElementById('map-legend-search')?.value || '').toLowerCase();
    const nodes      = window.meshState?.nodes || {};

    let total = 0, visible = 0, online = 0, gps = 0;
    const now = Date.now() / 1000;

    Object.keys(app.markers).forEach(nodeId => {
        const node = nodes[nodeId];
        total++;
        const lh = node?.lastHeard || node?.last_heard || 0;
        if (lh && (now - lh) < 3600) online++;
        const lat = _pick(node, 'position.latitude', 'latitude');
        if (typeof lat === 'number' && lat !== 0) gps++;

        const matchFilter  = _nodeMatchesFilters(nodeId, node);
        const matchLegend  = !legSearch || (_nodeLabel(node)).toLowerCase().includes(legSearch)
                             || nodeId.toLowerCase().includes(legSearch);
        const match = matchFilter && matchLegend;
        if (match) visible++;

        app.markers[nodeId]?.forEach(m => {
            const isMain = m instanceof L.Marker;
            const isHist = m instanceof L.CircleMarker;
            const show   = match && ((isMain && showNodes) || (isHist && showTraj));
            show ? (app.map.hasLayer(m) || m.addTo(app.map))
                 : (app.map.hasLayer(m) && m.remove());
        });
        const traj = app.trajectories[nodeId];
        if (traj) (match && showTraj)
            ? (app.map.hasLayer(traj) || traj.addTo(app.map))
            : (app.map.hasLayer(traj) && traj.remove());
        const leg = document.getElementById(`leg-${nodeId}`);
        if (leg) leg.style.display = match ? 'flex' : 'none';
    });

    Object.values(app.neighbourLines).forEach(l => {
        showLinks ? (app.map.hasLayer(l) || l.addTo(app.map))
                  : (app.map.hasLayer(l) && l.remove());
    });

    // Update stats bar
    const _sv = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    _sv('msb-total',   Object.keys(nodes).length || total);
    _sv('msb-visible', visible);
    _sv('msb-online',  online);
    _sv('msb-gps',     gps);
};

window.c2FitMapBounds = function() {
    const app=window.C2MapApp;
    const coords=[];
    Object.keys(app.markers).forEach(nid=>{
        const m=app.markers[nid]?.find(m=>m instanceof L.Marker);
        if(m&&app.map.hasLayer(m)){const ll=m.getLatLng();if(!isNaN(ll.lat))coords.push(ll);}
    });
    if(coords.length>0){try{app.map.fitBounds(L.latLngBounds(coords),{padding:[50,50],maxZoom:16});}catch(e){}}
};

window.c2FlyTo = function(nid) {
    const app=window.C2MapApp;
    const m=app.markers[nid]?.find(m=>m instanceof L.Marker);
    if(m){app.map.flyTo(m.getLatLng(),16,{animate:true,duration:1});}
};

window.c2FlyToActive = function() {
    if (window.C2MapApp._activeNodeId) window.c2FlyTo(window.C2MapApp._activeNodeId);
};

/* ── Legend ────────────────────────────────────────────────────────────── */
function _buildLegend(nodes) {
    const app=window.C2MapApp;
    if(app._legendControl){try{app.map.removeControl(app._legendControl);}catch(e){}}
    const legend=L.control({position:'bottomright'});
    legend.onAdd=function(){
        const div=L.DomUtil.create('div','c2-legend');
        div.innerHTML=`<h4>NETWORK ASSETS</h4><input type="text" id="map-legend-search" placeholder="Filter nodes..." oninput="window.c2UpdateMapVisibility()">`;
        const list=document.createElement('div');
        Object.keys(app.nodeColors).forEach(nid=>{
            const node=nodes[nid];
            const name=_esc(_nodeLabel(node));
            const color=app.nodeColors[nid];
            const bat=_pick(node,'deviceMetrics.batteryLevel','battery_level');
            const lh=node?.lastHeard||node?.last_heard;
            const online=lh&&(Date.now()/1000-lh)<3600;
            const item=document.createElement('div');
            item.className='c2-legend-item';item.id=`leg-${nid}`;
            item.innerHTML=`
                <div style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0;${online?`box-shadow:0 0 5px ${color}`:''}"></div>
                <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${name}</span>
                ${bat!=null?`<span class="c2-legend-bat" style="color:${_batColor(bat)}">${bat}%</span>`:''}`;
            item.onclick=()=>window.c2FlyTo(nid);
            list.appendChild(item);
        });
        div.appendChild(list);
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);
        return div;
    };
    app._legendControl=legend.addTo(app.map);
}

/* ════════════════════════════════════════════════════════════════════════
 * NODE DETAIL PANEL
 * Created once on document.body, survives SPA view reloads
 * ════════════════════════════════════════════════════════════════════════ */

function _ensurePanel() {
    if (document.getElementById('node-panel')) return;

    if (!document.getElementById('np-styles')) {
        const s=document.createElement('style');
        s.id='np-styles';
        s.innerHTML=`
            #node-panel{position:fixed;top:0;right:0;bottom:0;width:480px;max-width:100vw;
                background:var(--bg1,#0b1320);border-left:1px solid var(--acc,#00c8f5);
                z-index:20000;display:flex;flex-direction:column;
                transform:translateX(100%);transition:transform .28s cubic-bezier(.4,0,.2,1);
                box-shadow:-8px 0 40px rgba(0,0,0,.75);}
            #node-panel.open{transform:translateX(0);}
            .np-header{padding:12px 14px;border-bottom:1px solid var(--bd2,#1e3048);background:var(--bg2,#0d1a2d);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
            .np-close{background:none;border:1px solid var(--bd2,#1e3048);color:var(--txt3,#4a6a88);width:26px;height:26px;border-radius:4px;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;}
            .np-close:hover{border-color:#ff3050;color:#ff3050;}
            .np-tabs{display:flex;border-bottom:1px solid var(--bd2,#1e3048);background:var(--bg2,#0d1a2d);flex-shrink:0;overflow-x:auto;}
            .np-tab{padding:8px 12px;font-family:var(--mono,'Fira Code',monospace);font-size:10px;color:var(--txt3,#4a6a88);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:all .15s;}
            .np-tab:hover{color:var(--txt,#c8d8e8);}
            .np-tab.active{color:var(--acc,#00c8f5);border-bottom-color:var(--acc,#00c8f5);}
            .np-body{flex:1;overflow-y:auto;overflow-x:hidden;}
            .np-pane{display:none;padding:14px;}
            .np-pane.active{display:block;}
            .np-stat-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:12px;}
            .np-stat{background:var(--bg2,#0d1a2d);border:1px solid var(--bd,#162338);border-radius:4px;padding:8px;text-align:center;}
            .np-stat-lbl{font-size:8px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);letter-spacing:.5px;margin-bottom:2px;}
            .np-stat-val{font-size:13px;font-weight:bold;font-family:var(--mono,'Fira Code',monospace);color:var(--txt,#c8d8e8);}
            .np-section{font-family:var(--mono,'Fira Code',monospace);font-size:8px;letter-spacing:2px;color:var(--acc,#00c8f5);padding:5px 0 5px 6px;margin:10px 0 6px;border-left:2px solid var(--acc,#00c8f5);background:rgba(0,200,245,.04);}
            .np-mini-chart{position:relative;height:90px;margin-bottom:10px;}
            .np-empty{text-align:center;padding:28px 12px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);font-size:10px;letter-spacing:1px;}
            .np-tab-hidden{display:none!important;}
            .msg-thread{display:flex;flex-direction:column;gap:8px;margin-bottom:12px;max-height:340px;overflow-y:auto;padding-right:4px;}
            .msg-bubble{padding:7px 10px;border-radius:4px;font-size:11px;font-family:var(--mono,'Fira Code',monospace);line-height:1.5;max-width:88%;word-break:break-word;}
            .msg-bubble.inbound{background:var(--bg2,#0d1a2d);border:1px solid var(--bd2,#1e3048);color:var(--txt,#c8d8e8);align-self:flex-start;border-left:2px solid var(--acc,#00c8f5);}
            .msg-bubble.outbound{background:rgba(0,232,122,.08);border:1px solid rgba(0,232,122,.25);color:var(--txt,#c8d8e8);align-self:flex-end;border-right:2px solid #00e87a;}
            .msg-meta{font-size:8px;color:var(--txt3,#4a6a88);margin-top:3px;}
            .msg-status-DELIVERED{color:#00e87a;} .msg-status-FAILED{color:#ff3050;} .msg-status-SENT{color:#ffa826;} .msg-status-BROADCAST{color:var(--acc,#00c8f5);}
            .msg-compose{display:flex;gap:6px;padding:10px 14px;border-top:1px solid var(--bd2,#1e3048);background:var(--bg2,#0d1a2d);flex-shrink:0;}
            .msg-compose.hidden{display:none;}
            .nb-row{display:flex;align-items:center;justify-content:space-between;padding:5px 8px;background:var(--bg2,#0d1a2d);border:1px solid var(--bd,#162338);border-radius:3px;margin-bottom:4px;font-family:var(--mono,'Fira Code',monospace);font-size:10px;}
            .nb-snr-bar{height:4px;border-radius:2px;margin-top:3px;}
            .pos-row{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:6px;padding:4px 6px;font-family:var(--mono,'Fira Code',monospace);font-size:9px;border-bottom:1px solid var(--bd,#162338);color:var(--txt2,#8aa0b8);}
            .pos-row:hover{background:var(--bg2,#0d1a2d);}
            #np-overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:19999;display:none;}
            #np-overlay.vis{display:block;}
        `;
        document.head.appendChild(s);
    }

    if (!document.getElementById('np-overlay')) {
        const ov=document.createElement('div');
        ov.id='np-overlay';ov.onclick=window.c2ClosePanel;
        document.body.appendChild(ov);
    }

    const panel=document.createElement('div');
    panel.id='node-panel';
    panel.innerHTML=`
        <div class="np-header">
            <div>
                <div id="np-name" style="font-weight:800;font-size:13px;color:var(--acc,#00c8f5);font-family:var(--mono,'Fira Code',monospace);">—</div>
                <div id="np-id" style="font-size:9px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);margin-top:2px;">—</div>
            </div>
            <div style="display:flex;gap:6px;align-items:center;">
                <button class="btn btn-sm btn-acc" onclick="window.c2FlyToActive()" style="font-size:9px;padding:3px 8px;">⌖ LOCATE</button>
                <button class="btn btn-sm" onclick="window.c2OpenAnalytics()" style="font-size:9px;padding:3px 8px;">📊 ANALYTICS</button>
                <button class="np-close" onclick="window.c2ClosePanel()">✕</button>
            </div>
        </div>
        <div class="np-tabs">
            <div class="np-tab active" data-pane="pane-stats"   onclick="window.c2SwitchTab(this)">TELEMETRY</div>
            <div class="np-tab"        data-pane="pane-comms"   onclick="window.c2SwitchTab(this)">COMMS</div>
            <div class="np-tab"        data-pane="pane-signal"  onclick="window.c2SwitchTab(this)">SIGNAL</div>
            <div class="np-tab"        data-pane="pane-network" onclick="window.c2SwitchTab(this)">NETWORK</div>
            <div class="np-tab"        data-pane="pane-pos"     onclick="window.c2SwitchTab(this)">POSITION</div>
        </div>
        <div class="np-body">
            <!-- TELEMETRY -->
            <div class="np-pane active" id="pane-stats">
                <div class="np-section">IDENTITY</div>
                <div class="np-stat-grid">
                    <div class="np-stat"><div class="np-stat-lbl">HARDWARE</div><div class="np-stat-val" id="np-hw" style="font-size:9px;">—</div></div>
                    <div class="np-stat"><div class="np-stat-lbl">FIRMWARE</div><div class="np-stat-val" id="np-fw" style="font-size:9px;">—</div></div>
                    <div class="np-stat"><div class="np-stat-lbl">ROLE</div><div class="np-stat-val" id="np-role" style="font-size:9px;">—</div></div>
                    <div class="np-stat"><div class="np-stat-lbl">MAC</div><div class="np-stat-val" id="np-mac" style="font-size:8px;">—</div></div>
                    <div class="np-stat"><div class="np-stat-lbl">LAST HEARD</div><div class="np-stat-val" id="np-lh" style="font-size:9px;">—</div></div>
                    <div class="np-stat"><div class="np-stat-lbl">NODE NUM</div><div class="np-stat-val" id="np-num" style="font-size:9px;">—</div></div>
                </div>
                <div id="np-sec-power">
                    <div class="np-section">POWER</div>
                    <div class="np-stat-grid">
                        <div class="np-stat"><div class="np-stat-lbl">BATTERY</div><div class="np-stat-val" id="np-bat">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">VOLTAGE</div><div class="np-stat-val" id="np-volt">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">UPTIME</div><div class="np-stat-val" id="np-uptime" style="font-size:9px;">—</div></div>
                    </div>
                    <div class="np-mini-chart"><canvas id="np-chart-bat"></canvas></div>
                </div>
                <div id="np-sec-rf">
                    <div class="np-section">RF / NETWORK</div>
                    <div class="np-stat-grid">
                        <div class="np-stat"><div class="np-stat-lbl">SNR</div><div class="np-stat-val" id="np-snr">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">RSSI</div><div class="np-stat-val" id="np-rssi">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">CH UTIL</div><div class="np-stat-val" id="np-util">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">AIR TX</div><div class="np-stat-val" id="np-airtx">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">AVG HOPS</div><div class="np-stat-val" id="np-hops">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">PKTS SEEN</div><div class="np-stat-val" id="np-pkts" style="font-size:9px;">—</div></div>
                    </div>
                </div>
                <div id="np-sec-env" style="display:none;">
                    <div class="np-section">ENVIRONMENT</div>
                    <div class="np-stat-grid">
                        <div class="np-stat"><div class="np-stat-lbl">TEMP</div><div class="np-stat-val" id="np-temp">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">HUMIDITY</div><div class="np-stat-val" id="np-hum">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">PRESSURE</div><div class="np-stat-val" id="np-pres" style="font-size:9px;">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">GAS Ω</div><div class="np-stat-val" id="np-gas" style="font-size:9px;">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">IAQ</div><div class="np-stat-val" id="np-iaq">—</div></div>
                    </div>
                </div>
                <div id="np-sec-gps">
                    <div class="np-section">GPS</div>
                    <div class="np-stat-grid">
                        <div class="np-stat"><div class="np-stat-lbl">LATITUDE</div><div class="np-stat-val" id="np-lat" style="font-size:9px;">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">LONGITUDE</div><div class="np-stat-val" id="np-lon" style="font-size:9px;">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">ALTITUDE</div><div class="np-stat-val" id="np-alt">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">SATS</div><div class="np-stat-val" id="np-sats">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">SPEED</div><div class="np-stat-val" id="np-speed">—</div></div>
                        <div class="np-stat"><div class="np-stat-lbl">HEADING</div><div class="np-stat-val" id="np-hdg">—</div></div>
                    </div>
                </div>
            </div>
            <!-- COMMS -->
            <div class="np-pane" id="pane-comms">
                <div id="np-comms-inner"><div class="np-empty">LOADING COMMS...</div></div>
            </div>
            <!-- SIGNAL -->
            <div class="np-pane" id="pane-signal">
                <div id="np-signal-inner"><div class="np-empty">LOADING SIGNAL...</div></div>
            </div>
            <!-- NETWORK -->
            <div class="np-pane" id="pane-network">
                <div id="np-network-inner"><div class="np-empty">LOADING NETWORK...</div></div>
            </div>
            <!-- POSITION -->
            <div class="np-pane" id="pane-pos">
                <div id="np-pos-inner"><div class="np-empty">LOADING POSITION...</div></div>
            </div>
        </div>
        <div class="msg-compose hidden" id="np-compose" style="display:none!important;"></div>
    `;
    document.body.appendChild(panel);
}

/* ── Panel open/close/tab ────────────────────────────────────────────── */
window.c2OpenPanel = async function(nid) {
    const app = window.C2MapApp;
    app._activeNodeId = nid;
    _ensurePanel();

    const panel   = document.getElementById('node-panel');
    const overlay = document.getElementById('np-overlay');
    const node    = window.meshState?.nodes?.[nid] || {};

    const nameEl=document.getElementById('np-name'); if(nameEl) nameEl.textContent=_nodeLabel(node);
    const idEl=document.getElementById('np-id');     if(idEl)   idEl.textContent=`${nid}  •  ${_pick(node,'user.shortName','short_name')||''}`;

    // Reset tabs — all back to loading state
    document.querySelectorAll('#node-panel .np-tab').forEach((t,i)=>t.classList.toggle('active',i===0));
    document.querySelectorAll('#node-panel .np-pane').forEach((p,i)=>p.classList.toggle('active',i===0));
    document.querySelectorAll('#node-panel .np-tab').forEach(t=>t.classList.remove('np-tab-hidden'));
    const compose=document.getElementById('np-compose'); if(compose) compose.classList.add('hidden');
    ['np-comms-inner','np-signal-inner','np-network-inner','np-pos-inner'].forEach(id=>{
        const el=document.getElementById(id); if(el) el.innerHTML='<div class="np-empty">LOADING...</div>';
    });

    panel.classList.add('open');
    if(overlay) overlay.classList.add('vis');

    _fillStaticStats(nid, node);
    await _loadPanelData(nid, node);

    // Start live poll so incoming messages appear without page refresh
    _startCommsPoll(nid);
};

window.c2ClosePanel = function() {
    const panel=document.getElementById('node-panel');
    const overlay=document.getElementById('np-overlay');
    if(panel)   panel.classList.remove('open');
    if(overlay) overlay.classList.remove('vis');
    window.C2MapApp._activeNodeId=null;
    _stopCommsPoll();
};

window.c2SwitchTab = function(el) {
    document.querySelectorAll('#node-panel .np-tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('#node-panel .np-pane').forEach(p=>p.classList.remove('active'));
    el.classList.add('active');
    const pane=document.getElementById(el.dataset.pane);
    if(pane) pane.classList.add('active');
    const compose=document.getElementById('np-compose');
    if(compose) compose.classList.toggle('hidden', el.dataset.pane!=='pane-comms');
};

window.c2OpenAnalytics = function() {
    const nid=window.C2MapApp._activeNodeId; if(!nid) return;
    if(typeof window.C2AnalyticsApp?.selectTarget==='function'){
        window.loadView?.('analytics');
        setTimeout(()=>window.C2AnalyticsApp.selectTarget(nid),300);
    }
};

/* ── Fill static stats (from in-memory meshState) ─────────────────────── */
function _fillStaticStats(nid, node) {
    const sv=(id,val,unit='',dec=1,colorFn=null)=>{
        const el=document.getElementById(id); if(!el) return;
        if(val==null){el.textContent='—';el.style.color='';return;}
        el.textContent=`${typeof val==='number'?val.toFixed(dec):val}${unit}`;
        el.style.color=colorFn?colorFn(val):'';
    };

    sv('np-hw',   _pick(node,'hw_model'),'',0);
    sv('np-fw',   _pick(node,'firmware_version'),'',0);
    sv('np-role', node.role,'',0);
    sv('np-mac',  _pick(node,'user.macaddr','macaddr'),'',0);
    sv('np-num',  node.node_num!=null?`#${node.node_num}`:null,'',0);
    const lhEl=document.getElementById('np-lh');
    const lh=node.lastHeard||node.last_heard;
    if(lhEl) lhEl.textContent=lh?_fmtDateTime(lh):'—';

    const bat=_pick(node,'deviceMetrics.batteryLevel','battery_level');
    sv('np-bat',   bat,'%',0,_batColor);
    sv('np-volt',  _pick(node,'deviceMetrics.voltage','voltage'),'V',2);
    const ups=_pick(node,'deviceMetrics.uptimeSeconds','uptime_seconds');
    const upEl=document.getElementById('np-uptime'); if(upEl) upEl.textContent=ups!=null?_fmtUptime(ups):'—';

    sv('np-snr',   node.snr,'dB',1,_snrColor);
    sv('np-rssi',  node.rssi,'dBm',0);
    sv('np-util',  _pick(node,'deviceMetrics.channelUtilization','channel_utilization'),'%',1);
    sv('np-airtx', _pick(node,'deviceMetrics.airUtilTx','air_util_tx'),'%',1);

    // Show env section only if we have env data
    const temp=_pick(node,'environmentMetrics.temperature','temperature');
    const hum=_pick(node,'environmentMetrics.relativeHumidity','relative_humidity');
    const pres=_pick(node,'environmentMetrics.barometricPressure','barometric_pressure');
    const gas=_pick(node,'environmentMetrics.gasResistance','gas_resistance');
    const iaq=_pick(node,'environmentMetrics.iaq','iaq');
    const hasEnv = temp!=null||hum!=null||pres!=null||gas!=null||iaq!=null;
    const envSec=document.getElementById('np-sec-env');
    if(envSec) envSec.style.display=hasEnv?'':'none';
    if(hasEnv){sv('np-temp',temp,'°C',1);sv('np-hum',hum,'%',1);sv('np-pres',pres,'hPa',1);sv('np-gas',gas,'Ω',0);sv('np-iaq',iaq,'',0);}

    const lat=_pick(node,'position.latitude','latitude');
    const lon=_pick(node,'position.longitude','longitude');
    const alt=_pick(node,'position.altitude','altitude');
    const sats=_pick(node,'position.satsInView','sats_in_view');
    const spd=_pick(node,'position.groundSpeed','ground_speed');
    const hdg=_pick(node,'position.groundTrack','ground_track');
    const hasGps=lat!=null&&lon!=null;
    const gpsSec=document.getElementById('np-sec-gps');
    if(gpsSec) gpsSec.style.display=hasGps?'':'none';
    if(hasGps){sv('np-lat',lat,'°',5);sv('np-lon',lon,'°',5);sv('np-alt',alt,'m',0);sv('np-sats',sats,'',0);sv('np-speed',spd,'m/s',1);sv('np-hdg',hdg,'°',0);}
}

/* ── Load all async panel data ─────────────────────────────────────────── */
async function _loadPanelData(nid, node) {
    // No time limit — fetch ALL available history for this node
    // Use large limits to pull everything in the DB
    const baseQ = 'limit=2000';

    try {
        // Run all fetches concurrently
        // Messages: fetch both directions explicitly as the API supports from_id/to_id params
        const localId = window.meshState?.local_node_id ||
            Object.values(window.meshState?.nodes||{}).find(n=>n.isLocal||n.is_local)?.node_id || '';

        const _npSQ = (window._activeSlotId && window._activeSlotId !== 'node_0') ? `&slot_id=${encodeURIComponent(window._activeSlotId)}` : '';
        const [telemetry, packets, positions, msgFrom, msgTo, neighbours, traceroutes] = await Promise.all([
            fetch(`/api/nodes/${nid}/history/telemetry?${baseQ}${_npSQ}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/nodes/${nid}/history/packets?${baseQ}${_npSQ}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/nodes/${nid}/history/positions?${baseQ}${_npSQ}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/messages/history?from_id=${encodeURIComponent(nid)}&limit=200${_npSQ}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/messages/history?to_id=${encodeURIComponent(nid)}&limit=200${_npSQ}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/neighbors${_npSQ ? '?'+_npSQ.slice(1) : ''}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
            fetch(`/api/traceroutes?limit=500${_npSQ}`).then(r=>r.ok?r.json():[]).catch(()=>[]),
        ]);

        const sort = arr => Array.isArray(arr) ? [...arr].sort((a,b)=>a.timestamp-b.timestamp) : [];
        const t = sort(telemetry);
        const p = sort(positions);
        const k = sort(packets);

        // Merge messages — deduplicate by id, sort chronologically
        const msgMap = {};
        [...msgFrom, ...msgTo].forEach(m => { if(m.id||m.packet_event_id) msgMap[m.id||m.packet_event_id]=m; });
        const messages = Object.values(msgMap).sort((a,b)=>a.timestamp-b.timestamp);

        const myNeighbours = Array.isArray(neighbours) ? neighbours.filter(n=>n.node_id===nid||n.neighbor_id===nid) : [];
        const myTraceroutes = Array.isArray(traceroutes) ? traceroutes.filter(tr=>tr.from_id===nid||tr.to_id===nid) : [];

        // ── Update stats tab live counters ──────────────────────────────
        const pktEl=document.getElementById('np-pkts'); if(pktEl) pktEl.textContent=k.length;
        const hopPkts=k.filter(r=>r.hop_start!=null&&r.hop_limit!=null);
        const hopEl=document.getElementById('np-hops');
        if(hopEl&&hopPkts.length){
            const avg=hopPkts.reduce((a,r)=>a+Math.max(0,(r.hop_start||0)-(r.hop_limit||0)),0)/hopPkts.length;
            hopEl.textContent=avg.toFixed(1);
        }

        // Battery chart in stats tab
        _miniChart('np-chart-bat',[
            _mkDs(t,'battery_level','Bat %','#00e87a','y'),
            _mkDs(t,'voltage','Volts','#00c8f5','y1'),
        ],{y:_miniAxis('left','%',{min:0,max:100}),y1:_miniAxis('right','V')});

        // ── SIGNAL tab ──────────────────────────────────────────────────
        _renderSignalTab(nid, k, t);

        // ── NETWORK tab ─────────────────────────────────────────────────
        _renderNetworkTab(nid, myNeighbours, myTraceroutes, t);

        // ── POSITION tab ────────────────────────────────────────────────
        _renderPositionTab(nid, p);

        // ── COMMS tab ───────────────────────────────────────────────────
        _renderCommsTab(nid, messages, localId);

    } catch(e) {
        console.error('Panel data load error:', e);
    }
}

/* ── Tab show/hide helpers ─────────────────────────────────────────────── */
function _hideTab(paneId) {
    const btn = document.querySelector(`#node-panel .np-tab[data-pane="${paneId}"]`);
    if (btn) btn.classList.add('np-tab-hidden');
    const pane = document.getElementById(paneId);
    if (pane?.classList.contains('active')) {
        const firstVisible = document.querySelector('#node-panel .np-tab:not(.np-tab-hidden)');
        if (firstVisible) window.c2SwitchTab(firstVisible);
    }
}

function _showTab(paneId) {
    const btn = document.querySelector(`#node-panel .np-tab[data-pane="${paneId}"]`);
    if (btn) btn.classList.remove('np-tab-hidden');
}

/* ── Signal tab renderer ───────────────────────────────────────────────── */
function _renderSignalTab(nid, k, t) {
    const inner = document.getElementById('np-signal-inner');
    if (!inner) return;

    const hasSignal = k.some(r=>r.rx_snr!=null||r.rx_rssi!=null);
    const hasHops   = k.some(r=>r.hop_start!=null&&r.hop_limit!=null);
    const hasPktRate = k.length > 0;
    const hasUtil   = t.some(r=>r.channel_utilization!=null||r.air_util_tx!=null);

    if (!hasSignal && !hasHops && !hasPktRate && !hasUtil) {
        inner.innerHTML = '<div class="np-empty">[ NO SIGNAL DATA IN DATABASE ]</div>';
        _hideTab('pane-signal');
        return;
    }
    _showTab('pane-signal');

    inner.innerHTML = `
        ${hasSignal?`<div class="np-section">SNR & RSSI</div><div class="np-mini-chart" style="height:110px;"><canvas id="np-chart-signal"></canvas></div>`:''}
        ${hasHops?`<div class="np-section">HOP COUNT</div><div class="np-mini-chart" style="height:90px;"><canvas id="np-chart-hops"></canvas></div>`:''}
        ${hasPktRate?`<div class="np-section">PACKET RATE / HOUR</div><div class="np-mini-chart" style="height:90px;"><canvas id="np-chart-pktrate"></canvas></div>`:''}
        ${hasUtil?`<div class="np-section">CHANNEL UTILISATION</div><div class="np-mini-chart" style="height:90px;"><canvas id="np-chart-util"></canvas></div>`:''}
    `;

    if(hasSignal) _miniChart('np-chart-signal',[_mkDs(k,'rx_snr','SNR dB','#b060ff','y'),_mkDs(k,'rx_rssi','RSSI dBm','#ff3050','y1')],{y:_miniAxis('left','dB'),y1:_miniAxis('right','dBm')});
    if(hasHops){
        const hopPts=k.filter(r=>r.hop_start!=null&&r.hop_limit!=null).map(r=>({x:r.timestamp*1000,y:Math.max(0,(r.hop_start||0)-(r.hop_limit||0))}));
        _miniChart('np-chart-hops',[{label:'Hops Used',yAxisID:'y',borderColor:'#00d4aa',backgroundColor:'#00d4aa18',borderWidth:1.5,pointRadius:0,tension:0.3,data:hopPts}],{y:_miniAxis('left','Hops',{min:0})});
    }
    if(hasPktRate){
        const pktMap={};
        k.forEach(r=>{const h=new Date(r.timestamp*1000);h.setMinutes(0,0,0);const k2=h.getTime();pktMap[k2]=(pktMap[k2]||0)+1;});
        const pktPts=Object.entries(pktMap).sort((a,b)=>+a[0]-+b[0]).map(([ts,cnt])=>({x:+ts,y:cnt}));
        _miniChart('np-chart-pktrate',[{label:'Pkts/hr',data:pktPts,backgroundColor:'#00c8f555',borderColor:'#00c8f5',borderWidth:1.5,yAxisID:'y'}],{y:_miniAxis('left','Pkts/hr',{min:0})},'bar');
    }
    if(hasUtil) _miniChart('np-chart-util',[_mkDs(t,'channel_utilization','Ch Util %','#ffa826','y'),_mkDs(t,'air_util_tx','Air TX %','#ff3050','y')],{y:_miniAxis('left','%',{min:0})});
}

/* ── Network tab renderer ──────────────────────────────────────────────── */
function _renderNetworkTab(nid, myNeighbours, myTraceroutes, t) {
    const inner = document.getElementById('np-network-inner');
    if (!inner) return;

    const hasNb = myNeighbours.length > 0;
    const hasTr = myTraceroutes.length > 0;

    if (!hasNb && !hasTr) {
        inner.innerHTML = '<div class="np-empty">[ NO NETWORK DATA IN DATABASE ]</div>';
        _hideTab('pane-network');
        return;
    }
    _showTab('pane-network');

    const nodes = window.meshState?.nodes||{};

    let html = '';

    if (hasNb) {
        html += `<div class="np-section">DIRECT NEIGHBOURS (${myNeighbours.length})</div>`;
        html += myNeighbours.map(nb=>{
            const otherId=nb.node_id===nid?nb.neighbor_id:nb.node_id;
            const other=nodes[otherId];
            const name=_esc(_nodeLabel(other)||otherId);
            const snr=nb.snr;
            const col=snr==null?'var(--txt3)':snr>5?'var(--ok)':snr>0?'var(--acc)':snr>-10?'#ffa826':'var(--err)';
            const pct=snr==null?0:Math.min(100,Math.max(0,(snr+20)*3.3));
            return `<div class="nb-row">
                <div style="flex:1;overflow:hidden;">
                    <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${name}</div>
                    <div style="font-size:8px;color:var(--txt3);">${otherId}</div>
                    <div class="nb-snr-bar" style="width:${pct}%;background:${col};"></div>
                </div>
                <div style="color:${col};margin-left:8px;font-weight:bold;">${snr!=null?snr.toFixed(1)+' dB':'—'}</div>
            </div>`;
        }).join('');
    }

    if (hasTr) {
        const recent = myTraceroutes.slice(-10).reverse();
        html += `<div class="np-section">RECENT TRACEROUTES (${myTraceroutes.length} total)</div>`;
        html += recent.map(tr=>{
            let rp=tr.route_path;
            if(typeof rp==='string'){try{rp=JSON.parse(rp);}catch(e){rp={};}}
            const hops=rp?.hops_used??((rp?.route_to?.length||0)+(rp?.route_back?.length||0));
            const fromName=_esc(_nodeLabel(nodes[tr.from_id])||tr.from_id);
            const toName=_esc(_nodeLabel(nodes[tr.to_id])||tr.to_id);
            const dt=_fmtDateTime(tr.timestamp);
            const via=(rp?.route_to||[]).map(id=>`<span style="color:var(--acc)">${_esc(_nodeLabel(nodes[id])||id)}</span>`).join(' → ');
            return `<div style="background:var(--bg2,#0d1a2d);border:1px solid var(--bd,#162338);border-radius:3px;padding:7px 9px;margin-bottom:5px;font-family:var(--mono,'Fira Code',monospace);font-size:9px;">
                <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
                    <span style="color:var(--txt2,#8aa0b8);">${fromName} <span style="color:var(--acc,#00c8f5)">→</span> ${toName}</span>
                    <span style="color:var(--txt3,#4a6a88);">${hops} hop${hops!==1?'s':''}</span>
                </div>
                ${via?`<div style="color:var(--txt3,#4a6a88);margin-bottom:2px;">via: ${via}</div>`:''}
                <div style="color:var(--txt3,#4a6a88);font-size:8px;">${dt}${rp?.snr!=null?` · SNR ${rp.snr.toFixed(1)}dB`:''}</div>
            </div>`;
        }).join('');
    }

    inner.innerHTML = html;
}

/* ── Position tab renderer ─────────────────────────────────────────────── */
function _renderPositionTab(nid, p) {
    const inner = document.getElementById('np-pos-inner');
    if (!inner) return;

    const hasAlt   = p.some(r=>r.altitude!=null);
    const hasSpeed = p.some(r=>r.ground_speed!=null);
    const hasPos   = p.length > 0;

    if (!hasPos) {
        inner.innerHTML = '<div class="np-empty">[ NO POSITION DATA IN DATABASE ]</div>';
        _hideTab('pane-pos');
        return;
    }
    _showTab('pane-pos');

    inner.innerHTML = `
        ${(hasAlt||hasSpeed)?`<div class="np-section">ALTITUDE & SPEED</div><div class="np-mini-chart" style="height:100px;"><canvas id="np-chart-motion"></canvas></div>`:''}
        <div class="np-section">POSITION LOG (${p.length} entries)</div>
        <div style="font-family:var(--mono,'Fira Code',monospace);font-size:9px;color:var(--txt3,#4a6a88);display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:4px;padding:4px 6px;border-bottom:1px solid var(--bd2,#1e3048);margin-bottom:4px;">
            <span>LAT</span><span>LON</span><span>ALT</span><span>TIME</span>
        </div>
        <div style="max-height:280px;overflow-y:auto;">
            ${[...p].reverse().slice(0,100).map(pos=>`
            <div class="pos-row">
                <span>${pos.latitude!=null?pos.latitude.toFixed(5):'—'}</span>
                <span>${pos.longitude!=null?pos.longitude.toFixed(5):'—'}</span>
                <span>${pos.altitude!=null?pos.altitude+'m':'—'}</span>
                <span style="color:var(--txt3,#4a6a88);">${_fmtTime(pos.timestamp)}</span>
            </div>`).join('')}
        </div>
    `;

    if(hasAlt||hasSpeed) _miniChart('np-chart-motion',[
        _mkDs(p,'altitude','Alt m','#7a96b2','y',{fill:true}),
        _mkDs(p,'ground_speed','Speed m/s','#ffa826','y1'),
    ],{y:_miniAxis('left','m'),y1:_miniAxis('right','m/s')});
}

/* ── Comms tab renderer ─────────────────────────────────────────────────
 * Pure DM to the selected node — no channel selector needed.
 * ──────────────────────────────────────────────────────────────────────── */
function _renderCommsTab(nid, messages, localId) {
    const inner = document.getElementById('np-comms-inner');
    if (!inner) return;

    _showTab('pane-comms');

    const node = window.meshState?.nodes?.[nid] || {};
    const name = _esc(_nodeLabel(node));
    const isOnline = (node.lastHeard||node.last_heard) && (Date.now()/1000 - (node.lastHeard||node.last_heard)) < 3600;

    const threadHtml = messages.length
        ? messages.map(m => _buildMsgBubble(m, nid, localId)).join('')
        : `<div class="np-empty" style="margin-top:20px;">[ NO PRIOR COMMS — SEND BELOW ]</div>`;

    inner.innerHTML = `
        <div style="display:flex;flex-direction:column;height:100%;">

            <div style="display:flex;align-items:center;gap:8px;padding:8px 0 10px;border-bottom:1px solid var(--bd,#162338);margin-bottom:8px;flex-shrink:0;">
                <div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${isOnline?'var(--ok,#00e87a)':'var(--bd2,#1e3048)'};${isOnline?'box-shadow:0 0 5px var(--ok,#00e87a)':''}"></div>
                <span style="font-family:var(--mono,'Fira Code',monospace);font-size:10px;color:var(--acc,#00c8f5);font-weight:bold;">${name}</span>
                <span style="font-size:9px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);">${_esc(nid)}</span>
                ${messages.length?`<span style="margin-left:auto;font-size:8px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);">${messages.length} messages</span>`:''}
            </div>

            <div id="np-msg-thread" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:4px 0 8px;min-height:0;max-height:300px;">
                ${threadHtml}
            </div>

            <div style="border-top:1px solid var(--bd2,#1e3048);padding-top:10px;flex-shrink:0;">
                <div style="display:flex;gap:6px;align-items:center;">
                    <div style="flex:1;position:relative;background:var(--bg,#080f1a);border:1px solid var(--bd,#162338);border-radius:4px;display:flex;align-items:center;padding:0 10px;">
                        <span style="color:var(--ok,#00e87a);font-family:var(--mono,'Fira Code',monospace);margin-right:6px;font-size:11px;">$</span>
                        <input id="np-msg-input" class="inp mono"
                            style="border:none;background:transparent;flex:1;font-size:11px;padding:8px 0;"
                            placeholder="Direct message to ${name}..."
                            maxlength="230"
                            onkeydown="if(event.key==='Enter')window.c2SendFromPanel()"
                            oninput="const c=document.getElementById('np-char-count');if(c){c.textContent=this.value.length+'/230';c.style.color=this.value.length>200?'var(--err,#ff3050)':'var(--txt3,#4a6a88)';}">
                        <span id="np-char-count" style="font-size:8px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);white-space:nowrap;margin-left:6px;">0/230</span>
                    </div>
                    <button class="btn btn-acc" onclick="window.c2SendFromPanel()"
                        style="height:38px;padding:0 16px;font-size:10px;letter-spacing:1px;flex-shrink:0;">
                        TX ↗
                    </button>
                </div>
                <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:8px;font-family:var(--mono,'Fira Code',monospace);color:var(--txt3,#4a6a88);">
                    <span><i class="fas fa-lock"></i> P2P DIRECT MESSAGE</span>
                    <span id="np-comms-status">READY</span>
                </div>
            </div>

        </div>
    `;

    const thread = document.getElementById('np-msg-thread');
    if (thread) setTimeout(() => thread.scrollTop = thread.scrollHeight, 50);

    // Focus input immediately
    setTimeout(() => document.getElementById('np-msg-input')?.focus(), 80);
}

/* ── Build a single message bubble (matches C2CommsApp style) ─────────── */
function _buildMsgBubble(m, nid, localId) {
    const senderId = m.from_id || m.fromId || '';
    const isOut    = localId ? senderId === localId : m.to_id === nid;
    const text     = _esc(m.text || m.decoded?.text || m.decoded?.payload || '');
    const status   = m.status || 'SENT';
    const msgId    = m.mesh_packet_id || m.id || '';
    const evtId    = m.packet_event_id || '';
    const time     = _fmtTime(m.timestamp);

    const node     = window.meshState?.nodes?.[senderId] || {};
    const snr      = m.rx_snr  ?? null;
    const rssi     = m.rx_rssi ?? null;

    // Status icon for outbound
    const statusIcon = isOut ? `
        <span class="np-ack-icon" data-msgid="${msgId}" style="margin-left:4px;color:${
            status==='DELIVERED'?'#00c8f5':status==='FAILED'?'#ff3050':'var(--txt3,#4a6a88)'
        };">
            <i class="fas ${status==='DELIVERED'?'fa-check-double':status==='FAILED'?'fa-exclamation-circle':'fa-check'}"></i>
        </span>` : '';

    const metaItems = [
        time,
        isOut && status ? `<span class="np-msg-status-${status}" style="color:${status==='DELIVERED'?'#00c8f5':status==='FAILED'?'#ff3050':status==='BROADCAST'?'var(--acc,#00c8f5)':'var(--txt3,#4a6a88)'}">${status}</span>` : null,
        snr  != null ? `<span style="color:${_snrColor(snr)}">${snr.toFixed(1)}dB SNR</span>` : null,
        rssi != null ? `<span>${rssi}dBm</span>` : null,
    ].filter(Boolean).join(' · ');

    const align = isOut ? 'flex-end' : 'flex-start';
    const bubbleStyle = isOut
        ? 'background:rgba(0,232,122,.07);border:1px solid rgba(0,232,122,.22);border-right:2px solid #00e87a;align-self:flex-end;'
        : 'background:var(--bg2,#0d1a2d);border:1px solid var(--bd2,#1e3048);border-left:2px solid var(--acc,#00c8f5);align-self:flex-start;';

    return `<div class="np-msg-wrap"
        id="${evtId?`npm-${evtId}`:''}"
        data-msgid="${msgId}"
        style="display:flex;flex-direction:column;align-items:${align};max-width:92%;">
        <div style="${bubbleStyle}padding:7px 10px;border-radius:4px;font-family:var(--mono,'Fira Code',monospace);font-size:11px;line-height:1.5;word-break:break-word;">
            <div>${text}</div>
            <div style="font-size:8px;color:var(--txt3,#4a6a88);margin-top:3px;display:flex;align-items:center;gap:2px;">
                ${metaItems}${statusIcon}
            </div>
        </div>
    </div>`;
}

/* ── Update ACK status on an existing bubble ──────────────────────────── */
function _updatePanelMsgStatus(packetId, status) {
    if (!packetId) return;
    const wrap = document.querySelector(`#np-msg-thread [data-msgid="${packetId}"]`);
    if (!wrap) return;
    const icon = wrap.querySelector('.np-ack-icon');
    if (!icon) return;
    const col  = status==='DELIVERED'?'#00c8f5':status==='FAILED'?'#ff3050':'var(--txt3,#4a6a88)';
    const cls  = status==='DELIVERED'?'fa-check-double':status==='FAILED'?'fa-exclamation-circle':'fa-check';
    icon.style.color = col;
    icon.innerHTML   = `<i class="fas ${cls}"></i>`;
}

/* ── Poll active chat for new messages ────────────────────────────────── */
let _panelPollTimer = null;

function _startCommsPoll(nid) {
    clearInterval(_panelPollTimer);
    _panelPollTimer = setInterval(() => _pollComms(nid), 5000);
}

function _stopCommsPoll() {
    clearInterval(_panelPollTimer);
    _panelPollTimer = null;
}

async function _pollComms(nid) {
    // Only poll if panel is open and on comms tab
    const panel = document.getElementById('node-panel');
    if (!panel?.classList.contains('open')) { _stopCommsPoll(); return; }
    const pane  = document.getElementById('pane-comms');
    if (!pane?.classList.contains('active')) return;
    const thread = document.getElementById('np-msg-thread');
    if (!thread) { _stopCommsPoll(); return; }

    const localId = window.meshState?.local_node_id || '';
    const _dmSQ = (window._activeSlotId && window._activeSlotId !== 'node_0') ? `&slot_id=${encodeURIComponent(window._activeSlotId)}` : '';
    try {
        const [r1, r2] = await Promise.all([
            fetch(`/api/messages/history?from_id=${encodeURIComponent(nid)}&limit=100${_dmSQ}`).then(r=>r.ok?r.json():[]),
            fetch(`/api/messages/history?to_id=${encodeURIComponent(nid)}&limit=100${_dmSQ}`).then(r=>r.ok?r.json():[]),
        ]);
        const msgMap = {};
        [...r1,...r2].forEach(m => { const k=m.id||m.packet_event_id; if(k) msgMap[k]=m; });
        const msgs = Object.values(msgMap).sort((a,b)=>a.timestamp-b.timestamp);

        let addedNew = false;
        msgs.forEach(m => {
            const evtId = m.packet_event_id;
            const pktId = m.mesh_packet_id||m.id||'';
            const byEvt = evtId ? document.getElementById(`npm-${evtId}`) : null;
            const byPkt = pktId ? document.querySelector(`#np-msg-thread [data-msgid="${pktId}"]`) : null;
            if (!byEvt && !byPkt) {
                thread.insertAdjacentHTML('beforeend', _buildMsgBubble(m, nid, localId));
                addedNew = true;
            } else {
                // Update status in case ACK arrived
                _updatePanelMsgStatus(pktId, m.status);
            }
        });
        if (addedNew) thread.scrollTop = thread.scrollHeight;
    } catch(e) {}
}

/* ── Hook SSE message_status_update for ACK delivery in panel ─────────── */
(function _hookPanelSSEAck() {
    // Retry until the SSE EventSource `es` is available (loaded by app.js)
    const _try = () => {
        if (typeof es !== 'undefined' && es !== null) {
            es.addEventListener('message_status_update', e => {
                try {
                    const data = JSON.parse(e.data);
                    _updatePanelMsgStatus(data.mesh_packet_id, data.status);
                } catch(_) {}
            });
        } else {
            setTimeout(_try, 1000);
        }
    };
    _try();
})();

/* ── Send message from panel ───────────────────────────────────────────── */
window.c2SendFromPanel = async function() {
    const nid    = window.C2MapApp._activeNodeId;
    const input  = document.getElementById('np-msg-input');
    const chSel  = document.getElementById('np-ch-sel');
    const status = document.getElementById('np-comms-status');
    if (!nid || !input) return;
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    const charCount = document.getElementById('np-char-count');
    if (charCount) charCount.textContent = '0/230';
    if (status) { status.textContent = 'TRANSMITTING...'; status.style.color = '#ffa826'; }

    // Ensure thread container exists (first message ever scenario)
    let thread = document.getElementById('np-msg-thread');
    if (!thread) {
        const inner = document.getElementById('np-comms-inner');
        if (!inner) return;
        // Trigger a minimal render so the thread exists
        inner.innerHTML = `
            <div style="display:flex;flex-direction:column;height:100%;">
                <div class="np-section" style="flex-shrink:0;">COMMS THREAD</div>
                <div id="np-msg-thread" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:8px 0;min-height:0;max-height:320px;"></div>
                <div style="border-top:1px solid var(--bd2,#1e3048);padding:10px 0 4px;flex-shrink:0;">
                    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;">
                        <select id="np-ch-sel" class="inp mono" style="width:66px;font-size:10px;padding:4px;flex-shrink:0;">
                            <option value="0">CH 0</option><option value="1">CH 1</option>
                            <option value="2">CH 2</option><option value="3">CH 3</option>
                        </select>
                        <div style="flex:1;position:relative;background:var(--bg,#080f1a);border:1px solid var(--bd,#162338);border-radius:4px;display:flex;align-items:center;padding:0 8px;">
                            <span style="color:var(--ok,#00e87a);font-family:var(--mono,'Fira Code',monospace);margin-right:6px;font-size:11px;">$</span>
                            <input id="np-msg-input" class="inp mono"
                                style="border:none;background:transparent;flex:1;font-size:11px;padding:7px 0;"
                                placeholder="Secure message..." maxlength="230"
                                onkeydown="if(event.key==='Enter')window.c2SendFromPanel()"
                                oninput="const c=document.getElementById('np-char-count');if(c)c.textContent=this.value.length+'/230';">
                            <span id="np-char-count" style="font-size:8px;color:var(--txt3,#4a6a88);font-family:var(--mono,'Fira Code',monospace);white-space:nowrap;margin-left:4px;">0/230</span>
                        </div>
                        <button class="btn btn-acc" onclick="window.c2SendFromPanel()"
                            style="height:36px;padding:0 14px;font-size:10px;flex-shrink:0;">TX ↗</button>
                    </div>
                    <div style="display:flex;justify-content:space-between;font-size:8px;font-family:var(--mono,'Fira Code',monospace);color:var(--txt3,#4a6a88);">
                        <span><i class="fas fa-lock"></i> AES-256 P2P LINK</span>
                        <span id="np-comms-status">READY</span>
                    </div>
                </div>
            </div>`;
        thread = document.getElementById('np-msg-thread');
        _showTab('pane-comms');
    }

    const localId = window.meshState?.local_node_id || '';
    const tempId  = `tmp-${Date.now()}`;

    // Optimistic bubble
    const bubble = document.createElement('div');
    bubble.className = 'np-msg-wrap';
    bubble.setAttribute('data-msgid', tempId);
    bubble.style.cssText = 'display:flex;flex-direction:column;align-items:flex-end;max-width:92%;';
    bubble.innerHTML = `
        <div style="background:rgba(0,232,122,.07);border:1px solid rgba(0,232,122,.22);border-right:2px solid #00e87a;
            align-self:flex-end;padding:7px 10px;border-radius:4px;
            font-family:var(--mono,'Fira Code',monospace);font-size:11px;line-height:1.5;word-break:break-word;">
            <div>${_esc(text)}</div>
            <div style="font-size:8px;color:var(--txt3,#4a6a88);margin-top:3px;display:flex;align-items:center;gap:2px;">
                ${_fmtTime(Date.now()/1000)}
                · <span class="np-msg-status-SENT" style="color:var(--txt3,#4a6a88);">SENT</span>
                <span class="np-ack-icon" data-msgid="${tempId}" style="margin-left:4px;color:var(--txt3,#4a6a88);">
                    <i class="fas fa-check"></i>
                </span>
            </div>
        </div>`;
    thread.appendChild(bubble);
    thread.scrollTop = thread.scrollHeight;

    try {
        const res  = await fetch('/api/messages', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({message:text, destination:nid, channel:parseInt(chSel?.value||0), slot_id: window._activeSlotId || 'node_0'})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

        // Update temp bubble with real packet ID
        const realId = data.packet_id;
        if (realId) {
            bubble.setAttribute('data-msgid', realId);
            const ackIcon = bubble.querySelector('.np-ack-icon');
            if (ackIcon) ackIcon.setAttribute('data-msgid', realId);
        }

        if (status) { status.textContent = 'TRANSMITTED'; status.style.color = 'var(--ok,#00e87a)'; }
        window.triggerToast?.(`TX → ${nid}`, 'ok');

        // Start polling if not already running (ensures ACKs arrive)
        _startCommsPoll(nid);

    } catch(e) {
        // Mark as failed
        const ackIcon = bubble.querySelector('.np-ack-icon');
        if (ackIcon) { ackIcon.style.color='#ff3050'; ackIcon.innerHTML='<i class="fas fa-exclamation-circle"></i>'; }
        if (status) { status.textContent = 'TX FAILED'; status.style.color = 'var(--err,#ff3050)'; }
        window.triggerToast?.(`TX Failed: ${e.message}`, 'err');
    }

    // Reset status after 3s
    setTimeout(()=>{ const s=document.getElementById('np-comms-status'); if(s){s.textContent='READY';s.style.color='var(--txt3,#4a6a88)';} }, 3000);
};

/* ════════════════════════════════════════════════════════════════════════
 * SONAR PING SYSTEM
 * ════════════════════════════════════════════════════════════════════════ */
window.C2SonarPing = (() => {
    let canvas=null, ctx=null, rings=[], raf=null, running=false;
    const DURATION=2200, MAX_RINGS=20;

    function _ensureCanvas(){
        if(canvas) return;
        const mapEl=document.getElementById('main-c2-map');
        if(!mapEl) return;
        canvas=document.createElement('canvas');
        canvas.id='c2-sonar-canvas';
        mapEl.appendChild(canvas);
        _resize();
        window.addEventListener('resize',_resize);
    }

    function _resize(){
        if(!canvas) return;
        const mapEl=document.getElementById('main-c2-map');
        if(!mapEl) return;
        canvas.width=mapEl.offsetWidth; canvas.height=mapEl.offsetHeight;
        ctx=canvas.getContext('2d');
    }

    function _pxRadius(){
        const app=window.C2MapApp;
        if(!app?.map) return 120;
        try{
            const center=app.map.getCenter(), zoom=app.map.getZoom();
            const mpp=156543.03392*Math.cos(center.lat*Math.PI/180)/Math.pow(2,zoom);
            return Math.max(60,Math.min(400,500000/mpp));
        }catch(e){return 120;}
    }

    function _loop(now){
        if(!ctx||!canvas){running=false;return;}
        const app=window.C2MapApp;
        ctx.clearRect(0,0,canvas.width,canvas.height);
        rings=rings.filter(ring=>{
            const age=now-ring.born; if(age>DURATION) return false;
            const progress=Math.min(1,age/DURATION); // clamp: RAF timestamps can drift
            const eased=1-Math.pow(1-progress,2);
            const r=Math.max(0,eased*ring.maxR); // never negative
            const alpha=(1-progress)*0.8;
            if(ring.latLng&&app.map){try{const pt=app.map.latLngToContainerPoint(ring.latLng);ring.x=pt.x;ring.y=pt.y;}catch(e){}}
            if(r>0){ctx.beginPath();ctx.arc(ring.x,ring.y,r,0,Math.PI*2);ctx.strokeStyle=ring.color;ctx.lineWidth=Math.max(0.5,2.5*(1-progress));ctx.globalAlpha=alpha;ctx.stroke();ctx.globalAlpha=1;}
            if(progress>0.15){
                const ip=Math.min(1,(age-DURATION*0.15)/(DURATION*0.85));
                const ir=Math.max(0,(1-Math.pow(1-ip,2))*ring.maxR*0.45); // never negative
                if(ir>0){ctx.beginPath();ctx.arc(ring.x,ring.y,ir,0,Math.PI*2);ctx.strokeStyle=ring.color;ctx.lineWidth=Math.max(0.5,1.5*(1-ip));ctx.globalAlpha=(1-ip)*0.4;ctx.stroke();ctx.globalAlpha=1;}
            }
            return true;
        });
        if(rings.length>0){raf=requestAnimationFrame(_loop);}else{running=false;ctx.clearRect(0,0,canvas.width,canvas.height);}
    }

    return {
        fire(nodeId,color){
            _ensureCanvas(); if(!canvas||!ctx) return;
            const app=window.C2MapApp; if(!app?.map) return;
            let latLng=null;
            const marker=app.markers[nodeId]?.find(m=>m instanceof L.Marker&&m._nodeLatLng);
            if(marker) latLng=L.latLng(marker._nodeLatLng[0],marker._nodeLatLng[1]);
            else{
                const node=window.meshState?.nodes?.[nodeId];
                const lat=_pick(node,'position.latitude','latitude');
                const lon=_pick(node,'position.longitude','longitude');
                if(lat&&lon&&!(lat===0&&lon===0)) latLng=L.latLng(lat,lon);
            }
            if(!latLng) return;
            let x=0,y=0;
            try{const pt=app.map.latLngToContainerPoint(latLng);x=pt.x;y=pt.y;}catch(e){return;}
            if(rings.length>=MAX_RINGS) rings.shift();
            rings.push({latLng,x,y,maxR:_pxRadius(),color:color||'#00c8f5',born:performance.now()});
            if(!running){running=true;raf=requestAnimationFrame(_loop);}
        },
        resize(){_resize();}
    };
})();

/* ── SSE packet → sonar ping (3 interception methods) ─────────────────── */
(function(){
    function _ping(data){
        try{
            const fromId=data?.fromId||data?.from_id; if(!fromId) return;
            const app=window.C2MapApp; if(!app?.map) return;
            window.C2SonarPing.fire(fromId, app.nodeColors[fromId]||'#00c8f5');
        }catch(e){}
    }

    // Method 1: patch global SSE handler
    const orig=window.handleSSEEvent;
    if(typeof orig==='function'){
        window.handleSSEEvent=function(event,data){_ping_if_packet(event,data);return orig(event,data);};
    }
    function _ping_if_packet(event,data){if(event==='packet')_ping(data);}

    // Method 2: custom DOM event
    document.addEventListener('sse:packet',e=>_ping(e.detail));

    // Method 3: wrap EventSource
    const _NES=window.EventSource;
    window.EventSource=function(url,cfg){
        const es=new _NES(url,cfg);
        es.addEventListener('packet',e=>{try{_ping(JSON.parse(e.data));}catch(_){}});
        return es;
    };
    try{Object.keys(_NES).forEach(k=>{try{window.EventSource[k]=_NES[k];}catch(_){}});} catch(_){}
    window.EventSource.prototype=_NES.prototype;
})();

/* ── Pulse + pointer-events keyframes ─────────────────────────────────── */
(function(){
    if(!document.getElementById('c2-map-kf')){
        const s=document.createElement('style');
        s.id='c2-map-kf';
        s.innerHTML=`@keyframes pulse{0%,100%{transform:scale(1);opacity:.35}50%{transform:scale(1.5);opacity:.1}}`;
        document.head.appendChild(s);
    }
})();

/* ════════════════════════════════════════════════════════════════════════
 * TRACEROUTE MAP OVERLAY
 * Draws recent traceroutes as animated dashed arcs on the map.
 * Toggle via ROUTES checkbox in toolbar.
 * ════════════════════════════════════════════════════════════════════════ */
window.C2TracerouteOverlay = (() => {
    let _lines = [];    // L.Polyline array currently on map
    let _labels = [];   // L.Marker tooltip labels
    let _visible = false;
    let _lastFetch = 0;

    function _colForHops(hops) {
        if (hops <= 1) return '#00e87a';
        if (hops <= 2) return '#00c8f5';
        if (hops <= 3) return '#ffa826';
        return '#ff3050';
    }

    function _animStyle(hops) {
        // dash offset: longer routes get slower animation
        return { dashArray: '10 6', dashOffset: '0' };
    }

    function _clear() {
        const app = window.C2MapApp;
        if (!app.map) return;
        _lines.forEach(l => { try { app.map.removeLayer(l); } catch(e) {} });
        _labels.forEach(l => { try { app.map.removeLayer(l); } catch(e) {} });
        _lines = []; _labels = [];
    }

    async function _fetch() {
        const now = Date.now();
        if (now - _lastFetch < 10000) return; // debounce
        _lastFetch = now;
        const sq = (window._activeSlotId && window._activeSlotId !== 'node_0')
            ? `?slot_id=${encodeURIComponent(window._activeSlotId)}&limit=200`
            : '?limit=200';
        try {
            return await fetch(`/api/traceroutes${sq}`).then(r => r.ok ? r.json() : []);
        } catch(e) { return []; }
    }

    async function _draw() {
        if (!_visible) return;
        const app = window.C2MapApp;
        if (!app.map) return;
        _clear();

        const routes = await _fetch();
        if (!routes?.length) return;

        const nodes = window.meshState?.nodes || {};

        // Only show routes from the last 6 hours, deduplicated by from→to
        const now = Date.now() / 1000;
        const seen = new Set();
        const recent = routes
            .filter(r => r.timestamp && (now - r.timestamp) < 21600)
            .sort((a, b) => b.timestamp - a.timestamp);

        for (const tr of recent) {
            const key = [tr.from_id, tr.to_id].sort().join('|');
            if (seen.has(key)) continue;
            seen.add(key);

            let rp = tr.route_path;
            if (typeof rp === 'string') { try { rp = JSON.parse(rp); } catch(e) { rp = {}; } }

            // Build node chain: from → via[] → to
            const chain = [tr.from_id, ...(rp?.route_to || []), tr.to_id];
            const hops = Math.max(1, chain.length - 1);
            const col = _colForHops(hops);

            // Resolve coordinates for each hop
            const coords = [];
            for (const nid of chain) {
                const n = nodes[nid];
                if (!n) continue;
                const lat = _pick(n, 'position.latitude', 'latitude');
                const lon = _pick(n, 'position.longitude', 'longitude');
                if (typeof lat === 'number' && lat !== 0 && typeof lon === 'number') {
                    coords.push([lat, lon, nid]);
                }
            }
            if (coords.length < 2) continue;

            // Draw segment-by-segment so each hop is clickable
            for (let i = 0; i < coords.length - 1; i++) {
                const a = coords[i], b = coords[i + 1];
                const line = L.polyline([[a[0], a[1]], [b[0], b[1]]], {
                    color: col,
                    weight: 2,
                    opacity: 0.75,
                    dashArray: '10 6',
                    lineCap: 'round',
                });
                const fromName = _nodeLabel(nodes[a[2]]) || a[2];
                const toName   = _nodeLabel(nodes[b[2]]) || b[2];
                line.bindTooltip(
                    `<b style="color:${col}">${fromName} → ${toName}</b><br/>` +
                    `Route: ${chain.map(id => _nodeLabel(nodes[id]) || id).join(' → ')}<br/>` +
                    `${hops} hop${hops !== 1 ? 's' : ''} · ${_fmtDateTime(tr.timestamp)}`,
                    { sticky: true, className: 'c2-tr-tip' }
                );
                line.addTo(app.map);
                _lines.push(line);
            }

            // Midpoint hop-count label
            const mid = coords[Math.floor(coords.length / 2)];
            if (mid) {
                const lbl = L.marker([mid[0], mid[1]], {
                    icon: L.divIcon({
                        html: `<div style="background:${col};color:#000;font-family:monospace;font-size:8px;font-weight:900;padding:1px 4px;border-radius:2px;white-space:nowrap;pointer-events:none;">${hops}H</div>`,
                        className: '',
                        iconAnchor: [10, 8],
                    }),
                    interactive: false,
                });
                lbl.addTo(app.map);
                _labels.push(lbl);
            }
        }

        // Inject CSS animation for dashes if not present
        if (!document.getElementById('c2-tr-anim')) {
            const s = document.createElement('style');
            s.id = 'c2-tr-anim';
            s.textContent = `
                .c2-tr-tip { background: var(--bg2,#0d1a2d)!important; border: 1px solid var(--bd2,#1e3048)!important;
                    color: var(--txt,#c8d8e8)!important; font-family: var(--mono,monospace)!important;
                    font-size: 9px!important; padding: 6px 8px!important; border-radius:3px!important; }
                .c2-tr-tip .leaflet-tooltip-tip { display: none!important; }
                @keyframes c2-dash { to { stroke-dashoffset: -32; } }
                .c2-route-animated path { animation: c2-dash 1.2s linear infinite; }
            `;
            document.head.appendChild(s);
        }
    }

    return {
        toggle(on) {
            _visible = on;
            if (on) _draw();
            else _clear();
        },
        refresh() { if (_visible) { _lastFetch = 0; _draw(); } },
        clear() { _clear(); _visible = false; },
    };
})();

window.c2ToggleTracerouteOverlay = function(on) {
    window.C2TracerouteOverlay.toggle(on);
    window.triggerToast?.(on ? 'Route overlay ON' : 'Route overlay OFF', on ? 'ok' : 'warn');
};


/* ════════════════════════════════════════════════════════════════════════
 * RANGE RINGS OVERLAY
 * Draws concentric circles from the local node at 1 / 5 / 10 / 20 / 50 km.
 * ════════════════════════════════════════════════════════════════════════ */
window.C2RangeRings = (() => {
    let _rings = [];
    const RADII_KM = [1, 5, 10, 20, 50];

    function _clear() {
        const app = window.C2MapApp;
        _rings.forEach(r => { try { app.map?.removeLayer(r); } catch(e) {} });
        _rings = [];
    }

    function _draw() {
        const app = window.C2MapApp;
        if (!app.map) return;
        _clear();

        // Find local node position
        const nodes = window.meshState?.nodes || {};
        const localId = window.meshState?.local_node_id ||
            Object.values(nodes).find(n => n.isLocal || n.is_local)?.node_id;
        const local = nodes[localId];
        const lat = _pick(local, 'position.latitude', 'latitude');
        const lon = _pick(local, 'position.longitude', 'longitude');

        if (!lat || !lon || (lat === 0 && lon === 0)) {
            window.triggerToast?.('Local node has no GPS — cannot draw range rings', 'warn');
            return;
        }

        RADII_KM.forEach((km, i) => {
            const alpha = 0.55 - i * 0.08;
            const circle = L.circle([lat, lon], {
                radius: km * 1000,
                color: '#00c8f5',
                weight: 1,
                opacity: alpha,
                fillColor: '#00c8f5',
                fillOpacity: 0.03,
                dashArray: i === 0 ? null : '6 4',
                interactive: false,
            });
            const label = L.marker([lat + (km / 111), lon], {
                icon: L.divIcon({
                    html: `<span style="font-family:monospace;font-size:8px;color:#00c8f5;opacity:.7;pointer-events:none;white-space:nowrap;">${km} km</span>`,
                    className: '',
                    iconAnchor: [-4, 8],
                }),
                interactive: false,
            });
            circle.addTo(app.map);
            label.addTo(app.map);
            _rings.push(circle, label);
        });
    }

    return {
        toggle(on) { if (on) _draw(); else _clear(); },
        clear() { _clear(); },
    };
})();

window.c2ToggleRangeRings = function(on) {
    window.C2RangeRings.toggle(on);
    if (on) window.triggerToast?.('Range rings ON — from local node', 'ok');
};


/* ════════════════════════════════════════════════════════════════════════
 * EMBED MODAL SYSTEM
 * Builds an iframe URL pointing to /map/embed with query params.
 * The server must serve that route without authentication.
 * ════════════════════════════════════════════════════════════════════════ */
window.c2OpenEmbedModal = function() {
    document.getElementById('map-embed-modal')?.classList.add('open');
    window.c2RebuildEmbedCode();
};

window.c2CloseEmbedModal = function() {
    document.getElementById('map-embed-modal')?.classList.remove('open');
};

/* ════════════════════════════════════════════════════════════════════════
 * OFFLINE MAPS MANAGER — download, upload, file management, status
 * ════════════════════════════════════════════════════════════════════════ */

// ── Tab switching ─────────────────────────────────────────────────────────
window._mapTabSwitch = function(tab) {
    const tabs = ['offline', 'overlays'];
    tabs.forEach(t => {
        const btn = document.getElementById('maptab-' + t);
        const panel = document.getElementById('maptab-' + t + '-panel');
        if (btn) btn.style.borderBottomColor = t === tab ? 'var(--acc)' : 'transparent';
        if (panel) panel.style.display = t === tab ? 'block' : 'none';
    });
};

// ── Format helpers ────────────────────────────────────────────────────────
function _fmtBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
    return (bytes / 1073741824).toFixed(2) + ' GB';
}
function _fmtSpeed(bps) {
    if (bps < 1024) return bps.toFixed(0) + ' B/s';
    if (bps < 1048576) return (bps / 1024).toFixed(0) + ' KB/s';
    return (bps / 1048576).toFixed(1) + ' MB/s';
}
function _fmtEta(secs) {
    if (!secs || secs <= 0) return '--';
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
}

// ── Open / Close modal ────────────────────────────────────────────────────
window.c2OpenHelpModal = function() {
    const modal = document.getElementById('map-help-modal');
    if (modal) {
        modal.style.display = 'flex';
        window.c2RefreshMapFiles();
    }
};

window.c2CloseHelpModal = function() {
    const modal = document.getElementById('map-help-modal');
    if (modal) modal.style.display = 'none';
};

// ── Offline status indicator ──────────────────────────────────────────────
window._c2UpdateOfflineIndicator = async function() {
    const el = document.getElementById('offline-map-indicator');
    if (!el) return;
    try {
        const res = await fetch('/api/map/status');
        if (!res.ok) throw new Error();
        const data = await res.json();
        if (data.active_file && data.available) {
            el.style.display = 'inline-block';
            el.textContent = '● ' + data.active_file.replace(/\.mbtiles$/i, '');
            el.title = 'Offline tiles active: ' + data.active_file;
        } else {
            el.style.display = 'none';
        }
    } catch {
        el.style.display = 'none';
    }
};
// Update indicator on load and periodically
setTimeout(() => window._c2UpdateOfflineIndicator(), 2000);

// ── Download from URL ─────────────────────────────────────────────────────
let _downloadEventSource = null;

window.c2StartMapDownload = async function() {
    const urlInput = document.getElementById('mbtiles-download-url');
    const url = (urlInput?.value || '').trim();
    if (!url) return window.triggerToast?.('Paste a download URL first', 'warn');

    const btn = document.getElementById('mbtiles-download-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'STARTING...'; }

    try {
        const res = await fetch('/api/map/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Download failed to start');

        // Show progress panel
        const prog = document.getElementById('map-download-progress');
        if (prog) prog.style.display = 'block';
        const fnEl = document.getElementById('mdp-filename');
        if (fnEl) fnEl.textContent = data.filename || 'Downloading...';

        // Connect to SSE progress stream
        _c2ConnectDownloadSSE();

        window.triggerToast?.('Download started: ' + (data.filename || url), 'ok');
    } catch (err) {
        window.triggerToast?.(err.message, 'err');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-cloud-download-alt"></i> DOWNLOAD'; }
    }
};

function _c2ConnectDownloadSSE() {
    if (_downloadEventSource) { _downloadEventSource.close(); _downloadEventSource = null; }

    const es = new EventSource('/api/map/download/progress');
    _downloadEventSource = es;

    const bar = document.getElementById('mdp-bar');
    const pctEl = document.getElementById('mdp-percent');
    const sizeEl = document.getElementById('mdp-size');
    const speedEl = document.getElementById('mdp-speed');
    const etaEl = document.getElementById('mdp-eta');
    const statusMsg = document.getElementById('mdp-status-msg');
    const cancelBtn = document.getElementById('mdp-cancel-btn');

    es.addEventListener('progress', (e) => {
        try {
            const d = JSON.parse(e.data);
            if (bar) bar.style.width = d.percent + '%';
            if (pctEl) pctEl.textContent = d.percent + '%';
            if (sizeEl) sizeEl.textContent = _fmtBytes(d.downloaded) + ' / ' + (d.total > 0 ? _fmtBytes(d.total) : '?');
            if (speedEl) speedEl.textContent = _fmtSpeed(d.speed);
            if (etaEl) etaEl.textContent = 'ETA: ' + _fmtEta(d.eta);

            if (d.status === 'complete') {
                if (bar) bar.style.background = 'var(--ok)';
                if (statusMsg) {
                    statusMsg.style.display = 'block';
                    statusMsg.style.color = 'var(--ok)';
                    statusMsg.textContent = '✓ Download complete! File saved as ' + d.filename;
                }
                if (cancelBtn) cancelBtn.style.display = 'none';
                window.triggerToast?.('Map download complete: ' + d.filename, 'ok');
                window.c2RefreshMapFiles();
                window._c2UpdateOfflineIndicator();
                es.close(); _downloadEventSource = null;
                // Auto-hide progress after 5s
                setTimeout(() => {
                    const prog = document.getElementById('map-download-progress');
                    if (prog) prog.style.display = 'none';
                    _resetDownloadUI();
                }, 5000);
            } else if (d.status === 'error') {
                if (bar) bar.style.background = 'var(--err)';
                if (statusMsg) {
                    statusMsg.style.display = 'block';
                    statusMsg.style.color = 'var(--err)';
                    statusMsg.textContent = '✕ Error: ' + (d.error || 'Download failed');
                }
                if (cancelBtn) cancelBtn.style.display = 'none';
                window.triggerToast?.('Download failed: ' + (d.error || 'Unknown error'), 'err');
                es.close(); _downloadEventSource = null;
            } else if (d.status === 'cancelled') {
                if (statusMsg) {
                    statusMsg.style.display = 'block';
                    statusMsg.style.color = 'var(--txt3)';
                    statusMsg.textContent = 'Download cancelled.';
                }
                if (cancelBtn) cancelBtn.style.display = 'none';
                es.close(); _downloadEventSource = null;
                setTimeout(() => {
                    const prog = document.getElementById('map-download-progress');
                    if (prog) prog.style.display = 'none';
                    _resetDownloadUI();
                }, 3000);
            }
        } catch { /* ignore parse errors */ }
    });

    es.onerror = () => {
        es.close(); _downloadEventSource = null;
    };
}

function _resetDownloadUI() {
    const bar = document.getElementById('mdp-bar');
    const cancelBtn = document.getElementById('mdp-cancel-btn');
    const statusMsg = document.getElementById('mdp-status-msg');
    if (bar) { bar.style.width = '0%'; bar.style.background = 'var(--acc)'; }
    if (cancelBtn) cancelBtn.style.display = 'inline-block';
    if (statusMsg) statusMsg.style.display = 'none';
}

window.c2CancelMapDownload = async function() {
    try {
        await fetch('/api/map/download/cancel', { method: 'POST' });
        window.triggerToast?.('Cancelling download...', 'warn');
    } catch (err) {
        window.triggerToast?.('Failed to cancel: ' + err.message, 'err');
    }
};

// ── File manager ──────────────────────────────────────────────────────────
window.c2RefreshMapFiles = async function() {
    const container = document.getElementById('map-files-list');
    if (!container) return;
    try {
        const res = await fetch('/api/map/files');
        if (!res.ok) throw new Error('Failed to load');
        const data = await res.json();
        const files = data.files || [];

        if (files.length === 0) {
            container.innerHTML = '<div style="font-size:9px; color:var(--txt3); padding:12px; text-align:center; border:1px dashed var(--bd); border-radius:4px;">No map archives found. Download or upload an .mbtiles file to get started.</div>';
            return;
        }

        let html = '<table style="width:100%; border-collapse:collapse; font-size:10px;">';
        html += '<thead><tr style="color:var(--txt3); font-size:8px; letter-spacing:1px; border-bottom:1px solid var(--bd);">';
        html += '<th style="text-align:left; padding:4px 6px;">FILENAME</th>';
        html += '<th style="text-align:right; padding:4px 6px;">SIZE</th>';
        html += '<th style="text-align:right; padding:4px 6px;">DATE</th>';
        html += '<th style="text-align:center; padding:4px 6px;">STATUS</th>';
        html += '<th style="text-align:right; padding:4px 6px;">ACTIONS</th>';
        html += '</tr></thead><tbody>';

        files.forEach(f => {
            const dateStr = f.modified ? new Date(f.modified).toLocaleDateString([], { month:'short', day:'numeric', year:'numeric' }) : '—';
            const isActive = f.active;
            const rowBg = isActive ? 'rgba(0,200,100,.06)' : 'transparent';
            const activeBadge = isActive
                ? '<span style="font-size:8px; padding:2px 6px; background:rgba(0,200,100,.15); color:#00e87a; border-radius:3px; font-weight:800;">ACTIVE</span>'
                : '<span style="font-size:8px; color:var(--txt3);">—</span>';
            const activateBtn = isActive
                ? ''
                : '<button class="btn btn-sm" onclick="window.c2ActivateMapFile(\'' + _esc(f.filename) + '\')" style="font-size:8px; padding:2px 8px; margin-right:4px;" title="Set as active">ACTIVATE</button>';

            html += '<tr style="border-bottom:1px solid var(--bd); background:' + rowBg + ';">';
            html += '<td style="padding:6px; color:var(--txt); font-weight:600;"><i class="fas fa-database" style="margin-right:4px; color:var(--acc); font-size:9px;"></i>' + _esc(f.filename) + '</td>';
            html += '<td style="padding:6px; text-align:right; color:var(--txt2);">' + f.size_mb + ' MB</td>';
            html += '<td style="padding:6px; text-align:right; color:var(--txt3);">' + dateStr + '</td>';
            html += '<td style="padding:6px; text-align:center;">' + activeBadge + '</td>';
            html += '<td style="padding:6px; text-align:right; white-space:nowrap;">' + activateBtn;
            html += '<button class="btn btn-sm" onclick="window.c2DeleteMapFile(\'' + _esc(f.filename) + '\')" style="font-size:8px; padding:2px 8px; color:var(--err);" title="Delete">✕</button>';
            html += '</td></tr>';
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (err) {
        container.innerHTML = '<div style="font-size:9px; color:var(--err); padding:8px;">Error loading files: ' + _esc(err.message) + '</div>';
    }
};

window.c2ActivateMapFile = async function(filename) {
    try {
        const res = await fetch('/api/map/files/' + encodeURIComponent(filename) + '/activate', { method: 'PUT' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Activation failed');
        window.triggerToast?.('Activated: ' + filename, 'ok');
        window.c2RefreshMapFiles();
        window._c2UpdateOfflineIndicator();
        // If currently viewing offline tiles, refresh them
        const styleSelect = document.getElementById('map-style');
        if (styleSelect?.value === 'offline') {
            window.c2ChangeMapStyle('offline');
        }
    } catch (err) {
        window.triggerToast?.('Failed to activate: ' + err.message, 'err');
    }
};

window.c2DeleteMapFile = async function(filename) {
    if (!confirm('Delete "' + filename + '"? This cannot be undone.')) return;
    try {
        const res = await fetch('/api/map/files/' + encodeURIComponent(filename), { method: 'DELETE' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Delete failed');
        window.triggerToast?.('Deleted: ' + filename, 'ok');
        window.c2RefreshMapFiles();
        window._c2UpdateOfflineIndicator();
    } catch (err) {
        window.triggerToast?.('Failed to delete: ' + err.message, 'err');
    }
};

// ── Upload (updated to refresh file list) ─────────────────────────────────
window.c2UploadMBTiles = async function() {
    const input = document.getElementById('mbtiles-upload');
    const statusEl = document.getElementById('upload-status');
    if (!input.files.length) return window.triggerToast?.('Please select an .mbtiles file', 'warn');

    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);

    statusEl.style.display = 'block';
    statusEl.style.color = 'var(--txt3)';
    statusEl.textContent = 'UPLOADING ' + file.name + ' (' + _fmtBytes(file.size) + ')... PLEASE WAIT.';

    try {
        const res = await fetch('/api/map/upload_tiles', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Upload failed');

        statusEl.style.color = 'var(--ok)';
        statusEl.textContent = '✓ UPLOADED: ' + data.filename;
        window.triggerToast?.('Offline map uploaded!', 'ok');
        setTimeout(() => statusEl.style.display = 'none', 4000);
        input.value = '';
        window.c2RefreshMapFiles();
        window._c2UpdateOfflineIndicator();
    } catch (err) {
        statusEl.style.color = 'var(--err)';
        statusEl.textContent = '✕ ERROR: ' + err.message;
    }
};

window.c2RebuildEmbedCode = function() {
    const g = id => document.getElementById(id);
    const title    = (g('mem-title')?.value || '').trim();
    const style    = g('mem-style')?.value || 'dark';
    const zoom     = parseInt(g('mem-zoom')?.value || '7');
    const filter   = (g('mem-filter')?.value || '').trim();
    const age      = parseInt(g('mem-age')?.value || '168');
    const trails   = g('mem-trails')?.checked   ? '1' : '0';
    const links    = g('mem-links')?.checked    ? '1' : '0';
    const gpsOnly  = g('mem-gps-only')?.checked ? '1' : '0';
    const online   = g('mem-online-only')?.checked ? '1' : '0';

    const params = new URLSearchParams({
        embed: '1',
        style,
        zoom: zoom || 7,
        ...(filter  ? { filter }  : {}),
        ...(age > 0 ? { age }     : {}),
        ...(trails  === '1' ? { trails: '1' }  : {}),
        ...(links   === '1' ? { links:  '1' }  : {}),
        ...(gpsOnly === '1' ? { gps:    '1' }  : {}),
        ...(online  === '1' ? { online: '1' }  : {}),
        ...(title   ? { title }   : {}),
        slot: window._activeSlotId || 'node_0',
    });

    const base = `${location.protocol}//${location.host}/map/embed`;
    const url  = `${base}?${params.toString()}`;
    const code = `<iframe src="${url}" width="800" height="500" frameborder="0" style="border:1px solid #1e3048;border-radius:4px;" allowfullscreen></iframe>`;
    const el = g('mem-code');
    if (el) el.textContent = code;
};

window.c2CopyEmbedCode = function() {
    const code = document.getElementById('mem-code')?.textContent || '';
    if (!code || code === '—') return;
    navigator.clipboard.writeText(code).then(() => {
        const copied = document.getElementById('mem-copied');
        if (copied) {
            copied.style.display = 'inline';
            setTimeout(() => { copied.style.display = 'none'; }, 2200);
        }
        window.triggerToast?.('Embed code copied', 'ok');
    }).catch(() => {
        // Fallback: select the element text
        const el = document.getElementById('mem-code');
        if (el) { const r = document.createRange(); r.selectNode(el); window.getSelection().removeAllRanges(); window.getSelection().addRange(r); }
        window.triggerToast?.('Select + copy the code above', 'warn');
    });
};

// Close embed modal on Escape
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        window.c2CloseEmbedModal();
    }
});

/* ════════════════════════════════════════════════════════════════════════
 * EMBED / PUBLIC MAP MODE
 * When the URL contains ?embed=1 (served at /map/embed by the backend),
 * strip all chrome and show a read-only clean map.
 * The backend should serve this HTML without auth middleware.
 * ════════════════════════════════════════════════════════════════════════ */
(function _initEmbedMode() {
    const params = new URLSearchParams(location.search);
    if (params.get('embed') !== '1') return;

    // Hide all non-map chrome immediately
    const style = document.createElement('style');
    style.id = 'embed-mode-styles';
    style.textContent = `
        #map-toolbar, #map-filter-drawer, #map-embed-modal,
        .sidebar, .nav, #topbar, #sidebar, .ct, #map-embed-btn,
        #map-filter-btn, #map-auto-ref, #map-stats-bar { display: none !important; }
        #main-c2-map { border: none !important; }
        body, html { background: #060b12 !important; margin: 0; padding: 0; overflow: hidden; }
        .vs { height: 100vh !important; }
        #node-panel { display: none !important; }
    `;
    document.head.appendChild(style);

    // Apply embed params to the MQTT-style filter so map.js can use them
    window.addEventListener('load', () => {
        const style_  = params.get('style')  || 'dark';
        const zoom    = parseInt(params.get('zoom') || '7');
        const filter  = params.get('filter') || '';
        const age     = parseInt(params.get('age') || '0');
        const trails  = params.get('trails') === '1';
        const links   = params.get('links')  === '1';
        const gps     = params.get('gps')    === '1';
        const online  = params.get('online') === '1';
        const title   = params.get('title')  || '';
        const slot    = params.get('slot')   || 'node_0';

        // Set active slot if embedded
        window._activeSlotId = slot;

        // Inject embed header bar
        const header = document.createElement('div');
        header.style.cssText = `
            position: fixed; top: 0; left: 0; right: 0; z-index: 5000;
            background: rgba(6,11,18,.88); border-bottom: 1px solid #1e3048;
            font-family: monospace; font-size: 10px; color: #4a6a88;
            display: flex; align-items: center; gap: 10px; padding: 4px 12px;
            pointer-events: none; backdrop-filter: blur(4px);
        `;
        header.innerHTML = `
            <span style="color:#00c8f5; font-weight:800;">◫ MESH</span>
            ${title ? `<span style="color:#8aa0b8;">${title}</span>` : ''}
            <span id="embed-node-count" style="margin-left:auto;">LOADING...</span>
        `;
        document.body.appendChild(header);

        // Inject live node count updater
        setInterval(() => {
            const nodes = window.meshState?.nodes || {};
            const total = Object.keys(nodes).length;
            const online_ = Object.values(nodes).filter(n => {
                const lh = n.lastHeard || n.last_heard || 0;
                return lh && (Date.now()/1000 - lh) < 3600;
            }).length;
            const el = document.getElementById('embed-node-count');
            if (el) el.textContent = `${total} NODES · ${online_} ONLINE`;
        }, 5000);

        // Store embed filter config so _mqttFilter / c2FetchAndDrawMap can consume it
        window._embedFilter = { maxNodes: 500, maxAgeDays: age / 24 || 0, nameFilter: filter,
            showTrails: trails, showNeighbors: links, onlyWithGps: gps, onlineOnly: online };

        // Override c2UpdateMapVisibility to also apply embed filters
        const _origVis = window.c2UpdateMapVisibility;
        window.c2UpdateMapVisibility = function() {
            // Apply embed filters to _mapFilters
            window._mapFilters.search = filter.toLowerCase();
            window._mapFilters.age    = age;
            window._mapFilters.gpsOnly = gps;
            window._mapFilters.onlineOnly = online;
            _origVis?.();
        };

        // Set map style after map initialises
        const _waitStyle = setInterval(() => {
            if (window.C2MapApp?.map) {
                clearInterval(_waitStyle);
                window.c2ChangeMapStyle(style_);
                if (zoom) {
                    setTimeout(() => {
                        try { window.C2MapApp.map.setZoom(zoom); } catch(e) {}
                    }, 1500);
                }
            }
        }, 200);
    });
})();

/* ════════════════════════════════════════════════════════════════════════
 * REFRESH HOOK — refresh overlays after map draw
 * ════════════════════════════════════════════════════════════════════════ */
(function _hookOverlayRefresh() {
    const _orig = window.c2FetchAndDrawMap;
    if (typeof _orig !== 'function') return;
    window.c2FetchAndDrawMap = async function() {
        const r = await _orig.apply(this, arguments);
        // Refresh active overlays after each draw cycle
        window.C2TracerouteOverlay?.refresh();
        // Update stats bar even if no filters active
        window.c2UpdateMapVisibility?.();
        return r;
    };
})();