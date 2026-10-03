"""
URL configuration for the routing app.

Endpoints
---------
GET  /api/health/  ->  HealthView   (liveness check)
POST /api/route/   ->  RouteView    (fuel-optimised route planner)
"""

from django.urls import path

from routing.views import HealthView, RouteView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("route/", RouteView.as_view(), name="route"),
]
