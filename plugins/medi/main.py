import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from pubsub import pub

# Use httpx for all HTTP calls — consistent with rest of codebase
import httpx

logger = logging.getLogger("plugin.medi")
plugin_router = APIRouter()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_BASE_DIR, "medi_v2.db")
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

DEFAULT_SYS_PROMPT = (
    "You are a survival medical AI. You have NO medical equipment, NO labs, and NO imaging. "
    "Rule 1: Confirm diagnoses using ONLY bare-hand physical exams. "
    "Rule 2: If a user asks how to confirm a condition, describe the physical bare-hand test. Never suggest a machine. "
    "Rule 3: Maximum 3 sentences. No disclaimers. End by asking for the result of the physical test."
)
DISCLAIMER = "\n[AI Gen. Not a doctor.]"

class ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    channel_index: Optional[int] = None
    session_timeout: Optional[int] = None
    sys_prompt: Optional[str] = None
    model_repo: Optional[str] = None
    model_file: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = None
    api_model: Optional[str] = None

class ModelDownloadReq(BaseModel):
    model_repo: str
    model_file: str

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
            session_timeout INTEGER DEFAULT 60,
            sys_prompt TEXT,
            model_repo TEXT DEFAULT 'bartowski/Phi-3.1-mini-4k-instruct-GGUF',
            model_file TEXT DEFAULT 'Phi-3.1-mini-4k-instruct-Q4_K_M.gguf',
            provider TEXT DEFAULT 'local',
            api_key TEXT DEFAULT '',
            api_model TEXT DEFAULT '',
            status TEXT DEFAULT 'uninitialized',
            progress TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            status TEXT DEFAULT 'active',
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
        conn.execute(
            "INSERT OR IGNORE INTO config (id, sys_prompt) VALUES (1, ?)",
            (DEFAULT_SYS_PROMPT,)
        )
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
                "INSERT INTO sessions (id, node_id, status, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
                (sess_id, node_id, now, now)
            )
        conn.commit()
        conn.close()
    return sess_id

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
        logger.error("Medi AI: huggingface_hub or llama_cpp_python not installed")
    except Exception as e:
        _set_status("error", f"Download failed: {str(e)}")
        logger.error(f"Medi AI setup failed: {e}")

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
                    "max_tokens": 150, "temperature": 0.1, "stream": False
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
                res_data = r.json()
                return res_data["choices"][0]["message"]["content"].strip()
                
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
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 150}
                }
                
                r = client.post(url, json=payload, headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key
                })
                if r.status_code >= 400:
                    logger.error(f"Gemini API HTTPError {r.status_code}: {r.text}")
                    return f"API Error {r.status_code}."
                res_data = r.json()
                return res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Hosted generation error: {e}")
        return f"Error contacting {provider} API."
        
    return "Unknown provider."

def _generate_response(node_id: str, prompt_text: str) -> str:
    cfg = _get_config()
    sess_id = _get_or_create_session(node_id)
    
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
            cfg["sys_prompt"], 
            [dict(h) for h in history], 
            prompt_text
        )
        _add_message(sess_id, "assistant", reply)
        return reply

    with _llm_lock:
        llm = _load_llm()
        
        prompt = f"<|system|>\n{cfg['sys_prompt']}<|end|>\n"
        for turn in history:
            prompt += f"<|{turn['role']}|>\n{turn['content']}<|end|>\n"
        prompt += f"<|user|>\n{prompt_text}<|end|>\n<|assistant|>\n"
        
        res = llm(
            prompt,
            max_tokens=150,
            stop=["<|end|>", "<|user|>"],
            temperature=0.1
        )
        
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
    if not cm:
        return None
    try:
        result = await cm.sendText(text, destinationId=dest_id, wantAck=want_ack)
        if result is not None:
            pid = getattr(result, "id", None)
            if pid:
                return str(pid)
        return "sent"
    except Exception as e:
        logger.error(f"Medi _send_raw error: {e}")
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
    if ev:
        ev.set()

async def _send_dm(node_id: str, text: str, require_ack: bool = True):
    chunks = _split_to_chunks(text, _MESH_MAX_BYTES - 15)
    n = len(chunks)
    for i, chunk in enumerate(chunks):
        annotated = chunk
        if n > 1:
            annotated = f"[{i+1}/{n}] {chunk}"
        
        success = False
        for attempt in range(_ACK_MAX_RETRIES + 1):
            if attempt > 0:
                await asyncio.sleep(1.5)
            
            packet_id = await _send_raw(annotated, node_id, want_ack=require_ack)
            if packet_id and packet_id != "sent" and require_ack:
                got_ack = await _wait_for_ack(packet_id, _ACK_TIMEOUT_S)
                if got_ack:
                    success = True
                    break
            else:
                success = True
                break
        
        if not success:
            logger.warning(f"Medi: chunk {i+1}/{n} failed after retries for {node_id}")

        if i < n - 1:
            await asyncio.sleep(_CHUNK_DELAY_S)

async def _llm_queue_worker():
    while True:
        req = await _llm_queue.get()
        req_id = req["id"]
        node_id = req["node_id"]
        query = req["query"]
        
        _update_request_status(req_id, "processing")
        
        try:
            gen_task = asyncio.create_task(asyncio.to_thread(_generate_response, node_id, query))
            
            start_time = time.time()
            sent_30 = False
            sent_60 = False
            timed_out = False
            
            while not gen_task.done():
                elapsed = time.time() - start_time
                
                if elapsed > 130:
                    timed_out = True
                    break
                    
                if elapsed > 60 and not sent_60:
                    await _send_dm(node_id, "[Medi] Still thinking (60s)...", require_ack=True)
                    sent_60 = True
                    await asyncio.sleep(5)
                    
                elif elapsed > 30 and not sent_30:
                    await _send_dm(node_id, "[Medi] Still thinking (30s)...", require_ack=True)
                    sent_30 = True
                    await asyncio.sleep(5)
                    
                await asyncio.sleep(1)
                
            if timed_out:
                await _send_dm(node_id, "[Medi] Failed, please try again.", require_ack=True)
                _update_request_status(req_id, "error")
            else:
                reply_text = gen_task.result()
                final_text = f"{reply_text}{DISCLAIMER}"
                await _send_dm(node_id, final_text, require_ack=True)
                _update_request_status(req_id, "completed")
                
        except Exception as e:
            logger.error(f"Medi LLM error: {e}")
            await _send_dm(node_id, "[Medi] Error generating response.", require_ack=True)
            _update_request_status(req_id, "error")
        finally:
            _llm_queue.task_done()

def _on_receive(packet, interface=None):
    event_loop = _context.get("event_loop")
    if not event_loop:
        return

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

        if portnum not in ("TEXT_MESSAGE_APP", 1):
            return

        text = decoded.get("text") or ""
        if not text:
            payload = decoded.get("payload")
            if isinstance(payload, bytes):
                try:
                    text = payload.decode("utf-8", errors="replace")
                except Exception:
                    return

        if not text.lower().startswith("medi."):
            return

        cfg = _get_config()
        if not cfg["enabled"]:
            return

        from_id = packet.get("fromId") or ""
        if not from_id:
            raw_from = packet.get("from")
            if isinstance(raw_from, int):
                from_id = f"!{raw_from:08x}"
        if not from_id:
            return

        to_id = packet.get("toId") or ""
        is_broadcast = to_id == "^all" or packet.get("to") == 0xFFFFFFFF
        channel_idx = packet.get("channel") or packet.get("channelIndex") or 0

        if is_broadcast and channel_idx != cfg["channel_index"]:
            return

        query = text[5:].strip()
        if not query:
            return

        if query.lower() == "help":
            help_txt = (
                "[Medi AI Help]\n"
                "Send queries like: 'medi.my tooth hurts'.\n"
                "Sessions timeout after inactivity.\n"
                "All advice is AI generated and NOT medical fact."
            )
            asyncio.run_coroutine_threadsafe(_send_dm(from_id, help_txt, require_ack=True), event_loop)
            return

        if cfg.get("provider", "local") == "local" and cfg["status"] != "ready":
            asyncio.run_coroutine_threadsafe(
                _send_dm(from_id, f"[Medi] Offline AI is installing. Status: {cfg['status']}", require_ack=True),
                event_loop
            )
            return

        asyncio.run_coroutine_threadsafe(_send_dm(from_id, "[Medi] Thinking...", require_ack=True), event_loop)
        
        req_id = _enqueue_request(from_id, query)
        event_loop.call_soon_threadsafe(_llm_queue.put_nowait, {"id": req_id, "node_id": from_id, "query": query})

    except Exception as e:
        logger.error(f"Medi _on_receive error: {e}")

async def _watchdog(context: dict):
    wd = context.get("plugin_watchdog")
    pid = context.get("plugin_id")
    while True:
        try:
            await asyncio.sleep(30)
            if wd and pid:
                wd[pid] = time.time()
        except asyncio.CancelledError:
            return

def init_plugin(context: dict):
    global _context
    _context = context
    _init_db()

    cfg = _get_config()
    if cfg.get("provider", "local") == "local":
        if cfg["status"] == "uninitialized" or not os.path.exists(os.path.join(_BASE_DIR, cfg["model_file"])):
            threading.Thread(target=_download_model_worker, args=(cfg["model_repo"], cfg["model_file"]), daemon=True).start()

    threading.Thread(target=_reap_sessions_worker, daemon=True).start()

    try:
        pub.unsubscribe(_on_receive, "meshtastic.receive")
    except Exception:
        pass
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
        stats = {
            "total_sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "total_msgs": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        }
        conn.close()
        
    return {
        "config": cfg,
        "queue": queue,
        "sessions": sessions,
        "stats": stats
    }

@plugin_router.post("/config")
async def set_config(body: ConfigUpdate):
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields:
        return {"status": "ok"}
        
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _DB_LOCK:
        conn = _get_db()
        conn.execute(f"UPDATE config SET {set_clause} WHERE id=1", (*fields.values(),))
        conn.commit()
        conn.close()
    return {"status": "ok"}

@plugin_router.post("/config/reset_prompt")
async def reset_prompt():
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE config SET sys_prompt=? WHERE id=1", (DEFAULT_SYS_PROMPT,))
        conn.commit()
        conn.close()
    return {"status": "ok", "sys_prompt": DEFAULT_SYS_PROMPT}

@plugin_router.post("/model/download")
async def download_model(body: ModelDownloadReq):
    with _DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE config SET model_repo=?, model_file=? WHERE id=1", (body.model_repo, body.model_file))
        conn.commit()
        conn.close()
        
    threading.Thread(target=_download_model_worker, args=(body.model_repo, body.model_file), daemon=True).start()
    return {"status": "downloading"}

@plugin_router.get("/sessions/{sess_id}/messages")
async def get_session_msgs(sess_id: str):
    with _DB_LOCK:
        conn = _get_db()
        msgs = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY ts ASC", (sess_id,)).fetchall()]
        conn.close()
    return {"messages": msgs}