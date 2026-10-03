"""Root URL configuration for the fuel-route assessment project."""

from django.urls import path, include

urlpatterns = [
    path("api/", include("routing.urls")),
]
