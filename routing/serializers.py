"""
DRF serializers for the routing application.
"""

from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """
    Input serializer for ``POST /api/route/``.

    Both fields accept either:

    * A free-text US place name: ``"New York, NY"``, ``"Houston"``,
      ``"Los Angeles International Airport"``
    * A ``"lat,lng"`` coordinate string: ``"40.7128,-74.0060"``

    The serializer only validates structure and length; coordinate-format
    detection and geocoding happen in the service layer so that any
    ``GeocodingError`` can be mapped to the correct HTTP status.
    """

    start = serializers.CharField(
        min_length=2,
        max_length=300,
        trim_whitespace=True,
        help_text='Start location: US place name or "lat,lng" string.',
    )
    finish = serializers.CharField(
        min_length=2,
        max_length=300,
        trim_whitespace=True,
        help_text='Finish location: US place name or "lat,lng" string.',
    )
    max_range_miles = serializers.FloatField(
        required=False,
        min_value=50.0,
        max_value=2000.0,
        help_text="Optional max vehicle range in miles (default 500).",
    )
    mpg = serializers.FloatField(
        required=False,
        min_value=1.0,
        max_value=100.0,
        help_text="Optional fuel efficiency in miles per gallon (default 10).",
    )

    def validate(self, data: dict) -> dict:
        """Cross-field validation: start and finish must differ."""
        if data["start"].lower() == data["finish"].lower():
            raise serializers.ValidationError(
                {"non_field_errors": "start and finish must be different locations."}
            )
        return data
