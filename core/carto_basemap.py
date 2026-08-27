"""CARTO raster-basemap configuration shared by core and plugins."""

import os
from urllib.parse import quote


_DEFAULT_KEY_FILE = "/run/secrets/carto_basemap_key"
_BASE_URL = "https://{s}.basemaps.cartocdn.com"


def get_carto_basemap_key() -> str:
    """Read the CARTO key from the environment or a runtime-mounted file."""
    environment_key = os.environ.get("CARTO_BASEMAP_API_KEY", "").strip()
    if environment_key:
        return environment_key

    key_file = os.environ.get("CARTO_BASEMAP_KEY_FILE", _DEFAULT_KEY_FILE)
    try:
        with open(key_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def raster_tile_url(style: str = "dark_all") -> str:
    """Return a CARTO raster tile template with the current key when present."""
    url = f"{_BASE_URL}/{style}/{{z}}/{{x}}/{{y}}{{r}}.png"
    key = get_carto_basemap_key()
    return f"{url}?key={quote(key, safe='')}" if key else url
