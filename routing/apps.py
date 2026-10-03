"""routing app — Django app configuration."""

from django.apps import AppConfig


class RoutingConfig(AppConfig):
    """Configuration for the routing application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "routing"

    def ready(self) -> None:
        """
        AppConfig ready hook.

        Stations are managed as a thread-safe lazy singleton in
        ``routing.services.fuel_planner._get_station_index``, which loads
        once into memory on the first request (taking ~36ms) and stays cached
        for the process lifetime (sub-microsecond access, 0 DB queries).

        Can also be explicitly pre-warmed by setting WARM_STATIONS_AT_START=true.
        """
        import os
        import sys

        if os.getenv("WARM_STATIONS_AT_START", "false").lower() == "true":
            skip = {"migrate", "makemigrations", "collectstatic", "test"}
            if not any(cmd in sys.argv for cmd in skip) and "pytest" not in sys.modules:
                try:
                    from routing.services.fuel_planner import warm_station_index

                    warm_station_index()
                except Exception:
                    pass
