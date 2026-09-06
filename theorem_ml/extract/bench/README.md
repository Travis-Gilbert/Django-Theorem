# Extraction evaluation

`corpus/manifest.json` names 100 authored fixtures: ten each of native PDF,
scans, warped page images, multi-column pages, merged-cell tables, emails,
DOCX, PPTX, Python source and HTML. Gold JSON is authored from the source
content; it is not parser or model output. These fixtures establish a repeatable
test corpus, not representative production accuracy.

Every source, comparison PDF, page image and gold file has a SHA-256 in the
manifest. Office/email/code/HTML page illustrations use an explicitly recorded
parallel authored layout, not a claim of rendering those containers faithfully.
Native conversion is measured separately as `parse.native`.

`docx-01` is the explicit exception: its DOCX contains both a row merge and a
column merge and its PDF was actually rendered by LibreOffice 26.8.0.3.
`docx-01.render.json` binds the source, PDF and PDFium page image by digest.
The native Docling DOCX parser is checked directly against authored text/table
gold. The remaining Office illustrations retain their parallel-layout label.

```bash
python -m theorem_ml.extract.bench --validate-corpus
uv run --no-project --with reportlab --with python-docx --with python-pptx \
  --with pypdfium2 python theorem_ml/extract/bench/generate_corpus.py \
  --soffice /Applications/LibreOffice.app/Contents/MacOS/soffice
python -m pytest theorem_ml/extract/parse/test_docx_render_fixture.py
THEOREM_EXTRACTION_LIVE_DOCLING=1 python -m pytest \
  theorem_ml/extract/parse/test_docx_render_fixture.py
```

The full regeneration command requires a real LibreOffice executable and refuses
to overwrite an existing renderer-backed fixture with an authored approximation.
To replace only the merged DOCX pair, run
`python -m theorem_ml.extract.bench.render_docx_fixture --soffice PATH`.
The live Docling comparison loads real PDF layout/table weights. Paddle parity
uses this same DOCX/page-image pair by default in `parse/test_live_parsers.py`.

For real model measurements, configure the authenticated schema MCP and the
Paddle vLLM endpoint, and supply the tenant's benchmark object type. Its fields
are `reference: text`, `person: relation`, `amount: number`, and `deadline: date`;
mark the desired scalar fields `span: true` when evaluating their GLiNER spans.
The name field's label is generated from the effective schema on each run.

The checked-in setup runner creates `ExtractionBenchPerson` and
`ExtractionBenchLease` through the existing `schema_declare` tool. It creates
the corpus's ten people using `create_one_extraction_bench_person`, reads each
back with `find_one_extraction_bench_person`, and checks returned tenant, name,
and ID before writing `artifacts/extraction-bench/entity-gold.json`. IDs come
from the authority; they are never guessed or taken from resolver predictions.
Declarations and content-addressed record creation are idempotent; conflicting
existing declarations are refused. This command writes benchmark records into
the authenticated tenant, so select the intended benchmark tenant explicitly.

```bash
export THEOREM_LIVE_TENANT=TENANT
export THEOREM_SCHEMA_MCP_URL=https://YOUR_AUTHORITY/mcp
export THEOREM_SCHEMA_MCP_TOKEN=YOUR_AUTHENTICATED_TOKEN
export THEOREM_PADDLE_VLLM_URL=https://YOUR_PADDLE_ENDPOINT/v1
python -m theorem_ml.extract.bench --bootstrap-schema
export THEOREM_EXTRACTION_MINERU_DIR=/path/to/mineru-results
python -m theorem_ml.extract.bench --all
```

`--all` defaults to `ExtractionBenchLease`, the bootstrap entity-gold path,
and the configured/default MLflow store. `THEOREM_LIVE_OBJECT_TYPE`,
`THEOREM_EXTRACTION_ENTITY_GOLD`, and `THEOREM_EXTRACTION_MINERU_DIR` override
those inputs. Bootstrap owns only its two benchmark types; unset an unrelated
`THEOREM_LIVE_OBJECT_TYPE` before running it. For a separately prepared schema,
provide its name and real identity gold directly:

```bash
python -m theorem_ml.extract.bench --all \
  --tenant TENANT --object-type BENCHMARK_TYPE \
  --mineru-dir /path/to/mineru-results \
  --entity-gold /path/to/tenant-entity-gold.json \
  --tracking-uri sqlite:////path/to/extraction-mlflow.db
python -m theorem_ml.executors list --tenant TENANT \
  --tracking-uri sqlite:////path/to/extraction-mlflow.db
```

MinerU is an external evaluation reference, never an installed production
dependency. Each `<document-id>.json` must contain `source_sha256` matching the
comparison PDF, a nonempty `model_version`, and the normalized `tree`.
Resolution gold has shape
`{"tenant":"...","documents":{"pdf-01":[{"field_key":"person","text":"Avery Morgan","entity_id":"..."}]}}`.
Its identifiers must come from the tenant's real golden entities.
Relation-field correctness compares returned entity IDs to those real gold IDs;
it does not compare graph IDs to human-readable names or promote predicted
names into identity gold.

`results.jsonl` contains one row per document per stage. Text, reading order,
table structure, merged cells, heading hierarchy and span recovery remain
separate metrics. Field scoring distinguishes omission from hallucination;
name equivalence uses authored aliases and array matching never reuses an item.
Atom stability uses cosine alignment of actual dependency-encoder embeddings
over three executions. Relex predictions are evaluated but never written.

Missing dependencies, models, references, schema or gold remain missing
evidence. The CLI exits nonzero; affected MLflow stage runs are failed. Executor
listing reports measured fitness only from finished harness runs, otherwise
`null`. No constant substitutes for a measured fitness value.

Measurement and acceptance are separate. `thresholds.json` is a checked-in
engineering policy chosen before the full model run, with a reason for each
floor. It is not a claim about published benchmark performance. Production
stages have quality floors; Docling, MinerU and Relex remain measured comparison
columns. O2.2 requires Paddle's reading order to trail MinerU by at most .02 on
the ten multi-column documents. O5.1 requires atom cosine stability strictly
above .99 and nonempty atom sets on every eligible authored document. Merged
tables have a separate subset gate so empty-table sources cannot dilute errors.

`acceptance.json` records every check and the policy digest. A low measured
score stays `measured`, with acceptance `failed_quality`; absent evidence has
acceptance `missing_evidence`. Both make the CLI exit nonzero. An MLflow run
with all measurements is `FINISHED` even if quality failed, and its independent
`theorem.acceptance` tag records that failure so poor scores remain visible.
The markdown projection includes acceptance alongside the per-metric results.

The admission pipeline observes actual Paddle page calls, GLiNER2 inference,
nonempty relation resolution and nonempty MiniCheck verification in
`theorem-extraction-runtime/TENANT`. Scheduled claim escalation observes its
MiniCheck call too. Run IDs persist in `ExtractionRun.output.executor_runs`,
including failed calls; they do not introduce a receipt variant. Runtime runs
measure elapsed time and link the latest successful harness fitness separately.
They do not report quality measured against unknown production gold. Native-only
parsing, absent span labels and empty claim/relation lists create no corresponding
model runs. `MLFLOW_TRACKING_URI` configures storage; the default is the local
SQLite file `BASE_DIR/var/mlflow.db`, excluded from version control.
