/* ==========================================================================
 * MeshDash C2 — Automated Task Scheduler Module
 * ========================================================================== */

window.C2TasksApp = {
    tasks: [],

    init() {
        this.setupListeners();
        this.renderNodeGrid();
        this.fetchTasks();
    },

    setupListeners() {
        const search = document.getElementById('tasks-node-search');
        if (search) search.oninput = () => this.renderNodeGrid();

        const form = document.getElementById('c2-task-form');
        if (form) form.onsubmit = (e) => this.saveTask(e);

        const timeInp = document.getElementById('c2-task-time');
        if (timeInp) timeInp.onchange = () => this.updateCronPreview();
        
        const custCron = document.getElementById('c2-task-custom-cron');
        if (custCron) custCron.oninput = (e) => { document.getElementById('c2-task-cron-preview').innerText = e.target.value; };
    },

    renderNodeGrid() {
        const grid = document.getElementById('tasks-node-grid');
        if (!grid) return;

        const term = (document.getElementById('tasks-node-search')?.value || '').toLowerCase();
        const nodes = Object.values(window.meshState?.nodes || {});
        nodes.sort((a,b) => (a.user?.longName || '').localeCompare(b.user?.longName || ''));

        // Always put Broadcast first
        let html = `
            <div class="c2-node-card" style="border-color:var(--acc);" onclick="window.C2TasksApp.openTaskModal('^all')">
                <div class="c2-avatar" style="background:rgba(0,200,245,0.1); color:var(--acc); border-color:var(--acc);"><i class="fas fa-broadcast-tower"></i></div>
                <div style="overflow:hidden;">
                    <div class="c2-name" style="color:var(--acc);">BROADCAST</div>
                    <div class="c2-id">^all</div>
                </div>
            </div>
        `;

        nodes.forEach(n => {
            const name = n.user?.longName || n.user?.shortName || n.node_id;
            if (term && !name.toLowerCase().includes(term) && !n.node_id.toLowerCase().includes(term)) return;

            const initials = name !== n.node_id ? name.substring(0,2).toUpperCase() : '??';
            
            html += `
                <div class="c2-node-card" onclick="window.C2TasksApp.openTaskModal('${n.node_id}')">
                    <div class="c2-avatar">${initials}</div>
                    <div style="overflow:hidden;">
                        <div class="c2-name">${window.escapeHtml(name)}</div>
                        <div class="c2-id">${n.node_id}</div>
                    </div>
                </div>
            `;
        });

        grid.innerHTML = html;
    },

    async fetchTasks() {
        const tbody = document.getElementById('tasks-table-body');
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:40px; color:var(--txt3);"><i class="fas fa-circle-notch fa-spin"></i> LOADING...</td></tr>`;
        
        try {
            const res = await fetch('/api/tasks/');
            if (!res.ok) throw new Error("Failed to fetch");
            this.tasks = await res.json();
            this.renderTasksTable();
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:40px; color:var(--err);">FAILED TO LOAD TASKS</td></tr>`;
        }
    },

    renderTasksTable() {
        const tbody = document.getElementById('tasks-table-body');
        
        if (!this.tasks || this.tasks.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding:40px; color:var(--txt3);">[ NO ACTIVE TASKS ]</td></tr>`;
            return;
        }

        tbody.innerHTML = this.tasks.map(t => {
            const n = window.meshState?.nodes[t.nodeId];
            const nodeName = t.nodeId === '^all' ? 'BROADCAST' : (n?.user?.longName || n?.user?.shortName || t.nodeId);
            
            const isWeb = t.taskType === 'website_monitor';
            const typeBadge = isWeb ? `<span style="color:var(--pur); font-weight:bold;">WEB SENSOR</span>` : `<span style="color:var(--ok); font-weight:bold;">MESSAGE</span>`;
            
            let payloadStr = t.actionPayload;
            if (isWeb) {
                try { 
                    const p = JSON.parse(t.actionPayload); 
                    payloadStr = `[${p.prefix}] ${p.url.substring(0,30)}...`; 
                } catch(e) {}
            } else {
                payloadStr = payloadStr.length > 40 ? payloadStr.substring(0,40) + '...' : payloadStr;
            }

            const slotLabel = typeof window._slotLabel === 'function' ? window._slotLabel(t.slotId || t.slot_id || 'node_0') : 'PRIMARY';
            return `
                <tr>
                    <td>
                        <div style="font-weight:bold; color:var(--txt);">${window.escapeHtml(nodeName)}</div>
                        <div style="font-size:9px; color:var(--txt3);">${t.nodeId}</div>
                    </td>
                    <td>${typeBadge}</td>
                    <td style="color:var(--warn); font-weight:bold;">${window.escapeHtml(t.cronString)}</td>
                    <td style="font-size:10px; color:var(--txt2); max-width:200px; overflow:hidden; text-overflow:ellipsis;">${window.escapeHtml(payloadStr)}</td>
                    <td>
                        <span style="font-size:9px;font-family:var(--mono);background:rgba(176,96,255,0.12);color:var(--pur);border:1px solid rgba(176,96,255,0.25);padding:1px 6px;border-radius:3px;">⬡ ${window.escapeHtml(slotLabel)}</span>
                    </td>
                    <td style="text-align:right;">
                        <button class="btn btn-sm btn-acc" title="Edit" onclick="window.C2TasksApp.editTask(${t.id})" style="padding:4px 8px; min-width:auto; margin-right:4px;"><i class="fas fa-pencil"></i></button>
                        <button class="btn btn-sm btn-err" title="Delete" onclick="window.C2TasksApp.deleteTask(${t.id})" style="padding:4px 8px; min-width:auto;"><i class="fas fa-trash"></i></button>
                    </td>
                </tr>
            `;
        }).join('');
    },

    openTaskModal(nodeId, existingTask = null) {
        document.getElementById('c2-task-form').reset();
        
        const n = window.meshState?.nodes[nodeId];
        const name = nodeId === '^all' ? 'BROADCAST' : (n?.user?.longName || n?.user?.shortName || nodeId);
        
        document.getElementById('c2-task-title').innerText = existingTask ? `EDIT TASK: ${name}` : `NEW TASK: ${name}`;
        document.getElementById('c2-task-node').value = nodeId;
        document.getElementById('c2-task-id').value = existingTask ? existingTask.id : '';

        // Inject slot picker for "send via" if not already present
        if (!document.getElementById('c2-task-slot-wrap')) {
            const form = document.getElementById('c2-task-form');
            if (form) {
                const wrap = document.createElement('div');
                wrap.id = 'c2-task-slot-wrap';
                wrap.style.cssText = 'margin-bottom:16px;';
                wrap.innerHTML = `
                    <label style="font-family:var(--mono);font-size:9px;color:var(--txt3);display:block;margin-bottom:6px;">TRANSMIT VIA RADIO</label>
                    <div id="c2-task-slot-picker"></div>`;
                form.prepend(wrap);
            }
        }
        const existingSlotId = existingTask?.slotId || existingTask?.slot_id || window._activeSlotId || 'node_0';
        if (typeof window._buildSlotPicker === 'function') {
            window._buildSlotPicker('c2-task-slot-picker', existingSlotId);
        }

        if (existingTask) {
            document.getElementById('c2-task-type').value = existingTask.taskType;
            if (existingTask.taskType === 'message') {
                document.getElementById('c2-task-msg').value = existingTask.actionPayload;
            } else {
                try {
                    const p = typeof existingTask.actionPayload === 'string' ? JSON.parse(existingTask.actionPayload) : existingTask.actionPayload;
                    document.getElementById('c2-task-json').value = JSON.stringify(p, null, 2);
                } catch(e) {
                    document.getElementById('c2-task-json').value = existingTask.actionPayload;
                }
            }
            document.getElementById('c2-task-schedule').value = 'custom';
            document.getElementById('c2-task-custom-cron').value = existingTask.cronString;
            document.getElementById('c2-task-cron-preview').innerText = existingTask.cronString;
        } else {
            document.getElementById('c2-task-type').value = 'message';
            document.getElementById('c2-task-schedule').value = 'once';
            document.getElementById('c2-task-custom-cron').value = '';
            
            const now = new Date();
            now.setMinutes(now.getMinutes() + 5);
            now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
            document.getElementById('c2-task-time').value = now.toISOString().slice(0,16);
            this.updateCronPreview();
        }

        this.toggleTaskType();
        this.toggleScheduleType();
        
        document.getElementById('c2-task-modal').style.display = 'flex';
    },

    editTask(id) {
        const task = this.tasks.find(t => t.id === id);
        if (task) this.openTaskModal(task.nodeId, task);
    },

    async deleteTask(id) {
        if (!confirm("Terminate this automated directive?")) return;
        try {
            const res = await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
            if (res.ok) {
                window.triggerToast("Task Terminated", "warn");
                this.fetchTasks();
            }
        } catch(e) { window.triggerToast("Delete Failed", "err"); }
    },

    toggleTaskType() {
        const t = document.getElementById('c2-task-type').value;
        document.getElementById('c2-task-wrap-msg').style.display = t === 'message' ? 'block' : 'none';
        document.getElementById('c2-task-wrap-sensor').style.display = t === 'website_monitor' ? 'block' : 'none';
        
        document.getElementById('c2-task-msg').required = t === 'message';
        document.getElementById('c2-task-json').required = t === 'website_monitor';
    },

    toggleScheduleType() {
        const s = document.getElementById('c2-task-schedule').value;
        const timeWrap = document.getElementById('c2-task-wrap-time');
        const timeInp = document.getElementById('c2-task-time');
        const customInp = document.getElementById('c2-task-custom-cron');

        customInp.style.display = s === 'custom' ? 'block' : 'none';
        
        if (s === 'custom' || s === 'hourly') {
            timeWrap.style.display = 'none';
            timeInp.required = false;
        } else if (s === 'daily') {
            timeWrap.style.display = 'block';
            timeInp.type = 'time';
            timeInp.required = true;
        } else {
            timeWrap.style.display = 'block';
            timeInp.type = 'datetime-local';
            timeInp.required = true;
        }
        
        this.updateCronPreview();
    },

    updateCronPreview() {
        const s = document.getElementById('c2-task-schedule').value;
        const t = document.getElementById('c2-task-time').value;
        const p = document.getElementById('c2-task-cron-preview');
        const c = document.getElementById('c2-task-custom-cron').value;

        if (s === 'custom') {
            p.innerText = c || "* * * * *";
            return;
        }
        if (s === 'hourly') {
            p.innerText = "0 * * * *";
            return;
        }
        if (s === 'daily' && t) {
            const [h, m] = t.split(':');
            p.innerText = `${parseInt(m)} ${parseInt(h)} * * *`;
            return;
        }
        if (s === 'once' && t) {
            const d = new Date(t);
            p.innerText = `${d.getMinutes()} ${d.getHours()} ${d.getDate()} ${d.getMonth()+1} *`;
            return;
        }
        p.innerText = "WAITING FOR INPUT...";
    },

    async saveTask(e) {
        e.preventDefault();
        
        const btn = document.getElementById('c2-btn-save-task');
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> DEPLOYING...';
        btn.disabled = true;

        const id = document.getElementById('c2-task-id').value;
        const type = document.getElementById('c2-task-type').value;
        const cron = document.getElementById('c2-task-cron-preview').innerText;
        
        let payload = '';
        if (type === 'message') {
            payload = document.getElementById('c2-task-msg').value;
        } else {
            try {
                payload = JSON.stringify(JSON.parse(document.getElementById('c2-task-json').value));
            } catch(err) {
                window.triggerToast("Invalid JSON syntax", "err");
                btn.innerHTML = '<i class="fas fa-save"></i> DEPLOY TASK';
                btn.disabled = false;
                return;
            }
        }

        const taskData = {
            nodeId: document.getElementById('c2-task-node').value,
            taskType: type,
            actionPayload: payload,
            cronString: cron,
            slotId: window._slotPickerValue ? window._slotPickerValue('c2-task-slot-picker') : (window._activeSlotId || 'node_0')
        };

        const url = id ? `/api/tasks/${id}` : '/api/tasks/';
        const method = id ? 'PUT' : 'POST';

        try {
            const res = await fetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(taskData)
            });

            if (res.ok) {
                window.triggerToast("Directive Deployed Successfully", "ok");
                document.getElementById('c2-task-modal').style.display = 'none';
                this.fetchTasks();
            } else {
                window.triggerToast("Deployment Rejected", "err");
            }
        } catch (error) {
            window.triggerToast("Network Error", "err");
        } finally {
            btn.innerHTML = '<i class="fas fa-save"></i> DEPLOY TASK';
            btn.disabled = false;
        }
    }
};