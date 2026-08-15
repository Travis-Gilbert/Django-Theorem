#!/bin/sh
set -eu

# Runtime and migration traffic use direct Postgres URLs. Django sets the
# isolated control schema through a startup search_path option, which Neon's
# transaction pooler deliberately rejects; migrations also need a direct URL
# for advisory locks and CREATE SCHEMA.
APP_DATABASE_URL="${DATABASE_URL}"
MIGRATE_URL="${MIGRATE_DATABASE_URL:-$DATABASE_URL}"

export DATABASE_URL="$MIGRATE_URL"
python - <<'PY'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "theorem_control.settings")
import django
django.setup()
from django.db import connection
with connection.cursor() as cur:
    cur.execute("CREATE SCHEMA IF NOT EXISTS control")
print("control schema ready", flush=True)
PY

python manage.py migrate --noinput

export DATABASE_URL="$APP_DATABASE_URL"
exec gunicorn theorem_control.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 2
