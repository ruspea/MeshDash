#!/bin/bash
# Replay of the 2026-10-11/12 overview customisations wiped by the R3.1.11 update.
set -e
cd /opt/meshdash/static
TS=$(date +%H%M%S)
cp views/overview.html views/overview.html.bak-replay-$TS
cp js/views/overview.js  js/views/overview.js.bak-replay-$TS

# 1. Heatmap card (class-based) + its CSS
python3 - <<'EOF'
path = "/opt/meshdash/static/views/overview.html"
src = open(path).read()
block = open("/tmp/heat_html.txt").read()
start = src.find("<!-- Channel activity heatmap")
if start != -1:
    end = src.find("<!-- Avg SNR + RSSI mini chart -->")
    assert end != -1 and start < end
    src = src[:start] + block + src[end:]
else:
    marker = "                        <!-- Avg SNR + RSSI mini chart -->"
    assert src.count(marker) == 1, src.count(marker)
    src = src.replace(marker, block + marker)
anchor = ".ov-ring-lbl  { font-size: 9px; color: var(--txt3); font-family: var(--mono); margin-top: 3px; letter-spacing: .5px; }"
assert src.count(anchor) == 1, src.count(anchor)
src = src.replace(anchor, anchor + "\n" + open("/tmp/heat_css.txt").read().rstrip())
open(path, "w").write(src)
print("heatmap card ok:", len(src))
EOF

# 2. Ring wrap clearance
sed -i "s/gap: 10px; margin-bottom: 6px; }/gap: 10px; margin-bottom: 14px; }/" views/overview.html

# 3. Append the four modal script blocks (heat modal first — it carries the ovd-* CSS)
for f in heat_modal ring_modals tile_modals kpi_modals; do
python3 - "$f" <<'EOF'
import sys
f = sys.argv[1]
key = {"heat_modal": "c2OpenHeatmapDetail", "ring_modals": "c2OpenRingDetail",
       "tile_modals": "c2OpenSnrDetail", "kpi_modals": "c2OpenKpiDetail"}[f]
path = "/opt/meshdash/static/views/overview.html"
src = open(path).read()
assert key not in src, key + " already present"
src = src.rstrip() + "\n" + open("/tmp/%s.txt" % f).read() + "\n"
open(path, "w").write(src)
print(f, "appended:", len(src))
EOF
done

# 4. overview.js: FLIP reorder, dark mini-map tiles, topology engine + smooth fit
python3 /tmp/patch_flip.py
python3 /tmp/patch_ov_dark.py
python3 /tmp/patch_topo.py
python3 /tmp/patch_topo_smooth.py

echo "REPLAY DONE"