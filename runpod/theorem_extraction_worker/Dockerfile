# syntax=docker/dockerfile:1.7
FROM vllm/vllm-openai:v0.28.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    THEOREM_EXTRACTION_CONTRACT=/app/contracts/theorem.extraction.v1.json \
    THEOREM_MODEL_PATH=/models/gemma-4-12B-it

USER root
WORKDIR /app

COPY runpod/theorem_extraction_worker/requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

COPY contracts/theorem.extraction.v1.json /app/contracts/theorem.extraction.v1.json
COPY runpod/theorem_extraction_worker/worker.py /app/worker.py
COPY runpod/theorem_extraction_worker/entrypoint.sh /app/entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh

RUN --mount=type=secret,id=hf_token,required=true \
    python - <<'PY'
import json
from importlib.metadata import version
from pathlib import Path

from huggingface_hub import snapshot_download

contract = json.loads(Path("/app/contracts/theorem.extraction.v1.json").read_text())
for package, expected in contract["package"].items():
    distribution = "atlas-rag" if package == "atlas_rag" else package
    actual = version(distribution)
    if actual != expected:
        raise SystemExit(f"{distribution} version mismatch: {actual} != {expected}")
token = Path("/run/secrets/hf_token").read_text().strip()
if not token:
    raise SystemExit("hf_token build secret is empty")
snapshot_download(
    repo_id=contract["model"]["default_id"],
    local_dir="/models/gemma-4-12B-it",
    token=token,
)
PY

ENTRYPOINT ["/app/entrypoint.sh"]
