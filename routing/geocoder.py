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
    source: str  # "postal" or "geonamescache"


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


import csv
import os

def _build_postal_index() -> dict[tuple[str, str], tuple[float, float]]:
    index = {}
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "us_places.csv")
    if not os.path.exists(path):
        return index
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader) # skip header
        for row in reader:
            if len(row) >= 4:
                place = _normalise(row[0])
                state = row[1]
                index[(place, state)] = (float(row[2]), float(row[3]))
    return index


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

    Returns ``None`` if no match is found.

    Parameters
    ----------
    city:
        City name exactly as it appears in the source data.
    state:
        Two-letter US state abbreviation (e.g. ``"TX"``).
    """
    norm_city = _normalise(city)

    # 1. Exact match (postal data first, then geonamescache)
    coords = _POSTAL_INDEX.get((norm_city, state))
    if coords:
        return GeoResult(*coords, source="postal")
    coords = _CITY_INDEX.get((norm_city, state))
    if coords:
        return GeoResult(*coords, source="geonamescache")

    # 2. Try progressively stripping common suffixes
    for suffix in _STRIP_SUFFIXES:
        if norm_city.endswith(suffix):
            candidate = norm_city[: -len(suffix)].strip()
            coords = _POSTAL_INDEX.get((candidate, state))
            if coords:
                return GeoResult(*coords, source="postal")
            coords = _CITY_INDEX.get((candidate, state))
            if coords:
                return GeoResult(*coords, source="geonamescache")

    # 3. Try removing everything after a slash or dash (e.g. "Abilene/Clyde")
    for sep in ("/", "-"):
        if sep in norm_city:
            candidate = norm_city.split(sep)[0].strip()
            coords = _POSTAL_INDEX.get((candidate, state))
            if coords:
                return GeoResult(*coords, source="postal")
            coords = _CITY_INDEX.get((candidate, state))
            if coords:
                return GeoResult(*coords, source="geonamescache")

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
_POSTAL_INDEX: dict[tuple[str, str], tuple[float, float]] = _build_postal_index()
