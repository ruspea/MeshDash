window.C2CommsApp = {
    selectedNodeId: null,
    sendSlotId: 'node_0',
    pktCache: {},
    unreadCounts: {},
    _sseHooked: false,
    _pollInterval: null,
    _nodeColors: {},

    // ─── Slot-context helpers ─────────────────────────────────────────────────

    /**
     * The currently active slot ID.  Never returns 'all' — callers that need
     * to iterate all slots use _historySlotIds() instead.
     */
    _slot() {
        const s = window._activeSlotId || 'node_0';
        return s === 'all' ? 'node_0' : s;
    },

    /**
     * True if the active slot is an MQTT connection.
     * MQTT slots are observer-mode: they see DMs between OTHER nodes, so we
     * cannot filter history by "conversation with our local node".
     */
    _isMqttSlot() {
        const sid = window._activeSlotId || 'node_0';
        if (sid === 'all' || sid === 'node_0') return false;
        const info = (window._knownSlots || {})[sid];
        return info && (info.connection_type || '').toUpperCase() === 'MQTT';
    },

    /**
     * Slot IDs to query for message history.
     * In 'all' mode returns every known slot; otherwise just the active slot.
     */
    _historySlotIds() {
        if (window._activeSlotId === 'all') {
            return Object.keys(window._knownSlots || { node_0: true });
        }
        // Keep this.sendSlotId in sync — this is the only place we derive it
        return [this.sendSlotId || 'node_0'];
    },

    /**
     * Our local node ID for the active slot.
     * Returns null for MQTT observer slots where the local ID is meaningless.
     */
    _localId() {
        if (this._isMqttSlot()) {
            // MQTT: only trust local_node_id if it was explicitly configured
            const id = window.meshState.local_node_id;
            if (id && id !== '!00000000') return id;
            return null;
        }
        return window.meshState.local_node_id || null;
    },

    // ─── Init ─────────────────────────────────────────────────────────────────

    init() {
        // Clear poll interval FIRST — before any async work so rapid re-inits
        // don't leave a ghost interval running alongside the new one.
        clearInterval(this._pollInterval);
        this._pollInterval = null;

        // Reset SSE hook so the listener is re-attached to the current EventSource.
        // Without this, navigating away and back leaves the hook on a dead ES instance.
        this._sseHooked = false;

        // Sync sendSlotId to the current active slot every time the view loads
        const active = window._activeSlotId || 'node_0';
        this.sendSlotId = active === 'all' ? 'node_0' : active;

        this.selectedNodeId = null;
        this.pktCache = {};

        const log = document.getElementById('comms-log');
        if (log) log.innerHTML = '';
        const input = document.getElementById('comms-input');
        if (input) input.disabled = true;
        const sendBtn = document.getElementById('btn-comms-send');
        if (sendBtn) sendBtn.disabled = true;
        const nameEl = document.getElementById('comms-target-name');
        if (nameEl) nameEl.innerText = 'SELECT A CONTACT';
        const idEl = document.getElementById('comms-target-id');
        if (idEl) idEl.innerText = '';
        document.getElementById('comms-slot-picker-wrap')?.remove();

        Object.entries(window.meshState.dmUnread || {}).forEach(([id, count]) => {
            this.unreadCounts[id] = (this.unreadCounts[id] || 0) + count;
        });

        this.renderContacts();
        this.setupListeners();
        this._updateInternalBadge();

        // Proactively fetch nodes if missing (e.g. direct tab load)
        if (!window.meshState.nodes || Object.keys(window.meshState.nodes).length === 0) {
            const sid      = active;
            const endpoint = sid === 'all'
                ? '/api/nodes?slot_id=all'
                : `/api/slots/${encodeURIComponent(sid)}/status`;

            fetch(endpoint)
                .then(r => r.ok ? r.json() : null)
                .then(data => {
                    if (!data) return;
                    if (sid === 'all') {
                        window.meshState.nodes = data;
                    } else if (data.nodes) {
                        window.meshState.nodes = data.nodes;
                        if (data.local_node_info?.node_id) {
                            window.meshState.local_node_id = data.local_node_info.node_id;
                        }
                    }
                    this.renderContacts();
                }).catch(() => {});
        }

        // Hook into the SSE stream for real-time ACK status updates.
        // _sse.instance is the active EventSource (defined in app.js).
        // We use this instead of the bare `es` variable which is local to
        // _sse.start() and not accessible here.
        const sseInstance = window._sse?.instance;
        if (sseInstance && sseInstance.readyState !== EventSource.CLOSED && !this._sseHooked) {
            sseInstance.addEventListener('message_status_update', (e) => {
                try {
                    const data = JSON.parse(e.data);
                    this.updateMessageStatus(data.mesh_packet_id, data.status);
                } catch(err) {}
            });
            this._sseHooked = true;
        }

        clearInterval(this._pollInterval);
        this._pollInterval = setInterval(() => this.pollActiveChat(), 5000);
    },

    // ─── Badge ────────────────────────────────────────────────────────────────

    _updateInternalBadge() {
        const total = Object.values(this.unreadCounts).reduce((a, b) => a + b, 0);
        const badge = document.getElementById('dmes-total-unread-badge');
        if (!badge) return;
        if (total > 0) { badge.textContent = total; badge.style.display = ''; }
        else badge.style.display = 'none';
    },

    getColor(nodeId) {
        if (!nodeId) return 'var(--txt2)';
        if (this._nodeColors[nodeId]) return this._nodeColors[nodeId];
        // Cap cache to prevent unbounded growth on busy MQTT firehoses
        const keys = Object.keys(this._nodeColors);
        if (keys.length >= 500) delete this._nodeColors[keys[0]];
        let hash = 0;
        for (let i = 0; i < nodeId.length; i++) hash = nodeId.charCodeAt(i) + ((hash << 5) - hash);
        const color = `hsl(${Math.abs(hash % 360)}, 70%, 65%)`;
        this._nodeColors[nodeId] = color;
        return color;
    },

    // ─── Listeners ────────────────────────────────────────────────────────────

    setupListeners() {
        const input   = document.getElementById('comms-input');
        const sendBtn = document.getElementById('btn-comms-send');
        input.oninput = (e) => {
            const bytes  = new TextEncoder().encode(e.target.value).length;
            const countEl = document.getElementById('comms-char-count');
            countEl.innerText   = `${bytes}/230`;
            countEl.style.color = bytes > 200 ? (bytes > 220 ? 'var(--err)' : 'var(--warn)') : 'var(--txt3)';
        };
        input.onkeypress = (e) => { if (e.key === 'Enter') this.transmit(); };
        sendBtn.onclick  = () => this.transmit();
        document.getElementById('comms-filter').oninput = () => this.renderContacts();
    },

    // ─── Contact list ─────────────────────────────────────────────────────────

    renderContacts() {
        const list = document.getElementById('comms-contact-list');
        if (!list) return;
        const filter    = (document.getElementById('comms-filter')?.value || '').toLowerCase();
        const isAllMode = window._activeSlotId === 'all';
        const isMqtt    = this._isMqttSlot();
        const localId   = this._localId();

        const nodes = Object.values(window.meshState.nodes || {}).filter(n => {
            if (!n || !n.node_id) return false;

            if (isAllMode) {
                // All-mode: hide only the primary local node to avoid self-DM clutter
                return !(n.isLocal && n.node_id === window.meshState.local_node_id);
            }

            if (isMqtt) {
                // MQTT observer: show all nodes — we see traffic between everyone.
                // Only hide if we have an explicit node ID configured and this is it.
                if (localId) return n.node_id !== localId;
                return true;
            }

            // Serial / TCP / BLE / WebSerial: hide our own local radio
            return !n.isLocal && !n.is_local;
        });

        nodes.sort((a, b) => {
            const ua = this.unreadCounts[a.node_id] || 0;
            const ub = this.unreadCounts[b.node_id] || 0;
            if (ub !== ua) return ub - ua;
            return (b.lastHeard || 0) - (a.lastHeard || 0);
        });

        const filtered = nodes.filter(n => {
            const name = (window.getMeshVal(n, 'long_name', 'longName', 'short_name', 'shortName') || n.node_id).toLowerCase();
            return name.includes(filter) || n.node_id.toLowerCase().includes(filter);
        });

        if (!filtered.length) {
            list.innerHTML = `<div style="padding:20px;text-align:center;color:var(--txt3);font-family:var(--mono);font-size:10px;">NO CONTACTS FOUND</div>`;
            return;
        }

        list.innerHTML = filtered.map(n => {
            const isActive = n.node_id === this.selectedNodeId;
            const name     = window.getMeshVal(n, 'long_name', 'longName', 'short_name', 'shortName') || n.node_id;
            const unread   = this.unreadCounts[n.node_id] || 0;
            const timeAgo  = window.fmtTimeAgo ? window.fmtTimeAgo(n.lastHeard) : '';
            const isOnline = n.lastHeard && (Date.now() / 1000 - n.lastHeard) < 3600;
            const heardBy  = window._heardByBadge ? window._heardByBadge(n.heard_by_slot) : '';
            // Use data-nodeid attribute — never interpolate node_id into onclick handler
            // because a malicious long_name or forged node ID could contain quotes/scripts.
            return `
                <div class="ni ${isActive ? 'active' : ''}"
                     data-nodeid="${window.escapeHtml(n.node_id)}"
                     style="cursor:pointer;border-left:none;border-bottom:1px solid var(--bd);padding:12px 16px;${unread > 0 ? 'background:rgba(0,200,245,0.04);' : ''}">
                    <div style="width:10px;height:10px;border-radius:50%;background:${isOnline ? 'var(--ok)' : 'var(--bd2)'};margin-right:12px;flex-shrink:0;${isOnline ? 'box-shadow:0 0 5px var(--ok)' : ''}"></div>
                    <div style="flex:1;overflow:hidden;">
                        <div style="font-weight:bold;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:11px;${unread > 0 ? 'color:var(--acc);' : ''}">${window.escapeHtml(name)}${heardBy}</div>
                        <div style="font-size:9px;color:var(--txt3);font-family:var(--mono);">${window.escapeHtml(n.node_id)} • ${timeAgo}</div>
                    </div>
                    ${unread > 0 ? `<span class="nbadge" style="background:var(--acc);color:#000;font-weight:bold;flex-shrink:0;margin-left:8px;">${unread}</span>` : ''}
                </div>`;
        }).join('');

        // Event delegation — one listener on the container handles all contact clicks safely
        list.onclick = (e) => {
            const row = e.target.closest('[data-nodeid]');
            if (row) window.C2CommsApp.selectContact(row.dataset.nodeid);
        };
    },

    // ─── Select contact ───────────────────────────────────────────────────────

    async selectContact(nodeId) {
        this.selectedNodeId = nodeId;

        // Always re-sync sendSlotId at selection time so slot switches mid-session
        // don't leave history queries hitting the wrong DB.
        const active = window._activeSlotId || 'node_0';
        this.sendSlotId = active === 'all' ? 'node_0' : active;

        this.unreadCounts[nodeId] = 0;
        if (window.meshState.dmUnread) window.meshState.dmUnread[nodeId] = 0;
        window.updateDmNavBadge?.();
        this._updateInternalBadge();
        this.renderContacts();

        const node = window.meshState.nodes?.[nodeId];
        const name = window.getMeshVal(node, 'long_name', 'longName', 'short_name', 'shortName') || nodeId;
        document.getElementById('comms-target-name').innerText = name.toUpperCase();
        document.getElementById('comms-target-id').innerText   = nodeId;
        document.getElementById('comms-input').disabled        = false;
        document.getElementById('btn-comms-send').disabled     = false;

        // Slot picker in 'all' mode
        document.getElementById('comms-slot-picker-wrap')?.remove();
        if (active === 'all' && typeof window._buildSlotPicker === 'function') {
            const hdr = document.getElementById('comms-header');
            if (hdr) {
                const wrap = document.createElement('div');
                wrap.id = 'comms-slot-picker-wrap';
                wrap.style.cssText = 'margin-left:auto;display:flex;align-items:center;gap:6px;';
                wrap.innerHTML = `<span style="font-size:9px;color:var(--acc);font-family:var(--mono);font-weight:800;">SEND VIA</span><div id="comms-slot-picker"></div>`;
                hdr.querySelector('div')?.after(wrap);
            }
            window._buildSlotPicker('comms-slot-picker', 'node_0');
            const sel = document.getElementById('comms-slot-picker-sel');
            if (sel) { sel.onchange = null; sel.addEventListener('change', e => { this.sendSlotId = e.target.value; }); }
        }

        const isOnline = node?.lastHeard && (Date.now() / 1000 - node.lastHeard) < 3600;
        const dot  = document.getElementById('comms-target-status-dot');
        const warn = document.getElementById('comms-offline-warning');
        if (dot)  { dot.style.background  = isOnline ? 'var(--ok)' : 'var(--bd2)'; dot.style.boxShadow = isOnline ? '0 0 5px var(--ok)' : 'none'; }
        if (warn) { warn.style.display    = isOnline ? 'none' : ''; }

        this.loadHistory(nodeId);
    },

    // ─── History fetching ─────────────────────────────────────────────────────

    /**
     * Build all fetch promises needed to load history for a given nodeId.
     *
     * MQTT / observer mode:
     *   We see DMs between arbitrary nodes, not just ones addressed to us.
     *   Query: from_id=NODE (messages this node sent)
     *        + to_id=NODE   (messages addressed to this node)
     *   These two together capture the entire conversation in both directions.
     *   We do NOT cross-filter with our local ID because MQTT DMs can be
     *   !A → !B where neither end is us.
     *
     * Normal mode (Serial / TCP / BLE / WebSerial):
     *   Our local radio is one end of every DM.
     *   Query: from_id=NODE&to_id=LOCAL (messages they sent to us)
     *        + from_id=LOCAL&to_id=NODE (messages we sent to them)
     *   This matches the DB schema: to_id is always populated by save_packet.
     *
     * 'all' mode:
     *   Repeat both queries across every known slot.
     */
    _buildFetches(nodeId) {
        const isMqtt  = this._isMqttSlot();
        const localId = this._localId();
        const slots   = this._historySlotIds();
        const fetches = [];
        const enc     = encodeURIComponent;

        for (const sid of slots) {
            const sp = `&slot_id=${enc(sid)}`;

            if (isMqtt || !localId) {
                // MQTT observer / no local ID: fetch all messages involving nodeId
                fetches.push(
                    fetch(`/api/messages/history?from_id=${enc(nodeId)}&limit=100${sp}`)
                        .then(r => r.ok ? r.json() : []).catch(() => [])
                );
                fetches.push(
                    fetch(`/api/messages/history?to_id=${enc(nodeId)}&limit=100${sp}`)
                        .then(r => r.ok ? r.json() : []).catch(() => [])
                );
            } else {
                // Normal: conversation between nodeId and our local radio
                fetches.push(
                    fetch(`/api/messages/history?from_id=${enc(nodeId)}&to_id=${enc(localId)}&limit=50${sp}`)
                        .then(r => r.ok ? r.json() : []).catch(() => [])
                );
                fetches.push(
                    fetch(`/api/messages/history?from_id=${enc(localId)}&to_id=${enc(nodeId)}&limit=50${sp}`)
                        .then(r => r.ok ? r.json() : []).catch(() => [])
                );
            }
        }
        return fetches;
    },

    _dedupeAndSort(arrays, excludeBroadcasts = false) {
        const seen = new Set();
        const out  = [];
        for (const arr of arrays) {
            if (!Array.isArray(arr)) continue;
            for (const m of arr) {
                // Primary key: packet_event_id (unique per DB row, always preferred).
                // Secondary: mesh_packet_id (the Meshtastic packet ID, unique per TX).
                // Fallback: timestamp + from + to + text — includes to_id so a message
                // relayed to two different destinations is not incorrectly deduplicated,
                // and retransmits of identical text within the same second are collapsed.
                const key = m.packet_event_id ||
                            (m.mesh_packet_id  ? `pkt_${m.mesh_packet_id}` : null) ||
                            `${Math.round((m.timestamp||0)*10)}_${m.from_id||m.fromId}_${m.to_id||m.toId}_${m.text}`;
                if (seen.has(key)) continue;
                seen.add(key);
                if (excludeBroadcasts) {
                    const toId = m.to_id || m.toId || '';
                    if (toId === '^all' || toId === 'ffffffff' ||
                        Number(toId) === 4294967295 || !toId) continue;
                }
                out.push(m);
            }
        }
        return out.sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
    },

    // ─── Load history (called on contact select) ──────────────────────────────

    async loadHistory(nodeId) {
        const log = document.getElementById('comms-log');
        log.innerHTML = `<div style="color:var(--txt3);font-family:var(--mono);text-align:center;margin-top:50px;">[ INITIALIZING SECURE LINK... ]</div>`;
        try {
            const results = await Promise.all(this._buildFetches(nodeId));
            // Exclude broadcasts — the channel view handles those. A DM query for
            // from_id=X can return channel messages that node sent, we don't want those here.
            const msgs    = this._dedupeAndSort(results, true);

            log.innerHTML = '';
            if (msgs.length === 0) {
                log.innerHTML = `<div style="color:var(--txt3);font-family:var(--mono);text-align:center;margin-top:50px;">[ NO PRIOR COMMS RECORDED ]</div>`;
            } else {
                msgs.forEach(m => this.appendMessageToLog(m));
            }
            this.scrollToBottom();
        } catch (e) {
            log.innerHTML = `<div style="color:var(--err);text-align:center;margin-top:50px;">FAILED TO LOAD ARCHIVES</div>`;
        }
    },

    // ─── Poll (live updates while chat is open) ───────────────────────────────

    _pollInFlight: false,

    async pollActiveChat() {
        if (!document.getElementById('comms-log')) { clearInterval(this._pollInterval); return; }
        if (!this.selectedNodeId) return;
        // Guard: skip if a previous poll is still in flight — prevents duplicate
        // DOM insertions when the server is slow and polls overlap.
        if (this._pollInFlight) return;
        this._pollInFlight = true;

        // Re-sync sendSlotId on every poll — handles slot switches while view is open
        const active = window._activeSlotId || 'node_0';
        if (active !== 'all') this.sendSlotId = active;

        try {
            const nodeId  = this.selectedNodeId;
            const results = await Promise.all(this._buildFetches(nodeId));
            const msgs    = this._dedupeAndSort(results, true);  // exclude broadcasts

            let addedNew = false;
            msgs.forEach(m => {
                const domId1   = m.packet_event_id ? `msg-${m.packet_event_id}` : null;
                const domId2   = (m.mesh_packet_id || m.id) ? `msg-pkt-${m.mesh_packet_id || m.id}` : null;
                const existing = (domId1 && document.getElementById(domId1)) ||
                                 (domId2 && document.getElementById(domId2));
                if (!existing) {
                    // Final guard: scan visible messages for same timestamp+sender+text
                    // to catch the edge case where a message was appended optimistically
                    // on TX with a tmp ID before the poll returned the real event_id.
                    const ts  = Math.round((m.timestamp || 0) * 10);
                    const txt = (m.text || '').trim();
                    const fid = m.from_id || m.fromId || '';
                    const tid = m.to_id   || m.toId   || '';
                    if (ts && txt && document.querySelector(
                        `.mr[data-ts="${ts}"][data-from="${CSS.escape(fid)}"][data-to="${CSS.escape(tid)}"]`
                    )) return;

                    this.appendMessageToLog(m); addedNew = true;
                } else {
                    this.updateMessageStatus(m.mesh_packet_id, m.status);
                }
            });
            if (addedNew) this.scrollToBottom();
        } catch(e) {
            // Silent — poll failures are transient and logged by the browser
        } finally {
            this._pollInFlight = false;
        }
    },

    // ─── Transmit ─────────────────────────────────────────────────────────────

    async transmit() {
        const input = document.getElementById('comms-input');
        const text  = input.value.trim();
        if (!text || !this.selectedNodeId) return;

        // MQTT observer: block send if no node ID configured
        if (this._isMqttSlot() && !this._localId()) {
            window.triggerToast('MQTT observer mode — set MQTT_NODE_ID in slot config to send', 'warn');
            return;
        }

        const active = window._activeSlotId || 'node_0';
        const slotId = active === 'all'
            ? (window._slotPickerValue ? window._slotPickerValue('comms-slot-picker') : 'node_0')
            : active;
        this.sendSlotId = slotId;

        // Disable input during send to prevent double-submit
        input.disabled = true;
        document.getElementById('btn-comms-send').disabled = true;

        try {
            const res  = await fetch('/api/messages', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ message: text, destination: this.selectedNodeId, channel: 0, slot_id: slotId })
            });
            const data = await res.json();
            if (res.ok) {
                input.value = '';
                document.getElementById('comms-char-count').innerText = '0/230';
                document.getElementById('comms-char-count').style.color = 'var(--txt3)';
                const senderSlot = slotId !== 'node_0' ? slotId : null;
                const senderId   = senderSlot
                    ? Object.values(window.meshState.nodes || {}).find(n => n.isLocal && n.heard_by_slot === senderSlot)?.node_id
                        || window.meshState.local_node_id
                    : window.meshState.local_node_id;
                this.appendMessageToLog({
                    from_id:        senderId,
                    to_id:          this.selectedNodeId,
                    text,
                    timestamp:      data.timestamp,
                    mesh_packet_id: data.packet_id,
                    packet_event_id: `tx_optimistic_${data.packet_id || Date.now()}`,
                    status:         data.status === 'broadcast' ? 'DELIVERED' : 'SENT',
                });
            } else {
                window.triggerToast('Transmission failed — message preserved', 'err');
            }
        } catch (e) {
            window.triggerToast('Transmission failed — message preserved', 'err');
        } finally {
            // Re-enable regardless of outcome
            input.disabled = false;
            document.getElementById('btn-comms-send').disabled = false;
            input.focus();
        }
    },

    // ─── Message log helpers ──────────────────────────────────────────────────

    scrollToBottom() {
        const log = document.getElementById('comms-log');
        if (log && document.getElementById('comms-auto-scroll')?.checked) log.scrollTop = log.scrollHeight;
    },

    appendMessageToLog(m) {
        const log = document.getElementById('comms-log');
        if (!log) return;
        if (log.innerHTML.includes('NO PRIOR COMMS') ||
            log.innerHTML.includes('INITIALIZING SECURE LINK') ||
            log.innerHTML.includes('SELECT NODE')) {
            log.innerHTML = '';
        }

        const senderId   = m.from_id || m.fromId;
        const isMe       = window._isFromSelf
            ? window._isFromSelf(senderId)
            : senderId === window.meshState.local_node_id;
        const time       = window.fmtTime(m.timestamp || m.rxTime || (Date.now() / 1000));
        const text       = m.text || m.decoded?.text || m.decoded?.payload || '';
        const msgId      = m.mesh_packet_id || m.id || m.packet_id || '';
        const status     = m.status || 'SENT';

        const nodeSource = isMe
            ? (window.meshState.nodes?.[window.meshState.local_node_id] || window.meshState.local_node_info || {})
            : (window.meshState.nodes?.[senderId] || {});
        const color = isMe ? 'var(--acc)' : this.getColor(senderId);

        const msgDiv     = document.createElement('div');
        msgDiv.className = `mr ${isMe ? 'tx' : 'rx'}`;
        if (msgId) msgDiv.dataset.msgId = msgId;
        // Stamp dedup attributes so the poll can detect optimistic-append duplicates
        msgDiv.dataset.ts   = String(Math.round((m.timestamp || 0) * 10));
        msgDiv.dataset.from = senderId || '';
        msgDiv.dataset.to   = (m.to_id || m.toId || '');

        // ID assignment:
        // - Real DB record: use packet_event_id → "msg-EVENT_ID"
        // - Optimistic TX append (packet_event_id = "tx_optimistic_PACKETID"):
        //   use "msg-pkt-PACKETID" so the poll's domId2 lookup matches without
        //   needing the fallback data-ts selector.
        // - mesh_packet_id only: "msg-pkt-ID"
        // - Nothing: tmp random (will rely on data-ts fallback)
        const isOptimistic = (m.packet_event_id || '').startsWith('tx_optimistic_');
        if (isOptimistic && msgId) {
            msgDiv.id = `msg-pkt-${msgId}`;
        } else if (m.packet_event_id) {
            msgDiv.id = `msg-${m.packet_event_id}`;
        } else if (msgId) {
            msgDiv.id = `msg-pkt-${msgId}`;
        } else {
            msgDiv.id = `msg-tmp-${Math.random().toString(36).substr(2, 9)}`;
        }

        const nodeHeader = this._buildNodeHeader(senderId, nodeSource, color, m, isMe, status);

        msgDiv.innerHTML = `
            <div class="bub" style="${!isMe ? `border-top: 2px solid ${color};` : ''}">
                ${nodeHeader}
                <div class="m-text">${window.escapeHtml(text)}</div>
                <div class="m-foot" style="justify-content: ${isMe ? 'flex-end' : 'flex-start'}; margin-top: 6px;">
                    <span class="m-time">${time}</span>
                    ${isMe ? `<span class="ack-status" style="margin-left:6px;color:${status === 'DELIVERED' ? '#00c8f5' : status === 'FAILED' ? 'var(--err)' : 'var(--txt3)'};" title="${status}"><i class="fas ${status === 'DELIVERED' ? 'fa-check-double' : status === 'FAILED' ? 'fa-exclamation-circle' : 'fa-check'}"></i></span>` : ''}
                </div>
            </div>`;

        log.appendChild(msgDiv);
        this.scrollToBottom();
    },

    updateMessageStatus(packetId, status) {
        if (!packetId) return;
        const msgDiv = document.querySelector(`.mr[data-msg-id="${packetId}"]`);
        if (!msgDiv) return;
        const statusEl = msgDiv.querySelector('.ack-status');
        if (!statusEl) return;
        if (status === 'DELIVERED') { statusEl.innerHTML = `<i class="fas fa-check-double"></i>`; statusEl.style.color = '#00c8f5'; }
        else if (status === 'FAILED') { statusEl.innerHTML = `<i class="fas fa-exclamation-circle"></i>`; statusEl.style.color = 'var(--err)'; }

        const statusBadge = msgDiv.querySelector('.m-badge[title="Delivered"], .m-badge[title="Sent"], .m-badge[title="Failed"]');
        if (statusBadge) {
            if (status === 'DELIVERED') { statusBadge.style.color = '#00c8f5'; statusBadge.innerHTML = `<i class="fas fa-check-double"></i> DELIVERED`; statusBadge.title = 'Delivered'; }
            else if (status === 'FAILED') { statusBadge.style.color = 'var(--err)'; statusBadge.innerHTML = `<i class="fas fa-exclamation-circle"></i> FAILED`; statusBadge.title = 'Failed'; }
        }
    },

    handleAck(packetId) { this.updateMessageStatus(packetId, 'DELIVERED'); },

    // ─── Message bubble header ────────────────────────────────────────────────

    _buildNodeHeader(senderId, node, color, m, isMe, status) {
        const longName    = window.getMeshVal(node, 'long_name', 'longName') || '';
        const shortName   = window.getMeshVal(node, 'short_name', 'shortName') || '';
        const displayName = longName || shortName || (isMe ? 'You' : senderId);
        const hw          = node.hw_model || node.hwModel || '';
        const role        = node.role || '';
        const fw          = node.firmware_version || node.firmwareVersion || '';
        const isOnline    = isMe ? true : !!(node.lastHeard && (Date.now() / 1000 - node.lastHeard) < 3600);

        const snrVal  = m.rx_snr  ?? null;
        const rssiVal = m.rx_rssi ?? null;
        const battVal = node.deviceMetrics?.batteryLevel ?? node.battery_level;
        const voltVal = node.deviceMetrics?.voltage      ?? node.voltage;
        const chUtil  = node.deviceMetrics?.channelUtilization ?? node.channel_utilization;
        const airUtil = node.deviceMetrics?.airUtilTx    ?? node.air_util_tx;
        const uptime  = node.deviceMetrics?.uptimeSeconds;
        const lat     = node.position?.latitude  ?? node.latitude;
        const lon     = node.position?.longitude ?? node.longitude;
        const alt     = node.position?.altitude  ?? node.altitude;

        // MQTT source badge — shown on messages that arrived via broker
        const mqttBadge = (m.viaMqtt || m.via_mqtt || m.source === 'MQTT')
            ? `<span class="m-badge" style="color:var(--acc);" title="Via MQTT"><i class="fas fa-tower-broadcast"></i> MQTT</span>`
            : '';

        let badges = mqttBadge;
        if (battVal !== undefined && battVal !== null) {
            const bIcon = battVal > 75 ? 'fa-battery-full' : battVal > 50 ? 'fa-battery-three-quarters' : battVal > 25 ? 'fa-battery-half' : 'fa-battery-empty';
            const bCol  = battVal > 25 ? 'var(--ok)' : 'var(--err)';
            badges += `<span class="m-badge" style="color:${bCol};" title="Battery: ${battVal}%"><i class="fas ${bIcon}"></i> ${battVal}%</span>`;
        }
        if (voltVal !== undefined && voltVal !== null && voltVal > 0) {
            badges += `<span class="m-badge" style="color:var(--txt2);" title="Voltage"><i class="fas fa-bolt"></i> ${voltVal.toFixed(2)}V</span>`;
        }
        if (chUtil !== undefined && chUtil !== null) {
            badges += `<span class="m-badge" style="color:var(--txt2);" title="Channel Utilisation"><i class="fas fa-water"></i> ${chUtil.toFixed(1)}%</span>`;
        }
        if (airUtil !== undefined && airUtil !== null) {
            badges += `<span class="m-badge" style="color:var(--txt2);" title="Air Util TX"><i class="fas fa-broadcast-tower"></i> ${airUtil.toFixed(1)}%</span>`;
        }
        if (!isMe && rssiVal !== null && rssiVal !== undefined) {
            badges += `<span class="m-badge" style="color:var(--txt2);" title="RSSI"><i class="fas fa-signal"></i> ${rssiVal}dBm</span>`;
        }
        if (!isMe && snrVal !== null && snrVal !== undefined) {
            const snrCol = snrVal > 5 ? 'var(--ok)' : snrVal > 0 ? 'var(--warn)' : 'var(--err)';
            badges += `<span class="m-badge" style="color:${snrCol};" title="SNR"><i class="fas fa-chart-line"></i> ${snrVal}SNR</span>`;
        }
        if (lat !== undefined && lat !== null && lon !== undefined && lon !== null) {
            const latStr = parseFloat(lat).toFixed(4);
            const lonStr = parseFloat(lon).toFixed(4);
            const altStr = alt !== undefined && alt !== null ? ` ${alt}m` : '';
            badges += `<span class="m-badge" style="color:var(--txt2);" title="Position"><i class="fas fa-map-marker-alt"></i> ${latStr}, ${lonStr}${altStr}</span>`;
        }
        if (uptime !== undefined && uptime !== null && uptime > 0) {
            const h  = Math.floor(uptime / 3600);
            const mn = Math.floor((uptime % 3600) / 60);
            badges += `<span class="m-badge" style="color:var(--txt2);" title="Uptime"><i class="fas fa-clock"></i> ${h}h ${mn}m</span>`;
        }

        let statusBadge = '';
        if (isMe && status) {
            if (status === 'DELIVERED') statusBadge = `<span class="m-badge" style="color:#00c8f5;" title="Delivered"><i class="fas fa-check-double"></i> DELIVERED</span>`;
            else if (status === 'FAILED') statusBadge = `<span class="m-badge" style="color:var(--err);" title="Failed"><i class="fas fa-exclamation-circle"></i> FAILED</span>`;
            else statusBadge = `<span class="m-badge" style="color:var(--txt3);" title="Sent"><i class="fas fa-check"></i> SENT</span>`;
        }

        let metaLine = '';
        if (hw)   metaLine += `<span><i class="fas fa-microchip" style="margin-right:3px;"></i>${window.escapeHtml(hw)}</span>`;
        if (role) metaLine += `<span style="margin-left:8px;"><i class="fas fa-tag" style="margin-right:3px;"></i>${window.escapeHtml(role)}</span>`;
        if (fw)   metaLine += `<span style="margin-left:8px;"><i class="fas fa-code-branch" style="margin-right:3px;"></i>${window.escapeHtml(fw)}</span>`;
        if (!isMe && senderId) metaLine += `<span style="margin-left:8px;"><i class="fas fa-hashtag" style="margin-right:3px;"></i>${window.escapeHtml(senderId)}</span>`;

        const justify = isMe ? 'flex-end' : 'flex-start';

        return `
            <div style="border-bottom:1px dashed rgba(255,255,255,0.07);margin-bottom:8px;padding-bottom:8px;">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-direction:${isMe ? 'row-reverse' : 'row'};">
                    <div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${isOnline ? 'var(--ok)' : 'var(--bd2)'};${isOnline ? 'box-shadow:0 0 5px var(--ok)' : ''}"></div>
                    <span style="color:${color};font-weight:bold;font-size:13px;font-family:var(--mono);">${window.escapeHtml(displayName)}</span>
                    ${shortName && shortName !== displayName ? `<span style="color:var(--txt3);font-size:10px;font-family:var(--mono);">[${window.escapeHtml(shortName)}]</span>` : ''}
                </div>
                ${metaLine ? `<div style="font-size:9px;font-family:var(--mono);color:var(--txt3);display:flex;flex-wrap:wrap;gap:8px;justify-content:${justify};margin-bottom:4px;">${metaLine}</div>` : ''}
                ${badges || statusBadge ? `<div class="m-telemetry" style="justify-content:${justify};margin-top:4px;">${badges}${statusBadge}</div>` : ''}
            </div>`;
    },
}