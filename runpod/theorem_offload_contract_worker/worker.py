#!/usr/bin/env python3
"""RunPod Serverless boundary worker for Theorem data-science offload jobs.

The control plane sends an Arrow *descriptor*, not Arrow bytes.  This worker
therefore validates ``theorem.offload.v1`` at the provider boundary and refuses
to manufacture an output until the artifact-store handoff and per-operation
runners are available.  Returning an error is intentional: it keeps an
unimplemented computation from becoming a forged provenance success.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any


CONTRACT = "theorem.offload.v1"
SUPPORTED_OPERATIONS = frozenset(
    {
        "data_science.tabfm.label_aggregate",
        "data_science.gnn.denoise",
        "data_science.gnn.embed",
        "data_science.community.assign",
    }
)


def _input_error(message: str) -> dict[str, str]:
    return {"error": f"invalid {CONTRACT} input: {message}"}


def validate_input(value: Any) -> str | None:
    """Return a validation error, if any, for a versioned offload request."""
    if not isinstance(value, Mapping):
        return "job input must be an object"
    if value.get("contract") != CONTRACT:
        return f"contract must equal {CONTRACT!r}"
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        return "operation is not a supported Theorem Python offload operation"
    operation_id = value.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id.strip():
        return "operation_id must be a non-empty string"
    descriptor = value.get("input")
    if not isinstance(descriptor, Mapping):
        return "input must be an Arrow descriptor object"
    if not isinstance(descriptor.get("schema_json"), str):
        return "input.schema_json must be a string"
    rows = descriptor.get("rows")
    if rows is not None and (not isinstance(rows, int) or isinstance(rows, bool) or rows < 0):
        return "input.rows must be a non-negative integer or null"
    digest = descriptor.get("payload_digest")
    if not isinstance(digest, str) or not digest.strip():
        return "input.payload_digest must be a non-empty string"
    if not isinstance(value.get("params"), Mapping):
        return "params must be an object"
    return None


def handler(job: dict[str, Any]) -> dict[str, str]:
    """Validate a RunPod job and fail closed until its Arrow runner exists."""
    request = job.get("input")
    error = validate_input(request)
    if error:
        return _input_error(error)
    return {
        "error": (
            "Theorem Python offload worker accepted theorem.offload.v1 but cannot "
            "execute this operation yet: artifact lookup/write and the operation-specific "
            "Arrow runner have not been configured. No output descriptor was produced."
        )
    }


def run_local(input_path: str) -> int:
    """Run the provider handler locally with a JSON request for contract checks."""
    payload = json.loads(open(input_path, encoding="utf-8").read())
    result = handler({"input": payload})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--local-input":
        raise SystemExit(run_local(sys.argv[2]))
    import runpod

    runpod.serverless.start({"handler": handler})
