# Django-Theorem

**The control plane and data science lab for [Theorem](https://github.com/Travis-Gilbert), an agent harness built on a Rust graph substrate.**

Theorem is a rust, rust is the runtime. Django-theorem is how we improve the runtime.

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
- Debian Graphviz 2.42.2-7+deb12u1 (linked runtime 2.43.0)/PyGraphviz positions plus bounded PlantUML and Diagrams rendering

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
| `RUNPOD_EXTRACTION_ENDPOINT_ID` | Dedicated queue endpoint for the pinned extraction worker image |
| `RUNPOD_EXTRACTION_IMAGE_DIGEST` | Immutable extraction image digest stamped on extraction provenance |
| `RUNPOD_JOB_TIMEOUT_SECONDS` | Control-plane deadline; timeout cancels the RunPod job |
| `R_OFFLOAD_EXECUTION_MODE` | `stub` for local/tests; `rpy2` only in the R worker image |
| `THEOREM_API_BASE` | Rust API base for provenance write-back |
| `THEOREM_MACHINE_KEY` | Bearer key scoped `provenance:write` |
| `THEOREM_MACHINE_KEY_PASSAGES` | Bearer key scoped `passages:read` for web and life-email source planning |
| `EXTRACTION_MAX_INPUT_BYTES` | Encoded Arrow shard ceiling; default `ARTIFACT_MAX_BYTES // 4` |
| `EXTRACTION_SWEEP_INTERVAL_SECONDS` | Extraction reconciliation beat interval (default 300) |
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
| `RENDER_CPU_SECONDS` / `RENDER_MEMORY_BYTES` | Linux renderer CPU/address-space limits (defaults: 10 seconds / 2 GiB) |
| `RENDER_MAX_SOURCE_BYTES` / `RENDER_OUTPUT_MAX_BYTES` | Renderer input/output bounds |
| `PLANTUML_JAR_PATH` / `PLANTUML_VERSION` / `PLANTUML_SECURITY_PROFILE` | Checksum-pinned PlantUML runtime identity and mandatory `SANDBOX` profile |

## Postgres schemas / roles

See `sql/roles.sql`:

- `theorem_control` owns schema `control`
- `theorem_spine` owns schema `spine`, with column-limited `SELECT` on D3 read models

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
| `control_extractionjob` | `id`, `tenant_id`, `operation`, `contract_version`, `source_kind`, `source_ref`, `params`, `params_hash`, `status`, `shard_count`, `rows_total`, `error`, `created_at`, `updated_at` |
| `control_extractionreview` | `id`, `tenant_id`, `job_id`, `candidate_digest`, `candidate_digest_version`, `claim_id`, `decision`, `merge_target_claim_id`, `reason`, `reviewer`, `created_at` |

## HTTP surface

- `POST /webhooks/workos` — WorkOS events (signature required)
- `POST /internal/offload/invoke` — enqueue a tenant-bound Celery task
- `POST /internal/offload/artifact-upload` — mint a tenant-scoped, short-lived Arrow upload URL
- `GET /internal/offload/{job_id}` — return the caller tenant's job status + ArrowBatch descriptor
- `POST /internal/offload/{job_id}/cancel` — cancel the caller tenant's job
- `POST /internal/extraction/submit` — plan and fan out a tenant corpus extraction
- `GET /internal/extraction/{job_id}` — return extraction and per-shard Arrow descriptors
- `POST /internal/extraction/{job_id}/cancel` — cancel every non-terminal shard
- `POST /internal/extraction/review` — append candidate review decisions
- `GET /internal/extraction/review?since=<iso>` — stream decisions for non-spine callers
- `POST /internal/competence/fit` — submit a tenant/project-bound competence fit
- `POST /internal/competence/refit` — submit a refit bound to a previous scorer
- `GET /internal/competence/jobs/{job_id}` — inspect fit/refit state and artifacts
- `POST /internal/competence/jobs/{job_id}/cleanup` — remove exact owned artifacts
- `POST /internal/layout/compute` — return deterministic Graphviz center positions
- `POST /internal/rendering/plantuml` — render SANDBOX PlantUML to tenant-owned SVG
- `POST /internal/rendering/diagrams` — render restricted Diagrams source to tenant-owned PNG/SVG
- `POST /internal/rendering/descriptor` — refresh a short-lived descriptor for one exact tenant-owned render key
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

### Extraction machine-key admission

Extraction routes use `extraction:submit`, `extraction:read`, and
`extraction:review`; `extraction:*` grants all three. Tenant identity always
comes from the verified machine key. Any nested `tenant` or `tenant_id` in a
request is treated only as an equality assertion and is refused on mismatch.

The parent ledger stores job and shard state, while candidate rows remain in
verified Arrow artifacts. Review decisions are append-only Postgres rows; a
newer decision for the same candidate digest supersedes an older decision.
Rust reads those rows through the column-limited `theorem_spine` role and owns
graph admission.

The committed extraction fixture is a deterministic stub replay and is not a
live GPU receipt. After deploying the dedicated extraction endpoint, run the
hosted boundary oracle with a disposable machine key carrying
`offload:invoke`, `extraction:submit`, and `extraction:read`:

```bash
THEOREM_EXTRACTION_LIVE_MACHINE_KEY=thk_... \
python manage.py extraction_live_smoke \
  --base-url https://travis-django-theorem-personal.fly.dev \
  --timeout-seconds 900
```

Exit zero means the fixture input was uploaded through a presigned capability,
the deployed extraction job succeeded, and every returned shard was downloaded
and independently verified for digest, Arrow schema, and row count. It does
not imply that the still-uncommitted Rust companion contract is byte-identical.

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

All three `/internal/rendering/*` routes require `rendering:render` (or
`rendering:*`). The descriptor route derives the tenant from that admitted key,
verifies the exact content-addressed object and digest, and presigns a fresh GET;
it does not accept a payload tenant or mint a URL before storage readback.
PlantUML is forced to SVG under its SANDBOX security profile.
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
The Docker image binds APT to snapshot `20260801T000000Z`, pins Debian Graphviz
2.42.2-7+deb12u1 (which reports linked runtime 2.43.0), compiles PyGraphviz 2.0.1
against that library, installs Diagrams 0.25.1 and OpenJDK 17, and
checksum-verifies the PlantUML 1.2026.6 jar.
`contracts/theorem.layout.v1.fixture.json` is a strict wire fixture; its named
coordinates are not a substitute for the native container or
hosted/authenticated oracles.

Evidence is intentionally split by boundary:

| Boundary | Current evidence | Not implied |
| --- | --- | --- |
| Fixture and source projection | Exact `theorem.layout.v1` fixture plus reproducible generation-15 31-node/44-edge topology | No Graphviz execution |
| Local process | Native Graphviz 14.1.5 exact-board layout and real Valkey cold/warm/version-key replay | No declared-image or hosted cache proof |
| Production adapter, local endpoint | `ArtifactStore.from_settings()` through checksum-bound disposable MinIO, signed readback/expiry, and cross-tenant refusal | No hosted object-storage proof |
| Native renderer | Checksum/version-bound PlantUML 1.2026.6 and Diagrams 0.25.1 through native Graphviz 14.1.5 | No declared-image Debian Graphviz 2.42.2-7+deb12u1 / linked runtime 2.43.0 proof |
| Declared image | Fly image `graph-layout-v03-20260827-r3` at digest `sha256:f103d8c52ddfb7ab11a8432f533eb5fc3031f128c04b58d266cf5f5379a8b47d`; exact checked-in probe on auto-removed Machine `28654915a91208` | Passed: pinned identities, real renderer smokes, Django checks, and no-skip cold fixture; not hosted/authenticated product proof |
| Hosted/authenticated | Env-gated tests committed | Not run without deployment URLs, credentials, hosted Valkey, and hosted object storage |

Replay the native rendering boundary without installing host packages:

```bash
TMPDIR=/tmp .venv/bin/python scripts/run-rendering-native-oracles.py
```

Exit `0` is the only passing native-renderer receipt. Exit `1` means an
identity, checksum, render, refusal, bound, or cleanup oracle failed; it is not
a prerequisite skip.

The helper downloads PlantUML 1.2026.6 and the official Graphviz 14.1.5 release
into a bounded temporary directory, verifies both reviewed SHA-256 values,
builds Graphviz under that temporary prefix, and calls the production PlantUML
and isolated Diagrams functions. It requires real SVG and PNG bytes,
digest-derived tenant render keys, SANDBOX local-include refusal, a refusal that
names a forbidden Diagrams import, and complete temporary-state cleanup. This
is native renderer evidence only: Graphviz 14.1.5 does not substitute for the
declared image's Debian Graphviz 2.42.2-7+deb12u1 package and linked runtime 2.43.0.

Run the declared-image preflight and gate with:

```bash
.venv/bin/python scripts/run-rendering-container-oracles.py
```

The command inventories Docker, Podman, Colima/Lima, Finch, nerdctl, Apple
`container`, and OrbStack without starting or installing a service. With an
already-usable image client and at least 20 GiB free, it builds `Dockerfile`,
checks exact Graphviz, PyGraphviz, Diagrams, Java, and PlantUML identities,
runs both production renderers, `manage.py check`, and the cold layout fixture,
then removes its helper-owned image tag. Missing runtime or disk capacity exits
with prerequisite status 2; host Graphviz is never credited as declared-image
evidence. Hosted/authenticated proof remains the separate live test below.
Exit `0` alone proves the declared image. Exit `2` means a named host
prerequisite is absent and keeps the gate open. Exit `1` means the declared
recipe or an executed image probe violated its oracle.

Run the deployed smoke with a machine key carrying both `layout:compute` and
`rendering:render`. The test downloads each signed artifact and verifies its
digest; keep the key in the environment, never in the repository:

```bash
THEOREM_LAYOUT_RENDERING_LIVE_BASE_URL=https://control.example \
THEOREM_LAYOUT_RENDERING_LIVE_MACHINE_KEY=thk_... \
.venv/bin/pytest -q -m live tests/test_layout_rendering_live.py
```

`fly.layout-test.toml` is the production-isolated hosted layout oracle used by
TheoremWeb staging. It deploys the same image with one web process, a dedicated
SQLite volume, and no production Postgres, Valkey, RunPod, or object-storage
credentials. Machine keys are minted inside that app for the staging tenant and
are stored only in the consuming Fly app's secrets.

The exact 31-node/44-edge Agent Chat board is committed as
`contracts/theorem.layout.v1.agent-chat-plan.fixture.json` and is the default
deployed board input. `THEOREM_CHAT_PLAN_LAYOUT_FIXTURE` may override that path
for an explicitly versioned successor. Set `THEOREM_LAYOUT_LIVE_VALKEY_URL` to
run the separate deployed Valkey cache oracle. Neither gate accepts the
two-node wire fixture as a substitute.

To run every hosted row in one invocation, provide all three variables:

```bash
THEOREM_LAYOUT_RENDERING_LIVE_BASE_URL=https://control.example \
THEOREM_LAYOUT_RENDERING_LIVE_MACHINE_KEY=thk_... \
THEOREM_LAYOUT_LIVE_VALKEY_URL=redis://hosted-valkey.example:6379/0 \
.venv/bin/pytest -q -m live tests/test_layout_rendering_live.py
```

Pytest exit `0` is credit only for rows that actually executed. A zero exit
with skipped rows is not a hosted receipt; inspect the summary and require zero
skips for the intended set.

Run the complete bounded local replay with:

```bash
.venv/bin/python scripts/run-layout-local-oracles.py
```

Exit `0` means the helper-owned receipt contained exactly four expected tests,
four passes, and zero skips/failures/errors, followed by bounded process/port/
temporary-state cleanup. Exit `1` means a prerequisite or oracle failed. Exit
`130` means interruption completed its cleanup; neither nonzero status is a
pass.

Before downloading MinIO, the replay resolves `THEOREM_TEST_VALKEY_SERVER` or
`valkey-server` on `PATH`, requires its real path to be executable, and verifies
the `Valkey server v=<version>` identity. The exact resolved binary is then
started on a dynamically selected loopback port with a temporary data
directory; a missing or invalid Valkey installation is fatal rather than a
pytest skip. The helper downloads the immutable official Darwin/ARM64 MinIO
archive `minio.RELEASE.2025-09-07T16-13-09Z` into a separate temporary directory
and checks the reviewed SHA-256 before making it executable. MinIO receives
fresh credentials, data, config, API port, and console port for that invocation.
The helper proves the binary and S3 server identity, then invokes exactly the
four required local-oracle test node IDs in a separate allowlisted environment.
Inherited pytest controls, plugins, credentials, hosted endpoints, MinIO
controls, and proxy settings are not passed to either child; loopback
`NO_PROXY` is explicit. A helper-owned JUnit receipt must identify all four
tests with four passes, zero skips, zero failures, and zero errors before the
success line is emitted. Finally, the helper requires the processes, ports, and
temporary state to be gone. It accepts no endpoint argument, so moto or an
operator-selected loopback service cannot receive real-process credit.

The direct pytest module remains useful when an operator already owns a local
S3-compatible process:

```bash
.venv/bin/pytest -q tests/test_layout_local_oracles.py
```

Its artifact-store case requires all `THEOREM_LAYOUT_LOCAL_S3_*` variables and
refuses non-loopback endpoints. That operator-hosted path is separate from the
pinned disposable-MinIO replay above.

Reproduce the Agent Chat fixture from the immutable Theorem Plan object with:

```bash
.venv/bin/python scripts/check-agent-chat-layout-fixture.py \
  --theorem-repo '/path/to/Theorem'
```

The checker reads generation 15 with `git show` at commit
`f125a04118fce2e7a971b89d663d24a4cf2caa43`, assigns edge IDs in source
task/dependency order, classifies an edge as `verifies` only when the source
names the target as its verification sibling, applies the documented W/V/plan
node sizes, and then sorts nodes and edges for the wire fixture. The
source-topology receipt is SHA-256 over UTF-8 JSON containing sorted node IDs
and sorted `{from,to}` pairs, encoded with sorted object keys and separators
`,` and `:`. The request receipt uses the same canonical JSON encoding over the
complete generated `layout_request`.

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
| `data_science.extraction.atlas` | Dedicated RunPod extraction image | Arrow `passage_id`, `text`, nullable `metadata_json` | `theorem.extraction.v1` entity, event, relation, and concept candidates |
| `data_science.extraction.typed` | Dedicated RunPod extraction image | Passage Arrow plus `params.object_type` | `theorem.extraction.v1` typed record candidates |
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
