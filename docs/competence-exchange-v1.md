# theorem.competence.v1

`theorem.competence.v1` is the versioned exchange between Theorem's existing
practice/promotion owners and Django-Theorem's competence job service.

## Message family

| Message | Purpose |
| --- | --- |
| `fit_request` | Submit de-duplicated sufficient evidence for a new scorer. |
| `refit_request` | Submit evidence bound to an existing scorer/model/prior. |
| `submission_receipt` | Bind a request digest to one inspectable job. |
| `job_status` | Report queued/running/terminal state and successful artifacts. |
| `refusal` | Return a stable reason code without inventing a result. |
| `cleanup_request` | Name the exact job scope and owned content addresses to remove. |
| `cleanup_receipt` | Audit an exact, idempotent cleanup attempt. |

The canonical fixture is
[`contracts/theorem.competence.v1.fixture.json`](../contracts/theorem.competence.v1.fixture.json).
Both Rust and Python parse it with unknown fields denied.
Its canonical cross-repository digest is
`sha256:20b9ab00f143c5a26fc62b0a0016c19177abf1e6f152c91ebb6bb41a3903474c`.

## Authority

Authentication derives one tenant from the machine key. `scope.tenant_id` is a
digest binding and must equal that tenant; it cannot substitute another tenant.
The project must belong to the admitted tenant. Status and cleanup queries are
filtered by the same derived tenant.

## Data boundary

Requests contain one summary per causal lineage: episode/source content
addresses, survival, and W02's already validated off-policy sufficient
statistics. Raw graphs, episode snapshots, prompts, source data, presigned
storage credentials, and promotion authority do not cross this boundary.

Model and prior outputs are content-addressed descriptors. Storage keys remain
server-side so cleanup can enforce the exact tenant/project/candidate prefix.

## Fit and refit execution

The API commits a durable `queued` job before dispatching the Celery task. The
default worker fits a Beta-Bernoulli scorer from sufficient evidence only:
training survival initializes the prior and held-out outcomes update the
posterior with W02 importance weights. Refit starts from the exact previous
posterior, requires the same package/candidate scope, and refuses any causal
lineage already consumed by that scorer.

Prior-pack and scorer-model JSON bytes are canonicalized, hashed, written under
the exact tenant/project/candidate prefix, and read back before the database can
publish `succeeded`. A periodic `sweep_competence_jobs` task re-dispatches queued
work and recovers abandoned `running` leases. It does not reinterpret failed or
refused work as complete.

The deterministic scorer in the shared fixture remains a W13 contract oracle.
W14 known-truth tests exercise the real fitter and a byte-preserving local
object-store double; they do not claim hosted Neon S3 or deployed Celery proof.
Those stronger evidence classes remain explicit deployment gates.

Run the provider artifact oracle with the five `ARTIFACT_S3_*` credentials in
the environment:

```bash
python -m pytest -q tests/test_competence_live.py
```

Without those credentials the test is skipped and must not be reported as live
storage evidence.

## Deployment topology and hosted oracle

The Fly application runs three process groups from the same image:

- `web` applies migrations and serves the authenticated HTTPS API;
- `worker` consumes the default Celery queue and executes competence fits; and
- `beat` schedules `sweep_competence_jobs`, which redispatches durable queued
  jobs and recovers expired `running` leases.

The hosted acceptance command must run inside the deployed environment so the
object-storage credentials never leave Fly:

```bash
python manage.py competence_live_smoke \
  --base-url https://travis-django-theorem-personal.fly.dev \
  --timeout-seconds 120 \
  --confirm-live-cleanup
```

It creates a disposable tenant, project, and machine key; calls the public
HTTPS fit, status, and cleanup routes; reads both artifacts directly from the
configured S3-compatible store; verifies byte length and SHA-256; replays
cleanup; confirms both objects are unreadable; and removes the disposable
database tenant. The confirmation flag is mandatory because the command
deletes live test state.

The command's boundary receipt has `evidence_class=live`: it proves deployed
admission, Celery execution, object-store readback, and cleanup behavior. Its
fit input is deliberately synthetic and is reported separately as
`promotion_evidence_class=deterministic_fixture`. It must not be cited as
real-world competence or promotion evidence.
