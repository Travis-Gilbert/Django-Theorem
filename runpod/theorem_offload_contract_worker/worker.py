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
from urllib.request import Request, urlopen

import pyarrow as pa


CONTRACT = "theorem.offload.v1"
SUPPORTED_OPERATIONS = frozenset(
    {
        "data_science.tabfm.label_aggregate",
        "data_science.gnn.denoise",
        "data_science.gnn.embed",
        "data_science.community.assign",
    }
)
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
ARROW_IPC_CONTENT_TYPE = "application/vnd.apache.arrow.stream"


def _input_error(message: str) -> dict[str, str]:
    return {"error": f"invalid {CONTRACT} input: {message}"}


def _sha256_digest(payload: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _is_sha256_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _schema_json(schema: pa.Schema) -> str:
    metadata = {
        key.decode("utf-8", errors="replace"): value.decode("utf-8", errors="replace")
        for key, value in sorted((schema.metadata or {}).items())
    }
    return json.dumps(
        {
            "fields": [
                {"name": field.name, "type": str(field.type), "nullable": field.nullable}
                for field in schema
            ],
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_arrow(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _decode_arrow(payload: bytes) -> pa.Table:
    try:
        return pa.ipc.open_stream(pa.py_buffer(payload)).read_all()
    except (pa.ArrowException, ValueError, OSError) as exc:
        raise ValueError("input artifact is not a valid Arrow IPC stream") from exc


def _download(url: str) -> bytes:
    with urlopen(url, timeout=60) as response:  # noqa: S310 - URL is a signed capability from Django.
        payload = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("input artifact exceeds the worker maximum")
    return payload


def _upload(url: str, payload: bytes) -> None:
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("output artifact exceeds the worker maximum")
    request = Request(
        url,
        data=payload,
        method="PUT",
        headers={"Content-Type": ARROW_IPC_CONTENT_TYPE},
    )
    with urlopen(request, timeout=60):  # noqa: S310 - URL is a signed capability from Django.
        pass


def _community_assign(table: pa.Table) -> pa.Table:
    """Assign deterministic undirected connected-component ids to string edge endpoints."""
    required = {"source", "target"}
    missing = required.difference(table.column_names)
    if missing:
        raise ValueError(f"community.assign requires columns: {', '.join(sorted(missing))}")
    source = table["source"].combine_chunks()
    target = table["target"].combine_chunks()
    if not pa.types.is_string(source.type) or not pa.types.is_string(target.type):
        raise ValueError("community.assign source and target must be Arrow string columns")
    sources = source.to_pylist()
    targets = target.to_pylist()
    if any(value is None for value in sources) or any(value is None for value in targets):
        raise ValueError("community.assign source and target cannot contain nulls")

    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for left, right in zip(sources, targets, strict=True):
        union(left, right)

    nodes = sorted(parent)
    roots = sorted({find(node) for node in nodes})
    community_ids = {root: index for index, root in enumerate(roots)}
    return pa.table(
        {
            "node": pa.array(nodes, type=pa.string()),
            "community_id": pa.array([community_ids[find(node)] for node in nodes], type=pa.int64()),
        }
    )


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
    if not _is_sha256_digest(digest):
        return "input.payload_digest must be a sha256 digest"
    if not isinstance(descriptor.get("artifact_key"), str) or not descriptor["artifact_key"]:
        return "input.artifact_key must be a non-empty string"
    if not isinstance(descriptor.get("read_url"), str) or not descriptor["read_url"].startswith("https://"):
        return "input.read_url must be an HTTPS signed URL"
    output = value.get("output")
    if not isinstance(output, Mapping):
        return "output must be an object"
    if not isinstance(output.get("artifact_key"), str) or not output["artifact_key"]:
        return "output.artifact_key must be a non-empty string"
    if not isinstance(output.get("write_url"), str) or not output["write_url"].startswith("https://"):
        return "output.write_url must be an HTTPS signed URL"
    if not isinstance(value.get("params"), Mapping):
        return "params must be an object"
    return None


def handler(job: dict[str, Any]) -> dict[str, Any]:
    """Execute a real Arrow operation through one-time signed object capabilities."""
    request = job.get("input")
    error = validate_input(request)
    if error:
        return _input_error(error)
    assert isinstance(request, Mapping)
    descriptor = request["input"]
    output = request["output"]
    assert isinstance(descriptor, Mapping)
    assert isinstance(output, Mapping)
    operation = request["operation"]
    if operation != "data_science.community.assign":
        return {
            "error": (
                f"Theorem Python offload worker has no real Arrow runner for {operation}; "
                "no output descriptor was produced."
            )
        }
    try:
        input_payload = _download(str(descriptor["read_url"]))
        if _sha256_digest(input_payload) != descriptor["payload_digest"]:
            raise ValueError("input artifact digest does not match the descriptor")
        input_table = _decode_arrow(input_payload)
        if _schema_json(input_table.schema) != descriptor["schema_json"]:
            raise ValueError("input artifact schema does not match the descriptor")
        if input_table.num_rows != descriptor["rows"]:
            raise ValueError("input artifact row count does not match the descriptor")
        output_table = _community_assign(input_table)
        output_payload = _encode_arrow(output_table)
        _upload(str(output["write_url"]), output_payload)
    except (OSError, ValueError) as exc:
        return {"error": f"{CONTRACT} execution failed: {exc}"}
    return {
        "output": {
            "schema_json": _schema_json(output_table.schema),
            "rows": output_table.num_rows,
            "payload_digest": _sha256_digest(output_payload),
        }
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
