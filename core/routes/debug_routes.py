"""
Debug routes — /api/debug/*

A privacy-conscious diagnostics endpoint for the Debug page:
  GET /api/debug/info     — version, install method, uptime, plugins, config flags
  GET /api/debug/errors   — recent WARNING/ERROR/CRITICAL log lines (core + plugins)
  GET /api/debug/logs     — full recent log buffer as a downloadable text file

Nothing here returns secret values (API keys, passwords, tokens). Config is
reported as booleans ("configured: yes/no") and log lines are redacted for
common secret patterns before leaving the server.
"""

import os
import platform
import re
import sys
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from core.auth import get_current_active_user, User

router = APIRouter(prefix="/api/debug", tags=["debug"])

# Captured when this module is first imported (i.e. at app startup) —
# a good-enough proxy for process start time.
_PROC_START = time.time()

# ---------------------------------------------------------------------------
# Redaction — never let a secret value leave the server
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r'(?i)\b([A-Za-z0-9_.-]*(?:key|password|passwd|token|secret|psk)[A-Za-z0-9_.-]*)\s*[:=]\s*("?)([^\s",&]{4,})\2',
)


def _redact(text: str) -> str:
    """Mask values of anything that looks like key/password/token/secret."""
    return _SECRET_KEY_RE.sub(lambda m: f"{m.group(1)}={m.group(2)}***REDACTED***{m.group(2)}", text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_method() -> str:
    """docker / docker-runner / systemd (native) / native."""
    try:
        if os.path.exists("/.dockerenv"):
            return "docker"
        if any("/docker" in p.lower() or "/opt/venv/bin" in p for p in sys.path):
            return "docker"
    except Exception:
        pass
    if os.environ.get("INVOCATION_ID") or os.path.exists("/run/systemd/system"):
        return "native (systemd)"
    return "native"


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _core_log_handler():
    import core.globals as _g
    return getattr(_g, "_core_log_handler", None)


def _plugin_registry() -> Dict[str, Any]:
    import core.globals as _g
    return getattr(_g, "PLUGIN_REGISTRY", None) or {}


def _plugin_log_handlers() -> Dict[str, Any]:
    import core.globals as _g
    return getattr(_g, "_plugin_log_handlers", None) or {}


def _last_update_summary() -> Dict[str, Any] | None:
    try:
        from core.update import read_state
        data_dir = os.path.join(_install_root(), "data")
        st = read_state(data_dir)
        if not st:
            return None
        return {k: st.get(k) for k in
                ("phase", "from_version", "to_version", "reason", "updated_at")
                if st.get(k)}
    except Exception:
        return None


def _install_root() -> str:
    """The panel's install directory (this file lives in <root>/core/routes/)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_size_bytes() -> int | None:
    try:
        p = os.path.join(_install_root(), "data", "meshtastic_data.db")
        return os.path.getsize(p) if os.path.exists(p) else None
    except Exception:
        return None


def _human_bytes(n: int | None) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def _plugin_summary() -> List[Dict[str, Any]]:
    out = []
    reg = _plugin_registry()
    wd = None
    try:
        import core.globals as _g
        wd = getattr(_g, "_plugin_watchdog", None)
    except Exception:
        pass
    for pid, entry in sorted(reg.items()):
        manifest = entry.get("manifest") or {}
        last_wd = None
        if wd and pid in wd:
            try:
                last_wd = round(time.time() - wd[pid], 1)
            except Exception:
                pass
        out.append({
            "id": pid,
            "name": manifest.get("name", pid),
            "version": manifest.get("version", "?"),
            "author": manifest.get("author", "?"),
            "status": entry.get("status", "?"),
            "error": entry.get("error"),
            "watchdog": bool(manifest.get("watchdog")),
            "last_wd_ping_s": last_wd,
            "loaded_at": entry.get("loaded_at"),
        })
    return out


def _config_flags() -> Dict[str, Any]:
    """Non-secret config facts only — booleans and harmless values."""
    import core.globals as _g
    cfg = getattr(_g, "loaded_config", None) or {}
    return {
        "public_mode": bool(getattr(_g, "PUBLIC_MODE", False)),
        "log_level": str(cfg.get("LOG_LEVEL", "INFO")),
        "community_key_configured": bool(getattr(_g, "COMMUNITY_API_KEY", "")),
        "auth_secret_configured": bool(getattr(_g, "AUTH_SECRET_KEY", "")),
        "community_host": str(cfg.get("COMMUNITY_API_HOST", "https://meshdash.co.uk")),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/info")
async def debug_info(request: Request, user: User = Depends(get_current_active_user)):
    import core.globals as _g
    app = getattr(_g, "app", None)
    version = getattr(app, "version", "?") if app else "?"

    node_registry = getattr(_g, "NODE_REGISTRY", None) or {}
    slots = []
    for sid, slot in node_registry.items():
        try:
            nid = slot.meshtastic_data.local_node_id
        except Exception:
            nid = None
        slots.append({"slot_id": sid, "local_node_id": nid})

    upd = _last_update_summary()
    return {
        "panel_version": version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "install_method": _install_method(),
        "install_root_basename": os.path.basename(_install_root()),
        "process_uptime_s": round(time.time() - _PROC_START, 1),
        "process_uptime_human": _fmt_duration(time.time() - _PROC_START),
        "last_update": upd,
        "db_size": _human_bytes(_db_size_bytes()),
        "slots": slots,
        "plugins": _plugin_summary(),
        "config": _config_flags(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/errors")
async def debug_errors(request: Request, user: User = Depends(get_current_active_user)):
    """Recent WARNING and above from the core buffer + plugin buffers + registry errors."""
    errors: List[Dict[str, Any]] = []

    h = _core_log_handler()
    if h:
        for line in h.get_lines():
            if line.get("lvl") in ("WARNING", "ERROR", "CRITICAL"):
                errors.append({
                    "source": "core",
                    "t": line.get("t"),
                    "lvl": line.get("lvl"),
                    "msg": _redact(str(line.get("msg", "")))[:2000],
                })

    for pid, ph in _plugin_log_handlers().items():
        try:
            for line in ph.get_lines():
                if line.get("lvl") in ("WARNING", "ERROR", "CRITICAL"):
                    errors.append({
                        "source": f"plugin:{pid}",
                        "t": line.get("t"),
                        "lvl": line.get("lvl"),
                        "msg": _redact(str(line.get("msg", "")))[:2000],
                    })
        except Exception:
            pass

    # Plugin load/start failures recorded in the registry
    for p in _plugin_summary():
        if p.get("error"):
            errors.append({
                "source": f"plugin:{p['id']}",
                "t": p.get("loaded_at"),
                "lvl": "ERROR",
                "msg": _redact(str(p["error"]))[:2000],
            })

    errors.sort(key=lambda e: e.get("t") or 0)
    return {"count": len(errors), "errors": errors[-300:]}


@router.get("/logs")
async def debug_logs(request: Request, user: User = Depends(get_current_active_user)):
    """Full recent log buffer as a downloadable .txt file (redacted)."""
    lines: List[str] = [
        f"MeshDash debug logs — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"version={getattr(getattr(__import__('core.globals', fromlist=['app']), 'app', None), 'version', '?')}",
        f"install_method={_install_method()}",
        "=" * 70,
        "",
        "--- CORE LOG ---",
    ]

    h = _core_log_handler()
    if h:
        for line in h.get_lines():
            lines.append(_redact(str(line.get("msg", ""))))
    else:
        lines.append("(core log buffer not available)")

    for pid, ph in _plugin_log_handlers().items():
        lines += ["", f"--- PLUGIN: {pid} ---"]
        try:
            for line in ph.get_lines():
                lines.append(_redact(str(line.get("msg", ""))))
        except Exception as e:
            lines.append(f"(error reading plugin log: {e})")

    body = "\n".join(lines)
    fname = f"meshdash-debug-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    return PlainTextResponse(
        body,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )