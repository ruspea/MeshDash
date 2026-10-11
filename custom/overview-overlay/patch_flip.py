import sys

path = "/opt/meshdash/static/js/views/overview.js"
src = open(path).read()

old_same = "const sameSet     = renderedIds.length === pagedIds.length && pagedIds.every((id, i) => id === renderedIds[i]);"
new_same = """const sameSet     = renderedIds.length === pagedIds.length
                && pagedIds.every(id => renderedIds.includes(id));"""

old_if = "if (sameSet) {\n"
new_if = """if (sameSet) {
            // Order changed? Smoothly reorder existing cards (FLIP) instead of a
            // full innerHTML rebuild — keeps mini-maps/sparklines alive, no jump.
            if (pagedIds.some((id, i) => id !== renderedIds[i])) {
                const _prevTops = new Map();
                grid.querySelectorAll('.ov-nc[data-node-id]').forEach(el =>
                    _prevTops.set(el.dataset.nodeId, el.getBoundingClientRect().top));
                pagedIds.forEach(id => {
                    const card = grid.querySelector(`.ov-nc[data-node-id="${CSS.escape(id)}"]`);
                    if (card) grid.appendChild(card);   // moves (not copies) into new order
                });
                grid.querySelectorAll('.ov-nc[data-node-id]').forEach(el => {
                    const prevTop = _prevTops.get(el.dataset.nodeId);
                    if (prevTop == null) return;
                    const dy = prevTop - el.getBoundingClientRect().top;
                    if (!dy) return;
                    el.style.transition = 'none';
                    el.style.transform  = `translateY(${dy}px)`;
                    requestAnimationFrame(() => {
                        el.style.transition = 'transform .35s ease';
                        el.style.transform  = '';
                        setTimeout(() => { el.style.transition = ''; }, 400);
                    });
                });
            }
"""

assert old_same in src, "sameSet line not found"
assert src.count(old_if) == 1, "if(sameSet) not unique"
src = src.replace(old_same, new_same).replace(old_if, new_if)
open(path, "w").write(src)
print("patched ok, size:", len(src))