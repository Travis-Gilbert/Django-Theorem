"""Django settings for theorem_control — SPEC-THEOREM-CONTROL-PLANE-1.0."""

from __future__ import annotations

import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    DISABLE_SERVER_SIDE_CURSORS=(bool, True),
    CONN_MAX_AGE=(int, 0),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Control-plane apps
    "apps.tenancy",
    "apps.identity",
    "apps.billing",
    "apps.keys",
    "apps.orchestration",
    "apps.competence",
    "apps.observation",
    "apps.support",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "theorem_control.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "theorem_control.wsgi.application"

# ---------------------------------------------------------------------------
# Database — direct Neon Postgres (D10)
# CONN_MAX_AGE=0 avoids holding connections across requests. Schema `control`
# is selected via a startup search_path option; Neon's pooler rejects that
# option, so the deployed DATABASE_URL must target the direct endpoint.
# ---------------------------------------------------------------------------
_db_url = env("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
DATABASES = {"default": env.db_url_config(_db_url)}
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = env("DISABLE_SERVER_SIDE_CURSORS")
DATABASES["default"]["CONN_MAX_AGE"] = env("CONN_MAX_AGE")

if DATABASES["default"].get("ENGINE", "").endswith("postgresql"):
    DATABASES["default"].setdefault("OPTIONS", {})
    # Keep Django migrations and ORM inside the control schema only.
    existing = DATABASES["default"]["OPTIONS"].get("options", "")
    search = "-c search_path=control,public"
    DATABASES["default"]["OPTIONS"]["options"] = f"{existing} {search}".strip()

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# External services (stubs tolerate absence)
# ---------------------------------------------------------------------------
WORKOS_API_KEY = env("WORKOS_API_KEY", default="")
WORKOS_CLIENT_ID = env("WORKOS_CLIENT_ID", default="")
WORKOS_WEBHOOK_SECRET = env("WORKOS_WEBHOOK_SECRET", default="test-webhook-secret")

STRIPE_API_KEY = env("STRIPE_API_KEY", default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# RunPod is an explicitly configured Serverless executor. Keeping the endpoint
# identifier separate from the API key lets the same deployment route only the
# intended Theorem worker image.
RUNPOD_API_KEY = env("RUNPOD_API_KEY", default="")
RUNPOD_SERVERLESS_ENDPOINT_ID = env("RUNPOD_SERVERLESS_ENDPOINT_ID", default="")
RUNPOD_API_BASE = env("RUNPOD_API_BASE", default="https://api.runpod.ai/v2")
RUNPOD_REQUEST_TIMEOUT_SECONDS = env.float(
    "RUNPOD_REQUEST_TIMEOUT_SECONDS", default=30.0
)
RUNPOD_JOB_TIMEOUT_SECONDS = env.int("RUNPOD_JOB_TIMEOUT_SECONDS", default=900)
RUNPOD_POLL_INTERVAL_SECONDS = env.float("RUNPOD_POLL_INTERVAL_SECONDS", default=5.0)
# This is a provenance identifier, not a registry credential. It must be an
# immutable image digest for the worker serving RUNPOD_SERVERLESS_ENDPOINT_ID.
RUNPOD_WORKER_IMAGE_DIGEST = env("RUNPOD_WORKER_IMAGE_DIGEST", default="")
OFFLOAD_EXECUTION_MODE = env("OFFLOAD_EXECUTION_MODE", default="stub").strip().lower()
R_OFFLOAD_EXECUTION_MODE = (
    env("R_OFFLOAD_EXECUTION_MODE", default="stub").strip().lower()
)
RENV_LOCKFILE_PATH = env("RENV_LOCKFILE_PATH", default=str(BASE_DIR / "renv.lock"))

# Neon Object Storage is S3-compatible. Only trusted Fly services receive
# these credentials; RunPod receives operation-scoped presigned URLs instead.
ARTIFACT_S3_ENDPOINT_URL = env("ARTIFACT_S3_ENDPOINT_URL", default="")
ARTIFACT_S3_ACCESS_KEY_ID = env("ARTIFACT_S3_ACCESS_KEY_ID", default="")
ARTIFACT_S3_SECRET_ACCESS_KEY = env("ARTIFACT_S3_SECRET_ACCESS_KEY", default="")
ARTIFACT_S3_REGION = env("ARTIFACT_S3_REGION", default="")
ARTIFACT_S3_BUCKET = env("ARTIFACT_S3_BUCKET", default="")
ARTIFACT_PRESIGN_SECONDS = env.int("ARTIFACT_PRESIGN_SECONDS", default=900)
ARTIFACT_MAX_BYTES = env.int("ARTIFACT_MAX_BYTES", default=64 * 1024 * 1024)

THEOREM_API_BASE = env("THEOREM_API_BASE", default="http://127.0.0.1:8080")
THEOREM_MACHINE_KEY = env("THEOREM_MACHINE_KEY", default="")

# Empty VALKEY_URL/REDIS_URL → tenant_cache uses in-memory dict (tests / no Redis).
VALKEY_URL = env("VALKEY_URL", default=env("REDIS_URL", default=""))
REDIS_URL = VALKEY_URL

# Celery
CELERY_BROKER_URL = VALKEY_URL or "memory://"
CELERY_RESULT_BACKEND = "cache+memory://" if not VALKEY_URL else VALKEY_URL
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ROUTES = {
    "apps.orchestration.tasks.run_r_*": {"queue": "offload.r"},
    "apps.orchestration.tasks.run_offload_r": {"queue": "offload.r"},
}
CELERY_TASK_DEFAULT_QUEUE = "celery"
COMPETENCE_STALE_AFTER_SECONDS = env.int(
    "COMPETENCE_STALE_AFTER_SECONDS", default=15 * 60
)
COMPETENCE_SWEEP_BATCH_SIZE = env.int("COMPETENCE_SWEEP_BATCH_SIZE", default=100)
COMPETENCE_SWEEP_INTERVAL_SECONDS = env.int(
    "COMPETENCE_SWEEP_INTERVAL_SECONDS", default=60 * 60
)
CELERY_BEAT_SCHEDULE = {
    "competence-sleep-cycle": {
        "task": "apps.competence.tasks.sweep_competence_jobs",
        "schedule": COMPETENCE_SWEEP_INTERVAL_SECONDS,
    }
}
# R queue note: workers consuming `offload.r` must pin R + renv; agent name is "R".
CELERY_R_QUEUE = "offload.r"
CELERY_R_AGENT_NAME = "R"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
