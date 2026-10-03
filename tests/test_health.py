"""
Unit tests for the health-check endpoint.

These tests exercise the GET /api/health/ route without hitting any
external services or the database.
"""

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    """Return a DRF test client."""
    return APIClient()


@pytest.mark.django_db
class TestHealthEndpoint:
    """Tests for GET /api/health/."""

    def test_returns_200(self, client: APIClient) -> None:
        """Health endpoint must respond with HTTP 200."""
        response = client.get("/api/health/")
        assert response.status_code == 200

    def test_returns_json_status_ok(self, client: APIClient) -> None:
        """Health endpoint must return {"status": "ok"}."""
        response = client.get("/api/health/")
        assert response.json() == {"status": "ok"}

    def test_content_type_is_json(self, client: APIClient) -> None:
        """Response Content-Type must be application/json."""
        response = client.get("/api/health/")
        assert "application/json" in response["Content-Type"]

    def test_post_not_allowed(self, client: APIClient) -> None:
        """Health endpoint is GET-only; POST should return 405."""
        response = client.post("/api/health/", data={}, format="json")
        assert response.status_code == 405
