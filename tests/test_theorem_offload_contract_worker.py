"""Contract-bound behavior for the dedicated Theorem RunPod worker."""

from __future__ import annotations

import importlib.util
from pathlib import Path


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


def _request(**overrides):
    request = {
        "contract": "theorem.offload.v1",
        "operation": "data_science.gnn.embed",
        "operation_id": "operation-1",
        "input": {"schema_json": "{}", "rows": 3, "payload_digest": "sha256:input"},
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


def test_worker_validates_a_registered_operation_but_refuses_synthetic_success():
    result = worker.handler({"input": _request()})

    assert "error" in result
    assert "cannot execute" in result["error"]
    assert "output" not in result
