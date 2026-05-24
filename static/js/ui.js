/* ==========================================================================
 * MeshDash C2 — UI Helpers, View Logic & Node Cards
 * ========================================================================== */

// --- 1. LEAFLET TILE FIX (Removes the strange white grid lines on maps) ---
const leafletFix = document.createElement('style');
leafletFix.innerHTML = `
    .leaflet-tile { margin-top: -1px !important; margin-left: -1px !important; width: 257px !important; height: 257px !important; }
    .leaflet-container { background: var(--bg2) !important; }
`;
document.head.appendChild(leafletFix);

// --- Global Utilities ---
window.escapeHtml = function(text) {
    if (text == null) return '';
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
};

window.fmtTime = function(ts) {
    if(!ts) return 'N/A';
    return new Date(ts * 1000).toTimeString().split(' ')[0]; // Strict HH:MM:SS
};

window.fmtTimeAgo = function(ts) {
    if (!ts) return 'N/A';
    const diff = Math.floor(Date.now()/1000 - ts);
    if (diff < 60) return `${diff}s`;
    if (diff < 3600) return `${Math.floor(diff/60)}m`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h`;
    return `${Math.floor(diff/86400)}d`;
};

window.fmtUptime = function(sec) {
    if (!sec) return '';
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
};

// Deep search utility to handle both flat and nested Python JSON payloads
window.getMeshVal = function(n, ...keys) {
    if (!n) return null;
    for (const k of keys) {
        if (n[k] != null) return n[k];
        for (const nest of ['deviceMetrics', 'user', 'position', 'localConfig', 'lora', 'environmentMetrics']) {
            if (n[nest] && n[nest][k] != null) return n[nest][k];
        }
    }
    return null;
};

// --- Safe Action Dispatcher (replaces eval() and new Function()) ---
window.SafeActions = window.SafeActions || {};
function safeExecAction(actionStr) {
    if (!actionStr) return;
    // Direct registry lookup
    if (window.SafeActions[actionStr]) {
        window.SafeActions[actionStr]();
        return;
    }
    // Parse "loadView('channels')" style
    const match = actionStr.match(/^([a-zA-Z_][a-zA-Z0-9_.]*)\(('([^']*)')\)$/);
    if (match) {
        const fnName = match[1];
        const arg = match[3];
        if (fnName === 'loadView' && typeof window.loadView === 'function') {
            window.loadView(arg);
            return;
        }
        if (fnName === 'window.C2Diagnostics.openModal' && window.C2Diagnostics) {
            window.C2Diagnostics.openModal();
            return;
        }
    }
    console.warn('Rejected unsafe action:', actionStr);
}

// --- Layout & Modals ---
window.toggleSidebar = function() {
    const sb = document.getElementById('sidebar');
    if (sb) sb.classList.toggle('open');
};

window.openInspector = function(id, name) {
    // Legacy inspector — opens the node detail modal instead.
    if (id) window.c2OpenNodeDetail(id);
};

window.closeInspector = function() {
    try {
        const el = document.getElementById('inspector');
        const bg = document.getElementById('ovBg');
        if (el) el.classList.remove('open');
        if (bg) bg.classList.remove('vis');
    } catch(e) {}
};

window.openModal = function() { 
    const m = document.getElementById('modal');
    if(m) { m.style.display = 'flex'; setTimeout(() => m.classList.add('open'), 10); }
};

window.closeModal = function() { 
    const m = document.getElementById('modal');
    if(m) { m.classList.remove('open'); setTimeout(() => m.style.display = 'none', 200); }
};



window.showModal = function({ id, icon, title, message, warning, buttons, showProgress }) {
    const modal = document.createElement('div');
    modal.className = 'modal-overlay ov vis';
    modal.id = id;
    modal.style.zIndex = "9999";
    modal.style.display = "flex";
    modal.style.alignItems = "center";
    modal.style.justifyContent = "center";
    
    let btnHtml = buttons.map((btn, i) => `<button class="btn ${window.escapeHtml(btn.class)}" data-modal-action="${i}">${window.escapeHtml(btn.text)}</button>`).join('');

    modal.innerHTML = `
        <div class="modal-inner" style="max-width:400px; text-align:center;">
            <div style="font-size:3rem; margin-bottom:10px;">${icon}</div>
            <h2 style="color:var(--txt); margin-bottom:10px; font-family:var(--mono);">${title}</h2>
            <div style="color:var(--txt2); font-size:12px; margin-bottom:15px;">${message}</div>
            ${warning ? `<div style="background:rgba(255,168,38,0.1); border:1px solid rgba(255,168,38,0.3); color:var(--warn); padding:10px; border-radius:4px; font-family:var(--mono); font-size:10px; margin-bottom:15px;">⚠️ ${warning}</div>` : ''}
            ${showProgress ? `<div style="height:4px; background:var(--bd2); border-radius:2px; margin-bottom:10px; overflow:hidden;"><div id="modal-progress-bar" style="height:100%; width:0%; background:var(--acc); transition:width 0.3s;"></div></div><div id="modal-status-area" style="font-family:var(--mono); font-size:10px; color:var(--txt3);"></div>` : ''}
            <div style="display:flex; justify-content:center; gap:10px; margin-top:20px;" id="modal-actions">
                ${btnHtml}
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    // Wire action buttons via addEventListener — avoids actionStr XSS
    buttons.forEach((btn, i) => {
        const el = modal.querySelector(`[data-modal-action="${i}"]`);
        if (!el) return;
        el.addEventListener('click', () => {
            try { modal.remove(); } catch(e) {}
            if (typeof btn.action === 'function') {
                try { btn.action(); } catch(e) {}
            } else if (btn.actionStr) {
                try { safeExecAction(btn.actionStr); } catch(e) {}
            }
        });
    });
    return modal;
};


/* ==========================================================================
 * OVERVIEW MODULE — removed; all rendering lives in overview.js
 * ========================================================================== */

let c2DetailMapInstance = null;
let c2TelemetryChartInstance = null;

/* ==========================================================================
 * DEEP-DIVE NODE MODAL (6-TABS)
 * ========================================================================== */
window.c2OpenNodeDetail = function(nodeId) {
    const node = window.meshState.nodes[nodeId];
    if(!node) return;

    const m = document.getElementById('modal');
    const inner = m.querySelector('.modal-inner');
    
    inner.style.width = '95%';
    inner.style.maxWidth = '1100px';
    inner.style.margin = '20px auto';

    const title = window.getMeshVal(node, 'long_name', 'longName', 'short_name', 'shortName') || node.node_id;

    inner.innerHTML = `
        <div class="modal-head" style="display:flex; justify-content:space-between; align-items:center; padding:16px 24px; border-bottom:1px solid var(--bd2);">
            <h3 id="modalTitle" style="margin:0; font-size:16px; letter-spacing:1px; text-transform:uppercase;">
                <i class="fas fa-microchip" style="color:var(--acc);"></i> ${title} 
                <span style="font-size:11px; color:var(--txt3); margin-left:12px; font-family:var(--mono); border-left:1px solid var(--bd); padding-left:12px;">${window.escapeHtml(nodeId)}</span>
            </h3>
            <button class="btn btn-sm" onclick="closeModal()" style="min-width:auto;">✕</button>
        </div>

        <div id="c2-modal-tabs" data-node-id="${window.escapeHtml(nodeId)}" style="display:flex; gap:8px; border-bottom:1px solid var(--bd2); margin:16px 24px 12px; padding-bottom:12px; overflow-x:auto; white-space:nowrap;">
            <button class="btn btn-sm btn-acc" data-tab="overview">OVERVIEW</button>
            <button class="btn btn-sm" data-tab="map">MAP</button>
            <button class="btn btn-sm" data-tab="telemetry">TELEMETRY</button>
            <button class="btn btn-sm" data-tab="position">POSITION</button>
            <button class="btn btn-sm" data-tab="messages">MESSAGES</button>
            <button class="btn btn-sm" data-tab="packets">PACKETS</button>
        </div>

        <div id="c2-modal-content" style="min-height: 400px; max-height:70vh; overflow-y:auto; padding: 0 24px 24px; width:100%; box-sizing:border-box;">
            <div style="text-align:center; padding:80px; color:var(--txt3); font-family:var(--mono);">
                <i class="fas fa-satellite-dish fa-spin" style="font-size:32px; margin-bottom:15px; color:var(--acc);"></i><br>
                ESTABLISHING SECURE DATA LINK...
            </div>
        </div>

        <div style="display: flex; gap: 10px; padding: 16px 24px; justify-content: flex-end; border-top: 1px solid var(--bd2); background: rgba(0,0,0,0.2);">
            <button class="btn" onclick="closeModal()">DISCONNECT SYSTEM</button>
        </div>
    `;
    
    openModal();
    // Delegated tab click handler — avoids injecting nodeId into onclick attributes (XSS)
    const tabsEl = document.getElementById('c2-modal-tabs');
    if (tabsEl) {
        tabsEl.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-tab]');
            if (btn) {
                const nid = tabsEl.dataset.nodeId;
                c2SwitchModalTab(btn.dataset.tab, nid);
            }
        });
    }
    c2SwitchModalTab('overview', nodeId);
};

window.c2SwitchModalTab = async function(tab, nodeId) {
    const tabsContainer = document.getElementById('c2-modal-tabs');
    const tabs = tabsContainer ? tabsContainer.querySelectorAll('button[data-tab]') : [];
    tabs.forEach(t => { t.className = t.dataset.tab === tab ? 'btn btn-sm btn-acc' : 'btn btn-sm'; });

    const content = document.getElementById('c2-modal-content');
    content.innerHTML = `<div style="text-align:center; padding:50px; color:var(--txt3); font-family:var(--mono);"><i class="fas fa-circle-notch fa-spin" style="font-size:24px; margin-bottom:10px; color:var(--acc);"></i><br>Executing DB Query...</div>`;

    const node = window.meshState.nodes[nodeId];

    if (c2DetailMapInstance) { c2DetailMapInstance.remove(); c2DetailMapInstance = null; }
    if (c2TelemetryChartInstance) { c2TelemetryChartInstance.destroy(); c2TelemetryChartInstance = null; }

    try {
        if (tab === 'overview') {
            let hw = window.getMeshVal(node, 'hardware_model_string', 'hw_model_str', 'hardware_model', 'hw_model') || 'Unknown';
            if (hw.includes('.')) hw = hw.split('.').pop();
            const fw = window.getMeshVal(node, 'firmware_version', 'firmwareVersion') || 'Unknown';
            const role = window.getMeshVal(node, 'role') || 'CLIENT';
            const mac = window.getMeshVal(node, 'macaddr') || 'N/A';
            
            const bat = window.getMeshVal(node, 'battery_level', 'batteryLevel');
            const volt = window.getMeshVal(node, 'voltage');
            const chUtil = window.getMeshVal(node, 'channel_utilization', 'channelUtilization');
            const airTx = window.getMeshVal(node, 'air_util_tx', 'airUtilTx');

            const lat = window.getMeshVal(node, 'latitude');
            const lon = window.getMeshVal(node, 'longitude');
            const alt = window.getMeshVal(node, 'altitude');
            const sats = window.getMeshVal(node, 'sats_in_view', 'satsInView');

            content.innerHTML = `
                <div class="g2" style="margin-bottom:16px;">
                    <div style="background:var(--bg); border:1px solid var(--bd); padding:16px; border-radius:var(--r);">
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Hardware</span><span class="sv" style="color:var(--txt)">${hw}</span></div>
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Firmware</span><span class="sv" style="color:var(--acc)">${fw}</span></div>
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Role</span><span class="sv pill p-node">${role}</span></div>
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">MAC</span><span class="sv mono">${mac}</span></div>
                    </div>
                    <div style="background:var(--bg); border:1px solid var(--bd); padding:16px; border-radius:var(--r);">
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Battery</span><span class="sv pill p-ok">${bat ?? '-'}%</span></div>
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Voltage</span><span class="sv mono">${volt != null ? parseFloat(volt).toFixed(2) : '-'}V</span></div>
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Channel Util</span><span class="sv mono">${chUtil != null ? parseFloat(chUtil).toFixed(2) : '-'}%</span></div>
                        <div class="stat-row" style="border:none; padding:4px 0;"><span class="sk">Air Util (TX)</span><span class="sv mono">${airTx != null ? parseFloat(airTx).toFixed(2) : '-'}%</span></div>
                    </div>
                </div>
                ${lat ? `
                <div style="background:var(--bg); border:1px dashed var(--acc); padding:16px; border-radius:var(--r); margin-bottom:16px;">
                    <div style="font-size:10px; color:var(--acc); font-weight:800; margin-bottom:10px; font-family:var(--mono);">LAST KNOWN POSITION</div>
                    <div class="mono" style="color:var(--txt); font-size:16px;">${parseFloat(lat).toFixed(5)}°, ${parseFloat(lon).toFixed(5)}°</div>
                    <div style="font-size:11px; margin-top:6px; color:var(--txt2); font-family:var(--mono);">ALT: ${alt || 0}m | SATS: ${sats || 0}</div>
                </div>` : ''}
                <div style="font-size:10px; color:var(--txt3); margin-bottom:6px; font-weight:bold; font-family:var(--mono);">RAW DB DUMP</div>
                <pre class="jv" style="height:180px; font-size:11px;">${window.escapeHtml(JSON.stringify(node, null, 2))}</pre>
            `;
        } 
        
        else if (tab === 'map') {
            const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/history/positions?limit=100${window._activeSlotId&&window._activeSlotId!=='node_0'?'&slot_id='+encodeURIComponent(window._activeSlotId):''}`);
            if (!res.ok) throw new Error('positions fetch failed');
            const positions = await res.json();
            const hasPos = positions && positions.length > 0;

            content.innerHTML = `
                <div style="position:relative; height:350px; border-radius:var(--r); border:1px solid var(--bd2); box-shadow: 0 4px 15px rgba(0,0,0,0.5); overflow:hidden;">
                    <div id="c2-modal-map" style="width:100%; height:100%; ${hasPos ? '' : 'filter: grayscale(100%) opacity(0.3);'}"></div>
                    ${hasPos ? '' : '<div style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; z-index:1000; color:var(--warn); font-family:var(--mono); font-size:16px; font-weight:bold; pointer-events:none; text-shadow:0 2px 10px #000;">[ NO GPS FIX LOGGED ]</div>'}
                </div>
            `;
            
            if (typeof L === 'undefined') throw new Error('Leaflet not loaded yet');
            c2DetailMapInstance = L.map('c2-modal-map').setView(hasPos ? [positions[0].latitude, positions[0].longitude] : [20, 0], hasPos ? 14 : 2);
            L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png').addTo(c2DetailMapInstance);
            
            if (hasPos) {
                const latlngs = positions.map(p => [p.latitude, p.longitude]);
                L.polyline(latlngs, {color: '#00c8f5', weight: 2, opacity: 0.8}).addTo(c2DetailMapInstance);
                L.circleMarker(latlngs[0], {color: '#00e87a', fillColor:'#00e87a', fillOpacity: 0.8, radius: 6}).addTo(c2DetailMapInstance).bindPopup("Current Ping").openPopup();
            }
        }
        
        else if (tab === 'telemetry') {
            const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/history/telemetry?limit=50${window._activeSlotId&&window._activeSlotId!=='node_0'?'&slot_id='+encodeURIComponent(window._activeSlotId):''}`);
            if (!res.ok) throw new Error('telemetry fetch failed');
            const data = await res.json();
            
            if(!data || data.length === 0) {
                content.innerHTML = `<div style="text-align:center; padding:50px; background:var(--bg); border:1px solid var(--bd); border-radius:var(--r); color:var(--warn); font-family:var(--mono);">[ ERROR: NO TELEMETRY DB ENTRIES ]</div>`;
                return;
            }

            let rows = data.map(d => `
                <tr>
                    <td style="color:var(--txt3); font-family:var(--mono);">${window.fmtTime(d.timestamp)}</td>
                    <td style="color:var(--ok); font-weight:bold;">${d.battery_level ?? '-'}%</td>
                    <td style="font-family:var(--mono);">${d.voltage != null ? parseFloat(d.voltage).toFixed(2) : '-'}V</td>
                    <td style="font-family:var(--mono);">${d.channel_utilization != null ? parseFloat(d.channel_utilization).toFixed(1) : '-'}%</td>
                    <td style="font-family:var(--mono); color:var(--txt2);">${d.air_util_tx != null ? parseFloat(d.air_util_tx).toFixed(1) : '-'}%</td>
                </tr>
            `).join('');

            content.innerHTML = `
                <div class="cw" style="height: 200px; margin-bottom: 20px; background:var(--bg); border:1px solid var(--bd); padding:10px; border-radius:var(--r);"><canvas id="c2-modal-telemetry-chart"></canvas></div>
                <div class="tw"><table><thead><tr><th>Time</th><th>Battery</th><th>Voltage</th><th>Ch Util</th><th>Air Tx</th></tr></thead><tbody>${rows}</tbody></table></div>
            `;

            if (typeof Chart === 'undefined') throw new Error('Chart.js not loaded yet');
            const ctx = document.getElementById('c2-modal-telemetry-chart').getContext('2d');
            const revData = [...data].reverse();
            c2TelemetryChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: revData.map(d => new Date(d.timestamp * 1000)),
                    datasets: [
                        { label: 'Battery %', data: revData.map(d => d.battery_level), borderColor: '#00e87a', backgroundColor:'rgba(0,232,122,0.1)', fill:true, tension: 0.4 },
                        { label: 'Voltage', data: revData.map(d => d.voltage), borderColor: '#00c8f5', tension: 0.4, yAxisID: 'y1' }
                    ]
                },
                options: { responsive: true, maintainAspectRatio: false, scales: { x: { type: 'time', time: { unit: 'hour' }, grid:{color:'#1e3048'} }, y: { grid:{color:'#1e3048'} }, y1: { position: 'right', grid:{drawOnChartArea:false} } } }
            });
        }
        
        else if (tab === 'position') {
            const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/history/positions?limit=50${window._activeSlotId&&window._activeSlotId!=='node_0'?'&slot_id='+encodeURIComponent(window._activeSlotId):''}`);
            if (!res.ok) throw new Error('position fetch failed');
            const pos = await res.json();
            
            if(!pos || pos.length === 0) {
                content.innerHTML = `<div style="text-align:center; padding:50px; background:var(--bg); border:1px solid var(--bd); border-radius:var(--r); color:var(--warn); font-family:var(--mono);">[ ERROR: NO POSITION DB ENTRIES ]</div>`;
                return;
            }

            let rows = pos.map(p => `
                <tr>
                    <td style="color:var(--txt3); font-family:var(--mono);">${window.fmtTime(p.timestamp)}</td>
                    <td style="color:var(--warn); font-family:var(--mono);">${p.latitude != null ? parseFloat(p.latitude).toFixed(5) : '-'}</td>
                    <td style="color:var(--warn); font-family:var(--mono);">${p.longitude != null ? parseFloat(p.longitude).toFixed(5) : '-'}</td>
                    <td style="font-family:var(--mono);">${p.altitude ?? '-'}m</td>
                    <td style="color:var(--acc); font-family:var(--mono);">${p.sats_in_view ?? '-'}</td>
                </tr>
            `).join('');

            content.innerHTML = `<div class="tw"><table><thead><tr><th>Time</th><th>Lat</th><th>Lon</th><th>Alt</th><th>Sats</th></tr></thead><tbody>${rows}</tbody></table></div>`;
        }
        
        else if (tab === 'messages') {
            const _uiSQ = (window._activeSlotId && window._activeSlotId !== 'node_0') ? `&slot_id=${encodeURIComponent(window._activeSlotId)}` : '';
            const [resFrom, resTo] = await Promise.all([
                fetch(`/api/messages/history?from_id=${encodeURIComponent(nodeId)}&limit=50${_uiSQ}`),
                fetch(`/api/messages/history?to_id=${encodeURIComponent(nodeId)}&limit=50${_uiSQ}`)
            ]);
            const dataFrom = resFrom.ok ? (await resFrom.json().catch(() => [])) : [];
            const dataTo   = resTo.ok   ? (await resTo.json().catch(() => []))   : [];
            const combined = [...dataFrom, ...dataTo].sort((a,b) => b.timestamp - a.timestamp);

            let msgsHtml = combined.length === 0 ? `<div style="text-align:center; padding:40px; color:var(--txt3); font-family:var(--mono);">[ EMPTY MESSAGE LOG ]</div>` : combined.map(m => {
                const isRx = m.from_id === nodeId;
                return `
                <div class="mr ${isRx ? 'rx' : 'tx'}">
                    <div class="mmeta"><span class="mn">${isRx ? 'Them' : 'You'}</span><span style="margin-left:auto">${window.fmtTime(m.timestamp)}</span></div>
                    <div class="bub" style="font-size:13px;">${window.escapeHtml(m.text)}</div>
                </div>`;
            }).join('');

            content.innerHTML = `
                <div style="display:flex; gap:10px; margin-bottom:16px; background:var(--bg); padding:10px; border:1px solid var(--bd); border-radius:var(--r);">
                    <input type="text" id="c2-dm-input" class="inp" placeholder="Send Direct Message to ${window.escapeHtml(nodeId)}..." style="flex:1; font-size:13px;">
                    <button class="btn btn-acc" id="c2-dm-send-btn" data-target-node="${window.escapeHtml(nodeId)}"><i class="fas fa-paper-plane"></i> SEND</button>
                </div>
                <div class="msg-feed" style="height:320px; border:1px solid var(--bd2); border-radius:var(--r); background:var(--bg); padding:16px;">${msgsHtml}</div>
            `;
            // Wire send button without onclick injection
            const _dmBtn = document.getElementById('c2-dm-send-btn');
            if (_dmBtn) _dmBtn.addEventListener('click', () => window.c2SendDM(_dmBtn.dataset.targetNode));
            const _dmIn = document.getElementById('c2-dm-input');
            if (_dmIn) _dmIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') window.c2SendDM(_dmBtn?.dataset.targetNode || nodeId); });
        }
        
        else if (tab === 'packets') {
            const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/history/packets?limit=50${window._activeSlotId&&window._activeSlotId!=='node_0'?'&slot_id='+encodeURIComponent(window._activeSlotId):''}`);
            if (!res.ok) throw new Error('packets fetch failed');
            const pkts = await res.json();

            if(!pkts || pkts.length === 0) {
                content.innerHTML = `<div style="text-align:center; padding:50px; background:var(--bg); border:1px solid var(--bd); border-radius:var(--r); color:var(--warn); font-family:var(--mono);">[ NO PACKET HISTORY ]</div>`;
                return;
            }

            let rows = pkts.map(p => `
                <div style="border-bottom:1px dashed var(--bd); padding:10px 0; font-family:var(--mono); font-size:11px;">
                    <div style="display:flex; justify-content:space-between; color:var(--txt3); margin-bottom:6px; align-items:center;">
                        <span style="display:flex; align-items:center; gap:6px;">
                            ${window.fmtTime(p.timestamp)}
                            ${p.source ? `<span style="padding:1px 4px; background:${p.source==='RF'?'rgba(0,232,122,0.1)':p.source==='MQTT'?'rgba(176,96,255,0.1)':'rgba(255,255,255,0.1)'}; color:${p.source==='RF'?'var(--ok)':p.source==='MQTT'?'#b060ff':'var(--txt2)'}; border-radius:2px; font-size:9px; border:1px solid ${p.source==='RF'?'rgba(0,232,122,0.3)':p.source==='MQTT'?'rgba(176,96,255,0.3)':'rgba(255,255,255,0.3)'};" title="${p.source_reasons ? window.escapeHtml(p.source_reasons.join(' | ')) : ''}">${p.source} ${(p.source_confidence*100).toFixed(0)}%</span>` : ''}
                        </span>
                        <span style="color:var(--pur); font-weight:bold;">Type: ${p.packet_type || 'Unknown'}</span>
                    </div>
                    <div style="color:var(--txt2); word-break:break-all; background:var(--bg2); padding:6px; border-radius:4px; border:1px solid var(--bd2);">${window.escapeHtml(p.decoded ? JSON.stringify(p.decoded) : 'Raw Binary Data')}</div>
                </div>
            `).join('');

            content.innerHTML = `<div style="background:var(--bg); border:1px solid var(--bd); border-radius:var(--r); padding:16px; height:380px; overflow-y:auto; box-shadow:inset 0 0 15px rgba(0,0,0,0.5);">${rows}</div>`;
        }
    } catch (e) {
        content.innerHTML = `<div style="color:var(--err); padding:40px; background:rgba(255,48,80,0.1); border:1px solid var(--err); border-radius:var(--r); text-align:center; font-family:var(--mono);">[ CRITICAL ERROR: FAILED TO FETCH DB STATE ]</div>`;
    }
};

window.c2SendDM = async function(nodeId) {
    const input = document.getElementById('c2-dm-input');
    const text = input.value.trim();
    if (!text) return;
    
    try {
        const slotId = window._activeSlotId === 'all' ? 'node_0' : (window._activeSlotId || 'node_0');
        const res = await fetch('/api/messages', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ destination: nodeId, message: text, channel: 0, slot_id: slotId })
        });
        if (res.ok) {
            input.value = '';
            window.triggerToast('Message dispatched to network', 'ok');
            setTimeout(() => window.c2SwitchModalTab('messages', nodeId), 500); 
        } else {
            window.triggerToast('Failed to dispatch message', 'err');
        }
    } catch(e) {
        window.triggerToast('Network error during dispatch', 'err');
    }
};

/* ==========================================================================
 * SETTINGS MODULE
 * ========================================================================== */

const C2_CONFIG_KEYS = [
    'AUTH_SECRET_KEY', 'AUTH_TOKEN_EXPIRE_MINUTES',
    'MESHTASTIC_CONNECTION_TYPE', 'MESHTASTIC_SERIAL_PORT', 'MESHTASTIC_HOST', 'MESHTASTIC_PORT', 'MESHTASTIC_BLE_MAC',
    'WEBSERVER_HOST', 'WEBSERVER_PORT', 'NETWORK_WEBSERVER_PORT',
    'DB_PATH', 'NETWORK_DB_PATH', 'MAX_PACKETS_MEMORY', 'HISTORY_DAYS', 'LOG_LEVEL',
    'SEND_LOCAL_NODE_LOCATION', 'SEND_OTHER_NODES_LOCATION', 'LOCATION_OFFSET_ENABLED', 'LOCATION_OFFSET_METERS',
    'SCHEDULER_MAX_RETRIES', 'SCHEDULER_RETRY_DELAY_SECONDS', 'SCHEDULER_CONNECT_TIMEOUT', 'SCHEDULER_RW_TIMEOUT',
    'REMOTE_C2', 'C2_ACCESS_LEVEL',
    'COMMUNITY_API', 'PUBLIC_MODE'
    // HEARTBEAT API_KEY and URLs are hardcoded — always active, never user-configurable
];

window.c2InitSettings = async function() {
    try {
        const res = await fetch('/api/system/config');
        if (!res.ok) throw new Error('Failed to load');
        const data = await res.json();

        C2_CONFIG_KEYS.forEach(key => {
            const el = document.getElementById(key);
            if (!el) return;
            if (el.type === 'checkbox') el.checked = data[key] === true || data[key] === 'true';
            else el.value = data[key] ?? '';
        });

        window.c2ToggleSettingsFields();
        window.triggerToast("Configuration loaded from backend", "ok");
        if (typeof window.slotRefresh === 'function') window.slotRefresh();
    } catch (e) {
        window.triggerToast("Failed to load configuration", "err");
    }
};

window.c2ToggleSettingsFields = function() {
    const type = document.getElementById('MESHTASTIC_CONNECTION_TYPE')?.value;
    if (type) {
        document.getElementById('grp-SERIAL').style.display   = type === 'SERIAL'    ? 'block' : 'none';
        document.getElementById('grp-TCP').style.display      = type === 'TCP'       ? 'grid'  : 'none';
        document.getElementById('grp-BLE').style.display      = type === 'BLE'       ? 'block' : 'none';
        // grp-WEBSERIAL visibility is managed by the settings.html inline script
        // (it patches this function) — we just ensure the others are hidden
    }
    const offsetEnabled = document.getElementById('LOCATION_OFFSET_ENABLED')?.checked;
    const offsetGrp = document.getElementById('grp-OFFSET');
    if (offsetGrp) offsetGrp.style.display = offsetEnabled ? 'block' : 'none';
};

window.c2SaveSettings = async function() {
    const btn = document.getElementById('btn-save-settings');
    const originalHtml = btn.innerHTML;
    btn.innerHTML = '⚙ SAVING...';
    btn.disabled = true;

    const payload = {};
    C2_CONFIG_KEYS.forEach(key => {
        const el = document.getElementById(key);
        if (!el) return;
        if (el.type === 'checkbox') payload[key] = el.checked;
        else if (el.type === 'number') payload[key] = parseFloat(el.value) || 0;
        else payload[key] = el.value;
    });

    try {
        const res = await fetch('/api/system/config/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Save failed");

        btn.innerHTML = '✅ SAVED';
        btn.className = 'btn btn-ok';
        window.triggerToast("Settings saved. Restart required.", "warn");
        
        setTimeout(() => {
            btn.innerHTML = originalHtml;
            btn.className = 'btn btn-acc';
            btn.disabled = false;
        }, 2000);
    } catch (e) {
        btn.innerHTML = '❌ ERROR';
        btn.className = 'btn btn-err';
        window.triggerToast("Error saving configuration", "err");
        setTimeout(() => { btn.innerHTML = originalHtml; btn.className = 'btn btn-acc'; btn.disabled = false; }, 3000);
    }
};

window.c2RestartServer = function() {
    window.showModal({
        id: 'restart-confirm',
        icon: '⟳',
        title: 'RESTART SERVICE',
        message: 'Are you sure you want to restart the MeshDash backend?',
        warning: 'The dashboard will be unresponsive for 10-15 seconds.',
        buttons: [
            { text: 'CANCEL', class: 'btn-sm', action: function() {} },
            { text: 'RESTART NOW', class: 'btn-sm btn-err', action: function() { window.c2ExecuteRestart(); } }
        ],
        showProgress: false
    });
};

window.c2ExecuteRestart = function() {
    const m = window.showModal({
        id: 'restart-progress',
        icon: '<i class="fas fa-sync-alt fa-spin" style="color:var(--warn);"></i>',
        title: 'RESTARTING...',
        message: 'Please wait while the server reboots.',
        showProgress: true,
        buttons: []
    });

    const bar = document.getElementById('modal-progress-bar');
    const status = document.getElementById('modal-status-area');
    const actions = document.getElementById('modal-actions');

    try { fetch('/api/system/restart', { method: 'POST' }).catch(() => {}); } catch(e) {}

    bar.style.width = '20%';
    status.innerHTML = 'Sending command...';

    let attempts = 0;
    const checkInterval = setInterval(async () => {
        attempts++;
        const progress = Math.min(90, 20 + (attempts * 2));
        bar.style.width = `${progress}%`;
        status.innerHTML = `Waiting for server... (${attempts}/40)`;

        try {
            const res = await fetch('/api/status', { signal: AbortSignal.timeout(1500) });
            if (res.ok) {
                clearInterval(checkInterval);
                bar.style.width = '100%';
                bar.style.background = 'var(--ok)';
                m.querySelector('h2').innerText = 'ONLINE!';
                status.innerHTML = '<span style="color:var(--ok);">Server restarted successfully.</span>';
                actions.innerHTML = `<button class="btn btn-ok" onclick="window.location.reload()">RELOAD DASHBOARD</button>`;
            }
        } catch (e) {
            if (attempts >= 40) {
                clearInterval(checkInterval);
                bar.style.background = 'var(--err)';
                status.innerHTML = '<span style="color:var(--err);">Timeout: Server might still be booting.</span>';
                actions.innerHTML = `<button class="btn btn-acc" onclick="window.location.reload()">FORCE RELOAD</button>`;
            }
        }
    }, 1000);
};

window.showGlobalContextMenu = function(x, y) {
    const rootMenu = document.getElementById('custom-ctx-container')?.firstChild;
    if (!rootMenu) return;

    rootMenu.style.display = 'block';
    
    if (x + rootMenu.offsetWidth > window.innerWidth) x = window.innerWidth - rootMenu.offsetWidth;
    if (y + rootMenu.offsetHeight > window.innerHeight) y = window.innerHeight - rootMenu.offsetHeight;
    
    rootMenu.style.left = x + 'px';
    rootMenu.style.top = y + 'px';
};

window.hideGlobalContextMenu = function() {
    const rootMenu = document.getElementById('custom-ctx-container')?.firstChild;
    if (rootMenu) rootMenu.style.display = 'none';
};

window.initContextMenu = async function() {
    const ctxContainer = document.createElement('div');
    ctxContainer.id = 'custom-ctx-container';
    document.body.appendChild(ctxContainer);

    let rootMenu = null;

    function buildMenuElement(xmlNode, isRoot) {
        const menuDiv = document.createElement('div');
        menuDiv.className = isRoot ? 'ctx-menu' : 'ctx-menu ctx-submenu';
        
        Array.from(xmlNode.children).forEach(child => {
            if (child.tagName === 'item' || child.tagName === 'menu') {
                const itemDiv = document.createElement('div');
                itemDiv.className = 'ctx-item';
                
                let innerHTML = '';
                const icon = child.getAttribute('icon');
                if (icon) innerHTML += `<i class="${icon} ctx-icon"></i>`;
                
                const label = child.getAttribute('label') || (child.childNodes.length && child.childNodes[0].nodeType === 3 ? child.childNodes[0].nodeValue.trim() : '');
                innerHTML += `<span class="ctx-label">${label}</span>`;

                if (child.tagName === 'menu') {
                    itemDiv.classList.add('has-submenu');
                    innerHTML += `<i class="fas fa-chevron-right" style="font-size:10px; color:var(--txt3); margin-left:12px;"></i>`;
                    itemDiv.innerHTML = innerHTML;
                    
                    const submenu = buildMenuElement(child, false);
                    itemDiv.appendChild(submenu);
                } else {
                    itemDiv.innerHTML = innerHTML;
                    const action = child.getAttribute('action');
                    const href = child.getAttribute('href');
                    
                    itemDiv.onclick = (e) => {
                        e.stopPropagation();
                        if (action) safeExecAction(action);
                        else if (href) window.open(href, child.getAttribute('target') || '_self');
                        window.hideGlobalContextMenu();
                    };
                }
                menuDiv.appendChild(itemDiv);
            }
        });
        return menuDiv;
    }

    try {
        const res = await fetch('/static/context.xml');
        if (res.ok) {
            const text = await res.text();
            const parser = new DOMParser();
            const xml = parser.parseFromString(text, 'text/xml');
            const menuNode = xml.querySelector('menu');
            
            if (menuNode) {
                rootMenu = buildMenuElement(menuNode, true);
                ctxContainer.appendChild(rootMenu);
            }
        }
    } catch (err) {
        console.error('Context menu init failed:', err);
    }

    document.addEventListener('contextmenu', (e) => {
        if (!rootMenu) return;
        e.preventDefault();
        window.showGlobalContextMenu(e.pageX, e.pageY);
    });

    document.addEventListener('click', (e) => {
        if (e.button !== 2) window.hideGlobalContextMenu();
    });
};

document.addEventListener('DOMContentLoaded', window.initContextMenu);