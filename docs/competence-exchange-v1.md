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
`sha256:2b6d4a1f23cd527170259f0c3ba0c0c9cae6ed2c009dead34786acef2344ba88`.

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

## W13 versus W14

W13 accepts valid work into a durable `queued` state but does not fit anything.
The deterministic scorer in the fixture is a contract oracle only. W14 must add
the live fitter and prove known-truth recovery, isolation, model versioning, and
provider artifact behavior before a live result can be claimed.
