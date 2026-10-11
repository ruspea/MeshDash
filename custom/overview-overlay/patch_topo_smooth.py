import sys

path = "/opt/meshdash/static/js/views/overview.js"
src = open(path).read()

# 1. Warm the simulation headlessly before first paint — no visible scatter.
old_build = """        T.alpha = 1;
        const st = document.getElementById('topo-stats');"""
new_build = """        T.alpha = 1;
        if (!T.warmed) { T.warmed = true; for (let i = 0; i < 200; i++) _step(); }
        const st = document.getElementById('topo-stats');"""
assert old_build in src, "build anchor not found"
src = src.replace(old_build, new_build, 1)

# 2. Ease the view transform toward the fit instead of snapping every frame.
old_fit = """        const pad = 30;
        const s = Math.min((w - pad * 2) / Math.max(1, maxX - minX), (h - pad * 2) / Math.max(1, maxY - minY));
        const ox = (w - (maxX - minX) * s) / 2 - minX * s;
        const oy = (h - (maxY - minY) * s) / 2 - minY * s;
        T.view = { s, ox, oy };
        const P = n => [n.x * s + ox, n.y * s + oy];"""
new_fit = """        const pad = 30;
        const ts  = Math.min((w - pad * 2) / Math.max(1, maxX - minX), (h - pad * 2) / Math.max(1, maxY - minY));
        const tox = (w - (maxX - minX) * ts) / 2 - minX * ts;
        const toy = (h - (maxY - minY) * ts) / 2 - minY * ts;
        const V = T.view;
        if (!V.init) { V.s = ts; V.ox = tox; V.oy = toy; V.init = true; }
        else { V.s += (ts - V.s) * 0.12; V.ox += (tox - V.ox) * 0.12; V.oy += (toy - V.oy) * 0.12; }
        const s = V.s, ox = V.ox, oy = V.oy;
        const P = n => [n.x * s + ox, n.y * s + oy];"""
assert old_fit in src, "fit block not found"
src = src.replace(old_fit, new_fit, 1)

# 3. Canvas resize (card <-> modal) should snap the view once, not lerp.
old_size = """    function _size(cv) {
        const r = cv.getBoundingClientRect();
        if (r.width < 2) return;"""
new_size = """    function _size(cv) {
        const r = cv.getBoundingClientRect();
        if (r.width < 2) return;
        T.view.init = false;   // snap fit once on (re)attach/resize"""
assert old_size in src, "size fn not found"
src = src.replace(old_size, new_size, 1)

open(path, "w").write(src)
print("patched ok, size:", len(src))