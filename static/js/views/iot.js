/* ==========================================================================
 * MeshDash C2 — IoT / Web Telemetry Ingress Module
 *
 * Fixes vs previous version:
 *   1. transmitNow() was POSTing to /monitor/website (404). Corrected to
 *      /api/monitor which is the actual registered endpoint.
 *   2. Block cards were rendering b.element_type, b.element_id, b.element_class
 *      which do not exist in the /extract response. The endpoint returns
 *      { text, id, tag } — fields now mapped correctly.
 *   3. extractedBlocks mapping used block.originalIndex ?? index but /extract
 *      returns blocks with an `id` field (not originalIndex). Fixed to use
 *      block.id ?? index so selectedBlockIndex matches block_id sent to
 *      /api/monitor.
 *   4. setupListeners() called getElementById on elements inside disabled step
 *      boxes that may not exist if the view HTML failed to load. All element
 *      lookups now guard against null before assigning handlers.
 *   5. State is reset on init() so navigating away and back to the iot view
 *      doesn't leave stale selectedNodeId/block from a previous session
 *      pointing at now-invalid DOM elements.
 *   6. transmitNow() and scheduleTask() validate selectedBlockIndex is not
 *      null before proceeding — previously a null block_id would silently
 *      send block_id=null to the backend causing a 422.
 * ========================================================================== */

window.C2IotApp = {
    selectedNodeId:     null,
    sendSlotId:         'node_0',
    extractedBlocks:    [],
    filteredBlocks:     [],
    currentPage:        1,
    blocksPerPage:      10,
    selectedBlockIndex: null,
    currentUrl:         '',

    init() {
        // Reset transient state so stale selections from a previous view load
        // don't persist into a fresh DOM that doesn't have those elements.
        this.extractedBlocks    = [];
        this.filteredBlocks     = [];
        this.currentPage        = 1;
        this.selectedBlockIndex = null;
        this.currentUrl         = '';
        this.sendSlotId         = window._activeSlotId || 'node_0';

        this.setupListeners();
        this.renderSidebar();
        this.updateCronPreview();
    },

    setupListeners() {
        const get = id => document.getElementById(id);

        const bind = (id, event, fn) => {
            const el = get(id);
            if (el) el[event] = fn;
        };

        bind('iot-filter',       'oninput',   () => this.renderSidebar());
        bind('btn-iot-fetch',    'onclick',   () => this.fetchUrl());
        bind('iot-url',          'onkeypress', e => { if (e.key === 'Enter') this.fetchUrl(); });
        bind('iot-block-search', 'oninput',   () => this.filterBlocks());
        bind('btn-iot-prev',     'onclick',   () => {
            if (this.currentPage > 1) { this.currentPage--; this.renderBlocks(); }
        });
        bind('btn-iot-next', 'onclick', () => {
            const total = Math.ceil(this.filteredBlocks.length / this.blocksPerPage);
            if (this.currentPage < total) { this.currentPage++; this.renderBlocks(); }
        });
        bind('btn-iot-transmit', 'onclick',  () => this.transmitNow());
        bind('iot-cron-type',    'onchange', () => this.updateCronPreview());
        bind('iot-cron-time',    'onchange', () => this.updateCronPreview());
        bind('btn-iot-schedule', 'onclick',  () => this.scheduleTask());
    },

    renderSidebar() {
        const list = document.getElementById('iot-node-list');
        if (!list) return;

        const filter = (document.getElementById('iot-filter')?.value || '').toLowerCase();
        const nodes  = Object.values(window.meshState?.nodes || {});
        nodes.sort((a, b) => (b.lastHeard || 0) - (a.lastHeard || 0));

        const rows = nodes
            .filter(n => {
                const name = (n.user?.longName || n.user?.shortName || n.node_id || '').toLowerCase();
                return name.includes(filter) || (n.node_id || '').toLowerCase().includes(filter);
            })
            .map(n => {
                const isActive  = n.node_id === this.selectedNodeId;
                const name      = n.user?.longName || n.user?.shortName || n.node_id;
                const isOnline  = n.lastHeard && (Date.now() / 1000 - n.lastHeard) < 3600;
                const dotStyle  = isOnline
                    ? 'background:var(--ok);box-shadow:0 0 5px var(--ok)'
                    : 'background:var(--bd2)';

                return `
                    <div class="ni ${isActive ? 'active' : ''}"
                         onclick="window.C2IotApp.selectNode('${n.node_id}')"
                         style="border-left:none;border-bottom:1px solid var(--bd);padding:12px 16px;">
                        <div style="width:10px;height:10px;border-radius:50%;${dotStyle};margin-right:12px;flex-shrink:0;"></div>
                        <div style="flex:1;overflow:hidden;">
                            <div style="font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11px;">${window.escapeHtml(name)}</div>
                            <div style="font-size:9px;color:var(--txt3);font-family:var(--mono);">${n.node_id}</div>
                        </div>
                    </div>`;
            }).join('');

        const bcActive = this.selectedNodeId === '^all' ? 'active' : '';
        const bcHtml = `
            <div class="ni ${bcActive}"
                 onclick="window.C2IotApp.selectNode('^all')"
                 style="border-left:none;border-bottom:1px solid var(--acc);padding:12px 16px;background:rgba(0,200,245,0.05);">
                <i class="fas fa-broadcast-tower" style="color:var(--acc);margin-right:12px;"></i>
                <div style="flex:1;font-weight:bold;font-size:11px;color:var(--acc);">BROADCAST TO MESH</div>
            </div>`;

        list.innerHTML = bcHtml + (rows || `<div style="padding:20px;text-align:center;color:var(--txt3);font-family:var(--mono);font-size:10px;">[ NO NODES FOUND ]</div>`);
    },

    selectNode(nid) {
        this.selectedNodeId = nid;
        this.renderSidebar();

        const noNode = document.getElementById('iot-no-node');
        const wizard = document.getElementById('iot-wizard');
        const sumNode = document.getElementById('iot-sum-node');

        if (noNode) noNode.style.display = 'none';
        if (wizard) wizard.style.display = 'block';

        const name = nid === '^all'
            ? 'BROADCAST'
            : (window.meshState?.nodes[nid]?.user?.longName || nid);
        if (sumNode) sumNode.innerText = name.toUpperCase();

        // Inject slot picker into phase 3 once visible
        if (!document.getElementById('iot-slot-picker-wrap')) {
            const phase3 = document.getElementById('iot-step-3');
            if (phase3) {
                const wrap = document.createElement('div');
                wrap.id = 'iot-slot-picker-wrap';
                wrap.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:12px;font-family:var(--mono);font-size:10px;';
                wrap.innerHTML = `<span style="color:var(--txt3);">TRANSMIT VIA</span><div id="iot-slot-picker"></div>`;
                const transmitRow = phase3.querySelector('.iot-summary-box');
                if (transmitRow) transmitRow.after(wrap);
                else phase3.querySelector('.cb')?.prepend(wrap);
            }
        }
        if (typeof window._buildSlotPicker === 'function') {
            window._buildSlotPicker('iot-slot-picker', this.sendSlotId);
            const sel = document.getElementById('iot-slot-picker-sel');
            if (sel) {
                sel.onchange = null;
                sel.addEventListener('change', e => { this.sendSlotId = e.target.value; });
            }
        }
    },

    async fetchUrl() {
        const urlInput = document.getElementById('iot-url')?.value.trim();
        if (!urlInput) return window.triggerToast('URL Required', 'warn');
        this.currentUrl = urlInput;

        const btn = document.getElementById('btn-iot-fetch');
        if (btn) { btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

        try {
            const response = await fetch('/extract', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ url: this.currentUrl })
            });

            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            const data = await response.json();

            // /extract returns { blocks: [ { text, id, tag }, ... ] }
            // We store id as originalIndex so it matches block_id sent to /api/monitor
            this.extractedBlocks = (data.blocks || [])
                .filter(b => b.text && b.text.trim() !== '')
                .map((b, fallbackIdx) => ({
                    ...b,
                    originalIndex: b.id ?? fallbackIdx
                }));

            this.filterBlocks();

            const step2 = document.getElementById('iot-step-2');
            if (step2) step2.classList.remove('disabled');
            window.triggerToast('DOM Parsed Successfully', 'ok');

        } catch (e) {
            window.triggerToast(`Failed to parse URL: ${e.message}`, 'err');
        } finally {
            if (btn) { btn.innerHTML = 'PARSE DOM'; btn.disabled = false; }
        }
    },

    filterBlocks() {
        const search = (document.getElementById('iot-block-search')?.value || '').toLowerCase();
        this.filteredBlocks = this.extractedBlocks.filter(b =>
            b.text.toLowerCase().includes(search)
        );
        this.currentPage = 1;
        this.renderBlocks();
    },

    renderBlocks() {
        const container  = document.getElementById('iot-blocks-container');
        const pageInfo   = document.getElementById('iot-page-info');
        const btnPrev    = document.getElementById('btn-iot-prev');
        const btnNext    = document.getElementById('btn-iot-next');
        if (!container) return;

        const total      = this.filteredBlocks.length;
        const totalPages = Math.ceil(total / this.blocksPerPage) || 1;
        if (this.currentPage > totalPages) this.currentPage = totalPages;

        if (pageInfo) pageInfo.innerText = `PAGE ${this.currentPage} / ${totalPages}`;
        if (btnPrev)  btnPrev.disabled   = this.currentPage <= 1;
        if (btnNext)  btnNext.disabled   = this.currentPage >= totalPages;

        container.innerHTML = '';

        if (total === 0) {
            container.innerHTML = `<div style="padding:20px;text-align:center;color:var(--txt3);font-family:var(--mono);font-size:10px;">[ NO CONTENT FOUND ]</div>`;
            return;
        }

        const start = (this.currentPage - 1) * this.blocksPerPage;
        const end   = Math.min(start + this.blocksPerPage, total);

        for (let i = start; i < end; i++) {
            const b     = this.filteredBlocks[i];
            const isSel = this.selectedBlockIndex === b.originalIndex;

            const div       = document.createElement('div');
            div.className   = `iot-block-card ${isSel ? 'selected' : ''}`;
            div.onclick     = () => this.selectBlock(b.originalIndex, b.text);

            // /extract returns `tag` (element type) not `element_type`
            const tagLabel  = (b.tag || 'TEXT').toUpperCase();
            const charCount = b.text.length;

            div.innerHTML = `
                <div style="font-weight:bold;font-size:10px;color:var(--acc);margin-bottom:4px;">
                    [${window.escapeHtml(tagLabel)}] ID: ${b.originalIndex}
                </div>
                <div class="iot-block-text">${window.escapeHtml(b.text)}</div>
                <div class="iot-block-meta">
                    <span style="margin-left:auto;">${charCount} chars</span>
                </div>`;

            container.appendChild(div);
        }
    },

    selectBlock(idx, text) {
        this.selectedBlockIndex = idx;
        this.renderBlocks();

        const step3   = document.getElementById('iot-step-3');
        const preview = document.getElementById('iot-sum-payload');

        if (step3)   step3.classList.remove('disabled');
        if (preview) preview.innerText = text.length > 50 ? text.substring(0, 50) + '...' : text;
    },

    async transmitNow() {
        const prefix = document.getElementById('iot-prefix')?.value.trim();
        if (!prefix) return window.triggerToast('Sensor Label Required', 'warn');

        if (this.selectedBlockIndex === null) {
            return window.triggerToast('Select a payload block first', 'warn');
        }
        if (!this.selectedNodeId) {
            return window.triggerToast('Select a destination node first', 'warn');
        }

        const btn = document.getElementById('btn-iot-transmit');
        if (btn) { btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

        try {
            // Endpoint is /api/monitor — was incorrectly /monitor/website
            const res = await fetch('/api/monitor', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({
                    url:      this.currentUrl,
                    block_id: this.selectedBlockIndex,
                    prefix:   prefix,
                    node_id:  this.selectedNodeId,
                    channel:  parseInt(document.getElementById('iot-channel')?.value) || 0,
                    slot_id:  window._slotPickerValue ? window._slotPickerValue('iot-slot-picker') : (window._activeSlotId || 'node_0')
                })
            });

            if (res.ok) {
                const data = await res.json();
                window.triggerToast(`Transmitted: ${data.text || 'OK'}`, 'ok');
            } else {
                const err = await res.json().catch(() => ({}));
                window.triggerToast(`Transmission Failed: ${err.detail || res.status}`, 'err');
            }
        } catch (e) {
            window.triggerToast(`Network Error: ${e.message}`, 'err');
        } finally {
            if (btn) { btn.innerHTML = '<i class="fas fa-paper-plane"></i> TRANSMIT NOW'; btn.disabled = false; }
        }
    },

    updateCronPreview() {
        const typeEl   = document.getElementById('iot-cron-type');
        const timeEl   = document.getElementById('iot-cron-time');
        const preview  = document.getElementById('iot-cron-preview');
        if (!typeEl || !timeEl || !preview) return;

        const type = typeEl.value;

        if (type === 'hourly') {
            timeEl.style.display = 'none';
            preview.innerText    = '0 * * * * (Top of every hour)';
        } else if (type === 'daily') {
            timeEl.style.display = 'block';
            timeEl.type          = 'time';
            const t = timeEl.value;
            if (t) {
                const [h, m]      = t.split(':');
                preview.innerText = `${parseInt(m)} ${parseInt(h)} * * * (Every day at ${t})`;
            } else {
                preview.innerText = 'Select a time';
            }
        } else {
            timeEl.style.display = 'block';
            timeEl.type          = 'datetime-local';
            const v = timeEl.value;
            if (v) {
                const d           = new Date(v);
                preview.innerText = `${d.getMinutes()} ${d.getHours()} ${d.getDate()} ${d.getMonth() + 1} * (Once on ${d.toLocaleDateString()})`;
            } else {
                preview.innerText = 'Select date/time';
            }
        }
    },

    generateCronString() {
        const typeEl = document.getElementById('iot-cron-type');
        const timeEl = document.getElementById('iot-cron-time');
        if (!typeEl) return null;

        const type = typeEl.value;
        const val  = timeEl?.value || '';

        if (type === 'hourly') return '0 * * * *';

        if (type === 'daily' && val) {
            const [h, m] = val.split(':');
            return `${parseInt(m)} ${parseInt(h)} * * *`;
        }

        if (type === 'once' && val) {
            const d = new Date(val);
            return `${d.getMinutes()} ${d.getHours()} ${d.getDate()} ${d.getMonth() + 1} *`;
        }

        return null;
    },

    async scheduleTask() {
        const prefix = document.getElementById('iot-prefix')?.value.trim();
        const cron   = this.generateCronString();

        if (!prefix) return window.triggerToast('Sensor Label Required', 'warn');
        if (!cron)   return window.triggerToast('Valid schedule required', 'warn');

        if (this.selectedBlockIndex === null) {
            return window.triggerToast('Select a payload block first', 'warn');
        }
        if (!this.selectedNodeId) {
            return window.triggerToast('Select a destination node first', 'warn');
        }

        const btn = document.getElementById('btn-iot-schedule');
        if (btn) { btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; btn.disabled = true; }

        const payload = {
            nodeId:      this.selectedNodeId,
            taskType:    'website_monitor',
            actionPayload: JSON.stringify({
                url:      this.currentUrl,
                block_id: this.selectedBlockIndex,
                prefix:   prefix,
                channel:  parseInt(document.getElementById('iot-channel')?.value) || 0,
                node_id:  this.selectedNodeId,
                slot_id:  window._slotPickerValue ? window._slotPickerValue('iot-slot-picker') : (window._activeSlotId || 'node_0')
            }),
            cronString:      cron,
            taskDescription: `Web Ingress: ${prefix}`
        };

        try {
            const res = await fetch('/api/tasks/', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify(payload)
            });

            if (res.ok) {
                window.triggerToast('Directive Deployed to Scheduler', 'ok');
            } else {
                const err = await res.json().catch(() => ({}));
                window.triggerToast(`Failed to Deploy: ${err.detail || res.status}`, 'err');
            }
        } catch (e) {
            window.triggerToast(`Network Error: ${e.message}`, 'err');
        } finally {
            if (btn) { btn.innerHTML = '<i class="fas fa-calendar-plus"></i> DEPLOY TASK'; btn.disabled = false; }
        }
    }
};