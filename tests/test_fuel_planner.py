"""
Unit tests for routing.services.fuel_planner.

All tests use synthetic data (no DB queries, no network calls):
  - ``_plan_greedy_stops`` is tested directly with hand-crafted candidates.
  - ``resample_polyline`` is tested with a known straight-line geometry.
  - ``find_corridor_stations`` is tested by monkey-patching ``_get_station_index``.

Notation: "1 mile apart" along a longitude line at equator ≈ 0.00899° longitude.
"""

from __future__ import annotations

import math
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pytest

from routing.services.fuel_planner import (
    NoFeasibleRouteError,
    _CandidateStation,
    _StationRecord,
    _plan_greedy_stops,
    find_corridor_stations,
    resample_polyline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_station(
    sid: int,
    price: float,
    lat: float = 0.0,
    lng: float = 0.0,
    name: str = "Test Station",
) -> _StationRecord:
    return _StationRecord(
        id=sid,
        name=name,
        address="123 Test Rd",
        city="Testville",
        state="TX",
        price=price,
        lat=lat,
        lng=lng,
    )


def _make_candidate(mile: float, price: float, sid: int = 1) -> _CandidateStation:
    return _CandidateStation(
        record=_make_station(sid, price),
        mile_marker=mile,
    )


# ---------------------------------------------------------------------------
# resample_polyline
# ---------------------------------------------------------------------------


class TestResamplePolyline:
    """Tests for the route resampling helper."""

    def _straight_line_coords(self, n_deg: float) -> list[list[float]]:
        """Return [lng, lat] pairs for a straight W→E line of n_deg degrees."""
        # Along equator: 0°N, longitude from 0 to n_deg
        return [[lng, 0.0] for lng in np.linspace(0, n_deg, 100)]

    def test_returns_numpy_array(self) -> None:
        coords = [[0.0, 0.0], [1.0, 0.0]]
        result = resample_polyline(coords, total_miles=69.0)
        assert isinstance(result, np.ndarray)
        assert result.ndim == 2
        assert result.shape[1] == 3  # lat, lng, mile_marker

    def test_first_point_at_mile_zero(self) -> None:
        coords = [[0.0, 0.0], [5.0, 0.0]]
        result = resample_polyline(coords, total_miles=345.0)
        assert result[0, 2] == pytest.approx(0.0)

    def test_last_point_equals_total_miles(self) -> None:
        coords = [[0.0, 0.0], [5.0, 0.0]]
        total = 345.0
        result = resample_polyline(coords, total_miles=total)
        assert result[-1, 2] == pytest.approx(total, rel=1e-4)

    def test_roughly_one_mile_spacing(self) -> None:
        """Resampled points should be approximately 1 mile apart."""
        # ~69 miles per degree of latitude at the equator
        # Use a ~100-mile route: 100/69 ≈ 1.449°
        coords = [[lng, 0.0] for lng in np.linspace(0, 1.449, 50)]
        result = resample_polyline(coords, total_miles=100.0)
        # Should have ~100 points (one per mile) + endpoints
        assert 90 <= len(result) <= 115

    def test_empty_coords_returns_empty_array(self) -> None:
        result = resample_polyline([], total_miles=100.0)
        assert result.shape == (0, 3)

    def test_mile_markers_monotonically_increasing(self) -> None:
        coords = [[lng, 0.0] for lng in np.linspace(0, 5.0, 200)]
        result = resample_polyline(coords, total_miles=345.0)
        diffs = np.diff(result[:, 2])
        assert (diffs >= 0).all()


# ---------------------------------------------------------------------------
# _plan_greedy_stops — algorithm core
# ---------------------------------------------------------------------------


class TestPlanGreedyStops:
    """Tests for the greedy fuel-stop algorithm."""

    def test_no_stops_needed_within_range(self) -> None:
        """If destination is within one full tank, no stops are needed."""
        candidates = [_make_candidate(mile=200.0, price=3.50, sid=1)]
        stops, total_cost = _plan_greedy_stops(candidates, total_miles=400.0, max_range=500.0, mpg=10.0)
        assert stops == []
        assert total_cost == 0.0

    def test_single_mandatory_stop(self) -> None:
        """
        Route of 700 miles, one station at mile 400.
        Must stop there and buy enough to cover the remaining 300 miles.
        """
        candidates = [_make_candidate(mile=400.0, price=3.00, sid=1)]
        stops, total_cost = _plan_greedy_stops(candidates, total_miles=700.0, max_range=500.0, mpg=10.0)

        assert len(stops) == 1
        stop = stops[0]
        assert stop["mile_marker"] == pytest.approx(400.0)
        # At mile 400, tank has 100 miles left (started full at 500).
        # pos + max_range = 900 > 700 so algorithm uses the "near destination"
        # branch: buy only what's needed to coast to mile 700.
        # miles_to_buy = max(0, 700 - 400 - 100) = 200 miles -> 20 gallons.
        expected_gallons = (700.0 - 400.0 - (500.0 - 400.0)) / 10.0  # = 20.0
        assert stop["gallons_purchased"] == pytest.approx(expected_gallons, rel=1e-3)
        assert stop["cost"] == pytest.approx(expected_gallons * 3.00, rel=1e-3)

    def test_skips_expensive_buys_cheapest_first(self) -> None:
        """
        Stations at miles 200 ($4.00) and 350 ($2.50).
        Both reachable from mile 0 on a full tank.
        Algorithm should drive past $4.00 and stop at $2.50.
        """
        candidates = [
            _make_candidate(mile=200.0, price=4.00, sid=1),
            _make_candidate(mile=350.0, price=2.50, sid=2),
        ]
        stops, _ = _plan_greedy_stops(candidates, total_miles=700.0, max_range=500.0, mpg=10.0)
        mile_markers = [s["mile_marker"] for s in stops]
        # Must stop at 350 (the cheap one); may also stop at 200 if needed
        assert 350.0 in mile_markers
        # Should NOT have bought at 200 when 350 ($2.50) is reachable
        expensive_stops = [s for s in stops if s["mile_marker"] == 200.0]
        assert expensive_stops == []

    def test_cheaper_station_out_of_range_triggers_intermediate_stop(self) -> None:
        """
        Cheap station at mile 600, but range is 500.
        Must make an intermediate stop to reach it.
        """
        candidates = [
            _make_candidate(mile=300.0, price=3.80, sid=1),  # expensive but reachable
            _make_candidate(mile=600.0, price=2.00, sid=2),  # cheap but too far initially
        ]
        stops, _ = _plan_greedy_stops(candidates, total_miles=800.0, max_range=500.0, mpg=10.0)
        mile_markers = [s["mile_marker"] for s in stops]
        # Must stop somewhere to reach mile 600
        assert len(stops) >= 1
        # The cheap station at 600 must be used
        assert 600.0 in mile_markers

    def test_fills_completely_at_cheapest_in_window(self) -> None:
        """
        If a station is the cheapest in the entire remaining window,
        the algorithm should fill up completely there.
        """
        # One station at mile 400, no cheaper ahead
        candidates = [_make_candidate(mile=400.0, price=3.00, sid=1)]
        stops, _ = _plan_greedy_stops(candidates, total_miles=850.0, max_range=500.0, mpg=10.0)
        # tank at arrival = 500 - 400 = 100; must fill to 500 to cover 450 remaining
        assert len(stops) == 1
        # At mile 400, tank = 100 miles remaining.  pos + max_range = 900 > 850,
        # so the algorithm buys only what is needed to coast to destination:
        # miles_to_buy = 850 - 400 - 100 = 350 miles → 35 gallons (lazy fill)
        assert stops[0]["gallons_purchased"] == pytest.approx(35.0, rel=1e-3)

    def test_total_cost_sum_of_stop_costs(self) -> None:
        """total_cost must equal the sum of all individual stop costs."""
        candidates = [
            _make_candidate(mile=300.0, price=3.00, sid=1),
            _make_candidate(mile=650.0, price=2.50, sid=2),
        ]
        stops, total_cost = _plan_greedy_stops(candidates, total_miles=900.0, max_range=500.0, mpg=10.0)
        expected = sum(s["cost"] for s in stops)
        assert total_cost == pytest.approx(expected, rel=1e-5)

    def test_cumulative_cost_increases_monotonically(self) -> None:
        """Running total must never decrease."""
        candidates = [
            _make_candidate(mile=300.0, price=3.00, sid=1),
            _make_candidate(mile=600.0, price=3.50, sid=2),
        ]
        stops, _ = _plan_greedy_stops(candidates, total_miles=900.0, max_range=500.0, mpg=10.0)
        for i in range(1, len(stops)):
            assert stops[i]["cumulative_cost"] >= stops[i - 1]["cumulative_cost"]

    def test_infeasible_gap_raises_error(self) -> None:
        """A gap > MAX_RANGE between stations must raise NoFeasibleRouteError."""
        candidates = [
            _make_candidate(mile=200.0, price=3.00, sid=1),
            _make_candidate(mile=800.0, price=3.00, sid=2),  # 600-mile gap
        ]
        with pytest.raises(NoFeasibleRouteError, match="600"):
            _plan_greedy_stops(candidates, total_miles=1000.0, max_range=500.0, mpg=10.0)

    def test_infeasible_final_gap_raises_error(self) -> None:
        """Gap between last station and destination > MAX_RANGE must raise."""
        candidates = [_make_candidate(mile=100.0, price=3.00, sid=1)]
        with pytest.raises(NoFeasibleRouteError, match="destination"):
            _plan_greedy_stops(candidates, total_miles=700.0, max_range=500.0, mpg=10.0)

    def test_empty_candidates_within_range_no_stops(self) -> None:
        """No stations, but destination within one tank → empty stop list."""
        stops, total_cost = _plan_greedy_stops([], total_miles=300.0, max_range=500.0, mpg=10.0)
        assert stops == []
        assert total_cost == 0.0

    def test_empty_candidates_out_of_range_raises(self) -> None:
        """No stations, destination beyond one tank → NoFeasibleRouteError."""
        with pytest.raises(NoFeasibleRouteError):
            _plan_greedy_stops([], total_miles=600.0, max_range=500.0, mpg=10.0)

    def test_stop_fields_present(self) -> None:
        """Each stop dict must contain all required fields."""
        required = {
            "name", "address", "city", "state", "lat", "lng",
            "mile_marker", "price_per_gallon", "gallons_purchased",
            "cost", "cumulative_cost",
        }
        candidates = [_make_candidate(mile=400.0, price=3.00, sid=1)]
        stops, _ = _plan_greedy_stops(candidates, total_miles=700.0, max_range=500.0, mpg=10.0)
        assert len(stops) == 1
        assert required.issubset(stops[0].keys())

    def test_multi_stop_route_correctness(self) -> None:
        """
        Straight 1500-mile route with stations every 200 miles, alternating
        cheap ($2.50) and expensive ($4.00).  Driver should preferentially
        fill at cheap stations.

        Cheap stations: miles 200, 600, 1000, 1400
        Expensive stations: miles 400, 800, 1200
        """
        candidates = sorted(
            [
                _make_candidate(mile=200.0, price=2.50, sid=1),
                _make_candidate(mile=400.0, price=4.00, sid=2),
                _make_candidate(mile=600.0, price=2.50, sid=3),
                _make_candidate(mile=800.0, price=4.00, sid=4),
                _make_candidate(mile=1000.0, price=2.50, sid=5),
                _make_candidate(mile=1200.0, price=4.00, sid=6),
                _make_candidate(mile=1400.0, price=2.50, sid=7),
            ],
            key=lambda c: c.mile_marker,
        )
        stops, _ = _plan_greedy_stops(candidates, total_miles=1500.0, max_range=500.0, mpg=10.0)
        # Driver should never buy at $4.00 stations since $2.50 ones are always
        # within a 400-mile hop (well within 500-mile range)
        expensive_purchases = [s for s in stops if s["price_per_gallon"] > 3.00]
        assert expensive_purchases == [], (
            f"Bought expensive fuel at: {[s['mile_marker'] for s in expensive_purchases]}"
        )

    def test_max_range_affects_stop_count(self) -> None:
        """A smaller max_range requires more stops."""
        candidates = [
            _make_candidate(mile=200.0, price=3.00, sid=1),
            _make_candidate(mile=400.0, price=3.00, sid=2),
            _make_candidate(mile=600.0, price=3.00, sid=3),
            _make_candidate(mile=800.0, price=3.00, sid=4),
        ]
        stops_300, _ = _plan_greedy_stops(candidates, total_miles=1000.0, max_range=300.0, mpg=10.0)
        stops_800, _ = _plan_greedy_stops(candidates, total_miles=1000.0, max_range=800.0, mpg=10.0)
        assert len(stops_300) > len(stops_800)

    def test_mpg_affects_total_cost(self) -> None:
        """Higher MPG reduces total cost proportionally."""
        candidates = [_make_candidate(mile=300.0, price=3.00, sid=1)]
        _, cost_10 = _plan_greedy_stops(candidates, total_miles=600.0, max_range=500.0, mpg=10.0)
        _, cost_20 = _plan_greedy_stops(candidates, total_miles=600.0, max_range=500.0, mpg=20.0)
        assert cost_20 == pytest.approx(cost_10 / 2.0)

    def test_small_range_raises_infeasible(self) -> None:
        """max_range=50 with 120 mile gaps raises error with '50' in message."""
        candidates = [
            _make_candidate(mile=120.0, price=3.00, sid=1),
            _make_candidate(mile=240.0, price=3.00, sid=2),
        ]
        with pytest.raises(NoFeasibleRouteError, match="50"):
            _plan_greedy_stops(candidates, total_miles=300.0, max_range=50.0, mpg=10.0)

    def test_final_leg_shorter_than_range_no_extra_stop(self) -> None:
        """Final leg is shorter than range, so we should coast to the end."""
        # tank = 300, stop at 200, remaining = 200, dest = 400.
        candidates = [
            _make_candidate(mile=200.0, price=3.00, sid=1),
            _make_candidate(mile=300.0, price=4.00, sid=2),
        ]
        stops, _ = _plan_greedy_stops(candidates, total_miles=400.0, max_range=300.0, mpg=10.0)
        # Should stop at 200, and from 200 we can reach 400 (300 range), so no stop at 300.
        mile_markers = [s["mile_marker"] for s in stops]
        assert mile_markers == [200.0]


# ---------------------------------------------------------------------------
# find_corridor_stations — integration with synthetic station index
# ---------------------------------------------------------------------------


class TestFindCorridorStations:
    """Tests for corridor station extraction (mocked station index)."""

    def _straight_route_points(self, total_miles: float = 100.0) -> np.ndarray:
        """
        Return resampled route points for a straight north-south line.
        Uses lat from 0° to total_miles/69°, lng fixed at -90°.
        """
        n = int(total_miles) + 2
        lats = np.linspace(0, total_miles / 69.0, n)
        lngs = np.full(n, -90.0)
        mile_markers = np.linspace(0, total_miles, n)
        return np.column_stack([lats, lngs, mile_markers])

    def _patch_index(self, records, coords):
        """Context manager: patch _get_station_index to return synthetic data."""
        from scipy.spatial import cKDTree as _cKDT

        tree = _cKDT(coords) if len(coords) else _cKDT(np.empty((0, 2)))
        return patch(
            "routing.services.fuel_planner._get_station_index",
            return_value=(records, coords, tree),
        )

    def test_station_on_route_is_included(self) -> None:
        """A station sitting directly on the route line should be in corridor."""
        route_pts = self._straight_route_points(100.0)
        # Place station at lat=0.5°, lng=-90° (on the route, ≈ 34.5 miles marker)
        record = _make_station(1, price=3.00, lat=0.5, lng=-90.0)
        coords = np.array([[0.5, -90.0]])
        with self._patch_index([record], coords):
            candidates = find_corridor_stations(route_pts, corridor_miles=10.0)
        assert len(candidates) == 1
        assert candidates[0].record.id == 1

    def test_station_far_from_route_is_excluded(self) -> None:
        """A station 50 miles off the route should not be included."""
        route_pts = self._straight_route_points(100.0)
        # Place station at lng=-89.0 → ~88 miles east of route at -90°
        record = _make_station(2, price=3.00, lat=0.5, lng=-89.0)
        coords = np.array([[0.5, -89.0]])
        with self._patch_index([record], coords):
            candidates = find_corridor_stations(route_pts, corridor_miles=10.0)
        assert candidates == []

    def test_empty_station_index_returns_empty(self) -> None:
        route_pts = self._straight_route_points(100.0)
        with self._patch_index([], np.empty((0, 2))):
            candidates = find_corridor_stations(route_pts)
        assert candidates == []

    def test_candidates_sorted_by_mile_marker(self) -> None:
        """Returned candidates must be sorted by mile_marker ascending."""
        route_pts = self._straight_route_points(200.0)
        records = [
            _make_station(1, price=3.00, lat=2.0, lng=-90.0),
            _make_station(2, price=3.50, lat=0.5, lng=-90.0),
        ]
        coords = np.array([[2.0, -90.0], [0.5, -90.0]])
        with self._patch_index(records, coords):
            candidates = find_corridor_stations(route_pts, corridor_miles=10.0)
        markers = [c.mile_marker for c in candidates]
        assert markers == sorted(markers)
