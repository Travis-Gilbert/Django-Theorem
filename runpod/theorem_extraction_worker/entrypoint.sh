#!/usr/bin/env bash
set -Eeuo pipefail

contract_path="${THEOREM_EXTRACTION_CONTRACT:-/app/contracts/theorem.extraction.v1.json}"
model_id="$(python -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["model"]["default_id"])' "$contract_path")"
model_path="${THEOREM_MODEL_PATH:-/models/gemma-4-12B-it}"

cleanup() {
  local exit_code=$?
  if [[ -n "${worker_pid:-}" ]]; then
    kill "$worker_pid" 2>/dev/null || true
  fi
  if [[ -n "${vllm_pid:-}" ]]; then
    kill "$vllm_pid" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

vllm serve "$model_path" \
  --served-model-name "$model_id" \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len "${VLLM_MAX_MODEL_LEN:-8192}" \
  --limit-mm-per-prompt '{"image":0,"video":0,"audio":0}' &
vllm_pid=$!

python - <<'PY'
import json
import time
from urllib.error import URLError
from urllib.request import urlopen

deadline = time.monotonic() + 900
while time.monotonic() < deadline:
    try:
        with urlopen("http://127.0.0.1:8000/v1/models", timeout=2) as response:
            if response.status == 200:
                json.load(response)
                raise SystemExit(0)
    except (OSError, URLError, ValueError):
        time.sleep(2)
raise SystemExit("vLLM did not become ready within 900 seconds")
PY

python -u /app/worker.py &
worker_pid=$!
wait -n "$vllm_pid" "$worker_pid"
