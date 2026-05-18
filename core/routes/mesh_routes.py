import core.globals as g
from core.c2 import send_system_message
from core.config_loader import _resolve_heartbeat
from core.geocode import _geocode_reverse, _geocode_cache
from core.utils import validate_url
# Auto-extracted from meshtastic_dashboard.py
import asyncio
import logging
from typing import Dict, List, Optional, Any
from fastapi import APIRouter, Request, Depends, HTTPException, Body, status
from fastapi.responses import JSONResponse
from core.routes.schemas import User, ConsoleRequest, MessageRequest, WebsiteMonitorRequest, URLRequest
from core.auth import verify_csrf, get_current_active_user, ensure_serializable

try:
    import httpx
    from bs4 import BeautifulSoup
except ImportError:
    httpx = None
    BeautifulSoup = None

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/extract")
async def extract(req: URLRequest, user: User = Depends(verify_csrf)):
    is_valid, reason = await asyncio.to_thread(validate_url, req.url)
    if not is_valid:
        raise HTTPException(400, f"Invalid Target: {reason}")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            resp = await client.get(req.url, headers={"User-Agent": "MeshDash/1.0"})
            if resp.status_code >= 400:
                raise HTTPException(400, f"Remote server returned {resp.status_code}")
            html_content = resp.text
    except httpx.RequestError as e:
        raise HTTPException(502, f"Connection failed: {str(e)}")

    def parse_html(html):
        soup = BeautifulSoup(html, "html.parser")
        data = []
        for i, el in enumerate(soup.find_all(["p", "article", "h1", "h2", "h3", "div"])):
            txt = el.get_text(strip=True)
            if txt and len(txt) > 20:
                data.append({"text": txt, "id": i, "tag": el.name})
        return data

    blocks = await asyncio.to_thread(parse_html, html_content)
    if req.block_id is not None:
        if req.block_id < len(blocks):
            return blocks[req.block_id]
        raise HTTPException(404, "Block ID not found")
    return {"blocks": blocks[:50]}


@router.get("/api/geocode-cache")
async def get_geocode_cache():
    """Return the geocode cache for the frontend overview."""
    return _geocode_cache