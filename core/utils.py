# Auto-extracted from meshtastic_dashboard.py

import asyncio
from core.routes.schemas import NodeSlot
import urllib
from typing import Dict, Tuple
from urllib.parse import urlparse
import socket

NODE_REGISTRY: Dict[str, "NodeSlot"] = {}

def get_node_registry() -> Dict[str, "NodeSlot"]:
    """Return the live NODE_REGISTRY. External modules (task_scheduler, monitor, etc.)
    should call this rather than caching connection_manager at import time, so they
    always route to the correct slot at execution time."""
    return NODE_REGISTRY


def validate_url(url: str) -> Tuple[bool, str]:
    """Synchronous SSRF guard. Use only inside asyncio.to_thread."""
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return False, "Invalid URL structure"
        if not parsed.scheme in ("http", "https"):
            return False, "Only http/https allowed"
        ip = socket.gethostbyname(parsed.hostname)
        private_prefixes = ("127.", "10.", "192.168.", "172.16.", "172.17.",
                            "172.18.", "172.19.", "172.20.", "172.21.", "172.22.",
                            "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
                            "172.28.", "172.29.", "172.30.", "172.31.", "0.", "169.254.")
        if any(ip.startswith(prefix) for prefix in private_prefixes):
            return False, "Access to internal/private network is forbidden"
        if ip == "::1" or ip.startswith("fc") or ip.startswith("fd"):
            return False, "Access to internal IPv6 network is forbidden"
        return True, ""
    except Exception as e:
        return False, f"Could not resolve host: {e}"
