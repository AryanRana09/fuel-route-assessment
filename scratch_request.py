import urllib.request
import json
req = urllib.request.Request(
    'http://127.0.0.1:8001/api/route/', 
    data=json.dumps({'start': 'New York, NY', 'finish': 'Los Angeles, CA', 'max_range_miles': 500.0, 'mpg': 10.0}).encode(), 
    headers={'Content-Type': 'application/json'}
)
with urllib.request.urlopen(req) as response:
    data = json.loads(response.read())
    print(f"Total Stops: {len(data['fuel_stops'])}")
    for i, s in enumerate(data['fuel_stops'], 1):
        print(f"{i}. {s['city']}, {s['state']} @ ({s['lat']}, {s['lng']}) | ${s['cost']:.2f}")
