# Document extraction admission

Django-Theorem owns capture, the eight-stage admission loop, and separate claim
escalation jobs. Theorem owns schema, deterministic typed extraction, resolution,
graph publication, recall, and the Rust Index surface. The spec's old
`apps/notebook` paths are historical references, not another runtime dependency.

## Run the service

Install the normal project dependencies, apply migrations, and run the existing
Django/Celery worker and beat services. Use Python 3.12 for the validated local
environment. Configure secrets outside source control:

```text
THEOREM_SCHEMA_MCP_URL=https://YOUR_AUTHORITY/mcp
THEOREM_SCHEMA_MCP_TOKEN=<authenticated token with the tenant's required permissions>
THEOREM_PADDLE_VLLM_URL=https://YOUR_PADDLE_ENDPOINT/v1
HF_HOME=/path/to/model-cache
NLTK_DATA=/path/to/nltk-data
MLFLOW_TRACKING_URI=sqlite:////path/to/mlflow.db
```

The Paddle endpoint must advertise PaddleOCR-VL-1.6 and be deployed at revision
`c5630abae1d940eafe0697512a0325494b02ab42`. Its model-list response alone cannot
prove that revision. The local PaddleOCR package runs the official layout
pipeline, using the service for recognition. GLiNER2 and MiniCheck adapters pin
their own model revisions; first execution may download model assets.

`EXTRACTION_FRONTIER_BINDINGS` is a JSON array of `name`, `model`, `endpoint`,
`api_key`, and finite nonnegative `batch_price` entries. Set it through private
environment configuration. Only a scheduled ambiguity job uses these bindings;
the lowest price wins, then name. An absent binding fails that job explicitly.

The existing artifact store handles uploads. `EXTRACTION_MAX_INPUT_BYTES`
limits capture size, and `EXTRACTION_SWEEP_INTERVAL_SECONDS` controls queued-job
sweeps. MLflow defaults to `BASE_DIR/var/mlflow.db` when not configured.

## Internal API

Use a tenant machine key with the existing extraction submit/read scopes. No
request-supplied tenant can override the admitted key.

1. `POST /internal/extraction/artifacts` with either `{"text":"..."}` or
   `{"artifact_key":"<uploaded key>","filename":"lease.pdf"}` returns an
   artifact ID and source digest. Optional URL metadata is provenance; the server
   does not fetch an arbitrary supplied URL.
2. `POST /internal/extraction/artifacts/{artifact_id}/extract` with
   `{"object_types":["Lease"]}` queues admission against declared schema.
3. `GET /internal/extraction/runs/{run_id}` returns status, ordered stage history,
   parser receipt, and failure information.
4. `GET /internal/extraction/artifacts/{artifact_id}/read?element_id=...&tier=summary`
   reads the current graph projection. `pointer` and `full` are also explicit
   choices. Summary is the default.

Duplicate capture and scheduling are idempotent. A failed job resumes at its
last completed checkpoint when scheduled again. A job left running after a
worker process is killed needs operational recovery; automatic lease reclamation
is not implemented.

## Evidence and tests

The normalized UTF-8 document is the source of byte offsets. GLiNER character
spans become byte spans before publication. Claims must quote exact element
text; verification uses the pinned MiniCheck implementation and strict
`score > cutoff`. The default cutoff is 0.5. Source message/document timestamps
take precedence over capture time for temporal assertions.

```bash
python manage.py migrate
python manage.py makemigrations --check --dry-run
pytest theorem_ml/extract tests/test_document_admission.py \
  tests/test_document_mlflow.py theorem_ml/executors/test_extraction_registry.py \
  tests/test_extraction.py tests/test_artifacts.py -q
THEOREM_LIVE_MINICHECK=1 pytest theorem_ml/extract/claims/test_claims.py -q
python -m theorem_ml.extract.bench --validate-corpus
```

See [bench/README.md](bench/README.md) for authenticated schema bootstrap,
100-document measurements, external MinerU references, real entity identity gold,
quality thresholds, and executor fitness publication. A complete set of rows is
not a passing result: missing evidence and failed quality both fail acceptance.
