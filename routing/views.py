"""
Views for the routing application.

All views return JSON; no template rendering is used.
"""

from __future__ import annotations

import time

import requests
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from routing.serializers import RouteRequestSerializer
from routing.services.fuel_planner import NoFeasibleRouteError, plan_fuel_stops
from routing.services.response_builder import build_route_response
from routing.services.routing_client import (
    GeocodingError,
    RoutingError,
    _COORD_RE,
    _resolve_location,
    get_route,
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class HealthView(APIView):
    """
    GET /api/health/

    Liveness check endpoint.  Returns HTTP 200 with ``{"status": "ok"}``
    when the server is running and Django is correctly configured.
    """

    def get(self, request: Request) -> Response:  # noqa: ARG002
        """Return a simple health-check payload."""
        return Response({"status": "ok"})


# ---------------------------------------------------------------------------
# Route planner
# ---------------------------------------------------------------------------


class RouteView(APIView):
    """
    POST /api/route/

    Compute the minimum-cost driving route between two US locations, with
    optimal fuel stops selected from the OPIS station database.

    Request body (JSON)
    -------------------
    .. code-block:: json

        {
            "start":  "New York, NY",
            "finish": "Los Angeles, CA"
        }

    Both fields accept either a **free-text US place name** or a
    **"lat,lng" coordinate string** (e.g. ``"40.7128,-74.0060"``).

    Response body (JSON)
    --------------------
    .. code-block:: json

        {
            "start":  {"name": "...", "lat": ..., "lng": ...},
            "finish": {"name": "...", "lat": ..., "lng": ...},
            "total_distance_miles": 2789.45,
            "total_fuel_cost_usd":  825.50,
            "total_gallons":        278.945,
            "fuel_stops": [
                {
                    "name":              "Pilot Travel Center #42",
                    "address":           "I-40 EXIT 7",
                    "city":              "Amarillo",
                    "state":             "TX",
                    "lat":               35.1813,
                    "lng":              -101.8313,
                    "mile_marker":       503.2,
                    "price_per_gallon":  3.29900,
                    "gallons_purchased": 18.5,
                    "cost":              61.03,
                    "cumulative_cost":  124.77
                }
            ],
            "route_geojson": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": {"type": "LineString", ...}, ...},
                    {"type": "Feature", "geometry": {"type": "Point",      ...}, ...}
                ]
            },
            "meta": {
                "external_api_calls": 3,
                "compute_time_ms":    312
            }
        }

    .. tip::
        Paste ``route_geojson`` into https://geojson.io to view the route
        line and every fuel-stop pin on an interactive map.

    HTTP status codes
    -----------------
    * **200** – success
    * **400** – invalid input (missing field, place not geocodable)
    * **422** – no feasible route (gap between stations > max_range mi)
    * **502** – upstream routing API unavailable or returned an error
    """

    def post(self, request: Request) -> Response:
        """Plan the optimal fuel route."""
        t_start = time.monotonic()

        # ── 1. Validate input ────────────────────────────────────────────
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        start_str: str = serializer.validated_data["start"]
        finish_str: str = serializer.validated_data["finish"]
        max_range_miles: float | None = serializer.validated_data.get("max_range_miles")
        mpg: float | None = serializer.validated_data.get("mpg")

        # Pre-compute API call count (before any network I/O)
        api_calls = _count_api_calls(start_str, finish_str)

        # ── 2. Call services ─────────────────────────────────────────────
        try:
            t_ext_start = time.monotonic()
            route = get_route(start_str, finish_str)

            # _resolve_location is LRU-cached: these are free cache hits
            start_coords = _resolve_location(start_str)
            finish_coords = _resolve_location(finish_str)
            t_ext_end = time.monotonic()
            external_call_ms = round((t_ext_end - t_ext_start) * 1000)

            plan = plan_fuel_stops(
                route,
                max_range=max_range_miles,
                mpg=mpg,
            )

        except GeocodingError as exc:
            return Response(
                {"error": str(exc), "code": "GEOCODING_FAILED"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except RoutingError as exc:
            return Response(
                {"error": str(exc), "code": "ROUTING_FAILED"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except requests.exceptions.Timeout:
            return Response(
                {
                    "error": "Upstream routing service timed out. Please retry.",
                    "code": "UPSTREAM_TIMEOUT",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except requests.exceptions.ConnectionError as exc:
            return Response(
                {
                    "error": f"Could not reach routing service: {exc}",
                    "code": "UPSTREAM_UNAVAILABLE",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except NoFeasibleRouteError as exc:
            return Response(
                {"error": str(exc), "code": "NO_FEASIBLE_ROUTE"},
                status=422,  # HTTP 422 Unprocessable Entity
            )

        # ── 3. Assemble response ─────────────────────────────────────────
        total_time_ms = round((time.monotonic() - t_start) * 1000)
        compute_ms = max(0, total_time_ms - external_call_ms)

        payload = build_route_response(
            start_str=start_str,
            finish_str=finish_str,
            start_coords=start_coords,
            finish_coords=finish_coords,
            route=route,
            plan=plan,
            api_calls=api_calls,
            compute_ms=compute_ms,
            external_call_ms=external_call_ms,
            total_time_ms=total_time_ms,
            max_range_miles=max_range_miles,
            mpg=mpg,
        )
        return Response(payload, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_api_calls(start: str, finish: str) -> int:
    """
    Return the number of external HTTP calls this request will make.

    * 0 geocoding calls if both inputs are coordinate strings.
    * 1 geocoding call per free-text input (Nominatim).
    * Always exactly 1 OSRM call.

    Total: 1–3.
    """
    geocoding_calls = (0 if _COORD_RE.match(start) else 1) + (
        0 if _COORD_RE.match(finish) else 1
    )
    return geocoding_calls + 1  # +1 for OSRM
