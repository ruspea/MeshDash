/* ==========================================================================
 * MeshDash — Plugin Registry Module  (Enhanced v2)
 * ========================================================================== */

window.C2PluginsApp = {
    registry:     new Map(),
    activeFilter: 'all',
    syncTimer:    null,
    logTimer:     null,
    activeLogId:  null,
    _detailId:    null,

    // ── Helpers ─────────────────────────────────────────────────────────────

    _isVerified(p) {
        return (p.author || '').toLowerCase() === 'meshdash';
    },

    /** Returns stripe CSS colour and chip label/classes for a given plugin */
    _statusMeta(p) {
        if (!p.installed) {
            return { color: 'var(--acc)', chip: 'AVAILABLE', chipBg: 'bg-acc', chipTxt: 'sc-acc', cardCls: 'pl-remote' };
        }
        const map = {
            running:          { color: 'var(--ok)',            chip: 'RUNNING',         chipBg: 'bg-run',  chipTxt: 'sc-run',  cardCls: 'pl-running' },
            stopped:          { color: 'var(--warn)',          chip: 'STOPPED',         chipBg: 'bg-stop', chipTxt: 'sc-stop', cardCls: 'pl-stopped' },
            crashed:          { color: 'var(--err)',           chip: 'CRASHED',         chipBg: 'bg-err',  chipTxt: 'sc-err',  cardCls: 'pl-crashed' },
            hung:             { color: 'var(--err)',           chip: 'HUNG',            chipBg: 'bg-err',  chipTxt: 'sc-err',  cardCls: 'pl-hung'    },
            invalid_manifest: { color: 'var(--err)',           chip: 'INVALID',         chipBg: 'bg-err',  chipTxt: 'sc-err',  cardCls: 'pl-crashed' },
            pending_restart:  { color: 'var(--pur,#b060ff)',   chip: 'PENDING RESTART', chipBg: 'bg-pur',  chipTxt: 'sc-pur',  cardCls: 'pl-pending' },
            loading:          { color: 'var(--acc)',           chip: 'LOADING',         chipBg: 'bg-acc',  chipTxt: 'sc-acc',  cardCls: ''           },
        };
        return map[p.status] || { color: 'var(--bd)', chip: (p.status || 'UNKNOWN').toUpperCase(), chipBg: '', chipTxt: '', cardCls: '' };
    },

    _fmtPing(ts) {
        if (!ts) return null;
        const ago = Math.floor(Date.now() / 1000 - ts);
        if (ago < 60) return `${ago}s ago`;
        if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
        return `${Math.floor(ago / 3600)}h ago`;
    },

    _esc(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },

    // ── Initialise ──────────────────────────────────────────────────────────

    init() {
        this.syncRegistry();
        this.syncTimer = setInterval(() => this.softSync(), 30000);

        // Listen for real-time plugin_update SSE events from the dashboard
        window.addEventListener('plugin_update_sse', (e) => this.softSync());

        // Drag-and-drop on upload zone
        const dz = document.getElementById('pl-drop-zone');
        if (dz) {
            dz.ondragover  = e => { e.preventDefault(); dz.style.borderColor = 'var(--acc)'; };
            dz.ondragleave = e => { e.preventDefault(); dz.style.borderColor = 'var(--bd)';  };
            dz.ondrop      = e => {
                e.preventDefault();
                dz.style.borderColor = 'var(--bd)';
                if (e.dataTransfer?.files?.[0]) this.handleFile(e.dataTransfer.files[0]);
            };
        }

        // Close detail modal when clicking the backdrop
        const dm = document.getElementById('pl-modal-detail');
        if (dm) dm.addEventListener('click', e => { if (e.target === dm) this.closeDetail(); });
    },

    // ── Data sync ───────────────────────────────────────────────────────────

    async syncRegistry(silent = false) {
        if (!silent) {
            document.getElementById('pl-grid').innerHTML =
                `<div style="grid-column:1/-1;text-align:center;color:var(--txt3);padding:48px;font-family:var(--mono);">
                    <i class="fas fa-circle-notch fa-spin"></i>&nbsp; QUERYING REGISTRY…
                </div>`;
        }
        this.registry.clear();

        // 1. Installed plugins
        try {
            const r = await fetch('/api/system/plugins');
            if (r.ok) {
                const d = await r.json();
                Object.entries(d.plugins || {}).forEach(([id, pData]) => {
                    const m = pData.manifest || {};
                    this.registry.set(id, {
                        id,
                        name:          m.name || id,
                        version:       m.version || '1.0.0',
                        description:   m.description || 'No description provided.',
                        author:        m.author || 'Unknown',
                        icon:          m.nav_menu?.[0]?.icon || 'fa-puzzle-piece',
                        installed:     true,
                        status:        pData.status || 'stopped',
                        errorMsg:      pData.error || null,
                        watchdog:      !!pData.watchdog_monitored,
                        lastPing:      pData.last_watchdog_ping || null,
                        updateAvail:   false,
                        routerPrefix:  m.router_prefix || '',
                        staticPrefix:  m.static_prefix || '',
                        bridge:        m.bridge || null,
                        permissions:   m.permissions || [],
                        navMenu:       m.nav_menu || [],
                        entryPoint:    m.entry_point || 'main.py',
                        path:          pData.path || '',
                        watchdogFlag:  m.watchdog === true,
                    });
                });
            }
        } catch (e) { console.warn('Plugin list fetch failed:', e); }

        // 2. Remote available plugins
        try {
            const r = await fetch('/api/plugins/available');
            if (r.ok) {
                const remotes = await r.json();
                remotes.forEach(rp => {
                    if (this.registry.has(rp.id)) {
                        const ex = this.registry.get(rp.id);
                        const vR = String(rp.version || '').replace(/^v/i, '').split('.').map(n => parseInt(n) || 0);
                        const vL = String(ex.version || '').replace(/^v/i, '').split('.').map(n => parseInt(n) || 0);
                        let isUpdate = false;
                        for (let i = 0; i < Math.max(vR.length, vL.length); i++) {
                            const rNum = vR[i] || 0;
                            const lNum = vL[i] || 0;
                            if (rNum > lNum) { isUpdate = true; break; }
                            if (rNum < lNum) { break; }
                        }
                        if (isUpdate && ex.status !== 'pending_restart') ex.updateAvail = true;
                        ex.downloadUrl    = rp.download_url;
                        ex.remoteVersion  = rp.version;
                    } else {
                        this.registry.set(rp.id, {
                            id:           rp.id,
                            name:         rp.name || rp.id,
                            version:      rp.version || '1.0.0',
                            description:  rp.description || 'No description.',
                            author:       rp.author || 'Unknown',
                            icon:         rp.nav_menu?.[0]?.icon || 'fa-cloud-download-alt',
                            installed:    false,
                            status:       'uninstalled',
                            downloadUrl:  rp.download_url,
                            watchdog:     false,
                            permissions:  rp.permissions || [],
                            navMenu:      rp.nav_menu || [],
                            entryPoint:   rp.entry_point || 'main.py',
                            watchdogFlag: rp.watchdog === true,
                        });
                    }
                });
            }
        } catch (e) { console.warn('Remote plugins fetch failed:', e); }

        this.updateHeader();
        this.renderGrid();
    },

    async softSync() {
        try {
            const r = await fetch('/api/system/plugins');
            if (!r.ok) return;
            const d = await r.json();
            let changed = false;
            Object.entries(d.plugins || {}).forEach(([id, pData]) => {
                if (!this.registry.has(id)) return;
                const p     = this.registry.get(id);
                const nStat = pData.status || 'stopped';
                if (p.status !== nStat || p.lastPing !== pData.last_watchdog_ping) {
                    p.status   = nStat;
                    p.errorMsg = pData.error;
                    p.watchdog = !!pData.watchdog_monitored;
                    p.lastPing = pData.last_watchdog_ping || null;
                    changed    = true;
                }
            });
            if (changed) { this.updateHeader(); this.renderGrid(); }
        } catch (e) {}

        const d  = new Date();
        const el = document.getElementById('pl-last-sync');
        if (el) el.innerText = `SYNCED ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
    },

    // ── Summary bar ─────────────────────────────────────────────────────────

    updateHeader() {
        const c = { all:0, running:0, stopped:0, crashed:0, avail:0, update:0 };
        this.registry.forEach(p => {
            c.all++;
            if (p.status === 'running')                                              c.running++;
            if (p.status === 'stopped')                                              c.stopped++;
            if (['crashed','hung','invalid_manifest'].includes(p.status))            c.crashed++;
            if (!p.installed)                                                        c.avail++;
            if (p.updateAvail)                                                       c.update++;
        });

        const bar = document.getElementById('pl-summary-bar');
        if (!bar) return;

        const btn = (k, lbl, val, col) =>
            `<div class="pl-sum-box ${this.activeFilter === k ? 'active' : ''}"
                  style="color:${col}"
                  onclick="window.C2PluginsApp.setFilter('${k}')">
                <div class="pl-sum-val">${val}</div>
                <div class="pl-sum-lbl">${lbl}</div>
             </div>`;

        bar.innerHTML =
            btn('all',     'ALL',      c.all,     'var(--txt)')  +
            btn('running', 'RUNNING',  c.running, 'var(--ok)')   +
            btn('stopped', 'STOPPED',  c.stopped, 'var(--warn)') +
            btn('crashed', 'ERRORS',   c.crashed, 'var(--err)')  +
            btn('avail',   'AVAILABLE',c.avail,   'var(--acc)')  +
            (c.update > 0 ? btn('update', 'UPDATES', c.update, 'var(--pur,#b060ff)') : '');
    },

    setFilter(k) {
        this.activeFilter = k;
        this.updateHeader();
        this.renderGrid();
    },

    // ── Grid render ─────────────────────────────────────────────────────────

    renderGrid() {
        const grid       = document.getElementById('pl-grid');
        const search     = (document.getElementById('pl-search')?.value || '').toLowerCase();
        const sort       = document.getElementById('pl-sort')?.value || 'name';
        const showRemote = document.getElementById('pl-show-remote')?.checked || false;

        let items = Array.from(this.registry.values()).filter(p => {
            if (search &&
                !p.name.toLowerCase().includes(search) &&
                !p.id.toLowerCase().includes(search) &&
                !(p.author || '').toLowerCase().includes(search)) return false;

            if (this.activeFilter === 'running') return p.status === 'running';
            if (this.activeFilter === 'stopped') return p.status === 'stopped';
            if (this.activeFilter === 'crashed') return ['crashed','hung','invalid_manifest'].includes(p.status);
            if (this.activeFilter === 'avail')   return !p.installed;
            if (this.activeFilter === 'update')  return p.updateAvail;

            // In "all" view, hide remote unless toggled
            if (!p.installed && !showRemote && this.activeFilter === 'all') return false;
            return true;
        });

        items.sort((a, b) => {
            if (sort === 'status') return a.status.localeCompare(b.status);
            if (sort === 'author') return (a.author || '').localeCompare(b.author || '');
            return a.name.localeCompare(b.name);
        });

        if (items.length === 0) {
            grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:var(--txt3);padding:48px;font-family:var(--mono);">
                [ NO PLUGINS MATCH CRITERIA ]
            </div>`;
            return;
        }

        const verified  = items.filter(p =>  p.installed && this._isVerified(p));
        const community = items.filter(p =>  p.installed && !this._isVerified(p));
        const remote    = items.filter(p => !p.installed);

        let html = '';

        const sectionHd = (cls, icon, label, count) =>
            `<div class="pl-section-hd ${cls}" style="${html ? 'margin-top:4px;' : ''}">
                <span class="pl-sec-badge"><i class="fas ${icon}"></i> ${label}</span>
                <span class="pl-sec-line"></span>
                <span class="pl-sec-count">${count} plugin${count !== 1 ? 's' : ''}</span>
             </div>`;

        if (verified.length) {
            html += sectionHd('verified',  'fa-shield-check', 'VERIFIED PLUGINS',   verified.length);
            verified.forEach(p => { html += this._buildCard(p); });
        }
        if (community.length) {
            html += sectionHd('community', 'fa-users',        'COMMUNITY PLUGINS',  community.length);
            community.forEach(p => { html += this._buildCard(p); });
        }
        if (remote.length) {
            html += sectionHd('remote',    'fa-cloud',        'AVAILABLE TO INSTALL', remote.length);
            remote.forEach(p => { html += this._buildCard(p); });
        }

        grid.innerHTML = html;
    },

    _buildCard(p) {
        const sm  = this._statusMeta(p);
        const ver = this._isVerified(p);
        const e   = s => this._esc(s);
        const ping = this._fmtPing(p.lastPing);

        // Trust badge
        const trustBadge = ver
            ? `<span class="pl-trust verified"><i class="fas fa-shield-check"></i>VERIFIED</span>`
            : (p.installed ? `<span class="pl-trust community"><i class="fas fa-users"></i>COMMUNITY</span>` : '');

        // Update pill
        const updPill = p.updateAvail
            ? `<span class="pl-update-pill" title="Update: v${e(p.remoteVersion || '?')} available"><i class="fas fa-arrow-up"></i>UPDATE</span>`
            : '';

        // Status chip
        const chip = `<span class="pl-status-chip ${sm.chipBg} ${sm.chipTxt}">${sm.chip}</span>`;

        // Quick meta — always visible, wraps cleanly
        const wdItem = p.watchdog
            ? `<span class="pl-qmi sc-run"><i class="fas fa-dog"></i><span class="pl-qmv sc-run">${ping ? 'WD ' + ping : 'MONITORED'}</span></span>`
            : `<span class="pl-qmi"><i class="fas fa-ban" style="opacity:.35"></i><span class="pl-qmv">NO WATCHDOG</span></span>`;

        const permItem = p.permissions?.length
            ? `<span class="pl-qmi"><i class="fas fa-key"></i><span class="pl-qmv">${p.permissions.length} PERM${p.permissions.length !== 1 ? 'S' : ''}</span></span>`
            : '';

        const meta = `
            <div class="pl-quick-meta">
                <span class="pl-qmi"><i class="fas fa-user-pen"></i><span class="pl-qmv">${e(p.author)}</span></span>
                <span class="pl-qmi"><i class="fas fa-tag"></i><span class="pl-qmv">v${e(p.version)}</span></span>
                ${wdItem}
                ${permItem}
            </div>`;

        // Error strip
        const errStrip = p.errorMsg
            ? `<div class="pl-alert-strip"><i class="fas fa-exclamation-triangle"></i>&nbsp;${e(p.errorMsg)}</div>`
            : '';

        // Footer buttons — stopPropagation prevents card click opening detail
        let footBtns = '';
        if (p.installed) {
            if (p.status === 'running') {
                footBtns += `<button class="btn btn-sm btn-warn" onclick="event.stopPropagation();window.C2PluginsApp.act('${this._esc(p.id)}','stop')"><i class="fas fa-stop"></i> STOP</button>`;
            } else if (!['pending_restart','invalid_manifest'].includes(p.status)) {
                footBtns += `<button class="btn btn-sm btn-ok" onclick="event.stopPropagation();window.C2PluginsApp.act('${this._esc(p.id)}','start')"><i class="fas fa-play"></i> START</button>`;
            }
            if (p.updateAvail && p.downloadUrl) {
                footBtns += `<button class="btn btn-sm" style="background:rgba(176,96,255,.15);color:var(--pur,#b060ff);border-color:rgba(176,96,255,.4);" title="Update to v${this._esc(p.remoteVersion || '?')}" onclick="event.stopPropagation();window.C2PluginsApp.triggerUpdate('${this._esc(p.id)}')"><i class="fas fa-arrow-up"></i> UPDATE</button>`;
            }
            footBtns += `<button class="btn btn-sm" title="Logs" onclick="event.stopPropagation();window.C2PluginsApp.openLogs('${this._esc(p.id)}','${this._esc(p.name)}')"><i class="fas fa-terminal"></i></button>`;
            footBtns += `<button class="btn btn-sm btn-err" title="Uninstall" onclick="event.stopPropagation();window.C2PluginsApp.act('${this._esc(p.id)}','delete')"><i class="fas fa-trash"></i></button>`;
        } else {
            footBtns += `<button class="btn btn-sm btn-acc" onclick="event.stopPropagation();window.C2PluginsApp.installUrl('${this._esc(p.downloadUrl || '')}')"><i class="fas fa-cloud-download-alt"></i> INSTALL</button>`;
        }

        return `
        <div class="pl-card ${sm.cardCls}" onclick="window.C2PluginsApp.openDetail('${this._esc(p.id)}')">
            <div class="pl-head">
                <div class="pl-icon"><i class="fas ${e(p.icon)}"></i></div>
                <div class="pl-head-meta">
                    <div class="pl-title-row">
                        <span class="pl-name">${e(p.name)}</span>
                        ${trustBadge}
                        ${updPill}
                    </div>
                    <div class="pl-plugin-id">${e(p.id)}</div>
                </div>
                ${chip}
            </div>
            <div class="pl-desc">${e(p.description)}</div>
            ${meta}
            ${errStrip}
            <div class="pl-foot">
                ${footBtns}
                <span class="pl-expand-hint"><i class="fas fa-expand-alt"></i>&nbsp;DETAILS</span>
            </div>
        </div>`;
    },

    // ── Detail modal ────────────────────────────────────────────────────────

    openDetail(id) {
        const p = this.registry.get(id);
        if (!p) return;
        this._detailId = id;

        const sm   = this._statusMeta(p);
        const ver  = this._isVerified(p);
        const e    = s => this._esc(s);
        const ping = this._fmtPing(p.lastPing);

        // Header stripe colour via border-top
        const head = document.getElementById('pldm-head');
        if (head) head.style.borderTopColor = sm.color;

        document.getElementById('pldm-icon').className = `fas ${p.icon}`;

        // Badges in title
        const trustBadge = ver
            ? `<span class="pl-trust verified"><i class="fas fa-shield-check"></i>VERIFIED</span>`
            : (p.installed ? `<span class="pl-trust community"><i class="fas fa-users"></i>COMMUNITY</span>` : '');
        const updPill = p.updateAvail
            ? `<span class="pl-update-pill"><i class="fas fa-arrow-up"></i>UPDATE AVAILABLE</span>`
            : '';

        document.getElementById('pldm-title').innerHTML = `${e(p.name)}&nbsp;${trustBadge}${updPill}`;
        document.getElementById('pldm-sub').innerHTML =
            `<span style="opacity:.6">ID:</span> ${e(p.id)}&nbsp;&nbsp;·&nbsp;&nbsp;<span style="opacity:.6">BY</span> ${e(p.author)}`;

        // ── Body sections ──────────────────────────────────────────────────

        const kv = (lbl, val) =>
            `<div class="pl-dm-kv">
                <div class="pl-dm-kv-label">${lbl}</div>
                <div class="pl-dm-kv-val">${val}</div>
             </div>`;

        let body = '';

        // Status
        const dotAnim = (p.status === 'hung') ? 'pl-anim-pulse' : '';
        body += `<div class="pl-dm-sec">
            <div class="pl-dm-sec-title"><i class="fas fa-circle-dot"></i> STATUS</div>
            <div class="pl-dm-status-row">
                <div class="pl-dm-status-dot ${dotAnim}" style="background:${sm.color};${dotAnim?'box-shadow:0 0 6px '+sm.color:''}"></div>
                <span class="pl-dm-status-label ${sm.chipTxt}">${sm.chip}</span>
                ${ping ? `<span class="pl-dm-status-hint">Last WD ping: ${e(ping)}</span>` : ''}
            </div>
            ${p.errorMsg ? `<div class="pl-dm-err-text"><i class="fas fa-exclamation-triangle"></i>&nbsp;${e(p.errorMsg)}</div>` : ''}
        </div>`;

        // Description
        body += `<div class="pl-dm-sec">
            <div class="pl-dm-sec-title"><i class="fas fa-info-circle"></i> DESCRIPTION</div>
            <div class="pl-dm-desc-text">${e(p.description)}</div>
        </div>`;

        // Plugin details grid
        const versionVal = p.updateAvail
            ? `v${e(p.version)} <span style="color:var(--pur,#b060ff);font-size:9px;"> → v${e(p.remoteVersion || '?')} available</span>`
            : `v${e(p.version)}`;

        body += `<div class="pl-dm-sec">
            <div class="pl-dm-sec-title"><i class="fas fa-sliders"></i> PLUGIN DETAILS</div>
            <div class="pl-dm-kv-grid">
                ${kv('AUTHOR',      e(p.author))}
                ${kv('VERSION',     versionVal)}
                ${kv('ENTRY POINT', e(p.entryPoint || 'main.py'))}
                ${kv('WATCHDOG',    p.watchdogFlag
                    ? `<span style="color:var(--ok)"><i class="fas fa-dog"></i> MONITORED</span>`
                    : `<span style="color:var(--txt3)">DISABLED</span>`)}
                ${kv('BRIDGE',      p.bridge
                    ? `<span style="color:var(--acc)">${e(p.bridge)}</span>`
                    : `<span style="color:var(--txt3)">—</span>`)}
                ${kv('INSTALLED',   p.installed
                    ? `<span style="color:var(--ok)"><i class="fas fa-check-circle"></i> YES</span>`
                    : `<span style="color:var(--txt3)">NOT INSTALLED</span>`)}
            </div>
        </div>`;

        // API paths (installed only)
        if (p.installed && (p.routerPrefix || p.staticPrefix || p.path)) {
            body += `<div class="pl-dm-sec">
                <div class="pl-dm-sec-title"><i class="fas fa-code"></i> API &amp; PATHS</div>
                <div class="pl-dm-kv-grid">
                    ${p.routerPrefix ? kv('ROUTER PREFIX', `<span style="color:var(--acc)">${e(p.routerPrefix)}</span>`) : ''}
                    ${p.staticPrefix ? kv('STATIC PREFIX', `<span style="color:var(--txt2)">${e(p.staticPrefix)}</span>`) : ''}
                    ${p.path ? kv('DISK PATH', `<span style="color:var(--txt3);font-size:9px;">${e(p.path)}</span>`) : ''}
                </div>
            </div>`;
        }

        // Permissions
        if (p.permissions?.length) {
            body += `<div class="pl-dm-sec">
                <div class="pl-dm-sec-title"><i class="fas fa-key"></i> PERMISSIONS (${p.permissions.length})</div>
                <div class="pl-dm-perms-wrap">
                    ${p.permissions.map(pr => `<span class="pl-dm-perm">${e(pr)}</span>`).join('')}
                </div>
            </div>`;
        }

        // Nav menu
        if (p.navMenu?.length) {
            body += `<div class="pl-dm-sec">
                <div class="pl-dm-sec-title"><i class="fas fa-bars"></i> NAV MENU</div>
                <div class="pl-dm-nav-items">
                    ${p.navMenu.map(n => `
                        <div class="pl-dm-nav-row">
                            <i class="fas ${e(n.icon || 'fa-link')}"></i>
                            <span class="pl-dm-nav-label">${e(n.label || '—')}</span>
                            ${p.installed ? `<a href="${e(n.href || '#')}" target="_blank" onclick="event.stopPropagation()"><i class="fas fa-external-link-alt"></i>&nbsp;OPEN</a>` : ''}
                        </div>`).join('')}
                </div>
            </div>`;
        }

        // Trust notice
        if (ver) {
            body += `<div class="pl-dm-notice ok">
                <i class="fas fa-shield-check"></i>
                This plugin is developed and maintained by MeshDash. It has been reviewed and is considered safe.
            </div>`;
        } else if (p.installed) {
            body += `<div class="pl-dm-notice warn">
                <i class="fas fa-triangle-exclamation"></i>
                Community plugin — only install from trusted sources. Third-party plugins run with full server access.
            </div>`;
        }

        document.getElementById('pldm-body').innerHTML = body;

        // ── Footer actions ─────────────────────────────────────────────────

        let foot = '';
        if (p.installed) {
            if (p.status === 'running') {
                foot += `<button class="btn btn-sm btn-warn" onclick="window.C2PluginsApp.closeDetail();window.C2PluginsApp.act('${this._esc(p.id)}','stop')"><i class="fas fa-stop"></i> STOP</button>`;
            } else if (!['pending_restart','invalid_manifest'].includes(p.status)) {
                foot += `<button class="btn btn-sm btn-ok" onclick="window.C2PluginsApp.closeDetail();window.C2PluginsApp.act('${this._esc(p.id)}','start')"><i class="fas fa-play"></i> START</button>`;
            }
            foot += `<button class="btn btn-sm" onclick="window.C2PluginsApp.closeDetail();window.C2PluginsApp.openLogs('${this._esc(p.id)}','${e(p.name)}')"><i class="fas fa-terminal"></i> LOGS</button>`;
            if (p.updateAvail && p.downloadUrl) {
                foot += `<button class="btn btn-sm" style="background:rgba(176,96,255,.12);color:var(--pur,#b060ff);border-color:rgba(176,96,255,.3);"
                         onclick="window.C2PluginsApp.closeDetail();window.C2PluginsApp.triggerUpdate('${this._esc(p.id)}')">
                         <i class="fas fa-arrow-up"></i> UPDATE</button>`;
            }
            foot += `<span style="flex:1"></span>`;
            foot += `<button class="btn btn-sm btn-err" onclick="window.C2PluginsApp.closeDetail();window.C2PluginsApp.act('${this._esc(p.id)}','delete')"><i class="fas fa-trash"></i> UNINSTALL</button>`;
        } else if (p.downloadUrl) {
            foot += `<button class="btn btn-acc" onclick="window.C2PluginsApp.closeDetail();window.C2PluginsApp.installUrl('${e(p.downloadUrl)}')">
                <i class="fas fa-cloud-download-alt"></i> INSTALL PLUGIN
            </button>`;
        } else {
            foot += `<span style="font-family:var(--mono);font-size:9px;color:var(--txt3);">No actions available.</span>`;
        }
        document.getElementById('pldm-foot').innerHTML = foot;

        // Show
        document.getElementById('pl-modal-detail').style.display = 'flex';
    },

    closeDetail() {
        document.getElementById('pl-modal-detail').style.display = 'none';
        this._detailId = null;
    },

    // ── Actions ─────────────────────────────────────────────────────────────

    async act(id, action) {
        if (action === 'delete') {
            const p = this.registry.get(id);
            if (!confirm(`Permanently uninstall "${p ? p.name : id}"?\n\nThis will delete all plugin files. A restart is required to fully remove it.`)) return;
            try {
                const r = await fetch(`/api/system/plugins/${id}`, { method: 'DELETE' });
                if (!r.ok) throw new Error('Failed');
                window.triggerToast('Plugin Uninstalled', 'ok');
                this.syncRegistry(true);
                document.getElementById('pl-btn-restart').style.display = 'inline-flex';
            } catch (e) { window.triggerToast('Uninstall Failed', 'err'); }
            return;
        }
        try {
            const r = await fetch(`/api/system/plugins/${id}/toggle?action=${action}`, { method: 'POST' });
            if (!r.ok) throw new Error('Failed');
            const d = await r.json();
            window.triggerToast(`Signal sent: ${action.toUpperCase()}`, 'ok');
            if (d.requires_restart) document.getElementById('pl-btn-restart').style.display = 'inline-flex';
            this.syncRegistry(true);
        } catch (e) { window.triggerToast('Action Failed', 'err'); }
    },

    // ── Install modal ────────────────────────────────────────────────────────

    openInstallModal() {
        const modal = document.getElementById('pl-modal-install');
        if (!modal) return;
        // Reset to ZIP tab
        modal.querySelectorAll('.pl-tab').forEach(b => b.classList.remove('active'));
        modal.querySelectorAll('.pl-tab-body').forEach(b => b.classList.remove('active'));
        const zipTabBtn = modal.querySelector('.pl-tab');
        if (zipTabBtn) zipTabBtn.classList.add('active');
        const zipTab = document.getElementById('pl-tab-zip');
        if (zipTab) zipTab.classList.add('active');
        // Clear both logs
        const logZip = document.getElementById('pl-log-zip');
        const logUrl = document.getElementById('pl-log-url');
        if (logZip) { logZip.style.display = 'none'; logZip.innerHTML = ''; }
        if (logUrl) { logUrl.style.display = 'none'; logUrl.innerHTML = ''; }
        modal.style.display = 'flex';
    },

    closeInstallModal() {
        const modal = document.getElementById('pl-modal-install');
        if (modal) modal.style.display = 'none';
        const logZip = document.getElementById('pl-log-zip');
        const logUrl = document.getElementById('pl-log-url');
        if (logZip) { logZip.style.display = 'none'; logZip.innerHTML = ''; }
        if (logUrl) { logUrl.style.display = 'none'; logUrl.innerHTML = ''; }
    },

    switchTab(t, btn) {
        const modal = document.getElementById('pl-modal-install');
        if (modal) {
            modal.querySelectorAll('.pl-tab').forEach(b => b.classList.remove('active'));
            modal.querySelectorAll('.pl-tab-body').forEach(b => b.classList.remove('active'));
        }
        btn.classList.add('active');
        const tab = document.getElementById(`pl-tab-${t}`);
        if (tab) tab.classList.add('active');
    },

    async handleFile(file) {
        if (!file) return;
        const log = document.getElementById('pl-log-zip');
        log.style.display = 'block';
        log.innerHTML = `<div style="color:var(--txt3);">[UPLOADING] ${this._esc(file.name)}…</div>`;
        const fd = new FormData();
        fd.append('file', file);
        try {
            const r = await fetch('/api/system/plugins/install', { method: 'POST', body: fd });
            const d = await r.json();
            if (!r.ok) throw new Error(d.detail || 'Install failed');
            log.innerHTML += `<div style="color:var(--ok);">[SUCCESS] ${this._esc(d.message || 'Installed.')}</div>`;
            window.triggerToast('Plugin Installed — Restart Required', 'ok');
            document.getElementById('pl-btn-restart').style.display = 'inline-flex';
            setTimeout(() => { this.closeInstallModal(); this.syncRegistry(true); }, 2000);
        } catch (e) {
            log.innerHTML += `<div style="color:var(--err);">[ERROR] ${this._esc(e.message)}</div>`;
            window.triggerToast('Install Failed — Check Log', 'err');
        }
    },

    async installRemote() { this.installUrl(document.getElementById('pl-url-input').value); },

    // Called from both card and detail modal UPDATE buttons.
    // Opens the install modal so the user sees progress/errors.
    triggerUpdate(id) {
        const p = this.registry.get(id);
        if (!p || !p.downloadUrl) { window.triggerToast('No download URL for this plugin', 'err'); return; }
        const modal = document.getElementById('pl-modal-install');
        if (modal) {
            modal.style.display = 'flex';
            modal.querySelectorAll('.pl-tab').forEach(b => b.classList.remove('active'));
            modal.querySelectorAll('.pl-tab-body').forEach(b => b.classList.remove('active'));
            const urlTab = document.getElementById('pl-tab-url');
            if (urlTab) urlTab.classList.add('active');
            // Activate the URL tab button by finding the one that switches to 'url'
            modal.querySelectorAll('.pl-tab').forEach(b => {
                if ((b.getAttribute('onclick') || '').includes("'url'")) b.classList.add('active');
            });
        }
        const log = document.getElementById('pl-log-url');
        if (log) {
            log.style.display = 'block';
            log.innerHTML = `<div style="color:var(--acc);">[UPDATE] Downloading ${this._esc(p.name)} v${this._esc(p.remoteVersion || '?')}…</div>`;
        }
        this.installUrl(p.downloadUrl);
    },

    async installUrl(url) {
        if (!url) return;
        const log = document.getElementById('pl-log-url');
        if (log) {
            log.style.display = 'block';
            // Only set initial fetching message if log is empty (triggerUpdate may have pre-seeded it)
            if (!log.innerHTML.trim()) {
                log.innerHTML = `<div style="color:var(--txt3);">[FETCHING] ${this._esc(url)}…</div>`;
            }
        }
        try {
            const r = await fetch('/api/system/plugins/install-remote', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            const d = await r.json();
            if (!r.ok) throw new Error(d.detail || 'Install failed');
            if (log) log.innerHTML += `<div style="color:var(--ok);">[SUCCESS] ${this._esc(d.message || 'Installed.')}</div>`;
            window.triggerToast('Plugin Installed — Restart Required', 'ok');
            document.getElementById('pl-btn-restart').style.display = 'inline-flex';
            setTimeout(() => { this.closeInstallModal(); this.syncRegistry(true); }, 2000);
        } catch (e) {
            if (log) log.innerHTML += `<div style="color:var(--err);">[ERROR] ${this._esc(e.message)}</div>`;
            window.triggerToast('Install Failed — Check Plugin Log', 'err');
        }
    },

    // ── Logs modal ───────────────────────────────────────────────────────────

    openLogs(id, name) {
        this.activeLogId = id;
        document.getElementById('pl-log-title').innerText = name;
        document.getElementById('pl-modal-logs').style.display = 'flex';
        this.refreshLogs();
        this.logTimer = setInterval(() => this.refreshLogs(), 3000);
    },

    closeLogs() {
        document.getElementById('pl-modal-logs').style.display = 'none';
        clearInterval(this.logTimer);
        this.activeLogId = null;
    },

    async refreshLogs() {
        if (!this.activeLogId) return;
        const stream = document.getElementById('pl-log-stream');
        try {
            const r = await fetch(`/api/system/plugins/${this.activeLogId}/logs`);
            if (!r.ok) throw new Error('Fetch failed');
            const d = await r.json();
            const p = this.registry.get(this.activeLogId);
            const wdStr = p?.watchdog
                ? `<span style="color:var(--ok)"><i class="fas fa-dog"></i> ACTIVE</span>`
                : `<span style="color:var(--txt3)"><i class="fas fa-ban"></i> DISABLED</span>`;
            document.getElementById('pl-log-meta').innerHTML = `WD: ${wdStr} &nbsp;·&nbsp; BUFFER: ${d.count} LINES`;
            if (!d.logs.length) {
                stream.innerHTML = `<div style="color:var(--txt3);text-align:center;margin-top:50px;">[ NO LOG DATA CAPTURED ]</div>`;
                return;
            }
            const wasBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 50;
            stream.innerHTML = d.logs.map(l => `<div class="log-line l-${l.lvl}">${this._esc(l.msg)}</div>`).join('');
            if (wasBottom) stream.scrollTop = stream.scrollHeight;
        } catch (e) {
            stream.innerHTML = `<div style="color:var(--err);text-align:center;margin-top:50px;">[ FAILED TO READ PIPE ]</div>`;
        }
    },

    async clearLogs() {
        if (!this.activeLogId) return;
        try { await fetch(`/api/system/plugins/${this.activeLogId}/logs`, { method: 'DELETE' }); this.refreshLogs(); } catch (e) {}
    },

    // ── Restart ──────────────────────────────────────────────────────────────

    restartCore() {
        if (!confirm('Restarting will apply all plugin changes. The UI will disconnect briefly. Proceed?')) return;
        document.getElementById('pl-grid').innerHTML =
            `<div style="grid-column:1/-1;text-align:center;color:var(--warn);padding:60px;font-family:var(--mono);">
                <i class="fas fa-power-off fa-3x fa-fade" style="margin-bottom:18px;display:block;"></i>
                RESTARTING CORE SERVICES…
             </div>`;
        fetch('/api/system/restart', { method: 'POST' }).catch(() => {});
        setTimeout(() => {
            setInterval(async () => {
                try { const r = await fetch('/api/status'); if (r.ok) window.location.reload(); } catch (e) {}
            }, 2000);
        }, 3000);
    },
};