import sys

path = "/opt/meshdash/static/js/views/overview.js"
src = open(path).read()

# 1. Inject the dark-tile CSS (map.js's copy only loads on the map view).
anchor = "/* ── Color palette (deterministic per nodeId) ── */"
css = """/* Keyless dark map tiles (same filter as map.js — CSS lives there only on the map view) */
(function() {
    const s = document.createElement('style');
    s.textContent = '.osm-dark-tiles { filter: invert(1) hue-rotate(180deg) brightness(.95) contrast(.9) saturate(.7); }';
    document.head.appendChild(s);
})();

"""
assert anchor in src, "palette anchor not found"
src = src.replace(anchor, css + anchor, 1)

# 2. Give the mini-map tile layer the dark class.
old_tiles = """        const _bm = window.MeshDashBasemaps || {};
        L.tileLayer(_bm.default || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom:    19,
            maxNativeZoom: 15,
            detectRetina: false,
            keepBuffer: 0,
            attribution: _bm.defaultAttribution || '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        }).addTo(map);"""
new_tiles = """        const _bm = window.MeshDashBasemaps || {};
        L.tileLayer(_bm.default || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom:    19,
            maxNativeZoom: 15,
            detectRetina: false,
            keepBuffer: 0,
            className:  'osm-dark-tiles',   // inverted-OSM dark, matches the map view
            attribution: _bm.defaultAttribution || '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        }).addTo(map);"""
assert old_tiles in src, "mini-map tileLayer not found"
src = src.replace(old_tiles, new_tiles, 1)

open(path, "w").write(src)
print("patched ok, size:", len(src))