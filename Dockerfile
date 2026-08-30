FROM python:3.12.12-slim-bookworm

ARG DEBIAN_SNAPSHOT_TIMESTAMP=20260801T000000Z
ARG GRAPHVIZ_DEBIAN_VERSION=2.42.2-7+deb12u1
ARG DEFAULT_JRE_DEBIAN_VERSION=2:1.17-74
ARG OPENJDK_DEBIAN_VERSION=17.0.20+8-1~deb12u1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN printf '%s\n' \
      "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT_TIMESTAMP} bookworm main" \
      "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT_TIMESTAMP} bookworm-security main" \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      default-jre-headless="${DEFAULT_JRE_DEBIAN_VERSION}" \
      graphviz="${GRAPHVIZ_DEBIAN_VERSION}" \
      libgraphviz-dev="${GRAPHVIZ_DEBIAN_VERSION}" \
      libpq5 \
      openjdk-17-jre-headless="${OPENJDK_DEBIAN_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

ADD --checksum=sha256:89948f14c93756c7a3fb7b69078ff37e8489fd79dd430c582b931e2f65358690 \
    https://github.com/plantuml/plantuml/releases/download/v1.2026.6/plantuml-1.2026.6.jar \
    /opt/plantuml/plantuml.jar

RUN echo "89948f14c93756c7a3fb7b69078ff37e8489fd79dd430c582b931e2f65358690  /opt/plantuml/plantuml.jar" | sha256sum -c -

COPY requirements.txt .
# Build pygraphviz against the pinned Debian Graphviz rather than accepting a
# wheel that bundles an unrelated Graphviz runtime.
RUN pip install --no-binary=pygraphviz -r requirements.txt

COPY . .

RUN chmod +x /app/scripts/railway-boot.sh
RUN python manage.py collectstatic --noinput || true

ENV PORT=8000
ENV PLANTUML_JAR_PATH=/opt/plantuml/plantuml.jar \
    PLANTUML_VERSION=1.2026.6 \
    PLANTUML_SHA256=89948f14c93756c7a3fb7b69078ff37e8489fd79dd430c582b931e2f65358690 \
    PLANTUML_SECURITY_PROFILE=SANDBOX \
    LAYOUT_SUBPROCESS_TIMEOUT_SECONDS=8 \
    RENDER_SUBPROCESS_TIMEOUT_SECONDS=12 \
    RENDER_MEMORY_BYTES=2147483648 \
    RENDER_OUTPUT_MAX_BYTES=16777216
EXPOSE 8000

# Create control schema (D1), migrate on direct Postgres, serve via PgBouncer.
CMD ["/app/scripts/railway-boot.sh"]
