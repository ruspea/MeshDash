# Auto-extracted from meshtastic_dashboard.py
from fastapi import HTTPException, Request
import httpx
import re
import asyncio
import logging

logger = logging.getLogger(__name__)

async def available_plugins():
    """Fetches the official plugin manifest from meshdash.co.uk."""
    remote_url = "https://meshdash.co.uk/plugins"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(remote_url)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to plugin manifest timed out.")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Plugin server returned error {e.response.status_code}.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch plugin manifest: {str(e)}")


async def check_version_periodically():
    """Runs to check for updates automatically."""
    await asyncio.sleep(30)  # brief startup delay before first check
    while True:
        try:
            from core.routes.system_routes import get_version_status
            await get_version_status(notify=True)
            await asyncio.sleep(43200)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Background version check failed: {e}")
            await asyncio.sleep(300)


def _parse_version_number(v_str) -> tuple:
    try:
        clean = re.sub(r"[^0-9.]", "", str(v_str))
        parts = [int(p) for p in clean.split(".") if p.isdigit()]
        return tuple(parts) if parts else (0,)
    except Exception:
        return (0,)


