// node_config.js
window.NodeConfigApp = {
    snapshot: null,
    pending: {},
    activeSectionKey: null,
    _pollTimer: null,
    _pollStart: 0,

    init() {
        document.getElementById('nc-btn-save').onclick  = () => this.save();
        document.getElementById('nc-btn-reset').onclick = () => this.reset();
        document.addEventListener('keydown', e => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); this.save(); }
        });
        this.load();
    },

    async load() {
        document.getElementById('nc-topnav').innerHTML =
            `<div style="padding:10px;text-align:center;color:var(--txt3);font-family:var(--mono);font-size:10px;width:100%;">LOADING TABS...</div>`;
        document.getElementById('nc-fields').innerHTML =
            `<div style="padding:40px;text-align:center;color:var(--txt3);font-family:var(--mono);font-size:10px;">[ READING NODE CONFIGURATION... ]</div>`;

        // ALL RADIOS mode — can't configure multiple radios simultaneously
        if (window._activeSlotId === 'all') {
            document.getElementById('nc-topnav').innerHTML = '';
            document.getElementById('nc-fields').innerHTML =
                `<div style="padding:40px;text-align:center;color:var(--warn);font-family:var(--mono);font-size:11px;border:1px solid var(--warn);border-radius:var(--r);background:rgba(255,168,38,0.05);">
                    ⚠️ NODE CONFIG UNAVAILABLE IN ALL RADIOS MODE<br>
                    <span style="font-size:9px;color:var(--txt3);margin-top:8px;display:block;">Select a specific radio from the topbar to configure it.</span>
                </div>`;
            return;
        }

        try {
            const _ncSlot = window._activeSlotId || 'node_0';
            const r = await fetch(`/api/node/config?slot_id=${encodeURIComponent(_ncSlot)}`, { credentials: 'include' });
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                window.triggerToast?.(`Config load failed: ${d.detail || r.status}`, 'err');
                document.getElementById('nc-fields').innerHTML =
                    `<div style="padding:40px;text-align:center;color:var(--err);font-family:var(--mono);font-size:10px;">FAILED TO LOAD CONFIG — RADIO CONNECTED?</div>`;
                return;
            }
            this.snapshot = await r.json();
            this.pending = {};
            this._buildTopnav();
            const first = document.querySelector('.nc-navitem[data-key]');
            if (first) this._activateSection(first.dataset.key);
            this._updateBadge();
            window.triggerToast?.('Configuration loaded', 'ok');
        } catch (e) {
            window.triggerToast?.(`Error: ${e.message}`, 'err');
        }
    },

    async save() {
        const n = Object.keys(this.pending).length;
        if (n === 0) { window.triggerToast?.('No changes to save', 'inf'); return; }

        const changes = Object.entries(this.pending).map(([path, v]) => ({ path, value: v.current }));
        const reboot  = document.getElementById('nc-reboot-check').checked;
        const btn     = document.getElementById('nc-btn-save');
        btn.disabled  = true;
        btn.textContent = 'SAVING...';

        try {
            const r = await fetch('/api/node/config/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ changes, reboot, slot_id: window._activeSlotId || 'node_0' }),
            });
            const d = await r.json();
            if (r.ok) {
                const written = d.written || [];
                const errors  = d.errors  || [];
                if (errors.length) {
                    window.triggerToast?.(`${written.length} written, ${errors.length} error(s): ${errors[0]}`, 'err');
                } else {
                    window.triggerToast?.(`✓ Saved: ${written.join(', ') || 'no sections changed'}`, 'ok');
                }

                this._applyPendingToSnapshot(changes);
                this.pending = {};
                this._updateBadge();
                
                document.querySelectorAll('.nc-inp.changed').forEach(el => el.classList.remove('changed'));
                document.querySelectorAll('.nc-field-row.modified').forEach(el => el.classList.remove('modified'));

                if (d.reboot_triggered) {
                    this._startPolling();
                } else {
                    if (this.activeSectionKey) this._activateSection(this.activeSectionKey);
                }
            } else {
                window.triggerToast?.(`Save failed: ${d.detail || r.status}`, 'err');
            }
        } catch (e) {
            window.triggerToast?.(`Save error: ${e.message}`, 'err');
        } finally {
            btn.disabled = Object.keys(this.pending).length === 0;
            btn.textContent = 'SAVE CHANGES';
        }
    },

    _applyPendingToSnapshot(changes) {
        for (const { path, value } of changes) {
            if (path.startsWith('identity.')) {
                const field = path.slice(9);
                if (this.snapshot.identity) this.snapshot.identity[field] = value;
                continue;
            }

            let found = false;
            for (const sec of Object.values(this.snapshot.localConfig || {})) {
                const f = sec.find(f => f.path === path);
                if (f) { f.value = this._coerceValue(f, value); found = true; break; }
            }
            if (found) continue;

            for (const sec of Object.values(this.snapshot.moduleConfig || {})) {
                const f = sec.find(f => f.path === path);
                if (f) { f.value = this._coerceValue(f, value); found = true; break; }
            }
            if (found) continue;

            for (const ch of Object.values(this.snapshot.channels || {})) {
                const f = ch.fields?.find(f => f.path === path);
                if (f) { f.value = this._coerceValue(f, value); break; }
            }
        }
    },

    _coerceValue(field, strValue) {
        if (field.type === 'bool')  return strValue === 'true';
        if (field.type === 'int')   return parseInt(strValue, 10);
        if (field.type === 'float') return parseFloat(strValue);
        if (field.type === 'enum')  return parseInt(strValue, 10);
        return strValue;
    },

    reset() {
        if (!this.snapshot) return;
        this.pending = {};
        if (this.activeSectionKey) this._activateSection(this.activeSectionKey);
        this._updateBadge();
        window.triggerToast?.('Changes reset', 'inf');
    },

    _startPolling() {
        this._pollStart = Date.now();
        const banner = document.getElementById('nc-poll-banner');
        banner.style.display = 'flex';
        if (this._pollTimer) clearInterval(this._pollTimer);
        this._pollTimer = setInterval(async () => {
            const elapsed = Math.floor((Date.now() - this._pollStart) / 1000);
            const el = document.getElementById('nc-poll-count');
            if (el) el.textContent = `${elapsed}s`;
            if (elapsed > 90) { this.stopPolling(true); window.triggerToast?.('Reconnect timed out', 'err'); return; }
            try {
                const r = await fetch(`/api/status?slot_id=${encodeURIComponent(window._activeSlotId||'node_0')}`, { credentials: 'include' });
                if (r.ok) {
                    const d = await r.json();
                    if (d.is_system_ready) {
                        this.stopPolling(false);
                        window.triggerToast?.('✓ Node back online', 'ok');
                    }
                }
            } catch (_) {}
        }, 2500);
    },

    stopPolling(timedOut = false) {
        if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
        const banner = document.getElementById('nc-poll-banner');
        if (banner) banner.style.display = 'none';
        if (!timedOut) this.load();
    },

    _buildTopnav() {
        const nav = document.getElementById('nc-topnav');
        nav.innerHTML = '';

        const makeTab = (key, label, count, role) => {
            const el = document.createElement('div');
            el.className = 'nc-navitem';
            el.dataset.key = key;
            el.style.cssText = 'padding:6px 12px; border-radius:var(--r); font-size:11px; font-weight:600; cursor:pointer; white-space:nowrap; border:1px solid var(--bd2); background:var(--bg2); color:var(--txt3); display:flex; align-items:center; gap:8px; user-select:none; transition:all 0.2s;';

            const txt = document.createElement('span');
            txt.textContent = label.toUpperCase();
            el.appendChild(txt);

            if (role && role !== '0') {
                const rname = this._roleStr(role);
                const chip = document.createElement('span');
                chip.className = 'pill';
                chip.style.cssText = 'font-size:8px;padding:1px 4px;';
                chip.textContent = rname.slice(0, 3);
                el.appendChild(chip);
            }

            if (count !== undefined && count !== '—') {
                const b = document.createElement('span');
                b.className = 'nc-navbadge';
                b.style.cssText = 'background:var(--bg3); padding:2px 6px; border-radius:10px; font-size:9px; font-family:var(--mono);';
                b.textContent = count;
                el.appendChild(b);
            }

            el.addEventListener('click', () => this._activateSection(key));
            return el;
        };

        const addGroup = (title, items) => {
            if (!items.length) return;
            const wrap = document.createElement('div');
            wrap.style.cssText = 'display:flex; flex-direction:column; gap:8px;';
            wrap.innerHTML = `<div style="font-size:9px; font-weight:800; color:var(--txt3); letter-spacing:1px; margin-left:2px;">${title}</div>`;
            
            const chips = document.createElement('div');
            chips.style.cssText = 'display:flex; flex-wrap:wrap; gap:6px;';
            items.forEach(i => chips.appendChild(makeTab(i.key, i.label, i.count, i.role)));
            
            wrap.appendChild(chips);
            nav.appendChild(wrap);
        };

        // Identity Group
        addGroup('IDENTITY', [{ key: '__identity__', label: 'Identity', count: '—' }]);

        // Local Config Group
        const lcItems = Object.keys(this.snapshot.localConfig).map(sec => ({
            key: `lc::${sec}`, label: sec, count: this.snapshot.localConfig[sec].length
        }));
        addGroup('LOCAL CONFIG', lcItems);

        // Module Config Group
        const mcItems = Object.keys(this.snapshot.moduleConfig).map(sec => ({
            key: `mc::${sec}`, label: sec, count: this.snapshot.moduleConfig[sec].length
        }));
        addGroup('MODULE CONFIG', mcItems);

        // Channels Group
        if (Object.keys(this.snapshot.channels).length) {
            const chItems = Object.entries(this.snapshot.channels).map(([idx, ch]) => {
                const label = ch.fields?.find(f => f.name === 'name')?.value || '';
                return { key: `ch::${idx}`, label: `CH${idx} ${label}`.trim(), count: ch.fields?.length || 0, role: ch.role };
            });
            addGroup('CHANNELS', chItems);
        }
    },

    _roleStr(r) {
        if (r === '1' || r === 'PRIMARY')   return 'PRIMARY';
        if (r === '2' || r === 'SECONDARY') return 'SECONDARY';
        return 'DISABLED';
    },

    _activateSection(key) {
        this.activeSectionKey = key;
        document.querySelectorAll('.nc-navitem').forEach(el => {
            if (el.dataset.key === key) {
                el.style.background = 'var(--acc)';
                el.style.color = '#fff';
                el.style.borderColor = 'var(--acc)';
                const badge = el.querySelector('.nc-navbadge');
                if(badge) { badge.style.background = 'rgba(255,255,255,0.2)'; badge.style.color = '#fff'; }
            } else {
                el.style.background = 'var(--bg2)';
                el.style.color = 'var(--txt3)';
                el.style.borderColor = 'var(--bd2)';
                const badge = el.querySelector('.nc-navbadge');
                if(badge) { badge.style.background = 'var(--bg3)'; badge.style.color = 'inherit'; }
            }
        });

        if (key === '__identity__') {
            this._renderIdentity();
        } else if (key.startsWith('lc::')) {
            this._renderFields(`localConfig :: ${key.slice(4)}`, this.snapshot.localConfig[key.slice(4)] || []);
        } else if (key.startsWith('mc::')) {
            this._renderFields(`moduleConfig :: ${key.slice(4)}`, this.snapshot.moduleConfig[key.slice(4)] || []);
        } else if (key.startsWith('ch::')) {
            const ch = this.snapshot.channels[key.slice(4)];
            this._renderFields(`channel [${key.slice(4)}] — ${this._roleStr(ch?.role || '0')}`, ch?.fields || []);
        }
    },

    _renderIdentity() {
        const id = this.snapshot.identity || {};
        document.getElementById('nc-section-title').textContent = 'NODE IDENTITY';
        document.getElementById('nc-section-sub').textContent   = 'User protobuf · broadcast to mesh on change';

        const fields = document.getElementById('nc-fields');
        fields.innerHTML = '';

        const grid = document.createElement('div');
        grid.style.cssText = 'display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:16px;';
        
        grid.innerHTML = `
            <div style="background:var(--bg2); padding:12px; border:1px solid var(--bd2); border-radius:var(--r); display:flex; flex-direction:column; gap:8px;">
                <div style="font-size:10px;color:var(--txt);font-weight:700">LONG NAME</div>
                <div style="font-size:10px;color:var(--txt3);margin-bottom:4px;">The full name of your node shown on the mesh.</div>
                <input class="inp inp-mono nc-inp" style="width:100%" data-path="identity.long_name"
                    data-original="${this._esc(id.long_name || '')}"
                    value="${this._esc(id.long_name || '')}"
                    oninput="window.NodeConfigApp._onInput(this)">
            </div>
            <div style="background:var(--bg2); padding:12px; border:1px solid var(--bd2); border-radius:var(--r); display:flex; flex-direction:column; gap:8px;">
                <div style="font-size:10px;color:var(--txt);font-weight:700">SHORT NAME (MAX 4)</div>
                <div style="font-size:10px;color:var(--txt3);margin-bottom:4px;">Abbreviated 4-character ID shown on screens.</div>
                <input class="inp inp-mono nc-inp" style="width:100%" data-path="identity.short_name"
                    data-original="${this._esc(id.short_name || '')}"
                    value="${this._esc(id.short_name || '')}"
                    maxlength="4"
                    oninput="window.NodeConfigApp._onInput(this)">
            </div>`;
        fields.appendChild(grid);
    },

    _getTypeBadge(type) {
        let bg, col, bd;
        switch(type) {
            case 'bool':   col = '#10b981'; bg = 'rgba(16, 185, 129, 0.1)'; bd = 'rgba(16, 185, 129, 0.2)'; break; // Emerald
            case 'int':
            case 'float':  col = '#3b82f6'; bg = 'rgba(59, 130, 246, 0.1)'; bd = 'rgba(59, 130, 246, 0.2)'; break; // Blue
            case 'string': col = '#f59e0b'; bg = 'rgba(245, 158, 11, 0.1)'; bd = 'rgba(245, 158, 11, 0.2)'; break; // Amber
            case 'enum':   col = '#8b5cf6'; bg = 'rgba(139, 92, 246, 0.1)'; bd = 'rgba(139, 92, 246, 0.2)'; break; // Purple
            case 'ip':     col = '#06b6d4'; bg = 'rgba(6, 182, 212, 0.1)';  bd = 'rgba(6, 182, 212, 0.2)'; break; // Cyan
            default:       col = 'var(--txt3)'; bg = 'var(--bg3)'; bd = 'transparent'; break;
        }
        return `<span style="color:${col}; background:${bg}; border:1px solid ${bd}; padding:2px 6px; border-radius:4px; font-family:var(--mono); font-size:9px; flex-shrink:0;">${type}</span>`;
    },

    _renderFields(title, fields) {
        const modCount = fields.filter(f => this.pending[f.path] !== undefined).length;
        document.getElementById('nc-section-title').textContent = title.toUpperCase();
        document.getElementById('nc-section-sub').textContent =
            `${fields.length} fields${modCount ? ` · ${modCount} modified` : ''}`;

        const wrap = document.getElementById('nc-fields');
        wrap.innerHTML = '';

        if (fields.length === 0) {
            wrap.innerHTML = `<div style="padding:40px;text-align:center;color:var(--txt3);font-family:var(--mono);font-size:10px;">NO FIELDS IN THIS SECTION</div>`;
            return;
        }

        const grid = document.createElement('div');
        grid.style.cssText = 'display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:16px;';

        fields.forEach(f => {
            const isMod = this.pending[f.path] !== undefined;
            const row   = document.createElement('div');
            row.className = `nc-field-row${isMod ? ' modified' : ''}`;
            row.dataset.path = f.path;
            
            // Subtle dynamic border based on modification state
            const borderLeft = isMod ? 'border-left: 3px solid var(--warn);' : 'border-left: 1px solid var(--bd2);';
            row.style.cssText = `background:var(--bg2); padding:12px; border:1px solid var(--bd2); ${borderLeft} border-radius:var(--r); display:flex; transition: border 0.2s;`;

            const descHtml = f.description ? `<div style="font-size:10px; color:var(--txt3); margin-top:8px; line-height:1.4; overflow-wrap:break-word;">${this._esc(f.description)}</div>` : '';

            if (f.type === 'bool') {
                row.style.justifyContent = 'space-between';
                row.style.alignItems = 'center';
                row.style.gap = '12px';
                row.innerHTML = `
                    <div style="flex:1; min-width:0;">
                        <div style="display:flex; align-items:center; gap:8px; margin-bottom:4px;">
                            <div style="font-weight:700; color:var(--txt); font-size:11px; overflow-wrap:break-word;">${this._esc(f.name).toUpperCase()}</div>
                            ${this._getTypeBadge(f.type)}
                        </div>
                        <div style="font-size:9px; color:var(--txt3); font-family:var(--mono); word-break:break-all;">${this._esc(f.path)}</div>
                        ${descHtml}
                    </div>
                    <div style="flex-shrink:0; display:flex;">
                        ${this._buildInput(f, isMod)}
                    </div>
                `;
            } else {
                row.style.flexDirection = 'column';
                row.style.justifyContent = 'center';
                row.style.gap = '8px'; 
                row.innerHTML = `
                    <div style="font-size:10px;color:var(--txt);font-weight:700;display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                        <span style="flex:1; min-width:0; overflow-wrap:break-word;">${this._esc(f.name).toUpperCase()}</span>
                        ${this._getTypeBadge(f.type)}
                    </div>
                    ${descHtml}
                    ${this._buildInput(f, isMod)}
                    <div style="font-size:9px; color:var(--txt3); font-family:var(--mono); word-break:break-all;">${this._esc(f.path)}</div>
                `;
            }
            grid.appendChild(row);
        });
        wrap.appendChild(grid);
    },

    _buildInput(f, isMod) {
        const isRO  = f.readonly || f.type === 'repeated';
        const cur   = isMod ? this.pending[f.path].current : String(f.value ?? '');
        const orig  = this._esc(String(f.value ?? ''));
        const mod   = isMod ? ' changed' : '';
        const path  = this._esc(f.path);

        if (f.type === 'bool') {
            const chk = isMod ? this.pending[f.path].current === 'true' : !!f.value;
            return `<label class="tog"><input type="checkbox" class="nc-inp${mod}" data-path="${path}"
                data-original="${f.value ? 'true' : 'false'}"
                ${chk ? 'checked' : ''} ${isRO ? 'disabled' : ''}
                onchange="window.NodeConfigApp._onCheckbox(this)"><span class="tog-sl"></span></label>`;
        }

        if (f.type === 'enum' && f.options) {
            const curVal = isMod ? this.pending[f.path].current : f.value;
            const opts = Object.entries(f.options).map(([nm, num]) =>
                `<option value="${num}" ${num == curVal ? 'selected' : ''}>${this._esc(nm)}</option>`
            ).join('');
            return `<select class="inp mono nc-inp${mod}" style="width:100%" data-path="${path}" data-original="${f.value}"
                ${isRO ? 'disabled' : ''} onchange="window.NodeConfigApp._onInput(this)">${opts}</select>`;
        }

        const type = (f.type === 'int' || f.type === 'float') ? 'number' : 'text';
        const step = f.type === 'float' ? ' step="any"' : '';
        return `<input type="${type}"${step} class="inp inp-mono nc-inp${mod}" style="width:100%" data-path="${path}"
            data-original="${orig}" value="${this._esc(cur)}"
            ${isRO ? 'readonly' : ''}
            oninput="window.NodeConfigApp._onInput(this)">`;
    },

    _onInput(el) {
        const path = el.dataset.path, orig = el.dataset.original, cur = el.value;
        const changed = cur !== orig;
        el.classList.toggle('changed', changed);
        el.closest('.nc-field-row')?.classList.toggle('modified', changed);
        if (changed) this.pending[path] = { original: orig, current: cur };
        else delete this.pending[path];
        this._updateBadge();
    },

    _onCheckbox(el) {
        const path = el.dataset.path, orig = el.dataset.original, cur = el.checked ? 'true' : 'false';
        const changed = cur !== orig;
        el.closest('.nc-field-row')?.classList.toggle('modified', changed);
        if (changed) this.pending[path] = { original: orig, current: cur };
        else delete this.pending[path];
        this._updateBadge();
    },

    _updateBadge() {
        const n    = Object.keys(this.pending).length;
        const pill = document.getElementById('nc-changes-pill');
        if (pill) { pill.textContent = `${n} PENDING`; pill.style.display = n > 0 ? '' : 'none'; }
        document.getElementById('nc-btn-save').disabled  = n === 0;
        document.getElementById('nc-btn-reset').disabled = n === 0;

        document.querySelectorAll('.nc-navitem[data-key]').forEach(el => {
            const key = el.dataset.key;
            const fields = this._getSectionFields(key);
            const hasMod = fields.some(f => this.pending[f.path] !== undefined);
            const badge = el.querySelector('.nc-navbadge');
            
            if (badge) {
                if (hasMod) {
                    badge.textContent = '●';
                    badge.style.color = 'var(--warn)';
                } else {
                    badge.textContent = fields.length || '—';
                    badge.style.color = el.dataset.key === this.activeSectionKey ? '#fff' : 'inherit';
                }
            }
        });

        const key = this.activeSectionKey;
        if (key && key !== '__identity__') {
            const fields = this._getSectionFields(key);
            const modCount = fields.filter(f => this.pending[f.path] !== undefined).length;
            const sub = document.getElementById('nc-section-sub');
            if (sub) sub.textContent = `${fields.length} fields${modCount ? ` · ${modCount} modified` : ''}`;
        }
    },

    _getSectionFields(key) {
        if (!this.snapshot || key === '__identity__') return [];
        if (key.startsWith('lc::')) return this.snapshot.localConfig[key.slice(4)] || [];
        if (key.startsWith('mc::')) return this.snapshot.moduleConfig[key.slice(4)] || [];
        if (key.startsWith('ch::')) return this.snapshot.channels[key.slice(4)]?.fields || [];
        return [];
    },

    _esc(s) {
        return String(s ?? '')
            .replace(/&/g,'&amp;').replace(/</g,'&lt;')
            .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    },
};