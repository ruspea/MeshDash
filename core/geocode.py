import core.globals as g
# Auto-extracted from meshtastic_dashboard.py
from core.routes.schemas import User
import httpx
import time
import json
import asyncio
from pathlib import Path

_geocode_cache: dict = {}
_geocode_last_request: float = 0.0
_NOMINATIM_MIN_INTERVAL = 1.1
_NOMINATIM_UA = "MeshDash/2.0 (meshtastic dashboard)"


def _geocode_cache_path() -> Path:
    """Resolve the cache file path lazily so DATA_DIR is set by the time we need it."""
    return Path(g.DATA_DIR) / "geocode_cache.json"

def _load_geocode_cache() -> None:
    global _geocode_cache
    try:
        cache_file = _geocode_cache_path()
        if cache_file.exists():
            with open(cache_file, "r", encoding="utf-8") as f:
                _geocode_cache = json.load(f)
    except Exception:
        _geocode_cache = {}


def _save_geocode_cache() -> None:
    try:
        cache_file = _geocode_cache_path()
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(_geocode_cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _cache_key(lat: float, lon: float) -> str:
    """3dp (~111m) — tolerant of GPS drift, still accurate for road names."""
    return f"{round(lat, 3)},{round(lon, 3)}"


async def _geocode_reverse(lat: float, lon: float) -> dict:
    global _geocode_last_request
    key = _cache_key(lat, lon)
    if key in _geocode_cache:
        return {"cached": True, "key": key, **_geocode_cache[key]}
    now = time.monotonic()
    wait = _NOMINATIM_MIN_INTERVAL - (now - _geocode_last_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _geocode_last_request = time.monotonic()
    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&addressdetails=1"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={
                "User-Agent": _NOMINATIM_UA,
                "Accept-Language": "en",
                "Referer": "http://localhost",
            })
    except Exception as e:
        return {"error": str(e), "cached": False}
    if resp.status_code == 429:
        return {"error": "rate_limited", "cached": False}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "cached": False}
    try:
        data = resp.json()
    except Exception:
        return {"error": "bad_json", "cached": False}
    a = data.get("address", {})
    named = (data.get("name") or a.get("shop") or a.get("amenity") or a.get("building") or a.get("road") or "")
    area  = (a.get("suburb") or a.get("village") or a.get("town") or a.get("city") or a.get("county") or "")
    if named and area:
        short = f"{named}, {area}"
    elif named:
        short = named
    elif area:
        short = area
    else:
        parts = data.get("display_name", "").split(",")
        short = ", ".join(p.strip() for p in parts[:2] if p.strip())
    result = {
        "short":    short or "Unknown location",
        "full":     data.get("display_name", ""),
        "city":     a.get("city") or a.get("town") or a.get("village") or "",
        "county":   a.get("county") or "",
        "country":  a.get("country") or "",
        "postcode": a.get("postcode") or "",
    }
    _geocode_cache[key] = result
    _save_geocode_cache()
    return {"cached": False, "key": key, **result}


