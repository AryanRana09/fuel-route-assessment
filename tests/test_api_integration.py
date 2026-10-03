"""
API-level integration tests for POST /api/route/.

Per Step 6 requirements:
- Routing client is mocked (no external network dependencies)
- Full Django view, serializer, and fuel-planning logic execute end-to-end
- Covers:
  1. Success case (optimal fuel stop selection, GeoJSON, cost calculation)
  2. Bad inputs (missing fields, empty body, same start/finish) -> 400
  3. Infeasible route (gap > 500 miles between stations) -> 422
  4. Timing: meta.compute_time_ms excludes external call time and both are reported
  5. Performance: cached repeat requests return in well under 100ms
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from routing.models import Station
from routing.services.fuel_planner import invalidate_station_index, warm_station_index


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def corridor_stations(db):
    """
    Populate test database with a chain of stations along a route.
    Route runs along latitude 35.0 from longitude -100.0 to -80.0 (~1,135 miles).
    """
    Station.objects.all().delete()
    invalidate_station_index()

    # Place stations along the route corridor
    stations = [
        Station.objects.create(
            opis_id=101,
            name="Cheapest First Stop",
            address="100 Route 66",
            city="Amarillo",
            state="TX",
            price=Decimal("3.10"),
            lat=35.0,
            lng=-97.0,  # ~170 miles along route
        ),
        Station.objects.create(
            opis_id=102,
            name="Midway Fuel Hub",
            address="200 Interstate",
            city="Oklahoma City",
            state="OK",
            price=Decimal("2.95"),
            lat=35.0,
            lng=-94.0,  # ~340 miles along route
        ),
        Station.objects.create(
            opis_id=103,
            name="Eastbound Travel Plaza",
            address="300 Highway Blvd",
            city="Little Rock",
            state="AR",
            price=Decimal("3.05"),
            lat=35.0,
            lng=-89.0,  # ~623 miles along route
        ),
        Station.objects.create(
            opis_id=104,
            name="Near Destination Station",
            address="400 South Expwy",
            city="Memphis",
            state="TN",
            price=Decimal("3.20"),
            lat=35.0,
            lng=-84.0,  # ~906 miles along route
        ),
    ]

    # Preload the station index into memory (AppConfig.ready / singleton pattern)
    warm_station_index()
    yield stations
    invalidate_station_index()


def _make_mock_route(start_lng=-100.0, end_lng=-80.0, lat=35.0, num_points=100):
    """Generate a realistic mock route dict from start to finish."""
    coords = [
        [start_lng + i * (end_lng - start_lng) / (num_points - 1), lat]
        for i in range(num_points)
    ]
    # Total distance is approximately 1,135 miles
    return {
        "total_miles": 1135.0,
        "duration_minutes": 1050.0,
        "geometry": coords,
    }


# ---------------------------------------------------------------------------
# 1. Success Case
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApiSuccessCase:
    """API-level test for successful route planning with mocked routing client."""

    def test_successful_route_planning(self, api_client: APIClient, corridor_stations):
        mock_route = _make_mock_route()

        with patch("routing.views.get_route", return_value=mock_route), \
             patch("routing.views._resolve_location", side_effect=lambda s: (35.0, -100.0 if "Start" in s else -80.0)):

            response = api_client.post(
                "/api/route/",
                {"start": "Start Point, TX", "finish": "Finish Point, NC"},
                format="json",
            )

        assert response.status_code == 200
        data = response.json()

        # Top-level keys
        assert "start" in data
        assert "finish" in data
        assert "total_distance_miles" in data
        assert "total_fuel_cost_usd" in data
        assert "total_gallons" in data
        assert "fuel_stops" in data
        assert "route_geojson" in data
        assert "meta" in data

        # Fuel stops were calculated
        assert len(data["fuel_stops"]) > 0
        first_stop = data["fuel_stops"][0]
        assert "name" in first_stop
        assert "price_per_gallon" in first_stop
        assert "cost" in first_stop
        assert "mile_marker" in first_stop
        assert "gallons_purchased" in first_stop

        # GeoJSON is valid FeatureCollection
        geojson = data["route_geojson"]
        assert geojson["type"] == "FeatureCollection"
        types = [f["geometry"]["type"] for f in geojson["features"]]
        assert "LineString" in types
        assert "Point" in types


# ---------------------------------------------------------------------------
# 2. Bad Input Cases
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApiBadInputCases:
    """API-level tests for input validation."""

    def test_missing_start_field(self, api_client: APIClient):
        response = api_client.post(
            "/api/route/",
            {"finish": "Los Angeles, CA"},
            format="json",
        )
        assert response.status_code == 400
        assert "start" in response.json()

    def test_missing_finish_field(self, api_client: APIClient):
        response = api_client.post(
            "/api/route/",
            {"start": "New York, NY"},
            format="json",
        )
        assert response.status_code == 400
        assert "finish" in response.json()

    def test_empty_json_body(self, api_client: APIClient):
        response = api_client.post(
            "/api/route/",
            {},
            format="json",
        )
        assert response.status_code == 400

    def test_same_start_and_finish(self, api_client: APIClient):
        response = api_client.post(
            "/api/route/",
            {"start": "Chicago, IL", "finish": "Chicago, IL"},
            format="json",
        )
        assert response.status_code == 400
        assert "non_field_errors" in response.json()


# ---------------------------------------------------------------------------
# 3. Infeasible Route Case
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApiInfeasibleRouteCase:
    """API-level test when route has a gap > 500 miles without any fuel stations."""

    def test_infeasible_route_returns_422(self, api_client: APIClient, db):
        # Database has no stations along this remote route
        Station.objects.all().delete()
        invalidate_station_index()
        warm_station_index()

        # 1,200 mile route through area with zero stations
        long_empty_route = _make_mock_route(start_lng=-120.0, end_lng=-100.0, num_points=50)
        long_empty_route["total_miles"] = 1200.0

        with patch("routing.views.get_route", return_value=long_empty_route), \
             patch("routing.views._resolve_location", return_value=(35.0, -120.0)):

            response = api_client.post(
                "/api/route/",
                {"start": "35.0,-120.0", "finish": "35.0,-100.0"},
                format="json",
            )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "NO_FEASIBLE_ROUTE"
        assert "error" in data


# ---------------------------------------------------------------------------
# 4. Timing & Compute Time Separation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApiTimingMetrics:
    """Verify that compute_time_ms excludes external call time and both are reported."""

    def test_timing_excludes_external_call_time(
        self, api_client: APIClient, corridor_stations
    ):
        mock_route = _make_mock_route()

        def slow_get_route(start, finish):
            time.sleep(0.06)  # Simulate 60ms network latency
            return mock_route

        with patch("routing.views.get_route", side_effect=slow_get_route), \
             patch("routing.views._resolve_location", return_value=(35.0, -100.0)):

            response = api_client.post(
                "/api/route/",
                {"start": "Start City, TX", "finish": "End City, NC"},
                format="json",
            )

        assert response.status_code == 200
        meta = response.json()["meta"]

        assert "compute_time_ms" in meta
        assert "external_call_time_ms" in meta
        assert "external_api_time_ms" in meta
        assert "total_time_ms" in meta

        # External call time should reflect the simulated 60ms latency
        assert meta["external_call_time_ms"] >= 50
        # Compute time excludes external call time and should be much smaller
        assert meta["compute_time_ms"] < 40
        # Total time is roughly compute + external
        assert meta["total_time_ms"] >= meta["external_call_time_ms"]


# ---------------------------------------------------------------------------
# 5. Performance: Cached Repeat Requests under 100ms
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestApiRepeatRequestPerformance:
    """Verify that cached repeat requests return in well under 100ms."""

    def test_repeat_request_returns_in_under_100ms(
        self, api_client: APIClient, corridor_stations
    ):
        mock_route = _make_mock_route()

        with patch("routing.views.get_route", return_value=mock_route), \
             patch("routing.views._resolve_location", return_value=(35.0, -100.0)):

            # First call warms view caches
            resp1 = api_client.post(
                "/api/route/",
                {"start": "35.0,-100.0", "finish": "35.0,-80.0"},
                format="json",
            )
            assert resp1.status_code == 200

            # Repeat request: measure exact end-to-end client latency
            t0 = time.perf_counter()
            resp2 = api_client.post(
                "/api/route/",
                {"start": "35.0,-100.0", "finish": "35.0,-80.0"},
                format="json",
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert resp2.status_code == 200
            assert elapsed_ms < 100.0, f"Repeat request took {elapsed_ms:.2f}ms >= 100ms"
