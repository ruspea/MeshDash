/* ═══════════════════════════════════════════════════════════════════
   Emoji Picker for MeshDash — Pure JS, no dependencies
   Injects picker buttons into all message input fields.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
    'use strict';
    if (window._emojiPickerLoaded) return;
    window._emojiPickerLoaded = true;

    // ── Emoji data (curated for mesh/radio context) ──────────────────
    const EMOJIS = {
        '😀 Smiley': ['😀','😃','😄','😁','😆','😅','🤣','😂','🙂','😉','😊','😇','🥰','😍','🤩','😘','😗','😚','😙','🥲','😋','😛','😜','🤪','😝','🤑','🤗','🤭','🫢','🤫','🤔','🫡','🤐','🤨','😐','😑','😶','🫥','😏','😒','🙄','😬','🤥','😌','😔','😪','🤤','😴','😷','🤒','🤕','🤢','🤮','🥵','🥶','🥴','😵','🤯','🤠','🥳','🥸','😎','🤓','🧐','😕','🫤','😟','🙁','😮','😯','😲','😳','🥺','🥹','😦','😧','😨','😰','😥','😢','😭','😱','😖','😣','😞','😓','😩','😫','🥱','😤','😡','😠','🤬','😈','👿','💀','☠️','💩','🤡','👹','👺','👻','👽','👾','🤖','😺','😸','😹','😻','😼','😽','🙀','😿','😾'],
        '👋 Hand': ['👋','🤚','🖐️','✋','🖖','🫱','🫲','🫳','🫴','👌','🤌','🤏','✌️','🤞','🫰','🤟','🤘','🤙','👈','👉','👆','🖕','👇','☝️','🫵','👍','👎','✊','👊','🤛','🤜','👏','🙌','🫶','👐','🤲','🤝','🙏'],
        '❤️ Hearts': ['❤️','🧡','💛','💚','💙','💜','🟤','🖤','🤍','🤎','💔','❣️','💕','💞','💓','💗','💖','💘','💝','💟'],
        '🏠 Places': ['🏠','🏡','🏢','🏣','🏤','🏥','🏦','🏨','🏩','🏪','🏫','🏬','🏭','🏯','🏰','💒','🗼','🗽','⛪','🕌','🛕','🕍','⛩️','🕋','⛺','🏕️','⛰️','🏔️','🌋','🏜️','🏝️','🏖️','🏟️','🛣️','🛤️','🗺️'],
        '🚗 Travel': ['🚗','🚕','🚙','🚌','🚎','🏎️','🚓','🚑','🚒','🚐','🛻','🚚','🚛','🚜','🏍️','🛵','🚲','🛴','🛹','🛼','🚏','🛣️','🛤️','⛽','🚨','🚥','🚦','🛑','🚧','✈️','🛩️','🚁','🛸','🚀','🛰️','🚢','⚓','⛵','🚤','🛥️','🛳️'],
        '📡 Tech': ['📱','📲','💻','⌨️','🖥️','🖨️','🖱️','🖲️','💽','💾','💿','📀','📼','📷','📸','📹','🎥','📽️','🎞️','📞','☎️','📟','📠','📺','📻','🎙️','🎚️','🎛️','🧭','⏱️','⏲️','⏰','🕰️','⌛','⏳','📡','🔋','🔌','💡','🔦','🕯️','🪔','🧯','🛡️','🔑','🗝️','🔒','🔓'],
        '⚡ Radio': ['📡','📻','📶','📳','📴','⚡','🔌','🔋','💡','🔧','🔨','⚒️','🛠️','⛏️','🔩','⚙️','🧰','🔗','⛓️','🧲','🧪','🧫','🧬','🔬','🔭','💻','📱','📟','☎️','📞','🔊','🔉','🔈','🔔','🔕','📣','📢','💬','💭','🗯️'],
        '🌤 Weather': ['☀️','🌤️','⛅','🌥️','☁️','🌦️','🌧️','⛈️','🌩️','🌨️','❄️','☃️','⛄','🌬️','💨','🌪️','🌫️','🌈','☂️','☔','⚡','🌊','💧','🔥','✨','🌟','💫','🌞','🌙','🌛','🌜','🌡️','🌪️'],
        '🔥 Misc': ['✅','❌','⭕','❗','❓','‼️','⁉️','💯','🔥','✨','🌟','💫','🌀','💠','🔖','🏷️','♻️','⚠️','🚸','🔰','🚫','🚫','Ⓜ️','🛂','🛃','🛄','🛅','🚹','🚺','🚻','🚼','🚾','🛂','🔴','🟠','🟡','🟢','🔵','🟣','🟤','⚫','⚪','🟥','🟧','🟨','🟩','🟦','🟪','⬛','⬜','🔶','🔷','🔸','🔹','🔺','🔻','💠','🔘']
    };

    const SKIN_TONES = ['', '🏻', '🏼', '🏽', '🏾', '🏿'];
    const SKIN_COLORS = ['#fbd369', '#f5c28e', '#dba47b', '#c68e5b', '#8d5e3c', '#5b3a1a'];

    let _skinTone = ''; // default: no skin tone modifier

    // ── Build picker HTML ─────────────────────────────────────────────
    function buildPicker() {
        const el = document.createElement('div');
        el.className = 'ep-picker';

        // Category tabs
        const tabs = document.createElement('div');
        tabs.className = 'ep-tabs';
        const categories = Object.keys(EMOJIS);
        const catIcons = ['😀','👋','❤️','🏠','🚗','📡','⚡','📡','🌤️','🔥'];
        categories.forEach((cat, i) => {
            const tab = document.createElement('button');
            tab.className = 'ep-tab' + (i === 0 ? ' active' : '');
            tab.textContent = catIcons[i] || cat.split(' ')[0];
            tab.title = cat;
            tab.dataset.cat = i;
            tab.addEventListener('click', () => {
                tabs.querySelectorAll('.ep-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                renderGrid(i);
            });
            tabs.appendChild(tab);
        });
        el.appendChild(tabs);

        // Search
        const search = document.createElement('div');
        search.className = 'ep-search';
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.placeholder = 'Search emoji...';
        searchInput.addEventListener('input', () => {
            const q = searchInput.value.trim().toLowerCase();
            if (!q) { renderGrid(parseInt(tabs.querySelector('.ep-tab.active').dataset.cat)); return; }
            const all = Object.values(EMOJIS).flat();
            // Simple search: just filter by recent or show all matching
            const filtered = all.filter(e => e); // all emojis match when no name search
            renderEmojis(filtered);
        });
        search.appendChild(searchInput);
        el.appendChild(search);

        // Grid
        const grid = document.createElement('div');
        grid.className = 'ep-grid';
        el.appendChild(grid);

        // Skin tone
        const skinBar = document.createElement('div');
        skinBar.className = 'ep-skin';
        SKIN_COLORS.forEach((color, i) => {
            const btn = document.createElement('button');
            btn.className = 'ep-skin-btn' + (i === 0 ? ' active' : '');
            btn.style.background = color;
            btn.title = i === 0 ? 'Default' : `Skin tone ${i}`;
            btn.addEventListener('click', () => {
                _skinTone = SKIN_TONES[i];
                skinBar.querySelectorAll('.ep-skin-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
            });
            skinBar.appendChild(btn);
        });
        el.appendChild(skinBar);

        el._grid = grid;
        el._search = searchInput;
        return el;
    }

    function renderGrid(catIndex) {
        const categories = Object.keys(EMOJIS);
        const cat = categories[catIndex];
        renderEmojis(EMOJIS[cat]);
    }

    function renderEmojis(emojis) {
        // Find the active picker
        const picker = document.querySelector('.ep-picker.open');
        if (!picker || !picker._grid) return;
        picker._grid.innerHTML = '';
        emojis.forEach(emoji => {
            const span = document.createElement('span');
            span.className = 'ep-emoji';
            span.textContent = emoji;
            span.addEventListener('click', () => insertEmoji(emoji));
            picker._grid.appendChild(span);
        });
    }

    // ── Insert emoji into focused input ────────────────────────────────
    let _lastFocused = null;

    function insertEmoji(emoji) {
        const finalEmoji = _skinTone ? emoji + _skinTone : emoji;

        // Use the tracked last-focused input
        const input = _lastFocused;
        if (!input) return;

        if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
            const start = input.selectionStart || input.value.length;
            const end = input.selectionEnd || input.value.length;
            const before = input.value.substring(0, start);
            const after = input.value.substring(end);
            input.value = before + finalEmoji + after;
            input.selectionStart = input.selectionEnd = start + finalEmoji.length;
            // Fire input event so any listeners pick it up
            input.dispatchEvent(new Event('input', { bubbles: true }));
        }

        // Close picker after insertion (mobile-friendly)
        closeAllPickers();
    }

    function closeAllPickers() {
        document.querySelectorAll('.ep-picker.open').forEach(p => {
            p.classList.remove('open');
        });
    }

    // ── Inject picker button next to message inputs ───────────────────
    const MESSAGE_INPUTS = [
        '#channels-input',     // Broadcast
        '#comms-input',        // DM / Secure message
        '#c2-task-msg',        // Task message (textarea)
        '#compose-body',       // MeshBB bulletin (textarea)
        '#ep-demo',            // Demo on emoji picker settings page
    ];

    // Also handle dynamically-created inputs
    const MESSAGE_INPUT_SELECTORS = MESSAGE_INPUTS.join(',');

    function injectTrigger(inputEl) {
        if (inputEl._epTrigger) return; // already injected

        // Find or create a wrapper
        const parent = inputEl.parentElement;
        if (!parent) return;

        // Create trigger button
        const btn = document.createElement('button');
        btn.className = 'ep-trigger';
        btn.textContent = '😀';
        btn.title = 'Emoji picker';
        btn.type = 'button';

        // Build picker instance for this input
        const picker = buildPicker();
        document.body.appendChild(picker);

        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            const isOpen = picker.classList.contains('open');
            closeAllPickers();
            if (!isOpen) {
                // Position near the button
                const rect = btn.getBoundingClientRect();
                picker.style.top = (rect.bottom + 4) + 'px';
                // Keep picker within viewport
                const pickerWidth = 340;
                let left = rect.left;
                if (left + pickerWidth > window.innerWidth) {
                    left = window.innerWidth - pickerWidth - 8;
                }
                if (left < 8) left = 8;
                picker.style.left = left + 'px';
                picker.classList.add('open');
                renderGrid(0); // show first category
                _lastFocused = inputEl;
                inputEl.focus();
            }
        });

        // Track focus on input
        inputEl.addEventListener('focus', () => { _lastFocused = inputEl; });
        inputEl.addEventListener('click', () => { _lastFocused = inputEl; });

        // Insert trigger button after input (or at end of parent)
        if (inputEl.nextSibling) {
            parent.insertBefore(btn, inputEl.nextSibling);
        } else {
            parent.appendChild(btn);
        }

        inputEl._epTrigger = btn;
        inputEl._epPicker = picker;
    }

    // ── Close picker when clicking outside ─────────────────────────────
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.ep-picker') && !e.target.closest('.ep-trigger')) {
            closeAllPickers();
        }
    });

    // Close on Escape
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeAllPickers();
    });

    // ── Initial injection ──────────────────────────────────────────────
    function injectAll() {
        MESSAGE_INPUTS.forEach(sel => {
            const el = document.querySelector(sel);
            if (el) injectTrigger(el);
        });
    }

    // Inject after a short delay to let the DOM settle
    setTimeout(injectAll, 500);

    // Also inject when views change (SPA navigation)
    const _origLoadView = window.loadView;
    if (_origLoadView) {
        window.loadView = function (view) {
            const result = _origLoadView.call(this, view);
            setTimeout(injectAll, 300);
            return result;
        };
    }

    // MutationObserver for dynamically-added inputs
    const observer = new MutationObserver(() => {
        MESSAGE_INPUTS.forEach(sel => {
            const el = document.querySelector(sel);
            if (el && !el._epTrigger) injectTrigger(el);
        });
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Expose for debug
    window.EmojiPicker = { inject: injectAll, close: closeAllPickers };

})();