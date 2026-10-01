"""Basemap configuration shared by core and plugins.

OpenStreetMap is the default everywhere: it needs no key. CARTO (C2 Dark)
is used only when a CARTO key is configured.
"""

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


OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
CARTO_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, '
    '&copy; <a href="https://carto.com/attributions">CARTO</a>'
)


def carto_available() -> bool:
    return bool(get_carto_basemap_key())


def dark_tile(style: str = "dark_all") -> tuple:
    """(url, attribution) for the "C2 Dark" style: CARTO with a key, else OSM."""
    if carto_available():
        return raster_tile_url(style), CARTO_ATTRIBUTION
    return OSM_TILE_URL, OSM_ATTRIBUTION


def browser_config() -> dict:
    """What the browser gets as window.MeshDashBasemaps.

    `default` is what every map shows unless the user picks another style.
    `dark` stays for plugins that read it as a URL string; it now points at
    the default (OSM) so older plugin copies switch over too.
    """
    carto_url = raster_tile_url("dark_all") if carto_available() else None
    return {
        "default": OSM_TILE_URL,
        "defaultAttribution": OSM_ATTRIBUTION,
        "defaultMaxZoom": 19,
        "dark": OSM_TILE_URL,
        "darkAttribution": OSM_ATTRIBUTION,
        "cartoDark": carto_url,
        "cartoAttribution": CARTO_ATTRIBUTION,
    }
