"""Contract-bound behavior for the dedicated Theorem RunPod worker."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pyarrow as pa


WORKER_PATH = (
    Path(__file__).resolve().parents[1]
    / "runpod"
    / "theorem_offload_contract_worker"
    / "worker.py"
)
SPEC = importlib.util.spec_from_file_location("theorem_offload_contract_worker", WORKER_PATH)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def _edge_payload() -> bytes:
    return worker._encode_arrow(
        pa.table(
            {
                "source": pa.array(["a", "b", "d"], type=pa.string()),
                "target": pa.array(["b", "c", "e"], type=pa.string()),
            }
        )
    )


def _request(*, payload: bytes | None = None, **overrides):
    payload = payload or _edge_payload()
    input_table = worker._decode_arrow(payload)
    request = {
        "contract": "theorem.offload.v1",
        "operation": "data_science.gnn.embed",
        "operation_id": "operation-1",
        "input": {
            "schema_json": worker._schema_json(input_table.schema),
            "rows": input_table.num_rows,
            "payload_digest": worker._sha256_digest(payload),
            "artifact_key": "tenants/tenant/inputs/input.arrow",
            "read_url": "https://example.test/input",
        },
        "output": {
            "artifact_key": "tenants/tenant/outputs/output.arrow",
            "write_url": "https://example.test/output",
        },
        "params": {"dimensions": 8},
    }
    request.update(overrides)
    return request


def test_worker_rejects_unknown_contract_without_emitting_an_output():
    result = worker.handler({"input": _request(contract="other.contract.v1")})

    assert "error" in result
    assert "invalid theorem.offload.v1 input" in result["error"]
    assert "output" not in result


def test_worker_rejects_invalid_descriptor_without_emitting_an_output():
    result = worker.handler(
        {"input": _request(input={"schema_json": "{}", "rows": -1, "payload_digest": "x"})}
    )

    assert "error" in result
    assert "input.rows" in result["error"]
    assert "output" not in result


def test_worker_refuses_registered_operations_without_a_real_runner():
    result = worker.handler({"input": _request()})

    assert "error" in result
    assert "no real Arrow runner" in result["error"]
    assert "output" not in result


def test_worker_executes_community_assignment_through_signed_artifact_capabilities(monkeypatch):
    payload = _edge_payload()
    uploaded = {}
    monkeypatch.setattr(
        worker,
        "_download",
        lambda url, *, max_bytes: payload,
    )
    monkeypatch.setattr(
        worker,
        "_upload",
        lambda url, body, *, max_bytes: uploaded.update(url=url, body=body),
    )

    result = worker.handler(
        {"input": _request(payload=payload, operation="data_science.community.assign")}
    )

    assert "error" not in result
    assert uploaded["url"] == "https://example.test/output"
    output = worker._decode_arrow(uploaded["body"])
    assert output.to_pydict() == {
        "node": ["a", "b", "c", "d", "e"],
        "community_id": [0, 0, 0, 1, 1],
    }
    assert result["output"]["payload_digest"] == worker._sha256_digest(uploaded["body"])
