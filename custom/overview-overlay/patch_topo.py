import sys

# ── 1. overview.html — insert the topology card before the Live feed card ──
p = "/opt/meshdash/static/views/overview.html"
src = open(p).read()

anchor = "                <!-- Live feed -->"
card = """                <!-- Mesh topology -->
                <div class="card" style="flex-shrink:0;">
                    <div class="ch" style="padding-bottom:8px;">
                        <span class="ct" style="font-size:10px;">⬡ MESH TOPOLOGY</span>
                        <span style="margin-left:auto;font-size:8px;font-family:var(--mono);color:var(--txt3);" id="topo-stats">—</span>
                        <button class="btn btn-sm" id="topo-expand" title="Expand" style="padding:2px 8px;font-size:10px;">⤢</button>
                    </div>
                    <div class="cb" style="padding:4px;">
                        <canvas id="ov-topo-canvas" style="width:100%;height:170px;display:block;cursor:grab;"></canvas>
                    </div>
                </div>

"""
assert anchor in src, "live feed anchor not found"
assert "ov-topo-canvas" not in src, "card already present"
src = src.replace(anchor, card + anchor, 1)
open(p, "w").write(src)
print("overview.html: topology card inserted")

# ── 2. overview.js — append the force-graph engine ──────────────────────
p = "/opt/meshdash/static/js/views/overview.js"
src = open(p).read()
assert "MESH TOPOLOGY" not in src, "topo js already present"

topo_js = r"""

/* ════════════════════════════════════════════════════════════════════════
 * ⬡ MESH TOPOLOGY — force-directed graph of RF neighbour links
 * Data: /api/neighbors (node_id, neighbor_id, snr) + meshState node meta.
 * Card canvas (compact) + expandable full-screen modal. No dependencies.
 * ════════════════════════════════════════════════════════════════════════ */
(function() {
    const T = window._ovTopo = window._ovTopo || {
        nodes: [], links: [], byId: new Map(), raw: [],
        alpha: 1, raf: null, canvas: null, ctx: null, dpr: 1,
        view: { s: 1, ox: 0, oy: 0 },
        hover: null, drag: null, tip: null, modal: null, timer: null,
    };

    function _snrCol(snr) {
        if (snr == null) return '#7a96b2';
        if (snr > 5)   return '#00e87a';
        if (snr > 0)   return '#00c8f5';
        if (snr > -10) return '#ffa826';
        return '#ff3050';
    }

    async function _fetch() {
        const sq = (window._activeSlotId && window._activeSlotId !== 'node_0')
            ? '?slot_id=' + encodeURIComponent(window._activeSlotId) : '';
        try {
            const r = await fetch('/api/neighbors' + sq, { credentials: 'include' });
            T.raw = r.ok ? await r.json() : [];
        } catch (e) { /* keep old */ }
        _build();
    }

    function _build() {
        const mesh = Object.values(window.meshState?.nodes || {});
        const sorted = mesh.slice().sort((a, b) => (b.lastHeard || 0) - (a.lastHeard || 0)).slice(0, 200);
        const ids = new Set(sorted.map(n => n.node_id));
        T.nodes = sorted.map(n => {
            const prev = T.byId.get(n.node_id);
            return {
                id: n.node_id,
                short: (window.getMeshVal(n, 'short_name', 'shortName') || n.node_id).slice(0, 8),
                name:  window.getMeshVal(n, 'long_name', 'longName', 'short_name', 'shortName') || n.node_id,
                color: _ovColor(n.node_id),
                online: !!(n.lastHeard && (Date.now() / 1000 - n.lastHeard) < 3600),
                x: prev ? prev.x : (Math.random() - .5) * 200,
                y: prev ? prev.y : (Math.random() - .5) * 200,
                vx: 0, vy: 0, deg: 0,
            };
        });
        T.byId = new Map(T.nodes.map(n => [n.id, n]));
        const seen = new Set();
        T.links = [];
        (T.raw || []).forEach(nb => {
            if (!ids.has(nb.node_id) || !ids.has(nb.neighbor_id)) return;
            const key = [nb.node_id, nb.neighbor_id].sort().join('|');
            if (seen.has(key)) return; seen.add(key);
            T.links.push({ a: nb.node_id, b: nb.neighbor_id, snr: nb.snr, key });
        });
        T.links.forEach(l => {
            const a = T.byId.get(l.a), b = T.byId.get(l.b);
            if (a) a.deg++; if (b) b.deg++;
        });
        T.alpha = 1;
        const st = document.getElementById('topo-stats');
        if (st) st.textContent = T.nodes.length + 'N · ' + T.links.length + 'L';
    }

    function _step() {
        const N = T.nodes, L = T.links;
        const REP = 2600, K = 0.02, GRAV = 0.015, DAMP = 0.86;
        for (let i = 0; i < N.length; i++) {
            for (let j = i + 1; j < N.length; j++) {
                const a = N[i], b = N[j];
                let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
                if (d2 < 1) { d2 = 1; dx = Math.random() - .5; dy = Math.random() - .5; }
                const d = Math.sqrt(d2), f = REP / d2, fx = dx / d * f, fy = dy / d * f;
                a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
            }
        }
        L.forEach(l => {
            const a = T.byId.get(l.a), b = T.byId.get(l.b);
            if (!a || !b) return;
            const snr = Math.max(-15, Math.min(15, l.snr || 0));
            const rest = 95 - snr * 2.2;               // stronger link → closer
            const dx = b.x - a.x, dy = b.y - a.y;
            const d = Math.sqrt(dx * dx + dy * dy) || 1;
            const f = (d - rest) * K, fx = dx / d * f, fy = dy / d * f;
            a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
        });
        N.forEach(n => {
            n.vx -= n.x * GRAV; n.vy -= n.y * GRAV;
            if (T.drag === n) { n.vx = 0; n.vy = 0; return; }
            n.vx *= DAMP; n.vy *= DAMP;
            n.x += n.vx; n.y += n.vy;
        });
        T.alpha = Math.max(0.03, T.alpha * 0.996);     // gentle drift forever
    }

    function _render() {
        const cv = T.canvas, ctx = T.ctx;
        if (!cv || !ctx) return;
        const w = cv.width / T.dpr, h = cv.height / T.dpr;
        ctx.setTransform(T.dpr, 0, 0, T.dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        if (!T.nodes.length) return;
        let minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
        T.nodes.forEach(n => {
            minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
            minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
        });
        const pad = 30;
        const s = Math.min((w - pad * 2) / Math.max(1, maxX - minX), (h - pad * 2) / Math.max(1, maxY - minY));
        const ox = (w - (maxX - minX) * s) / 2 - minX * s;
        const oy = (h - (maxY - minY) * s) / 2 - minY * s;
        T.view = { s, ox, oy };
        const P = n => [n.x * s + ox, n.y * s + oy];

        L: for (const l of T.links) {
            const a = T.byId.get(l.a), b = T.byId.get(l.b);
            if (!a || !b) continue;
            const [ax, ay] = P(a), [bx, by] = P(b);
            const hov = l === T.hover;
            ctx.strokeStyle = _snrCol(l.snr);
            ctx.globalAlpha = hov ? 1 : 0.5;
            ctx.lineWidth = Math.max(0.8, 1 + Math.abs(l.snr || 0) / 9) * (hov ? 2 : 1);
            ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        }
        ctx.globalAlpha = 1;
        for (const n of T.nodes) {
            const [x, y] = P(n);
            const r = 3 + Math.min(6, n.deg * 0.7);
            ctx.beginPath(); ctx.arc(x, y, r, 0, 7);
            ctx.fillStyle = n.color;
            ctx.globalAlpha = n.online ? 1 : 0.3;
            ctx.fill();
            ctx.globalAlpha = 1;
            if (n === T.hover) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.stroke(); }
            if (T.nodes.length <= 40 || n === T.hover || n.deg >= 3) {
                ctx.font = '8px monospace'; ctx.textAlign = 'center';
                ctx.fillStyle = n === T.hover ? '#fff' : 'rgba(255,255,255,.65)';
                ctx.fillText(n.short, x, y + r + 9);
            }
        }
    }

    function _loop() {
        if (!T.canvas || !T.canvas.isConnected) { T.raf = null; return; }
        if (!document.hidden) { _step(); _render(); }
        T.raf = requestAnimationFrame(_loop);
    }

    function _kick() { if (!T.raf) T.raf = requestAnimationFrame(_loop); }

    function _size(cv) {
        const r = cv.getBoundingClientRect();
        if (r.width < 2) return;
        T.dpr = window.devicePixelRatio || 1;
        cv.width = Math.round(r.width * T.dpr);
        cv.height = Math.round(r.height * T.dpr);
    }

    /* ── hit-testing ── */
    function _pick(mx, my) {
        const { s, ox, oy } = T.view;
        let bestN = null, bestD = 1e9;
        for (const n of T.nodes) {
            const x = n.x * s + ox, y = n.y * s + oy;
            const r = 3 + Math.min(6, n.deg * 0.7) + 5;
            const d = Math.hypot(mx - x, my - y);
            if (d < r && d < bestD) { bestD = d; bestN = n; }
        }
        if (bestN) return { node: bestN };
        let bestL = null, bestLD = 1e9;
        for (const l of T.links) {
            const a = T.byId.get(l.a), b = T.byId.get(l.b);
            if (!a || !b) continue;
            const ax = a.x * s + ox, ay = a.y * s + oy;
            const bx = b.x * s + ox, by = b.y * s + oy;
            const dx = bx - ax, dy = by - ay;
            const len2 = dx * dx + dy * dy || 1;
            let t = ((mx - ax) * dx + (my - ay) * dy) / len2;
            t = Math.max(0, Math.min(1, t));
            const d = Math.hypot(mx - (ax + dx * t), my - (ay + dy * t));
            if (d < 6 && d < bestLD) { bestLD = d; bestL = l; }
        }
        return bestL ? { link: bestL } : null;
    }

    function _tipShow(html, ev) {
        if (!T.tip) {
            T.tip = document.createElement('div');
            T.tip.style.cssText = 'position:fixed;z-index:10000;pointer-events:none;background:var(--bg2,#111);border:1px solid var(--bd2,#333);border-radius:4px;padding:6px 9px;font:10px monospace;color:#ddd;white-space:nowrap;box-shadow:0 6px 20px rgba(0,0,0,.7);display:none;';
            document.body.appendChild(T.tip);
        }
        T.tip.innerHTML = html;
        T.tip.style.display = 'block';
        T.tip.style.left = Math.min(window.innerWidth - 180, ev.clientX + 14) + 'px';
        T.tip.style.top  = (ev.clientY + 14) + 'px';
    }
    function _tipHide() { if (T.tip) T.tip.style.display = 'none'; }

    function _wire(cv) {
        let down = null, moved = 0;
        const pos = ev => {
            const r = cv.getBoundingClientRect();
            return [ev.clientX - r.left, ev.clientY - r.top];
        };
        cv.addEventListener('mousemove', ev => {
            const [mx, my] = pos(ev);
            if (T.drag) {
                moved++;
                const { s, ox, oy } = T.view;
                T.drag.x = (mx - ox) / s; T.drag.y = (my - oy) / s;
                T.alpha = Math.max(T.alpha, 0.3);
                return;
            }
            const hit = _pick(mx, my);
            T.hover = hit ? (hit.node || hit.link) : null;
            cv.style.cursor = hit && hit.node ? 'grab' : 'default';
            if (hit && hit.node) _tipShow('<b style="color:' + hit.node.color + '">' + hit.node.name + '</b><br>' + hit.node.deg + ' links · ' + (hit.node.online ? 'ONLINE' : 'stale'), ev);
            else if (hit && hit.link) {
                const a = T.byId.get(hit.link.a), b = T.byId.get(hit.link.b);
                _tipShow(a.short + ' ↔ ' + b.short + '<br>SNR: <b style="color:' + _snrCol(hit.link.snr) + '">' + (hit.link.snr ?? '?') + ' dB</b>', ev);
            } else _tipHide();
        });
        cv.addEventListener('mousedown', ev => {
            const [mx, my] = pos(ev);
            const hit = _pick(mx, my);
            if (hit && hit.node) { T.drag = hit.node; moved = 0; cv.style.cursor = 'grabbing'; }
            down = [mx, my];
        });
        window.addEventListener('mouseup', () => {
            if (T.drag && moved < 4) { try { window.c2OpenNodeDetail(T.drag.id); } catch (e) {} }
            T.drag = null;
        });
        cv.addEventListener('mouseleave', () => { T.hover = null; _tipHide(); });
    }

    /* ── expand modal ── */
    function _openModal() {
        if (T.modal) { T.modal.style.display = 'flex'; return; }
        const ov = document.createElement('div');
        ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9998;display:flex;align-items:center;justify-content:center;';
        ov.innerHTML = '<div style="background:var(--bg2,#101418);border:1px solid var(--bd2,#2a3138);border-radius:6px;width:92vw;max-width:1200px;">'
            + '<div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--bd2,#2a3138);font:bold 12px monospace;letter-spacing:2px;color:var(--acc,#00c8f5);">'
            + '⬡ MESH TOPOLOGY'
            + '<span id="topo-stats-m" style="margin-left:10px;font-weight:normal;font-size:9px;color:var(--txt3,#666);"></span>'
            + '<span style="margin-left:auto;font-size:9px;font-family:monospace;color:#7a96b2;">'
            + '<span style="color:#00e87a">■</span>&gt;5dB <span style="color:#00c8f5">■</span>0..5 <span style="color:#ffa826">■</span>-10..0 <span style="color:#ff3050">■</span>&lt;-10</span>'
            + '<button id="topo-close" style="background:none;border:none;color:#888;font-size:18px;cursor:pointer;margin-left:14px;">✕</button></div>'
            + '<canvas id="ov-topo-canvas-m" style="width:100%;height:72vh;display:block;cursor:grab;border-radius:0 0 6px 6px;"></canvas></div>';
        document.body.appendChild(ov);
        T.modal = ov;
        const cvM = ov.querySelector('#ov-topo-canvas-m');
        ov.querySelector('#topo-close').onclick = () => { ov.style.display = 'none'; _attachCard(); };
        ov.addEventListener('click', ev => { if (ev.target === ov) { ov.style.display = 'none'; _attachCard(); } });
        _wire(cvM);
        T.canvas = cvM; T.ctx = cvM.getContext('2d');
        requestAnimationFrame(() => { _size(cvM); _kick(); });
        const sync = () => { const m = document.getElementById('topo-stats-m'), c = document.getElementById('topo-stats'); if (m && c) m.textContent = c.textContent; };
        const si = setInterval(sync, 2000); ov.dataset.syncTimer = si;
    }
    function _attachCard() {
        const cv = document.getElementById('ov-topo-canvas');
        if (!cv) return;
        if (T.timer) clearInterval(T.timer);
        T.timer = setInterval(() => _size(cv), 1000);
        T.canvas = cv; T.ctx = cv.getContext('2d');
        _size(cv); _kick();
    }

    /* ── boot ── */
    function _boot() {
        const cv = document.getElementById('ov-topo-canvas');
        if (!cv) { setTimeout(_boot, 500); return; }
        _wire(cv);
        _attachCard();
        const btn = document.getElementById('topo-expand');
        if (btn) btn.addEventListener('click', _openModal);
        _build();
        _fetch();
        setInterval(_fetch, 30000);
        // rebuild when the active slot changes
        const _origRender = window.c2RenderNodes;
    }
    _boot();
})();
"""

src += topo_js
open(p, "w").write(src)
print("overview.js: topology engine appended, size:", len(src))