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
- RunPod Serverless v2 lifecycle client for GPU/Python workloads
- Separate Fly app and Celery queue `offload.r` for R workloads (agent name `"R"`)

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

R queue worker (separate image with pinned R + renv + rpy2):

```bash
celery -A theorem_control worker -l info -Q offload.r
```

The deployment image is `Dockerfile.r`; it performs the same R/rpy2/renv
preflight as the Fly R worker before starting Celery.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Django secret |
| `DEBUG` | Django debug flag |
| `DATABASE_URL` | Direct Neon Postgres URL. Local default: SQLite |
| `VALKEY_URL` / `REDIS_URL` | Authenticated URL for the dedicated `travis-django-theorem-valkey` Fly-private broker/cache. Empty → in-memory cache for tests |
| `ARTIFACT_S3_ENDPOINT_URL` | Neon Object Storage S3-compatible endpoint (Fly secret) |
| `ARTIFACT_S3_ACCESS_KEY_ID` | Neon Object Storage access key (Fly secret) |
| `ARTIFACT_S3_SECRET_ACCESS_KEY` | Neon Object Storage secret key (Fly secret) |
| `ARTIFACT_S3_REGION` | Neon Object Storage region (Fly secret) |
| `ARTIFACT_S3_BUCKET` | Private Neon Object Storage bucket for Arrow artifacts |
| `WORKOS_API_KEY` | WorkOS API (live AuthKit; optional for stubs) |
| `WORKOS_CLIENT_ID` | WorkOS client id |
| `WORKOS_WEBHOOK_SECRET` | HMAC secret for `POST /webhooks/workos` |
| `STRIPE_API_KEY` | Stripe (billing; stub-tolerant) |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhooks |
| `OFFLOAD_EXECUTION_MODE` | `stub` for local/tests; `runpod` for the production Python worker |
| `RUNPOD_API_KEY` | RunPod Serverless API key (Fly secret) |
| `RUNPOD_SERVERLESS_ENDPOINT_ID` | Queue-based endpoint that accepts `theorem.offload.v1` jobs (Fly secret) |
| `RUNPOD_WORKER_IMAGE_DIGEST` | Immutable worker image digest stamped on RunPod provenance |
| `RUNPOD_JOB_TIMEOUT_SECONDS` | Control-plane deadline; timeout cancels the RunPod job |
| `R_OFFLOAD_EXECUTION_MODE` | `stub` for local/tests; `rpy2` only in the R worker image |
| `THEOREM_API_BASE` | Rust API base for provenance write-back |
| `THEOREM_MACHINE_KEY` | Bearer key scoped `provenance:write` |
| `RENV_LOCKFILE_PATH` | Pinned R worker `renv.lock`; SHA-256 becomes provenance `code_ref` |
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
- `POST /internal/offload/invoke` — enqueue a tenant-bound Celery task
- `POST /internal/offload/artifact-upload` — mint a tenant-scoped, short-lived Arrow upload URL
- `GET /internal/offload/{job_id}` — return the caller tenant's job status + ArrowBatch descriptor
- `POST /internal/offload/{job_id}/cancel` — cancel the caller tenant's job
- `/admin/` — ops console (revoke key, re-run job, reset usage, impersonate grant)
- `GET /healthz`

### Offload machine-key admission

Every `/internal/offload/*` request requires `Authorization: Bearer thk_...`.
Keys are minted in the Django admin and belong to exactly one tenant; callers
never submit a `tenant_id`. Grant only the scopes required by the caller:

| Route | Required scope |
| --- | --- |
| `POST /invoke` | `offload:invoke` |
| `GET /{job_id}` | `offload:read` |
| `POST /{job_id}/cancel` | `offload:cancel` |

`offload:*` grants all three offload scopes. Revoked, expired, inactive-tenant,
or unknown keys are refused; job lookups are filtered to the admitted tenant.


## RunPod and R execution contract

The default Python worker submits an asynchronous job to
`POST /v2/{endpoint}/run`, persists the returned RunPod job id in `control_job`,
polls `GET /v2/{endpoint}/status/{job_id}`, and cancels the remote job on the
control-plane deadline or user cancellation. The endpoint must return:

```json
{
  "schema_json": "<Arrow schema JSON>",
  "rows": 42,
  "payload_digest": "sha256:<content-addressed-output>"
}
```

inside its final `output` object. The descriptor—not Arrow bytes—crosses the
control-plane request. Each live input descriptor must name an `artifact_key`
under `tenants/<tenant-id>/`; the worker receives only a short-lived presigned
GET for that input and PUT for the server-selected output key. Django downloads
the output itself and verifies the reported digest, schema, and row count before
marking the job successful. The status response mints a fresh tenant-authorized
`download_url`; no S3 credential is returned to a caller or RunPod.

Implemented operations:

| Operation | Runtime | Input | Output |
| --- | --- | --- | --- |
| `data_science.community.assign` | RunPod Python | Arrow string `source`, `target` edges | Stable `node`, `community_id` connected components |
| `data_science.r.survey_weight` | Fly R/rpy2 | Arrow numeric `value`, non-negative `weight` | One-row `weighted_mean`, `input_rows` table |

The remaining TabFM, GNN, mixed-model, and survival operations return explicit
errors until their operation-specific runners are implemented. They never
manufacture an output descriptor from a digest.

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
