import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
from typing import List, Optional
from core.routes.schemas import User
from fastapi import HTTPException

import time as _time
import logging
logger = logging.getLogger(__name__)

_DEFAULT_CORS_ORIGINS = ["*"]
_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300
_login_failures: dict = {}


def _resolve_cors_origins() -> List[str]:
    """Resolve allowed CORS origins from config, falling back to safe defaults."""
    raw = g.loaded_config.get("CORS_ORIGINS", "")
    origins = []
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
    ws_port = g.loaded_config.get('WEBSERVER_PORT', 8181)
    origins += [f"http://localhost:{ws_port}", f"http://127.0.0.1:{ws_port}"]
    return origins if origins else _DEFAULT_CORS_ORIGINS


def _check_login_not_locked(username: str) -> Optional[str]:
    """Return error message if locked out, None if OK."""
    entry = _login_failures.get(username)
    if not entry:
        return None
    if entry.get("locked_until", 0) > _time.time():
        remaining = int(entry["locked_until"] - _time.time())
        return f"Account locked. Try again in {remaining}s."
    del _login_failures[username]
    return None


def _record_login_failure(username: str) -> None:
    """Record a failed login attempt; lock the account after MAX_ATTEMPTS."""
    entry = _login_failures.setdefault(username, {"attempts": 0, "locked_until": 0})
    entry["attempts"] += 1
    if entry["attempts"] >= _MAX_LOGIN_ATTEMPTS:
        entry["locked_until"] = _time.time() + _LOCKOUT_SECONDS
        logger.warning(f"Account '{username}' locked out for {_LOCKOUT_SECONDS}s after {entry['attempts']} failures.")


def _clear_login_failure(username: str) -> None:
    """Clear failure record on successful login."""
    _login_failures.pop(username, None)


def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r


def _require_admin(user: User):
    """Guard: raise 403 if user is not an admin (role 0)."""
    from starlette.responses import RedirectResponse, JSONResponse
    if isinstance(user, (HTTPException, RedirectResponse, JSONResponse)):
        return user
    if user.role != 0:
        raise HTTPException(403, "Admin access required.")


async def _inject_sw_header(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith("/sw.js"):
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
    return response


async def _inject_request_id(request, call_next):
    """Ensure every response has a request ID for log correlation."""
    import uuid
    rid = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response


async def _security_headers(request, call_next):
    response = await call_next(request)
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://unpkg.com https://use.fontawesome.com https://www.gstatic.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com https://use.fontawesome.com; "
        "img-src 'self' data: blob: https://*.tile.openstreetmap.org https://*.basemaps.cartocdn.com https://cdnjs.cloudflare.com https://fonts.gstatic.com https://www.google.com; "
        "connect-src 'self' ws: wss: https://unpkg.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://translate.googleapis.com https://translate-pa.googleapis.com; "
        "frame-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    response.headers["Content-Security-Policy"] = csp
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    
    # Set CSRF cookie if missing — required for verify_csrf on POST/PUT/DELETE
    csrf_cookie = request.cookies.get("csrf-token", "")
    if not csrf_cookie:
        from core.auth import _generate_csrf_token
        csrf_val = _generate_csrf_token()
        response.set_cookie("csrf-token", csrf_val, httponly=True, samesite="strict", path="/")
    
    return response