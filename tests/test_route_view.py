"""
Unit tests for POST /api/route/ and supporting helpers.

All external calls (routing_client, fuel_planner) are mocked so tests run
offline and without the station database loaded.

Test coverage:
- Input validation (missing field, same start/finish, coord vs text detection)
- Happy path: coord inputs, text inputs
- Exception-to-status mapping (GeocodingError, RoutingError, Timeout, Connection, NoFeasibleRoute)
- Response structure: all required keys, GeoJSON shape, rounding
- _count_api_calls helper
- GeoJSON builder
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from rest_framework.test import APIClient

from routing.services.fuel_planner import NoFeasibleRouteError
from routing.services.response_builder import build_geojson, build_route_response
from routing.services.routing_client import GeocodingError, RoutingError
from routing.views import _count_api_calls


# ---------------------------------------------------------------------------
# Fixtures / shared helpers
# ---------------------------------------------------------------------------

FAKE_ROUTE = {
    "total_miles": 2789.45,
    "duration_minutes": 2210.5,
    "geometry": [[-74.006, 40.7128], [-87.6298, 41.8781], [-118.2437, 34.0522]],
}

FAKE_PLAN = {
    "stops": [
        {
            "name": "Pilot #42",
            "address": "I-40 EXIT 7",
            "city": "Amarillo",
            "state": "TX",
            "lat": 35.1813,
            "lng": -101.8313,
            "mile_marker": 503.2,
            "price_per_gallon": 3.299,
            "gallons_purchased": 18.5,
            "cost": 61.0315,
            "cumulative_cost": 61.0315,
        }
    ],
    "total_cost": 61.0315,
    "total_miles": 2789.45,
}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def _mock_services(route=None, plan=None):
    """Context manager that patches both service calls."""
    route = route or FAKE_ROUTE
    plan = plan or FAKE_PLAN

    return (
        patch("routing.views.get_route", return_value=route),
        patch("routing.views._resolve_location", side_effect=lambda s: (40.0, -74.0)),
        patch("routing.views.plan_fuel_stops", return_value=plan),
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRouteInputValidation:
    """DRF serializer validation tests."""

    def test_missing_start_returns_400(self, client: APIClient) -> None:
        resp = client.post("/api/route/", {"finish": "LA"}, format="json")
        assert resp.status_code == 400
        assert "start" in resp.json()

    def test_missing_finish_returns_400(self, client: APIClient) -> None:
        resp = client.post("/api/route/", {"start": "NY"}, format="json")
        assert resp.status_code == 400
        assert "finish" in resp.json()

    def test_empty_body_returns_400(self, client: APIClient) -> None:
        resp = client.post("/api/route/", {}, format="json")
        assert resp.status_code == 400

    def test_same_start_finish_returns_400(self, client: APIClient) -> None:
        resp = client.post(
            "/api/route/",
            {"start": "Chicago, IL", "finish": "Chicago, IL"},
            format="json",
        )
        assert resp.status_code == 400

    def test_too_short_value_returns_400(self, client: APIClient) -> None:
        resp = client.post(
            "/api/route/", {"start": "A", "finish": "Los Angeles"}, format="json"
        )
        assert resp.status_code == 400

    def test_get_not_allowed(self, client: APIClient) -> None:
        resp = client.get("/api/route/")
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRouteHappyPath:
    """Successful response structure and content tests."""

    def _post(self, client, start="41.8781,-87.6298", finish="34.0522,-118.2437"):
        p1, p2, p3 = _mock_services()
        with p1, p2, p3:
            return client.post(
                "/api/route/",
                {"start": start, "finish": finish},
                format="json",
            )

    def test_returns_200(self, client: APIClient) -> None:
        assert self._post(client).status_code == 200

    def test_top_level_keys_present(self, client: APIClient) -> None:
        data = self._post(client).json()
        required = {
            "start", "finish", "total_distance_miles", "total_fuel_cost_usd",
            "total_gallons", "fuel_stops", "route_geojson", "meta",
        }
        assert required.issubset(data.keys())

    def test_start_finish_have_name_lat_lng(self, client: APIClient) -> None:
        data = self._post(client).json()
        for key in ("start", "finish"):
            assert {"name", "lat", "lng"} <= data[key].keys()

    def test_total_fuel_cost_rounded_to_2dp(self, client: APIClient) -> None:
        data = self._post(client).json()
        cost = data["total_fuel_cost_usd"]
        assert cost == round(cost, 2)

    def test_stop_cost_rounded_to_2dp(self, client: APIClient) -> None:
        data = self._post(client).json()
        for stop in data["fuel_stops"]:
            assert stop["cost"] == round(stop["cost"], 2)
            assert stop["cumulative_cost"] == round(stop["cumulative_cost"], 2)

    def test_route_geojson_is_feature_collection(self, client: APIClient) -> None:
        data = self._post(client).json()
        gj = data["route_geojson"]
        assert gj["type"] == "FeatureCollection"
        assert isinstance(gj["features"], list)

    def test_route_geojson_has_linestring_feature(self, client: APIClient) -> None:
        data = self._post(client).json()
        features = data["route_geojson"]["features"]
        line_features = [f for f in features if f["geometry"]["type"] == "LineString"]
        assert len(line_features) == 1

    def test_route_geojson_has_point_per_stop(self, client: APIClient) -> None:
        data = self._post(client).json()
        features = data["route_geojson"]["features"]
        point_features = [f for f in features if f["geometry"]["type"] == "Point"]
        assert len(point_features) == len(data["fuel_stops"])

    def test_point_feature_properties(self, client: APIClient) -> None:
        data = self._post(client).json()
        features = data["route_geojson"]["features"]
        point = next(f for f in features if f["geometry"]["type"] == "Point")
        required_props = {"name", "price_per_gallon", "mile_marker", "cost"}
        assert required_props <= point["properties"].keys()

    def test_meta_has_api_calls_and_compute_time(self, client: APIClient) -> None:
        data = self._post(client).json()
        meta = data["meta"]
        assert "external_api_calls" in meta
        assert "compute_time_ms" in meta
        assert isinstance(meta["compute_time_ms"], int)

    def test_coord_inputs_report_1_api_call(self, client: APIClient) -> None:
        data = self._post(client, "41.8781,-87.6298", "34.0522,-118.2437").json()
        assert data["meta"]["external_api_calls"] == 1

    def test_text_inputs_report_3_api_calls(self, client: APIClient) -> None:
        p1, p2, p3 = _mock_services()
        with p1, p2, p3:
            resp = client.post(
                "/api/route/",
                {"start": "New York, NY", "finish": "Los Angeles, CA"},
                format="json",
            )
        assert resp.json()["meta"]["external_api_calls"] == 3

    def test_mixed_inputs_report_2_api_calls(self, client: APIClient) -> None:
        p1, p2, p3 = _mock_services()
        with p1, p2, p3:
            resp = client.post(
                "/api/route/",
                {"start": "40.7128,-74.0060", "finish": "Los Angeles, CA"},
                format="json",
            )
        assert resp.json()["meta"]["external_api_calls"] == 2


# ---------------------------------------------------------------------------
# Exception → HTTP status mapping
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRouteExceptionMapping:
    """Each service exception must map to the correct HTTP status."""

    def _post_with_route_error(self, client, exc):
        with patch("routing.views.get_route", side_effect=exc):
            return client.post(
                "/api/route/",
                {"start": "New York, NY", "finish": "Los Angeles, CA"},
                format="json",
            )

    def _post_with_plan_error(self, client, exc):
        with patch("routing.views.get_route", return_value=FAKE_ROUTE), \
             patch("routing.views._resolve_location", return_value=(40.0, -74.0)), \
             patch("routing.views.plan_fuel_stops", side_effect=exc):
            return client.post(
                "/api/route/",
                {"start": "New York, NY", "finish": "Los Angeles, CA"},
                format="json",
            )

    def test_geocoding_error_returns_400(self, client: APIClient) -> None:
        resp = self._post_with_route_error(
            client, GeocodingError("Cannot geocode 'Nowhere'")
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "GEOCODING_FAILED"

    def test_routing_error_returns_502(self, client: APIClient) -> None:
        resp = self._post_with_route_error(client, RoutingError("NoRoute"))
        assert resp.status_code == 502
        assert resp.json()["code"] == "ROUTING_FAILED"

    def test_timeout_returns_502(self, client: APIClient) -> None:
        resp = self._post_with_route_error(client, requests.exceptions.Timeout())
        assert resp.status_code == 502
        assert resp.json()["code"] == "UPSTREAM_TIMEOUT"

    def test_connection_error_returns_502(self, client: APIClient) -> None:
        resp = self._post_with_route_error(
            client, requests.exceptions.ConnectionError("refused")
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"

    def test_no_feasible_route_returns_422(self, client: APIClient) -> None:
        resp = self._post_with_plan_error(
            client, NoFeasibleRouteError("Gap of 600 miles")
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "NO_FEASIBLE_ROUTE"

    def test_error_response_has_error_field(self, client: APIClient) -> None:
        resp = self._post_with_route_error(
            client, GeocodingError("Cannot geocode 'Nowhere'")
        )
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# _count_api_calls helper
# ---------------------------------------------------------------------------


class TestCountApiCalls:
    """Tests for the API-call counter in views.py."""

    def test_both_coords_returns_1(self) -> None:
        assert _count_api_calls("41.0,-87.0", "34.0,-118.0") == 1

    def test_both_text_returns_3(self) -> None:
        assert _count_api_calls("New York, NY", "Los Angeles, CA") == 3

    def test_mixed_returns_2(self) -> None:
        assert _count_api_calls("41.0,-87.0", "Los Angeles, CA") == 2

    def test_reversed_mixed_returns_2(self) -> None:
        assert _count_api_calls("New York, NY", "34.0,-118.0") == 2


# ---------------------------------------------------------------------------
# build_geojson (response_builder)
# ---------------------------------------------------------------------------


class TestBuildGeoJson:
    """Unit tests for the GeoJSON assembly helper."""

    def _stop(self, **overrides) -> dict:
        base = {
            "name": "Test Station",
            "address": "123 Rd",
            "city": "Dallas",
            "state": "TX",
            "lat": 32.7767,
            "lng": -96.797,
            "mile_marker": 300.0,
            "price_per_gallon": 3.29,
            "gallons_purchased": 20.0,
            "cost": 65.80,
            "cumulative_cost": 65.80,
        }
        base.update(overrides)
        return base

    def test_returns_feature_collection(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [])
        assert gj["type"] == "FeatureCollection"

    def test_linestring_feature_first(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [])
        first = gj["features"][0]
        assert first["geometry"]["type"] == "LineString"

    def test_linestring_coordinates_match_route(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [])
        assert gj["features"][0]["geometry"]["coordinates"] == FAKE_ROUTE["geometry"]

    def test_point_feature_per_stop(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [self._stop(), self._stop()])
        points = [f for f in gj["features"] if f["geometry"]["type"] == "Point"]
        assert len(points) == 2

    def test_point_coordinates_lng_lat_order(self) -> None:
        stop = self._stop(lat=32.7767, lng=-96.797)
        gj = build_geojson(FAKE_ROUTE, [stop])
        point = next(f for f in gj["features"] if f["geometry"]["type"] == "Point")
        coords = point["geometry"]["coordinates"]
        assert coords == [-96.797, 32.7767]  # [lng, lat] GeoJSON order

    def test_point_properties_contain_required_fields(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [self._stop()])
        point = next(f for f in gj["features"] if f["geometry"]["type"] == "Point")
        required = {"name", "price_per_gallon", "mile_marker", "cost"}
        assert required <= point["properties"].keys()

    def test_point_cost_rounded_to_2dp(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [self._stop(cost=61.0315)])
        point = next(f for f in gj["features"] if f["geometry"]["type"] == "Point")
        assert point["properties"]["cost"] == 61.03

    def test_no_stops_yields_only_linestring(self) -> None:
        gj = build_geojson(FAKE_ROUTE, [])
        assert len(gj["features"]) == 1
