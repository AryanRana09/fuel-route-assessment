"""
Database models for the routing application.

Only one model is needed: Station, which stores a fuel stop with its
geo-coordinates and retail price, all loaded offline from the OPIS CSV.
"""

from django.db import models


class Station(models.Model):
    """
    A fuel station loaded from the OPIS truckstop price feed.

    Coordinates are stored as plain floats (no PostGIS required) because
    all spatial queries in this project are handled by the haversine formula
    in Python — keeping the stack dependency-free.
    """

    opis_id = models.IntegerField(db_index=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=10, decimal_places=5)
    lat = models.FloatField()
    lng = models.FloatField()

    class Meta:
        indexes = [
            models.Index(fields=["lat", "lng"], name="station_lat_lng_idx"),
        ]
        ordering = ["state", "city", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) – ${self.price}"
