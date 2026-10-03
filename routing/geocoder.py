"""
Offline geocoder using the geonamescache US cities dataset.

Strategy (no network calls):
1. Build an index keyed by (normalised_city, state_abbr) → (lat, lng).
2. For each station, try an exact match on (city, state).
3. If that fails, try stripping common suffixes/punctuation and retry.
4. If still unmatched, fall back to state-centroid (so the station is not
   silently discarded, but callers can detect approximate matches via the
   ``exact`` flag).

All normalisation is deterministic and O(1) per lookup after the one-time
index build.
"""

import re
import unicodedata
from functools import lru_cache
from typing import NamedTuple

import geonamescache


class GeoResult(NamedTuple):
    """Result of a geocoding attempt."""

    lat: float
    lng: float
    exact: bool  # False when only a state-level fallback was found


# ---------------------------------------------------------------------------
# Index construction (built once at module import time)
# ---------------------------------------------------------------------------

_gc = geonamescache.GeonamesCache()


def _build_city_index() -> dict[tuple[str, str], tuple[float, float]]:
    """
    Return a mapping of (normalised_city, state) → (lat, lng).

    Only US cities are indexed.  When multiple entries share the same
    (city, state) key (e.g. multiple suburbs with the same name), the entry
    with the highest population is kept so that the most prominent location
    wins.
    """
    index: dict[tuple[str, str], tuple[float, float, int]] = {}
    for city in _gc.get_cities().values():
        if city["countrycode"] != "US":
            continue
        state: str = city["admin1code"]
        key = (_normalise(city["name"]), state)
        pop: int = city.get("population") or 0
        if key not in index or pop > index[key][2]:
            index[key] = (float(city["latitude"]), float(city["longitude"]), pop)

    return {k: (v[0], v[1]) for k, v in index.items()}


def _build_state_centroid_index() -> dict[str, tuple[float, float]]:
    """
    Return a rough centroid per US state, averaged from all geonamescache
    cities in that state.  Used only as a last-resort fallback.
    """
    buckets: dict[str, list[tuple[float, float]]] = {}
    for city in _gc.get_cities().values():
        if city["countrycode"] != "US":
            continue
        state = city["admin1code"]
        buckets.setdefault(state, []).append(
            (float(city["latitude"]), float(city["longitude"]))
        )

    return {
        state: (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
        )
        for state, pts in buckets.items()
    }


# Common city-name suffixes that sometimes differ between the CSV and geonames
_STRIP_SUFFIXES: tuple[str, ...] = (
    " twp",
    " township",
    " junction",
    " jct",
    " heights",
    " hts",
    " springs",
    " spgs",
    " beach",
    " park",
    " village",
    " vlg",
    " city",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def geocode(city: str, state: str) -> GeoResult | None:
    """
    Return a :class:`GeoResult` for the given US city + state abbreviation.

    Returns ``None`` only when no state-centroid exists either (should never
    happen for valid US states).

    Parameters
    ----------
    city:
        City name exactly as it appears in the source data.
    state:
        Two-letter US state abbreviation (e.g. ``"TX"``).
    """
    norm_city = _normalise(city)

    # 1. Exact match
    coords = _CITY_INDEX.get((norm_city, state))
    if coords:
        return GeoResult(*coords, exact=True)

    # 2. Try progressively stripping common suffixes
    for suffix in _STRIP_SUFFIXES:
        if norm_city.endswith(suffix):
            candidate = norm_city[: -len(suffix)].strip()
            coords = _CITY_INDEX.get((candidate, state))
            if coords:
                return GeoResult(*coords, exact=True)

    # 3. Try removing everything after a slash or dash (e.g. "Abilene/Clyde")
    for sep in ("/", "-"):
        if sep in norm_city:
            candidate = norm_city.split(sep)[0].strip()
            coords = _CITY_INDEX.get((candidate, state))
            if coords:
                return GeoResult(*coords, exact=True)

    # 4. State-centroid fallback
    centroid = _STATE_CENTROIDS.get(state)
    if centroid:
        return GeoResult(*centroid, exact=False)

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _normalise(text: str) -> str:
    """
    Normalise a city name for comparison:

    * Convert to ASCII (strip accents via unicode decomposition).
    * Lower-case.
    * Replace punctuation/whitespace runs with a single space.
    * Strip leading/trailing whitespace.
    """
    # Decompose Unicode and drop combining characters
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    lower = ascii_text.lower()
    # Collapse any sequence of non-alphanumeric chars to a space
    clean = re.sub(r"[^a-z0-9]+", " ", lower).strip()
    return clean


# Module-level singletons built once (after _normalise is defined)
_CITY_INDEX: dict[tuple[str, str], tuple[float, float]] = _build_city_index()
_STATE_CENTROIDS: dict[str, tuple[float, float]] = _build_state_centroid_index()
