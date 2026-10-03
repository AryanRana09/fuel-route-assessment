"""
routing.services.response_builder
====================================

Assembles the final response dict for ``POST /api/route/``, including the
GeoJSON FeatureCollection that can be pasted directly into
`geojson.io <https://geojson.io>`_ to visualise the route and fuel stops
on an interactive map.
"""

from __future__ import annotations


def build_geojson(route: dict, stops: list[dict]) -> dict:
    """
    Build a GeoJSON FeatureCollection from the route geometry and fuel stops.

    .. tip::
        Paste the returned object into https://geojson.io to view the
        route line and every fuel-stop pin on an interactive map.

    Features produced
    -----------------
    * **LineString** – the full OSRM driving route, with ``total_miles``
      and ``duration_minutes`` as properties.
    * **Point** (one per stop) – located at the station's lat/lng, with
      ``name``, ``address``, ``city``, ``state``, ``price_per_gallon``,
      ``mile_marker``, ``gallons_purchased``, ``cost``, and
      ``cumulative_cost`` as properties.

    Parameters
    ----------
    route:
        Dict as returned by :func:`routing.services.routing_client.get_route`.
    stops:
        List of stop dicts as returned by
        :func:`routing.services.fuel_planner.plan_fuel_stops`.

    Returns
    -------
    A valid GeoJSON ``FeatureCollection`` dict.
    """
    features: list[dict] = [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": route["geometry"],  # already [[lng, lat], …]
            },
            "properties": {
                "type": "route",
                "total_miles": route["total_miles"],
                "duration_minutes": route["duration_minutes"],
            },
        }
    ]

    for stop in stops:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [stop["lng"], stop["lat"]],  # GeoJSON [lng, lat]
                },
                "properties": {
                    "type": "fuel_stop",
                    "name": stop["name"],
                    "address": stop["address"],
                    "city": stop["city"],
                    "state": stop["state"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "mile_marker": stop["mile_marker"],
                    "gallons_purchased": stop["gallons_purchased"],
                    "cost": round(stop["cost"], 2),
                    "cumulative_cost": round(stop["cumulative_cost"], 2),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def build_route_response(
    start_str: str,
    finish_str: str,
    start_coords: tuple[float, float],
    finish_coords: tuple[float, float],
    route: dict,
    plan: dict,
    api_calls: int,
    compute_ms: int,
    external_call_ms: int = 0,
    total_time_ms: int | None = None,
) -> dict:
    """
    Assemble the complete ``POST /api/route/`` response dict.

    Parameters
    ----------
    start_str / finish_str:
        Raw user-supplied location strings (used as display names).
    start_coords / finish_coords:
        ``(lat, lng)`` tuples resolved from the user strings.
    route:
        Dict from :func:`routing.services.routing_client.get_route`.
    plan:
        Dict from :func:`routing.services.fuel_planner.plan_fuel_stops`,
        containing ``stops``, ``total_cost``, and ``total_miles``.
    api_calls:
        Number of external HTTP calls made for this request (1–3).
    compute_ms:
        Time spent on local computation in milliseconds (excludes external calls).
    external_call_ms:
        Time spent waiting on external HTTP calls (geocoding + routing) in ms.
    total_time_ms:
        Total wall-clock duration of the request in ms.

    Returns
    -------
    Response dict ready for ``rest_framework.response.Response``.
    """
    stops = plan["stops"]
    total_miles = plan["total_miles"]
    total_cost = plan["total_cost"]
    total_gallons = round(sum(s["gallons_purchased"] for s in stops), 3)

    if total_time_ms is None:
        total_time_ms = compute_ms + external_call_ms

    # Round per-stop money fields to 2 decimal places in the flat list
    rounded_stops = [
        {**s, "cost": round(s["cost"], 2), "cumulative_cost": round(s["cumulative_cost"], 2)}
        for s in stops
    ]

    return {
        "start": {
            "name": start_str,
            "lat": round(start_coords[0], 6),
            "lng": round(start_coords[1], 6),
        },
        "finish": {
            "name": finish_str,
            "lat": round(finish_coords[0], 6),
            "lng": round(finish_coords[1], 6),
        },
        "total_distance_miles": round(total_miles, 2),
        "total_fuel_cost_usd": round(total_cost, 2),
        "total_gallons": total_gallons,
        "fuel_stops": rounded_stops,
        "route_geojson": build_geojson(route, stops),
        "meta": {
            "external_api_calls": api_calls,
            "compute_time_ms": compute_ms,
            "external_call_time_ms": external_call_ms,
            "external_api_time_ms": external_call_ms,
            "total_time_ms": total_time_ms,
        },
    }
