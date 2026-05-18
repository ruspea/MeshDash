import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
import httpx  # migrated from httpx for consistency
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from pubsub import pub

logger = logging.getLogger("plugin.mesh_chat")
plugin_router = APIRouter()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_BASE_DIR, "mesh_chat.db")
_DB_LOCK = threading.Lock()
_context: Dict[str, Any] = {}

_MESH_MAX_BYTES = 200
_CHUNK_DELAY_S = 3.0
_ACK_TIMEOUT_S = 12.0
_ACK_MAX_RETRIES = 2

_llm_instance = None
_llm_lock = threading.Lock()
_llm_queue = asyncio.Queue()

_ack_pending: Dict[str, asyncio.Event] = {}
_ack_lock = asyncio.Lock()

DEFAULT_PROMPTS = [
    ("Concise Q&A", "Direct, brief, factual answers. No elaboration.", "You are a highly concise assistant. Provide direct, factual answers only. Use maximum 2 sentences. Do not elaborate. Do not use conversational filler."),
    ("Helpful Assistant", "Standard AI assistant, balances detail and brevity.", "You are a helpful AI assistant. Provide clear, well-structured answers. Balance detail with brevity. Be polite and objective."),
    ("Storyteller", "Creative, narrative-driven, elaborates with examples.", "You are a creative storyteller. When answering, use narratives, analogies, and detailed examples. Be descriptive and engaging."),
    ("Philosophical Debater", "Deep, analytical, questions the user's premises.", "You are a philosophical debater. Analyze the user's query deeply, question their underlying premises, and offer alternative perspectives. Be intellectual but respectful."),
    ("Chatterbox", "Highly extroverted, enthusiastic, asks follow-up questions.", "You are a highly extroverted, enthusiastic chatterbox. Answer the prompt fully, then immediately ask engaging follow-up questions to keep the conversation going. Show high energy.")
]

class ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    channel_index: Optional[int] = None
    session_timeout: Optional[int] = None
    default_prompt_id: Optional[int] = None
    model_repo: Optional[str] = None
    model_file: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = None
    api_model: Optional[str] = None

class ModelDownloadReq(BaseModel):
    model_repo: str
    model_file: str

class PromptData(BaseModel):
    name: str
    description: str
    sys_prompt: str

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _init_db():
    with _DB_LOCK:
        conn = _get_db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER DEFAULT 1,
            channel_index INTEGER DEFAULT 0,
            session_timeout INTEGER DEFAULT 300,
            default_prompt_id INTEGER DEFAULT 2,
            model_repo TEXT DEFAULT 'bartowski/Phi-3.1-mini-4k-instruct-GGUF',
            model_file TEXT DEFAULT 'Phi-3.1-mini-4k-instruct-Q4_K_M.gguf',
            provider TEXT DEFAULT 'local',
            api_key TEXT DEFAULT '',
            api_model TEXT DEFAULT '',
            status TEXT DEFAULT 'uninitialized',
            progress TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL,
            sys_prompt TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            prompt_id INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS live_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            query TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sess_node ON sessions(node_id);
        CREATE INDEX IF NOT EXISTS idx_msg_sess ON messages(session_id);
        """)
        
        try:
            conn.execute("ALTER TABLE config ADD COLUMN provider TEXT DEFAULT 'local'")
            conn.execute("ALTER TABLE config ADD COLUMN api_key TEXT DEFAULT ''")
            conn.execute("ALTER TABLE config ADD COLUMN api_model TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        c = conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        if c == 0:
            for name, desc, txt in DEFAULT_PROMPTS:
                conn.execute("INSERT INTO prompts (name, description, sys_prompt) VALUES (?, ?, ?)", (name, desc, txt))
                
        conn.execute("INSERT OR IGNORE INTO config (id) VALUES (1)")
        conn.commit()
        conn.close()

def _set_status(status: str, progress: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE config SET status=?, progress=? WHERE id=1", (status, progress))
        conn.commit()
        conn.close()

def _get_config() -> Dict:
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("SELECT * FROM config WHERE id=1").fetchone()
        conn.close()
    return dict(row)

def _get_or_create_session(node_id: str) -> str:
    now = time.time()
    with _DB_LOCK:
        conn = _get_db()
        cfg = conn.execute("SELECT default_prompt_id FROM config WHERE id=1").fetchone()
        def_pid = cfg["default_prompt_id"] if cfg else 2
        
        row = conn.execute(
            "SELECT id FROM sessions WHERE node_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
            (node_id,)
        ).fetchone()
        
        if row:
            sess_id = row["id"]
            conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sess_id))
        else:
            sess_id = str(uuid.uuid4())[:8]
            conn.execute(
                "INSERT INTO sessions (id, node_id, status, prompt_id, created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?)",
                (sess_id, node_id, def_pid, now, now)
            )
        conn.commit()
        conn.close()
    return sess_id

def _set_session_prompt(sess_id: str, prompt_id: int):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE sessions SET prompt_id=?, updated_at=? WHERE id=?", (prompt_id, time.time(), sess_id))
        conn.commit()
        conn.close()

def _get_session_prompt_text(sess_id: str) -> str:
    with _DB_LOCK:
        conn = _get_db()
        row = conn.execute("""
            SELECT p.sys_prompt 
            FROM sessions s 
            JOIN prompts p ON s.prompt_id = p.id 
            WHERE s.id=?
        """, (sess_id,)).fetchone()
        if not row:
            cfg = conn.execute("SELECT default_prompt_id FROM config WHERE id=1").fetchone()
            def_pid = cfg["default_prompt_id"] if cfg else 2
            row = conn.execute("SELECT sys_prompt FROM prompts WHERE id=?", (def_pid,)).fetchone()
        conn.close()
    return row["sys_prompt"] if row else ""

def _add_message(session_id: str, role: str, content: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(
            "INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time())
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
        conn.commit()
        conn.close()

def _enqueue_request(node_id: str, query: str) -> int:
    with _DB_LOCK:
        conn = _get_db()
        cursor = conn.execute(
            "INSERT INTO live_queue (node_id, query, ts) VALUES (?, ?, ?)",
            (node_id, query, time.time())
        )
        req_id = cursor.lastrowid
        conn.commit()
        conn.close()
    return req_id

def _update_request_status(req_id: int, status: str):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE live_queue SET status=? WHERE id=?", (status, req_id))
        if status in ('completed', 'error'):
            conn.execute("DELETE FROM live_queue WHERE id=?", (req_id,))
        conn.commit()
        conn.close()

def _reap_sessions_worker():
    while True:
        try:
            time.sleep(10)
            cfg = _get_config()
            timeout = cfg["session_timeout"]
            cutoff = time.time() - timeout
            
            with _DB_LOCK:
                conn = _get_db()
                conn.execute(
                    "UPDATE sessions SET status='closed' WHERE status='active' AND updated_at < ?",
                    (cutoff,)
                )
                conn.commit()
                conn.close()
        except Exception as e:
            logger.error(f"Reap error: {e}")

def _download_model_worker(repo: str, file: str):
    global _llm_instance
    try:
        _set_status("downloading", f"Downloading {file}...")
        from huggingface_hub import hf_hub_download
        hf_hub_download(repo_id=repo, filename=file, local_dir=_BASE_DIR)
        with _llm_lock:
            _llm_instance = None 
        _set_status("ready", "Model loaded and ready.")
    except ImportError:
        _set_status("error", "Missing dependencies: pip install huggingface_hub llama_cpp_python")
        logger.error("Mesh Chat: huggingface_hub or llama_cpp_python not installed")
    except Exception as e:
        _set_status("error", f"Download failed: {str(e)}")

def _load_llm():
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance
    cfg = _get_config()
    from llama_cpp import Llama
    model_path = os.path.join(_BASE_DIR, cfg["model_file"])
    if not os.path.exists(model_path):
        raise FileNotFoundError("Model file not found")
    _llm_instance = Llama(model_path=model_path, n_ctx=2048, n_threads=4, verbose=False)
    return _llm_instance

def _generate_hosted(provider: str, api_key: str, model: str, sys_prompt: str, history: list, query: str) -> str:
    if not api_key:
        return "API key not configured."

    api_key = api_key.strip()
    if api_key.lower().startswith("bearer "):
        api_key = api_key[7:].strip()

    try:
        with httpx.Client(timeout=30.0) as client:
            if provider in ("openai", "nvidia"):
                url = "https://api.openai.com/v1/chat/completions" if provider == "openai" else "https://integrate.api.nvidia.com/v1/chat/completions"
                messages = [{"role": "system", "content": sys_prompt}]
                for msg in history:
                    messages.append({"role": msg["role"], "content": msg["content"]})
                messages.append({"role": "user", "content": query})

                payload = {
                    "model": model, "messages": messages,
                    "max_tokens": 150, "temperature": 0.8, "stream": False
                }
                if provider == "nvidia":
                    payload.update({"top_p": 1.0, "frequency_penalty": 0.0, "presence_penalty": 0.0})

                r = client.post(url, json=payload, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                })
                if r.status_code >= 400:
                    logger.error(f"Hosted API HTTPError {r.status_code}: {r.text}")
                    return f"API Error {r.status_code}."
                return r.json()["choices"][0]["message"]["content"].strip()

            elif provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                contents = []
                for msg in history:
                    role = "user" if msg["role"] == "user" else "model"
                    contents.append({"role": role, "parts": [{"text": msg["content"]}]})
                contents.append({"role": "user", "parts": [{"text": query}]})

                payload = {
                    "systemInstruction": {"parts": [{"text": sys_prompt}]},
                    "contents": contents,
                    "generationConfig": {"temperature": 0.8, "maxOutputTokens": 150}
                }

                r = client.post(url, json=payload, headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key
                })
                if r.status_code >= 400:
                    logger.error(f"Gemini API HTTPError {r.status_code}: {r.text}")
                    return f"API Error {r.status_code}."
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Hosted generation error: {e}")
        return f"Error contacting {provider} API."

    return "Unknown provider."

def _generate_response(node_id: str, prompt_text: str) -> str:
    cfg = _get_config()
    sess_id = _get_or_create_session(node_id)
    sys_prompt = _get_session_prompt_text(sess_id)
    
    with _DB_LOCK:
        conn = _get_db()
        history = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY ts ASC",
            (sess_id,)
        ).fetchall()
        conn.close()
        
    _add_message(sess_id, "user", prompt_text)
    
    if cfg.get("provider", "local") != "local":
        reply = _generate_hosted(
            cfg["provider"], 
            cfg.get("api_key", ""), 
            cfg.get("api_model", ""), 
            sys_prompt, 
            [dict(h) for h in history], 
            prompt_text
        )
        _add_message(sess_id, "assistant", reply)
        return reply

    with _llm_lock:
        llm = _load_llm()
        
        prompt = f"<|system|>\n{sys_prompt}<|end|>\n"
        for turn in history:
            prompt += f"<|{turn['role']}|>\n{turn['content']}<|end|>\n"
        prompt += f"<|user|>\n{prompt_text}<|end|>\n<|assistant|>\n"
        
        res = llm(prompt, max_tokens=200, stop=["<|end|>", "<|user|>"], temperature=0.3)
        reply = res["choices"][0]["text"].strip()
        _add_message(sess_id, "assistant", reply)
        return reply

def _split_to_chunks(text: str, max_bytes: int) -> List[str]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]
    chunks = []
    while encoded:
        chunk_b = encoded[:max_bytes]
        while chunk_b:
            try:
                chunk_b.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk_b = chunk_b[:-1]
        chunks.append(chunk_b.decode("utf-8"))
        encoded = encoded[len(chunk_b):]
    return chunks

async def _send_raw(text: str, dest_id: str, want_ack: bool = False) -> Optional[str]:
    cm = _context.get("connection_manager")
    if not cm: return None
    try:
        result = await cm.sendText(text, destinationId=dest_id, wantAck=want_ack)
        if result is not None:
            pid = getattr(result, "id", None)
            if pid: return str(pid)
        return "sent"
    except Exception:
        return None

async def _wait_for_ack(msg_id: str, timeout: float) -> bool:
    async with _ack_lock:
        ev = asyncio.Event()
        _ack_pending[msg_id] = ev
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        async with _ack_lock:
            _ack_pending.pop(msg_id, None)

async def _signal_ack(packet_id: str):
    async with _ack_lock:
        ev = _ack_pending.get(packet_id)
    if ev: ev.set()

async def _send_dm(node_id: str, text: str, require_ack: bool = True):
    chunks = _split_to_chunks(text, _MESH_MAX_BYTES - 15)
    n = len(chunks)
    for i, chunk in enumerate(chunks):
        annotated = chunk if n == 1 else f"[{i+1}/{n}] {chunk}"
        success = False
        for attempt in range(_ACK_MAX_RETRIES + 1):
            if attempt > 0: await asyncio.sleep(1.5)
            packet_id = await _send_raw(annotated, node_id, want_ack=require_ack)
            if packet_id and packet_id != "sent" and require_ack:
                if await _wait_for_ack(packet_id, _ACK_TIMEOUT_S):
                    success = True
                    break
            else:
                success = True
                break
        if i < n - 1: await asyncio.sleep(_CHUNK_DELAY_S)

async def _llm_queue_worker():
    while True:
        req = await _llm_queue.get()
        req_id, node_id, query = req["id"], req["node_id"], req["query"]
        _update_request_status(req_id, "processing")
        try:
            gen_task = asyncio.create_task(asyncio.to_thread(_generate_response, node_id, query))
            start_time = time.time()
            sent_30, sent_60, timed_out = False, False, False
            
            while not gen_task.done():
                elapsed = time.time() - start_time
                if elapsed > 130:
                    timed_out = True
                    break
                if elapsed > 60 and not sent_60:
                    await _send_dm(node_id, "[Chat AI] Still thinking (60s)...", require_ack=True)
                    sent_60 = True
                    await asyncio.sleep(5)
                elif elapsed > 30 and not sent_30:
                    await _send_dm(node_id, "[Chat AI] Still thinking (30s)...", require_ack=True)
                    sent_30 = True
                    await asyncio.sleep(5)
                await asyncio.sleep(1)
                
            if timed_out:
                await _send_dm(node_id, "[Chat AI] Failed, please try again.", require_ack=True)
                _update_request_status(req_id, "error")
            else:
                reply_text = gen_task.result()
                await _send_dm(node_id, reply_text, require_ack=True)
                _update_request_status(req_id, "completed")
        except Exception:
            await _send_dm(node_id, "[Chat AI] Error generating response.", require_ack=True)
            _update_request_status(req_id, "error")
        finally:
            _llm_queue.task_done()

def _on_receive(packet, interface=None):
    event_loop = _context.get("event_loop")
    if not event_loop: return

    try:
        decoded = packet.get("decoded") or {}
        portnum = decoded.get("portnum")
        
        if portnum == "ROUTING_APP":
            routing = decoded.get("routing") or {}
            error_reason = routing.get("errorReason", -1)
            is_ack = error_reason in (0, "NONE", "ack_variant") or routing.get("variant") == "ack_variant"
            if is_ack:
                req_id = (packet.get("requestId") or packet.get("request_id") or packet.get("decoded", {}).get("requestId"))
                if req_id:
                    asyncio.run_coroutine_threadsafe(_signal_ack(str(req_id)), event_loop)
            return

        if portnum not in ("TEXT_MESSAGE_APP", 1): return

        text = decoded.get("text") or ""
        if not text:
            payload = decoded.get("payload")
            if isinstance(payload, bytes):
                try: text = payload.decode("utf-8", errors="replace")
                except Exception: return

        if not text.lower().startswith("chat."): return

        cfg = _get_config()
        if not cfg["enabled"]: return

        from_id = packet.get("fromId") or ""
        if not from_id:
            raw_from = packet.get("from")
            if isinstance(raw_from, int): from_id = f"!{raw_from:08x}"
        if not from_id: return

        to_id = packet.get("toId") or ""
        is_broadcast = to_id == "^all" or packet.get("to") == 0xFFFFFFFF
        channel_idx = packet.get("channel") or packet.get("channelIndex") or 0

        if is_broadcast and channel_idx != cfg["channel_index"]: return

        cmd = text[5:].strip()
        if not cmd: return

        cmd_lower = cmd.lower()
        
        if cmd_lower == "help":
            help_txt = (
                "[Chat AI Help]\n"
                "chat.<msg> - Talk to AI\n"
                "chat.mode - List modes\n"
                "chat.mode.<name> - Change mode"
            )
            asyncio.run_coroutine_threadsafe(_send_dm(from_id, help_txt, require_ack=True), event_loop)
            return

        if cmd_lower == "mode":
            with _DB_LOCK:
                conn = _get_db()
                prompts = conn.execute("SELECT name FROM prompts ORDER BY id ASC").fetchall()
                conn.close()
            names = [p["name"] for p in prompts]
            msg = "[Chat Modes]\n" + "\n".join(f"- {n}" for n in names) + "\nUse: chat.mode.<name>"
            asyncio.run_coroutine_threadsafe(_send_dm(from_id, msg, require_ack=True), event_loop)
            return
            
        if cmd_lower.startswith("mode."):
            mode_req = cmd[5:].strip()
            with _DB_LOCK:
                conn = _get_db()
                row = conn.execute("SELECT id, name FROM prompts WHERE LOWER(name)=LOWER(?)", (mode_req,)).fetchone()
                conn.close()
            if row:
                sess_id = _get_or_create_session(from_id)
                _set_session_prompt(sess_id, row["id"])
                asyncio.run_coroutine_threadsafe(_send_dm(from_id, f"Chat mode changed to: {row['name']}", require_ack=True), event_loop)
            else:
                asyncio.run_coroutine_threadsafe(_send_dm(from_id, f"Mode '{mode_req}' not found. Send chat.mode for list.", require_ack=True), event_loop)
            return

        if cfg.get("provider", "local") == "local" and cfg["status"] != "ready":
            asyncio.run_coroutine_threadsafe(_send_dm(from_id, f"[Chat AI] Offline AI is installing. Status: {cfg['status']}", require_ack=True), event_loop)
            return

        asyncio.run_coroutine_threadsafe(_send_dm(from_id, "[Chat AI] Thinking...", require_ack=True), event_loop)
        req_id = _enqueue_request(from_id, cmd)
        event_loop.call_soon_threadsafe(_llm_queue.put_nowait, {"id": req_id, "node_id": from_id, "query": cmd})

    except Exception:
        pass

async def _watchdog(context: dict):
    wd = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd and pid: wd[pid] = time.time()
        except asyncio.CancelledError: return

def init_plugin(context: dict):
    global _context
    _context = context
    _init_db()

    cfg = _get_config()
    if cfg.get("provider", "local") == "local":
        if cfg["status"] == "uninitialized" or not os.path.exists(os.path.join(_BASE_DIR, cfg["model_file"])):
            threading.Thread(target=_download_model_worker, args=(cfg["model_repo"], cfg["model_file"]), daemon=True).start()

    threading.Thread(target=_reap_sessions_worker, daemon=True).start()

    try: pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception: pass
    pub.subscribe(_on_receive, "meshtastic.receive")

    loop = context.get("event_loop")
    if loop:
        asyncio.run_coroutine_threadsafe(_llm_queue_worker(), loop)
        asyncio.run_coroutine_threadsafe(_watchdog(context), loop)


@plugin_router.get("/state")
async def get_state():
    cfg = _get_config()
    with _DB_LOCK:
        conn = _get_db()
        queue = [dict(r) for r in conn.execute("SELECT * FROM live_queue ORDER BY ts ASC").fetchall()]
        sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 20").fetchall()]
        prompts = [dict(r) for r in conn.execute("SELECT * FROM prompts ORDER BY id ASC").fetchall()]
        stats = {
            "total_sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "total_msgs": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        }
        conn.close()
        
    return {"config": cfg, "queue": queue, "sessions": sessions, "prompts": prompts, "stats": stats}

@plugin_router.post("/config")
async def set_config(body: ConfigUpdate):
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields: return {"status": "ok"}
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(f"UPDATE config SET {set_clause} WHERE id=1", (*fields.values(),))
        conn.commit()
        conn.close()
    return {"status": "ok"}

@plugin_router.post("/model/download")
async def download_model(body: ModelDownloadReq):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE config SET model_repo=?, model_file=? WHERE id=1", (body.model_repo, body.model_file))
        conn.commit()
        conn.close()
    threading.Thread(target=_download_model_worker, args=(body.model_repo, body.model_file), daemon=True).start()
    return {"status": "downloading"}

@plugin_router.post("/prompts")
async def add_prompt(body: PromptData):
    with _DB_LOCK:
        conn = _get_db()
        try:
            conn.execute("INSERT INTO prompts (name, description, sys_prompt) VALUES (?, ?, ?)", (body.name, body.description, body.sys_prompt))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            raise HTTPException(status_code=400, detail="Prompt name exists")
        conn.close()
    return {"status": "ok"}

@plugin_router.delete("/prompts/{pid}")
async def delete_prompt(pid: int):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("DELETE FROM prompts WHERE id=?", (pid,))
        conn.commit()
        conn.close()
    return {"status": "ok"}

@plugin_router.get("/sessions/{sess_id}/messages")
async def get_session_msgs(sess_id: str):
    with _DB_LOCK:
        conn = _get_db()
        msgs = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY ts ASC", (sess_id,)).fetchall()]
        conn.close()
    return {"messages": msgs}