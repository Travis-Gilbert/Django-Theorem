# Theorem Offload Contract Worker

This is the dedicated RunPod Serverless boundary for Django-Theorem's Python
data-science operations. It accepts only `theorem.offload.v1` and validates a
byte-free Arrow descriptor. It receives only time-limited, tenant-scoped
capabilities instead of an object-store credential:

1. a presigned GET for the content-addressed Arrow input and a presigned PUT
   for the Django-selected output key in the private `theorem-artifacts` Neon
   Object Storage bucket; and
2. the implemented `data_science.community.assign` runner, which returns
   deterministic connected components from string `source`/`target` edges.

TabFM and GNN requests still return explicit errors. Django re-downloads every
claimed output and checks its digest, schema, and row count before it records a
successful computation/provenance record.

Build and publish via the `publish-theorem-offload-worker` workflow. If GitHub
Actions is unavailable, the `fly.image.toml` configuration supports a remote,
image-only Fly build with `fly deploy --build-only --push`; it creates no Fly
Machine. Configure the resulting immutable image reference as
`RUNPOD_WORKER_IMAGE_DIGEST` in the Fly control-plane app and select that exact
image for the RunPod endpoint.
