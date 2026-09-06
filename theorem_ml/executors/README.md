# Index learned executors

`POST /internal/index/execute` admits the existing machine key with
`offload:invoke`. Its verified tenant slug is authoritative. The JSON body has
`executor: "discovery" | "corpus_filing"` plus the executor input below. Missing
dependencies return an unavailable response; there is no production stand-in.

Local entry:

```bash
python -m theorem_ml.executors --tenant Travis-Gilbert --executor discovery \
  --input batch.json --tracking-uri http://localhost:5000
```

Install `.[index]` for discovery/MLflow and `.[index-corpus]` for the pinned real
TabICLv2 implementation. Configure `MLFLOW_TRACKING_URI`; each execution records
its own MLflow run, tenant, executor tier, input/output digests and held-out
filing accuracy when labels exist. Failed runs are recorded as failed. Runs
without held-out labels explicitly report fitness as unmeasured. Raw documents
are not copied into MLflow artifacts.

## Rust input and output boundary

The Rust caller materializes live admitted records before invoking Django. No
embedding is manufactured when ingestion has not produced one.

Discovery input:

```json
{
  "items": [{"id":"item-a","tenant_id":"Travis-Gilbert","embedding":[0.1,0.2],"title":"Rust compiler","text":"Optional body","entities":["entity-rust"]}],
  "edges": [],
  "neighbors": 8,
  "resolution": 1.0,
  "training_filings": {},
  "held_out_filings": {}
}
```

Optional `entity_nodes` materialize related entities as `{id,tenant_id,name}`.
Edges join admitted batch items or these entity nodes, with `tenant_id`, `from_id`,
`to_id`, optional nonnegative `weight`, `tombstone`, and `epistemic_status`.
`training_filings` and `held_out_filings` map disjoint batch IDs to collections;
labels never enter community detection, only the held-out fitness calculation.
Names use extracted terms/entities and make no generative call.

The output contains `proposals` with `proposal_id`, `name`, `members`,
`top_terms`, `top_entities`, and `cohesion`; `proposal_nodes` carry the exact
`ProposedCollection` graph node payloads including tenant, proposed state and
MLflow run linkage. The Rust caller persists these nodes and owns
accept/rename/dismiss. A returned proposal is not an accepted collection.

Corpus input has `items`, disjoint `held_out`, and `feature_layout`. Every row
contains `id`, `tenant_id`, `collection`, and `features` in the Rust-derived
layout's exact coordinates. Layout wire JSON is the serialized Rust
`FeatureLayout`: `object_type`, `schema_anchor`, `feature_dim`, and contiguous
`blocks` with `field`, `offset`, and tagged `kind`. Exactly one nonempty embedding
block provides the prototype dimensions.

Corpus output has `updates: [{collection, prototype, prior}]`, the schema anchor,
held-out fitness and MLflow linkage. Apply the complete update vector through
Rust `T1Head::seed_prototypes` once per pass. Neither executor writes item
membership or calls filing receipts.

## TabICLv2 provenance and caching

Source is pinned to
[`0dbff3ec8fc68c123c87af77b0ea8b25cd2d23f3`](https://github.com/soda-inria/tabicl/tree/0dbff3ec8fc68c123c87af77b0ea8b25cd2d23f3).
The official classifier checkpoint is
`tabicl-classifier-v2-20260212.ckpt`, from
[weights revision `4dcd344ece2c00be9e831fdd35bed57b5ad83e19`](https://huggingface.co/jingang/TabICL/tree/4dcd344ece2c00be9e831fdd35bed57b5ad83e19),
SHA-256 `bdc7dbd5e4ff21f8f0456fcf90c6b7cdf72dbea960f2d05b19bec19f9b3d4ed0`.
Set `THEOREM_TABICL_CHECKPOINT` to that verified file and optionally
`THEOREM_TABICL_DEVICE` (`cpu` by default). Auto-download is disabled.
The core source and official model card declare BSD-3-Clause.

Upstream rejects KV caching beyond the native ten-class limit. This executor
uses the native cached classifier through ten classes and an explicit cached
binary one-vs-rest ensemble above ten. Each context is fitted once, then reused
for held-out inference and prototype construction. This decomposition is not
claimed numerically equivalent to upstream's uncached many-class strategy.

## Verification and remaining live obligations

```bash
pytest theorem_ml/executors -k 'discovery or corpus'
```

Local tests cover actual community detection, real MLflow persistence, HTTP
machine-key admission, layout and prototype algebra. The explicitly named
`RecordingBinaryEstimator` checks wrapper call topology only; it is not a
TabICL model or a model-quality oracle.

Real model smoke on explicitly synthetic data:

```bash
THEOREM_INDEX_RUN_TABICL_SMOKE=1 pytest theorem_ml/executors/test_corpus_filing.py \
  -k real_tabicl_cached_smoke
```

This exercises the actual pinned checkpoint for both two-class native caching
and eleven-class cached one-vs-rest, with at least 300 context rows. It is
runtime proof, separate from the private 784-item fixture gate below.

The real corpus gate requires `THEOREM_INDEX_784_FIXTURE`,
`THEOREM_TABICL_CHECKPOINT`, `MLFLOW_TRACKING_URI`, and
`THEOREM_INDEX_T1_ORACLE`. The fixture contains `tenant_id`, `discovery`,
`corpus`, `accepted_proposals` (proposal ID to accepted collection ID), and the
measured `tier1_fixture_accuracy`. Context filings must agree with the accepted
proposal membership rather than bypassing discovery. The Rust oracle executable
reads `{feature_layout,updates,held_out}` JSON on stdin and returns
`{generation_before,generation_after,accuracy}`. The test requires exactly 784
discovery rows, the baseline accuracy floor and exactly one generation advance.
Missing configuration skips this gate explicitly.

The canonical labeled 784-item export corpus was not present in the inspected
Theorem/Django checkouts or targeted Downloads search. Rust must still connect
its ingest embedding pipeline, proposal persistence/acceptance and atomic
prototype application. A local small-model smoke does not discharge those
requirements or establish hosted deployment.
