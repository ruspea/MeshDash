/* ==========================================================================
 * MeshDash C2 — Command Terminal & OTA Update Engine
 * ========================================================================== */

window.C2Terminal = {
    history: [],
    historyIdx: -1,
    isOpen: false,
    updatePending: false,
    versionData: null,
    isDragging: false,
    _saveTimer: null,
    _lastSystemMsg: '',
    _systemMsgCount: 0,

    init() {
        try {
            this.history = JSON.parse(localStorage.getItem('c2-term-history') || '[]');
        } catch (e) { this.history = []; }

        this.setupListeners();
        this.loadPersistence();
        
        // Hook into the global SSE stream gracefully
        this.hookSSE();

        // Start Vitals Refresh
        this.refreshVitals();
        setInterval(() => this.refreshVitals(), 20000);

        // Version Check (Delay slightly to ensure UI loads first)
        setTimeout(() => this.checkVersion(true), 3000);
        setInterval(() => this.checkVersion(false), 43200000); // 12 hours
    },

    setupListeners() {
        const consoleIn = document.getElementById('c2-console-in');
        if (consoleIn) {
            consoleIn.onkeydown = (e) => {
                if (e.key === 'Enter') this.executeCommand();
                if (e.key === 'ArrowUp') this.browseHistory(-1);
                if (e.key === 'ArrowDown') this.browseHistory(1);
            };
        }

        const updateIn = document.getElementById('c2-update-in');
        if (updateIn) {
            updateIn.onkeydown = (e) => {
                if (e.key !== 'Enter') return;
                const val = e.target.value.trim().toUpperCase();
                if (val === 'Y' || val === 'YES') {
                    this.executeOTAUpdate();
                } else if (val === 'N' || val === 'NO') {
                    this.logUpdate('<span style="color:var(--err)">[ABORT] Update cancelled by operator.</span>');
                    document.getElementById('c2-update-input-row').style.display = 'none';
                    this.updatePending = false;
                } else {
                    this.logUpdate('<span style="color:var(--warn)">[WARN] Invalid input. Type Y to proceed or N to cancel.</span>');
                }
                e.target.value = '';
            };
        }

        const badge = document.getElementById('c2-term-ver-badge');
        if (badge) {
            badge.onclick = (e) => {
                e.stopPropagation();
                this.handleVersionClick();
            };
        }

        // Resize Logic
        const handle = document.getElementById('c2-term-handle');
        const drawer = document.getElementById('c2-terminal-drawer');
        if (handle && drawer) {
            handle.onmousedown = () => {
                this.isDragging = true;
                document.onmousemove = me => {
                    if (!this.isDragging) return;
                    let h = window.innerHeight - me.clientY - 32;
                    h = Math.max(180, Math.min(h, window.innerHeight * 0.9));
                    drawer.style.height = h + 'px';
                };
                document.onmouseup = () => {
                    this.isDragging = false;
                    localStorage.setItem('c2-term-height', drawer.style.height);
                    document.onmousemove = null;
                    document.onmouseup = null;
                };
            };
        }
    },

    hookSSE() {
        // Poll for the global `es` object from app.js to become available
        const hookInterval = setInterval(() => {
            if (typeof es !== 'undefined' && es !== null && !es._c2_term_hooked) {
                es._c2_term_hooked = true;
                clearInterval(hookInterval);
                
                es.addEventListener('system_update', (e) => {
                    try {
                        const data = JSON.parse(e.data);
                        const msg = (data.message || String(e.data) || '').trim();
                        if (!msg) return;

                        if (msg === this._lastSystemMsg) {
                            this._systemMsgCount++;
                            const lastRow = document.getElementById('c2-tab-system')?.lastElementChild;
                            if (lastRow) {
                                const counter = lastRow.querySelector('.c2-dup-count');
                                if (counter) {
                                    counter.textContent = ` (×${this._systemMsgCount})`;
                                } else {
                                    lastRow.innerHTML += `<span class="c2-dup-count" style="color:var(--txt3); font-size:0.8em;"> (×${this._systemMsgCount})</span>`;
                                }
                            }
                        } else {
                            this._lastSystemMsg = msg;
                            this._systemMsgCount = 1;
                            this.log('system', `<span style="color:var(--acc)">[SYS]</span> ${msg}`);
                        }

                        if (msg.toLowerCase().includes('update available')) this.checkVersion();
                        this.triggerSave();
                    } catch(err) {}
                });

                es.addEventListener('packet', (e) => {
                    try {
                        const pkt = JSON.parse(e.data);
                        const type = pkt.app_packet_type || 'Data';
                        const from = pkt.fromId || 'Unknown';
                        
                        this.log('stream', `[${window.escapeHtml(type)}] From <span style="color:var(--acc)">${window.escapeHtml(from)}</span>`);
                        
                        if (type === 'Message') {
                            this.log('chat', `<b style="color:var(--acc)">${from}:</b> ${window.escapeHtml(pkt.decoded?.payload || '')}`);
                        }
                        if (type === 'Telemetry') {
                            const met = pkt.decoded?.telemetry?.deviceMetrics || {};
                            if (met.batteryLevel !== undefined) {
                                this.log('sensors', `<b>${from}:</b> Bat ${met.batteryLevel}% | ${met.voltage?.toFixed(2)}V`);
                            }
                        }
                        this.triggerSave();
                    } catch(err) {}
                });
            }
        }, 1000);
    },

    toggle() {
        this.isOpen = !this.isOpen;
        const drawer = document.getElementById('c2-terminal-drawer');
        const chevron = document.getElementById('c2-term-chevron');
        
        if (!drawer || !chevron) return;

        if (this.isOpen) {
            drawer.classList.add('open');
            chevron.style.transform = 'rotate(180deg)';
            setTimeout(() => {
                const activeTab = document.querySelector('.c2-term-tab.active')?.dataset.tab;
                if (activeTab === 'c2-tab-console') document.getElementById('c2-console-in')?.focus();
            }, 300);
        } else {
            drawer.classList.remove('open');
            chevron.style.transform = 'rotate(0deg)';
        }
        localStorage.setItem('c2-term-is-open', this.isOpen);
    },

    switchTab(tabId) {
        document.querySelectorAll('.c2-term-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.c2-term-content').forEach(c => c.classList.remove('active'));
        
        const tabBtn = document.querySelector(`[data-tab="${tabId}"]`);
        if(tabBtn) {
            tabBtn.classList.add('active');
            tabBtn.classList.remove('unread');
        }
        
        const target = document.getElementById(tabId);
        if (target) {
            target.classList.add('active');
            target.scrollTop = target.scrollHeight;
        }
        
        if (tabId === 'c2-tab-console') document.getElementById('c2-console-in')?.focus();
        if (tabId === 'c2-tab-update') document.getElementById('c2-update-in')?.focus();
        
        localStorage.setItem('c2-term-active-tab', tabId);
        this.triggerSave();
    },

    // ─────────────────── Vitals Mapping ───────────────────
    async refreshVitals() {
        try {
            const r = await fetch('/api/local_node/full');
            if(r.ok) {
                const data = await r.json();
                this.renderVitals(data);
            }
        } catch(e) {
            console.error("Vitals load failed:", e);
        }
    },

    renderVitals(data) {
        const container = document.getElementById('c2-vitals-container');
        if (!container) return;

        // Parse flat data structure from backend
        const name = data.long_name || data.short_name || 'Unnamed';
        const nid = data.node_id_hex || data.node_id || '00000000';
        const role = data.role || 'UNKNOWN';
        const bat = data.battery_level;
        const batStr = bat != null ? `${bat}%` : '-';
        
        // Internal Uptime Formatter (bulletproof)
        let up = '-';
        const s = data.uptime_seconds || 0;
        if (s > 0) {
            const d = Math.floor(s / 86400);
            const h = Math.floor((s % 86400) / 3600);
            const m = Math.floor((s % 3600) / 60);
            up = d > 0 ? `${d}d ${h}h` : `${h}h ${m}m`;
        }

        let hw = data.hw_model || data.hardware_model_string || 'Unknown';
        if (hw.includes('.')) hw = hw.split('.').pop();
        
        const fw = data.firmware_version || '-';
        const air = data.air_util_tx;
        const airStr = air != null ? `${parseFloat(air).toFixed(1)}%` : '-';
        const pwr = data.lora_tx_power;
        const pwrStr = pwr != null ? `${pwr} dBm` : '-';

        requestAnimationFrame(() => {
            container.innerHTML = `
                <div class="c2-vital-card">
                    <div class="c2-vital-title"><i class="fas fa-id-card"></i> Node Identity</div>
                    <div class="c2-vital-row"><span>Name</span><span class="c2-vital-val" style="color:var(--acc)">${name}</span></div>
                    <div class="c2-vital-row"><span>ID</span><span class="c2-vital-val">${nid.replace('!','')}</span></div>
                    <div class="c2-vital-row"><span>Role</span><span class="c2-vital-val">${role}</span></div>
                </div>
                <div class="c2-vital-card">
                    <div class="c2-vital-title"><i class="fas fa-bolt"></i> System Vitals</div>
                    <div class="c2-vital-row"><span>Battery</span><span class="c2-vital-val">${batStr}</span></div>
                    <div class="c2-vital-row"><span>Uptime</span><span class="c2-vital-val">${up}</span></div>
                </div>
                <div class="c2-vital-card">
                    <div class="c2-vital-title"><i class="fas fa-microchip"></i> Hardware</div>
                    <div class="c2-vital-row"><span>Model</span><span class="c2-vital-val">${hw}</span></div>
                    <div class="c2-vital-row"><span>Firmware</span><span class="c2-vital-val">${fw}</span></div>
                </div>
                <div class="c2-vital-card">
                    <div class="c2-vital-title"><i class="fas fa-broadcast-tower"></i> LoRa Status</div>
                    <div class="c2-vital-row"><span>Air Util</span><span class="c2-vital-val">${airStr}</span></div>
                    <div class="c2-vital-row"><span>TX Power</span><span class="c2-vital-val">${pwrStr}</span></div>
                </div>`;
        });
    },

    // ─────────────────── Console Engine ───────────────────
    async executeCommand() {
        const input = document.getElementById('c2-console-in');
        const output = document.getElementById('c2-console-out');
        const cmd = input.value.trim();
        if (!cmd) return;

        this.history.unshift(cmd);
        if (this.history.length > 50) this.history.pop();
        localStorage.setItem('c2-term-history', JSON.stringify(this.history));
        this.historyIdx = -1;

        output.innerHTML += `<div class="c2-cmd-echo">> meshtastic ${window.escapeHtml(cmd)}</div>`;
        input.value = '';
        output.scrollTop = output.scrollHeight;

        try {
            const res = await fetch('/api/console', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command: cmd, slot_id: window._activeSlotId === 'all' ? 'node_0' : (window._activeSlotId || 'node_0') })
            });
            const text = await res.text();
            output.innerHTML += `<div class="c2-cmd-resp">${window.escapeHtml(text)}</div>`;
            this.triggerSave();
        } catch (err) {
            output.innerHTML += `<div class="c2-cmd-resp" style="color:var(--err); border-color:var(--err);">Connection Error: ${window.escapeHtml(err.message)}</div>`;
        }
        output.scrollTop = output.scrollHeight;
    },

    browseHistory(dir) {
        const input = document.getElementById('c2-console-in');
        this.historyIdx += dir;
        if (this.historyIdx < -1) this.historyIdx = -1;
        if (this.historyIdx >= this.history.length) this.historyIdx = this.history.length - 1;
        input.value = this.historyIdx === -1 ? '' : this.history[this.historyIdx];
    },

    // ─────────────────── Logging & Persistence ───────────────────
    log(target, html) {
        html = window.escapeHtml(html);
        const container = document.getElementById(`c2-tab-${target}`);
        if (!container) return;
        const div = document.createElement('div');
        div.className = 'c2-log-row';
        const ts = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
        div.innerHTML = `<span class="c2-log-ts">${ts}</span> ${html}`;
        
        requestAnimationFrame(() => {
            container.appendChild(div);
            
            const tabBtn = document.querySelector(`[data-tab="c2-tab-${target}"]`);
            if (tabBtn && !tabBtn.classList.contains('active')) tabBtn.classList.add('unread');

            while (container.childElementCount > 100) container.removeChild(container.firstChild);
            if (container.classList.contains('active')) container.scrollTop = container.scrollHeight;
        });
    },

    triggerSave() {
        if (this._saveTimer) return;
        this._saveTimer = setTimeout(() => {
            this._saveTimer = null;
            try {
                const state = {
                    stream:  document.getElementById('c2-tab-stream').innerHTML,
                    chat:    document.getElementById('c2-tab-chat').innerHTML,
                    sensors: document.getElementById('c2-tab-sensors').innerHTML,
                    system:  document.getElementById('c2-tab-system').innerHTML,
                    console: document.getElementById('c2-console-out').innerHTML
                };
                localStorage.setItem('c2-term-state', JSON.stringify(state));
            } catch(e) {}
        }, 1000);
    },

    loadPersistence() {
        try {
            const savedState = localStorage.getItem('c2-term-state');
            if (savedState) {
                const state = JSON.parse(savedState);
                document.getElementById('c2-tab-stream').innerHTML  = state.stream  || '';
                document.getElementById('c2-tab-chat').innerHTML    = state.chat    || '';
                document.getElementById('c2-tab-sensors').innerHTML = state.sensors || '';
                document.getElementById('c2-tab-system').innerHTML  = state.system  || '';
                document.getElementById('c2-console-out').innerHTML = state.console || 'CLI Bridge Ready.\n';
            }
            const savedHeight = localStorage.getItem('c2-term-height');
            if (savedHeight) document.getElementById('c2-terminal-drawer').style.height = savedHeight;
            
            const isOpen = localStorage.getItem('c2-term-is-open') === 'true';
            if (isOpen) this.toggle();

            const activeTab = localStorage.getItem('c2-term-active-tab');
            if (activeTab) this.switchTab(activeTab);
        } catch(e) {}
    },

    clearPersistence() {
        if (confirm('Clear all log history and persistence?')) {
            localStorage.removeItem('c2-term-state');
            localStorage.removeItem('c2-term-history');
            location.reload();
        }
    },

    // ─────────────────── OTA UPDATE ENGINE ───────────────────
    async checkVersion(initialLoad = false) {
        const icon = document.getElementById('c2-term-ver-icon');
        const text = document.getElementById('c2-term-ver-text');
        const badge = document.getElementById('c2-term-ver-badge');
        
        if(icon) icon.className = 'fas fa-circle-notch fa-spin';

        try {
            const r = await fetch('/api/system/version-status');
            if (!r.ok) throw new Error('API Error');
            const d = await r.json();

            this.versionData = d;

            if (d.status === 'update_needed') {
                badge.className = 'term-badge update';
                icon.className = 'fas fa-cloud-download-alt';
                text.innerText = `v${d.local} ➔ v${d.remote}`;
                this.stageUpdate(d);
            } else if (d.status === 'beta') {
                badge.className = 'term-badge beta';
                icon.className = 'fas fa-flask';
                text.innerText = `v${d.local} (BETA)`;
            } else {
                badge.className = 'term-badge current';
                icon.className = 'fas fa-check';
                text.innerText = `v${d.local}`;
            }
        } catch (e) {
            if(badge) badge.className = 'term-badge';
            if(icon) icon.className = 'fas fa-unlink';
            if(text) text.innerText = 'OFFLINE';
        }
    },

    handleVersionClick() {
        if (!this.versionData) return;
        const icon = document.getElementById('c2-term-ver-icon');
        if(icon) icon.className = 'fas fa-circle-notch fa-spin';

        if (this.versionData.status === 'update_needed') {
            this.updatePending = false;
            this.stageUpdate(this.versionData);
            return;
        }

        this.checkVersion().then(() => {
            this.log('system', `Manual Version check complete. Status: ${this.versionData?.status || 'Unknown'}`);
        });
    },

    async triggerForceUpdate() {
        if (!confirm('FORCE UPDATE: This will overwrite system files even if you are up to date. Continue?')) return;
        await this.checkVersion();
        const forceData = { ...(this.versionData || {}), status: 'update_needed', isForced: true };
        this.updatePending = false;
        this.stageUpdate(forceData);
    },

    stageUpdate(data) {
        const updateTabBtn = document.getElementById('c2-tab-btn-update');
        if (updateTabBtn) {
            updateTabBtn.style.display = 'block';
            updateTabBtn.classList.add('update');
        }

        if (!this.updatePending) {
            this.updatePending = true;
            if (!this.isOpen) this.toggle();
            this.switchTab('c2-tab-update');

            document.getElementById('c2-update-log').innerHTML = '';

            if (data.isForced) {
                this.logUpdate(`🛠️ <b style="color:var(--err)">FORCE INSTALL TRIGGERED</b>`);
                this.logUpdate(`Current: v${data.local || '?'} | Target: v${data.remote || data.local || '?'}`);
            } else {
                this.logUpdate(`🚀 <b>OTA UPDATE AVAILABLE:</b> v${data.local} ➔ v${data.remote}`);
            }
            this.logUpdate('------------------------------------------------');
            this.logUpdate('⚠️  WARNING: This process will replace core system files.');
            this.logUpdate(' - <b>Databases & Configs:</b> PRESERVED');
            this.logUpdate(' - <b>System Service:</b> WILL AUTO-RESTART');
            this.logUpdate('------------------------------------------------');
            this.logUpdate('Do you wish to proceed with the download and installation?');

            document.getElementById('c2-update-input-row').style.display = 'flex';
        }
    },

    logUpdate(htmlStr) {
        htmlStr = window.escapeHtml(htmlStr);
        const container = document.getElementById('c2-update-log');
        if (!container) return;
        const ts = new Date().toLocaleTimeString([], { hour12: false });
        container.innerHTML += `<div><span class="c2-log-ts">${ts}</span> ${htmlStr}</div>`;
        container.scrollTop = container.scrollHeight;
    },

    async executeOTAUpdate() {
        const input = document.getElementById('c2-update-in');
        const inputRow = document.getElementById('c2-update-input-row');
        
        input.disabled = true;
        inputRow.style.display = 'none';
        this.logUpdate('<span style="color:var(--acc)">[INIT] Executing OTA Update Sequence...</span>');

        try {
            const response = await fetch('/api/system/start-update', { method: 'POST' });
            const result = await response.json();
            
            if (response.ok) {
                this.logUpdate('⬇️  Downloading payload...');
                this.logUpdate('✅  Verification successful.');
                this.logUpdate('🔄  <span style="color:var(--warn)">REBOOTING CORE ARCHITECTURE...</span>');
                this.pollForLife();
            } else {
                throw new Error(result.detail || 'Unknown Server Error');
            }
        } catch (err) {
            this.logUpdate(`<span style="color:var(--err)">❌ FATAL: ${err.message}</span>`);
            input.disabled = false;
            inputRow.style.display = 'flex';
        }
    },

    pollForLife() {
        let attempts = 0;
        const spinner = ['|', '/', '-', '\\'];
        
        const statusRow = document.createElement('div');
        statusRow.style.color = 'var(--warn)';
        document.getElementById('c2-update-log').appendChild(statusRow);

        const interval = setInterval(async () => {
            attempts++;
            statusRow.innerHTML = `<span class="c2-log-ts">SYS</span> Awaiting API restart... ${spinner[attempts % 4]}`;
            
            try {
                const resp = await fetch('/api/status', { cache: 'no-store' });
                if (resp.ok) {
                    clearInterval(interval);
                    statusRow.innerHTML = `<span class="c2-log-ts">SYS</span> <span style="color:var(--ok)">CORE ONLINE. Reloading UI.</span>`;
                    setTimeout(() => location.reload(), 1500);
                }
            } catch (e) { /* Server is down/restarting */ }
        }, 2000);
    }
};

window.escapeHtml = function(text) {
    if (!text) return '';
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
};

document.addEventListener('DOMContentLoaded', () => {
    window.C2Terminal.init();
});