window.C2ChannelsApp = {
    selectedChannelIdx: null,
    sendSlotId: 'node_0',
    channelsConfig: {},
    unreadCounts: {},
    _pollInterval: null,
    _nodeColors: {},

    // ─── Slot helpers ──────────────────────────────────────────────────────────

    _isMqttSlot() {
        const sid = window._activeSlotId || 'node_0';
        if (sid === 'all' || sid === 'node_0') return false;
        const info = (window._knownSlots || {})[sid];
        return info && (info.connection_type || '').toUpperCase() === 'MQTT';
    },

    _historySlotIds() {
        if (window._activeSlotId === 'all') {
            return Object.keys(window._knownSlots || { node_0: true });
        }
        return [this.sendSlotId || 'node_0'];
    },

    // ─── Init ─────────────────────────────────────────────────────────────────

    async init() {
        // Clear interval FIRST before any async work
        clearInterval(this._pollInterval);
        this._pollInterval = null;

        const active = window._activeSlotId || 'node_0';
        this.sendSlotId = active === 'all' ? 'node_0' : active;
        this.selectedChannelIdx = null;

        const log = document.getElementById('channels-log');
        if (log) log.innerHTML = '';
        const input = document.getElementById('channels-input');
        if (input) input.disabled = true;
        const sendBtn = document.getElementById('btn-channels-send');
        if (sendBtn) sendBtn.disabled = true;
        document.getElementById('ch-slot-picker-wrap')?.remove();

        const global = window.meshState.channelUnread || {};
        Object.entries(global).forEach(([idx, count]) => {
            const key = Number(idx);
            this.unreadCounts[key] = (this.unreadCounts[key] || 0) + count;
        });

        this.setupListeners();
        await this.fetchChannels();
        this._updateInternalBadge();
        clearInterval(this._pollInterval);
        this._pollInterval = setInterval(() => this.pollActiveChannel(), 5000);
    },

    _updateInternalBadge() {
        const total = Object.values(this.unreadCounts).reduce((a, b) => a + b, 0);
        const badge = document.getElementById('channels-total-unread-badge');
        if (!badge) return;
        if (total > 0) { badge.textContent = total; badge.style.display = ''; }
        else badge.style.display = 'none';
    },

    getColor(nodeId) {
        if (!nodeId) return 'var(--txt2)';
        if (this._nodeColors[nodeId]) return this._nodeColors[nodeId];
        // Cap to prevent unbounded growth on busy MQTT firehoses
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
        const input   = document.getElementById('channels-input');
        const sendBtn = document.getElementById('btn-channels-send');
        input.oninput = (e) => {
            const len     = e.target.value.length;
            const countEl = document.getElementById('channels-char-count');
            countEl.innerText   = `${len}/230`;
            countEl.style.color = len > 200 ? 'var(--err)' : 'var(--txt3)';
        };
        input.onkeypress = (e) => { if (e.key === 'Enter') this.transmit(); };
        sendBtn.onclick  = () => this.transmit();
    },

    // ─── Fetch channels config ────────────────────────────────────────────────

    async fetchChannels() {
        try {
            const isMqtt = this._isMqttSlot();
            const active = window._activeSlotId || 'node_0';

            // For MQTT slots the /api/channels endpoint may return nothing useful
            // (no meshtastic interface). We synthesise a channel list from the
            // MQTT topic subscription and any channels we've seen traffic on.
            const slotForChannels = (active === 'all') ? 'node_0' : active;
            let arr = [];
            try {
                arr = await fetch(`/api/channels?slot_id=${encodeURIComponent(slotForChannels)}`).then(r => r.json());
            } catch(e) { arr = []; }

            this.channelsConfig = {};
            if (Array.isArray(arr)) {
                arr.forEach(c => {
                    if (c?.index != null && c.role !== 'DISABLED' && c.role !== '0') {
                        this.channelsConfig[c.index] = c;
                    }
                });
            }

            if (isMqtt) {
                // For MQTT: supplement with channels discovered from message history.
                // Query all distinct channel indices that have broadcast messages.
                await this._discoverMqttChannels();
            }

            if (!Object.keys(this.channelsConfig).length) {
                // Fallback: always show at least channel 0 (LongFast / primary)
                this.channelsConfig[0] = {
                    index:    0,
                    name:     'LongFast',
                    role:     'PRIMARY',
                    has_key:  true,   // will attempt default key auto-decrypt
                    psk:      'AQ==', // well-known Meshtastic default
                };
            }

            this.renderSidebar();
            if (this.selectedChannelIdx === null) {
                const firstIdx = Object.keys(this.channelsConfig).map(Number).sort((a, b) => a - b)[0];
                if (firstIdx !== undefined) this.selectChannel(firstIdx);
            }
        } catch (e) {
            const list = document.getElementById('channels-list');
            if (list) list.innerHTML = `<div style="padding:20px;color:var(--err);text-align:center;">Failed to load channels</div>`;
        }
    },

    /**
     * For MQTT slots: query distinct channel indices from the history DB
     * and add any we haven't seen in the API response yet.
     * This ensures channels that nodes broadcast on are always listed.
     */
    async _discoverMqttChannels() {
        const slots = this._historySlotIds();
        // Parallel — sequential for-await is 5-8× slower with multiple slots
        const results = await Promise.all(slots.map(sid =>
            fetch(`/api/messages/history?limit=200&to_id=%5Eall&slot_id=${encodeURIComponent(sid)}`)
                .then(r => r.ok ? r.json() : []).catch(() => [])
        ));
        results.forEach(msgs => {
            if (!Array.isArray(msgs)) return;
            msgs.forEach(m => {
                const ch = m.channel ?? 0;
                if (!this.channelsConfig[ch]) {
                    this.channelsConfig[ch] = {
                        index:   ch,
                        name:    ch === 0 ? 'LongFast' : `Channel ${ch}`,
                        role:    ch === 0 ? 'PRIMARY' : 'SECONDARY',
                        has_key: true,
                        psk:     'AQ==',
                    };
                }
            });
        });
    },

    // ─── Sidebar ──────────────────────────────────────────────────────────────

    renderSidebar() {
        const list   = document.getElementById('channels-list');
        if (!list) return;
        const isMqtt = this._isMqttSlot();
        const keys   = Object.keys(this.channelsConfig).sort((a, b) => Number(a) - Number(b));

        list.innerHTML = keys.map(idx => {
            const ch       = this.channelsConfig[idx];
            const isActive = Number(this.selectedChannelIdx) === Number(idx);
            const unread   = this.unreadCounts[Number(idx)] || 0;
            const hasKey   = ch.has_key !== false;
            const isPrimary = ch.role === 'PRIMARY' || ch.role === '1';

            const encIcon = hasKey
                ? '<i class="fas fa-lock" style="color:var(--ok);"></i>'
                : '<i class="fas fa-lock" style="color:var(--err);"></i> <span style="color:var(--err);">LOCKED</span>';

            const roleBadge = isPrimary
                ? `<span style="background:var(--acc);color:var(--bg);padding:2px 4px;border-radius:2px;font-size:8px;font-weight:bold;">PRI</span>`
                : isMqtt
                    ? `<span style="background:rgba(0,200,245,.2);color:var(--acc);padding:2px 4px;border-radius:2px;font-size:8px;font-weight:bold;">MQTT</span>`
                    : `<span style="background:var(--bd2);padding:2px 4px;border-radius:2px;font-size:8px;">SEC</span>`;

            return `
                <div class="ni ${isActive ? 'active' : ''}"
                     onclick="window.C2ChannelsApp.selectChannel(${Number(idx)})"
                     style="border-left:none;border-bottom:1px solid var(--bd);padding:12px 16px;flex-direction:column;align-items:flex-start;gap:6px;${unread > 0 ? 'background:rgba(255,168,38,0.04);' : ''}">
                    <div style="display:flex;justify-content:space-between;width:100%;align-items:center;">
                        <div style="font-weight:bold;font-size:12px;color:${unread > 0 ? 'var(--warn)' : 'var(--txt)'};">
                            <span style="color:var(--txt3);">#</span> ${window.escapeHtml(ch.name || 'Channel ' + idx)}
                        </div>
                        ${unread > 0 ? `<span class="nbadge" style="background:var(--warn);color:#000;font-weight:bold;flex-shrink:0;">${unread}</span>` : ''}
                    </div>
                    <div style="display:flex;gap:8px;font-family:var(--mono);font-size:9px;align-items:center;">
                        ${roleBadge}
                        <span title="Encryption">${encIcon}</span>
                        <span style="color:var(--txt3);margin-left:auto;">${idx}</span>
                    </div>
                </div>`;
        }).join('');
    },

    // ─── Select channel ───────────────────────────────────────────────────────

    async selectChannel(idx) {
        // Normalise to number — idx may arrive as string from onclick or number from auto-select
        const idxNum = Number(idx);
        this.selectedChannelIdx = idxNum;

        // Re-sync sendSlotId on every selection
        const active = window._activeSlotId || 'node_0';
        this.sendSlotId = active === 'all' ? 'node_0' : active;

        this.unreadCounts[idxNum] = 0;
        if (window.meshState.channelUnread) window.meshState.channelUnread[idxNum] = 0;
        window.updateChannelNavBadge?.();
        this._updateInternalBadge();
        this.renderSidebar();

        const isMqtt = this._isMqttSlot();
        const ch     = this.channelsConfig[idxNum];
        const hasKey = ch?.has_key !== false;

        document.getElementById('channels-target-name').innerHTML =
            `${isMqtt ? 'TOPIC' : 'CH'}_${idxNum} <span style="color:var(--txt3);">//</span> ${window.escapeHtml(ch?.name || 'UNKNOWN').toUpperCase()}`;

        document.getElementById('channels-target-meta').innerHTML = `
            <span>ROLE: ${window.escapeHtml(ch?.role || (isMqtt ? 'MQTT' : 'UNKNOWN'))}</span>
            <span>UPLINK: ${ch?.uplink ? 'ON' : 'OFF'}</span>
            <span>DOWNLINK: ${ch?.downlink ? 'ON' : 'OFF'}</span>
            ${isMqtt ? '<span style="color:var(--acc);">📡 MQTT SOURCE</span>' : ''}`;

        const inputArea = document.getElementById('channels-input-area');
        const keyArea   = document.getElementById('channels-key-area');
        const hint      = document.getElementById('channels-encryption-hint');

        if (hasKey) {
            inputArea.style.display = 'flex';
            keyArea.style.display   = 'none';
            document.getElementById('channels-input').disabled   = false;
            document.getElementById('btn-channels-send').disabled = false;
            const keyNote = (ch?.psk === 'AQ==' || isMqtt)
                ? ' <span style="color:var(--acc);font-size:9px;">(DEFAULT KEY)</span>'
                : '';
            hint.innerHTML = `<i class="fas fa-lock" style="color:var(--ok);"></i> AES-256 ENCRYPTED BROADCAST${keyNote}`;
        } else {
            inputArea.style.display = 'none';
            keyArea.style.display   = 'flex';
            hint.innerHTML = '<i class="fas fa-lock" style="color:var(--err);"></i> MISSING DECRYPTION KEY — enter PSK below';

            // Pre-fill with default key hint
            const keyInput = document.getElementById('channels-key-input');
            if (keyInput && !keyInput.value) keyInput.placeholder = 'e.g. AQ== (default) or your custom Base64 PSK';

            document.getElementById('btn-channels-save-key').onclick = async () => {
                const psk = document.getElementById('channels-key-input').value.trim();
                if (!psk) return;
                try {
                    const res = await fetch('/api/mqtt/channel_key', {
                        method:  'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window._csrfToken || '' },
                        body:    JSON.stringify({
                            slot_id:    this.sendSlotId,
                            channel_id: ch?.name || String(idxNum),
                            channel_idx: idxNum,
                            psk,
                        }),
                    });
                    if (res.ok) {
                        // Mark channel as having a key and re-render
                        if (this.channelsConfig[idxNum]) {
                            this.channelsConfig[idxNum].has_key = true;
                            this.channelsConfig[idxNum].psk     = psk;
                        }
                        document.getElementById('channels-key-input').value = '';
                        window.triggerToast('Key saved — reloading history', 'ok');
                        this.renderSidebar();
                        this.selectChannel(idxNum);
                    } else {
                        window.triggerToast('Failed to save key', 'err');
                    }
                } catch (e) {
                    window.triggerToast('Failed to save key', 'err');
                }
            };
        }

        // Slot picker for 'all' mode
        document.getElementById('ch-slot-picker-wrap')?.remove();
        if (active === 'all' && typeof window._buildSlotPicker === 'function') {
            const hdr = document.getElementById('channels-header');
            if (hdr) {
                const wrap = document.createElement('div');
                wrap.id = 'ch-slot-picker-wrap';
                wrap.style.cssText = 'margin-left:auto;display:flex;align-items:center;gap:6px;';
                wrap.innerHTML = `<span style="font-size:9px;color:var(--acc);font-family:var(--mono);font-weight:800;">SEND VIA</span><div id="ch-slot-picker"></div>`;
                hdr.appendChild(wrap);
                window._buildSlotPicker('ch-slot-picker', 'node_0');
                const sel = document.getElementById('ch-slot-picker-sel');
                if (sel) { sel.onchange = null; sel.addEventListener('change', e => { this.sendSlotId = e.target.value; }); }
            }
        }
        this.loadHistory(idxNum);
    },

    // ─── History loading ──────────────────────────────────────────────────────

    /**
     * Build fetch promises for channel broadcast history.
     * Key fix: use Number comparison throughout, not === on mixed string/int.
     * For MQTT: also include messages with encrypted=true so they show as redacted.
     */
    _buildChannelFetches(idxNum) {
        const slots   = this._historySlotIds();
        const fetches = [];
        for (const sid of slots) {
            fetches.push(
                fetch(`/api/messages/history?limit=100&channel=${idxNum}&slot_id=${encodeURIComponent(sid)}`)
                    .then(r => r.ok ? r.json() : []).catch(() => [])
            );
        }
        return fetches;
    },

    _filterBroadcasts(msgs, idxNum) {
        const seen = new Set();
        return msgs
            .filter(m => {
                // Primary: packet_event_id. Secondary: mesh_packet_id.
                // Fallback: timestamp (10ths of second) + from + channel + text.
                // Including channel prevents cross-channel dedup of identical text.
                const key = m.packet_event_id ||
                            (m.mesh_packet_id ? `pkt_${m.mesh_packet_id}` : null) ||
                            `${Math.round((m.timestamp||0)*10)}_${m.from_id||m.fromId}_ch${m.channel??0}_${m.text}`;
                if (seen.has(key)) return false;
                seen.add(key);
                const toId        = m.to_id || m.toId || '';
                const isBroadcast = !toId || toId === '^all' || toId === 'ffffffff' ||
                                    Number(toId) === 4294967295;
                const chMatch = Number(m.channel ?? 0) === Number(idxNum);
                return isBroadcast && chMatch;
            })
            .sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
    },

    async loadHistory(idxNum) {
        const log = document.getElementById('channels-log');
        log.innerHTML = `<div style="color:var(--txt3);font-family:var(--mono);text-align:center;margin-top:50px;">[ TUNING TO FREQUENCY... ]</div>`;
        try {
            const results    = await Promise.all(this._buildChannelFetches(Number(idxNum)));
            const broadcasts = this._filterBroadcasts(results.flat(), Number(idxNum));

            log.innerHTML = '';
            if (broadcasts.length === 0) {
                log.innerHTML = `<div style="color:var(--txt3);font-family:var(--mono);text-align:center;margin-top:50px;">[ NO BROADCASTS RECORDED ON CH_${idxNum} ]</div>`;
            } else {
                broadcasts.forEach(m => this.appendMessageToLog(m));
            }
            this.scrollToBottom();
        } catch (e) {
            log.innerHTML = `<div style="color:var(--err);text-align:center;margin-top:50px;font-family:var(--mono);">[ FAILED TO DECODE HISTORY ]</div>`;
        }
    },

    // ─── Poll (live updates while channel open) ───────────────────────────────

    _pollInFlight: false,

    async pollActiveChannel() {
        if (!document.getElementById('channels-log')) { clearInterval(this._pollInterval); return; }
        if (this.selectedChannelIdx === null) return;
        // Guard against concurrent polls when server is slow
        if (this._pollInFlight) return;
        this._pollInFlight = true;

        // Re-sync sendSlotId on every poll
        const active = window._activeSlotId || 'node_0';
        if (active !== 'all') this.sendSlotId = active;

        try {
            const idxNum  = Number(this.selectedChannelIdx);
            const results = await Promise.all(this._buildChannelFetches(idxNum));
            const broadcasts = this._filterBroadcasts(results.flat(), idxNum);

            let addedNew = false;
            broadcasts.forEach(m => {
                const domId1   = m.packet_event_id ? `msg-${m.packet_event_id}` : null;
                const domId2   = (m.mesh_packet_id || m.id) ? `msg-pkt-${m.mesh_packet_id || m.id}` : null;
                const existing = (domId1 && document.getElementById(domId1)) ||
                                 (domId2 && document.getElementById(domId2));
                if (!existing) {
                    const ts  = Math.round((m.timestamp || 0) * 10);
                    const fid = m.from_id || m.fromId || '';
                    const ch  = String(m.channel ?? 0);
                    const txt = (m.text || '').trim();
                    if (ts && txt && document.querySelector(
                        `.mr[data-ts="${ts}"][data-from="${CSS.escape(fid)}"][data-ch="${ch}"]`
                    )) return;
                    this.appendMessageToLog(m); addedNew = true;
                }
            });
            if (addedNew) this.scrollToBottom();
        } catch(e) {
            // Silent — transient poll failures don't need user-visible errors
        } finally {
            this._pollInFlight = false;
        }
    },

    // ─── Message log ─────────────────────────────────────────────────────────

    appendMessageToLog(m) {
        const log = document.getElementById('channels-log');
        if (!log) return;
        if (log.innerHTML.includes('NO BROADCASTS') ||
            log.innerHTML.includes('TUNING TO FREQUENCY') ||
            log.innerHTML.includes('SELECT A CHANNEL')) {
            log.innerHTML = '';
        }

        const senderId = m.fromId || m.from_id;
        const isMe     = senderId === 'me' || (window._isFromSelf ? window._isFromSelf(senderId) : senderId === window.meshState.local_node_id);
        const color    = isMe ? 'var(--acc)' : this.getColor(senderId);
        const time     = window.fmtTime(m.timestamp || m.rxTime || (Date.now() / 1000));
        const msgId    = m.mesh_packet_id || m.id || m.packet_id || '';

        // Encrypted packets that the server couldn't decrypt — show redacted
        const isEncrypted = m.encrypted === true || m.encrypted === 1;
        const text = isEncrypted
            ? null
            : (m.text || m.decoded?.text || m.decoded?.payload || '');

        const nodeSource = isMe
            ? (window.meshState.nodes?.[window.meshState.local_node_id] || window.meshState.local_node_info || {})
            : (window.meshState.nodes?.[senderId] || {});

        const nodeHeader = this._buildNodeHeader(senderId, nodeSource, color, m, isMe);

        const msgDiv     = document.createElement('div');
        msgDiv.className = `mr ${isMe ? 'tx' : 'rx'}`;
        if (msgId) msgDiv.dataset.msgId = msgId;
        msgDiv.dataset.ts   = String(Math.round((m.timestamp || 0) * 10));
        msgDiv.dataset.from = senderId || '';
        msgDiv.dataset.ch   = String(m.channel ?? 0);

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

        const textContent = isEncrypted
            ? `<div class="m-text" style="color:var(--txt3);font-style:italic;font-family:var(--mono);font-size:11px;"><i class="fas fa-lock" style="margin-right:6px;color:var(--err);"></i>[ ENCRYPTED — PSK required to decrypt ]</div>`
            : `<div class="m-text">${window.escapeHtml(text || '')}</div>`;

        // MQTT source tag
        const mqttBadge = (m.viaMqtt || m.via_mqtt || m.source === 'MQTT')
            ? `<span class="m-badge" style="color:var(--acc);margin-top:4px;" title="Via MQTT"><i class="fas fa-tower-broadcast"></i> MQTT</span>`
            : '';

        msgDiv.innerHTML = `
            <div class="bub" style="${!isMe ? `border-top: 2px solid ${color};` : ''}">
                ${nodeHeader}
                ${textContent}
                ${mqttBadge}
                <div class="m-foot" style="justify-content: ${isMe ? 'flex-end' : 'flex-start'}; margin-top: 6px;">
                    <span class="m-time">${time}</span>
                </div>
            </div>`;

        log.appendChild(msgDiv);
        this.scrollToBottom();
    },

    // ─── Transmit ─────────────────────────────────────────────────────────────

    async transmit() {
        const input = document.getElementById('channels-input');
        const text  = input.value.trim();
        if (!text || this.selectedChannelIdx === null) return;

        // MQTT observer: block if no node ID configured
        if (this._isMqttSlot()) {
            const localId = window.meshState.local_node_id;
            if (!localId || localId === '!00000000') {
                window.triggerToast('MQTT observer mode — set MQTT_NODE_ID to broadcast', 'warn');
                return;
            }
        }

        const active = window._activeSlotId || 'node_0';
        const slotId = active === 'all'
            ? (window._slotPickerValue ? window._slotPickerValue('ch-slot-picker') : 'node_0')
            : active;
        this.sendSlotId = slotId;

        // Disable during send to prevent double-submit
        input.disabled = true;
        document.getElementById('btn-channels-send').disabled = true;

        try {
            const res  = await fetch('/api/messages', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window._csrfToken || '' },
                body:    JSON.stringify({ message: text, destination: '^all', channel: this.selectedChannelIdx, slot_id: slotId })
            });
            const data = await res.json();
            if (res.ok) {
                // Only clear on success
                input.value = '';
                document.getElementById('channels-char-count').innerText = '0/230';
                this.appendMessageToLog({
                    from_id:         window.meshState.local_node_id,
                    to_id:           '^all',
                    text,
                    timestamp:       data.timestamp,
                    mesh_packet_id:  data.packet_id,
                    packet_event_id: `tx_optimistic_${data.packet_id || Date.now()}`,
                    channel:         this.selectedChannelIdx,
                    status:          'DELIVERED',
                });
            } else {
                window.triggerToast('Broadcast failed — message preserved', 'err');
            }
        } catch (e) {
            window.triggerToast('Broadcast failed — message preserved', 'err');
        } finally {
            input.disabled = false;
            document.getElementById('btn-channels-send').disabled = false;
            input.focus();
        }
    },

    scrollToBottom() {
        const log = document.getElementById('channels-log');
        if (log && document.getElementById('channels-auto-scroll')?.checked) log.scrollTop = log.scrollHeight;
    },

    // ─── Node header for message bubbles ─────────────────────────────────────

    _buildNodeHeader(senderId, node, color, m, isMe) {
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

        let badges = '';
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
        if (isMe) {
            badges += `<span class="m-badge" style="color:var(--warn);" title="Broadcast"><i class="fas fa-satellite-dish"></i> BROADCAST</span>`;
        }

        let metaLine = '';
        if (hw)    metaLine += `<span><i class="fas fa-microchip" style="margin-right:3px;"></i>${window.escapeHtml(hw)}</span>`;
        if (role)  metaLine += `<span style="margin-left:8px;"><i class="fas fa-tag" style="margin-right:3px;"></i>${window.escapeHtml(role)}</span>`;
        if (fw)    metaLine += `<span style="margin-left:8px;"><i class="fas fa-code-branch" style="margin-right:3px;"></i>${window.escapeHtml(fw)}</span>`;
        if (!isMe) metaLine += `<span style="margin-left:8px;"><i class="fas fa-hashtag" style="margin-right:3px;"></i>${window.escapeHtml(senderId || '?')}</span>`;

        const justify = isMe ? 'flex-end' : 'flex-start';

        return `
            <div style="border-bottom:1px dashed rgba(255,255,255,0.07);margin-bottom:8px;padding-bottom:8px;">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-direction:${isMe ? 'row-reverse' : 'row'};">
                    <div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:${isOnline ? 'var(--ok)' : 'var(--bd2)'};${isOnline ? 'box-shadow:0 0 5px var(--ok)' : ''}"></div>
                    <span style="color:${color};font-weight:bold;font-size:13px;font-family:var(--mono);">${window.escapeHtml(displayName)}</span>
                    ${shortName && shortName !== displayName ? `<span style="color:var(--txt3);font-size:10px;font-family:var(--mono);">[${window.escapeHtml(shortName)}]</span>` : ''}
                </div>
                ${metaLine ? `<div style="font-size:9px;font-family:var(--mono);color:var(--txt3);display:flex;flex-wrap:wrap;gap:8px;justify-content:${justify};margin-bottom:4px;">${metaLine}</div>` : ''}
                ${badges ? `<div class="m-telemetry" style="justify-content:${justify};margin-top:4px;">${badges}</div>` : ''}
            </div>`;
    },
};