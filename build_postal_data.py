import urllib.request
import zipfile
import io
import csv
from collections import defaultdict
import os

url = "https://download.geonames.org/export/zip/US.zip"
print("Downloading GeoNames postal data...")
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req) as response:
    zip_data = response.read()

places = defaultdict(list)

print("Parsing US.txt...")
with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
    with z.open("US.txt") as f:
        # Format is tab-separated, no header.
        # columns: 0: country code, 1: postal code, 2: place name, 3: admin name1, 4: admin code1 (state),
        # 5: admin name2, 6: admin code2, 7: admin name3, 8: admin code3, 9: latitude, 10: longitude, 11: accuracy
        for line in f:
            parts = line.decode('utf-8').strip("\r\n").split('\t')
            if len(parts) >= 11:
                place_name = parts[2].strip()
                state = parts[4].strip()
                try:
                    lat = float(parts[9])
                    lng = float(parts[10])
                except ValueError:
                    continue
                if place_name and state:
                    key = (place_name.lower(), state.lower())
                    places[key].append((place_name, state, lat, lng))

output_path = os.path.join("data", "us_places.csv")
os.makedirs("data", exist_ok=True)
print(f"Averaging coordinates and writing to {output_path}...")
with open(output_path, "w", newline="", encoding="utf-8") as out:
    writer = csv.writer(out)
    writer.writerow(["place_name", "state", "lat", "lng"])
    for (place_lower, state_lower), records in places.items():
        avg_lat = sum(r[2] for r in records) / len(records)
        avg_lng = sum(r[3] for r in records) / len(records)
        # Use the first record's original casing for place_name and state
        writer.writerow([records[0][0], records[0][1], round(avg_lat, 6), round(avg_lng, 6)])

print(f"Created {output_path} with {len(places)} unique places.")
