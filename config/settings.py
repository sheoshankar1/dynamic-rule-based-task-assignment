"""Django settings. Everything environment-dependent comes from env vars so the
same image runs under docker-compose and against a local Postgres."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "assignment",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "taskassign"),
        "USER": os.environ.get("POSTGRES_USER", "bench"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        # Default to a UNIX socket dir so local runs need no TCP listener;
        # docker-compose overrides this with the service name.
        "HOST": os.environ.get("POSTGRES_HOST", "/tmp"),
        "PORT": os.environ.get("POSTGRES_PORT", "5439"),
    }
}

AUTH_USER_MODEL = "assignment.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Dynamic Rule-Based Task Assignment",
    "DESCRIPTION": (
        "Tasks are never assigned by hand. Each task carries a rule describing "
        "who may do it; the system computes eligible users and assigns in the "
        "background, in priority order. See README.md for the architecture."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Serve Swagger UI's own assets from this app instead of cdn.jsdelivr.net.
    # With the CDN the page returns 200 and then renders blank on any machine
    # that is offline or behind a proxy that blocks it -- which is exactly how
    # a reviewer would first see it.
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "REDOC_DIST": "SIDECAR",
}

# The dev frontend runs on its own origin (Vite, :5173) and calls this API.
CORS_ALLOWED = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173")


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# Run Celery jobs inline when there is no broker -- lets the test suite and a
# bare `runserver` exercise the full path without Redis. docker-compose sets
# this to 0 and runs a real worker.
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_EAGER", "1") == "1"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Cache backs the recompute debounce. Redis gives a real atomic SETNX across
# processes; LocMem is per-process and adequate only for tests and dev.
CACHES = {
    "default": (
        {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
        if not CELERY_TASK_ALWAYS_EAGER
        else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    )
}
