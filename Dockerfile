FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x /app/scripts/railway-boot.sh
RUN python manage.py collectstatic --noinput || true

ENV PORT=8000
EXPOSE 8000

# Create control schema (D1), migrate on direct Postgres, serve via PgBouncer.
CMD ["/app/scripts/railway-boot.sh"]
