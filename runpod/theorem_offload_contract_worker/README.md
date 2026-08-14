# Theorem Offload Contract Worker

This is the dedicated RunPod Serverless boundary for Django-Theorem's Python
data-science operations. It accepts only `theorem.offload.v1`, validates the
byte-free Arrow descriptor, and deliberately returns an error until both parts
of the real execution seam are implemented:

1. content-addressed Arrow artifact lookup and output write through the private
   `theorem-artifacts` Neon Object Storage bucket; and
2. an operation-specific runner for each registered TabFM, GNN, and community
   operation.

It must not return a synthetic descriptor. A successful RunPod response is
treated as a derivation by the control plane, so success without execution would
forge provenance.

Build and publish via the `publish-theorem-offload-worker` workflow. If GitHub
Actions is unavailable, the `fly.image.toml` configuration supports a remote,
image-only Fly build with `fly deploy --build-only --push`; it creates no Fly
Machine. Configure the resulting immutable image reference as
`RUNPOD_WORKER_IMAGE_DIGEST` in the Fly control-plane app and select that exact
image for the RunPod endpoint.
