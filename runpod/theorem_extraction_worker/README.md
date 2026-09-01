# Theorem extraction worker

This RunPod Serverless image implements `data_science.extraction.atlas` and
`data_science.extraction.typed` through the existing `theorem.offload.v1`
descriptor boundary. The image starts a pinned vLLM server on loopback before
starting the RunPod handler, so GPU time is not spent waiting on a hosted model
API.

The build uses the official `vllm/vllm-openai:v0.28.0` CUDA image and installs
the exact versions in `requirements.txt`. It reads the model id from
`contracts/theorem.extraction.v1.json`, downloads that snapshot during the
image build, and refuses the build if any package version differs from the
contract. Gemma access is supplied as a BuildKit secret and is not retained in
an image layer:

```text
fly deploy --config runpod/theorem_extraction_worker/fly.image.toml \
  --build-only --push --build-secret hf_token="$HF_TOKEN"
```

The local deterministic replay is explicitly a fixture oracle, not live model
evidence:

```text
python runpod/theorem_extraction_worker/worker.py \
  --local-input contracts/theorem.extraction.v1.fixture.json --stub-model
```

Production requests contain only tenant-scoped presigned GET/PUT URLs. The
worker verifies the input digest, exact Arrow schema, row count, and extraction
contract before invoking ATLAS or guided typed extraction. Output bytes are
checked against `max_bytes` before upload; oversize output returns
`output_exceeds_max_bytes` and uploads nothing.
