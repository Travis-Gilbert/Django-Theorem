---
Theorem Control Plane And Data Science Lab
---
Django service for Theorem business/fleet management: tenancy, identity
(WorkOS shadow rows), billing, machine API keys, offload orchestration,
feature flags, and support notes.

## Stack

- Django 5.x + django-ninja
- Celery (broker: Valkey/Redis)
- argon2-cffi (machine key hashes)
- psycopg (Postgres via PgBouncer)
- Separate Celery queue `offload.r` for R workloads (agent name `"R"`)

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8001
```

Celery worker (default queue):

```bash
celery -A theorem_control worker -l info
```

R queue worker (separate image with R + renv pinned):

```bash
celery -A theorem_control worker -l info -Q offload.r
```

## Environment variables

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Django secret |
| `DEBUG` | Django debug flag |
| `DATABASE_URL` | Postgres URL (PgBouncer). Local default: SQLite |
| `VALKEY_URL` / `REDIS_URL` | Celery broker + org/membership cache + key-revocation publish. Empty → in-memory cache for tests |
| `WORKOS_API_KEY` | WorkOS API (live AuthKit; optional for stubs) |
| `WORKOS_CLIENT_ID` | WorkOS client id |
| `WORKOS_WEBHOOK_SECRET` | HMAC secret for `POST /webhooks/workos` |
| `STRIPE_API_KEY` | Stripe (billing; stub-tolerant) |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhooks |
| `RUNPOD_API_KEY` | RunPod GPU dispatch from Celery (stub when unset) |
| `THEOREM_API_BASE` | Rust API base for provenance write-back |
| `THEOREM_MACHINE_KEY` | Bearer key scoped `provenance:write` |
| `RENV_LOCKFILE_HASH` | R worker lockfile hash stamped on provenance `code_ref` |
| `DISABLE_SERVER_SIDE_CURSORS` | Must be `True` under PgBouncer transaction pooling |
| `CONN_MAX_AGE` | Must be `0` under PgBouncer |
| `CELERY_TASK_ALWAYS_EAGER` | Run tasks inline (tests / local) |

## Postgres schemas / roles

See `sql/roles.sql`:

- `theorem_control` owns schema `control`
- `theorem_spine` owns schema `spine`, `SELECT` only on the five D3 tables

Django sets `search_path=control,public`. Settings force
`DISABLE_SERVER_SIDE_CURSORS=True` and `CONN_MAX_AGE=0`.

### D3 tables Rust may SELECT

| Table | Columns |
| --- | --- |
| `control_tenant` | `id`, `slug`, `display_name`, `is_active` |
| `control_project` | `tenant_id`, `slug`, `display_name` |
| `control_subscription` | `tenant_id`, `plan_code`, `status` |
| `control_plan` | `code`, `limits` |
| `control_apikey` | `id`, `tenant_id`, `key_hash`, `scopes`, `revoked_at`, `expires_at` |

## HTTP surface

- `POST /webhooks/workos` — WorkOS events (signature required)
- `POST /internal/offload/invoke` — enqueue Celery task, return job id
- `GET /internal/offload/{job_id}` — status + ArrowBatch descriptor
- `POST /internal/offload/{job_id}/cancel`
- `/admin/` — ops console (revoke key, re-run job, reset usage, impersonate grant)
- `GET /healthz`

## Tests

```bash
source .venv/bin/activate
pip install pytest pytest-django
CELERY_TASK_ALWAYS_EAGER=1 pytest -q
# or
CELERY_TASK_ALWAYS_EAGER=1 python manage.py test tests
```

`tests/test_schema_roles.py` skips unless `DATABASE_URL` points at Postgres.
When roles from `sql/roles.sql` are applied, it also asserts GRANT/REVOKE live
permission errors (A1).

### A3 — control schema contract (CI)

After `migrate` and applying `sql/roles.sql` on a scratch Postgres, from a
pinned Theorem checkout run:

```bash
export CONTROL_DATABASE_URL='postgres://theorem_spine:...@localhost:5432/theorem'
cargo test -p rustyred-thg-catalog --lib control_schema_contract_holds -- --ignored
```

Wire this into CI on every Django migration change so column drift fails the
contract before Rust read-model clients break.

## Spec

Implements the Django half of
`SPEC-THEOREM-CONTROL-PLANE-1.0` (Theorem `docs/plans/control-plane/`).
