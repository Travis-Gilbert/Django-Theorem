# Index executor implementation and evidence

Validated on 2026-09-05 in the isolated Django worktree based on `fbf654c`.
The callable boundary is `POST /internal/index/execute` with the existing
`offload:invoke` machine-key scope. See the
[executor contract](../theorem_ml/executors/README.md) for inputs and outputs.

Discovery uses real cosine kNN and NetworkX Louvain over admitted embedding
rows and graph edges. Names come from extracted terms and entity names.
Corpus filing uses the actual TabICLv2 classifier and returns prototype/prior
updates. Both register as Learned executors and persist execution and fitness
receipts through MLflow. Tenant identity comes from the authenticated principal.

## Pinned model

- [TabICL source](https://github.com/soda-inria/tabicl/tree/0dbff3ec8fc68c123c87af77b0ea8b25cd2d23f3):
  `0dbff3ec8fc68c123c87af77b0ea8b25cd2d23f3`, package version 2.2.0.
- [Official weights](https://huggingface.co/jingang/TabICL/tree/4dcd344ece2c00be9e831fdd35bed57b5ad83e19):
  revision `4dcd344ece2c00be9e831fdd35bed57b5ad83e19`,
  `tabicl-classifier-v2-20260212.ckpt`, 110,368,038 bytes.
- Verified SHA-256:
  `bdc7dbd5e4ff21f8f0456fcf90c6b7cdf72dbea960f2d05b19bec19f9b3d4ed0`.
- The [source license](https://github.com/soda-inria/tabicl/blob/0dbff3ec8fc68c123c87af77b0ea8b25cd2d23f3/LICENSE)
  and [official model card](https://huggingface.co/jingang/TabICL/blob/4dcd344ece2c00be9e831fdd35bed57b5ad83e19/README.md)
  declare BSD-3-Clause.

The executor uses native KV caching through ten classes. Above ten it fits one
cached binary classifier per class and normalizes the resulting one-vs-rest
scores. Each cached context is reused for held-out inference and prototype
construction. This is not numerical-parity evidence for upstream's uncached
many-class strategy. Checkpoint auto-download is disabled in production.

## Reproduced gates

The isolated environment was Python 3.13.12 with Torch 2.14.0, NumPy 2.5.2,
NetworkX 3.6.1, MLflow Skinny 3.16.0 and the pinned TabICL source. The checkpoint
and environment live outside the repository under
`~/.cache/theorem-index-w10/`.

```bash
THEOREM_TABICL_CHECKPOINT="$HOME/.cache/theorem-index-w10/tabicl-classifier-v2-20260212.ckpt" \
THEOREM_INDEX_RUN_TABICL_SMOKE=1 MLFLOW_DISABLE_AGENT_HINT=1 OMP_NUM_THREADS=2 \
"$HOME/.cache/theorem-index-w10/venv/bin/python" -m pytest \
  theorem_ml/executors -k 'discovery or corpus' -q --durations=3
```

Result: **12 passed, 1 skipped in 26.16 seconds**. The one skip is the canonical
784-item gate. Local MLflow file storage is explicitly enabled inside tests.
The complete test output is retained at
`~/.cache/theorem-index-w10/executors-tests.log`.

| Real checkpoint test | Context / held-out rows | Accuracy | MLflow run ID | Test duration |
| --- | --- | --- | --- | --- |
| Two classes, native cache | 300 / 8 | 1.0 | `b8fc90918457425f8fe25a0f0ba1c67f` | 3.40 s |
| Eleven classes, cached one-vs-rest | 330 / 44 | 1.0 | `29b5a1f2869e4b7cb151287a66933af2` | 21.03 s |

These inputs are explicitly synthetic separated numeric classes. The results
prove actual checkpoint loading, cached inference, prototype construction and
MLflow recording; they do not measure filing quality on real user documents.
The separately named `RecordingBinaryEstimator` test checks wrapper call
topology only and is not model-quality evidence.

The combined gate also exercises real discovery and MLflow persistence through
the authenticated HTTP endpoint, including missing credentials, insufficient
scope and foreign-tenant rejection. It covers graph/entity clustering,
disjoint held-out labels, failed-run receipts and feature-layout validation.

Additional reproduced checks:

- `pytest tests/test_offload_api.py tests/test_layout_service.py tests/test_competence_api.py -q`:
  **49 passed**, with 17 existing Pydantic deprecation warnings.
- `python manage.py check`: no issues.
- `ruff check theorem_ml theorem_control/urls.py` and `git diff --check`: passed.
- `uv pip install --no-deps --python "$HOME/.cache/theorem-index-w10/venv/bin/python" '.[index-corpus]'`:
  built and installed the package. An import from `/tmp` verified that the
  installed wheel contains both executors. Explicit setuptools package discovery
  fixes the pre-existing flat-layout build failure exposed by the new extras.

## Outstanding end-to-end evidence

The canonical labeled 784-item corpus was absent from the inspected Theorem and
Django checkouts and targeted Downloads search. The env-gated test requires
`THEOREM_INDEX_784_FIXTURE` and `THEOREM_INDEX_T1_ORACLE`, along with the real
checkpoint and MLflow tracking URI. That gate has **not run**. Its contract
requires accepted discovery proposals, the measured Tier-1 accuracy floor and
one Rust prototype-generation advance.

At the initial Django commit, the Rust ingest embedding pipeline, proposal
persistence and user acceptance, and atomic prototype application were not
connected to this endpoint. Companion bridge implementation is now present in
the Rust worktree, with separate Rust gates and deployment proof. The executor
still refuses missing embeddings rather than manufacturing them. No hosted
service deployment, mounted VFS behavior, or real-corpus accuracy is established
by these checks.

## Cold-start contract follow-up

The Rust bridge exposed a legitimate cold-start condition: accepted context
labels exist but independent held-out truth does not. Empty `held_out` now
performs the actual corpus pass while returning `fitness: null`; MLflow records
`not_measured_no_held_out_filings` and no accuracy metric.

The same combined command above was rerun after this change: **14 passed,
1 skipped in 84.22 seconds**. Actual two-class and eleven-class models each ran
both with and without held-out labels. The no-held-out cases verify real
prototype output and actual unmeasured MLflow tags, not a classifier substitute.
The canonical 784-item gate remains the sole skip. Output is retained in
`~/.cache/theorem-index-w10/executors-resume-tests.log`.
