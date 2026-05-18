/* ==========================================================================
 * MeshDash C2 — Mesh Shark (Deep Packet Inspector) Module
 * ========================================================================== */

window.C2SharkApp = {
    MAX_BUFFER: 5000,
    packets: [],
    selectedId: null,
    filterType: 'all',
    bpf: '',
    isLive: true,
    pktCounter: 0,
    prevPktTime: null,
    snrHistory: [],
    sort: { col: 'no', dir: 'desc' },
    sidebarNode: null,

    init() {
        // Clear previous slot's captured packets on every init
        this.packets = [];
        this.pktCounter = 0;
        this.setupListeners();
        this.setupResizer();
        this.renderSidebar();
        
        // Pull existing global memory packets (already slot-filtered via meshState)
        if (window.meshState?.packets && this.packets.length === 0) {
            const arr = [...window.meshState.packets].reverse();
            arr.forEach(p => this.ingest(p, false));
            this.renderTable(null, true);
        }
        
        // Start drawing the sparkline loop
        if(!this.sparkInterval) {
            this.sparkInterval = setInterval(() => this.drawSparkline(), 2000);
        }
    },

    setupListeners() {
        const el = id => document.getElementById(id);
        
        // BPF Filter
        el('sh-bpf').oninput = (e) => { this.bpf = e.target.value.toLowerCase(); this.renderTable(); };
        el('sh-bpf-clear').onclick = () => { el('sh-bpf').value = ''; this.bpf = ''; this.renderTable(); };

        // Toolbar Buttons
        el('sh-btn-live').onclick = (e) => {
            this.isLive = !this.isLive;
            e.target.innerHTML = this.isLive ? '<i class="fas fa-record-vinyl"></i> CAPTURING' : '<i class="fas fa-pause"></i> PAUSED';
            e.target.className = this.isLive ? 'btn btn-sm btn-acc' : 'btn btn-sm btn-warn';
        };
        
        el('sh-btn-clear').onclick = () => {
            this.packets = [];
            this.selectedId = null;
            this.pktCounter = 0;
            this.snrHistory = [];
            this.renderTable();
            this.clearDetails();
        };

        el('sh-btn-hist').onclick = async () => {
            if(typeof window.triggerToast === 'function') window.triggerToast('Fetching DB History...', 'warn');
            try {
                const _shSQ = window._activeSlotId && window._activeSlotId !== 'node_0'
                    ? `&slot_id=${encodeURIComponent(window._activeSlotId)}` : '';
                const res = await fetch(`/api/packets/history?limit=1000${_shSQ}`);
                if(res.ok) {
                    const data = await res.json();
                    this.packets = [];
                    this.pktCounter = 0;
                    data.reverse().forEach(p => this.ingest(p, false));
                    this.renderTable(null, true);
                    if(typeof window.triggerToast === 'function') window.triggerToast(`Loaded ${data.length} packets`, 'ok');
                }
            } catch(e) {
                if(typeof window.triggerToast === 'function') window.triggerToast('DB Fetch Failed', 'err');
            }
        };

        // Quick Pills
        document.querySelectorAll('.sh-pill').forEach(p => {
            p.onclick = () => {
                document.querySelectorAll('.sh-pill').forEach(x => x.classList.remove('active'));
                p.classList.add('active');
                this.filterType = p.dataset.type;
                this.renderTable();
            };
        });

        // Table Headers
        document.querySelectorAll('#sh-table-wrap th[data-col]').forEach(th => {
            th.onclick = () => {
                const col = th.dataset.col;
                this.sort.dir = (this.sort.col === col && this.sort.dir === 'desc') ? 'asc' : 'desc';
                this.sort.col = col;
                this.renderTable();
            };
        });
    },

    setupResizer() {
        const resizer = document.getElementById('sh-resizer');
        const pane = document.getElementById('sh-detail-pane');
        let dragging = false, startY, startH;

        resizer.onmousedown = (e) => {
            dragging = true;
            startY = e.clientY;
            startH = pane.getBoundingClientRect().height;
            resizer.classList.add('dragging');
            document.body.style.cursor = 'ns-resize';
            e.preventDefault();
        };

        document.onmousemove = (e) => {
            if (!dragging) return;
            const newH = Math.max(100, startH - (e.clientY - startY));
            pane.style.height = `${newH}px`;
        };

        document.onmouseup = () => {
            if (dragging) {
                dragging = false;
                resizer.classList.remove('dragging');
                document.body.style.cursor = '';
            }
        };
    },

    // ─────────────────── INGESTION PIPELINE ───────────────────
    // Called publicly by app.js whenever a packet arrives globally
    globalIngest(rawPacket) {
        if (!this.isLive) return;
        this.ingest(rawPacket, true);
        
        // If we are currently looking at the Shark view, update the UI
        if (window.meshState?.currentView === 'shark') {
            this.renderTable(rawPacket.id || rawPacket.packet_event_id);
            this.renderSidebar(); // Update packet counts in sidebar
        }
    },

    ingest(raw, liveUpdate = false) {
        this.pktCounter++;
        const t = raw.timestamp || raw.rxTime || raw.rx_time || (Date.now()/1000);
        
        const p = {
            id: raw.id || raw.packet_event_id || `pkt-${Date.now()}-${this.pktCounter}`,
            no: this.pktCounter,
            time: t,
            delta: this.prevPktTime ? +(t - this.prevPktTime).toFixed(2) : 0,
            type: raw.packet_type || raw.app_packet_type || 'Unknown',
            from: raw.fromId || raw.from_id || String(raw.from || ''),
            to: raw.toId || raw.to_id || String(raw.to || ''),
            ch: raw.channel ?? 0,
            snr: raw.rx_snr ?? raw.rxSnr ?? raw.decoded?._snr ?? null,
            rssi: raw.rx_rssi ?? raw.rxRssi ?? raw.decoded?._rssi ?? null,
            hops: raw.hop_limit ?? raw.hopLimit ?? raw.decoded?._hopLimit ?? null,
            hStart: raw.hop_start ?? raw.hopStart ?? null,
            rawObj: raw
        };

        this.prevPktTime = t;
        this.packets.unshift(p);
        
        if (this.packets.length > this.MAX_BUFFER) this.packets.pop();

        if (p.snr != null) {
            this.snrHistory.push(p.snr);
            if(this.snrHistory.length > 50) this.snrHistory.shift();
        }
    },

    // ─────────────────── RENDERERS ───────────────────
    renderSidebar() {
        const list = document.getElementById('sh-node-list');
        if (!list) return;

        // Count packets per node locally from our buffer
        const counts = {};
        this.packets.forEach(p => { counts[p.from] = (counts[p.from] || 0) + 1; });

        const nodes = Object.values(window.meshState?.nodes || {});
        document.getElementById('sh-sb-count').innerText = nodes.length;

        let html = `<div class="sh-sb-item ${this.sidebarNode === null ? 'active' : ''}" onclick="window.C2SharkApp.filterNode(null)">
                        <div style="width:10px; height:10px; border-radius:50%; background:var(--acc);"></div>
                        <div style="flex:1; font-weight:bold; font-size:11px; color:var(--acc);">ALL TRAFFIC</div>
                        <div style="font-family:var(--mono); font-size:10px;">${this.packets.length}</div>
                    </div>`;

        nodes.sort((a,b) => (b.lastHeard || 0) - (a.lastHeard || 0)).forEach(n => {
            const cnt = counts[n.node_id] || 0;
            if (cnt === 0 && this.sidebarNode !== n.node_id) return; // Hide zero-packet nodes from shark

            const name = n.user?.longName || n.user?.shortName || n.node_id;
            const isLocal = n.isLocal ? '<i class="fas fa-star" style="color:var(--warn); font-size:8px; margin-left:4px;"></i>' : '';
            const isActive = this.sidebarNode === n.node_id ? 'active' : '';

            html += `<div class="sh-sb-item ${isActive}" onclick="window.C2SharkApp.filterNode('${n.node_id}')">
                        <div style="width:10px; height:10px; border-radius:50%; background:var(--bd2);"></div>
                        <div style="flex:1; overflow:hidden;">
                            <div style="font-weight:bold; font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${window.escapeHtml(name)}${isLocal}</div>
                            <div style="font-family:var(--mono); font-size:8px; color:var(--txt3);">${n.node_id}</div>
                        </div>
                        <div style="font-family:var(--mono); font-size:9px; color:var(--acc);">${cnt}</div>
                    </div>`;
        });
        list.innerHTML = html;
    },

    filterNode(nid) {
        this.sidebarNode = nid;
        this.renderSidebar();
        this.renderTable();
    },

    applyFilters() {
        const terms = this.bpf.split(/\s+/).filter(Boolean);
        return this.packets.filter(p => {
            // Pill filter
            if (this.filterType !== 'all' && p.type !== this.filterType && !(this.filterType === 'Ack' && p.type.includes('Routing'))) return false;
            // Sidebar node filter
            if (this.sidebarNode && p.from !== this.sidebarNode && p.to !== this.sidebarNode) return false;
            
            // BPF String Filter
            if (terms.length > 0) {
                for (const t of terms) {
                    if (t.startsWith('src:')) { if (!p.from.toLowerCase().includes(t.slice(4))) return false; }
                    else if (t.startsWith('dst:')) { if (!p.to.toLowerCase().includes(t.slice(4))) return false; }
                    else if (t.startsWith('type:')) { if (!p.type.toLowerCase().includes(t.slice(5))) return false; }
                    else if (t.startsWith('snr>')) { if (p.snr == null || p.snr <= parseFloat(t.slice(4))) return false; }
                    else if (t.startsWith('snr<')) { if (p.snr == null || p.snr >= parseFloat(t.slice(4))) return false; }
                    else {
                        // Global text search
                        const rawStr = JSON.stringify(p.rawObj).toLowerCase();
                        if (!rawStr.includes(t)) return false;
                    }
                }
            }
            return true;
        });
    },

    renderTable(flashId = null, scrollBottom = false) {
        const tbody = document.getElementById('sh-tbody');
        if (!tbody) return;

        let filtered = this.applyFilters();
        
        // Sorting
        const m = this.sort.dir === 'asc' ? 1 : -1;
        filtered.sort((a,b) => {
            let av = a[this.sort.col], bv = b[this.sort.col];
            if (this.sort.col === 'snr') { av = a.snr ?? -999; bv = b.snr ?? -999; }
            return av < bv ? -1*m : av > bv ? 1*m : 0;
        });

        document.getElementById('sh-count').innerText = filtered.length;
        this.updatePills();

        if (filtered.length === 0) {
            tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:40px; color:var(--txt3);">[ NO PACKETS MATCH FILTER ]</td></tr>`;
            return;
        }

        const f = document.createDocumentFragment();
        filtered.slice(0, 1000).forEach(p => {
            const tr = document.createElement('tr');
            if (p.id === this.selectedId) tr.classList.add('selected');
            
            const pcol = p.type === 'Message' ? 'var(--ok)' : p.type === 'Position' ? 'var(--warn)' : p.type === 'Telemetry' ? 'var(--pur)' : 'var(--txt3)';
            const snrClass = p.snr == null ? 'color:var(--txt3)' : p.snr > 0 ? 'color:var(--ok)' : 'color:var(--warn)';
            const hps = p.hops != null ? (p.hStart != null ? `<span style="color:var(--acc)">${p.hStart - p.hops}</span><span style="color:var(--txt3)">/${p.hStart}</span>` : p.hops) : '-';

            tr.innerHTML = `
                <td class="c-no">${p.no}</td>
                <td class="c-time">${window.fmtTime(p.time)}</td>
                <td class="c-dt">+${p.delta}s</td>
                <td class="c-src">${window.escapeHtml(this.nodeName(p.from))}</td>
                <td class="c-dst" style="color:var(--txt3) !important;">${window.escapeHtml(this.nodeName(p.to))}</td>
                <td class="c-ch">${p.ch}</td>
                <td class="c-proto" style="color:${pcol}">${p.type.substring(0,10).toUpperCase()}</td>
                <td class="c-snr" style="${snrClass}">${p.snr ?? '-'}</td>
                <td class="c-hops">${hps}</td>
                <td>${window.escapeHtml(this.infoLine(p))}</td>
            `;
            
            tr.onclick = () => this.selectPacket(p, tr);
            f.appendChild(tr);
        });

        tbody.innerHTML = '';
        tbody.appendChild(f);

        if (scrollBottom) {
            document.getElementById('sh-table-wrap').scrollTop = 0;
        }
    },

    updatePills() {
        const c = { all:0, Message:0, Position:0, Telemetry:0, 'Node Info':0, Ack:0 };
        this.packets.forEach(p => {
            c.all++;
            if (c[p.type] !== undefined) c[p.type]++;
            if (p.type.includes('Routing')) c.Ack++;
        });
        document.getElementById('p-all').innerText = c.all;
        document.getElementById('p-msg').innerText = c.Message;
        document.getElementById('p-pos').innerText = c.Position;
        document.getElementById('p-tlm').innerText = c.Telemetry;
        document.getElementById('p-nfo').innerText = c['Node Info'];
        document.getElementById('p-ack').innerText = c.Ack;
    },

    // ─────────────────── SELECTION & DETAILS ───────────────────
    selectPacket(p, trEl) {
        this.selectedId = p.id;
        document.querySelectorAll('#sh-tbody tr').forEach(r => r.classList.remove('selected'));
        if(trEl) trEl.classList.add('selected');

        const raw = p.rawObj;
        
        // 1. RAW JSON
        let jsonStr = JSON.stringify(raw, null, 2);
        // Syntax highlight
        jsonStr = jsonStr.replace(/("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, m => {
            if (/^"/.test(m)) return /:$/.test(m) ? `<span class="jk">${m}</span>` : `<span class="jstr">${m}</span>`;
            if (/true|false/.test(m)) return `<span class="jboo">${m}</span>`;
            if (/null/.test(m)) return `<span class="jnul">${m}</span>`;
            return `<span class="jnum">${m}</span>`;
        });
        document.getElementById('sh-dt-raw').innerHTML = jsonStr;

        // 2. KV / DECODED METADATA
        const kv = (k,v) => `<div class="sh-kv"><div class="sh-k">${k}</div><div class="sh-v">${v ?? '-'}</div></div>`;
        let kHtml = kv('Protocol', p.type);
        
        // Add a clickable link to open the global C2 Node Modal!
        const srcLink = `<span style="color:var(--acc); cursor:pointer; text-decoration:underline;" onclick="window.c2OpenNodeDetail('${p.from}')">${window.escapeHtml(this.nodeName(p.from))}</span>`;
        kHtml += kv('Source', srcLink);
        
        if (p.type === 'Message') {
            kHtml += kv('Payload', `"${raw.decoded?.payload || raw.decoded?.text}"`);
        } else if (p.type === 'Position') {
            const pos = raw.decoded?.position || {};
            kHtml += kv('Latitude', pos.latitude);
            kHtml += kv('Longitude', pos.longitude);
            kHtml += kv('Altitude', pos.altitude ? pos.altitude+'m' : null);
        } else if (p.type === 'Telemetry') {
            const d = raw.decoded?.telemetry?.deviceMetrics || {};
            const e = raw.decoded?.telemetry?.environmentMetrics || {};
            if(d.batteryLevel) kHtml += kv('Battery', d.batteryLevel+'%');
            if(d.voltage) kHtml += kv('Voltage', d.voltage.toFixed(2)+'V');
            if(e.temperature) kHtml += kv('Temp', e.temperature+'°C');
        }
        document.getElementById('sh-dt-kv').innerHTML = kHtml;

        // 3. RADIO LAYER
        let rHtml = kv('Channel Idx', p.ch);
        rHtml += kv('SNR', p.snr != null ? p.snr+' dB' : null);
        rHtml += kv('RSSI', p.rssi != null ? p.rssi+' dBm' : null);
        rHtml += kv('Hop Limit', p.hops);
        rHtml += kv('Hop Start', p.hStart);
        rHtml += kv('RX Time', new Date(p.time * 1000).toISOString());
        document.getElementById('sh-dt-radio').innerHTML = rHtml;
    },

    copyJson() {
        const pre = document.getElementById('sh-dt-raw').innerText;
        navigator.clipboard.writeText(pre);
        if(typeof window.triggerToast === 'function') window.triggerToast('JSON Copied', 'ok');
    },

    // ─────────────────── UTILS ───────────────────
    nodeName(id) {
        if (!id) return '—';
        if (id === '^all' || id === 'ffffffff') return 'BROADCAST';
        const n = window.meshState?.nodes[id];
        return n?.user?.shortName || n?.user?.longName || id;
    },

    infoLine(p) {
        const d = p.rawObj?.decoded || {};
        if (p.type === 'Message') return d.text || d.payload || '[Empty]';
        if (p.type === 'Position') return `Lat: ${d.position?.latitude?.toFixed(4) || '?'} Lon: ${d.position?.longitude?.toFixed(4) || '?'}`;
        if (p.type === 'Telemetry') return `Bat: ${d.telemetry?.deviceMetrics?.batteryLevel || '?'}%`;
        if (p.type === 'Node Info') return `Identity: ${d.user?.longName || '?'}`;
        return JSON.stringify(d).substring(0, 60) + '...';
    },

    drawSparkline() {
        const canvas = document.getElementById('sh-spark');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const W = canvas.width, H = canvas.height;
        ctx.clearRect(0, 0, W, H);

        const data = this.snrHistory;
        if (data.length < 2) return;

        const min = Math.min(...data), max = Math.max(...data);
        const range = max - min || 1;

        ctx.beginPath();
        ctx.strokeStyle = '#00c8f5';
        ctx.lineWidth = 1.5;

        data.forEach((v, i) => {
            const x = (i / (data.length - 1)) * W;
            const y = H - ((v - min) / range) * (H - 4) - 2;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
    }
};