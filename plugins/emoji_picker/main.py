"""
Emoji Picker Plugin for MeshDash
==================================
Adds an emoji picker button next to all message input fields.
Pure frontend — no backend logic beyond serving the static files.
"""

import logging
from fastapi import APIRouter

plugin_router = APIRouter()
_logger = logging.getLogger("emoji_picker")


@plugin_router.get("/status")
async def api_status():
    return {
        "state": "ready",
        "ready": True,
        "plugin": "emoji_picker",
        "version": "1.0.0",
    }


def init_plugin(context: dict):
    global _logger
    _logger = context.get("logger") or logging.getLogger("emoji_picker")
    _logger.info("😀 Emoji Picker plugin v1.0.0 initialised — UI injection active")