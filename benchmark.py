import os
import statistics
import time
import django

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()

from rest_framework.test import APIClient
from routing.services.fuel_planner import _get_station_index, invalidate_station_index

print("=========================================================")
print("             STEP 6 PERFORMANCE BENCHMARK                ")
print("=========================================================")

print("\n--- 1. STATION SPATIAL INDEX LOADING ---")
invalidate_station_index()
t0 = time.perf_counter()
records, coords, tree = _get_station_index()
cold_load_ms = (time.perf_counter() - t0) * 1000
print(f"Cold Station Index Load : {cold_load_ms:.2f} ms")
print(f"  Stations in memory     : {len(records):,}")
print(f"  cKDTree coordinates    : {coords.shape}")

warm_times = []
for _ in range(100):
    t0 = time.perf_counter()
    _get_station_index()
    warm_times.append((time.perf_counter() - t0) * 1000)
print(f"Warm Index Access (100x): avg={statistics.mean(warm_times):.4f} ms, max={max(warm_times):.4f} ms")

print("\n--- 2. FIRST REQUEST (UNCACHED) ---")
client = APIClient()
payload = {"start": "Chicago, IL", "finish": "Dallas, TX"}

t0 = time.perf_counter()
resp1 = client.post("/api/route/", payload, format="json")
first_call_ms = (time.perf_counter() - t0) * 1000
assert resp1.status_code == 200, resp1.content
d1 = resp1.json()

print(f"End-to-End Latency      : {first_call_ms:.2f} ms")
print(f"  meta.external_call_time_ms : {d1['meta']['external_call_time_ms']} ms")
print(f"  meta.compute_time_ms       : {d1['meta']['compute_time_ms']} ms")
print(f"  meta.total_time_ms         : {d1['meta']['total_time_ms']} ms")
print(f"  Route Distance             : {d1['total_distance_miles']} miles")
print(f"  Optimal Fuel Stops         : {len(d1['fuel_stops'])}")
print(f"  Total Fuel Cost            : ${d1['total_fuel_cost_usd']}")

print("\n--- 3. REPEAT REQUESTS (CACHED) - 50 RUNS ---")
repeat_latencies = []
meta_compute_times = []

for _ in range(50):
    t0 = time.perf_counter()
    resp = client.post("/api/route/", payload, format="json")
    t_elapsed = (time.perf_counter() - t0) * 1000
    assert resp.status_code == 200
    repeat_latencies.append(t_elapsed)
    meta_compute_times.append(resp.json()["meta"]["compute_time_ms"])

repeat_latencies.sort()
p50 = statistics.median(repeat_latencies)
p90 = repeat_latencies[int(len(repeat_latencies) * 0.90)]
p99 = repeat_latencies[-1]

print("Latency Distribution:")
print(f"  Min   : {min(repeat_latencies):.2f} ms")
print(f"  Mean  : {statistics.mean(repeat_latencies):.2f} ms")
print(f"  p50   : {p50:.2f} ms")
print(f"  p90   : {p90:.2f} ms")
print(f"  Max   : {p99:.2f} ms")
print(f"Under 100ms Target: {'PASS - Well under 100ms' if max(repeat_latencies) < 100 else 'FAIL'}")
print(f"Average meta.compute_time_ms: {statistics.mean(meta_compute_times):.2f} ms")

print("\n--- 4. CROSS-COUNTRY ROUTE (NEW YORK -> LOS ANGELES, ~2,800 MILES) ---")
payload_ny_la = {"start": "New York, NY", "finish": "Los Angeles, CA"}
t0 = time.perf_counter()
resp_ny_la = client.post("/api/route/", payload_ny_la, format="json")
ny_la_first_ms = (time.perf_counter() - t0) * 1000
d_ny = resp_ny_la.json()
print(f"NY -> LA First Call:")
print(f"  Total Latency              : {ny_la_first_ms:.2f} ms")
print(f"  meta.external_call_time_ms : {d_ny['meta']['external_call_time_ms']} ms")
print(f"  meta.compute_time_ms       : {d_ny['meta']['compute_time_ms']} ms")
print(f"  Route Distance             : {d_ny['total_distance_miles']} miles")
print(f"  Fuel Stops                 : {len(d_ny['fuel_stops'])}")
print(f"  Total Cost                 : ${d_ny['total_fuel_cost_usd']}")

# Cached NY -> LA
t0 = time.perf_counter()
resp_ny_la_cached = client.post("/api/route/", payload_ny_la, format="json")
ny_la_cached_ms = (time.perf_counter() - t0) * 1000
print(f"NY -> LA Cached Repeat Call:")
print(f"  Total Latency              : {ny_la_cached_ms:.2f} ms")
print(f"  meta.compute_time_ms       : {resp_ny_la_cached.json()['meta']['compute_time_ms']} ms")
print("=========================================================")
