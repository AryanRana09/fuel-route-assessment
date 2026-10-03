"""
Unit tests for the data pipeline helpers.

These tests exercise _dedupe and _read_csv without touching the database,
and verify geocoder behaviour without network calls.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from routing.management.commands.load_stations import _clean_name, _dedupe, _read_csv
from routing.geocoder import geocode, _normalise


# ---------------------------------------------------------------------------
# _dedupe tests
# ---------------------------------------------------------------------------


class TestDedupe:
    """Tests for the _dedupe() helper."""

    def _row(
        self,
        opis_id: int = 1,
        address: str = "I-44, EXIT 1",
        city: str = "Tulsa",
        state: str = "OK",
        price: str = "3.50",
        name: str = "Station A",
    ) -> dict:
        return {
            "opis_id": opis_id,
            "address": address,
            "city": city,
            "state": state,
            "price": Decimal(price),
            "name": name,
        }

    def test_no_duplicates_unchanged(self) -> None:
        """Unique rows pass through unmodified."""
        rows = [self._row(opis_id=1), self._row(opis_id=2)]
        deduped, merged = _dedupe(rows)
        assert len(deduped) == 2
        assert merged == 0

    def test_duplicate_keeps_lowest_price(self) -> None:
        """When two rows share the same key, the lower price is kept."""
        rows = [
            self._row(price="3.50", name="Long Name Station"),
            self._row(price="3.10", name="Short Name"),
        ]
        deduped, merged = _dedupe(rows)
        assert len(deduped) == 1
        assert deduped[0]["price"] == Decimal("3.10")
        assert merged == 1

    def test_duplicate_keeps_shortest_name(self) -> None:
        """Shortest name is kept regardless of which row had the best price."""
        rows = [
            self._row(price="3.10", name="Pilot Travel Center #1243"),
            self._row(price="3.50", name="Pilot #1243"),
        ]
        deduped, merged = _dedupe(rows)
        assert deduped[0]["name"] == "Pilot #1243"

    def test_three_duplicates_merged_correctly(self) -> None:
        """Three identical keys → one row kept, two merged."""
        rows = [
            self._row(price="3.80", name="X"),
            self._row(price="3.20", name="XY"),
            self._row(price="3.60", name="XYZ"),
        ]
        deduped, merged = _dedupe(rows)
        assert len(deduped) == 1
        assert merged == 2
        assert deduped[0]["price"] == Decimal("3.20")
        assert deduped[0]["name"] == "X"

    def test_different_states_not_merged(self) -> None:
        """Same opis_id + address + city but different state → two rows."""
        rows = [
            self._row(state="TX"),
            self._row(state="OK"),
        ]
        deduped, merged = _dedupe(rows)
        assert len(deduped) == 2
        assert merged == 0


# ---------------------------------------------------------------------------
# _clean_name tests
# ---------------------------------------------------------------------------


class TestCleanName:
    """Tests for the _clean_name() helper."""

    def test_title_cases_name(self) -> None:
        assert _clean_name("WOODSHED OF BIG CABIN") == "Woodshed Of Big Cabin"

    def test_collapses_extra_spaces(self) -> None:
        assert _clean_name("PILOT  TRAVEL   CENTER") == "Pilot Travel Center"

    def test_strips_whitespace(self) -> None:
        assert _clean_name("  Station  ") == "Station"


# ---------------------------------------------------------------------------
# Geocoder tests (offline — no network)
# ---------------------------------------------------------------------------


class TestGeocoder:
    """Tests for the offline geocode() function."""

    def test_known_city_returns_coords(self) -> None:
        """A well-known city should resolve with exact=True."""
        result = geocode("Houston", "TX")
        assert result is not None
        assert result.exact is True
        # Houston is roughly 29.7°N, 95.4°W
        assert 29.0 < result.lat < 30.5
        assert -96.5 < result.lng < -94.5

    def test_unknown_city_falls_back_to_state(self) -> None:
        """A made-up city should fall back to state centroid (exact=False)."""
        result = geocode("Zzzyxnonexistent", "TX")
        assert result is not None
        assert result.exact is False

    def test_normalise_strips_accents(self) -> None:
        """Normaliser converts accented chars to ASCII equivalents."""
        assert _normalise("Québec") == "quebec"

    def test_normalise_lowercases(self) -> None:
        assert _normalise("San Antonio") == "san antonio"

    def test_normalise_collapses_punctuation(self) -> None:
        assert _normalise("St. Louis") == "st louis"

    def test_canadian_province_returns_none_or_fallback(self) -> None:
        """Canadian province codes should not match US state centroids."""
        # 'ON' is Ontario — not in our US state set, so no centroid exists
        result = geocode("Toronto", "ON")
        # Our state_centroids only cover US admin1codes, so should be None
        assert result is None
