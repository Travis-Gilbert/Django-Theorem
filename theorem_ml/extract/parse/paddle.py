"""Official Paddle page pipeline; the VLM recognition stage uses vLLM.

Generic OCR chat completions do not include Paddle's layout/order pipeline.
The serving endpoint is checked for the named model. The standard vLLM models
API does not attest a checkpoint digest, so receipts do not claim one. Deploy
with the pinned MODEL_REVISION and retain that separate deployment receipt.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..elements import DocElementTree, from_paddle


MODEL_ID = "PaddlePaddle/PaddleOCR-VL-1.6"
MODEL_REVISION = "c5630abae1d940eafe0697512a0325494b02ab42"


class PaddleParser:
    parser_id = "parse.paddle"
    version = "PaddleOCR-VL-1.6"

    def __init__(self, server_url: str | None = None) -> None:
        self.server_url = server_url or os.environ.get("THEOREM_PADDLE_VLLM_URL", "")
        self._pipeline = None

    def _load(self) -> None:
        if self._pipeline is not None:
            return
        if urlparse(self.server_url).scheme not in {"http", "https"}:
            raise RuntimeError("Configure THEOREM_PADDLE_VLLM_URL with the vLLM /v1 endpoint")
        # A server's model identity must not silently substitute 1.5 or latest.
        with httpx.Client(timeout=30) as client:
            response = client.get(self.server_url.rstrip("/") + "/models")
            response.raise_for_status()
            models = response.json().get("data", [])
        if not any(m.get("id") in {MODEL_ID, "PaddleOCR-VL-1.6-0.9B"} for m in models):
            raise RuntimeError("The vLLM server does not advertise PaddleOCR-VL-1.6")
        from paddleocr import PaddleOCRVL

        self._pipeline = PaddleOCRVL(
            pipeline_version="v1.6", vl_rec_backend="vllm-server",
            vl_rec_server_url=self.server_url,
        )

    def parse(self, path: Path, source_id: str, *, page: int = 1) -> DocElementTree:
        self._load()
        results = list(self._pipeline.predict(str(path)))
        if len(results) != 1:
            raise RuntimeError("Paddle page parser expected exactly one page result")
        return from_paddle(results[0], source_id, page=page)
