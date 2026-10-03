"""
Management command: load_stations
==================================

Reads the OPIS fuel-price CSV, cleans/dedupes it, geocodes offline, and
bulk-inserts into the Station table.

Usage
-----
    python manage.py load_stations
    python manage.py load_stations --csv data/fuel-prices-for-be-assessment.csv
    python manage.py load_stations --csv data/fuel-prices-for-be-assessment.csv --clear

Options
-------
--csv   Path to the OPIS CSV file.
        Default: data/fuel-prices-for-be-assessment.csv
--clear Truncate the Station table before loading (idempotent re-runs).
"""

import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from routing.geocoder import geocode
from routing.models import Station

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CSV = Path("data") / "fuel-prices-for-be-assessment.csv"

# Two-letter codes for all 50 US states + DC.
US_STATE_CODES: frozenset[str] = frozenset(
    {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
        "DC",
    }
)

# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    """Load fuel stations from the OPIS CSV into the database."""

    help = "Load fuel stations from the OPIS price CSV (offline geocoding)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--csv",
            default=str(DEFAULT_CSV),
            help="Path to the OPIS CSV file (default: data/fuel-prices-for-be-assessment.csv)",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing stations before loading.",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        csv_path = Path(options["csv"])
        if not csv_path.exists():
            raise CommandError(
                f"CSV file not found: {csv_path}\n"
                "Copy the CSV into the data/ directory and re-run."
            )

        if options["clear"]:
            deleted, _ = Station.objects.all().delete()
            self.stdout.write(f"  Cleared {deleted} existing stations.")

        # 1. Read
        raw_rows = _read_csv(csv_path)
        n_read = len(raw_rows)

        # 2. Filter to US only
        us_rows = [r for r in raw_rows if r["state"] in US_STATE_CODES]
        n_dropped_foreign = n_read - len(us_rows)

        # 3. Deduplicate
        deduped, n_dupes_merged = _dedupe(us_rows)

        # 4. Geocode (offline)
        stations: list[Station] = []
        n_postal = 0
        n_geonamescache = 0
        n_skipped = 0
        skipped_samples = []

        for row in deduped:
            result = geocode(row["city"], row["state"])
            if result is None:
                n_skipped += 1
                if len(skipped_samples) < 20:
                    skipped_samples.append(f"{row['city']}, {row['state']} (OPIS ID {row['opis_id']})")
                continue
            
            if result.source == "postal":
                n_postal += 1
            elif result.source == "geonamescache":
                n_geonamescache += 1

            stations.append(
                Station(
                    opis_id=row["opis_id"],
                    name=row["name"],
                    address=row["address"],
                    city=row["city"],
                    state=row["state"],
                    price=row["price"],
                    lat=result.lat,
                    lng=result.lng,
                )
            )

        # 5. Bulk insert
        Station.objects.bulk_create(stations, batch_size=500)

        # Invalidate and re-warm in-memory station cache
        from routing.services.fuel_planner import invalidate_station_index, warm_station_index

        invalidate_station_index()
        warm_station_index()

        # 6. Summary
        if skipped_samples:
            self.stdout.write(self.style.WARNING("\nFirst 20 skipped (unmatched) stations:"))
            for s in skipped_samples:
                self.stdout.write(f"  - {s}")

        self.stdout.write(self.style.SUCCESS("\n-- load_stations summary --"))
        self.stdout.write(f"  Rows read          : {n_read:>6,}")
        self.stdout.write(f"  Dropped (non-US)   : {n_dropped_foreign:>6,}")
        self.stdout.write(f"  Duplicates merged  : {n_dupes_merged:>6,}")
        self.stdout.write(f"  Matched via postal : {n_postal:>6,}")
        self.stdout.write(f"  Matched via geonames: {n_geonamescache:>6,}")
        self.stdout.write(f"  Skipped (unmatched): {n_skipped:>6,}")
        self.stdout.write(f"  Stations saved     : {len(stations):>6,}")
        self.stdout.write(self.style.SUCCESS("-- done --\n"))


# ---------------------------------------------------------------------------
# Helpers (module-level so they can be unit-tested independently)
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict]:
    """
    Read the OPIS CSV and return a list of dicts with normalised keys.

    Rows with an unparseable price are silently skipped (there are none in
    the reference dataset, but this makes the loader robust to future data).
    """
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig") as fh:  # handle optional BOM
        reader = csv.DictReader(fh)
        for raw in reader:
            price_str = raw.get("Retail Price", "").strip()
            try:
                price = Decimal(price_str)
            except InvalidOperation:
                continue
            rows.append(
                {
                    "opis_id": int(raw["OPIS Truckstop ID"]),
                    "name": _clean_name(raw["Truckstop Name"]),
                    "address": raw["Address"].strip(),
                    "city": raw["City"].strip().title(),
                    "state": raw["State"].strip().upper(),
                    "price": price,
                }
            )
    return rows


def _clean_name(raw: str) -> str:
    """
    Normalise a truckstop name:

    * Title-case.
    * Collapse multiple spaces.
    * Strip leading/trailing whitespace.
    """
    return re.sub(r"\s+", " ", raw.strip().title())


def _dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """
    Deduplicate rows on the natural key ``(opis_id, address, city, state)``.

    When duplicates exist, keep the row with the **lowest retail price** and
    use the **shortest clean name** (avoids "PILOT TRAVEL CENTER #1243" vs
    "PILOT #1243" inconsistencies).

    Returns
    -------
    deduped:
        List of unique rows after merging.
    n_merged:
        Number of rows that were eliminated as duplicates.
    """
    # Group by natural key
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["opis_id"], row["address"], row["city"], row["state"])
        groups.setdefault(key, []).append(row)

    deduped: list[dict] = []
    n_merged = 0

    for group in groups.values():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            n_merged += len(group) - 1
            # Best price (lowest cost for the driver)
            best = min(group, key=lambda r: r["price"])
            # Shortest non-empty name as the canonical display name
            best["name"] = min(
                (r["name"] for r in group if r["name"]),
                key=len,
                default=best["name"],
            )
            deduped.append(best)

    return deduped, n_merged
