'use strict';

// Global plugin permissions store (populated by _loadPluginBridges, used by _checkPermission)
window._pluginPermissions = Object.create(null); // { pluginId: Set([perm, ...]) }

const CONFIG = {
    get ssePath()              {
        const s = window._activeSlotId;
        if (!s || s === 'node_0') return '/sse';
        if (s === 'all') return '/sse/all';
        return '/sse/' + s;
    },
    get statusEndpoint()       { const s = window._activeSlotId || 'node_0'; return s === 'node_0' ? '/api/status' : '/api/status?slot_id=' + encodeURIComponent(s); },
    get localNodeFullEndpoint(){ const s = window._activeSlotId || 'node_0'; if (s === 'all') return '/api/local_node/full'; return s === 'node_0' ? '/api/local_node/full' : '/api/local_node/full?slot_id=' + encodeURIComponent(s); },
    get statsEndpoint()        { const s = window._activeSlotId || 'node_0'; return s === 'node_0' ? '/api/stats' : '/api/stats?slot_id=' + encodeURIComponent(s); },
    pluginsMenuEndpoint: '/api/system/plugins/menu',
    historyEndpoint: '/api/system/connection_history'
};

// ---------------------------------------------------------------------------
// Global multi-slot helpers
// ---------------------------------------------------------------------------

// Returns the label for a slot id (e.g. "node_0" → "PRIMARY" or slot label)
window._slotLabel = function(slotId) {
    if (!slotId || slotId === 'node_0') return 'PRIMARY';
    if (slotId === 'all') return 'ALL RADIOS';
    const slots = window._knownSlots || {};
    return (slots[slotId]?.label || slotId).toUpperCase();
};

// Returns Set of all local node IDs across all slots.
// In single-slot mode this is just {local_node_id}.
// In 'all' mode it includes every node marked isLocal across all slots.
window._getLocalNodeIds = function() {
    const ids = new Set();
    const primary = window.meshState.local_node_id;
    if (primary) ids.add(primary);
    Object.values(window.meshState.nodes || {}).forEach(n => {
        if ((n.isLocal || n.is_local) && n.node_id) ids.add(n.node_id);
    });
    return ids;
};

// Returns true if the given node_id is one of our own radios.
window._isFromSelf = function(nodeId) {
    if (!nodeId) return false;
    return window._getLocalNodeIds().has(nodeId);
};

// Builds an inline <select> populated from known connected slots for "send via" pickers
// containerId: id of the element to inject into; selectedSlotId: pre-selected value
window._buildSlotPicker = function(containerId, selectedSlotId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const slots = window._knownSlots || { node_0: { label: 'Primary', connection_type: 'TCP', is_ready: true } };
    const options = Object.entries(slots).map(([sid, s]) => {
        const ready = s.is_ready ? '' : ' ⚠';
        const sel = (selectedSlotId === sid || (!selectedSlotId && sid === 'node_0')) ? ' selected' : '';
        return `<option value="${sid}"${sel}>${window.escapeHtml((s.label || sid).toUpperCase())}${ready} [${(s.connection_type||'TCP').toUpperCase()}]</option>`;
    }).join('');
    el.innerHTML = `<select id="${containerId}-sel" class="inp mono" style="font-size:10px;padding:4px 8px;height:32px;">${options}</select>`;
};

// Returns current value from a slot picker built by _buildSlotPicker
window._slotPickerValue = function(containerId) {
    return document.getElementById(containerId + '-sel')?.value || window._activeSlotId || 'node_0';
};

// Polls /api/slots and caches into window._knownSlots; called on init and periodically
window._refreshKnownSlots = async function() {
    try {
        const r = await fetch('/api/slots', { cache: 'no-store' });
        if (r.ok) {
            window._knownSlots = await r.json();
            window._renderRadioSwitcher();
        }
    } catch (e) {}
};

// Heard-by badge HTML for node cards — shows which radio first heard a node/packet
window._heardByBadge = function(slotId) {
    if (!slotId || slotId === 'node_0') return '';
    const label = window._slotLabel(slotId);
    return `<span style="font-size:8px;font-family:var(--mono);background:rgba(176,96,255,0.15);color:var(--pur);border:1px solid rgba(176,96,255,0.3);padding:1px 5px;border-radius:3px;margin-left:4px;" title="Heard by ${label}">⬡ ${label}</span>`;
};

// ── Topbar radio switcher ──────────────────────────────────────────────────
window._renderRadioSwitcher = function() {
    const slots = window._knownSlots || {};
    const ids = Object.keys(slots);
    const switcher = document.getElementById('radio-switcher');
    if (!switcher) return;

    if (ids.length <= 1) { switcher.style.display = 'none'; return; }
    switcher.style.display = 'inline-flex';

    const active = window._activeSlotId || 'node_0';
    const isAll = active === 'all';
    const activeSlot = slots[active] || { label: 'PRIMARY', is_ready: true, connection_type: 'TCP' };
    const isReady = isAll ? true : activeSlot.is_ready;
    const dotColor = isAll ? 'var(--pur)' : (isReady ? 'var(--ok)' : 'var(--warn)');
    const dotShadow = isAll ? '0 0 5px var(--pur)' : (isReady ? '0 0 5px var(--ok)' : 'none');

    const lbl = document.getElementById('rsw-label');
    const dot = document.getElementById('rsw-dot');
    if (lbl) lbl.textContent = window._slotLabel(active);
    if (dot) { dot.style.background = dotColor; dot.style.boxShadow = dotShadow; }

    const dd = document.getElementById('radio-dropdown');
    if (!dd) return;

    // Build slot rows
    const slotRows = ids.map(sid => {
        const s = slots[sid];
        const isAct = sid === active;
        const ready = s.is_ready;
        const dc = ready ? 'var(--ok)' : 'var(--warn)';
        const ds = ready ? '0 0 5px var(--ok)' : 'none';
        const ct = (s.connection_type || 'TCP').toUpperCase();
        const ctColor = ct === 'TCP' ? 'var(--acc)' : ct === 'BLE' ? 'var(--pur)' : 'var(--warn)';
        return `<div onclick="window._switchRadio('${window.escapeHtml(sid)}')"
            style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;transition:background .1s;border-bottom:1px solid var(--bd);
            ${isAct ? 'background:rgba(0,200,245,0.08);' : ''}font-family:var(--mono);"
            onmouseover="this.style.background='rgba(255,255,255,0.04)'"
            onmouseout="this.style.background='${isAct ? 'rgba(0,200,245,0.08)' : 'transparent'}'">
            <span style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${dc};box-shadow:${ds};"></span>
            <div style="flex:1;min-width:0;">
                <div style="font-size:11px;font-weight:${isAct ? '800' : '600'};color:${isAct ? 'var(--acc)' : 'var(--txt)'};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                    ${window.escapeHtml(window._slotLabel(sid))}
                    ${isAct ? '<span style="color:var(--ok);font-size:9px;margin-left:4px;">●</span>' : ''}
                </div>
                <div style="font-size:9px;color:var(--txt3);">${window.escapeHtml(s.label || sid)}</div>
            </div>
            <span style="font-size:8px;font-weight:800;padding:2px 6px;border-radius:3px;background:rgba(0,0,0,.3);color:${ctColor};border:1px solid ${ctColor};opacity:.8;">${window.escapeHtml(ct)}</span>
        </div>`;
    }).join('');

    // ALL RADIOS option — only shown when >1 slot
    const allAct = active === 'all';
    const allRow = `<div onclick="window._switchRadio('all')"
        style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;transition:background .1s;border-bottom:1px solid var(--bd2);
        ${allAct ? 'background:rgba(176,96,255,0.1);' : ''}font-family:var(--mono);"
        onmouseover="this.style.background='rgba(255,255,255,0.04)'"
        onmouseout="this.style.background='${allAct ? 'rgba(176,96,255,0.1)' : 'transparent'}'">
        <span style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:var(--pur);box-shadow:0 0 5px var(--pur);"></span>
        <div style="flex:1;">
            <div style="font-size:11px;font-weight:${allAct ? '800' : '600'};color:${allAct ? 'var(--pur)' : 'var(--txt)'};">
                ALL RADIOS ${allAct ? '<span style="color:var(--ok);font-size:9px;margin-left:4px;">●</span>' : ''}
            </div>
            <div style="font-size:9px;color:var(--txt3);">Aggregate view — all ${ids.length} radios</div>
        </div>
        <span style="font-size:8px;font-weight:800;padding:2px 6px;border-radius:3px;background:rgba(0,0,0,.3);color:var(--pur);border:1px solid var(--pur);opacity:.8;">ALL</span>
    </div>`;

    dd.innerHTML = slotRows + allRow +
        `<div onclick="loadView('settings')" style="display:flex;align-items:center;gap:8px;padding:9px 14px;cursor:pointer;font-family:var(--mono);font-size:10px;color:var(--txt3);border-top:1px solid var(--bd2);"
        onmouseover="this.style.background='rgba(255,255,255,0.03)'" onmouseout="this.style.background='transparent'">
        <i class="fas fa-plus-circle" style="color:var(--acc);"></i> Manage Radios
    </div>`;
};

window._toggleRadioDropdown = function() {
    const dd = document.getElementById('radio-dropdown');
    const btn = document.getElementById('radio-switcher-btn');
    const chev = document.getElementById('rsw-chevron');
    if (!dd) return;
    const open = dd.style.display === 'block';
    dd.style.display = open ? 'none' : 'block';
    if (btn) btn.style.borderColor = open ? 'var(--bd2)' : 'var(--acc)';
    if (chev) chev.style.transform = open ? '' : 'rotate(180deg)';
};

window._switchRadio = function(slotId) {
    // Close dropdown
    const dd = document.getElementById('radio-dropdown');
    const btn = document.getElementById('radio-switcher-btn');
    const chev = document.getElementById('rsw-chevron');
    if (dd) dd.style.display = 'none';
    if (btn) btn.style.borderColor = 'var(--bd2)';
    if (chev) chev.style.transform = '';
    // Switch SSE slot
    if (slotId !== window._activeSlotId) {
        window._sseSetSlot(slotId);
        window._renderRadioSwitcher();
    }
};

// Close dropdown on outside click
document.addEventListener('click', function(e) {
    const sw = document.getElementById('radio-switcher');
    if (sw && !sw.contains(e.target)) {
        const dd = document.getElementById('radio-dropdown');
        const btn = document.getElementById('radio-switcher-btn');
        const chev = document.getElementById('rsw-chevron');
        if (dd) dd.style.display = 'none';
        if (btn) btn.style.borderColor = 'var(--bd2)';
        if (chev) chev.style.transform = '';
    }
});

// Active slot — which radio the dashboard is currently viewing
// Default is node_0 (primary). Settings view calls window._sseSetSlot(id)
// to switch. All API calls that are slot-scoped use _slotParam() helper.
// ---------------------------------------------------------------------------
window._activeSlotId = window._activeSlotId || 'node_0';

function _slotParam() {
    return window._activeSlotId && window._activeSlotId !== 'node_0'
        ? '?slot_id=' + encodeURIComponent(window._activeSlotId)
        : '';
}

function _slotAppend(url) {
    const sid = window._activeSlotId;
    if (!sid || sid === 'node_0') return url;
    const sep = url.includes('?') ? '&' : '?';
    return url + sep + 'slot_id=' + encodeURIComponent(sid);
}

// Called by settings.html slot switcher
window._sseSetSlot = function(slotId) {
    window._activeSlotId = slotId;
    // CONFIG.ssePath, statusEndpoint, localNodeFullEndpoint, statsEndpoint are now getters — auto-update

    // Reset all slot-scoped state
    window.meshState.nodes = {};
    window.meshState.local_node_id = null;
    window.meshState.packets = [];
    window.meshState.stats = {};
    window.meshState.connectionStatus = slotId === 'all' ? 'Connected' : 'Connecting';
    window._slotJustSwitched = true; // flag for nodes handler to re-init overview

    // Reset topbar
    try {
        const nidEl   = document.getElementById('rmd-u-nid');
        const nmetaEl = document.getElementById('rmd-u-nmeta');
        if (nidEl)   nidEl.innerText   = 'SWITCHING...';
        if (nmetaEl) nmetaEl.innerText = slotId === 'node_0' ? 'PRIMARY' : window._slotLabel(slotId);
        setRadioStatus('Connecting');
        setRadioRail('Connecting');
        updateStatusStripStats({ nodes_seen_session: 0, text_messages_session: 0, position_updates_session: 0, telemetry_reports_session: 0, channels_seen_session: 0 });
    } catch (e) {}

    // Paint topbar from slot status immediately — before SSE events arrive
    if (slotId === 'all') {
        // All-radios mode: fetch aggregated nodes directly, set topbar to show multi-radio state
        try {
            const nidEl   = document.getElementById('rmd-u-nid');
            const nmetaEl = document.getElementById('rmd-u-nmeta');
            if (nidEl)   nidEl.innerText   = 'ALL RADIOS';
            if (nmetaEl) nmetaEl.innerText = `${Object.keys(window._knownSlots||{}).length} CONNECTED`;
            setRadioStatus('Connected');
            setRadioRail('Connected');
        } catch (e) {}
        fetchWithTimeout('/api/nodes?slot_id=all').then(r => r?.ok ? r.json() : null).then(nodes => {
            if (nodes && typeof nodes === 'object') {
                window.meshState.nodes = nodes;
                triggerActiveViewNodeUpdate();
            }
        }).catch(() => {});
    } else {
        fetchWithTimeout('/api/slots/' + encodeURIComponent(slotId) + '/status').then(r => {
            if (!r || !r.ok) return null;
            return r.json();
        }).then(s => {
            if (!s) return;
            try {
                const csObj = { state: s.connection_state || s.connection_status, detail: s.connection_detail || '', transport: s.connection_transport || '', label: s.connection_status || 'Unknown' };
                setRadioStatus(csObj); setRadioRail(csObj);
            } catch (e) {}
            try {
                const localId = s.local_node_id || (s.local_node_info && s.local_node_info.node_id);
                // my_node_id: for MQTT, this is the MQTT_NODE_ID even in observer mode
                const myNodeId = s.my_node_id || localId;
                if (myNodeId) {
                    window.meshState.local_node_id = myNodeId;
                    // Mark the node as local in the nodes map
                    if (window.meshState.nodes[myNodeId]) {
                        window.meshState.nodes[myNodeId].isLocal = true;
                        window.meshState.nodes[myNodeId].is_local = true;
                    }
                }
                if (localId) {
                    updateLocalIdentity(s.local_node_info || { node_id: localId });
                } else {
                    // No local node identity yet (e.g. MQTT observer mode) — show slot label instead of AWAITING LINK
                    const nidEl   = document.getElementById('rmd-u-nid');
                    const nmetaEl = document.getElementById('rmd-u-nmeta');
                    if (nidEl && !myNodeId)   nidEl.innerText = window._slotLabel(slotId);
                    else if (nidEl && myNodeId) nidEl.innerText = myNodeId;
                    const transport = (s.connection_transport || '').toUpperCase() || 'RF';
                    if (nmetaEl) {
                        if (myNodeId) nmetaEl.innerText = transport.split(' ')[0] + ' NODE';
                        else nmetaEl.innerText = transport + ' OBSERVER';
                    }
                }
            } catch (e) {}
            try { if (s.stats) { window.meshState.stats = s.stats; updateStatusStripStats(s.stats); } } catch (e) {}
            try {
                if (s.nodes && typeof s.nodes === 'object') {
                    window.meshState.nodes = s.nodes;
                    triggerActiveViewNodeUpdate();
                }
            } catch (e) {}
        }).catch(() => {});
    }

    // In all-mode stats accumulate from multiple slots — reset before reconnect
    if (slotId === 'all') window.meshState.stats = {};

    _sse.reconnect(0);
    // This replicates what loadView() does after loading HTML — runs the view's
    // init() to reset its state, reload its data, and re-render from scratch.
    // sendSlotId is set by each init() call now (they all sync from _activeSlotId).
    const _reinitCurrentView = () => {
        const v = window.meshState.currentView;
        if (!v) return;
        const reinits = [
            ['overview',    () => typeof window.initOverviewCharts === 'function' && window.initOverviewCharts()],
            ['dmes',        () => typeof window.C2CommsApp !== 'undefined' && window.C2CommsApp.init()],
            ['channels',    () => typeof window.C2ChannelsApp !== 'undefined' && window.C2ChannelsApp.init()],
            ['map',         () => typeof window.initC2Map === 'function' && window.initC2Map()],
            ['traceroute',  () => typeof window.C2TracerouteApp !== 'undefined' && window.C2TracerouteApp.init()],
            ['analytics',   () => typeof window.C2AnalyticsApp !== 'undefined' && window.C2AnalyticsApp.init()],
            ['compare',     () => typeof window.C2CompareApp !== 'undefined' && window.C2CompareApp.init()],
            ['shark',       () => typeof window.C2SharkApp !== 'undefined' && window.C2SharkApp.init()],
            ['iot',         () => typeof window.C2IotApp !== 'undefined' && window.C2IotApp.init()],
            ['monitor',     () => typeof window.C2MonitorApp !== 'undefined' && window.C2MonitorApp.init()],
            ['tasks',       () => typeof window.C2TasksApp !== 'undefined' && window.C2TasksApp.init()],
            ['node_config', () => typeof window.NodeConfigApp !== 'undefined' && window.NodeConfigApp.init()],
        ];
        for (const [view, fn] of reinits) {
            if (v === view || v.startsWith(view)) {
                try { fn(); } catch (e) { console.warn('slot-switch reinit error [' + view + ']:', e); }
                return;
            }
        }
    };
    // Delay slightly so SSE reconnects and meshState.nodes begins populating first.
    // Map needs longer — its nodes come via the SSE 'nodes' batch event which arrives
    // after connection, and c2FetchAndDrawMap reads meshState.nodes. The 400ms nodes-batch
    // debounce + 350ms reinit delay means map would draw empty. Use 900ms so the nodes
    // batch has time to arrive and populate meshState.nodes before the draw runs.
    const _reinitDelay = window.meshState.currentView === 'map' ? 900 : 350;
    setTimeout(_reinitCurrentView, _reinitDelay);


};

const LORA_REGIONS = {
    0: 'UNSET', 1: 'US', 2: 'EU_433', 3: 'EU_868', 4: 'US_20', 5: 'US_20_2',
    6: 'AU_915', 7: 'AU_15_2', 8: 'CN_470', 9: 'KR_920', 10: 'IN_866',
    11: 'NZ_915', 12: 'RU_920', 13: 'SG_920', 14: 'TH_920', 15: 'LORA_24', 16: 'UK_433'
};

const SSE_BASE_BACKOFF_MS    = 1000;
const SSE_MAX_BACKOFF_MS     = 16000;
const FETCH_TIMEOUT_MS       = 10000;
const POLL_DEBOUNCE_MS       = 500;
const SSE_DEAD_THRESHOLD_MS  = 90000;  // if no event received in 90s, force reconnect
const SSE_HEALTH_INTERVAL_MS = 15000;  // watchdog tick

// ---------------------------------------------------------------------------
// Global state
// ---------------------------------------------------------------------------
window.meshState = {
    connectionStatus: 'Initializing',
    nodes: {},
    packets: [],
    stats: {},
    currentView: null,
    sessionStart: Date.now(),
    local_node_id: null,
    dmUnread: {},
    channelUnread: {}
};

// ---------------------------------------------------------------------------
// Global utilities
// ---------------------------------------------------------------------------
window.escapeHtml = function(text) {
    if (text == null) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
};

window.fmtTime = function(ts) {
    if (!ts) return 'N/A';
    try { return new Date(ts * 1000).toTimeString().split(' ')[0]; }
    catch (e) { return 'N/A'; }
};

window.fmtUptime = function(sec) {
    if (!sec || isNaN(sec)) return '';
    const d = Math.floor(sec / 86400),
          h = Math.floor((sec % 86400) / 3600),
          m = Math.floor((sec % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
};

window.getMeshVal = function(n, ...keys) {
    if (!n) return null;
    const nests = ['deviceMetrics', 'user', 'position', 'localConfig', 'lora', 'environmentMetrics'];
    for (const k of keys) {
        if (n[k] != null) return n[k];
        for (const nest of nests) {
            if (n[nest] && n[nest][k] != null) return n[nest][k];
        }
    }
    return null;
};

// ---------------------------------------------------------------------------
// Fetch with timeout + AbortController
// ---------------------------------------------------------------------------
async function fetchWithTimeout(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(id);
    }
}

// ---------------------------------------------------------------------------
// Web Serial — intercept /api/messages when browser owns the port
// ---------------------------------------------------------------------------
// When the Web Serial bridge is active the browser holds the COM port directly.
// We patch window.fetch so that POST /api/messages is redirected to the bridge
// sendText path instead of the server's radio, avoiding the "radio not connected"
// error that would otherwise fire when the server has no port of its own.
(function _patchFetchForWebSerial() {
    const _origFetch = window.fetch.bind(window);
    window.fetch = async function(url, options = {}, ...rest) {
        try {
            // Only intercept POST /api/messages when Web Serial bridge is active
            const urlStr = typeof url === 'string' ? url : (url?.url || '');
            if (
                typeof window.WebSerialBridge !== 'undefined' &&
                window.WebSerialBridge.enabled &&
                window.WebSerialBridge.connected &&
                (options.method || '').toUpperCase() === 'POST' &&
                (urlStr === '/api/messages' || urlStr.endsWith('/api/messages'))
            ) {
                try {
                    const body = typeof options.body === 'string'
                        ? JSON.parse(options.body)
                        : options.body || {};
                    const ok = await window.WebSerialBridge.sendText(
                        body.message || '',
                        body.destination || '^all',
                        body.channel    || 0
                    );
                    // Return a Response-compatible object so callers don't break
                    const fakePayload = JSON.stringify({
                        status:    ok ? 'sent' : 'error',
                        channel:   body.channel    || 0,
                        packet_id: null,
                        timestamp: Math.floor(Date.now() / 1000),
                        via:       'webserial',
                    });
                    return new Response(fakePayload, {
                        status:  ok ? 200 : 500,
                        headers: { 'Content-Type': 'application/json' }
                    });
                } catch (interceptErr) {
                    console.warn('Web Serial fetch intercept error:', interceptErr);
                    // Fall through to normal fetch
                }
            }
        } catch (e) {}
        return _origFetch(url, options, ...rest);
    };
})();

// ---------------------------------------------------------------------------
// Normalise a raw fromId/toId into !hex string
// ---------------------------------------------------------------------------
function _normNodeId(raw) {
    if (raw == null) return null;
    if (typeof raw === 'number') {
        if (raw === 4294967295 || raw === 0xffffffff) return '^all';
        return `!${raw.toString(16).padStart(8, '0')}`;
    }
    return String(raw);
}

// ---------------------------------------------------------------------------
// Nav badge helpers
// ---------------------------------------------------------------------------
window.updateDmNavBadge = function() {
    try {
        const total   = Object.values(window.meshState.dmUnread).reduce((a, b) => a + b, 0);
        const navItem = document.querySelector('.ni[data-view="dmes"]');
        if (!navItem) return;
        let badge = navItem.querySelector('.nav-unread-badge');
        if (total > 0) {
            if (!badge) {
                badge = document.createElement('span');
                badge.className  = 'nav-unread-badge nbadge';
                badge.style.cssText = 'margin-left:auto;font-size:9px;background:var(--acc);color:#000;font-weight:bold;';
                navItem.appendChild(badge);
            }
            badge.textContent = total;
        } else {
            if (badge) badge.remove();
        }
    } catch (e) {}
};

window.updateChannelNavBadge = function() {
    try {
        const total   = Object.values(window.meshState.channelUnread).reduce((a, b) => a + b, 0);
        const navItem = document.querySelector('.ni[data-view="channels"]');
        if (!navItem) return;
        let badge = navItem.querySelector('.nav-unread-badge');
        if (total > 0) {
            if (!badge) {
                badge = document.createElement('span');
                badge.className  = 'nav-unread-badge nbadge';
                badge.style.cssText = 'margin-left:auto;font-size:9px;background:var(--warn);color:#000;font-weight:bold;';
                navItem.appendChild(badge);
            }
            badge.textContent = total;
        } else {
            if (badge) badge.remove();
        }
    } catch (e) {}
};

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {


    // Fetch public_mode flag — hide logout in PUBLIC_MODE, clear stale state
    try {
        const _sr = await fetch('/api/status', { cache: 'no-store' });
        if (_sr.ok) {
            const _sd = await _sr.json();
            window._publicMode = !!_sd.public_mode;
            if (_sd.public_mode) {
                const logoutEl = document.getElementById('nav-logout');
                if (logoutEl) logoutEl.style.display = 'none';
                window.meshState.nodes = {};
                window.meshState.packets = [];
            }
        }
    } catch (e) {}

    try { if (window.C2Terminal) window.C2Terminal.init(); } catch (e) {}

    // CSRF token — read from meta tag or cookie for POST/PUT/DELETE requests
    try {
        const metaToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        const cookieToken = document.cookie.match(/(?:^|;)\s*csrf_token=([^;]*)/)?.[1];
        window._csrfToken = metaToken || cookieToken || '';
    } catch (e) { window._csrfToken = ''; }

    // Web Serial — initialise UI entry points if feature is enabled
    try { if (window.WebSerialBridge?.enabled) _wsInitTopbarButton(); } catch (e) {}

    // Start SSE and load overview immediately — do not block on secondary inits.
    // Plugin menu, historical packets and slot list are nice-to-have but must never
    // delay the core data stream. Run them in parallel, fire-and-forget.
    startSSE();
    loadView('overview');

    // Secondary inits — parallel, errors are swallowed individually
    Promise.allSettled([
        initPluginsMenu(),
        fetchHistoricalPackets(),
        window._refreshKnownSlots(),
    ]).catch(() => {});
    if (!window._knownSlotsInterval) window._knownSlotsInterval = setInterval(window._refreshKnownSlots, 30000);

    window._diagnosticsClockInterval = setInterval(() => { try { window.C2Diagnostics?.updateClocks(); } catch (e) {} }, 1000);
    window._diagnosticsPollInterval = setInterval(() => { try { window.C2Diagnostics?.pollSystem();   } catch (e) {} }, 30000);
    window._sseHealthInterval = setInterval(() => {
        try {
            const sinceLastEvent = Date.now() - _sse.lastEventAt;
            if (sinceLastEvent > SSE_DEAD_THRESHOLD_MS) {
                console.warn(`⚠️  SSE silent for ${Math.round(sinceLastEvent / 1000)}s — forcing reconnect.`);
                _sse.reconnect(0);
            }
        } catch (e) {}
    }, SSE_HEALTH_INTERVAL_MS);

    window.addEventListener('unload', () => {
        clearInterval(window._knownSlotsInterval);
        clearInterval(window._diagnosticsClockInterval);
        clearInterval(window._diagnosticsPollInterval);
        clearInterval(window._sseHealthInterval);
    });

    // Wake Lock — keep the CPU/network alive like a video player would
    let _wakeLock = null;
    async function _acquireWakeLock() {
        try {
            if ('wakeLock' in navigator && document.visibilityState === 'visible') {
                _wakeLock = await navigator.wakeLock.request('screen');
                _wakeLock.addEventListener('release', () => { _wakeLock = null; });

            }
        } catch (e) {}
    }
    _acquireWakeLock();

    document.addEventListener('visibilitychange', () => {
        try {
            if (document.visibilityState === 'visible') {
                // Re-acquire wake lock — browser releases it automatically when hidden
                _acquireWakeLock();

                const state = _sse.instance?.readyState;
                // CONNECTING (1) can be a zombie — treat it the same as CLOSED (2)
                if (state === undefined || state === EventSource.CLOSED || state === EventSource.CONNECTING) {

                    _sse.reconnect(0);
                } else if (state === EventSource.OPEN) {
                    // Stream appears open but may be silently dead (browser kept socket
                    // alive while tab was hidden but server closed the connection).
                    // Treat it like a watchdog tick — if last event was too long ago,
                    // force reconnect immediately rather than waiting up to 90s.
                    const sinceLastEvent = Date.now() - _sse.lastEventAt;
                    if (_sse.lastEventAt > 0 && sinceLastEvent > SSE_DEAD_THRESHOLD_MS) {
                        console.warn('👁️  Tab visible — SSE open but silent for ' + Math.round(sinceLastEvent/1000) + 's, forcing reconnect.');
                        _sse.reconnect(0);
                    }
                }

                // Force a fresh diagnostics poll when returning — clears stale "Initializing"
                // from HTML on reconnect even when SSE stays open and skips reconnect.
                window.C2Diagnostics._lastPollAt = 0;
                window.C2Diagnostics.pollSystem();

                // Always re-run the active view init after a visibility return
                // because Leaflet maps, Chart.js canvases, and stat strips can
                // be in a broken state after the browser throttled the tab
                try {
                    if (window.meshState.currentView === 'overview' &&
                        typeof window.initOverviewCharts === 'function') {
                        window.initOverviewCharts();
                    }
                } catch (e) {}
            }
        } catch (e) {}
    });

    // Network online event — reconnect immediately if SSE is down or zombie-CONNECTING
    window.addEventListener('online', () => {
        try {
    
            const state = _sse.instance?.readyState;
            if (state === undefined || state === EventSource.CLOSED || state === EventSource.CONNECTING) {
                _sse.reconnect(0);
            }
        } catch (e) {}
    });
});

async function fetchHistoricalPackets() {
    try {
        const res = await fetchWithTimeout(_slotAppend('/api/packets?limit=50'));
        if (res.ok) window.meshState.packets = await res.json();
    } catch (e) {}
}

// ---------------------------------------------------------------------------
// Routing & view management
// ---------------------------------------------------------------------------
let _viewAbortController = null;

async function loadView(viewName) {
    try {
        if (_viewAbortController) _viewAbortController.abort();
        _viewAbortController = new AbortController();

        // Destroy previous view if it has a cleanup function
        const prevView = window.meshState.currentView;
        if (prevView && typeof window['destroy' + prevView.charAt(0).toUpperCase() + prevView.slice(1) + 'View'] === 'function') {
            try { window['destroy' + prevView.charAt(0).toUpperCase() + prevView.slice(1) + 'View'](); } catch (e) {}
        }
        // Special case for connection view (camelCase)
        if (prevView === 'connection' && typeof window.destroyConnectionView === 'function') {
            try { window.destroyConnectionView(); } catch (e) {}
        }

        window.meshState.currentView = viewName;
        const contentArea = document.getElementById('content');

        document.querySelectorAll('.ni').forEach(el => el.classList.remove('active'));
        const targetNav = document.querySelector(`.ni[data-view="${viewName}"]`);
        if (targetNav) targetNav.classList.add('active');

        const response = await fetch(`/static/views/${viewName}.html`, {
            signal: _viewAbortController.signal
        });
        if (!response.ok) throw new Error(`View missing: ${viewName}.html`);

        const htmlData = await response.text();
        contentArea.innerHTML = `<div class="view-wrapper">${htmlData}</div>`;

        // innerHTML does not execute <script> tags — re-run them manually
        contentArea.querySelectorAll('script').forEach(oldScript => {
            const s = document.createElement('script');
            [...oldScript.attributes].forEach(a => s.setAttribute(a.name, a.value));
            s.textContent = oldScript.textContent;
            oldScript.parentNode.replaceChild(s, oldScript);
        });

        // Safe init dispatch — each wrapped so one failing never blocks others
        // Lazy-load the view script if not yet loaded, then run init
        const _runInit = () => {
            const inits = [
                ['overview',    () => typeof window.initOverviewCharts === 'function' && window.initOverviewCharts()],
                ['settings',    () => typeof window.c2InitSettings === 'function' && window.c2InitSettings()],
                ['map',         () => typeof window.initC2Map === 'function' && window.initC2Map()],
                ['dmes',        () => typeof window.C2CommsApp !== 'undefined' && window.C2CommsApp.init()],
                ['channels',    () => typeof window.C2ChannelsApp !== 'undefined' && window.C2ChannelsApp.init()],
                ['monitor',     () => typeof window.C2MonitorApp !== 'undefined' && window.C2MonitorApp.init()],
                ['analytics',   () => typeof window.C2AnalyticsApp !== 'undefined' && window.C2AnalyticsApp.init()],
                ['compare',     () => typeof window.C2CompareApp !== 'undefined' && window.C2CompareApp.init()],
                ['shark',       () => typeof window.C2SharkApp !== 'undefined' && window.C2SharkApp.init()],
                ['iot',         () => typeof window.C2IotApp !== 'undefined' && window.C2IotApp.init()],
                ['tasks',       () => typeof window.C2TasksApp !== 'undefined' && window.C2TasksApp.init()],
                ['autoreply',   () => typeof window.C2AutoReplyApp !== 'undefined' && window.C2AutoReplyApp.init()],
                ['plugins',     () => typeof window.C2PluginsApp !== 'undefined' && window.C2PluginsApp.init()],
                ['node_config', () => typeof window.NodeConfigApp !== 'undefined' && window.NodeConfigApp.init()],
                ['connection',  () => typeof window.initConnectionView === 'function' && window.initConnectionView()],
                ['traceroute',  () => typeof window.C2TracerouteApp !== 'undefined' && window.C2TracerouteApp.init()],
                ['account',     () => typeof window.AccountPageInit === 'function' && window.AccountPageInit()],
            ];
            for (const [view, fn] of inits) {
                if (viewName === view) {
                    try { fn(); } catch (initErr) {
                        console.error(`View init error [${view}]:`, initErr);
                    }
                }
            }
        };

        // overview is always pre-loaded; all others go through the lazy loader
        if (viewName === 'overview' || typeof window._lazyLoadView !== 'function') {
            _runInit();
        } else {
            window._lazyLoadView(viewName, _runInit);
        }

        if (window.innerWidth <= 860) document.getElementById('sidebar')?.classList.remove('open');
    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error(`loadView error [${viewName}]:`, err);
        try {
            document.getElementById('content').innerHTML =
                `<div class="vs"><div class="card"><div class="cb" style="color:var(--err)">Load Error: ${window.escapeHtml(viewName)}</div></div></div>`;
        } catch (e) {}
    }
}



function loadPluginFrame(pluginName, url, targetElement) {
    try {
        window.meshState.currentView = `plugin-${pluginName}`;
        const contentArea = document.getElementById('content');
        const frameUrl = url.startsWith('/plugin/')
            ? url.replace('/plugin/', '/static/plugins/')
            : url;

        document.querySelectorAll('.ni').forEach(el => el.classList.remove('active'));
        if (targetElement) targetElement.classList.add('active');

        contentArea.innerHTML = `
            <div class="view-wrapper" style="height:100%;display:flex;flex-direction:column;">
                <div class="card" style="flex:1;display:flex;flex-direction:column;overflow:hidden;border-color:var(--acc);">
                    <div class="ch">
                        <span class="ct"><span style="color:var(--acc);margin-right:8px;">❖</span>${window.escapeHtml(pluginName)}</span>
                        <a href="${frameUrl}" target="_blank" class="btn btn-sm" style="margin-left:auto;">Pop Out ↗</a>
                    </div>
                    <iframe src="${frameUrl}" style="flex:1;width:100%;border:none;background:var(--bg1);" sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox"></iframe>
                </div>
            </div>`;

        const iframe = contentArea.querySelector('iframe');
        iframe.onload = function() {
            try {
                const frameDoc = iframe.contentWindow.document;
                
                // ── Inject theme overrides into plugin iframe ──
                // The theme bridge injects into the parent doc, but CSS variables
                // don't cross iframe boundaries. Re-inject here so plugins stay themed.
                try {
                    const themeStyle = document.getElementById('md-theme-override');
                    if (themeStyle && themeStyle.textContent) {
                        let s = frameDoc.getElementById('md-theme-override');
                        if (s) { s.textContent = themeStyle.textContent; }
                        else { s = frameDoc.createElement('style'); s.id = 'md-theme-override'; s.setAttribute('data-theme-plugin','1'); s.textContent = themeStyle.textContent; frameDoc.head.appendChild(s); }
                    }
                } catch(te) { console.warn('[Theme] Could not inject into plugin iframe:', te); }

                frameDoc.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    const rect = iframe.getBoundingClientRect();
                    const parentX = e.clientX + rect.left;
                    const parentY = e.clientY + rect.top;
                    
                    if (window.showGlobalContextMenu) {
                        window.showGlobalContextMenu(parentX, parentY);
                    }
                });

                frameDoc.addEventListener('click', (e) => {
                    if (e.button !== 2 && window.hideGlobalContextMenu) {
                        window.hideGlobalContextMenu();
                    }
                });
            } catch (err) {
                console.warn("Could not attach context menu to plugin iframe:", err);
            }
        };

        if (window.innerWidth <= 860) document.getElementById('sidebar')?.classList.remove('open');
    } catch (e) {
        console.error('loadPluginFrame error:', e);
    }
}

async function initPluginsMenu() {
    try {
        const r = await fetchWithTimeout(CONFIG.pluginsMenuEndpoint);
        if (!r.ok) return;
        const data = await r.json();
        if (!data.nav_items?.length) return;

        const sidebar = document.getElementById('sidebar');
        if (!sidebar) return;
        const spacer = sidebar.querySelector('.spacer');

        const sectionTitle = document.createElement('div');
        sectionTitle.className = 'nsec';
        sectionTitle.innerText = 'Add-ons';
        sidebar.insertBefore(sectionTitle, spacer);

        data.nav_items.forEach(item => {
            try {
                const navItem = document.createElement('div');
                navItem.className = 'ni';
                navItem.innerHTML = `<span class="ico">❖</span>${window.escapeHtml(item.label)}`;
                navItem.onclick = (e) => loadPluginFrame(item.label, item.href, e.currentTarget);
                sidebar.insertBefore(navItem, spacer);
            } catch (e) {}
        });
    } catch (e) {}
}

// ---------------------------------------------------------------------------
// SSE — hardened state machine with watchdog
// ---------------------------------------------------------------------------
const _sse = {
    instance:        null,
    backoffMs:       SSE_BASE_BACKOFF_MS,
    reconnectTimer:  null,
    _unloadHandler:  null,
    lastEventAt:     0,            // set to Date.now() when start() fires; 0 = never connected
    _consecutiveFails: 0,

    start() {
        this._clearTimer();
        this._destroyInstance();
        this.lastEventAt = Date.now(); // reset so watchdog doesn't fire during reconnect delay

        let es;
        try {
            es = new EventSource(CONFIG.ssePath);
        } catch (e) {
            console.error('❌ Failed to create EventSource:', e);
            this._scheduleReconnect();
            return;
        }

        this.instance = es;

        if (this._unloadHandler) window.removeEventListener('beforeunload', this._unloadHandler);
        this._unloadHandler = () => { try { es.onerror = null; es.close(); } catch (e) {} };
        window.addEventListener('beforeunload', this._unloadHandler);

        es.onopen = () => {
            if (this.instance !== es) return;
            this.backoffMs         = SSE_BASE_BACKOFF_MS;
            this._consecutiveFails = 0;
            this.lastEventAt       = Date.now();
            setApiStatus(true);
            // For primary slot, use normal diagnostics poll.
            // For secondary slots, pollSystem would overwrite the topbar with node_0 data —
            // use the slot-scoped status endpoint instead.
            if (!window._activeSlotId || window._activeSlotId === 'node_0') {
                try { window.C2Diagnostics?.pollSystem(); } catch (e) {}
            } else {
                fetchWithTimeout(CONFIG.statusEndpoint).then(r => r.ok ? r.json() : null).then(s => {
                    if (!s) return;
                    try {
                        const csObj = { state: s.connection_state || s.connection_status, detail: s.connection_detail || '', transport: s.connection_transport || '', label: s.connection_status || 'Unknown' };
                        setRadioStatus(csObj); setRadioRail(csObj); window.meshState.connectionStatus = s.connection_status;
                    } catch (e) {}
                    try {
                        const localId = s.local_node_id || (s.local_node_info && s.local_node_info.node_id);
                        const myNodeId = s.my_node_id || localId;
                        if (myNodeId) {
                            window.meshState.local_node_id = myNodeId;
                            if (window.meshState.nodes[myNodeId]) {
                                window.meshState.nodes[myNodeId].isLocal = true;
                                window.meshState.nodes[myNodeId].is_local = true;
                            }
                        }
                        if (localId) {
                            updateLocalIdentity(s.local_node_info || { node_id: localId });
                        } else {
                            const nidEl   = document.getElementById('rmd-u-nid');
                            const nmetaEl = document.getElementById('rmd-u-nmeta');
                            const activeSlot = window._activeSlotId || 'node_0';
                            if (nidEl && !myNodeId)   nidEl.innerText = window._slotLabel(activeSlot);
                            else if (nidEl && myNodeId) nidEl.innerText = myNodeId;
                            const transport = (s.connection_transport || '').toUpperCase() || 'RF';
                            if (nmetaEl) {
                                if (myNodeId) nmetaEl.innerText = transport.split(' ')[0] + ' NODE';
                                else nmetaEl.innerText = transport + ' OBSERVER';
                            }
                        }
                    } catch (e) {}
                }).catch(() => {});
            }

            // Update the topbar radio switcher to reflect active slot
            try { window._renderRadioSwitcher(); } catch (e) {}
        };

        es.onerror = () => {
            if (this.instance !== es) return;
            this._consecutiveFails++;
            console.warn(`⚠️  SSE error #${this._consecutiveFails}. Reconnecting in ${this.backoffMs}ms...`);
            // Suppress CORE LOST flash during intentional slot switches
            if (!window._slotJustSwitched) setApiStatus(false);
            this.reconnect(this.backoffMs);
            this.backoffMs = Math.min(this.backoffMs * 2, SSE_MAX_BACKOFF_MS);
        };

        // Helper to stamp lastEventAt and guard against stale instances
        const stamp = (handler) => (e) => {
            if (this.instance !== es) return;
            this.lastEventAt = Date.now();
            try { handler(e); } catch (err) {
                console.error('SSE handler threw:', err);
            }
        };

        es.addEventListener('ping',              stamp(_sseHandlers.ping));
        es.addEventListener('connection_status', stamp(_sseHandlers.connectionStatus));
        es.addEventListener('stats',             stamp(_sseHandlers.stats));
        es.addEventListener('local_node_info',   stamp(_sseHandlers.localNodeInfo));
        es.addEventListener('nodes',             stamp(_sseHandlers.nodes));
        es.addEventListener('node_batch',        stamp(_sseHandlers.nodeBatch));
        es.addEventListener('node_update',       stamp(_sseHandlers.nodeUpdate));
        es.addEventListener('system_update',     stamp(_sseHandlers.systemUpdate));
        es.addEventListener('packet',            stamp(_sseHandlers.packet));
        es.addEventListener('activity',          stamp(_sseHandlers.activity));
        es.addEventListener('error',             stamp(_sseHandlers.serverError));
        es.addEventListener('sync_status',       stamp(_sseHandlers.syncStatus));
        es.addEventListener('traceroute_result', stamp(_sseHandlers.tracerouteResult));
        es.addEventListener('plugin_update', stamp(_sseHandlers.pluginUpdate));
    },

    _scheduleReconnect() {
        this._clearTimer();
        this.reconnectTimer = setTimeout(() => this.start(), this.backoffMs);
        this.backoffMs = Math.min(this.backoffMs * 2, SSE_MAX_BACKOFF_MS);
    },

    reconnect(delayMs) {
        this._clearTimer();
        this._destroyInstance();
        if (delayMs <= 0) {
            this.start();
        } else {
            this.reconnectTimer = setTimeout(() => this.start(), delayMs);
        }
    },

    _destroyInstance() {
        if (this.instance) {
            try {
                this.instance.onerror = null;
                this.instance.onopen  = null;
                this.instance.close();
            } catch (e) {}
            this.instance = null;
        }
    },

    _clearTimer() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
    }
};

function startSSE() { _sse.start(); }

// ---------------------------------------------------------------------------
// Web Serial — topbar button initialisation
// ---------------------------------------------------------------------------
function _wsInitTopbarButton() {
    try {
        if (!window.WebSerialBridge?.enabled) return;

        const btn  = document.getElementById('ws-connect-btn');
        const pill = document.getElementById('ws-stats-pill');

        // Wire click handlers only — visibility is controlled entirely by
        // webserial.js _wsShowButton() / _wsBootCheck() which run after auth.
        // Do NOT touch display here — that would race the boot check.
        if (btn) {
            btn.onclick = () => {
                try { window.WebSerialBridge.openModal(); } catch (e) {}
            };
            btn.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                try { window.WebSerialBridge.openModal(); } catch (e) {}
            });
        }

        if (pill) {
            pill.onclick = () => {
                try { window.WebSerialBridge.openModal(); } catch (e) {}
            };
        }

        // After the SSE stream opens and we know the config mode,
        // also re-run the boot check in case it fired before DOM was ready.
        // We give it 1s so the SSE open event has time to fire first.
        setTimeout(() => {
            try { window.WebSerialBridge._recheckConfig?.(); } catch (e) {}
        }, 1000);

    } catch (e) {
        console.warn('_wsInitTopbarButton error:', e);
    }
}

// Expose triggerActiveViewNodeUpdate to webserial.js injector
window._triggerActiveViewNodeUpdate = function(soft) { triggerActiveViewNodeUpdate(soft); };


const _sseHandlers = {

    ping(e) {
        // keepalive — absorbed silently, lastEventAt already stamped by wrapper
    },

    connectionStatus(e) {
        try {
            const raw = JSON.parse(e.data);
            // Handle both old (string) and new (structured JSON) formats
            let statusObj;
            if (typeof raw === 'string') {
                // Old format: raw is a status string like "Connected"
                statusObj = { state: raw, detail: '', transport: '', label: raw };
            } else if (typeof raw === 'object' && raw !== null) {
                // New format: { state, detail, transport, label }
                statusObj = raw;
            } else {
                return;
            }
            window.meshState.connectionStatus = statusObj.label || statusObj.state || 'Unknown';
            window.meshState.connectionState = statusObj;
            setRadioStatus(statusObj);
            setRadioRail(statusObj);
        } catch (err) {}
    },

    stats(e) {
        try {
            const stats = JSON.parse(e.data);
            if (window._activeSlotId === 'all') {
                // Accumulate numeric stats from all slots
                const merged = window.meshState.stats || {};
                for (const [k, v] of Object.entries(stats)) {
                    if (typeof v === 'number') merged[k] = (merged[k] || 0) + v;
                    else if (!(k in merged)) merged[k] = v;
                }
                window.meshState.stats = merged;
                updateStatusStripStats(merged);
            } else {
                window.meshState.stats = stats;
                updateStatusStripStats(stats);
            }
            if (window.meshState.currentView === 'overview' &&
                typeof window.c2UpdateOverviewStats === 'function') {
                window.c2UpdateOverviewStats();
            } else {
                if (typeof window._ovPushMetricsBackground === 'function') {
                    try { window._ovPushMetricsBackground(); } catch(e) {}
                }
            }
        } catch (err) {}
    },

    localNodeInfo(e) {
        try {
            const data = JSON.parse(e.data);
            if (data?.node_id) window.meshState.local_node_id = data.node_id;
            updateLocalIdentity(data);
        } catch (err) {}
    },

    nodes(e) {
        try {
            const allNodes = JSON.parse(e.data);
            if (!Array.isArray(allNodes)) return;

            if (window._activeSlotId === 'all') {
                // In all mode: merge — each batch contains nodes from ONE slot, stamp heard_by_slot
                allNodes.forEach(node => {
                    if (node?.node_id) {
                        // Preserve heard_by_slot if already set by backend, else keep existing
                        const existing = window.meshState.nodes[node.node_id];
                        window.meshState.nodes[node.node_id] = {
                            ...(existing || {}),
                            ...node,
                            heard_by_slot: node.heard_by_slot || (existing?.heard_by_slot) || 'node_0'
                        };
                    }
                });
            } else {
                // Single slot mode: full replace
                const fresh = {};
                allNodes.forEach(node => {
                    if (node?.node_id) fresh[node.node_id] = node;
                });
                window.meshState.nodes = fresh;
            }

            // After a slot switch, force a full overview re-init
            if (window._slotJustSwitched) {
                window._slotJustSwitched = false;
                if (window.meshState.currentView === 'overview' && typeof window.initOverviewCharts === 'function') {
                    try { window.initOverviewCharts(); } catch (e) {}
                } else {
                    triggerActiveViewNodeUpdate();
                }
            } else {
                triggerActiveViewNodeUpdate();
            }
        } catch (err) {}
    },

    nodeBatch(e) {
        // Incremental merge of a chunk of nodes (from chunked SSE initial burst)
        try {
            const batch = JSON.parse(e.data);
            if (!Array.isArray(batch)) return;
            batch.forEach(node => {
                if (node?.node_id) {
                    const existing = window.meshState.nodes[node.node_id];
                    window.meshState.nodes[node.node_id] = {
                        ...(existing || {}),
                        ...node,
                        heard_by_slot: node.heard_by_slot || existing?.heard_by_slot || (window._activeSlotId || 'node_0')
                    };
                }
            });
            // Re-render overview cards with new nodes
            triggerActiveViewNodeUpdate(true);
        } catch (err) {}
    },

    nodeUpdate(e) {
        try {
            const nodeData = JSON.parse(e.data);
            if (nodeData?.node_id) {
                const existing = window.meshState.nodes[nodeData.node_id];
                window.meshState.nodes[nodeData.node_id] = {
                    ...(existing || {}),
                    ...nodeData,
                    // Preserve heard_by_slot: backend stamps it for non-node_0 events
                    heard_by_slot: nodeData.heard_by_slot || existing?.heard_by_slot || 'node_0'
                };
                triggerActiveViewNodeUpdate(true);
            }
        } catch (err) {}
    },

    systemUpdate(e) {
        try {
            if (window.C2Terminal && typeof window.C2Terminal.handleSystemUpdate === 'function') {
                window.C2Terminal.handleSystemUpdate(e.data);
            }
        } catch (err) {}
    },

    packet(e) {
        // Outer try — if JSON.parse throws we bail immediately.
        // _patchSsePacket may have already parsed and stored e._parsed.
        let p;
        try {
            p = e._parsed || JSON.parse(e.data);
        } catch (err) {
            console.warn('SSE packet: bad JSON', err);
            return;
        }

        // ANTI-DUPLICATION & ENRICHMENT LOGIC
        // Stamp which slot this packet arrived from (backend sets slot_id/heard_by_slot on the packet)
        if (!p.heard_by_slot) p.heard_by_slot = p.slot_id || window._activeSlotId || 'node_0';
        // Check if Web Serial already injected this exact packet ID
        if (p.id) {
            const existingIdx = window.meshState.packets.findIndex(ex => ex.id === p.id);
            if (existingIdx !== -1) {
                // It exists! Merge the smart backend tags (RF/MQTT, SNR) into the basic USB packet
                Object.assign(window.meshState.packets[existingIdx], p);
                
                // Re-render the feed to smoothly update the tag colors without adding a new row
                if (window.meshState.currentView === 'overview' && typeof window.c2RenderFeed === 'function') {
                    _debouncedFeedRender();
                }
                return; // Halt here. Do not duplicate the row or unread counts.
            }
        }

        // Each step is individually guarded so one failing never blocks the rest
        try {
            window.meshState.packets.unshift(p);
            if (window.meshState.packets.length > 500) window.meshState.packets.pop();
        } catch (err) {}

        try { flashLED('rx'); } catch (err) {}
        try { logTraffic('RX', p.app_packet_type || 'RAW'); } catch (err) {}

        try {
            if (typeof window.C2SharkApp !== 'undefined') window.C2SharkApp.globalIngest(p);
        } catch (err) {}

        try {
            if (window.meshState.currentView === 'overview' &&
                typeof window.c2RenderFeed === 'function') _debouncedFeedRender();
        } catch (err) {}

        try {
            if (window.C2Terminal && typeof window.C2Terminal.handlePacket === 'function') {
                window.C2Terminal.handlePacket(p);
            }
        } catch (err) {}

        // ── Message unread tracking (DMs + channel broadcasts) ────────────
        if (p.app_packet_type === 'Message') {
            try {
                const fromId  = _normNodeId(p.fromId ?? p.from_id ?? null);
                const toId    = _normNodeId(p.toId   ?? p.to_id   ?? null);
                const localId = window.meshState.local_node_id ||
                    Object.values(window.meshState.nodes).find(n => n.isLocal)?.node_id ||
                    null;

                const isBroadcast = toId === '^all' ||
                                    toId === 'ffffffff' ||
                                    (p.toId ?? p.to_id) === 4294967295;
                const isFromSelf  = fromId ? window._isFromSelf(fromId) : false;

                // ── DM unread ─────────────────────────────────────────────
                if (!isBroadcast && !isFromSelf && fromId) {
                    const alreadyReading =
                        window.meshState.currentView === 'dmes' &&
                        typeof window.C2CommsApp !== 'undefined' &&
                        window.C2CommsApp.selectedNodeId === fromId;

                    if (!alreadyReading) {
                        window.meshState.dmUnread[fromId] =
                            (window.meshState.dmUnread[fromId] || 0) + 1;
                        window.updateDmNavBadge();

                        if (window.meshState.currentView === 'dmes' &&
                            typeof window.C2CommsApp !== 'undefined') {
                            window.C2CommsApp.unreadCounts[fromId] =
                                (window.C2CommsApp.unreadCounts[fromId] || 0) + 1;
                            try { window.C2CommsApp.renderContacts(); } catch (e) {}
                            try { window.C2CommsApp._updateInternalBadge(); } catch (e) {}
                        }
                    } else {
                        try {
                            if (typeof window.C2CommsApp !== 'undefined') {
                                window.C2CommsApp.appendMessageToLog(p);
                            }
                        } catch (e) {}
                    }
                }

                // ── Channel broadcast unread ──────────────────────────────
                if (isBroadcast && !isFromSelf) {
                    const chIdx = typeof p.channel === 'number' ? p.channel : 0;

                    const alreadyReading =
                        window.meshState.currentView === 'channels' &&
                        typeof window.C2ChannelsApp !== 'undefined' &&
                        window.C2ChannelsApp.selectedChannelIdx === chIdx;

                    if (!alreadyReading) {
                        window.meshState.channelUnread[chIdx] =
                            (window.meshState.channelUnread[chIdx] || 0) + 1;
                        window.updateChannelNavBadge();

                        if (window.meshState.currentView === 'channels' &&
                            typeof window.C2ChannelsApp !== 'undefined') {
                            window.C2ChannelsApp.unreadCounts[chIdx] =
                                (window.C2ChannelsApp.unreadCounts[chIdx] || 0) + 1;
                            try { window.C2ChannelsApp.renderSidebar(); } catch (e) {}
                            try { window.C2ChannelsApp._updateInternalBadge(); } catch (e) {}
                        }
                    } else {
                        try {
                            if (typeof window.C2ChannelsApp !== 'undefined') {
                                window.C2ChannelsApp.appendMessageToLog(p);
                            }
                        } catch (e) {}
                    }
                }
            } catch (err) {
                console.warn('SSE packet: unread tracking error', err);
            }
        }
    },

    activity(e) {
        try {
            const dir = JSON.parse(e.data);
            if (dir === 'TX') flashLED('tx');
            else if (dir === 'RX') flashLED('rx');
        } catch (err) {}
    },

    serverError(e) {
        try {
            const msg = JSON.parse(e.data);
            if (msg && window.C2Terminal && typeof window.C2Terminal.log === 'function') {
                window.C2Terminal.log('system',
                    `<span style="color:var(--err)">[ERR]</span> ${window.escapeHtml(String(msg))}`);
            }
        } catch (err) {}
    },

    syncStatus(e) {
        try {
            const d = JSON.parse(e.data);
            if (typeof window.onSyncStatus === 'function') window.onSyncStatus(d);
        } catch (err) {}
    },

    tracerouteResult(e) {
        try {
            const d = JSON.parse(e.data);
            if (typeof window.C2TracerouteApp !== 'undefined' &&
                typeof window.C2TracerouteApp.handleResult === 'function') {
                window.C2TracerouteApp.handleResult(d);
            }
        } catch (err) {}
    },

    pluginUpdate(e) {
        try {
            const d = JSON.parse(e.data);
            window.dispatchEvent(new CustomEvent('plugin_update_sse', { detail: d }));
        } catch (err) {}
    }
};

let _nodeUpdateDebounceTimer = null;
let _nodeUpdateSoftFlag = true;
let _feedRenderTimer = null;
function _debouncedFeedRender() {
    clearTimeout(_feedRenderTimer);
    _feedRenderTimer = setTimeout(() => {
        try { if (typeof window.c2RenderFeed === 'function') window.c2RenderFeed(); } catch(e) {}
    }, 800);
}
function triggerActiveViewNodeUpdate(softUpdate = false) {
    if (!softUpdate) _nodeUpdateSoftFlag = false;
    clearTimeout(_nodeUpdateDebounceTimer);
    // Hard updates (full nodes batch) render promptly.
    // Soft updates (single node_update events) use a longer debounce so rapid
    // per-packet node_update events coalesce — prevents constant card flickering.
    const delay = softUpdate ? 2000 : 400;
    _nodeUpdateDebounceTimer = setTimeout(() => {
        const soft = _nodeUpdateSoftFlag;
        _nodeUpdateSoftFlag = true;
        try {
            const v = window.meshState.currentView;
            if      (v === 'overview'  && typeof window.c2RenderNodes  === 'function')  window.c2RenderNodes(soft);
            else if (v === 'map'       && typeof window.c2FetchAndDrawMap === 'function') window.c2FetchAndDrawMap();
            else if (v === 'dmes'      && typeof window.C2CommsApp     !== 'undefined') window.C2CommsApp.renderContacts();
            else if (v === 'channels'  && typeof window.C2ChannelsApp  !== 'undefined') window.C2ChannelsApp.renderSidebar();
            else if (v === 'monitor'   && typeof window.C2MonitorApp   !== 'undefined') window.C2MonitorApp.renderSidebar();
            else if (v === 'analytics' && typeof window.C2AnalyticsApp !== 'undefined') {
                window.C2AnalyticsApp.renderSidebar();
                try { window.C2AnalyticsApp.refreshLiveStats(); } catch(e) {}
            }
            else if (v === 'iot'       && typeof window.C2IotApp       !== 'undefined') window.C2IotApp.renderSidebar();
        } catch (e) {}
    }, 400);
}

// ---------------------------------------------------------------------------
// Status helpers
// ---------------------------------------------------------------------------
function setApiStatus(online) {
    try {
        const dot = document.getElementById('dot-api');
        const val = document.getElementById('v-api');
        if (dot) dot.className = `rmd-u-dot ${online ? 'ok' : 'err'}`;
        if (val) { val.innerText = online ? 'LIVE' : 'LOST'; val.className = `rmd-u-val ${online ? 'ok' : 'err'}`; }
    } catch (e) {}
}

function setRadioStatus(status) {
    try {
        const dot = document.getElementById('dot-nod');
        const val = document.getElementById('v-nod');
        const lbl = document.getElementById('lbl-nod');
        if (!dot || !val) return;

        // Normalize: accept both string and structured object
        let state, label, detail, transport;
        if (typeof status === 'object' && status !== null) {
            state = (status.state || '').toLowerCase();
            label = status.label || status.state || 'Unknown';
            detail = status.detail || '';
            transport = (status.transport || '').toLowerCase();
        } else {
            // Legacy string - infer state from content
            const s = (status || '').toLowerCase();
            state = s;
            label = status || 'Unknown';
            detail = '';
            transport = '';
            // Infer transport from label text
            if (label.toLowerCase().includes('mqtt')) transport = 'mqtt';
            else if (label.toLowerCase().includes('web serial')) transport = 'webserial';
            else if (label.toLowerCase().includes('meshcore')) transport = 'meshcore';
        }

        // Map states to visual indicators
        let cls = 'warn';
        if (state === 'connected') cls = 'ok';
        else if (state === 'webserial') cls = 'ok';  // blue handled separately
        else if (state === 'mqtt') cls = 'ok';  // purple handled separately
        else if (state === 'connecting' || state === 'reconnecting') cls = 'warn';
        else if (state === 'degraded') cls = 'warn';
        else if (state === 'disconnected') cls = 'err';
        else if (state === 'idle') cls = 'warn';
        // Legacy string fallbacks
        else if (label.toLowerCase().includes('web serial')) cls = 'ok';
        else if (label.toLowerCase().includes('stream open')) cls = 'warn';
        else if (label.toLowerCase().includes('fail') || label.toLowerCase().includes('off')) cls = 'err';

        // Update transport label (RF → MQTT → BLE etc)
        if (lbl) {
            if (transport.includes('mqtt') || state === 'mqtt') lbl.innerText = 'MQTT';
            else if (transport.includes('webserial') || transport === 'webserial' || state === 'webserial') lbl.innerText = 'WS';
            else if (transport.includes('meshcore') || state === 'meshcore') lbl.innerText = 'MC';
            else if (transport.includes('serial')) lbl.innerText = 'SER';
            else if (transport.includes('tcp')) lbl.innerText = 'TCP';
            else lbl.innerText = 'RF';
        }

        dot.className = `rmd-u-dot lg ${cls}`;
        val.innerText = label.toUpperCase();
        val.className = `rmd-u-val ${cls}`;
        if (detail) val.title = detail;
    } catch (e) {}
}

// Updates the diagnostics modal rail — called by SSE events and slot switches
// so the rail stays in sync with the topbar at all times.
function setRadioRail(status) {
    try {
        const radDot = document.getElementById('mi-rdot-radio');
        const radVal = document.getElementById('mi-rval-radio');
        const radLbl = document.getElementById('mi-rlayer-radio');
        if (!radDot && !radVal) return;

        // Normalize: accept both string and structured object
        let state, label, detail, transport;
        if (typeof status === 'object' && status !== null) {
            state = (status.state || '').toLowerCase();
            label = status.label || status.state || 'Unknown';
            detail = status.detail || '';
            transport = (status.transport || '').toLowerCase();
        } else {
            const s = (status || '').toLowerCase();
            state = s;
            label = status || 'Unknown';
            detail = '';
            transport = '';
            if (label.toLowerCase().includes('mqtt')) transport = 'mqtt';
            else if (label.toLowerCase().includes('web serial')) transport = 'webserial';
            else if (label.toLowerCase().includes('meshcore')) transport = 'meshcore';
        }

        // Map states to visual indicators
        let rCls = 'warn';
        if (state === 'connected') rCls = 'ok';
        else if (state === 'webserial') rCls = 'ok';
        else if (state === 'mqtt') rCls = 'ok';
        else if (state === 'connecting' || state === 'reconnecting') rCls = 'warn';
        else if (state === 'degraded') rCls = 'warn';
        else if (state === 'disconnected') rCls = 'err';
        else if (state === 'idle') rCls = 'warn';
        // Legacy string fallbacks
        else if (label.toLowerCase().includes('web serial')) rCls = 'ok';
        else if (label.toLowerCase().includes('stream open')) rCls = 'warn';
        else if (label.toLowerCase().includes('fail') || label.toLowerCase().includes('off')) rCls = 'err';

        // Update diagnostics rail layer label (Radio Link → MQTT Link etc)
        if (radLbl) {
            if (transport.includes('mqtt') || state === 'mqtt') radLbl.innerText = 'MQTT Link';
            else if (transport.includes('webserial') || state === 'webserial') radLbl.innerText = 'Web Serial';
            else if (transport.includes('meshcore')) radLbl.innerText = 'MeshCore Link';
            else radLbl.innerText = 'Radio Link';
        }

        if (radDot) radDot.className = `rmd-rail-dot ${rCls}`;
        if (radVal) { radVal.innerText = label.toUpperCase(); radVal.className = `rmd-rail-val ${rCls}`; if (detail) radVal.title = detail; }
    } catch (e) {}
}

function updateLocalIdentity(n) {
    try {
        if (!n) return;
        const nidEl    = document.getElementById('rmd-u-nid');
        const nmetaEl  = document.getElementById('rmd-u-nmeta');
        const localId  = n.node_id || n.node_id_hex || window.meshState.local_node_id;
        const liveNode = window.meshState.nodes[localId] || {};
        const _invalid = ['Unknown', 'None', '', null, undefined];

        const getV = (...keys) => {
            for (const k of keys) {
                for (const src of [liveNode, n]) {
                    if (!_invalid.includes(src[k])) return src[k];
                    for (const nest of ['deviceMetrics', 'user', 'position', 'localConfig']) {
                        if (src[nest] && !_invalid.includes(src[nest][k])) return src[nest][k];
                    }
                }
            }
            return null;
        };

        if (nidEl)   nidEl.innerText = getV('node_id', 'node_id_hex') || 'AWAITING LINK';
        if (nmetaEl) {
            const name   = getV('short_name', 'shortName', 'long_name', 'longName') || 'Connecting...';
            const batRaw = getV('battery_level', 'batteryLevel');
            const bat    = batRaw != null ? ` · ${Math.min(100, batRaw)}%` : '';
            nmetaEl.innerText = `${name}${bat}`;
        }
    } catch (e) {}
}

function updateStatusStripStats(s) {
    try {
        const mapping = {
            'rs-nodes': 'nodes_seen_session',
            'rs-msg':   'text_messages_session',
            'rs-pos':   'position_updates_session',
            'rs-tel':   'telemetry_reports_session',
            'rs-ch':    'channels_seen_session'
        };
        Object.entries(mapping).forEach(([id, key]) => {
            const el = document.getElementById(id);
            if (el) el.innerText = s[key] || 0;
        });
    } catch (e) {}
}

function flashLED(dir) {
    try {
        const el = document.getElementById(`rmd-io-${dir}`);
        if (!el) return;
        el.classList.add(`${dir}-active`);
        setTimeout(() => { try { el.classList.remove(`${dir}-active`); } catch (e) {} }, 150);
    } catch (e) {}
}

function logTraffic(dir, type) {
    try {
        const body = document.getElementById('rmd-log-body');
        if (!body) return;
        if (body.innerText.includes('AWAITING')) body.innerHTML = '';
        const row = document.createElement('div');
        row.className = 'rmd-log-row';
        row.innerHTML = `<span class="rmd-log-ts">${new Date().toLocaleTimeString().split(' ')[0]}</span>`
                      + `<span class="rmd-log-dir ${dir}">${dir}</span>`
                      + `<span class="rmd-log-msg">${window.escapeHtml(type)} Traffic Detected</span>`;
        body.prepend(row);
        if (body.children.length > 50) body.lastElementChild?.remove();
    } catch (e) {}
}

// ---------------------------------------------------------------------------
// Diagnostics modal
// ---------------------------------------------------------------------------
window.C2Diagnostics = {

    _pollInFlight:     false,
    _pollDebounceTimer: null,

    openModal() {
        try {
            const m = document.getElementById('rmd-modal');
            if (m) m.style.display = 'flex';
            // Always force a fresh poll when opening — don't let a recent poll's
            // 5s debounce leave the rail showing stale "Initializing" from HTML.
            this._lastPollAt = 0;
            this.pollSystem();
            this.loadHistoryGraph();
        } catch (e) {}
    },

    closeModal() {
        try {
            const m = document.getElementById('rmd-modal');
            if (m) m.style.display = 'none';
        } catch (e) {}
    },

    openLogModal()  { try { document.getElementById('rmd-log-modal')?.classList.add('open');    } catch (e) {} },
    closeLogModal() { try { document.getElementById('rmd-log-modal')?.classList.remove('open'); } catch (e) {} },

    pollSystem() {
        if (this._pollInFlight) return;
        // Minimum 5s between polls regardless of how often this is called
        // (SSE reconnects can fire rapidly on flappy networks)
        const now = Date.now();
        if (this._lastPollAt && (now - this._lastPollAt) < 5000) return;
        clearTimeout(this._pollDebounceTimer);
        this._pollDebounceTimer = setTimeout(() => this._doPoll(), POLL_DEBOUNCE_MS);
    },

    async _doPoll() {
        this._pollInFlight = true;
        this._lastPollAt = Date.now();
        try {
            const [statusRes, nodeRes] = await Promise.all([
                fetchWithTimeout(CONFIG.statusEndpoint),
                fetchWithTimeout(CONFIG.localNodeFullEndpoint)
            ]);

            let nData = {};
            if (nodeRes.ok) {
                try { nData = { ...nData, ...(await nodeRes.json()) }; } catch (e) {}
            }

            if (statusRes.ok) {
                try {
                    const s = await statusRes.json();
                    this.updateDiagnosticsUI(s);
                    // Topbar connection status is driven by SSE connection_status events only.
                    // Do NOT overwrite setRadioStatus() here to avoid flicker during reconnections.
                    if (s.local_node_info) nData = { ...nData, ...s.local_node_info };
                } catch (e) {}
            }

            if (nData.node_id) window.meshState.local_node_id = nData.node_id;
            try { this.updateIdentityUI(nData); } catch (e) {}
            try { updateLocalIdentity(nData);   } catch (e) {}
        } catch (e) {
            setApiStatus(false);
        } finally {
            this._pollInFlight = false;
        }
    },

    updateDiagnosticsUI(s) {
        try {
            const apiDot = document.getElementById('mi-rdot-api');
            const apiVal = document.getElementById('mi-rval-api');
            if (apiDot) apiDot.className = `rmd-rail-dot ${s.api_status === 'online' ? 'ok' : 'err'}`;
            if (apiVal) {
                apiVal.innerText = s.api_status === 'online' ? 'STREAM OPEN' : 'OFFLINE';
                apiVal.className = `rmd-rail-val ${s.api_status === 'online' ? 'ok' : 'err'}`;
            }

            // Uptime only available from the poll — radio rail is driven by SSE events via setRadioRail()
            if (window.meshState.stats?.elapsed_time_session) {
                const upS = document.getElementById('mi-up-script');
                if (upS) upS.innerText = window.fmtUptime(window.meshState.stats.elapsed_time_session);
            }
        } catch (e) {}
    },

    updateIdentityUI(n) {
        try {
            const localId  = n.node_id || n.node_id_hex || window.meshState.local_node_id;
            const liveNode = window.meshState.nodes[localId] || {};
            const _invalid = ['Unknown', 'None', '', null, undefined];
            const nests    = ['deviceMetrics', 'user', 'position', 'localConfig', 'lora'];

            const getVal = (...keys) => {
                for (const k of keys) {
                    for (const src of [liveNode, n]) {
                        if (!_invalid.includes(src[k])) return src[k];
                        for (const nest of nests) {
                            if (src[nest] && !_invalid.includes(src[nest][k])) return src[nest][k];
                        }
                    }
                }
                return null;
            };

            const set = (id, val, cls, isTlm = false) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.innerHTML = (val != null && val !== '')
                    ? window.escapeHtml(String(val))
                    : (isTlm
                        ? '<span style="color:var(--txt3);font-size:9px;">AWAITING TLM</span>'
                        : '<span style="color:var(--txt3);font-size:9px;">N/A</span>');
                if (cls) el.className = `rmd-v ${cls}`;
            };

            set('mi-id',    getVal('node_id', 'node_id_hex'), 'grn');
            set('mi-short', getVal('short_name', 'shortName'));
            set('mi-long',  getVal('long_name', 'longName'));

            let hw = getVal('hwModel', 'hardware_model_string', 'hw_model_str', 'hardware_model', 'hw_model');
            if (hw && hw.includes('.')) hw = hw.split('.').pop();
            set('mi-hw', hw || 'Unknown');

            set('mi-fw',   getVal('firmware_version', 'firmwareVersion') || 'Unknown', 'blu');

            let role = getVal('role');
            if (role === '0' || role === 0) role = 'CLIENT';
            set('mi-role', role || 'CLIENT', 'pur');

            set('mi-reboot', getVal('reboot_count', 'rebootCount'));

            let loraReg = getVal('lora_region', 'loraRegion');
            if (!isNaN(loraReg) && LORA_REGIONS[loraReg]) loraReg = LORA_REGIONS[loraReg];
            set('mi-region',  getVal('region'), 'grn');
            set('mi-lregion', loraReg || 'Unknown');
            set('mi-hops',    getVal('lora_hop_limit', 'hop_limit', 'hopLimit'));

            const txPwr = getVal('lora_tx_power', 'tx_power', 'txPower');
            set('mi-txpwr', txPwr != null ? `${txPwr} dBm` : null);

            const txEn = getVal('lora_tx_enabled', 'tx_enabled', 'txEnabled');
            set('mi-txstate', txEn != null ? (txEn ? 'ENABLED' : 'DISABLED') : null, txEn ? 'grn' : 'red');

            set('mi-nodedb', getVal('nodedb_count', 'nodeDbCount'));

            const chUtil = getVal('channel_utilization', 'channelUtilization');
            set('mi-chutil', chUtil != null ? `${parseFloat(chUtil).toFixed(1)}%` : null, '', true);

            let bat = getVal('battery_level', 'batteryLevel');
            if (bat != null) bat = Math.min(100, bat);
            set('mi-bat', bat != null ? `${bat}%` : null, '', true);

            const volt = getVal('voltage');
            set('mi-volt', volt != null ? `${parseFloat(volt).toFixed(2)}V` : null, '', true);

            const air = getVal('air_util_tx', 'airUtilTx');
            set('mi-airutil', air != null ? `${parseFloat(air).toFixed(1)}%` : null, '', true);

            const bt = getVal('bluetooth_enabled', 'bluetoothEnabled');
            set('mi-bt', bt != null ? (bt ? 'ENABLED' : 'DISABLED') : null, bt ? 'grn' : 'grey');

            const lat = getVal('latitude'), lon = getVal('longitude');
            set('mi-pos-coords',
                (lat != null && lon != null)
                    ? `${parseFloat(lat).toFixed(5)}°, ${parseFloat(lon).toFixed(5)}°`
                    : null, '', true);

            const alt = getVal('altitude');
            set('mi-alt', alt != null ? `${alt}m` : null, '', true);

            const uptimeSecs = getVal('uptime_seconds', 'uptimeSeconds');
            const nodeUp = document.getElementById('mi-up-node');
            if (nodeUp) {
                nodeUp.innerText      = uptimeSecs ? window.fmtUptime(uptimeSecs) : 'AWAITING TLM';
                nodeUp.style.fontSize = uptimeSecs ? '20px' : '12px';
            }

            const bar = document.getElementById('mi-batbar');
            if (bar) {
                if (bat != null) {
                    bar.style.width      = Math.max(0, bat) + '%';
                    bar.style.background = bat > 60 ? 'var(--ok)' : bat > 20 ? 'var(--warn)' : 'var(--err)';
                } else {
                    bar.style.width = '0%';
                }
            }
        } catch (e) {}
    },

    updateClocks() {
        try {
            const sessionSec = Math.floor((Date.now() - window.meshState.sessionStart) / 1000);
            const el = document.getElementById('mi-up-session');
            if (el) el.innerText = window.fmtUptime(sessionSec);
        } catch (e) {}
    },

    async loadHistoryGraph() {
        try {
            await new Promise(r => setTimeout(r, 100));
            const canvas     = document.getElementById('rmd-full-canvas');
            const miniCanvas = document.getElementById('rmd-mini');

            const res = await fetchWithTimeout(CONFIG.historyEndpoint + '?limit=150');
            if (!res.ok) return;
            const data = await res.json();

            try { if (miniCanvas) this.drawMiniGraph(miniCanvas, data); } catch (e) {}

            if (!canvas) return;
            try {
                const rect = canvas.parentElement.getBoundingClientRect();
                if (rect.width > 0)  canvas.width  = rect.width;
                if (rect.height > 0) canvas.height = rect.height;
                this.drawGraph(canvas, data);
            } catch (e) {}
        } catch (e) {}
    },

    drawGraph(canvas, data) {
        try {
            const ctx = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            ctx.clearRect(0, 0, w, h);
            if (!data?.length) return;

            ctx.strokeStyle = 'rgba(255,255,255,0.05)';
            ctx.lineWidth = 1;
            [0.25, 0.5, 0.75].forEach(f => {
                ctx.beginPath(); ctx.moveTo(0, h - f * h); ctx.lineTo(w, h - f * h); ctx.stroke();
            });

            const plotData = [...data].reverse();
            const bw = w / plotData.length;

            plotData.forEach((d, i) => {
                let val = parseFloat(d.value);
                if (isNaN(val)) val = 0;
                const pct = Math.max(0, Math.min(1, val));
                const bh  = Math.max(2, pct * h);
                const x   = i * bw;
                if (pct > 0.75) {
                    ctx.fillStyle = 'rgba(0,232,122,0.1)';
                    ctx.fillRect(x, 0, Math.max(1, bw), h);
                    ctx.fillStyle = '#00e87a';
                } else if (pct > 0.3) {
                    ctx.fillStyle = '#ffa826';
                } else {
                    ctx.fillStyle = '#ff3050';
                }
                ctx.fillRect(x, h - bh, Math.max(1, bw - 0.5), bh);
            });
        } catch (e) {}
    },

    drawMiniGraph(canvas, data) {
        try {
            const ctx = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            ctx.clearRect(0, 0, w, h);
            if (!data?.length) return;

            const plotData = [...data].reverse();
            const bw = w / plotData.length;

            plotData.forEach((d, i) => {
                let val = parseFloat(d.value);
                if (isNaN(val)) val = 0;
                const pct = Math.max(0, Math.min(1, val));
                const bh  = Math.max(1, pct * h);
                const x   = i * bw;
                ctx.fillStyle = pct > 0.75 ? '#00e87a' : pct > 0.3 ? '#ffa826' : '#ff3050';
                ctx.fillRect(x, h - bh, Math.max(1, bw - 0.5), bh);
            });
        } catch (e) {}
    }
};

// ---------------------------------------------------------------------------
// C2 Terminal
// ---------------------------------------------------------------------------
window.C2Terminal = {
    history:        [],
    historyIdx:     -1,
    isOpen:         false,
    updatePending:  false,
    versionData:    null,
    isDragging:     false,
    _saveTimer:     null,
    _lastSystemMsg: '',
    _systemMsgCount: 0,

    init() {
        try { this.history = JSON.parse(localStorage.getItem('c2-term-history') || '[]'); }
        catch (e) { this.history = []; }
        try { this.setupListeners(); } catch (e) {}
        try { this.loadPersistence(); } catch (e) {}
        try { this.refreshVitals(); } catch (e) {}
        if (this._vitalsInterval) clearInterval(this._vitalsInterval);
        this._vitalsInterval = setInterval(() => { try { this.refreshVitals(); } catch (e) {} }, 20000);
        setTimeout(() => { try { this.checkVersion(true); } catch (e) {} }, 3000);
        if (this._versionInterval) clearInterval(this._versionInterval);
        this._versionInterval = setInterval(() => { try { this.checkVersion(false); } catch (e) {} }, 43200000);
    },

    setupListeners() {
        const consoleIn = document.getElementById('c2-console-in');
        if (consoleIn) {
            consoleIn.onkeydown = (e) => {
                try {
                    if (e.key === 'Enter')     this.executeCommand();
                    if (e.key === 'ArrowUp')   this.browseHistory(-1);
                    if (e.key === 'ArrowDown') this.browseHistory(1);
                } catch (err) {}
            };
        }

        const updateIn = document.getElementById('c2-update-in');
        if (updateIn) {
            updateIn.onkeydown = (e) => {
                try {
                    if (e.key !== 'Enter') return;
                    const val = e.target.value.trim().toUpperCase();
                    if (val === 'Y' || val === 'YES') {
                        this.executeOTAUpdate();
                    } else if (val === 'N' || val === 'NO') {
                        this.logUpdate('<span style="color:var(--err)">[ABORT] Update cancelled by operator.</span>');
                        const row = document.getElementById('c2-update-input-row');
                        if (row) row.style.display = 'none';
                        this.updatePending = false;
                    } else {
                        this.logUpdate('<span style="color:var(--warn)">[WARN] Invalid input. Type Y to proceed or N to cancel.</span>');
                    }
                    e.target.value = '';
                } catch (err) {}
            };
        }

        const badge = document.getElementById('c2-term-ver-badge');
        if (badge) badge.onclick = (e) => { try { e.stopPropagation(); this.handleVersionClick(); } catch (err) {} };

        const handle = document.getElementById('c2-term-handle');
        const drawer = document.getElementById('c2-terminal-drawer');
        if (handle && drawer) {
            handle.onmousedown = () => {
                this.isDragging = true;
                document.onmousemove = (me) => {
                    try {
                        if (!this.isDragging) return;
                        let h = window.innerHeight - me.clientY - 32;
                        h = Math.max(180, Math.min(h, window.innerHeight * 0.9));
                        drawer.style.height = h + 'px';
                    } catch (e) {}
                };
                document.onmouseup = () => {
                    try {
                        this.isDragging = false;
                        localStorage.setItem('c2-term-height', drawer.style.height);
                        document.onmousemove = null;
                        document.onmouseup  = null;
                    } catch (e) {}
                };
            };
        }
    },

    handleSystemUpdate(rawData) {
        try {
            const data = JSON.parse(rawData);
            const msg  = (data.message || String(rawData) || '').trim();
            if (!msg) return;

            if (msg === this._lastSystemMsg) {
                this._systemMsgCount++;
                const lastRow = document.getElementById('c2-tab-system')?.lastElementChild;
                if (lastRow) {
                    const counter = lastRow.querySelector('.c2-dup-count');
                    if (counter) counter.textContent = ` (×${this._systemMsgCount})`;
                    else lastRow.innerHTML += `<span class="c2-dup-count" style="color:var(--txt3);font-size:0.8em;"> (×${this._systemMsgCount})</span>`;
                }
            } else {
                this._lastSystemMsg  = msg;
                this._systemMsgCount = 1;
                this.log('system', `<span style="color:var(--acc)">[SYS]</span> ${window.escapeHtml(msg)}`);
            }

            if (msg.toLowerCase().includes('update available')) { try { this.checkVersion(); } catch (e) {} }
            this.triggerSave();
        } catch (err) {}
    },

    handlePacket(pkt) {
        try {
            const type = pkt.app_packet_type || 'Data';
            const from = pkt.fromId || 'Unknown';
            this.log('stream', `[${window.escapeHtml(type)}] From <span style="color:var(--acc)">${window.escapeHtml(from)}</span>`);

            if (type === 'Message') {
                this.log('chat', `<b style="color:var(--acc)">${window.escapeHtml(from)}:</b> ${window.escapeHtml(pkt.decoded?.payload || '')}`);
            }
            if (type === 'Telemetry') {
                const met = pkt.decoded?.telemetry?.deviceMetrics || {};
                if (met.batteryLevel !== undefined) {
                    this.log('sensors', `<b>${window.escapeHtml(from)}:</b> Bat ${met.batteryLevel}% | ${met.voltage?.toFixed(2)}V`);
                }
            }
            this.triggerSave();
        } catch (err) {}
    },

    toggle() {
        try {
            this.isOpen = !this.isOpen;
            const drawer  = document.getElementById('c2-terminal-drawer');
            const chevron = document.getElementById('c2-term-chevron');
            if (!drawer || !chevron) return;

            if (this.isOpen) {
                drawer.classList.add('open');
                chevron.style.transform = 'rotate(180deg)';
                setTimeout(() => {
                    try {
                        const activeTab = document.querySelector('.c2-term-tab.active')?.dataset.tab;
                        if (activeTab === 'c2-tab-console') document.getElementById('c2-console-in')?.focus();
                    } catch (e) {}
                }, 300);
            } else {
                drawer.classList.remove('open');
                chevron.style.transform = 'rotate(0deg)';
            }
            localStorage.setItem('c2-term-is-open', this.isOpen);
        } catch (e) {}
    },

    switchTab(tabId) {
        try {
            document.querySelectorAll('.c2-term-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.c2-term-content').forEach(c => c.classList.remove('active'));

            const tabBtn = document.querySelector(`[data-tab="${tabId}"]`);
            if (tabBtn) { tabBtn.classList.add('active'); tabBtn.classList.remove('unread'); }

            const target = document.getElementById(tabId);
            if (target) { target.classList.add('active'); target.scrollTop = target.scrollHeight; }

            if (tabId === 'c2-tab-console') document.getElementById('c2-console-in')?.focus();
            if (tabId === 'c2-tab-update')  document.getElementById('c2-update-in')?.focus();

            localStorage.setItem('c2-term-active-tab', tabId);
            this.triggerSave();
        } catch (e) {}
    },

    async refreshVitals() {
        try {
            const r = await fetchWithTimeout(CONFIG.localNodeFullEndpoint);
            if (r.ok) this.renderVitals(await r.json());
        } catch (e) {}
    },

    renderVitals(data) {
        try {
            const container = document.getElementById('c2-vitals-container');
            if (!container) return;

            const localId  = data.node_id || data.node_id_hex || window.meshState.local_node_id;
            const liveNode = window.meshState.nodes[localId] || {};
            const _invalid = ['Unknown', 'None', '', null, undefined];
            const nests    = ['deviceMetrics', 'user', 'position', 'localConfig', 'lora'];

            const getVal = (...keys) => {
                for (const k of keys) {
                    for (const src of [liveNode, data]) {
                        if (!_invalid.includes(src[k])) return src[k];
                        for (const nest of nests) {
                            if (src[nest] && !_invalid.includes(src[nest][k])) return src[nest][k];
                        }
                    }
                }
                return null;
            };

            const name = getVal('long_name', 'longName', 'short_name', 'shortName') || 'Unnamed';
            const nid  = getVal('node_id_hex', 'node_id') || '00000000';
            let role   = getVal('role');
            if (role === '0' || role === 0) role = 'CLIENT';
            role = role || 'CLIENT';

            let bat = getVal('battery_level', 'batteryLevel');
            if (bat != null) bat = Math.min(100, bat);
            const batStr = bat != null ? `${bat}%` : 'AWAITING TLM';

            const uptimeSecs = getVal('uptime_seconds', 'uptimeSeconds');
            const up = uptimeSecs ? window.fmtUptime(uptimeSecs) : 'AWAITING TLM';

            let hw = getVal('hwModel', 'hardware_model_string', 'hw_model_str', 'hardware_model', 'hw_model') || 'Unknown';
            if (hw.includes('.')) hw = hw.split('.').pop();

            const fw     = getVal('firmware_version', 'firmwareVersion') || 'Unknown';
            const air    = getVal('air_util_tx', 'airUtilTx');
            const airStr = air != null ? `${parseFloat(air).toFixed(1)}%` : 'AWAITING TLM';
            const pwr    = getVal('lora_tx_power', 'tx_power', 'txPower');
            const pwrStr = pwr != null ? `${pwr} dBm` : 'N/A';

            requestAnimationFrame(() => {
                try {
                    container.innerHTML = `
                        <div class="c2-vital-card">
                            <div class="c2-vital-title"><i class="fas fa-id-card"></i> Node Identity</div>
                            <div class="c2-vital-row"><span>Name</span><span class="c2-vital-val" style="color:var(--acc)">${window.escapeHtml(name)}</span></div>
                            <div class="c2-vital-row"><span>ID</span><span class="c2-vital-val">${window.escapeHtml(String(nid).replace('!',''))}</span></div>
                            <div class="c2-vital-row"><span>Role</span><span class="c2-vital-val">${window.escapeHtml(role)}</span></div>
                        </div>
                        <div class="c2-vital-card">
                            <div class="c2-vital-title"><i class="fas fa-bolt"></i> System Vitals</div>
                            <div class="c2-vital-row"><span>Battery</span><span class="c2-vital-val">${window.escapeHtml(batStr)}</span></div>
                            <div class="c2-vital-row"><span>Uptime</span><span class="c2-vital-val">${window.escapeHtml(up)}</span></div>
                        </div>
                        <div class="c2-vital-card">
                            <div class="c2-vital-title"><i class="fas fa-microchip"></i> Hardware</div>
                            <div class="c2-vital-row"><span>Model</span><span class="c2-vital-val">${window.escapeHtml(hw)}</span></div>
                            <div class="c2-vital-row"><span>Firmware</span><span class="c2-vital-val">${window.escapeHtml(fw)}</span></div>
                        </div>
                        <div class="c2-vital-card">
                            <div class="c2-vital-title"><i class="fas fa-broadcast-tower"></i> LoRa Status</div>
                            <div class="c2-vital-row"><span>Air Util</span><span class="c2-vital-val">${window.escapeHtml(airStr)}</span></div>
                            <div class="c2-vital-row"><span>TX Power</span><span class="c2-vital-val">${window.escapeHtml(pwrStr)}</span></div>
                        </div>`;
                } catch (e) {}
            });
        } catch (e) {}
    },

    async executeCommand() {
        try {
            const input  = document.getElementById('c2-console-in');
            const output = document.getElementById('c2-console-out');
            const cmd    = input.value.trim();
            if (!cmd) return;

            this.history.unshift(cmd);
            if (this.history.length > 50) this.history.pop();
            try { localStorage.setItem('c2-term-history', JSON.stringify(this.history)); } catch (e) {}
            this.historyIdx = -1;

            output.innerHTML += `<div class="c2-cmd-echo">> meshtastic ${window.escapeHtml(cmd)}</div>`;
            input.value = '';
            output.scrollTop = output.scrollHeight;

            const res  = await fetchWithTimeout('/api/console', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window._csrfToken || '' },
                body:    JSON.stringify({ command: cmd })
            });
            const text = await res.text();
            output.innerHTML += `<div class="c2-cmd-resp">${window.escapeHtml(text)}</div>`;
            this.triggerSave();
        } catch (err) {
            try {
                document.getElementById('c2-console-out').innerHTML +=
                    `<div class="c2-cmd-resp" style="color:var(--err);border-color:var(--err);">Connection Error: ${window.escapeHtml(err.message)}</div>`;
            } catch (e) {}
        }
        try { document.getElementById('c2-console-out').scrollTop = 99999; } catch (e) {}
    },

    browseHistory(dir) {
        try {
            const input = document.getElementById('c2-console-in');
            this.historyIdx += dir;
            if (this.historyIdx < -1) this.historyIdx = -1;
            if (this.historyIdx >= this.history.length) this.historyIdx = this.history.length - 1;
            input.value = this.historyIdx === -1 ? '' : this.history[this.historyIdx];
        } catch (e) {}
    },

    log(target, html) {
        html = window.escapeHtml(html);
        try {
            const container = document.getElementById(`c2-tab-${target}`);
            if (!container) return;
            const div = document.createElement('div');
            div.className = 'c2-log-row';
            const ts = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
            div.innerHTML = `<span class="c2-log-ts">${ts}</span> ${html}`;
            container.appendChild(div);
            const tabBtn = document.querySelector(`[data-tab="c2-tab-${target}"]`);
            if (tabBtn && !tabBtn.classList.contains('active')) tabBtn.classList.add('unread');
            while (container.childElementCount > 100) container.removeChild(container.firstChild);
            // Only scroll if the tab is visible — avoids forced layout on hidden tabs
            if (container.classList.contains('active') && container.offsetParent !== null) {
                container.scrollTop = container.scrollHeight;
            }
        } catch (e) {}
    },

    triggerSave() {
        if (this._saveTimer) return;
        this._saveTimer = setTimeout(() => {
            this._saveTimer = null;
            try {
                // Trim each tab to last 50 rows before serialising to keep localStorage write small
                const trim = (id) => {
                    const el = document.getElementById(id);
                    if (!el) return '';
                    const rows = el.querySelectorAll('.c2-log-row, .c2-cmd-echo, .c2-cmd-resp');
                    if (rows.length > 50) {
                        Array.from(rows).slice(0, rows.length - 50).forEach(r => r.remove());
                    }
                    return el.innerHTML || '';
                };
                const state = {
                    stream:  trim('c2-tab-stream'),
                    chat:    trim('c2-tab-chat'),
                    sensors: trim('c2-tab-sensors'),
                    system:  trim('c2-tab-system'),
                    console: trim('c2-console-out') || 'CLI Bridge Ready.\n'
                };
                const serialised = JSON.stringify(state);
                // Guard: skip write if payload exceeds 512 KB to avoid quota errors
                if (serialised.length < 524288) {
                    localStorage.setItem('c2-term-state', serialised);
                }
            } catch (e) {}
        }, 2000); // extended to 2 s to further reduce write frequency under heavy traffic
    },

    loadPersistence() {
        try {
            const savedState = localStorage.getItem('c2-term-state');
            if (savedState) {
                const state = JSON.parse(savedState);
                const map = {
                    'c2-tab-stream':  state.stream,
                    'c2-tab-chat':    state.chat,
                    'c2-tab-sensors': state.sensors,
                    'c2-tab-system':  state.system,
                    'c2-console-out': state.console || 'CLI Bridge Ready.\n'
                };
                Object.entries(map).forEach(([id, html]) => {
                    try { const el = document.getElementById(id); if (el && html) el.innerHTML = html; } catch (e) {}
                });
            }
            const savedHeight = localStorage.getItem('c2-term-height');
            if (savedHeight) {
                const d = document.getElementById('c2-terminal-drawer');
                if (d) d.style.height = savedHeight;
            }
            const isOpen = localStorage.getItem('c2-term-is-open') === 'true';
            if (isOpen) this.toggle();
            const activeTab = localStorage.getItem('c2-term-active-tab');
            if (activeTab) this.switchTab(activeTab);
        } catch (e) {}
    },

    clearPersistence() {
        try {
            if (confirm('Clear all log history and persistence?')) {
                localStorage.removeItem('c2-term-state');
                localStorage.removeItem('c2-term-history');
                location.reload();
            }
        } catch (e) {}
    },

    async checkVersion(initialLoad = false) {
        try {
            const icon  = document.getElementById('c2-term-ver-icon');
            const text  = document.getElementById('c2-term-ver-text');
            const badge = document.getElementById('c2-term-ver-badge');
            if (icon) icon.className = 'fas fa-circle-notch fa-spin';

            const r = await fetchWithTimeout('/api/system/version-status');
            if (!r.ok) throw new Error('API Error');
            const d = await r.json();
            this.versionData = d;

            if (d.status === 'update_needed') {
                if (badge) badge.className = 'term-badge update';
                if (icon)  icon.className  = 'fas fa-cloud-download-alt';
                if (text)  text.innerText  = `v${d.local} ➔ v${d.remote}`;
                this.stageUpdate(d);
            } else if (d.status === 'beta') {
                if (badge) badge.className = 'term-badge beta';
                if (icon)  icon.className  = 'fas fa-flask';
                if (text)  text.innerText  = `v${d.local} (BETA)`;
            } else {
                if (badge) badge.className = 'term-badge current';
                if (icon)  icon.className  = 'fas fa-check';
                if (text)  text.innerText  = `v${d.local}`;
            }
        } catch (e) {
            try {
                const icon  = document.getElementById('c2-term-ver-icon');
                const text  = document.getElementById('c2-term-ver-text');
                const badge = document.getElementById('c2-term-ver-badge');
                if (badge) badge.className = 'term-badge';
                if (icon)  icon.className  = 'fas fa-unlink';
                if (text)  text.innerText  = 'OFFLINE';
            } catch (e2) {}
        }
    },

    handleVersionClick() {
        try {
            if (!this.versionData) return;
            const icon = document.getElementById('c2-term-ver-icon');
            if (icon) icon.className = 'fas fa-circle-notch fa-spin';
            if (this.versionData.status === 'update_needed') {
                this.updatePending = false;
                this.stageUpdate(this.versionData);
                return;
            }
            this.checkVersion().then(() => {
                try { this.log('system', `Manual Version check complete. Status: ${this.versionData?.status || 'Unknown'}`); } catch (e) {}
            });
        } catch (e) {}
    },

    async triggerForceUpdate() {
        try {
            if (!confirm('FORCE UPDATE: This will overwrite system files even if you are up to date. Continue?')) return;
            await this.checkVersion();
            const forceData = { ...(this.versionData || {}), status: 'update_needed', isForced: true };
            this.updatePending = false;
            this.stageUpdate(forceData);
        } catch (e) {}
    },

    stageUpdate(data) {
        try {
            const updateTabBtn = document.getElementById('c2-tab-btn-update');
            if (updateTabBtn) { updateTabBtn.style.display = 'block'; updateTabBtn.classList.add('update'); }

            if (!this.updatePending) {
                this.updatePending = true;
                if (!this.isOpen) this.toggle();
                this.switchTab('c2-tab-update');

                const log = document.getElementById('c2-update-log');
                if (log) log.innerHTML = '';

                if (data.isForced) {
                    this.logUpdate(`🛠️ <b style="color:var(--err)">FORCE INSTALL TRIGGERED</b>`);
                    this.logUpdate(`Current: v${data.local || '?'} | Target: v${data.remote || data.local || '?'}`);
                } else {
                    this.logUpdate(`🚀 <b>OTA UPDATE AVAILABLE:</b> v${data.local} ➔ v${data.remote}`);
                }
                this.logUpdate('------------------------------------------------');
                this.logUpdate('⚠️  WARNING: This process will replace core system files.');
                this.logUpdate(' - <b>Databases &amp; Configs:</b> PRESERVED');
                this.logUpdate(' - <b>System Service:</b> WILL AUTO-RESTART');
                this.logUpdate('------------------------------------------------');
                this.logUpdate('Do you wish to proceed with the download and installation?');

                const inputRow = document.getElementById('c2-update-input-row');
                if (inputRow) inputRow.style.display = 'flex';
            }
        } catch (e) {}
    },

    logUpdate(htmlStr) {
        try {
            const container = document.getElementById('c2-update-log');
            if (!container) return;
            const ts = new Date().toLocaleTimeString([], { hour12: false });
            container.innerHTML += `<div><span class="c2-log-ts">${ts}</span> ${htmlStr}</div>`;
            container.scrollTop  = container.scrollHeight;
        } catch (e) {}
    },

    async executeOTAUpdate() {
        try {
            const input    = document.getElementById('c2-update-in');
            const inputRow = document.getElementById('c2-update-input-row');
            if (input)    input.disabled = true;
            if (inputRow) inputRow.style.display = 'none';
            this.logUpdate('<span style="color:var(--acc)">[INIT] Executing OTA Update Sequence...</span>');

            const response = await fetchWithTimeout('/api/system/start-update', { method: 'POST', headers: { 'X-CSRF-Token': window._csrfToken || '' } }, 90000);
            const result   = await response.json();

            if (response.ok) {
                this.logUpdate('⬇️  Downloading payload...');
                this.logUpdate('✅  Verification successful.');
                this.logUpdate('🔄  <span style="color:var(--warn)">REBOOTING CORE ARCHITECTURE...</span>');
                this.pollForLife();
            } else {
                throw new Error(result.detail || 'Unknown Server Error');
            }
        } catch (err) {
            try {
                this.logUpdate(`<span style="color:var(--err)">❌ FATAL: ${window.escapeHtml(err.message)}</span>`);
                const input    = document.getElementById('c2-update-in');
                const inputRow = document.getElementById('c2-update-input-row');
                if (input)    input.disabled = false;
                if (inputRow) inputRow.style.display = 'flex';
            } catch (e) {}
        }
    },

    pollForLife() {
        try {
            let attempts  = 0;
            const spinner = ['|', '/', '-', '\\'];
            const statusRow = document.createElement('div');
            statusRow.style.color = 'var(--warn)';
            document.getElementById('c2-update-log')?.appendChild(statusRow);

            const interval = setInterval(async () => {
                try {
                    attempts++;
                    statusRow.innerHTML = `<span class="c2-log-ts">SYS</span> Awaiting API restart... ${spinner[attempts % 4]}`;
                    if (attempts > 90) {
                        clearInterval(interval);
                        statusRow.innerHTML = `<span class="c2-log-ts">SYS</span> <span style="color:var(--err)">Timed out waiting for restart.</span>`;
                        return;
                    }
                    const resp = await fetchWithTimeout('/api/status', { cache: 'no-store' }, 3000);
                    if (resp.ok) {
                        clearInterval(interval);
                        statusRow.innerHTML = `<span class="c2-log-ts">SYS</span> <span style="color:var(--ok)">CORE ONLINE. Reloading UI.</span>`;
                        setTimeout(() => { try { location.reload(); } catch (e) {} }, 1500);
                    }
                } catch (e) { /* server still restarting */ }
            }, 2000);
        } catch (e) {}
    }
};
/* ==========================================================================
 * PLUGIN BRIDGE — window.PluginBridge
 *
 * Convenience API for bridge.html iframes (same-origin, window.parent access).
 * Provides named methods for common operations AND full escape hatches
 * (.dom / .state / .app) for unrestricted access — no artificial ceiling.
 *
 * Security model: plugins are same-origin and trusted by installation.
 * CSS.escape() guards all querySelector calls built from external data.
 * Every public method is individually try/caught so a plugin crash cannot
 * affect the host application.
 * ========================================================================== */

window.PluginBridge = (function () {
    'use strict';

    /* ── Internal registries — survive card re-renders ── */
    var _badges         = Object.create(null); // { nodeId: { badgeId: config } }
    var _sections       = Object.create(null); // { nodeId: { sectionId: config } }
    var _modalTabs      = [];                  // [{ id, label, pluginId, render(nodeId)->html }]
    var _ovPanels       = [];                  // [{ id, pluginId, html|render()->html, position }]
    var _ovToolbarItems = [];                  // [{ id, pluginId, html|render()->html }]
    var _nodeListeners  = [];                  // [fn(entries)]  — called after every _applyAll
    var _packetListeners= [];                  // [fn(packet)]
    var _nodeWatchers   = Object.create(null); // { nodeId: [fn(node,packet)] }
    var _injectedCss    = new Set();
    // _pluginPermissions is defined globally as window._pluginPermissions

    /* ── Permission check helper ── */
    var _permDeniedLogged = Object.create(null);
    function _checkPermission(pluginId, permission) {
        if (!pluginId) return false;
        var perms = window._pluginPermissions[pluginId];
        if (!perms || !perms.has(permission)) {
            var key = pluginId + ':' + permission;
            if (!_permDeniedLogged[key]) {
                _permDeniedLogged[key] = true;
                console.warn('[PluginBridge] Permission denied [' + permission + '] for plugin: ' + pluginId);
            }
            return false;
        }
        return true;
    }

    /* ── Safe CSS injection — idempotent, keyed by caller-supplied id ── */
    function _injectCss(id, css) {
        var safeId = 'pb-css-' + String(id).replace(/[^a-zA-Z0-9_-]/g, '_');
        if (_injectedCss.has(safeId)) return;
        try {
            var el = document.createElement('style');
            el.id = safeId;
            el.textContent = String(css);
            document.head.appendChild(el);
            _injectedCss.add(safeId);
        } catch (e) {}
    }

    /* ── Apply badges to one card element ── */
    function _applyBadgesToCard(cardEl, nodeId) {
        try {
            var zone = cardEl.querySelector('.pb-badge-zone');
            if (!zone) return;
            zone.innerHTML = '';
            var nodeBadges = _badges[nodeId];
            if (!nodeBadges) return;
            Object.keys(nodeBadges).forEach(function (bid) {
                var b = nodeBadges[bid];
                if (b.css) _injectCss(b.pluginId + '-badge-' + bid, b.css);
                try {
                    var wrap = document.createElement('span');
                    // The wrap fills the badge zone exactly (position:absolute; inset:0)
                    // so the ribbon child (position:absolute inside wrap) positions
                    // correctly within the 86x86 clipping box. pointer-events:none
                    // on the wrap means clicks fall through to the ribbon itself.
                    wrap.style.cssText = 'position:absolute;inset:0;pointer-events:none;';
                    wrap.innerHTML = String(b.html || '');
                    if (b.tabId) {
                        var _tabId = b.tabId, _nid = nodeId;
                        // Wire click on the ribbon element itself (not the wrap)
                        var ribbonEl = wrap.querySelector('.pb-ribbon');
                        var clickTarget = ribbonEl || wrap;
                        clickTarget.style.pointerEvents = 'auto';
                        clickTarget.addEventListener('click', function (e) {
                            e.stopPropagation();
                            try { window.c2OpenNodeDetail(_nid, _tabId); } catch(_e) {}
                        });
                    }
                    zone.appendChild(wrap);
                } catch (e) {}
            });
        } catch (e) {}
    }

    /* ── Apply sections to one card element ── */
    function _applySectionsToCard(cardEl, nodeId) {
        try {
            var zone = cardEl.querySelector('.pb-section-zone');
            if (!zone) return;
            zone.innerHTML = '';
            var nodeSections = _sections[nodeId];
            if (!nodeSections) return;
            Object.keys(nodeSections).forEach(function (sid) {
                var s = nodeSections[sid];
                if (s.css) _injectCss(s.pluginId + '-section-' + sid, s.css);
                try {
                    var div = document.createElement('div');
                    div.innerHTML = window.escapeHtml(String(s.html || ''));
                    zone.appendChild(div);
                } catch (e) {}
            });
        } catch (e) {}
    }

    /* ── Re-render overview panels ── */
    function _renderOvPanels() {
        try {
            if (!_ovPanels.length) return;
            if (window.meshState && window.meshState.currentView !== 'overview') return;
            _ovPanels.forEach(function (panel) {
                try {
                    var html = typeof panel.render === 'function' ? panel.render() : (panel.html || '');
                    var existing = document.getElementById('pb-panel-' + panel.id);
                    if (existing) { existing.innerHTML = html; return; }
                    var div = document.createElement('div');
                    div.id = 'pb-panel-' + panel.id;
                    div.className = 'pb-ov-panel';
                    div.innerHTML = html;
                    // Inject above or below #ov-node-grid
                    var grid = document.getElementById('ov-node-grid');
                    if (!grid) return;
                    var container = grid.parentElement;
                    if (!container) return;
                    if (panel.position === 'bottom') {
                        container.appendChild(div);
                    } else {
                        container.insertBefore(div, grid);
                    }
                } catch (e) {}
            });
        } catch (e) {}
    }

    /* ── Re-render overview toolbar items ── */
    function _renderOvToolbar() {
        try {
            if (!_ovToolbarItems.length) return;
            if (window.meshState && window.meshState.currentView !== 'overview') return;
            var bar = document.getElementById('pb-ov-toolbar');
            if (!bar) {
                bar = document.createElement('div');
                bar.id = 'pb-ov-toolbar';
                // Inject into .ov-node-toolbar which already has flex layout
                var toolbar = document.querySelector('.ov-node-toolbar');
                if (!toolbar) return;
                toolbar.appendChild(bar);
            }
            _ovToolbarItems.forEach(function (item) {
                if (bar.querySelector('#pb-tb-' + item.id)) return; // already injected
                try {
                    var div = document.createElement('div');
                    div.id = 'pb-tb-' + item.id;
                    div.innerHTML = typeof item.render === 'function' ? item.render() : (item.html || '');
                    bar.appendChild(div);
                } catch (e) {}
            });
        } catch (e) {}
    }

    /* ── Public bridge object ── */
    var PB = {

        /* ── Full escape hatches — unrestricted ── */
        get dom()   { return window.document; },
        get state() { return window.meshState; },
        get app()   { return window; },

        /* ── Live data ── */
        getNodes: function () {
            return Object.entries(window.meshState && window.meshState.nodes || {});
        },
        getNode: function (id) {
            return (window.meshState && window.meshState.nodes && window.meshState.nodes[id]) || null;
        },
        getMeshState: function () { return window.meshState; },
        getLocalNodeId: function () { return window.meshState && window.meshState.local_node_id; },
        getActiveSlot:  function () { return window._activeSlotId || 'node_0'; },

        /* ── UI primitives ── */
        toast: function (msg, type) {
            try { window.triggerToast(msg, type || 'acc'); } catch (e) {}
        },
        openNodeDetail: function (nodeId) {
            try { window.c2OpenNodeDetail(nodeId); } catch (e) {}
        },
        showModal: function (config) {
            try { return window.showModal(config); } catch (e) {}
        },
        navigateTo: function (viewName) {
            try { window.loadView(viewName); } catch (e) {}
        },

        /* ── Node card — badges ── */
        addNodeBadge: function (nodeId, config) {
            // config: { id, pluginId, html, css? }
            try {
                if (!nodeId || !config || !config.id) return;
                var pid = config.pluginId;
                if (!_checkPermission(pid, 'node_badges')) return;
                if (!_badges[nodeId]) _badges[nodeId] = Object.create(null);
                _badges[nodeId][config.id] = config;
                var card = document.querySelector('.ov-nc[data-node-id="' + CSS.escape(nodeId) + '"]');
                if (card) _applyBadgesToCard(card, nodeId);
            } catch (e) {}
        },
        removeNodeBadge: function (nodeId, badgeId) {
            try {
                if (_badges[nodeId]) {
                    delete _badges[nodeId][badgeId];
                    if (!Object.keys(_badges[nodeId]).length) delete _badges[nodeId];
                }
                var card = document.querySelector('.ov-nc[data-node-id="' + CSS.escape(nodeId) + '"]');
                if (card) _applyBadgesToCard(card, nodeId);
            } catch (e) {}
        },
        clearPluginBadges: function (pluginId) {
            try {
                Object.keys(_badges).forEach(function (nid) {
                    Object.keys(_badges[nid]).forEach(function (bid) {
                        if (_badges[nid][bid].pluginId === pluginId) delete _badges[nid][bid];
                    });
                    if (!Object.keys(_badges[nid]).length) delete _badges[nid];
                });
                PB._applyAll();
            } catch (e) {}
        },

        /* ── Node card — sections ── */
        addNodeCardSection: function (nodeId, config) {
            // config: { id, pluginId, html, css? }
            try {
                if (!nodeId || !config || !config.id) return;
                var pid = config.pluginId;
                if (!_checkPermission(pid, 'node_badges')) return;
                if (!_sections[nodeId]) _sections[nodeId] = Object.create(null);
                _sections[nodeId][config.id] = config;
                var card = document.querySelector('.ov-nc[data-node-id="' + CSS.escape(nodeId) + '"]');
                if (card) _applySectionsToCard(card, nodeId);
            } catch (e) {}
        },
        removeNodeCardSection: function (nodeId, sectionId) {
            try {
                if (_sections[nodeId]) delete _sections[nodeId][sectionId];
                var card = document.querySelector('.ov-nc[data-node-id="' + CSS.escape(nodeId) + '"]');
                if (card) _applySectionsToCard(card, nodeId);
            } catch (e) {}
        },
        clearPluginSections: function (pluginId) {
            try {
                Object.keys(_sections).forEach(function (nid) {
                    Object.keys(_sections[nid]).forEach(function (sid) {
                        if (_sections[nid][sid].pluginId === pluginId) delete _sections[nid][sid];
                    });
                    if (!Object.keys(_sections[nid]).length) delete _sections[nid];
                });
                PB._applyAll();
            } catch (e) {}
        },

        /* ── Node detail modal — extra tabs ── */
        addModalTab: function (config) {
            // config: { id, label, pluginId, render(nodeId) -> html string }
            try {
                if (!config || !config.id || typeof config.render !== 'function') return;
                if (!_checkPermission(config.pluginId, 'modal_tabs')) return;
                if (!_modalTabs.find(function (t) { return t.id === config.id; })) {
                    _modalTabs.push(config);
                }
            } catch (e) {}
        },
        removeModalTab: function (tabId) {
            try {
                var idx = _modalTabs.findIndex(function (t) { return t.id === tabId; });
                if (idx !== -1) _modalTabs.splice(idx, 1);
            } catch (e) {}
        },

        /* ── Overview — inject full panels ── */
        addOverviewPanel: function (config) {
            // config: { id, pluginId, html | render()->html, position: 'top'|'bottom' }
            try {
                if (!config || !config.id) return;
                if (!_checkPermission(config.pluginId, 'map_overlay')) return;
                if (!_ovPanels.find(function (p) { return p.id === config.id; })) {
                    _ovPanels.push(config);
                }
                _renderOvPanels();
            } catch (e) {}
        },
        removeOverviewPanel: function (id) {
            try {
                var idx = _ovPanels.findIndex(function (p) { return p.id === id; });
                if (idx !== -1) _ovPanels.splice(idx, 1);
                var el = document.getElementById('pb-panel-' + id);
                if (el) el.remove();
            } catch (e) {}
        },

        /* ── Overview — inject toolbar items ── */
        addOverviewToolbarItem: function (config) {
            // config: { id, pluginId, html | render()->html }
            try {
                if (!config || !config.id) return;
                if (!_checkPermission(config.pluginId, 'overview_toolbar')) return;
                if (!_ovToolbarItems.find(function (t) { return t.id === config.id; })) {
                    _ovToolbarItems.push(config);
                }
                _renderOvToolbar();
            } catch (e) {}
        },
        removeOverviewToolbarItem: function (id) {
            try {
                var idx = _ovToolbarItems.findIndex(function (t) { return t.id === id; });
                if (idx !== -1) _ovToolbarItems.splice(idx, 1);
                var el = document.getElementById('pb-tb-' + id);
                if (el) el.remove();
            } catch (e) {}
        },

        /* ── Event subscriptions ── */
        onNodesUpdated: function (callback) {
            try { if (typeof callback === 'function') _nodeListeners.push(callback); } catch (e) {}
        },
        onPacket: function (callback) {
            try { if (typeof callback === 'function') _packetListeners.push(callback); } catch (e) {}
        },
        onNodeUpdate: function (nodeId, callback) {
            try {
                if (!nodeId || typeof callback !== 'function') return;
                if (!_nodeWatchers[nodeId]) _nodeWatchers[nodeId] = [];
                _nodeWatchers[nodeId].push(callback);
            } catch (e) {}
        },
        /* ── Utility: wait for an element with timeout and max retries ── */
        waitForElement: function (selector, options) {
            options = options || {};
            var timeout = options.timeout || 5000;
            var interval = options.interval || 200;
            var maxRetries = options.maxRetries || Math.ceil(timeout / interval);
            var retries = 0;
            return new Promise(function (resolve, reject) {
                var timer = setInterval(function () {
                    var el = document.querySelector(selector);
                    if (el) { clearInterval(timer); resolve(el); return; }
                    retries++;
                    if (retries >= maxRetries) { clearInterval(timer); reject(new Error('Timeout waiting for ' + selector)); }
                }, interval);
            });
        },

        /* ── Check if a view is currently active ── */
        isViewActive: function (viewName) {
            return window.meshState && window.meshState.currentView === viewName;
        },

        /* ── Get current view name ── */
        getCurrentView: function () {
            return (window.meshState && window.meshState.currentView) || '';
        },

        offNodeUpdate: function (nodeId, callback) {
            try {
                if (!_nodeWatchers[nodeId]) return;
                _nodeWatchers[nodeId] = _nodeWatchers[nodeId].filter(function (fn) { return fn !== callback; });
            } catch (e) {}
        },

        /* ── Internal: called after every c2RenderNodes() ── */
        _applyAll: function () {
            try {
                document.querySelectorAll('.ov-nc[data-node-id]').forEach(function (card) {
                    var nid = card.getAttribute('data-node-id');
                    if (!nid) return;
                    _applyBadgesToCard(card, nid);
                    _applySectionsToCard(card, nid);
                });
                _renderOvPanels();
                _renderOvToolbar();
                // Notify node listeners
                var entries = PB.getNodes();
                _nodeListeners.forEach(function (cb) { try { cb(entries); } catch (e) {} });
            } catch (e) {}
        },

        /* ── Internal: inject plugin tabs into the node detail modal ── */
        _applyModalTabs: function (nodeId) {
            try {
                if (!_modalTabs.length) return;
                var tabBar    = document.getElementById('c2-modal-tabs');
                var contentEl = document.getElementById('c2-modal-content');
                if (!tabBar || !contentEl) return;

                _modalTabs.forEach(function (tab) {
                    if (tabBar.querySelector('[data-tab="pb-' + CSS.escape(tab.id) + '"]')) return;
                    var btn = document.createElement('button');
                    btn.className = 'btn btn-sm';
                    btn.setAttribute('data-tab', 'pb-' + tab.id);
                    btn.setAttribute('data-plugin-tab', tab.pluginId || '');
                    btn.textContent = String(tab.label || tab.id).toUpperCase();
                    btn.addEventListener('click', function (e) {
                        // stopPropagation prevents the delegated listener on #c2-modal-tabs
                        // (in ui.js) from catching this click and calling c2SwitchModalTab,
                        // which would immediately overwrite our content with "Executing DB Query..."
                        e.stopPropagation();
                        try {
                            tabBar.querySelectorAll('[data-tab]').forEach(function (b) { b.classList.remove('btn-acc'); });
                            btn.classList.add('btn-acc');
                            var html;
                            try { html = tab.render(nodeId); } catch (renderErr) {
                                html = '<div style="color:var(--err);padding:12px;font-family:var(--mono);font-size:11px;">Plugin render error: ' +
                                    window.escapeHtml(String(renderErr)) + '</div>';
                            }
                            contentEl.innerHTML = '<div style="padding:16px;">' + html + '</div>';
                        } catch (e) {}
                    });
                    tabBar.appendChild(btn);
                });
            } catch (e) {}
        },

        /* ── Internal: fire packet listeners — called by SSE packet handler ── */
        _onPacket: function (packet) {
            try {
                _packetListeners.forEach(function (cb) { try { cb(packet); } catch (e) {} });
                var fromId = packet && (packet.fromId || packet.from_id);
                if (fromId && _nodeWatchers[fromId] && _nodeWatchers[fromId].length) {
                    var node = PB.getNode(fromId);
                    _nodeWatchers[fromId].forEach(function (cb) { try { cb(node, packet); } catch (e) {} });
                }
            } catch (e) {}
        },

        /* ── Re-inject overview UI when view switches back ── */
        _onViewChange: function (viewName) {
            if (viewName !== 'overview') return;
            // Remove stale panel/toolbar elements so they re-render cleanly
            _ovPanels.forEach(function (p) {
                var el = document.getElementById('pb-panel-' + p.id);
                if (el) el.remove();
            });
            var bar = document.getElementById('pb-ov-toolbar');
            if (bar) bar.remove();
            // Re-render toolbar items after cleanup
            setTimeout(function() { try { _renderOvToolbar(); } catch(e) {} }, 100);
        },
    };

    return PB;
}());

/* ==========================================================================
 * PLUGIN BRIDGE — Hooks into existing app functions
 * All wrappers are self-healing: if the target function is not yet defined
 * (overview.js loads after app.js) they retry until it exists.
 * ========================================================================== */

/* ── Patch c2RenderNodes — call _applyAll after every render ── */
(function _patchRenderNodes() {
    'use strict';
    if (typeof window.c2RenderNodes !== 'function') {
        setTimeout(_patchRenderNodes, 100); return;
    }
    var _orig = window.c2RenderNodes;
    window.c2RenderNodes = function (softUpdate) {
        try { _orig.call(this, softUpdate); } catch (e) { console.warn('[PluginBridge] c2RenderNodes threw:', e); }
        try { window.PluginBridge._applyAll(); } catch (e) {}
    };
}());

/* ── Patch c2OpenNodeDetail — inject plugin tabs, support direct tab jump ── */
(function _patchNodeDetail() {
    'use strict';
    if (typeof window.c2OpenNodeDetail !== 'function') {
        setTimeout(_patchNodeDetail, 100); return;
    }
    var _orig = window.c2OpenNodeDetail;
    window.c2OpenNodeDetail = function (nodeId, _pbTabId) {
        // If a plugin badge click set a pending tab, store it before the modal opens.
        // c2SwitchModalTab is patched below to read this flag and skip the overview load.
        if (_pbTabId) window._pbPendingTab = _pbTabId;
        try { _orig.call(this, nodeId); } catch (e) { console.warn('[PluginBridge] c2OpenNodeDetail threw:', e); }
        setTimeout(function () { try { window.PluginBridge._applyModalTabs(nodeId); } catch (e) {} }, 60);
    };
}());

/* ── Patch c2SwitchModalTab — intercept overview load when plugin tab is pending ── */
(function _patchSwitchTab() {
    'use strict';
    if (typeof window.c2SwitchModalTab !== 'function') {
        setTimeout(_patchSwitchTab, 100); return;
    }
    var _origSwitch = window.c2SwitchModalTab;
    window.c2SwitchModalTab = function (tab, nodeId) {
        // If a plugin has requested a specific tab, suppress the default overview DB call
        // and switch to the plugin tab instead. The flag is consumed once.
        var pending = window._pbPendingTab;
        if (pending && tab === 'overview') {
            window._pbPendingTab = null;
            // Show a neutral loading state instead of "Executing DB Query..."
            var content = document.getElementById('c2-modal-content');
            if (content) {
                content.innerHTML = '<div style="text-align:center;padding:60px;color:var(--txt3);font-family:var(--mono);">'
                    + '<i class="fas fa-snowflake fa-spin" style="font-size:24px;margin-bottom:10px;color:var(--acc);"></i><br>LOADING...</div>';
            }
            // Switch to the plugin tab once it is injected (tabs arrive ~60ms after modal opens)
            setTimeout(function () {
                try {
                    var tabBar = document.getElementById('c2-modal-tabs');
                    if (!tabBar) return;
                    var target = tabBar.querySelector('[data-tab="pb-' + CSS.escape(pending) + '"]');
                    if (target) { target.click(); return; }
                    // Tab not injected yet — try once more after another tick
                    setTimeout(function () {
                        try {
                            var target2 = document.getElementById('c2-modal-tabs')
                                ?.querySelector('[data-tab="pb-' + CSS.escape(pending) + '"]');
                            if (target2) target2.click();
                        } catch (_e) {}
                    }, 100);
                } catch (_e) {}
            }, 80);
            return; // do NOT call the original — no DB query fires
        }
        window._pbPendingTab = null;
        // Safety net: if tab name starts with 'pb-' it's a plugin tab that should
        // have been handled by stopPropagation. If it somehow reaches here, ignore it
        // rather than showing "Executing DB Query..." for an unknown tab name.
        if (tab && String(tab).startsWith('pb-')) return;
        try { _origSwitch.call(this, tab, nodeId); } catch (e) {}
    };
}());

/* ── Patch loadView — notify bridge on view changes ── */
(function _patchLoadView() {
    'use strict';
    if (typeof window.loadView !== 'function') {
        setTimeout(_patchLoadView, 100); return;
    }
    var _orig = window.loadView;
    window.loadView = function (viewName) {
        try { window.PluginBridge._onViewChange(viewName); } catch (e) {}
        return _orig.call(this, viewName);
    };
}());

/* ── Tap the SSE packet handler to fire PluginBridge._onPacket ── */
/* _sseHandlers is defined earlier in this file — safe to patch here ── */
(function _patchSsePacket() {
    'use strict';
    if (typeof _sseHandlers === 'undefined') return;
    var _origPacket = _sseHandlers.packet;
    _sseHandlers.packet = function (e) {
        // Parse once, share with both the original handler and PluginBridge.
        // Store on a non-enumerable property of e to avoid two JSON.parse calls.
        var p = null;
        try { p = JSON.parse(e.data); e._parsed = p; } catch (_) {}
        try { _origPacket.call(_sseHandlers, e); } catch (err) {}
        if (p) {
            try { window.PluginBridge._onPacket(p); } catch (_) {}
        }
    };
}());

/* ==========================================================================
 * PLUGIN BRIDGE — Bridge iframe loader
 * Fetches /api/plugins/bridges after DOMContentLoaded, then creates hidden
 * iframes for every running plugin that declares "bridge" in its manifest.
 * Idempotent — safe to call multiple times.
 * ========================================================================== */

async function _loadPluginBridges() {
    'use strict';
    try {
        // Load plugin permissions from the system plugins endpoint
        window._pluginPermissions = Object.create(null);
        try {
            var pr = await fetch('/api/system/plugins', { credentials: 'include', cache: 'no-store' });
            if (pr.ok) {
                var pdata = await pr.json();
                var plugins = pdata.plugins || {};
                Object.keys(plugins).forEach(function (key) {
                    var p = plugins[key];
                    if (p.manifest && p.manifest.permissions) {
                        window._pluginPermissions[key] = new Set(p.manifest.permissions);
                    }
                });
            }
        } catch (_e) {}

        var r = await fetch('/api/plugins/bridges', { credentials: 'include', cache: 'no-store' });
        if (!r.ok) return;
        var data = await r.json();
        var container = document.getElementById('plugin-bridge-container');
        if (!container) return;

        (data.bridges || []).forEach(function (b) {
            if (!b.plugin_id || !b.bridge_src) return;
            var frameId = 'pb-iframe-' + b.plugin_id.replace(/[^a-zA-Z0-9_-]/g, '_');
            if (document.getElementById(frameId)) return; // idempotent

            var iframe = document.createElement('iframe');
            iframe.id  = frameId;
            // Allow same-origin scripts only — no external src allowed
            iframe.src = b.bridge_src;
            iframe.setAttribute('data-plugin-id', b.plugin_id);
            iframe.setAttribute('data-plugin-name', b.name || b.plugin_id);
            iframe.setAttribute('aria-hidden', 'true');
            iframe.setAttribute('tabindex', '-1');
            iframe.setAttribute('title', 'Plugin Bridge: ' + (b.name || b.plugin_id));
            // No sandbox attribute — same-origin, full parent access intentional
            container.appendChild(iframe);

        });
    } catch (e) {
        console.warn('[PluginBridge] Bridge loader error:', e);
    }
}

/* Hook into the existing DOMContentLoaded flow — call after initPluginsMenu */
(function _hookBridgeLoader() {
    'use strict';
    var _origInit = window._pluginBridgeHooked;
    if (_origInit) return; // prevent double-hook on re-execution
    window._pluginBridgeHooked = true;

    // The DOMContentLoaded handler in app.js calls initPluginsMenu().
    // We run _loadPluginBridges() after a short delay so the menu and
    // SSE connection are established first, meaning auth cookies are set.
    document.addEventListener('DOMContentLoaded', function () {
        setTimeout(_loadPluginBridges, 800);
    });

    // ── Theme change listener: re-inject theme into visible plugin iframe ──
    document.addEventListener('theme-changed', function(e) {
        var css = e.detail && e.detail.css;
        if (!css) return;
        try {
            var pluginFrame = document.querySelector('#content iframe[src*="/static/plugins/"]');
            if (pluginFrame && pluginFrame.contentDocument) {
                var existing = pluginFrame.contentDocument.getElementById('md-theme-override');
                if (existing) { existing.textContent = css; }
                else {
                    var s = pluginFrame.contentDocument.createElement('style');
                    s.id = 'md-theme-override';
                    s.setAttribute('data-theme-plugin', '1');
                    s.textContent = css;
                    pluginFrame.contentDocument.head.appendChild(s);
                }
            }
        } catch(err) { /* sandbox */ }
    });
}());