"""
Unit tests for routing.services.routing_client.

All HTTP calls are mocked — no network access is needed.

Tested behaviours
-----------------
* Coordinate string detection and parsing
* Free-text geocoding (success + error)
* OSRM route fetch (success + non-OK code + bad structure)
* End-to-end get_route with coord inputs (0 geocode calls)
* End-to-end get_route with free-text inputs (2 geocode calls)
* LRU cache: second identical call makes zero HTTP requests
"""

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from routing.services.routing_client import (
    GeocodingError,
    RoutingError,
    _COORD_RE,
    _fetch_osrm_route,
    _geocode_nominatim,
    _get_route_cached,
    _resolve_location,
    get_route,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _nominatim_response(lat: float = 41.8781, lon: float = -87.6298) -> MagicMock:
    """Return a mock Nominatim JSON response (list with one result)."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [{"lat": str(lat), "lon": str(lon)}]
    return mock_resp


def _osrm_response(
    distance_m: float = 3_435_000.0,
    duration_s: float = 132_630.0,
    coords: list | None = None,
) -> MagicMock:
    """Return a mock OSRM JSON response."""
    if coords is None:
        coords = [[-87.6298, 41.8781], [-90.0, 41.0], [-122.4194, 37.7749]]
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "code": "Ok",
        "routes": [
            {
                "legs": [{"distance": distance_m, "duration": duration_s}],
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords,
                },
            }
        ],
    }
    return mock_resp


# ---------------------------------------------------------------------------
# Coordinate regex
# ---------------------------------------------------------------------------


class TestCoordRegex:
    """Tests for the coordinate detection pattern."""

    def test_simple_coords_match(self) -> None:
        assert _COORD_RE.match("41.8781,-87.6298") is not None

    def test_coords_with_space_match(self) -> None:
        assert _COORD_RE.match("41.8781, -87.6298") is not None

    def test_negative_lat_matches(self) -> None:
        assert _COORD_RE.match("-33.87,151.21") is not None

    def test_integer_coords_match(self) -> None:
        assert _COORD_RE.match("41,-87") is not None

    def test_place_name_does_not_match(self) -> None:
        assert _COORD_RE.match("Chicago, IL") is None

    def test_single_number_does_not_match(self) -> None:
        assert _COORD_RE.match("41.8781") is None

    def test_text_with_numbers_does_not_match(self) -> None:
        assert _COORD_RE.match("Route 66, TX") is None


# ---------------------------------------------------------------------------
# _resolve_location
# ---------------------------------------------------------------------------


class TestResolveLocation:
    """Tests for the coordinate-vs-place-name resolver."""

    def test_coord_string_parses_without_network(self) -> None:
        """Coordinate strings must not trigger any HTTP call."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            lat, lng = _resolve_location.cache_clear() or _resolve_location("34.0522,-118.2437")
            mock_sess.get.assert_not_called()
        assert lat == pytest.approx(34.0522)
        assert lng == pytest.approx(-118.2437)

    def test_place_name_calls_nominatim(self) -> None:
        """Free-text inputs must trigger a Nominatim call."""
        _resolve_location.cache_clear()
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _nominatim_response(41.8781, -87.6298)
            lat, lng = _resolve_location("Chicago, IL")

        mock_sess.get.assert_called_once()
        assert lat == pytest.approx(41.8781)
        assert lng == pytest.approx(-87.6298)


# ---------------------------------------------------------------------------
# _geocode_nominatim
# ---------------------------------------------------------------------------


class TestGeocodeNominatim:
    """Tests for the Nominatim geocoding helper."""

    def test_successful_geocode(self) -> None:
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _nominatim_response(29.7604, -95.3698)
            lat, lng = _geocode_nominatim("Houston, TX")

        assert lat == pytest.approx(29.7604)
        assert lng == pytest.approx(-95.3698)

    def test_empty_result_raises_geocoding_error(self) -> None:
        empty = MagicMock()
        empty.raise_for_status.return_value = None
        empty.json.return_value = []

        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = empty
            with pytest.raises(GeocodingError, match="Could not geocode"):
                _geocode_nominatim("Xyzzy Nonexistent Place, ZZ")

    def test_http_error_raises_geocoding_error(self) -> None:
        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("503")

        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = err_resp
            with pytest.raises(GeocodingError, match="Nominatim HTTP error"):
                _geocode_nominatim("Chicago")

    def test_nominatim_restricted_to_us(self) -> None:
        """Verify countrycodes=us is passed in the request."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _nominatim_response()
            _geocode_nominatim("Springfield")

        _, kwargs = mock_sess.get.call_args
        assert kwargs["params"]["countrycodes"] == "us"


# ---------------------------------------------------------------------------
# _fetch_osrm_route
# ---------------------------------------------------------------------------


class TestFetchOsrmRoute:
    """Tests for the OSRM route fetch helper."""

    def test_successful_route_returns_correct_keys(self) -> None:
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response(
                distance_m=3_435_000, duration_s=132_630
            )
            result = _fetch_osrm_route(41.8781, -87.6298, 37.7749, -122.4194)

        assert set(result.keys()) == {"total_miles", "duration_minutes", "geometry"}

    def test_distance_converted_to_miles(self) -> None:
        # 1609.344 m = exactly 1 mile
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response(distance_m=16093.44)
            result = _fetch_osrm_route(41.0, -87.0, 42.0, -88.0)

        assert result["total_miles"] == pytest.approx(10.0, rel=1e-3)

    def test_duration_converted_to_minutes(self) -> None:
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response(duration_s=3600)
            result = _fetch_osrm_route(41.0, -87.0, 42.0, -88.0)

        assert result["duration_minutes"] == pytest.approx(60.0)

    def test_geometry_passed_through(self) -> None:
        coords = [[-87.0, 41.0], [-88.0, 42.0]]
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response(coords=coords)
            result = _fetch_osrm_route(41.0, -87.0, 42.0, -88.0)

        assert result["geometry"] == coords

    def test_osrm_non_ok_raises_routing_error(self) -> None:
        bad_resp = MagicMock()
        bad_resp.raise_for_status.return_value = None
        bad_resp.json.return_value = {"code": "NoRoute", "message": "No route found"}

        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = bad_resp
            with pytest.raises(RoutingError, match="NoRoute"):
                _fetch_osrm_route(41.0, -87.0, 42.0, -88.0)

    def test_osrm_http_error_raises_routing_error(self) -> None:
        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("429")

        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = err_resp
            with pytest.raises(RoutingError, match="OSRM HTTP error"):
                _fetch_osrm_route(41.0, -87.0, 42.0, -88.0)

    def test_malformed_response_raises_routing_error(self) -> None:
        bad_resp = MagicMock()
        bad_resp.raise_for_status.return_value = None
        bad_resp.json.return_value = {"code": "Ok", "routes": []}  # empty routes list

        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = bad_resp
            with pytest.raises(RoutingError, match="Unexpected OSRM response"):
                _fetch_osrm_route(41.0, -87.0, 42.0, -88.0)

    def test_osrm_called_with_lng_lat_order(self) -> None:
        """OSRM expects coordinates as lng,lat, not lat,lng."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response()
            _fetch_osrm_route(41.8781, -87.6298, 37.7749, -122.4194)

        url_called = mock_sess.get.call_args[0][0]
        # The URL path should contain lng,lat (i.e. -87.6298,41.8781)
        assert "-87.6298,41.8781" in url_called
        assert "-122.4194,37.7749" in url_called


# ---------------------------------------------------------------------------
# get_route end-to-end (LRU cache isolation)
# ---------------------------------------------------------------------------


class TestGetRoute:
    """End-to-end tests for the public get_route() function."""

    def setup_method(self) -> None:
        """Clear LRU caches before each test for isolation."""
        _get_route_cached.cache_clear()
        _resolve_location.cache_clear()

    def test_coord_inputs_skip_geocoding(self) -> None:
        """With coordinate inputs, only OSRM should be called."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response()
            get_route("41.8781,-87.6298", "37.7749,-122.4194")

        # Exactly one call total (OSRM); no Nominatim call
        assert mock_sess.get.call_count == 1
        url_called = mock_sess.get.call_args[0][0]
        assert "route/v1/driving" in url_called

    def test_free_text_inputs_call_nominatim_twice(self) -> None:
        """With two place-name inputs, two Nominatim calls + one OSRM call."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            # First two calls return Nominatim results; third returns OSRM route
            mock_sess.get.side_effect = [
                _nominatim_response(41.8781, -87.6298),  # Chicago
                _nominatim_response(37.7749, -122.4194),  # SF
                _osrm_response(),
            ]
            result = get_route("Chicago, IL", "San Francisco, CA")

        assert mock_sess.get.call_count == 3
        assert "total_miles" in result

    def test_second_identical_call_is_cached(self) -> None:
        """The second call with same args must make zero HTTP requests."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response()
            get_route("41.8781,-87.6298", "37.7749,-122.4194")
            get_route("41.8781,-87.6298", "37.7749,-122.4194")

        # Still only one actual HTTP call despite two get_route() invocations
        assert mock_sess.get.call_count == 1

    def test_return_structure(self) -> None:
        """Result must contain exactly the required keys with correct types."""
        with patch("routing.services.routing_client._SESSION") as mock_sess:
            mock_sess.get.return_value = _osrm_response(
                distance_m=500_000, duration_s=18_000, coords=[[-87.0, 41.0], [-88.0, 42.0]]
            )
            result = get_route("41.0,-87.0", "42.0,-88.0")

        assert isinstance(result["total_miles"], float)
        assert isinstance(result["duration_minutes"], float)
        assert isinstance(result["geometry"], list)
        assert result["total_miles"] > 0
        assert result["duration_minutes"] > 0
