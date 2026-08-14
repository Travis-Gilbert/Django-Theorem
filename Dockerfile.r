# A separate R image keeps rpy2, R, and renv out of the web/Python worker.
# The R base is pinned; renv.lock pins the R package set and its SHA is written
# to every live R activity as code_ref.
FROM rocker/r-ver:4.4.3

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      libpq-dev \
      python3 \
      python3-dev \
      python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

COPY requirements.txt requirements-r.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-r.txt

COPY renv.lock ./renv.lock
RUN R --vanilla --slave -e 'install.packages("renv", repos = "https://cloud.r-project.org")' \
    && R --vanilla --slave -e 'renv::restore(lockfile = "/app/renv.lock", prompt = FALSE)'

COPY . .

CMD ["sh", "-c", "python -m apps.orchestration.r_runtime --check && celery -A theorem_control worker -l info -Q offload.r"]
