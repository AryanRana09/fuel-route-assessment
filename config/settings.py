"""
Django settings for the fuel-route assessment project.

Reads all secrets from a .env file via python-dotenv.
"""

from pathlib import Path

from dotenv import load_dotenv
import os

# ---------------------------------------------------------------------------
# Base directory & .env loading
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
SECRET_KEY: str = os.environ["DJANGO_SECRET_KEY"]

DEBUG: bool = os.getenv("DJANGO_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS: list[str] = os.getenv(
    "DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver,*"
).split(",")

# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------
INSTALLED_APPS: list[str] = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "routing",
]

MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF: str = "config.urls"

WSGI_APPLICATION: str = "config.wsgi.application"

# ---------------------------------------------------------------------------
# Database – SQLite (no models needed for this project, kept for DRF compat)
# ---------------------------------------------------------------------------
DATABASES: dict = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# ---------------------------------------------------------------------------
# Django REST Framework – JSON only, no browsable API in production
# ---------------------------------------------------------------------------
REST_FRAMEWORK: dict = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
}

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE: str = "en-us"
TIME_ZONE: str = "UTC"
USE_I18N: bool = False
USE_TZ: bool = True

# ---------------------------------------------------------------------------
# Default primary key
# ---------------------------------------------------------------------------
DEFAULT_AUTO_FIELD: str = "django.db.models.BigAutoField"
