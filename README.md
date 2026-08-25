# Django-Theorem

**The control plane and data science lab for [Theorem](https://github.com/Travis-Gilbert), an agent harness built on a Rust graph substrate.**

This is the Django service that handles the business half of running an intelligent system as a product: multi-tenancy, identity, billing, machine API keys, feature flags — and a compute-offload orchestrator that ships tenant-bound data science jobs (Python on RunPod GPUs, R on a dedicated Fly worker) with content-addressed provenance on every result.

Design decisions worth noting:

- **Fail-closed tenancy.** Callers never submit a `tenant_id`; identity derives from the machine key alone, jobs are filtered to the admitted tenant, and reusing an idempotency key with a changed payload is refused.
- **Descriptors cross the wire, not data.** GPU workers exchange Arrow artifacts through short-lived presigned URLs scoped under `tenants/<id>/`; Django independently downloads and verifies digest, schema, and row count before a job may succeed. No S3 credential ever reaches a caller or worker.
- **Provenance is not optional.** Worker image digests, R lockfile hashes, and scoring snapshots are stamped onto results so any output can be traced to the exact code that produced it.
- **Contract-tested boundary with Rust.** The Rust read-model's view of the control schema is enforced by a cargo contract test wired to run on every Django migration change, so column drift fails CI before clients break.

The rest of this README is the operator's reference for the service itself.

---

Django service for Theorem business/fleet management: tenancy, identity
(WorkOS shadow rows), billing, machine API keys, offload orchestration,
feature flags, and support notes.

It also owns the authenticated `theorem.competence.v1` fit/refit job boundary.
The competence worker fits selection-corrected Beta-Bernoulli scorers, publishes
content-addressed prior/model artifacts, and exposes inspectable recovery state.

## Stack

- Django 5.x + django-ninja
- Celery (broker: Valkey/Redis)
- argon2-cffi (machine key hashes)
- psycopg (Postgres via PgBouncer)
- RunPod Serverless v2 lifecycle client for GPU/Python workloads
- Separate Fly app and Celery queue `offload.r` for R workloads (agent name `"R"`)
- Graphviz 2.42.2/PyGraphviz positions plus bounded PlantUML and Diagrams rendering

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

Celery beat (competence sleep-cycle recovery/refit dispatch):

```bash
celery -A theorem_control beat -l info
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
| `COMPETENCE_STALE_AFTER_SECONDS` | Age after which a running competence job is recoverable (default 900) |
| `COMPETENCE_SWEEP_BATCH_SIZE` | Maximum queued competence jobs dispatched per sleep cycle (default 100) |
| `COMPETENCE_SWEEP_INTERVAL_SECONDS` | Celery beat interval for competence recovery (default 3600) |
| `LAYOUT_CACHE_TTL_SECONDS` | Tenant-scoped canonical layout response TTL (default 86400) |
| `LAYOUT_MEMORY_CACHE_MAX_ENTRIES` | Bounded per-process fallback cache entry count (default 1024) |
| `LAYOUT_SUBPROCESS_TIMEOUT_SECONDS` | Hard Graphviz worker deadline (default 8 seconds) |
| `LAYOUT_MAX_OUTPUT_BYTES` | Maximum isolated Graphviz response bytes (default 4 MiB) |
| `RENDER_SUBPROCESS_TIMEOUT_SECONDS` | PlantUML/Diagrams worker deadline (default 12 seconds) |
| `RENDER_CPU_SECONDS` / `RENDER_MEMORY_BYTES` | Linux renderer CPU/address-space limits |
| `RENDER_MAX_SOURCE_BYTES` / `RENDER_OUTPUT_MAX_BYTES` | Renderer input/output bounds |
| `PLANTUML_JAR_PATH` / `PLANTUML_VERSION` / `PLANTUML_SECURITY_PROFILE` | Checksum-pinned PlantUML runtime identity and mandatory `SANDBOX` profile |

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
- `POST /internal/competence/fit` — submit a tenant/project-bound competence fit
- `POST /internal/competence/refit` — submit a refit bound to a previous scorer
- `GET /internal/competence/jobs/{job_id}` — inspect fit/refit state and artifacts
- `POST /internal/competence/jobs/{job_id}/cleanup` — remove exact owned artifacts
- `POST /internal/layout/compute` — return deterministic Graphviz center positions
- `POST /internal/rendering/plantuml` — render SANDBOX PlantUML to tenant-owned SVG
- `POST /internal/rendering/diagrams` — render restricted Diagrams source to tenant-owned PNG/SVG
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

### Competence machine-key admission

The competence routes use `competence:fit`, `competence:read`, and
`competence:cleanup`; `competence:*` grants all three. Each request carries the
tenant UUID as part of its content-addressed scope, but that value never selects
authority: it must equal the tenant derived from the verified machine key.

`theorem.competence.v1` carries lineage-de-duplicated sufficient evidence and
content-addressed scorer/prior references, not raw graph state or authority.
Exact operation retries reuse one job. Reusing the key with a changed payload
fails closed. Cleanup is restricted to the artifacts persisted for the exact
tenant, project, candidate, and job.

The worker uses training survival to establish a Beta prior and W02-corrected
held-out outcomes for the posterior. Fit/refit output is immutable: exact retry
reuses the job, refit binds the prior scorer and rejects reused lineage, and a
status cannot become `succeeded` until both artifacts pass readback.

The fixture at `contracts/theorem.competence.v1.fixture.json` is explicitly a
deterministic wire fixture. The W14 known-truth suite exercises the real fitter
with a byte-preserving local storage double; neither proves hosted object
storage or deployment behavior.

The complete wire, worker/Beat, evidence-class, and cleanup contract is in
[`docs/competence-exchange-v1.md`](docs/competence-exchange-v1.md). Run the
hosted boundary oracle inside the Fly web machine so credentials remain private:

```bash
python manage.py competence_live_smoke \
  --base-url https://travis-django-theorem-personal.fly.dev \
  --timeout-seconds 120 \
  --confirm-live-cleanup
```

This proves the public deployed boundary and real artifact readback/cleanup.
Its disposable scorer evidence remains a deterministic fixture and is not
promotion evidence.

### Graph layout and rendering

`/internal/layout/compute` requires `layout:compute` (or `layout:*`). Callers
send stable node/edge IDs and measured pixel sizes; Django selects a policy row,
serializes sorted DOT, executes PyGraphviz in a deadline-bound subprocess, and
returns center coordinates with the exact Graphviz version, effective policy,
and an input digest. Canonical response bytes are cached under a tenant-scoped
Valkey key. The service never mutates graph content.

The two `/internal/rendering/*` routes require `rendering:render` (or
`rendering:*`). PlantUML is forced to SVG under its SANDBOX security profile.
Diagrams source is statically refused if any import names a package outside
`diagrams`; the isolated interpreter repeats that import check at runtime. This
is capability admission plus resource hygiene, not a Python security sandbox.
Both renderers run synchronously in temporary working directories and isolated
process groups with a credential-free child environment, deadlines, and
pre-buffer file-size caps; the Linux deployment additionally applies CPU and
address-space rlimits. Same-container execution is still not an isolation
boundary for adversarial Python; deployments admitting untrusted Diagrams
source must move that worker to a credential-free sandbox or sidecar.

Rendered bytes are read back after upload and published as
`tenants/<tenant-id>/renders/<sha256>.<ext>` with a short-lived presigned GET.
The Docker image pins Debian Graphviz 2.42.2, compiles PyGraphviz 2.0.1 against
that library, installs Diagrams 0.25.1 and OpenJDK 17, and checksum-verifies the
PlantUML 1.2026.6 jar. `contracts/theorem.layout.v1.fixture.json` is a strict
wire fixture; its named coordinates are not a substitute for the native
container or hosted/authenticated oracles.

Run the deployed smoke with a machine key carrying both `layout:compute` and
`rendering:render`. The test downloads each signed artifact and verifies its
digest; keep the key in the environment, never in the repository:

```bash
THEOREM_LAYOUT_RENDERING_LIVE_BASE_URL=https://control.example \
THEOREM_LAYOUT_RENDERING_LIVE_MACHINE_KEY=thk_... \
pytest -q -m live tests/test_layout_rendering_live.py
```

Two additional live gates are opt-in: set `THEOREM_LAYOUT_LIVE_VALKEY_URL`
to prove real cache bytes equal a cold recompute, and set
`THEOREM_CHAT_PLAN_LAYOUT_FIXTURE` to the uncommitted real 31-node/44-edge
board JSON while supplying the deployed URL/key above. Neither gate accepts the
two-node wire fixture as a substitute.

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
