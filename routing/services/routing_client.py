"""
routing.services.routing_client
=================================

Provides a single public function, :func:`get_route`, that:

1. Accepts start/end as either ``"lat,lng"`` coordinate strings or free-text
   US place names.
2. Geocodes free-text inputs via the Nominatim API (at most one call per
   distinct place name, results LRU-cached so repeat inputs are free).
3. Fetches the driving route from the OSRM public API (exactly one HTTP call).
4. Returns a plain dict suitable for direct JSON serialisation.

External API budget per request
--------------------------------
* Free-text start  : 0 or 1 Nominatim call
* Free-text end    : 0 or 1 Nominatim call
* Route            : exactly 1 OSRM call
* Total            : 1-3 calls (never more)

Caching
--------
Both geocoding and routing results are held in in-process LRU caches so that
repeated identical requests make zero external calls.
"""

import os
import re
from functools import lru_cache

import requests

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class GeocodingError(Exception):
    """Raised when a place name cannot be resolved to coordinates."""


class RoutingError(Exception):
    """Raised when the OSRM routing API returns an unexpected response."""


# ---------------------------------------------------------------------------
# Constants / configuration
# ---------------------------------------------------------------------------

# Regex: optional sign, digits, optional decimal, comma, optional space, repeat
_COORD_RE = re.compile(
    r"^(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*(?P<lng>-?\d+(?:\.\d+)?)$"
)

_METERS_PER_MILE: float = 1609.344
_SECONDS_PER_MINUTE: float = 60.0

# Read from environment (set in .env); fall back to public defaults
_NOMINATIM_BASE: str = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
_OSRM_BASE: str = os.getenv(
    "ROUTING_BASE_URL", "http://router.project-osrm.org"
)
_USER_AGENT: str = os.getenv("USER_AGENT", "fuel-assessment/1.0 (dev@example.com)")

# Shared session for connection pooling
_SESSION: requests.Session = requests.Session()
_SESSION.headers.update({"User-Agent": _USER_AGENT})

_CONNECT_TIMEOUT: int = 5   # seconds to establish connection
_READ_TIMEOUT: int = 30     # seconds to wait for response body


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_route(start: str, end: str) -> dict:
    """
    Return the driving route between two US locations.

    Parameters
    ----------
    start:
        Either a ``"lat,lng"`` string (e.g. ``"41.8781,-87.6298"``) or a
        free-text place name (e.g. ``"Chicago, IL"``).
    end:
        Same format as *start*.

    Returns
    -------
    dict with keys:

    * ``total_miles``       – float, total driving distance in miles
    * ``duration_minutes``  – float, estimated driving time in minutes
    * ``geometry``          – list of ``[lng, lat]`` coordinate pairs (GeoJSON
                              LineString coordinate format)

    Raises
    ------
    GeocodingError
        If a free-text place cannot be geocoded.
    RoutingError
        If OSRM returns a non-OK response or an unexpected structure.
    requests.exceptions.Timeout
        If any HTTP call exceeds its timeout.
    requests.exceptions.ConnectionError
        If a network connection fails.
    """
    return _get_route_cached(start.strip(), end.strip())


# ---------------------------------------------------------------------------
# Internal — cached implementation
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _get_route_cached(start: str, end: str) -> dict:
    """
    Cache wrapper around the actual route fetch.

    Keyed by the exact (start, end) strings so that:
    * ``("Chicago, IL", "Los Angeles, CA")`` and
    * ``("41.8781,-87.6298", "34.0522,-118.2437")``
    are different cache entries (both fine; the latter re-uses geocode cache).
    """
    start_lat, start_lng = _resolve_location(start)
    end_lat, end_lng = _resolve_location(end)
    return _fetch_osrm_route(start_lat, start_lng, end_lat, end_lng)


@lru_cache(maxsize=512)
def _resolve_location(location: str) -> tuple[float, float]:
    """
    Return ``(lat, lng)`` for *location*.

    If *location* matches the coordinate pattern, parse it directly.
    Otherwise geocode via Nominatim (result is LRU-cached by place string).

    Raises
    ------
    GeocodingError
        If Nominatim returns no results.
    """
    m = _COORD_RE.match(location)
    if m:
        return float(m.group("lat")), float(m.group("lng"))
    return _geocode_nominatim(location)


def _geocode_nominatim(place: str) -> tuple[float, float]:
    """
    Geocode a US place name via Nominatim.

    Restricts search to ``countrycodes=us`` to avoid ambiguous international
    matches.  Uses the module-level ``_SESSION`` so the ``User-Agent`` header
    is always set (required by the Nominatim usage policy).

    Parameters
    ----------
    place:
        Free-text location string, e.g. ``"Houston, TX"`` or ``"JFK Airport"``.

    Returns
    -------
    tuple of (lat, lng) as floats.

    Raises
    ------
    GeocodingError
        If no result is found.
    """
    params = {
        "q": place,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    url = f"{_NOMINATIM_BASE}/search"
    try:
        resp = _SESSION.get(url, params=params, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
        resp.raise_for_status()
        results = resp.json()
    except requests.exceptions.HTTPError as exc:
        raise GeocodingError(f"Nominatim HTTP error for '{place}': {exc}") from exc

    if not results:
        raise GeocodingError(
            f"Could not geocode '{place}'. "
            "Provide a more specific US address or use 'lat,lng' directly."
        )

    return float(results[0]["lat"]), float(results[0]["lon"])


def _fetch_osrm_route(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
) -> dict:
    """
    Call the OSRM driving-route endpoint and parse the response.

    Uses ``overview=full`` to receive every geometry point and
    ``geometries=geojson`` so coordinates are in ``[lng, lat]`` order
    (GeoJSON standard), ready to embed in a Feature directly.

    Parameters
    ----------
    start_lat, start_lng:
        Origin coordinates.
    end_lat, end_lng:
        Destination coordinates.

    Returns
    -------
    dict with ``total_miles``, ``duration_minutes``, and ``geometry``.

    Raises
    ------
    RoutingError
        On a non-OK OSRM status code or a malformed response body.
    """
    # OSRM coordinate order: lng,lat (GeoJSON convention)
    coordinates = f"{start_lng},{start_lat};{end_lng},{end_lat}"
    url = f"{_OSRM_BASE}/route/v1/driving/{coordinates}"
    params = {
        "overview": "full",
        "geometries": "geojson",
    }

    try:
        resp = _SESSION.get(url, params=params, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        raise RoutingError(f"OSRM HTTP error: {exc}") from exc

    # Validate OSRM application-level status
    if data.get("code") != "Ok":
        raise RoutingError(
            f"OSRM returned non-OK status '{data.get('code')}': "
            f"{data.get('message', 'no message')}"
        )

    try:
        route = data["routes"][0]
        # OSRM aggregates legs; sum them for multi-stop correctness (single
        # leg here, but defensive coding pays off)
        total_distance_m: float = sum(leg["distance"] for leg in route["legs"])
        total_duration_s: float = sum(leg["duration"] for leg in route["legs"])
        geometry_coords: list[list[float]] = route["geometry"]["coordinates"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RoutingError(f"Unexpected OSRM response structure: {exc}") from exc

    return {
        "total_miles": round(total_distance_m / _METERS_PER_MILE, 4),
        "duration_minutes": round(total_duration_s / _SECONDS_PER_MINUTE, 2),
        "geometry": geometry_coords,  # already [[lng, lat], ...]
    }
