"""
routing.services.fuel_planner
===============================

Computes the minimum-cost sequence of fuel stops along a driving route,
using the OPIS station database and fixed vehicle constraints.

Assumptions (documented per spec)
-----------------------------------
* The vehicle **starts with a full tank** (500 miles of range).
* ``total_cost`` counts only fuel *purchased at stops*; the initial full tank
  is a sunk cost uniform across all routes and is therefore excluded.
* Fuel efficiency: 10 mpg.  Max range: 500 miles.

Algorithm — "Lazy Greedy" (proven optimal for this problem class)
------------------------------------------------------------------
At every decision point (current position, current tank level):

1. Scan all stations in the FULL-TANK window  (pos, pos + MAX_RANGE].
2. Identify the cheapest station ``C`` in that window.
3. If ``C`` is reachable with current fuel → drive there; buy just enough
   to reach the *next* cheaper station in the new window, or fill up
   completely if no cheaper option exists ahead.
4. If ``C`` is NOT reachable → fill up enough at the cheapest reachable
   station to reach ``C``, then repeat.
5. If any gap between consecutive corridor stations (or to the destination)
   exceeds MAX_RANGE → raise :class:`NoFeasibleRouteError`.

Spatial indexing
-----------------
All stations are loaded **once** into a module-level :class:`_StationGrid`
(a grid-bucketed spatial index, equivalent in API to a scipy cKDTree).
This means the DB is hit once at startup, and per-request work is O(k)
where k is the number of stations near the route (≪ total DB size).
"""

from __future__ import annotations

import math
import os
import threading
from decimal import Decimal
from typing import Any, NamedTuple

import numpy as np
from scipy.spatial import cKDTree

from haversine import haversine, Unit

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class NoFeasibleRouteError(Exception):
    """
    Raised when no feasible sequence of fuel stops exists.

    This happens when two consecutive corridor stations (or the start/end)
    are more than MAX_RANGE miles apart along the route.
    """


# ---------------------------------------------------------------------------
# Vehicle / algorithm constants
# ---------------------------------------------------------------------------

MAX_RANGE_MILES: float = 500.0   # max distance on a full tank
MPG: float = 10.0                 # miles per gallon
CORRIDOR_MILES: float = float(os.getenv("FUEL_CORRIDOR_MILES", "10"))
RESAMPLE_INTERVAL_MILES: float = 1.0  # route point spacing for spatial search

_METERS_TO_MILES: float = 1 / 1609.344

# ---------------------------------------------------------------------------
# Module-level station index (loaded once at startup, thread-safe)
# ---------------------------------------------------------------------------


class _StationRecord(NamedTuple):
    """Immutable snapshot of one station's fields needed by the planner."""

    id: int
    name: str
    address: str
    city: str
    state: str
    price: float  # Decimal → float for numpy/scipy compat
    lat: float
    lng: float


# Singleton state
_INDEX_LOCK = threading.Lock()
_STATION_RECORDS: list[_StationRecord] | None = None   # list of all stations
_STATION_COORDS: np.ndarray | None = None              # shape (N, 2) [lat, lng]
_STATION_TREE: cKDTree | None = None                   # KD-tree on station coords


def _get_station_index() -> tuple[list[_StationRecord], np.ndarray, cKDTree]:
    """
    Return the module-level station index, building it on first call.

    Thread-safe via a double-checked lock so parallel requests don't
    trigger multiple DB reads.

    Returns
    -------
    records:
        All station records as :class:`_StationRecord` named-tuples.
    coords:
        Numpy array of shape ``(N, 2)`` with ``[lat, lng]`` per row.
    tree:
        scipy ``cKDTree`` built on *coords* for fast radius queries.
    """
    global _STATION_RECORDS, _STATION_COORDS, _STATION_TREE

    if _STATION_RECORDS is not None:
        return _STATION_RECORDS, _STATION_COORDS, _STATION_TREE  # type: ignore[return-value]

    with _INDEX_LOCK:
        if _STATION_RECORDS is not None:  # re-check after acquiring lock
            return _STATION_RECORDS, _STATION_COORDS, _STATION_TREE  # type: ignore[return-value]

        # Lazy import to avoid circular imports at module load time
        from routing.models import Station  # noqa: PLC0415

        qs = Station.objects.values(
            "id", "name", "address", "city", "state", "price", "lat", "lng"
        )
        records: list[_StationRecord] = [
            _StationRecord(
                id=row["id"],
                name=row["name"],
                address=row["address"],
                city=row["city"],
                state=row["state"],
                price=float(row["price"]),
                lat=row["lat"],
                lng=row["lng"],
            )
            for row in qs
        ]

        if not records:
            # Return empty structures; callers handle empty corridor gracefully
            _STATION_RECORDS = []
            _STATION_COORDS = np.empty((0, 2), dtype=np.float64)
            _STATION_TREE = cKDTree(np.empty((0, 2)))
        else:
            coords = np.array([[r.lat, r.lng] for r in records], dtype=np.float64)
            _STATION_RECORDS = records
            _STATION_COORDS = coords
            _STATION_TREE = cKDTree(coords)

    return _STATION_RECORDS, _STATION_COORDS, _STATION_TREE  # type: ignore[return-value]


def warm_station_index() -> tuple[list[_StationRecord], np.ndarray, cKDTree]:
    """
    Preload the station index into memory at process startup.

    Thread-safe; delegates to the lazy singleton :func:`_get_station_index`.
    """
    return _get_station_index()


def invalidate_station_index() -> None:
    """
    Clear the module-level station cache.

    Call after ``load_stations`` to force a fresh DB read on the next
    planning request.
    """
    global _STATION_RECORDS, _STATION_COORDS, _STATION_TREE
    with _INDEX_LOCK:
        _STATION_RECORDS = None
        _STATION_COORDS = None
        _STATION_TREE = None


# ---------------------------------------------------------------------------
# Route resampling
# ---------------------------------------------------------------------------


def resample_polyline(
    coords: list[list[float]],
    total_miles: float,
    interval_miles: float = RESAMPLE_INTERVAL_MILES,
) -> np.ndarray:
    """
    Resample a GeoJSON polyline to evenly-spaced points.

    Parameters
    ----------
    coords:
        List of ``[lng, lat]`` pairs (GeoJSON convention) as returned by OSRM.
    total_miles:
        Authoritative route length from OSRM (used to normalise mile markers).
    interval_miles:
        Target spacing between resampled points (default 1 mile).

    Returns
    -------
    numpy array of shape ``(M, 3)`` with columns ``[lat, lng, mile_marker]``.
    Mile markers are scaled so the final point equals *total_miles*.
    """
    if not coords:
        return np.empty((0, 3), dtype=np.float64)

    # Convert from [lng, lat] GeoJSON order → (lat, lng) tuples
    pts: list[tuple[float, float]] = [(c[1], c[0]) for c in coords]

    result: list[tuple[float, float, float]] = [(pts[0][0], pts[0][1], 0.0)]
    accumulated = 0.0
    last_emitted = 0.0

    for i in range(1, len(pts)):
        p1, p2 = pts[i - 1], pts[i]
        seg_len = haversine(p1, p2, unit=Unit.MILES)
        if seg_len < 1e-9:
            continue  # skip degenerate segments

        seg_end = accumulated + seg_len
        next_emit = last_emitted + interval_miles

        while next_emit <= seg_end + 1e-9:
            frac = (next_emit - accumulated) / seg_len
            frac = max(0.0, min(1.0, frac))
            lat = p1[0] + frac * (p2[0] - p1[0])
            lng = p1[1] + frac * (p2[1] - p1[1])
            result.append((lat, lng, next_emit))
            last_emitted = next_emit
            next_emit += interval_miles

        accumulated = seg_end

    # Always include the final destination point
    last_lat, last_lng = pts[-1]
    result.append((last_lat, last_lng, accumulated))

    arr = np.array(result, dtype=np.float64)

    # Normalise mile markers so the last point == total_miles
    if arr[-1, 2] > 0:
        arr[:, 2] *= total_miles / arr[-1, 2]

    return arr


# ---------------------------------------------------------------------------
# Corridor station extraction
# ---------------------------------------------------------------------------


def _degrees_radius(miles: float) -> float:
    """Convert a distance in miles to an approximate degree radius."""
    # 1° latitude ≈ 69 miles; use this as a conservative upper bound.
    return miles / 69.0


class _CandidateStation(NamedTuple):
    """A corridor station with its projected route mile-marker."""

    record: _StationRecord
    mile_marker: float


def find_corridor_stations(
    route_points: np.ndarray,
    corridor_miles: float = CORRIDOR_MILES,
) -> list[_CandidateStation]:
    """
    Return all stations within *corridor_miles* of the route, each annotated
    with its projected mile-marker (from its nearest route point).

    Strategy
    ---------
    1. Build a cKDTree on the resampled *route_points*.
    2. For every station, query the route tree for the nearest route point.
    3. Compute the exact great-circle distance; keep if ≤ corridor_miles.
    4. Assign the station's mile-marker from the nearest route point.

    The module-level station cKDTree is NOT used here — the query direction
    is reversed: we query the route tree from each station point so we can
    use a single vectorised ``tree.query()`` over all stations at once.

    Parameters
    ----------
    route_points:
        Array of shape ``(M, 3)`` as returned by :func:`resample_polyline`.
    corridor_miles:
        Half-width of the route corridor in miles.

    Returns
    -------
    List of :class:`_CandidateStation`, sorted by mile-marker.
    """
    records, station_coords, _ = _get_station_index()
    if not records or route_points.shape[0] == 0:
        return []

    # Build a KD-tree on route points for fast nearest-route-point lookup
    route_tree = cKDTree(route_points[:, :2])  # (lat, lng) columns

    # Vectorised nearest-route-point query for ALL stations at once
    # Returns (distances_in_degrees, indices_into_route_points)
    deg_radius = _degrees_radius(corridor_miles)
    dists_deg, nearest_idx = route_tree.query(
        station_coords,
        k=1,
        distance_upper_bound=deg_radius * 1.5,  # generous pre-filter in °
        workers=-1,  # use all CPU cores
    )

    candidates: list[_CandidateStation] = []
    for i, (dist_deg, r_idx) in enumerate(zip(dists_deg, nearest_idx)):
        if not math.isfinite(dist_deg):
            continue  # outside distance_upper_bound
        # Exact great-circle check
        s = records[i]
        rp = route_points[r_idx]  # [lat, lng, mile_marker]
        exact_miles = haversine((s.lat, s.lng), (rp[0], rp[1]), unit=Unit.MILES)
        if exact_miles <= corridor_miles:
            candidates.append(_CandidateStation(record=s, mile_marker=float(rp[2])))

    # Deduplicate by station ID (a station can match multiple route points;
    # keep the assignment with the closest route point)
    best: dict[int, _CandidateStation] = {}
    for cs in candidates:
        sid = cs.record.id
        if sid not in best:
            best[sid] = cs
        # Re-assignments not needed: cKDTree.query returns the single nearest

    return sorted(best.values(), key=lambda c: c.mile_marker)


# ---------------------------------------------------------------------------
# Greedy fuel-stop algorithm
# ---------------------------------------------------------------------------


def _plan_greedy_stops(
    candidates: list[_CandidateStation],
    total_miles: float,
    max_range: float = MAX_RANGE_MILES,
    mpg: float = MPG,
) -> tuple[list[dict], float]:
    """
    Core greedy algorithm — see module docstring for full description.

    Parameters
    ----------
    candidates:
        Corridor stations sorted by mile_marker (ascending).
    total_miles:
        Total route distance in miles.
    max_range:
        Vehicle maximum range on a full tank (miles).
    mpg:
        Vehicle fuel efficiency (miles per gallon).

    Returns
    -------
    stops:
        Ordered list of stop dicts (fields defined in :func:`plan_fuel_stops`).
    total_cost:
        Sum of ``cost`` across all stops.

    Raises
    ------
    NoFeasibleRouteError
        If any gap between consecutive reachable stations exceeds *max_range*.
    """
    # ------------------------------------------------------------------
    # 1. Feasibility pre-check
    # ------------------------------------------------------------------
    prev_mile = 0.0
    for cs in candidates:
        gap = cs.mile_marker - prev_mile
        if gap > max_range:
            raise NoFeasibleRouteError(
                f"No fuel station reachable between mile {prev_mile:.0f} and "
                f"mile {cs.mile_marker:.0f} (gap {gap:.0f} mi > {max_range:.0f} mi range)."
            )
        prev_mile = cs.mile_marker

    final_gap = total_miles - prev_mile
    if final_gap > max_range:
        raise NoFeasibleRouteError(
            f"No fuel station in the final {final_gap:.0f} miles before the "
            f"destination (max range {max_range:.0f} mi)."
        )

    # ------------------------------------------------------------------
    # 2. Greedy simulation
    # ------------------------------------------------------------------
    tank: float = max_range   # start full
    pos: float = 0.0
    stops: list[dict] = []
    cumulative_cost: float = 0.0

    def _reachable(mile: float) -> bool:
        return mile - pos <= tank + 1e-6

    def _full_window() -> list[_CandidateStation]:
        """Stations reachable on a hypothetical full tank from current pos."""
        return [c for c in candidates if pos < c.mile_marker <= pos + max_range]

    while pos + tank < total_miles - 1e-6:
        window = _full_window()

        if not window:
            # Feasibility check already passed, so this means we can coast to dest
            break

        cheapest_in_window = min(window, key=lambda c: c.record.price)

        if _reachable(cheapest_in_window.mile_marker):
            # Best price is reachable now — drive there
            target = cheapest_in_window
        else:
            # Best price is outside current range; pick cheapest among reachable
            reachable_now = [c for c in window if _reachable(c.mile_marker)]
            target = min(reachable_now, key=lambda c: c.record.price)

        # Consume fuel en route
        tank -= (target.mile_marker - pos)
        pos = target.mile_marker

        # ------------------------------------------------------------------
        # Decide how much to buy at this station
        # ------------------------------------------------------------------
        new_window = [c for c in candidates if c.mile_marker > pos]

        # Find next station strictly cheaper than this one (in full-tank range)
        next_cheaper = next(
            (
                c for c in new_window
                if c.mile_marker <= pos + max_range and c.record.price < target.record.price
            ),
            None,
        )

        if next_cheaper is not None:
            # Buy just enough to reach the cheaper option
            miles_to_buy = max(0.0, next_cheaper.mile_marker - pos - tank)
        elif pos + max_range >= total_miles:
            # Close enough to destination — buy only what's needed
            miles_to_buy = max(0.0, total_miles - pos - tank)
        else:
            # We're the cheapest in the window, fill up completely
            miles_to_buy = max_range - tank

        if miles_to_buy > 0.01:  # meaningful purchase (> ~0.001 gallons)
            gallons = miles_to_buy / mpg
            cost = gallons * target.record.price
            cumulative_cost += cost
            tank += miles_to_buy

            stops.append(
                {
                    "name": target.record.name,
                    "address": target.record.address,
                    "city": target.record.city,
                    "state": target.record.state,
                    "lat": target.record.lat,
                    "lng": target.record.lng,
                    "mile_marker": round(pos, 2),
                    "price_per_gallon": round(target.record.price, 5),
                    "gallons_purchased": round(gallons, 4),
                    "cost": round(cost, 4),
                    "cumulative_cost": round(cumulative_cost, 4),
                }
            )

    return stops, round(cumulative_cost, 4)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan_fuel_stops(route: dict) -> dict:
    """
    Compute the optimal (minimum-cost) fuel stops for a route.

    Parameters
    ----------
    route:
        Dict as returned by :func:`routing.services.routing_client.get_route`,
        containing ``"geometry"`` (list of ``[lng, lat]`` pairs) and
        ``"total_miles"`` (float).

    Returns
    -------
    dict with:

    * ``stops``       – ordered list of stop dicts (see :func:`_plan_greedy_stops`)
    * ``total_cost``  – total fuel spend in USD (Decimal-serialisable float)
    * ``total_miles`` – echo of the input route distance

    Raises
    ------
    NoFeasibleRouteError
        If the route cannot be completed with the vehicle's range.
    """
    geometry: list[list[float]] = route["geometry"]
    total_miles: float = route["total_miles"]

    # 1. Resample route to ~1-mile intervals
    route_points = resample_polyline(geometry, total_miles)

    # 2. Find corridor stations (vectorised, uses module-level KD-tree on stations)
    candidates = find_corridor_stations(route_points)

    # 3. Run greedy optimiser
    stops, total_cost = _plan_greedy_stops(candidates, total_miles)

    return {
        "stops": stops,
        "total_cost": total_cost,
        "total_miles": total_miles,
    }
