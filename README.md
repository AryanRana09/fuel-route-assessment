# Fuel-Efficient Route Planner API

A high-performance Django REST Framework API that calculates the optimal, minimum-cost sequence of fuel stops along any driving route within the United States.

Built according to strict production constraints:
- **Zero external network calls during data import**: 6,600+ fuel stations are cleaned, deduped, and geocoded offline using `geonamescache`.
- **Strict external API budget**: Exactly **one** HTTP request per route calculation (OSRM public routing engine) and at most one call per endpoint for free-text geocoding (Nominatim), with full LRU caching.
- **Microsecond spatial queries**: Route polyline resampling and SciPy `cKDTree` spatial index loaded once as an in-memory singleton.
- **Globally optimal fuel stopping**: Implements the classic "Lazy Greedy" gas station algorithm with lookahead.
- **GeoJSON export**: Returns map-ready GeoJSON features for instant visualization on [geojson.io](https://geojson.io).

---

## Table of Contents

1. [Quickstart & Setup](#quickstart--setup)
2. [Data Pipeline (Loading Stations)](#data-pipeline-loading-stations)
3. [Running the Application](#running-the-application)
4. [API Specification](#api-specification)
   - [GET /api/health/](#get-apihealth)
   - [POST /api/route/](#post-apiroute)
5. [Visualizing Routes on GeoJSON.io](#visualizing-routes-on-geojsonio)
6. [Architecture & Design Decisions](#architecture--design-decisions)
   - [Offline Geocoding](#1-offline-geocoding)
   - [Single External Routing Call & Caching](#2-single-external-routing-call--caching)
   - [KD-Tree Spatial Corridor Indexing](#3-kd-tree-spatial-corridor-indexing)
   - [Greedy Lookahead Optimizer](#4-greedy-lookahead-optimizer)
   - [Process Startup & In-Memory Singleton](#5-process-startup--in-memory-singleton)
7. [Performance & Benchmark Numbers](#performance--benchmark-numbers)
8. [Assumptions & Limitations](#assumptions--limitations)
9. [Running the Test Suite](#running-the-test-suite)
10. [Postman Collection](#postman-collection)

---

## Quickstart & Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.12, 3.13, and 3.14)
- Git

### 1. Clone & Set Up Virtual Environment

```bash
git clone <repo-url>
cd FUEL-ASSESSMENT

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# Linux / macOS:
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

> **Note for Python 3.14 users**: If stable wheels for `numpy` and `scipy` are not yet on standard PyPI for your platform, install pre-release wheels via:
> ```bash
> pip install --pre --extra-index-url https://pypi.anaconda.org/scientific-python-nightly-wheels/simple numpy scipy
> ```

### 3. Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
# Windows
copy .env.example .env
# Linux / macOS
cp .env.example .env
```

Default configuration in `.env`:
```ini
DJANGO_SECRET_KEY=dev-only-insecure-key-change-in-production
DJANGO_DEBUG=True
ROUTING_BASE_URL=http://router.project-osrm.org
NOMINATIM_URL=https://nominatim.openstreetmap.org
USER_AGENT=FuelAssessmentApp/1.0 (contact@fuelassessment.internal)
```

### 4. Run Migrations

```bash
python manage.py migrate
```

---

## Data Pipeline (Loading Stations)

The OPIS fuel price dataset (`data/fuel-prices-for-be-assessment.csv`) contains thousands of truck stop records. The `load_stations` management command cleans, dedupes, geocodes, and loads them into SQLite.
We also use a bundled postal code mapping derived from [GeoNames](https://www.geonames.org/) (licensed under [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)) to geocode small towns missing from `geonamescache`.

```bash
python manage.py load_stations
```

Options:
- `--csv <path>`: Custom CSV path (default: `data/fuel-prices-for-be-assessment.csv`)
- `--clear`: Clear existing station rows prior to re-loading (idempotent reload)

### Pipeline Stages
1. **Filtering**: Retains only continental US states and Washington, D.C. (drops Canadian provinces and non-US records).
2. **Deduplication**: Groups by natural key `(opis_id, address, city, state)`. If duplicates exist, retains the row with the **lowest retail price** and cleans names (e.g. collapsing `"PILOT TRAVEL CENTER #123"` inconsistencies).
3. **Offline Geocoding**: Matches each `(city, state)` against the `geonamescache` US city database offline. Zero external API calls are made.
4. **Bulk Database Insert**: Writes records in batches of 500 using `Station.objects.bulk_create` with an index on `(lat, lng)`.
5. **Cache Refresh**: Automatically warms the in-memory spatial index.

**Output Summary:**
```
-- load_stations summary --
  Rows read          :  8,179
  Dropped (non-US)   :    233
  Duplicates merged  :  1,320
  Geocoded (exact)   :  6,598
  Unmatched / approx :     28
  Stations saved     :  6,626
-- done --
```

---

## Running the Application

Start the Django development server:

```bash
python manage.py runserver
```

The server listens on `http://127.0.0.1:8000`.

---

## API Specification

The API is strictly JSON-based (no HTML views or templates).

### GET `/api/health/`

Liveness and health check endpoint.

**Response `200 OK`**:
```json
{
  "status": "ok"
}
```

---

### POST `/api/route/`

Calculates the driving route between two US locations and returns the optimal sequence of fuel stops.

#### Request Body
Accepts free-text US place names or `"lat,lng"` coordinate strings:

```json
{
  "start": "Chicago, IL",
  "finish": "Dallas, TX"
}
```

*Coordinate example with optional vehicle parameters:*
```json
{
  "start": "41.8781,-87.6298",
  "finish": "32.7767,-96.7970",
  "max_range_miles": 800.0,
  "mpg": 15.0
}
```

#### Response Body (`200 OK`)

```json
{
  "start": {
    "name": "Chicago, IL",
    "lat": 41.878113,
    "lng": -87.629799
  },
  "finish": {
    "name": "Dallas, TX",
    "lat": 32.776272,
    "lng": -96.796856
  },
  "total_distance_miles": 966.92,
  "total_fuel_cost_usd": 135.48,
  "total_gallons": 46.692,
  "fuel_stops": [
    {
      "name": "Love'S Travel Stop",
      "address": "I-55 EXIT 88",
      "city": "Williamsville",
      "state": "IL",
      "lat": 40.0166,
      "lng": -89.5444,
      "mile_marker": 182.4,
      "price_per_gallon": 2.899,
      "gallons_purchased": 20.0,
      "cost": 57.98,
      "cumulative_cost": 57.98
    }
  ],
  "vehicle": {
    "max_range_miles": 800.0,
    "mpg": 15.0,
    "tank_capacity_gallons": 53.33
  },
  "route_geojson": {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "geometry": {
          "type": "LineString",
          "coordinates": [[-87.6298, 41.8781], ...]
        },
        "properties": {
          "type": "route",
          "total_miles": 966.92,
          "duration_minutes": 884.25
        }
      },
      {
        "type": "Feature",
        "geometry": {
          "type": "Point",
          "coordinates": [-89.5444, 40.0166]
        },
        "properties": {
          "type": "fuel_stop",
          "name": "Love'S Travel Stop",
          "price_per_gallon": 2.899,
          "mile_marker": 182.4,
          "cost": 57.98
        }
      }
    ]
  },
  "meta": {
    "external_api_calls": 3,
    "compute_time_ms": 15,
    "external_call_time_ms": 752,
    "external_api_time_ms": 752,
    "total_time_ms": 767
  }
}
```

#### HTTP Status Codes
- **`200 OK`**: Route and fuel stops planned successfully.
- **`400 Bad Request`**: Missing required fields, identical start and finish, invalid vehicle parameters, or location could not be geocoded (`GEOCODING_FAILED`).
- **`422 Unprocessable Entity`**: No feasible route (`NO_FEASIBLE_ROUTE`). Occurs if any gap between reachable fuel stations (or from the start / to the finish) exceeds the vehicle's maximum range.
- **`502 Bad Gateway`**: Upstream service failure (`ROUTING_FAILED`, `UPSTREAM_TIMEOUT`, or `UPSTREAM_UNAVAILABLE`).

---

## Visualizing Routes on GeoJSON.io

The response includes a standard GeoJSON `FeatureCollection` in `route_geojson`:
1. Copy the value of the `route_geojson` object from the API response.
2. Open [https://geojson.io](https://geojson.io).
3. Paste the JSON into the right-hand code panel.
4. The map will immediately render the driving path (`LineString`) and pin every recommended fuel stop (`Point`) with station name, price, and fuel stop metrics.

---

## Architecture & Design Decisions

```
POST /api/route/
      │
      ▼
 RouteRequestSerializer ───► Validate start & finish (length, distinct)
      │
      ▼
 routing_client.get_route()
      ├── Nominatim Geocoding (0 calls if lat/lng, max 1 per text endpoint, LRU-cached)
      └── OSRM Driving Route (exactly 1 call per route, LRU-cached)
      │
      ▼
 fuel_planner.plan_fuel_stops()
      ├── Polyline Resampling (points spaced ~1 mile apart with cumulative mile markers)
      ├── Spatial Corridor Search (cKDTree query against 6,626 stations, <= 10 miles)
      └── Lazy Greedy Optimizer (lookahead in 500-mile tank window, minimum fuel cost)
      │
      ▼
 response_builder.build_route_response()
      ├── GeoJSON FeatureCollection (LineString route + Point fuel stops)
      └── Metrics (compute_time_ms isolated from external_call_time_ms)
```

### 1. Offline Geocoding
Instead of making 8,000+ rate-limited HTTP calls during station loading, the pipeline uses `geonamescache` to match cities and states locally. Station coordinates are indexed in SQLite with `db_index=True`.

### 2. Single External Routing Call & Caching
- **Budget Compliance**: For coordinate inputs (`"41.8781,-87.6298"`), zero geocoding requests are made. For free text names, at most two geocoding calls occur. Exactly **one** HTTP call is made to the OSRM `/route/v1/driving/` endpoint with `overview=full&geometries=geojson`.
- **LRU In-Memory Cache**: Repeat requests for identical coordinates or place names bypass external networks entirely (0 HTTP calls).

### 3. KD-Tree Spatial Corridor Indexing
- The route polyline is resampled into discrete points approximately 1 mile apart with cumulative distance tracking.
- A SciPy `cKDTree` built over all 6,626 stations queries the corridor points in vectorized batches to identify candidate stations within 10 miles (`FUEL_CORRIDOR_MILES`).
- Each candidate is mapped to its nearest route point to obtain its precise mile marker along the travel trajectory.

### 4. Greedy Lookahead Optimizer
Solves the classic Gas Station Problem with proven polynomial-time optimality:
1. **Initial State**: Vehicle starts with a full tank (configurable max range and MPG per request; defaults to 500 miles and 10 MPG).
2. **Cheaper Station Ahead**: If a station cheaper than current fuel is reachable within current range, drive there and purchase only the minimum fuel required to reach it.
3. **Cheapest in Window**: If no cheaper station exists ahead, fill the tank up to the amount needed to reach the cheapest station in the full 500-mile window (or destination).
4. **Feasibility Guarantee**: If the distance between consecutive reachable stations exceeds 500 miles, raises `NoFeasibleRouteError` (HTTP 422).

### 5. Process Startup & In-Memory Singleton
Stations are loaded into memory as a thread-safe singleton (`_get_station_index` guarded by `threading.Lock`). Once loaded (~36 ms), all threads access station coordinates in sub-microsecond time with zero database queries.

---

## Performance & Benchmark Numbers

Measured using [`benchmark.py`](file:///c:/Users/aryan/Desktop/Projects/FUEL-ASSESSMENT/benchmark.py) on SQLite with 6,626 stations:

| Operation | Latency | Details |
| :--- | :--- | :--- |
| **Cold Station Index Load** | **36.10 ms** | 6,626 records loaded into memory + SciPy KDTree built (run once) |
| **Warm Station Index Access** | **0.0002 ms** | In-memory singleton reuse |
| **Uncached Route (Chicago -> Dallas)** | **800.08 ms** | Includes Nominatim + OSRM network transit (external: 752 ms, compute: 15 ms) |
| **Cached Repeat Route (N=50 runs)** | **22.20 ms avg** | **Well under the 100 ms requirement** (Min: 21.42 ms, Max: 24.44 ms) |
| **Cross-Country (NY -> LA, 2,794 mi)** | **71.53 ms** | Cached repeat request with 13 optimal fuel stops |

---

## Assumptions & Limitations

### Assumptions
1. **Starting Fuel**: The vehicle begins the trip with a full tank (configurable max range, default 500 miles). Fuel purchased at stops is tallied; the initial tank is a sunk cost.
2. **Fuel Economy**: Constant miles per gallon (configurable, default 10 MPG).
3. **Station Locations**: Stations are geocoded to city center coordinates from the OPIS city/state metadata.
4. **Corridor Radius**: Stations within 10 miles of the route polyline are considered accessible without significant detour penalty (configurable via `FUEL_CORRIDOR_MILES`).

### Limitations
1. **Public Demo API Limits**: Uses the public `router.project-osrm.org` server. For high-volume production deployments, a dedicated self-hosted OSRM Docker container would be used.
2. **US Territory Only**: Constrained to the 50 US states and Washington, D.C.
3. **Static Fuel Prices**: Fuel prices reflect the provided OPIS assessment dataset snapshot.

---

## Running the Test Suite

Run all unit and integration tests via `pytest`:

```bash
pytest -v
```

### Test Coverage (111 Tests)
- `tests/test_health.py`: Health endpoint status and structure.
- `tests/test_pipeline.py`: CSV parsing, state filtering, deduplication, and offline geocoding.
- `tests/test_routing_client.py`: Coordinate parsing, Nominatim geocoding, OSRM client, and LRU cache.
- `tests/test_fuel_planner.py`: Polyline resampling, KD-Tree corridor filtering, and greedy fuel optimization.
- `tests/test_route_view.py`: DRF serializer validation, HTTP status mapping (400, 422, 502), and GeoJSON construction.
- `tests/test_api_integration.py`: End-to-end API tests with mocked routing client (success, bad input, infeasible route, timing isolation, and < 100 ms cached repeats).

---

## Postman Collection

A complete Postman collection is included in [`postman/fuel-route.postman_collection.json`](file:///c:/Users/aryan/Desktop/Projects/FUEL-ASSESSMENT/postman/fuel-route.postman_collection.json).

### Requests Included
1. **Health Check**: `GET /api/health/`
2. **Short Route**: `POST /api/route/` (`Chicago, IL` -> `Dallas, TX`)
3. **Long Multi-Stop Route**: `POST /api/route/` (`New York, NY` -> `Los Angeles, CA`)
4. **Coordinates-Only**: `POST /api/route/` (`"41.8781,-87.6298"` -> `"32.7767,-96.7970"`)
5. **Error Case - Missing Field**: `POST /api/route/` (missing `finish` -> 400 Bad Request)
6. **Error Case - Identical Endpoints**: `POST /api/route/` (`Chicago, IL` -> `Chicago, IL` -> 400 Bad Request)
7. **Error Case - Invalid Vehicle Params**: `POST /api/route/` (out-of-bounds `max_range_miles` -> 400 Bad Request)

Import the file into Postman to test immediately.
