"""Contract and local-worker oracles for theorem.extraction.v1."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts/theorem.extraction.v1.json"
FIXTURE_PATH = ROOT / "contracts/theorem.extraction.v1.fixture.json"
WORKER_PATH = ROOT / "runpod/theorem_extraction_worker/worker.py"


def _worker():
    spec = importlib.util.spec_from_file_location("theorem_extraction_worker", WORKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_prompt_hashes_and_checksum_match_bytes():
    contract_bytes = CONTRACT_PATH.read_bytes()
    contract = json.loads(contract_bytes)
    for prompt in contract["prompts"].values():
        assert hashlib.sha256(prompt["text"].encode("utf-8")).hexdigest() == prompt[
            "sha256"
        ]
    checksum_lines = (ROOT / "contracts/CHECKSUMS").read_text().splitlines()
    expected = {
        path: digest
        for digest, path in (line.split(maxsplit=1) for line in checksum_lines if line.strip())
    }
    assert expected["contracts/theorem.extraction.v1.json"] == hashlib.sha256(
        contract_bytes
    ).hexdigest()
    assert contract["model"]["default_id"] == "google/gemma-4-12B-it"
    assert contract["package"] == {
        "atlas_rag": "0.0.5.post1",
        "pyarrow": "25.0.1",
        "runpod": "1.12.0",
        "vllm": "0.28.0",
    }


def test_contract_carries_exact_19_column_arrow_schema():
    fields = json.loads(CONTRACT_PATH.read_text())["arrow_schema"]["fields"]
    assert [field["name"] for field in fields] == [
        "tenant_id",
        "job_id",
        "shard",
        "stage",
        "passage_id",
        "span_start",
        "span_end",
        "subject",
        "subject_kind",
        "predicate",
        "object",
        "object_kind",
        "object_type_id",
        "record_json",
        "confidence",
        "model_id",
        "prompt_hash",
        "schema_hash",
        "extractor_version",
    ]
    assert [field["nullable"] for field in fields] == [
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
    ]


def test_stub_replay_matches_atlas_and_typed_fixture_digests():
    worker = _worker()
    fixture = json.loads(FIXTURE_PATH.read_text())
    payload = base64.b64decode(fixture["input"]["payload_base64"], validate=True)
    client = worker.FixtureModelClient(fixture)
    for operation, params_key, expected_key in (
        ("data_science.extraction.atlas", "params", "atlas_output"),
        ("data_science.extraction.typed", "typed_params", "typed_output"),
    ):
        request = worker._local_request(
            fixture,
            operation=operation,
            max_bytes=worker.MAX_ARTIFACT_BYTES,
        )
        request["params"] = fixture[params_key]
        result = worker.execute(
            request,
            input_payload=payload,
            upload=None,
            fixture_client=client,
        )
        assert result["output"] == {
            key: fixture["expected"][expected_key][key]
            for key in ("schema_json", "rows", "payload_digest")
        }


def test_shared_triple_has_distinct_passages_and_spans():
    rows = json.loads(FIXTURE_PATH.read_text())["expected"]["atlas_rows"]
    shared = [
        row
        for row in rows
        if (row["subject"], row["predicate"], row["object"])
        == ("Fett Law", "declined", "representation")
    ]
    assert {row["passage_id"] for row in shared} == {
        "passage:law:1",
        "passage:law:2",
    }
    assert all(row["span_start"] is not None and row["span_end"] is not None for row in shared)
    assert len({(row["passage_id"], row["span_start"]) for row in shared}) == 2


def test_typed_fixture_record_conforms_to_declared_shape():
    fixture = json.loads(FIXTURE_PATH.read_text())
    row = fixture["expected"]["typed_rows"][0]
    record = json.loads(row["record_json"])
    schema = fixture["typed_params"]["object_type"]["schema"]
    item = schema["properties"]["records"]["items"]
    assert set(record) == set(item["required"])
    assert row["subject"] == record[
        fixture["typed_params"]["object_type"]["label_identifier_field"]
    ]
    assert row["object_type_id"] == fixture["typed_params"]["object_type"][
        "object_type_id"
    ]


def test_oversize_output_refuses_before_upload():
    worker = _worker()
    fixture = json.loads(FIXTURE_PATH.read_text())
    payload = base64.b64decode(fixture["input"]["payload_base64"], validate=True)
    request = worker._local_request(
        fixture,
        operation="data_science.extraction.atlas",
        max_bytes=len(payload) * 2,
    )
    uploads = []
    result = worker.execute(
        request,
        input_payload=payload,
        upload=lambda url, body: uploads.append((url, body)),
        fixture_client=worker.FixtureModelClient(fixture),
    )
    assert result == {"error": "output_exceeds_max_bytes"}
    assert uploads == []


def test_worker_image_uses_contract_pins_and_model_build_secret():
    dockerfile = (ROOT / "runpod/theorem_extraction_worker/Dockerfile").read_text()
    requirements = (
        ROOT / "runpod/theorem_extraction_worker/requirements.txt"
    ).read_text()
    assert "FROM vllm/vllm-openai:v0.28.0" in dockerfile
    assert "id=hf_token,required=true" in dockerfile
    assert "atlas-rag==0.0.5.post1" in requirements
    assert "vllm==0.28.0" in requirements
