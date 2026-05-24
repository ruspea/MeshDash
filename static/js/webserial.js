'use strict';

const WEB_SERIAL_HARD_DEFAULT = true;
const WEB_SERIAL_ENABLED      = WEB_SERIAL_HARD_DEFAULT;

// 🟢 ADDED: Verbose logging toggle
const VERBOSE_DEBUG = false; 

const _SERIAL_MAGIC1 = 0x94;
const _SERIAL_MAGIC2 = 0xC3;
const _MAX_PAYLOAD   = 512;

const _WS_PACKET_EP = '/api/webserial/packet';
const _WS_SEND_EP   = '/api/webserial/send';
const _WS_STATUS_EP = '/api/webserial/status';

const _wsState = {
    port:            null,
    reader:          null,
    writer:          null,
    connected:       false,
    connecting:      false,
    rxBytes:         0,
    txBytes:         0,
    packetsRx:       0,
    packetsTx:       0,
    errors:          0,
    connectTime:     null,
    portInfo:        null,
    baudRate:        115200,
    _readLoopActive: false,
    _cancelReader:   false,
};

let _wsStreamActive = false;
let _wsDebugLogged  = false;

// 🟢 ADDED: Helper for verbose logging
function logDebug(ctx, ...args) {
    if (!VERBOSE_DEBUG) return;
    // console.log(`%c[WS-DEBUG] [${ctx}]`, 'color: #00c8f5; font-weight: bold;', ...args);
}

// ─── API ───────────────────────────────────────────────────────────────
window.WebSerialBridge = {

    get enabled()   { return WEB_SERIAL_ENABLED; },
    get supported() { return WEB_SERIAL_ENABLED && ('serial' in navigator); },
    get connected() { return _wsState.connected; },
    get stats()     { return { ..._wsState }; },

    async connect() { this.openModal(); return false; },

    async connectWithBaud(baudRate = 115200) {
        logDebug('CONNECT', `Initiating connectWithBaud(${baudRate})`);
        
        if (!('serial' in navigator)) {
            window.triggerToast('Web Serial requires HTTPS or localhost.', 'err');
            _wsRenderModal('setup');
            return false;
        }
        if (!WEB_SERIAL_ENABLED) { window.triggerToast('Web Serial is disabled.', 'err'); return false; }
        if (_wsState.connected || _wsState.connecting) { window.triggerToast('Already connected.', 'warn'); return false; }

        try {
            _wsState.connecting = true;
            _wsState.baudRate   = baudRate;
            _wsBroadcastStatus('CONNECTING');

            logDebug('CONNECT', 'Requesting port from user...');
            const port = await navigator.serial.requestPort({
                filters: [
                    { usbVendorId: 0x10C4 },
                    { usbVendorId: 0x1A86 },
                    { usbVendorId: 0x0403 },
                    { usbVendorId: 0x239A },
                    { usbVendorId: 0x303A },
                ]
            });

            logDebug('CONNECT', 'Port selected. Info:', port.getInfo?.());
            logDebug('CONNECT', `Opening port at ${baudRate} baud...`);
            
            await port.open({ baudRate });
            logDebug('CONNECT', 'Port opened successfully. Releasing DTR/RTS...');

            try {
                await port.setSignals({ dataTerminalReady: false, requestToSend: false });
                logDebug('CONNECT', 'DTR/RTS released successfully.');
            } catch (err) {
                logDebug('CONNECT', 'Warning: setSignals failed (non-fatal on some OS)', err);
            }

            _wsState.port          = port;
            _wsState.portInfo      = port.getInfo?.() || {};
            _wsState.connected     = true;
            _wsState.connecting    = false;
            _wsState.connectTime   = Date.now();
            _wsState.rxBytes       = 0;
            _wsState.txBytes       = 0;
            _wsState.packetsRx     = 0;
            _wsState.packetsTx     = 0;
            _wsState.errors        = 0;
            _wsState._cancelReader = false;
            _wsStreamActive        = false;
            _wsDebugLogged         = false;

            _wsState.writer = port.writable.getWriter();
            logDebug('CONNECT', 'Writer acquired.');

            // 🟢 INJECTED BLOCK: The Meshtastic Client Handshake
            try {
                logDebug('CONNECT', 'Fetching wakeup frame from backend...');
                const wakeRes = await fetch('/api/webserial/wakeup');
                if (wakeRes.ok) {
                    const wakeBytes = new Uint8Array(await wakeRes.arrayBuffer());
                    await _wsState.writer.write(wakeBytes);
                    logDebug('CONNECT', `Fired want_config_id frame (${wakeBytes.length} bytes) to radio!`);
                } else {
                    logDebug('CONNECT', `Backend wakeup failed: ${wakeRes.status}`);
                }
            } catch (e) {
                logDebug('CONNECT', 'Network error fetching wakeup frame:', e);
            }
            // 🟢 END INJECTED BLOCK

            await _wsNotifyBackend('connected');
            _wsBroadcastStatus('CONNECTED');
            _wsUpdateUI();
            window.triggerToast(`Web Serial: Connected at ${baudRate} bps.`, 'ok');


            _wsReadLoop(port);
            return true;

        } catch (err) {
            _wsState.connecting = false;
            _wsState.connected  = false;
            if (err.name === 'NotFoundError' || err.message?.includes('No port selected')) {
                logDebug('CONNECT', 'User cancelled port selection.');
                _wsBroadcastStatus('IDLE');
            } else {
                logDebug('CONNECT', 'FATAL ERROR during connect:', err);
                console.error('Web Serial connect error:', err);
                window.triggerToast(`Web Serial Error: ${err.message}`, 'err');
                _wsBroadcastStatus('ERROR');
            }
            return false;
        }
    },

    async disconnect() {
        logDebug('DISCONNECT', 'Disconnect requested.');
        if (!_wsState.connected && !_wsState.port) return;
        try {
            _wsState._cancelReader = true;
            if (_wsState.reader) { try { await _wsState.reader.cancel(); } catch (_) {} _wsState.reader = null; }
            if (_wsState.writer) { try { _wsState.writer.releaseLock(); } catch (_) {} _wsState.writer = null; }
            if (_wsState.port)   { try { await _wsState.port.close();   } catch (_) {} _wsState.port   = null; }
            _wsState.connected  = false;
            _wsState.connecting = false;
            await _wsNotifyBackend('disconnected');
            _wsBroadcastStatus('IDLE');
            _wsUpdateUI();
            logDebug('DISCONNECT', 'Disconnected successfully.');
            window.triggerToast('Web Serial: Disconnected.', 'acc');
        } catch (err) {
            logDebug('DISCONNECT', 'Error during disconnect:', err);
            console.error('Web Serial disconnect error:', err);
        }
    },

    async sendText(text, destinationId = '^all', channelIndex = 0) {
        logDebug('TX', `Attempting to send text: "${text}" to ${destinationId}`);
        if (!_wsState.connected || !_wsState.writer) return false;
        try {
            const res = await fetch(_WS_SEND_EP, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ text, destinationId, channelIndex }),
            });
            if (!res.ok) throw new Error(`encode error ${res.status}`);
            const bytes = new Uint8Array(await res.arrayBuffer());
            
            logDebug('TX', `Writing ${bytes.length} bytes to serial port`);
            await _wsState.writer.write(bytes);
            
            _wsState.txBytes  += bytes.length;
            _wsState.packetsTx++;
            try { flashLED?.('tx'); }           catch (_) {}
            try { logTraffic?.('TX', 'Message'); } catch (_) {}
            return true;
        } catch (err) {
            logDebug('TX', 'Send failed:', err);
            console.error('Web Serial sendText error:', err);
            window.triggerToast(`Send failed: ${err.message}`, 'err');
            _wsState.errors++;
            return false;
        }
    },

    openModal() { if (WEB_SERIAL_ENABLED) _wsRenderModal(); },
};

// ─── Read loop ────────────────────────────────────────────────────────────────

async function _wsReadLoop(port) {
    if (_wsState._readLoopActive) return;
    _wsState._readLoopActive = true;
    let buf = new Uint8Array(0);
    logDebug('RX-LOOP', 'Started main read loop.');

    try {
        while (port.readable && !_wsState._cancelReader) {
            let reader;
            try { reader = port.readable.getReader(); _wsState.reader = reader; }
            catch (e) { logDebug('RX-LOOP', 'Failed to get reader', e); break; }
            
            try {
                while (true) {
                    if (_wsState._cancelReader) break;
                    let result;
                    try { result = await reader.read(); }
                    catch (e) { logDebug('RX-LOOP', 'Read error:', e); console.warn('Web Serial read error:', e); _wsState.errors++; break; }
                    
                    if (result.done) {
                        logDebug('RX-LOOP', 'Reader signaled done.');
                        break;
                    }
                    
                    const chunk = result.value;
                    if (!chunk?.length) continue;
                    _wsState.rxBytes += chunk.length;

                    // 🟢 ADDED: Verbose logging of incoming raw chunk
                    if (VERBOSE_DEBUG) {
                        const hex   = Array.from(chunk).map(b => b.toString(16).padStart(2, '0')).join(' ');
                        const ascii = Array.from(chunk).map(b => (b >= 0x20 && b < 0x7f) || b === 0x0a || b === 0x0d ? String.fromCharCode(b) : '.').join('');
                        logDebug('RX-CHUNK', `Got ${chunk.length} bytes | ASCII: ${ascii.replace(/\n/g, '\\n').replace(/\r/g, '\\r')} | HEX: ${hex}`);
                    }

                    // Merge chunk into running buffer
                    const merged = new Uint8Array(buf.length + chunk.length);
                    merged.set(buf);
                    merged.set(chunk, buf.length);
                    buf = merged;

                    buf = _wsParse(buf);
                }
            } finally {
                try { reader.releaseLock(); } catch (_) {}
                _wsState.reader = null;
            }
        }
    } catch (err) {
        logDebug('RX-LOOP', 'Fatal error in read loop:', err);
        console.error('Web Serial read loop fatal:', err);
        _wsState.errors++;
    } finally {
        logDebug('RX-LOOP', 'Exited read loop.');
        _wsState._readLoopActive = false;
        if (_wsState.connected && !_wsState._cancelReader) {
            console.warn('Web Serial: read loop ended unexpectedly — disconnecting.');
            await window.WebSerialBridge.disconnect();
        }
    }
}

// ─── Frame parser ─────────────────────────────────────────────────────────────

function _wsParse(buf) {
    while (buf.length >= 4) {
        if (buf[0] !== _SERIAL_MAGIC1 || buf[1] !== _SERIAL_MAGIC2) {
            let i = 1;
            while (i < buf.length - 1 && !(buf[i] === _SERIAL_MAGIC1 && buf[i + 1] === _SERIAL_MAGIC2)) i++;
            const skipped = buf.slice(0, i);
            buf = buf.slice(i);
            // Too noisy to log every skipped byte during boot, but useful if stream is live
            if (_wsStreamActive) logDebug('PARSE', `Skipped ${i} non-magic bytes.`);
            continue;
        }

        const payloadLen = (buf[2] << 8) | buf[3];
        if (payloadLen === 0)            { logDebug('PARSE', 'Zero length payload dropped'); buf = buf.slice(4); continue; }
        if (payloadLen > _MAX_PAYLOAD)   { logDebug('PARSE', `Oversized payload dropped: ${payloadLen}`); buf = buf.slice(2); continue; }
        if (buf.length < 4 + payloadLen) { 
            // Incomplete frame, wait for more bytes
            break; 
        }

        const payload = buf.slice(4, 4 + payloadLen);
        buf = buf.slice(4 + payloadLen);

        if (!_wsStreamActive) {
            _wsStreamActive = true;
            _wsSetStatus('LIVE');

            logDebug('PARSE', 'Stream transitioned to LIVE state.');
        }

        logDebug('PARSE', `Extracted frame. Payload length: ${payloadLen}`);
        _wsDispatch(payload).catch(e => console.debug('WS dispatch error:', e));
    }
    return buf;
}

// ─── Packet dispatcher ────────────────────────────────────────────────────────

async function _wsDispatch(rawBytes) {
    try {
        try { flashLED?.('rx'); } catch (_) {}

        logDebug('DISPATCH', `Forwarding ${rawBytes.length} bytes to backend via POST ${_WS_PACKET_EP}`);
        
        const res = await fetch(_WS_PACKET_EP, {
            method:  'POST',
            headers: { 'Content-Type': 'application/octet-stream' },
            body:    rawBytes,
        });

        // 🟢 ADDED: Log HTTP status from backend to catch 502s
        logDebug('DISPATCH', `Backend responded with HTTP ${res.status}`);

        if (!res.ok) { 
            _wsState.errors++; 
 
            logDebug('DISPATCH', `Error response from backend. Raw bytes were not parsed.`);
            return; 
        }

        const ev = await res.json();
        logDebug('DISPATCH', `Decoded JSON from backend:`, ev);

        if (!ev) return;

        const { event: type, data } = ev;
        if (type === 'ignored' || type === 'dropped') {
            logDebug('DISPATCH', `Backend ignored/dropped packet`);
            return;
        }

        if (type === 'packet' && data) { _wsState.packetsRx++; _wsInject(data); return; }

        if (type === 'local_node_info' && data?.node_id) {
            logDebug('DISPATCH', `Updating local identity:`, data.node_id);
            window.meshState.local_node_id = data.node_id;
            window.meshState.nodes[data.node_id] = Object.assign(window.meshState.nodes[data.node_id] || {}, data);
            try { updateLocalIdentity?.(data); }                 catch (_) {}
            try { window._triggerActiveViewNodeUpdate?.(true); } catch (_) {}
            return;
        }

        if (type === 'node_update' && data?.node_id) {
            window.meshState.nodes[data.node_id] = Object.assign(window.meshState.nodes[data.node_id] || {}, data);
            try { window._triggerActiveViewNodeUpdate?.(true); } catch (_) {}
            return;
        }

        if (type === 'connection_status' && data) {
            logDebug('DISPATCH', `Connection status update:`, data);
            window.meshState.connectionStatus = data;
            try { setRadioStatus?.(data); } catch (_) {}
            return;
        }

        if (!type && (ev.fromId || ev.app_packet_type)) { _wsState.packetsRx++; _wsInject(ev); }

    } catch (err) {
        if (err.name !== 'AbortError') { 
            console.debug('Web Serial dispatch error:', err); 
            logDebug('DISPATCH', 'Exception in dispatch:', err);
            _wsState.errors++; 
        }
    }
}

// ─── UI ───────────────────────────────────────────────────────────────────────
// (The rest of the UI code remains exactly identical to your original code)

function _wsSetStatus(state) {
    try {
        const btn = document.getElementById('ws-connect-btn');
        if (state === 'LIVE' && btn && _wsState.connected) {
            btn.innerHTML = '<i class="fas fa-usb" style="font-size:11px;"></i><span>USB LIVE</span>';
            btn.className = 'btn btn-ok';
            btn.title     = 'Web Serial: packets flowing — click for stats';
        }
    } catch (_) {}
}

function _wsInject(p) {
    try {
        // ANTI-DUPLICATION LOGIC
        // If SSE already delivered the enriched packet, drop this raw USB version
        if (p.id) {
            const existing = window.meshState.packets.some(ex => ex.id === p.id);
            if (existing) return; 
        }

        window.meshState.packets.unshift(p);
        if (window.meshState.packets.length > 500) window.meshState.packets.pop();

        if (p.fromId && window.meshState.nodes) {
            const n = window.meshState.nodes[p.fromId] || {};
            if (p.lastHeard)    n.lastHeard = p.lastHeard;
            if (p.snr  != null) n.snr       = p.snr;
            if (p.rssi != null) n.rssi      = p.rssi;
            window.meshState.nodes[p.fromId] = n;
        }

        try { flashLED('rx'); }                            catch (_) {}
        try { logTraffic('RX', p.app_packet_type || 'RAW'); } catch (_) {}
        try { window.C2Terminal?.handlePacket(p); }        catch (_) {}
        try { window.C2SharkApp?.globalIngest(p); }        catch (_) {}

        if (window.meshState.currentView === 'overview') {
            try { window.c2RenderFeed?.(); } catch (_) {}
        }

        if (p.app_packet_type === 'Message') {
            try { _wsTrackUnread(p); } catch (_) {}
        }

        _wsUpdateStatsPill();
    } catch (err) {
        // Suppressed: webserial packet injection errors are non-fatal
    }
}

function _wsTrackUnread(p) {
    const norm = (raw) => {
        if (raw == null) return null;
        if (typeof raw === 'number') return raw === 4294967295 ? '^all' : `!${raw.toString(16).padStart(8, '0')}`;
        return String(raw);
    };
    const fromId     = norm(p.fromId ?? p.from_id ?? null);
    const toId       = norm(p.toId   ?? p.to_id   ?? null);
    const local      = window.meshState.local_node_id;
    const isBcast    = toId === '^all' || (p.toId ?? p.to_id) === 4294967295;
    const isFromSelf = local && fromId === local;

    if (!isBcast && !isFromSelf && fromId) {
        const reading = window.meshState.currentView === 'dmes' && window.C2CommsApp?.selectedNodeId === fromId;
        if (!reading) {
            window.meshState.dmUnread[fromId] = (window.meshState.dmUnread[fromId] || 0) + 1;
            window.updateDmNavBadge?.();
        } else {
            try { window.C2CommsApp?.appendMessageToLog(p); } catch (_) {}
        }
    }

    if (isBcast && !isFromSelf) {
        const ch      = typeof p.channel === 'number' ? p.channel : 0;
        const reading = window.meshState.currentView === 'channels' && window.C2ChannelsApp?.selectedChannelIdx === ch;
        if (!reading) {
            window.meshState.channelUnread[ch] = (window.meshState.channelUnread[ch] || 0) + 1;
            window.updateChannelNavBadge?.();
        } else {
            try { window.C2ChannelsApp?.appendMessageToLog(p); } catch (_) {}
        }
    }
}

async function _wsNotifyBackend(status) {
    try {
        await fetch(_WS_STATUS_EP, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ status }),
        });
    } catch (_) {}
}

function _wsBroadcastStatus(status) {
    window.meshState.webSerialStatus = status;
    try {
        const dot = document.getElementById('dot-nod');
        const val = document.getElementById('v-nod');
        if (status === 'CONNECTED') {
            dot && (dot.className = 'rmd-u-dot lg ok');
            if (val) { val.innerText = 'WEB SERIAL'; val.className = 'rmd-u-val ok'; }
        } else if (status === 'CONNECTING') {
            dot && (dot.className = 'rmd-u-dot lg warn');
            if (val) { val.innerText = 'SERIAL...'; val.className = 'rmd-u-val warn'; }
        } else if (status === 'ERROR') {
            dot && (dot.className = 'rmd-u-dot lg err');
            if (val) { val.innerText = 'SERIAL ERR'; val.className = 'rmd-u-val err'; }
        }
    } catch (_) {}
    try { _wsRefreshModal(); } catch (_) {}
}

function _wsUpdateUI() {
    try {
        const btn = document.getElementById('ws-connect-btn');
        if (!btn) return;
        if (_wsState.connected) {
            btn.innerHTML = _wsStreamActive
                ? '<i class="fas fa-usb" style="font-size:11px;"></i><span>USB LIVE</span>'
                : '<i class="fas fa-circle-notch fa-spin" style="font-size:11px;"></i><span>CONNECTING…</span>';
            btn.className = _wsStreamActive ? 'btn btn-ok' : 'btn btn-warn';
            btn.title     = 'Web Serial connected — click for stats';
        } else if (_wsState.connecting) {
            btn.innerHTML = '<i class="fas fa-circle-notch fa-spin" style="font-size:11px;"></i><span>CONNECTING…</span>';
            btn.className = 'btn btn-warn';
        } else {
            btn.innerHTML = '<i class="fas fa-usb" style="font-size:11px;"></i><span>WEB SERIAL</span>';
            btn.className = 'btn btn-acc';
            btn.title     = 'Connect your radio via USB directly in the browser';
        }
    } catch (_) {}
    _wsUpdateStatsPill();
}

function _wsUpdateStatsPill() {
    try {
        const pill = document.getElementById('ws-stats-pill');
        if (!pill) return;
        if (!_wsState.connected) { pill.style.display = 'none'; return; }
        pill.style.display = 'inline-flex';
        const uptime = _wsState.connectTime ? Math.floor((Date.now() - _wsState.connectTime) / 1000) : 0;
        pill.textContent = `USB · RX:${_wsState.packetsRx} TX:${_wsState.packetsTx} · ${window.fmtUptime?.(uptime) || '0m'}`;
    } catch (_) {}
}

function _wsRenderModal(view) {
    document.getElementById('ws-modal-overlay')?.remove();
    if (!view) view = _wsState.connected ? 'live' : 'setup';

    const overlay = document.createElement('div');
    overlay.id = 'ws-modal-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9990;background:rgba(0,0,0,0.82);display:flex;align-items:center;justify-content:center;backdrop-filter:blur(3px);';
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

    const wrap = document.createElement('div');
    wrap.style.cssText = 'background:var(--bg2);border:1px solid var(--acc);border-radius:10px;padding:0;max-width:560px;width:94vw;font-family:var(--mono);box-shadow:0 0 60px rgba(0,200,245,0.18);overflow:hidden;max-height:90vh;display:flex;flex-direction:column;';

    const hdr = document.createElement('div');
    hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--bd2);background:rgba(0,200,245,0.05);flex-shrink:0;';
    hdr.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;">
            <div style="width:36px;height:36px;border-radius:8px;background:rgba(0,200,245,0.12);border:1px solid rgba(0,200,245,0.3);display:flex;align-items:center;justify-content:center;font-size:18px;">🔌</div>
            <div>
                <div style="font-size:13px;font-weight:800;color:var(--acc);letter-spacing:2px;">WEB SERIAL BRIDGE</div>
                <div style="font-size:9px;color:var(--txt3);margin-top:1px;letter-spacing:1px;">BROWSER-DIRECT RADIO CONNECTION</div>
            </div>
        </div>
        <button id="ws-modal-close" class="btn btn-sm" style="min-width:auto;">✕ CLOSE</button>`;
    hdr.querySelector('#ws-modal-close').addEventListener('click', () => overlay.remove());
    wrap.appendChild(hdr);

    const body = document.createElement('div');
    body.style.cssText = 'padding:20px;overflow-y:auto;flex:1;';
    body.id = 'ws-modal-body';

    if (view === 'setup')   _wsSetupView(body);
    if (view === 'picking') _wsPickingView(body);
    if (view === 'live')    _wsLiveView(body);

    wrap.appendChild(body);
    overlay.appendChild(wrap);
    document.body.appendChild(overlay);
}

function _wsSetupView(body) {
    const isHttps   = location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
    // Chrome and Edge always include 'Safari/' in their UA for compat — must NOT exclude on it.
    // Most reliable: if navigator.serial exists, the browser supports Web Serial (Chrome/Edge).
    // Fallback UA check excludes only real Firefox; real Safari lacks 'Chrome' entirely.
    const isChrome  = ('serial' in navigator) ||
        (/Chrome|Edg/.test(navigator.userAgent) && !/Firefox/.test(navigator.userAgent));
    const hasSerial = 'serial' in navigator;

    const chk = (ok, label, sub) => `
        <div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--bd2);">
            <div style="width:20px;height:20px;border-radius:50%;flex-shrink:0;margin-top:1px;background:${ok ? 'rgba(0,232,122,0.15)' : 'rgba(255,48,80,0.12)'};border:1px solid ${ok ? 'var(--ok)' : 'var(--err)'};display:flex;align-items:center;justify-content:center;font-size:10px;color:${ok ? 'var(--ok)' : 'var(--err)'};">${ok ? '✓' : '✗'}</div>
            <div><div style="font-size:11px;color:${ok ? 'var(--txt)' : 'var(--err)'};font-weight:${ok ? '400' : '700'};">${label}</div>${sub ? `<div style="font-size:10px;color:var(--txt3);margin-top:2px;">${sub}</div>` : ''}</div>
        </div>`;

    if (!hasSerial) {
        body.innerHTML = `
            <div style="background:rgba(255,48,80,0.08);border:2px solid rgba(255,48,80,0.4);border-radius:8px;padding:16px;margin-bottom:18px;">
                <div style="font-size:13px;font-weight:800;color:var(--err);margin-bottom:8px;">🚫 Web Serial unavailable</div>
                <div style="font-size:11px;color:var(--txt2);line-height:1.8;">
                    Chrome/Edge only expose <code style="color:var(--acc);">navigator.serial</code> on <b>HTTPS</b> or <b>localhost</b>.<br>
                    Current origin: <code style="color:var(--warn);">${location.origin}</code>
                </div>
            </div>
            <div style="background:rgba(0,232,122,0.05);border:1px solid rgba(0,232,122,0.2);border-radius:6px;padding:14px;margin-bottom:10px;">
                <div style="font-size:11px;font-weight:800;color:var(--ok);margin-bottom:6px;">⭐ Open on localhost instead</div>
                <a href="http://localhost:${window.escapeHtml(location.port || 8000)}" target="_blank" style="display:inline-block;padding:5px 12px;background:rgba(0,232,122,0.1);border:1px solid var(--ok);border-radius:4px;color:var(--ok);font-size:11px;text-decoration:none;">http://localhost:${location.port || 8000}</a>
            </div>
            <div style="background:rgba(0,200,245,0.04);border:1px solid rgba(0,200,245,0.2);border-radius:6px;padding:14px;margin-bottom:18px;">
                <div style="font-size:11px;font-weight:800;color:var(--acc);margin-bottom:6px;">Or enable via Chrome flag</div>
                <code style="display:block;padding:6px 10px;background:rgba(0,0,0,0.3);border-radius:4px;color:var(--acc);font-size:10px;word-break:break-all;cursor:pointer;" onclick="navigator.clipboard?.writeText(this.textContent)">chrome://flags/#unsafely-treat-insecure-origin-as-secure</code>
                <div style="font-size:10px;color:var(--txt2);margin-top:6px;">Add <code style="color:var(--warn);">${window.escapeHtml(location.origin)}</code>, enable, relaunch.</div>
            </div>
            <div style="display:flex;justify-content:flex-end;"><button id="ws-cancel" class="btn btn-sm">CLOSE</button></div>`;
        body.querySelector('#ws-cancel').addEventListener('click', () => document.getElementById('ws-modal-overlay')?.remove());
        return;
    }

    body.innerHTML = `
        <div style="margin-bottom:18px;">
            <div style="font-size:9px;color:var(--txt3);font-weight:800;letter-spacing:1.5px;margin-bottom:10px;">REQUIREMENTS</div>
            <div style="background:var(--bg);border:1px solid var(--bd2);border-radius:6px;padding:4px 12px;">
                ${chk(isChrome,  'Chrome or Edge browser', isChrome ? 'Compatible browser detected' : 'Firefox and Safari do not support Web Serial')}
                ${chk(isHttps,   'HTTPS or localhost', `Origin: ${location.origin}`)}
                ${chk(hasSerial, 'Web Serial API available', 'navigator.serial detected ✓')}
            </div>
        </div>
        <div style="background:rgba(0,200,245,0.04);border:1px solid rgba(0,200,245,0.18);border-radius:6px;padding:14px;margin-bottom:18px;">
            <div style="font-size:9px;color:var(--acc);font-weight:800;letter-spacing:1.5px;margin-bottom:10px;">HOW IT WORKS</div>
            <div style="display:flex;flex-direction:column;gap:8px;">
                ${[
                    'Plug your Meshtastic radio into this computer via USB',
                    'Click <b>Select COM Port</b> — Chrome will show a port picker popup',
                    'Select the entry matching your radio chipset (CP210x, CH340…)',
                    'DTR/RTS are released immediately — no reboot, packets flow within seconds',
                ].map((t, i) => `<div style="display:flex;gap:10px;align-items:flex-start;"><div style="width:20px;height:20px;border-radius:50%;flex-shrink:0;background:rgba(0,200,245,0.12);border:1px solid rgba(0,200,245,0.3);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:800;color:var(--acc);">${i + 1}</div><div style="font-size:11px;color:var(--txt2);line-height:1.6;padding-top:1px;">${t}</div></div>`).join('')}
            </div>
        </div>
        <div style="margin-bottom:18px;">
            <div style="font-size:9px;color:var(--txt3);font-weight:800;letter-spacing:1.5px;margin-bottom:8px;">BAUD RATE</div>
            <select id="ws-baud" style="background:var(--bg);border:1px solid var(--bd2);color:var(--txt);font-family:var(--mono);font-size:12px;padding:7px 12px;border-radius:4px;width:100%;">
                <option value="115200" selected>115200 — Standard Meshtastic (recommended)</option>
                <option value="921600">921600 — High speed (some devices)</option>
                <option value="57600">57600 — Legacy modules</option>
            </select>
        </div>
        <div style="background:rgba(255,168,38,0.06);border:1px solid rgba(255,168,38,0.2);border-radius:6px;padding:12px;margin-bottom:20px;font-size:10px;color:var(--txt2);line-height:1.7;">
            <b style="color:var(--warn);">⚠ No port in list?</b> Install the driver:
            <a href="https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers" target="_blank" style="color:var(--acc);">CP210x (Silicon Labs)</a> ·
            <a href="https://www.wch.cn/downloads/CH341SER_EXE.html" target="_blank" style="color:var(--acc);">CH340 (WCH)</a>
        </div>
        <div style="display:flex;gap:10px;justify-content:flex-end;">
            <button id="ws-cancel" class="btn btn-sm">CANCEL</button>
            <button id="ws-go" class="btn btn-acc" style="display:flex;align-items:center;gap:8px;font-size:12px;padding:8px 18px;">
                <i class="fas fa-usb"></i> SELECT COM PORT &amp; CONNECT
            </button>
        </div>`;

    body.querySelector('#ws-cancel').addEventListener('click', () => document.getElementById('ws-modal-overlay')?.remove());
    body.querySelector('#ws-go').addEventListener('click', async () => {
        const baud = parseInt(body.querySelector('#ws-baud')?.value || '115200', 10);
        _wsRenderModal('picking');
        const ok = await window.WebSerialBridge.connectWithBaud(baud);
        if (ok) { _wsRenderModal('live'); }
        else if (document.getElementById('ws-modal-overlay')) { _wsRenderModal('setup'); }
    });
}

function _wsPickingView(body) {
    body.innerHTML = `
        <div style="text-align:center;padding:40px 20px;">
            <div style="font-size:40px;margin-bottom:16px;animation:ws-pulse 1.2s ease-in-out infinite;">🔌</div>
            <div style="font-size:14px;font-weight:800;color:var(--acc);letter-spacing:2px;margin-bottom:12px;">SELECT YOUR PORT</div>
            <div style="font-size:11px;color:var(--txt2);line-height:1.8;max-width:360px;margin:0 auto;">
                A browser popup is asking you to select a serial port.<br>
                Choose the entry matching your Meshtastic radio (CP210x, CH340 or similar),
                then click <b style="color:var(--txt);">Connect</b>.
            </div>
            <div style="margin-top:20px;font-size:10px;color:var(--txt3);">
                Waiting… <span style="display:inline-block;animation:ws-spin 1s linear infinite;">⟳</span>
            </div>
        </div>
        <style>
            @keyframes ws-pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.6;transform:scale(0.92)} }
            @keyframes ws-spin  { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
        </style>`;
}

function _wsLiveView(body) {
    const info   = _wsState.portInfo || {};
    const uptime = _wsState.connectTime ? Math.floor((Date.now() - _wsState.connectTime) / 1000) : 0;
    const vid    = (info.usbVendorId  || 0).toString(16).padStart(4, '0').toUpperCase();
    const pid    = (info.usbProductId || 0).toString(16).padStart(4, '0').toUpperCase();
    const chip   = { 0x10C4: 'Silicon Labs CP210x', 0x1A86: 'WCH CH340/CH9102', 0x0403: 'FTDI FT232', 0x239A: 'Adafruit', 0x303A: 'Espressif (built-in)' }[info.usbVendorId] || 'Unknown';

    const stat = (label, id, val, color) =>
        `<div style="background:var(--bg);border:1px solid var(--bd2);border-radius:6px;padding:12px;">
            <div style="font-size:9px;color:var(--txt3);font-weight:800;letter-spacing:1px;margin-bottom:5px;">${label}</div>
            <div style="font-size:18px;font-weight:800;color:${color};"><span id="${id}">${val}</span></div>
        </div>`;

    body.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;background:rgba(0,232,122,0.08);border:1px solid rgba(0,232,122,0.25);border-radius:8px;padding:14px 16px;margin-bottom:18px;">
            <div style="width:12px;height:12px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok);animation:ws-blink 2s ease-in-out infinite;flex-shrink:0;"></div>
            <div>
                <div style="font-size:13px;font-weight:800;color:var(--ok);letter-spacing:1px;">RADIO CONNECTED VIA BROWSER USB</div>
                <div style="font-size:10px;color:var(--txt2);margin-top:2px;">DTR/RTS released — no reboot, packets flow immediately.</div>
            </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:18px;">
            ${stat('PACKETS RECEIVED', 'ws-lv-rx',  _wsState.packetsRx,  'var(--acc)')}
            ${stat('PACKETS SENT',     'ws-lv-tx',  _wsState.packetsTx,  'var(--txt)')}
            ${stat('DATA RX',          'ws-lv-brx', _fmtBytes(_wsState.rxBytes), 'var(--txt)')}
            ${stat('DATA TX',          'ws-lv-btx', _fmtBytes(_wsState.txBytes), 'var(--txt)')}
            ${stat('UPTIME',           'ws-lv-up',  window.fmtUptime?.(uptime) || '<1m', 'var(--txt)')}
            ${stat('ERRORS',           'ws-lv-err', _wsState.errors, _wsState.errors > 0 ? 'var(--err)' : 'var(--ok)')}
        </div>
        <div style="background:var(--bg);border:1px solid var(--bd2);border-radius:6px;padding:14px;margin-bottom:18px;">
            <div style="font-size:9px;color:var(--txt3);font-weight:800;letter-spacing:1.5px;margin-bottom:10px;">ACTIVE PORT</div>
            <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:11px;">
                <span style="color:var(--txt3);">Chipset</span>   <span style="color:var(--acc);font-weight:700;">${chip}</span>
                <span style="color:var(--txt3);">USB IDs</span>   <span style="color:var(--txt);">VID:${window.escapeHtml(vid)} · PID:${window.escapeHtml(pid)}</span>
                <span style="color:var(--txt3);">Baud Rate</span> <span style="color:var(--txt);">${window.escapeHtml(_wsState.baudRate)} bps</span>
            </div>
        </div>
        <div style="display:flex;gap:10px;justify-content:flex-end;">
            <button id="ws-live-close" class="btn btn-sm">CLOSE</button>
            <button id="ws-live-disc" class="btn btn-err" style="display:flex;align-items:center;gap:8px;">
                <i class="fas fa-plug" style="font-size:11px;"></i> DISCONNECT
            </button>
        </div>
        <style>@keyframes ws-blink { 0%,100%{opacity:1} 50%{opacity:0.4} }</style>`;

    body.querySelector('#ws-live-close').addEventListener('click', () => document.getElementById('ws-modal-overlay')?.remove());
    body.querySelector('#ws-live-disc').addEventListener('click', async () => {
        const btn = body.querySelector('#ws-live-disc');
        btn.disabled  = true;
        btn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Disconnecting…';
        await window.WebSerialBridge.disconnect();
        document.getElementById('ws-modal-overlay')?.remove();
    });
}

function _wsRefreshModal() {
    try {
        const uptime = _wsState.connectTime ? Math.floor((Date.now() - _wsState.connectTime) / 1000) : 0;
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set('ws-lv-rx',  _wsState.packetsRx);
        set('ws-lv-tx',  _wsState.packetsTx);
        set('ws-lv-brx', _fmtBytes(_wsState.rxBytes));
        set('ws-lv-btx', _fmtBytes(_wsState.txBytes));
        set('ws-lv-up',  window.fmtUptime?.(uptime) || '<1m');
        const err = document.getElementById('ws-lv-err');
        if (err) { err.textContent = _wsState.errors; err.style.color = _wsState.errors > 0 ? 'var(--err)' : 'var(--ok)'; }
    } catch (_) {}
}

function _fmtBytes(b) {
    if (!b) return '0B';
    if (b < 1024) return `${b}B`;
    if (b < 1048576) return `${(b / 1024).toFixed(1)}KB`;
    return `${(b / 1048576).toFixed(1)}MB`;
}

const _wsStatsInterval = setInterval(() => {
    if (!WEB_SERIAL_ENABLED) return;
    if (_wsState.connected) {
        try { _wsUpdateStatsPill(); } catch (_) {}
        if (document.getElementById('ws-modal-overlay')) { try { _wsRefreshModal(); } catch (_) {} }
    }
}, 2000);

window.addEventListener('beforeunload', () => {
    if (!_wsState.connected) return;
    navigator.sendBeacon(_WS_STATUS_EP, new Blob([JSON.stringify({ status: 'disconnected' })], { type: 'application/json' }));
    try { _wsState._cancelReader = true; } catch(_) {}
});

window.addEventListener('unload', () => {
    clearInterval(_wsStatsInterval);
});

function _wsShowButton() {
    if (!WEB_SERIAL_HARD_DEFAULT) return;
    const btn = document.getElementById('ws-connect-btn');
    if (!btn) return;
    window.WebSerialBridge._configMode = 'WEBSERIAL';
    if ('serial' in navigator) {
        btn.style.cssText = 'display:inline-flex;';
        btn.title         = 'Connect radio via USB directly in the browser';
        btn.innerHTML     = '<i class="fas fa-usb" style="font-size:11px;"></i><span>WEB SERIAL</span>';
        btn.className     = 'btn btn-acc';

    } else {
        btn.style.display    = 'inline-flex';
        btn.style.background = 'rgba(255,168,38,0.15)';
        btn.style.border     = '1px solid var(--warn)';
        btn.style.color      = 'var(--warn)';
        btn.title            = 'Web Serial requires HTTPS — click for instructions';
        btn.innerHTML        = '<i class="fas fa-usb" style="font-size:11px;"></i><span>USB ⚠</span>';
    }
    const ns = document.getElementById('ws-not-supported');
    if (ns) ns.style.display = 'none';
}

window.WebSerialBridge._activateButton = _wsShowButton;
window.WebSerialBridge._modalAction    = async function () {
    if (_wsState.connected) { await window.WebSerialBridge.disconnect(); _wsRenderModal('setup'); }
    else { _wsRenderModal('picking'); await window.WebSerialBridge.connect(); }
    _wsUpdateUI();
};

async function _wsBootCheck() {
    if (!WEB_SERIAL_HARD_DEFAULT) return;
    try {
        const res = await fetch('/api/system/config', { credentials: 'same-origin' });
        if (!res.ok) return;
        const cfg  = await res.json();
        const mode = (cfg.MESHTASTIC_CONNECTION_TYPE || '').toUpperCase().trim();
        if (mode === 'WEBSERIAL') {
            window.WebSerialBridge._configMode = 'WEBSERIAL';
            _wsShowButton();
        } else if (window.WebSerialBridge._configMode !== 'WEBSERIAL') {
            const btn = document.getElementById('ws-connect-btn');
            if (btn) btn.style.display = 'none';
        }
    } catch (_) {}
}

window.WebSerialBridge._recheckConfig = _wsBootCheck;

(function () {
    if (!WEB_SERIAL_HARD_DEFAULT) return;
    const Orig = window.EventSource;
    window.EventSource = function (url, init) {
        const es = new Orig(url, init);
        es.addEventListener('connection_status', e => {
            try {
                const s = JSON.parse(e.data);
                if (typeof s === 'string' && s.toLowerCase().includes('web serial')) _wsShowButton();
            } catch (_) {}
        });
        return es;
    };
    Object.assign(window.EventSource, { prototype: Orig.prototype, CONNECTING: Orig.CONNECTING, OPEN: Orig.OPEN, CLOSED: Orig.CLOSED });
}());

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(_wsBootCheck, 6000));
} else {
    setTimeout(_wsBootCheck, 6000);
}

if (!WEB_SERIAL_ENABLED) {
    window.WebSerialBridge = {
        enabled: false, supported: false, connected: false, _configMode: 'DISABLED',
        connect: async () => false, connectWithBaud: async () => false, disconnect: async () => {},
        sendText: async () => false, openModal: () => {}, stats: {}, _recheckConfig: () => {}, _activateButton: () => {},
    };
}