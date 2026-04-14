"""Django settings for the crawler admin site."""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# ── Paths ───────────────────────────────────────────────────────────────────
# admin_site/admin_site/settings.py  →  parent = admin_site/  →  parent = repo root
BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent

load_dotenv(REPO_DIR / ".env")

# ── Security ────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-autism-crawler-admin-change-me-in-production",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
CSRF_TRUSTED_ORIGINS = os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "https://108.28.10.199,https://127.0.0.1",
).split(",")

# ── Applications ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "monitor",
]

# ── Middleware ───────────────────────────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "admin_site.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "admin_site.wsgi.application"

# ── Database ─────────────────────────────────────────────────────────────────
# Parse DATABASE_URL_SYNC (postgresql://user:pass@host:port/dbname)
_raw = os.environ.get("DATABASE_URL_SYNC", "")
_url = urlparse(_raw)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _url.path.lstrip("/") if _url.path else "autism_crawler",
        "USER": _url.username or "dbuser",
        "PASSWORD": _url.password or "dbpass",
        "HOST": _url.hostname or "localhost",
        "PORT": str(_url.port or 5432),
    }
}

# ── Internationalisation ─────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ── Static files ─────────────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ── Misc ─────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Human-readable site name in the admin header
ADMIN_SITE_HEADER = "Autism Crawler Admin"
ADMIN_SITE_TITLE = "Autism Crawler"
ADMIN_INDEX_TITLE = "Crawler Management"
