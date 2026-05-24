"""
HTML page serving routes for Mesh Dashboard.
Extracted from meshtastic_dashboard.py (2026-04-13).
Updated 2026-04-26 to use core.globals module instead of globals() dict.
"""
import core.globals as g
from fastapi import Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jose import JWTError, jwt
import asyncio
import httpx
import os
import secrets
import logging

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"


def _no_cache(r):
    """Set no-cache headers on a response."""
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r


def _inject_csrf(response, token):
    """Inject CSRF token into HTML meta tag and set cookie."""
    _no_cache(response)
    try:
        with open(response.path, 'r', encoding='utf-8') as f:
            html = f.read()
    except Exception:
        return response
    html = html.replace(
        '<meta name="csrf-token" content="">',
        f'<meta name="csrf-token" content="{token}">'
    )
    html = html.replace(
        "window._csrfToken = document.querySelector('meta[name=\\\"csrf-token\\\"]')?.content || '';",
        f"window._csrfToken = '{token}';"
    )
    r = HTMLResponse(content=html)
    _no_cache(r)
    r.set_cookie(key="csrf_token", value=token, httponly=True, samesite="strict")
    return r


def _resolve_community():
    """Load community server URL from config."""
    # Try data/ location first (R3.0+), then legacy root location
    _base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _data_cfg = os.path.join(_base, "data", ".mesh-dash_config")
    _root_cfg = os.path.join(_base, ".mesh-dash_config")
    cfg_path = _data_cfg if os.path.exists(_data_cfg) else _root_cfg
    try:
        from core.config_loader import load_configuration
        cfg = load_configuration(cfg_path)
        return cfg.get("COMMUNITY_SERVER", "https://www.communitymesh.com")
    except Exception:
        return "https://www.communitymesh.com"


# Page route definitions
PAGE_ROUTES = [
    ("/map", "MAP_HTML_PATH"),
    ("/dmes", "DMES_HTML_PATH"),
    ("/settings", "SETTINGS_HTML_PATH"),
    ("/channels", "PUBLIC_HTML_PATH"),
    ("/sensors", "SENSORS_HTML_PATH"),
    ("/hook", "HOOK_HTML_PATH"),
    ("/tasks", "TASKS_HTML_PATH"),
    ("/documentation", "DOX_HTML_PATH"),
    ("/compare", "COMPARE_HTML_PATH"),
    ("/shark", "SHARK_HTML_PATH"),
    ("/plugins", "PLUGINS_HTML_PATH"),
]


def register_all(app_ref, globals_dict):
    """Register all HTML page routes with the app."""
    from core.routes.schemas import User
    from core.auth import get_current_active_user

    # GET / — dashboard home
    @app_ref.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        csrf_token = secrets.token_urlsafe(32)
        if g.PUBLIC_MODE:
            if os.path.exists(g.INDEX_HTML_PATH):
                return _inject_csrf(FileResponse(g.INDEX_HTML_PATH), csrf_token)
            return HTMLResponse("Dashboard index.html missing", 404)
        user_count = await asyncio.to_thread(g.db_manager.count_users)
        if user_count == 0:
            # No users yet — redirect to setup wizard
            return RedirectResponse("/setup", 302)
        token = request.cookies.get("access_token")
        if not token or not token.startswith("Bearer "):
            return RedirectResponse("/login", 302)
        try:
            payload = jwt.decode(
                token.split(" ")[1], g.AUTH_SECRET_KEY, algorithms=[ALGORITHM]
            )
            username = payload.get("sub")
            if not username:
                raise JWTError("No username in token")
            user = await asyncio.to_thread(g.db_manager.get_user, username)
            if not user or user.get("disabled"):
                return RedirectResponse("/login", 302)
            if os.path.exists(g.INDEX_HTML_PATH):
                return _inject_csrf(FileResponse(g.INDEX_HTML_PATH), csrf_token)
            return HTMLResponse("Dashboard index.html missing", 404)
        except JWTError:
            return RedirectResponse("/login", 302)
        except Exception as e:
            logger.error(f"❌ Unexpected error in root route: {e}", exc_info=True)
            return RedirectResponse("/login", 302)

    # Static page routes (/map, /dmes, /settings, etc.)
    for route_path, path_const in PAGE_ROUTES:
        html_path = getattr(g, path_const)

        def make_handler(p):
            async def page(r: Request, u: User = Depends(get_current_active_user)):
                if os.path.exists(p):
                    return _no_cache(FileResponse(p))
                return HTMLResponse("Missing", 404)
            return page

        app_ref.get(route_path, response_class=HTMLResponse)(make_handler(html_path))

    # GET /community — community server proxy
    @app_ref.get("/community", response_class=HTMLResponse)
    async def community(u: User = Depends(get_current_active_user)):
        if not g.meshtastic_data.local_node_id:
            return HTMLResponse("Local ID Unknown. Ensure radio is connected.", 503)
        _node_id = g.meshtastic_data.local_node_id or "unknown"
        target_url = f"{_resolve_community()}?node_id={_node_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    target_url,
                    headers={"X-Api-Key": g.loaded_config.get("COMMUNITY_API_KEY", ""), "X-Node-Id": _node_id},
                )
                if response.status_code != 200:
                    return HTMLResponse(
                        f"Community Server Error: {response.status_code}", status_code=502
                    )
                return _no_cache(HTMLResponse(content=response.text))
        except httpx.RequestError:
            return HTMLResponse(
                "Connection Error: Could not reach community server.", 504
            )
        except Exception as e:
            return HTMLResponse(f"Internal Dashboard Error: {e}", 500)

    # GET /sse-debug — SSE debug page
    @app_ref.get("/sse-debug", response_class=HTMLResponse)
    async def sse_debug_page(request: Request):
        token = request.cookies.get("access_token")
        if not token or not token.startswith("Bearer "):
            return RedirectResponse("/login", 302)
        try:
            username = jwt.decode(
                token.split(" ")[1], g.AUTH_SECRET_KEY, algorithms=[ALGORITHM]
            ).get("sub")
            if not username:
                return RedirectResponse("/login", 302)
            user = await asyncio.to_thread(g.db_manager.get_user, username)
            if not user or user.get("disabled"):
                return RedirectResponse("/login", 302)
        except Exception:
            return RedirectResponse("/login", 302)
        path = os.path.join(g.STATIC_DIR, "sse_dump.html")
        if not os.path.exists(path):
            return HTMLResponse("<h1>SSE Debug File Missing</h1>", 404)
        return _no_cache(FileResponse(path))